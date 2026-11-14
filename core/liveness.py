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

A beat extends the newest run when its fingerprint is unchanged and it comes no more than
LIVENESS_MAX_GAP_S after the run's last beat; anything else starts a new run. A receiver beat
is its heartbeat file's mtime, so a receiver that stops writing never extends its run, and a
gap in sampling (the ingest stage itself was down) starts a new run too: continuity is never
assumed, and `up_since` is the earliest time Pulse can vouch for.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import text

from core.receivers import Pairs, pairs_cover, read_heartbeat
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


def beat(conn, component: str, at: datetime, fingerprint: str = "", detail: dict | None = None,
         max_gap_s: float | None = None) -> bool:
    """Record one beat; returns True when it started a new run. `conn` is a transaction."""
    max_gap_s = get_settings().liveness_max_gap_s if max_gap_s is None else max_gap_s
    at = _utc(at)
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:c))"), {"c": component})
    last = conn.execute(text("""SELECT up_since, last_seen, fingerprint FROM liveness WHERE component = :c
                                ORDER BY up_since DESC LIMIT 1"""), {"c": component}).first()
    payload = json.dumps(detail or {})
    if last is not None and at <= _utc(last.last_seen):
        return False                     # the same (or an older) heartbeat seen again: nothing new
    if last is not None and last.fingerprint == fingerprint and \
            (at - _utc(last.last_seen)).total_seconds() <= max_gap_s:
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
    beat(conn, receiver_component(name), hb.at, fp,
         {"pairs": [[label, f] for label, f in hb.pairs], "file": hb.path.name}, s.liveness_max_gap_s)
    return True


def record_ingest(source: str, *, blind: str | None = None, detail: dict | None = None,
                  receivers: set[str] | frozenset[str] = frozenset(), engine=None) -> None:
    """One ingest heartbeat for `source` (after a completed poll), plus its receivers'."""
    global _last_prune
    from core.db import get_engine
    s = get_settings()
    now = datetime.now(timezone.utc)
    with (engine or get_engine()).begin() as c:
        beat(c, ingest_component(source), now, f"blind:{blind}" if blind else "", detail, s.liveness_max_gap_s)
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

    def covers(self, ch) -> bool:
        """Did the receiver, during this run, listen to `ch` (pairs_cover; a plain heartbeat
        speaks for every channel of its receiver)?"""
        return self.fingerprint == "" or pairs_cover(self.pairs, ch)


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
