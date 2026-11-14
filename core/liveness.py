"""Liveness history: receiver and ingest heartbeats over time (the `liveness` table).

A heartbeat file only shows a receiver was alive when it last wrote it. To call a window
quiet, /v1/rail/activity (api/rail_verdicts.py) must show that, for the WHOLE window, the
receiver was listening to the channel and the ingest stage was turning its output into rows.
The ingest stage (pulse-transcribe's `sdr_files` and `traintap` sources) records both, about
every INGEST_HEARTBEAT_S, as runs:

  component    'ingest:<source>' or 'receiver:<name>'
  fingerprint  ingest: '' while the source can see its input (watch dir / CSV), else
               'blind:<why>'. receivers: '' for a plain heartbeat file (it speaks for every
               channel of its receiver), else the 'label@freq' pairs of rtl_airband's stats file
  up_since     the first beat of an unbroken run
  last_seen    the newest beat of that run
  detail       jsonb: the newest beat's details (ingest: `recording_now`, receivers: `pairs`)

A beat extends the newest run when its fingerprint is unchanged, it comes no more than the
component's own max gap after the run's last beat, and (rtl_airband) none of the stats file's
counters went down; anything else starts a new run. The max gap follows how often the component
beats, so an outage longer than a missed beat or two is never bridged:

  ingest:<source>   2 x INGEST_HEARTBEAT_S + 5 s (35 s)
  receiver:<name>   its heartbeat interval + INGEST_HEARTBEAT_S (the sampling) + half the interval,
                    at least 15 s: 45 s for rtl_airband's stats file (every 15 s), 105 s for
                    traintap's heartbeat (once a minute). The interval goes by the file
                    (RECEIVER_STATS_INTERVAL_S for <name>.prom, RECEIVER_HEARTBEAT_INTERVAL_S for
                    <name>.heartbeat), or `receivers: {<name>: {heartbeat_s: N}}` in
                    channels(.local).yaml.

A receiver beat is its heartbeat file's mtime, so a receiver that stops writing never extends
its run; rtl_airband's counters only grow while it runs, so one that went down shows a restart
however quickly it came back; and a gap in sampling (the ingest stage itself was down) starts a
new run too. Continuity is never assumed, and `up_since` is the earliest time Pulse can vouch for.

An ingest beat's time is when its poll began: everything its input held by then (files, CSV
rows) is in the database or in `recording_now`, so the newest beat is how far the ingest stage
has looked (/v1/rail/activity ends a quiet window there).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import text

from core.config_files import load_receiver_intervals
from core.receivers import Heartbeat, Pairs, pairs_cover, read_heartbeat
from core.settings import Settings, get_settings

log = structlog.get_logger()

_last_prune = 0.0


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def ingest_component(source: str) -> str:
    return f"ingest:{source}"


def receiver_component(name: str) -> str:
    return f"receiver:{name}"


def pairs_fingerprint(pairs: Pairs) -> str:
    return ";".join(f"{label or ''}@{f:.4f}" for label, f in pairs)


# ── how long a gap between two beats may be ─────────────────────────────────

def ingest_max_gap_s(s: Settings | None = None) -> float:
    """The ingest stage beats about every INGEST_HEARTBEAT_S: more than two missed beats ends its run."""
    s = s or get_settings()
    return 2 * s.ingest_heartbeat_s + 5.0


def receiver_interval_s(name: str, stats: bool, s: Settings | None = None) -> float:
    """How often receiver `name` rewrites its heartbeat file: `receivers.<name>.heartbeat_s`, else
    by the kind of file (a stats file: rtl_airband's stats_update_interval; else traintap's minute)."""
    s = s or get_settings()
    configured = load_receiver_intervals().get(name)
    if configured:
        return configured
    return s.receiver_stats_interval_s if stats else s.receiver_heartbeat_interval_s


def receiver_max_gap_s(name: str, stats: bool, s: Settings | None = None) -> float:
    """The longest gap between two of receiver `name`'s heartbeats, as the ingest stage samples them
    (every INGEST_HEARTBEAT_S), that still counts as one unbroken run: one interval, plus the
    sampling, plus half an interval (at least 15 s) for jitter."""
    s = s or get_settings()
    interval = receiver_interval_s(name, stats, s)
    return interval + s.ingest_heartbeat_s + max(15.0, interval / 2)


def max_gap_for(component: str, fingerprint: str, s: Settings | None = None) -> float:
    """A component's max gap ('receiver:<name>' with a stats fingerprint or not; else ingest)."""
    if component.startswith("receiver:"):
        return receiver_max_gap_s(component[len("receiver:"):], bool(fingerprint), s)
    return ingest_max_gap_s(s)


