"""Receiver heartbeats and coverage, for /v1/feeds/status and /v1/rail/activity.

"No transcripts" can mean two things: the channel was quiet, or nothing was listening. Pulse
tells them apart with a heartbeat that each SDR receiver writes even while its channels are
silent, into RECEIVER_STATUS_DIR (the `pulse_receivers` volume in deploy/docker-compose.yml):

  <name>.prom       rtl_airband's stats file (global `stats_filepath` in deploy/sdr/rail.conf),
                    rewritten every 15 s while the dongle delivers samples. It names every channel
                    rtl_airband demodulates (label = Pulse channel id, freq in MHz), so a channel
                    that isn't in it gets no heartbeat from that receiver. A channel whose label
                    is in it with another frequency than its configured `freq_mhz` (the config
                    changed and rail.local.conf wasn't re-rendered) gets none either, nor does a
                    frequency the receiver files under another channel that is disabled or
                    configured elsewhere (heard_via). Its counters only grow while rtl_airband
                    runs, so one that went down shows a restart (core/liveness.py).
  <name>.heartbeat  any file the receiver touches periodically: traintap's `--heartbeat`
                    (about once a minute while its decode loop runs).

`traintap-eot` also accepts `heartbeat` next to TRAINTAP_CSV (traintap's own default location).

A feed's receiver (`receiver:` in channels(.local).yaml, else by kind: rail voice channels ->
`rail-airband`, eot -> `traintap-eot`, others none) is

  not_installed  never reported: no heartbeat file, and no SDR recording / EOT packet for the
                 feed in the last 7 days
  offline        the newest of those is older than RECEIVER_STALE_S (default 300 s)
  online         newer than that. That is "listening now", not "quiet": /v1/rail/activity only
                 calls a window quiet when core/liveness.py's history shows the receiver (and the
                 ingest stage) listening across all of it (api/rail_verdicts.py).

Only real receiver output counts as evidence: recordings ingested by `sdr_files` and traintap
packets; fixture replays and legacy imports don't.
"""
from __future__ import annotations

import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.config_files import (RAIL_KINDS, Area, Channel, RailLine, Subdivision, haversine_km, load_channels,
                               load_receiver_devices, names_match)
from core.settings import Settings, get_settings

RECEIVER_STATES = ("not_installed", "offline", "online")
SUMMARY_GROUPS = ("rail_radio", "eot", "public_safety")
FREQ_TOLERANCE_MHZ = 0.003          # a stats line's frequency matches a channel's within 3 kHz

# rtl_airband stats lines: channel_activity_counter{freq="160.215",label="csx_road"}\t123
_METRIC = re.compile(r'^channel_\w+\{freq="([0-9.]+)"(?:,label="([^"]*)")?\}', re.M)
# its counters (TYPE counter: *_counter, *_count), on complete lines only: a line cut off by a read
# mid-rewrite must never look like a counter that went down
_COUNTER = re.compile(r'^(\w+_(?:counter|count))\{([^}\n]*)\}\t(\d+)\n', re.M)
_MAX_STATS_BYTES = 1 << 20

# (label or None, frequency in MHz) for each channel a receiver says it demodulates
Pairs = tuple[tuple[str | None, float], ...]


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def iso(t: datetime | None) -> str | None:
    t = _utc(t)
    return None if t is None else t.isoformat().replace("+00:00", "Z")


def _freq_ok(f: float, want: float) -> bool:
    return abs(f - want) < FREQ_TOLERANCE_MHZ


def _known(known: dict[str, Channel] | None, ch: Channel) -> dict[str, Channel]:
    """Every configured channel by id (in config order), with `ch` itself at the end if absent."""
    if known is None:
        known = {c.id: c for c in load_channels()}
    return known if ch.id in known else {**known, ch.id: ch}


def recorded_as(label: str | None, f: float, known: dict[str, Channel]) -> Channel | None:
    """The configured channel that `sdr_files` files a receiver channel's recordings under.
    scripts/render_rail_conf.py writes each rtl_airband channel with label = the Pulse channel id
    and that channel's file template, so a label that is a channel id means that channel, but
    only if it is configured on that frequency (else the render is stale and None). An
    unlabelled channel, or a label that is no channel id, resolves the way match_channel resolves
    its files: by template (the label), then by frequency, first match in config order."""
    if label is not None and label in known:
        c = known[label]
        return c if c.freq_mhz is None or _freq_ok(f, c.freq_mhz) else None
    if label is not None:
        for c in known.values():
            if label in c.templates:
                return c
    for c in known.values():
        if c.freq_mhz is not None and _freq_ok(f, c.freq_mhz):
            return c
    return None


