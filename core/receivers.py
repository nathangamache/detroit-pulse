"""Receiver heartbeats and coverage, for /v1/feeds/status and /v1/rail/activity.

"No transcripts" can mean two things: the channel was quiet, or nothing was listening. Pulse
tells them apart with a heartbeat that each SDR receiver writes even while its channels are
silent, into RECEIVER_STATUS_DIR (the `pulse_receivers` volume in deploy/docker-compose.yml):

  <name>.prom       rtl_airband's stats file (global `stats_filepath` in deploy/sdr/rail.conf),
                    rewritten every 15 s while the dongle delivers samples. It names every channel
                    rtl_airband demodulates (label = Pulse channel id, freq in MHz), so a channel
                    that isn't in it gets no heartbeat from that receiver.
  <name>.heartbeat  any file the receiver touches periodically: traintap's `--heartbeat`
                    (about once a minute while its decode loop runs).

`traintap-eot` also accepts `heartbeat` next to TRAINTAP_CSV (traintap's own default location).

A feed's receiver (`receiver:` in channels(.local).yaml, else by kind: rail voice channels ->
`rail-airband`, eot -> `traintap-eot`, others none) is

  not_installed  never reported: no heartbeat file, and no SDR recording / EOT packet for the
                 feed in the last 7 days
  offline        the newest of those is older than RECEIVER_STALE_S (default 300 s)
  online         newer than that. With no transcripts, the channel was QUIET, not uncovered.

Only real receiver output counts as evidence: recordings ingested by `sdr_files` and traintap
packets; fixture replays and legacy imports don't.
"""
from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.config_files import (RAIL_KINDS, Area, Channel, RailLine, Subdivision, haversine_km,
                               load_receiver_devices, names_match)
from core.settings import Settings, get_settings

RECEIVER_STATES = ("not_installed", "offline", "online")
SUMMARY_GROUPS = ("rail_radio", "eot", "public_safety")

# rtl_airband stats lines: channel_activity_counter{freq="160.215",label="csx_road"}\t123
_METRIC = re.compile(r'^channel_\w+\{freq="([0-9.]+)"(?:,label="([^"]*)")?\}', re.M)
_MAX_STATS_BYTES = 1 << 20


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def iso(t: datetime | None) -> str | None:
    t = _utc(t)
    return None if t is None else t.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Heartbeat:
    receiver: str
    path: Path
    at: datetime                              # file mtime, UTC
    labels: frozenset[str] = frozenset()      # rtl_airband channel labels (Pulse channel ids)
    freqs: tuple[float, ...] = ()             # rtl_airband channel frequencies, MHz

    def covers(self, ch: Channel) -> bool:
        """Does this heartbeat speak for `ch`? A plain heartbeat (or a stats file we couldn't
        parse, e.g. read mid-rewrite) covers every channel of its receiver."""
        if not self.freqs:
            return True
        if ch.id in self.labels:
            return True
        return ch.freq_mhz is not None and any(abs(f - ch.freq_mhz) < 0.003 for f in self.freqs)


def parse_airband_stats(text: str) -> tuple[frozenset[str], tuple[float, ...]]:
    labels: set[str] = set()
    freqs: set[float] = set()
    for m in _METRIC.finditer(text):
        freqs.add(round(float(m[1]), 4))
        if m[2]:
            labels.add(m[2])
    return frozenset(labels), tuple(sorted(freqs))


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
            labels, freqs = parse_airband_stats(f.read(_MAX_STATS_BYTES).decode("utf-8", "replace"))
    except OSError:
        labels, freqs = frozenset(), ()
    return Heartbeat(name, path, at, labels, freqs)


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
        seen = [t for t in (_utc(evidence_at), hb.at if hb is not None and hb.covers(ch) else None) if t]
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


def channel_covers_point(ch: Channel, lat: float, lon: float, subs: list[Subdivision], areas: list[Area]) -> bool:
    """Does `ch`'s receiver reach (lat, lon)? Only a place on record counts: the channel's
    `bias` point with a `coverage.radius_km`, its `area_id` area (radius: `coverage.radius_km`,
    else the area's), or a subdivision it is on whose center+radius / bbox contains the point.
    A channel with none of these covers no point: a hint naming its line is not enough."""
    area = next((a for a in areas if a.id == ch.area_id), None) if ch.area_id else None
    radius = ch.coverage_radius_km if ch.coverage_radius_km is not None else (area.radius_km if area else None)
    if radius is not None:
        if ch.bias_lat is not None and ch.bias_lon is not None and \
                haversine_km(ch.bias_lat, ch.bias_lon, lat, lon) <= radius:
            return True
        if area is not None and haversine_km(area.lat, area.lon, lat, lon) <= radius:
            return True
    return any(sd.near(lat, lon) and on_subdivision(ch, sd) for sd in subs)


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