def restarted(before: dict, now: dict) -> bool:
    """Did a counter go down (`before`: the run's last counters; `now`: the new read's, complete lines
    only)? rtl_airband's counters only grow while it runs, so that is a restart. A counter missing
    from `now` (a read mid-rewrite) proves nothing either way."""
    return any(k in now and int(now[k]) < int(v) for k, v in (before or {}).items())


def continues(last_seen: datetime, fingerprint: str, detail: dict | None, at: datetime, new_fingerprint: str,
              counters: dict | None, max_gap_s: float) -> bool:
    """Does a beat at `at` extend the run whose newest beat was at `last_seen`?"""
    if new_fingerprint != fingerprint or (_utc(at) - _utc(last_seen)).total_seconds() > max_gap_s:
        return False
    return not (counters and restarted((detail or {}).get("counters") or {}, counters))


def beat(conn, component: str, at: datetime, fingerprint: str = "", detail: dict | None = None,
         max_gap_s: float | None = None, counters: dict | None = None) -> bool:
    """Record one beat; returns True when it started a new run. `conn` is a transaction.
    `max_gap_s` defaults to the component's own (max_gap_for); `counters`: rtl_airband's, all of
    them (a counter that went down since the run's last beat starts a new run)."""
    max_gap_s = max_gap_for(component, fingerprint) if max_gap_s is None else max_gap_s
    at = _utc(at)
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:c))"), {"c": component})
    last = conn.execute(text("""SELECT up_since, last_seen, fingerprint, detail FROM liveness WHERE component = :c
                                ORDER BY up_since DESC LIMIT 1"""), {"c": component}).first()
    payload = json.dumps(detail or {})
    if last is not None and at <= _utc(last.last_seen):
        return False                     # the same (or an older) heartbeat seen again: nothing new
    before = dict(last.detail or {}) if last is not None else {}
    if last is not None and continues(last.last_seen, last.fingerprint, before, at, fingerprint, counters, max_gap_s):
        if counters is not None and before.get("counters"):
            # keep the run's highest value of a counter this read missed (cut off mid-rewrite)
            payload = json.dumps({**(detail or {}), "counters": {**before["counters"],
                                                                 **(detail or {}).get("counters", {})}})
        conn.execute(text("""UPDATE liveness SET last_seen = :at, detail = CAST(:d AS jsonb)
                             WHERE component = :c AND up_since = :u"""),
                     {"at": at, "d": payload, "c": component, "u": last.up_since})
        return False
    conn.execute(text("""INSERT INTO liveness (component, up_since, last_seen, fingerprint, detail)
                         VALUES (:c, :at, :at, :fp, CAST(:d AS jsonb))
                         ON CONFLICT (component, up_since) DO UPDATE
                           SET last_seen = EXCLUDED.last_seen, fingerprint = EXCLUDED.fingerprint,
                               detail = EXCLUDED.detail"""),
                 {"c": component, "at": at, "fp": fingerprint, "d": payload})
    return True