def heard_via(pairs: Pairs, ch: Channel, known: dict[str, Channel] | None = None) -> list[str]:
    """Through which receiver channels does a receiver that says it demodulates `pairs` hear
    `ch`? As the ids of the enabled channels their recordings are filed under (recorded_as):

    * `[ch.id]`: it lists ch's own label with ch's configured frequency (or, unlabelled, ch's
      frequency resolves to ch). Its label on another frequency means the receiver's config is
      stale: it is NOT listening to ch (`[]`), whatever else is on that frequency;
    * another enabled channel's id: ch shares that channel's frequency, which the receiver
      demodulates once, under that channel (a detector on the road channel);
    * `[]` otherwise, and when ch's frequency is filed under a channel that is disabled (its
      recordings are dropped: a channel renamed or retired without re-rendering rail.local.conf)
      or configured on another frequency.

    `known`: every configured channel by id (default: config/channels(.local).yaml)."""
    mine = [f for label, f in pairs if label == ch.id]
    if mine:
        return [ch.id] if ch.freq_mhz is None or any(_freq_ok(f, ch.freq_mhz) for f in mine) else []
    if ch.freq_mhz is None:
        return []
    known = _known(known, ch)
    out: list[str] = []
    for label, f in pairs:
        if not _freq_ok(f, ch.freq_mhz):
            continue
        c = recorded_as(label, f, known)
        if c is not None and c.enabled and c.id not in out:
            out.append(c.id)
    return out


def pairs_cover(pairs: Pairs, ch: Channel, known: dict[str, Channel] | None = None) -> bool:
    """Does a receiver that says it demodulates `pairs` listen to `ch` (heard_via)?"""
    return bool(heard_via(pairs, ch, known))


def pairs_problem(pairs: Pairs, ch: Channel, known: dict[str, Channel] | None = None) -> str | None:
    """Why a receiver that lists ch's frequency still isn't listening to `ch`, in plain words
    (None when it is, or when it doesn't list the frequency at all). The label case is
    reported_freqs'."""
    if ch.freq_mhz is None or heard_via(pairs, ch, known) or any(label == ch.id for label, _ in pairs):
        return None
    known = _known(known, ch)
    for label, f in pairs:
        if not _freq_ok(f, ch.freq_mhz):
            continue
        c = recorded_as(label, f, known)
        if c is None and label in known:
            other = known[label]
            return (f"the receiver demodulates {f:g} MHz as {other.id}, which is configured on "
                    f"{other.freq_mhz:g} MHz (re-render rail.local.conf)")
        if c is not None and not c.enabled:
            return (f"the receiver files {f:g} MHz under {c.id}, a disabled channel, so those recordings are "
                    "dropped (re-render rail.local.conf)")
    return None


def reported_freqs(pairs: Pairs, ch: Channel) -> list[float]:
    """The frequencies a receiver reports under `ch`'s label when none is the configured one
    (empty when they agree, or when the receiver doesn't label the channel)."""
    mine = sorted({f for label, f in pairs if label == ch.id})
    if not mine or ch.freq_mhz is None or any(_freq_ok(f, ch.freq_mhz) for f in mine):
        return []
    return mine


@dataclass(frozen=True)
class Heartbeat:
    receiver: str
    path: Path
    at: datetime                              # file mtime, UTC
    pairs: Pairs = ()                         # rtl_airband (label, freq MHz) per demodulated channel
    counters: dict[str, int] = field(default_factory=dict, compare=False, hash=False)   # rtl_airband's

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(label for label, _ in self.pairs if label)

    @property
    def freqs(self) -> tuple[float, ...]:
        return tuple(sorted({f for _, f in self.pairs}))

    @property
    def is_stats(self) -> bool:
        return self.path.suffix == ".prom"

    def covers(self, ch: Channel, known: dict[str, Channel] | None = None) -> bool:
        """Does this heartbeat speak for `ch`? A plain heartbeat (or a stats file we couldn't
        parse, e.g. read mid-rewrite) covers every channel of its receiver; a stats file
        covers the channels it lists with their configured frequency (pairs_cover)."""
        if not self.pairs:
            return True
        return pairs_cover(self.pairs, ch, known)


def parse_airband_pairs(text: str) -> Pairs:
    """(label or None, freq MHz) for every channel line in an rtl_airband stats file."""
    out: set[tuple[str | None, float]] = set()
    for m in _METRIC.finditer(text):
        out.add((m[2] or None, round(float(m[1]), 4)))
    return tuple(sorted(out, key=lambda p: (p[1], p[0] or "")))


def parse_airband_counters(text: str) -> dict[str, int]:
    """rtl_airband's counters ('metric{labels}' -> value), from complete lines only. They only
    grow while rtl_airband runs; after a restart they start again at 0."""
    return {f"{m[1]}{{{m[2]}}}": int(m[3]) for m in _COUNTER.finditer(text)}


def parse_airband_stats(text: str) -> tuple[frozenset[str], tuple[float, ...]]:
    pairs = parse_airband_pairs(text)
    return frozenset(label for label, _ in pairs if label), tuple(sorted({f for _, f in pairs}))


def heartbeat_paths(name: str, s: Settings) -> list[Path]:
    paths = [s.receiver_status_dir / f"{name}.prom", s.receiver_status_dir / f"{name}.heartbeat"]
    if name == "traintap-eot":
        paths.append(Path(s.traintap_csv).parent / "heartbeat")
    return paths


def read_heartbeat(name: str, s: Settings | None = None) -> Heartbeat | None:
    """The newest heartbeat file of receiver `name`, or None if it has never written one."""
    s = s or get_settings()
    best: tuple[Path, float] | None = None
    for p in heartbeat_paths(name, s):
        try:
            st = p.stat()
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and (best is None or st.st_mtime > best[1]):
            best = (p, st.st_mtime)
    if best is None:
        return None
    path, mtime = best
    at = datetime.fromtimestamp(mtime, tz=timezone.utc)
    if path.suffix != ".prom":
        return Heartbeat(name, path, at)
    try:
        with open(path, "rb") as f:
            body = f.read(_MAX_STATS_BYTES).decode("utf-8", "replace")
    except OSError:
        body = ""
    return Heartbeat(name, path, at, parse_airband_pairs(body), parse_airband_counters(body))


def state_from(last_seen: datetime | None, now: datetime, stale_s: float) -> str:
    if last_seen is None:
        return "not_installed"
    return "online" if (now - last_seen).total_seconds() <= stale_s else "offline"


class ReceiverView:
    """Receiver states for one request (each heartbeat file is read once)."""

    def __init__(self, settings: Settings | None = None, now: datetime | None = None):
        self.s = settings or get_settings()
        self.now = _utc(now) or datetime.now(timezone.utc)
        self.devices = load_receiver_devices()
        self.known = {c.id: c for c in load_channels()}
        self._hb: dict[str, Heartbeat | None] = {}

    def heartbeat(self, name: str | None) -> Heartbeat | None:
        if not name:
            return None
        if name not in self._hb:
            self._hb[name] = read_heartbeat(name, self.s)
        return self._hb[name]

    def heartbeat_age_s(self, name: str) -> float | None:
        hb = self.heartbeat(name)
        return None if hb is None else (self.now - hb.at).total_seconds()

    def status(self, ch: Channel | None, evidence_at: datetime | None = None) -> dict:
        """The contract's `receiver` object for a feed. `evidence_at` is the feed's newest real
        receiver output (SDR recording or EOT packet) from the database."""
        name = ch.receiver_name if ch is not None else None
        hb = self.heartbeat(name)
        seen = [t for t in (_utc(evidence_at), hb.at if hb is not None and hb.covers(ch, self.known) else None) if t]
        last = max(seen) if seen else None
        out = {"state": state_from(last, self.now, self.s.receiver_stale_s)}
        if last is not None:
            out["last_seen_at"] = iso(last)
        if name and self.devices.get(name):
            out["device"] = self.devices[name]
        return out


# ── Coverage ────────────────────────────────────────────────────────────────

def on_subdivision(ch: Channel, sd: Subdivision) -> bool:
    """Is `ch` a channel for `sd`: linked by channel_id, or one of its line names is one of the
    subdivision's names (FRA and NARN spellings both count)."""
    if any(rc.channel_id == ch.id for rc in sd.channels):
        return True
    rr = sd.railroad.upper()
    if ch.subdivision and (not ch.railroad or ch.railroad.upper() == rr) and sd.matches(ch.subdivision, exact=True):
        return True
    for ln in ch.coverage_lines or []:
        if (not ln.railroad or ln.railroad.upper() == rr) and any(sd.matches(n, exact=True) for n in ln.names()):
            return True
    return False


def channel_matches_hint(ch: Channel, hint: str, railroad: str | None = None) -> bool:
    """Does `hint` name a line `ch` hears (its `subdivision`, or a `coverage.lines` name or
    alias)? Exact once normalised (norm_line_name), never a substring: "est" names nothing.
    With `railroad`, the line must be that railroad's (a line with no railroad isn't)."""
    rr = (railroad or "").strip().upper()
    lines = [(ch.railroad or "", [ch.subdivision])] if ch.subdivision else []
    lines += [(ln.railroad or ch.railroad or "", ln.names()) for ln in ch.coverage_lines or []]
    return any((not rr or line_rr.upper() == rr) and names_match(hint, names, exact=True)
               for line_rr, names in lines)