def observe_receiver(conn, name: str, s: Settings | None = None) -> bool:
    """Record receiver `name`'s current heartbeat file, if it has one. A stats file that parses
    to nothing (read mid-rewrite) is skipped rather than recorded as a change."""
    s = s or get_settings()
    hb = read_heartbeat(name, s)
    if hb is None:
        return False
    if hb.is_stats and not hb.pairs:
        return False
    fp = pairs_fingerprint(hb.pairs) if hb.is_stats else ""
    detail = {"pairs": [[label, f] for label, f in hb.pairs], "file": hb.path.name}
    if any(hb.counters.values()):
        detail["counters"] = {k: v for k, v in hb.counters.items() if v}   # a zero can't go down
    beat(conn, receiver_component(name), hb.at, fp, detail, receiver_max_gap_s(name, hb.is_stats, s),
         counters=hb.counters)
    return True


def record_ingest(source: str, *, blind: str | None = None, detail: dict | None = None,
                  receivers: set[str] | frozenset[str] = frozenset(), engine=None,
                  at: datetime | None = None) -> None:
    """One ingest heartbeat for `source`, plus its receivers'. `at`: when the completed poll it
    reports on began (default now)."""
    global _last_prune
    from core.db import get_engine
    s = get_settings()
    now = datetime.now(timezone.utc)
    with (engine or get_engine()).begin() as c:
        beat(c, ingest_component(source), at or now, f"blind:{blind}" if blind else "", detail,
             ingest_max_gap_s(s))
        for name in sorted(receivers):
            observe_receiver(c, name, s)
        if time.monotonic() - _last_prune > 3600:
            _last_prune = time.monotonic()
            c.execute(text("DELETE FROM liveness WHERE last_seen < :t"),
                      {"t": now - timedelta(days=s.liveness_keep_days)})


# ── reading it back (the API) ───────────────────────────────────────────────

@dataclass
class Run:
    component: str
    up_since: datetime
    last_seen: datetime
    fingerprint: str
    detail: dict = field(default_factory=dict)

    @property
    def pairs(self) -> Pairs:
        return tuple((p[0], float(p[1])) for p in (self.detail.get("pairs") or []))

    @property
    def is_stats(self) -> bool:
        return self.fingerprint != ""

    def covers(self, ch, known: dict | None = None) -> bool:
        """Did the receiver, during this run, listen to `ch` (pairs_cover; a plain heartbeat
        speaks for every channel of its receiver)?"""
        return self.fingerprint == "" or pairs_cover(self.pairs, ch, known)


def runs_since(conn, components: list[str], since: datetime) -> dict[str, list[Run]]:
    """Every run of `components` that reaches into [since, now], newest first."""
    out: dict[str, list[Run]] = {c: [] for c in components}
    if not components:
        return out
    rows = conn.execute(text("""SELECT component, up_since, last_seen, fingerprint, detail FROM liveness
                                WHERE component = ANY(CAST(:cs AS text[])) AND last_seen >= :since
                                ORDER BY component, up_since DESC"""),
                        {"cs": list(components), "since": since}).mappings()
    for r in rows:
        out[r["component"]].append(Run(r["component"], _utc(r["up_since"]), _utc(r["last_seen"]),
                                       r["fingerprint"] or "", dict(r["detail"] or {})))
    return out


def current(runs: list[Run], now: datetime, max_gap_s: float) -> Run | None:
    """The run that is still going at `now` (its last beat within max_gap_s), if any."""
    if runs and (now - runs[0].last_seen).total_seconds() <= max_gap_s:
        return runs[0]
    return None


def file_continues(run: Run, hb: Heartbeat | None, max_gap_s: float) -> bool | None:
    """Does receiver heartbeat file `hb`, read now, continue `run` (the receiver's current run, as
    the ingest stage last recorded it)? True: same channel list, counters not reset, within the
    max gap. False: the receiver changed since (a restart or a re-render the ingest stage hasn't
    recorded yet). None: the file adds nothing newer (missing, not newer, or read mid-rewrite)."""
    if hb is None or hb.at <= run.last_seen or (hb.is_stats and not hb.pairs):
        return None
    fp = pairs_fingerprint(hb.pairs) if hb.is_stats else ""
    return continues(run.last_seen, run.fingerprint, run.detail, hb.at, fp, hb.counters, max_gap_s)