def own_places(ch: Channel, areas: list[Area]) -> list[tuple[float, float, float]]:
    """The channel's own receiver area(s) on record, as (lat, lon, radius_km): its `bias` point
    and/or its `area_id` area's center, with `coverage.radius_km` (else the area's radius).
    Empty when it has no radius or no center: a radius alone places nothing."""
    area = next((a for a in areas if a.id == ch.area_id), None) if ch.area_id else None
    radius = ch.coverage_radius_km if ch.coverage_radius_km is not None else (area.radius_km if area else None)
    if radius is None:
        return []
    out = []
    if ch.bias_lat is not None and ch.bias_lon is not None:
        out.append((ch.bias_lat, ch.bias_lon, radius))
    if area is not None:
        out.append((area.lat, area.lon, radius))
    return out


def channel_covers_point(ch: Channel, lat: float, lon: float, subs: list[Subdivision], areas: list[Area]) -> bool:
    """Does `ch`'s receiver reach (lat, lon)? Only a place on record counts, never a name and
    never a fixed distance:

    1. the channel's own area (own_places: `bias` or `area_id`, with `coverage.radius_km` or
       the area's radius) decides when it has one; a subdivision never stretches it;
    2. otherwise a subdivision it is on (on_subdivision) whose center+radius / bbox contains the
       point, and no farther from that subdivision's center than the channel's own
       `coverage.radius_km` when it gives one.

    A channel with none of these covers no point: a hint naming its line is not enough."""
    places = own_places(ch, areas)
    if places:
        return any(haversine_km(plat, plon, lat, lon) <= r for plat, plon, r in places)
    r = ch.coverage_radius_km
    for sd in subs:
        if not (sd.near(lat, lon) and on_subdivision(ch, sd)):
            continue
        if r is None or sd.center is None or haversine_km(sd.center[0], sd.center[1], lat, lon) <= r:
            return True
    return False


def on_railroad(ch: Channel, railroad: str | None, subs: list[Subdivision]) -> bool:
    """Is `ch` one of `railroad`'s channels (its own railroad, a line it hears, or a line in the
    rail plan it is on)? Always true without a railroad hint."""
    rr = (railroad or "").strip().upper()
    if not rr:
        return True
    if (ch.railroad or "").upper() == rr or any((ln.railroad or "").upper() == rr for ln in ch.coverage_lines or []):
        return True
    return any(sd.railroad.upper() == rr and on_subdivision(ch, sd) for sd in subs)


def lines_for(ch: Channel, subs: list[Subdivision]) -> list[RailLine]:
    if ch.coverage_lines is not None:
        return ch.coverage_lines
    out = [RailLine(sd.railroad, sd.name, list(sd.aliases)) for sd in subs if on_subdivision(ch, sd)]
    if not out and ch.subdivision:
        out.append(RailLine(ch.railroad or "", ch.subdivision, []))
    return out


def coverage_for(ch: Channel, subs: list[Subdivision], areas: list[Area]) -> dict:
    """The contract's `coverage` object: the channel's `coverage:` block, else derived from its
    area and the rail plan. Every key is optional; {} means nothing is known."""
    area = next((a for a in areas if a.id == ch.area_id), None) if ch.area_id else None
    name = ch.coverage_area or (area.name if area else None) or ch.city
    radius = ch.coverage_radius_km if ch.coverage_radius_km is not None else (area.radius_km if area else None)
    lines = lines_for(ch, subs) if ch.is_rail else (ch.coverage_lines or [])
    out: dict = {}
    if name:
        out["area"] = name
    if lines:
        out["lines"] = [ln.to_public() for ln in lines]
    if radius is not None:
        out["radius_km"] = radius
    return out


def summary_group(ch: Channel) -> str:
    if ch.kind == "eot":
        return "eot"
    return "rail_radio" if ch.kind in RAIL_KINDS else "public_safety"


def coverage_summary(items: list[tuple[Channel, str]]) -> dict[str, str]:
    """`coverage_summary` from (channel, receiver state) pairs. Disabled and example feeds
    don't count: a group with nothing else is `not_configured`."""
    out = {}
    for g in SUMMARY_GROUPS:
        states = [st for ch, st in items if ch.enabled and not ch.is_example and summary_group(ch) == g]
        out[g] = ("not_configured" if not states else "listening" if "online" in states
                  else "receiver_offline" if "offline" in states else "no_receiver")
    return out
