"""YAML config loading with gitignored local overrides.

Every config `X` is looked up as `config/X.local.yaml` first (personal, never
committed), then `config/X.yaml` (committed, generic example data only).

Committed files must only hold public, generic data: no home/work/school
points, no RadioReference-derived frequencies.
"""
from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.settings import get_settings

_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def config_path(name: str) -> Path:
    cdir = get_settings().config_dir
    local = cdir / f"{name}.local.yaml"
    if local.exists():
        return local
    return cdir / f"{name}.yaml"


def load_yaml(name: str) -> dict:
    """Load config/<name>(.local).yaml, re-reading when the file changes."""
    path = config_path(name)
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    key = str(path)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    with _lock:
        _cache[key] = (mtime, data)
    return data


def clear_cache() -> None:
    with _lock:
        _cache.clear()


# ── Geometry helpers ────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class BBox:
    south: float
    west: float
    north: float
    east: float

    def contains(self, lat: float | None, lon: float | None) -> bool:
        if lat is None or lon is None:
            return False
        return self.south <= lat <= self.north and self.west <= lon <= self.east

    @classmethod
    def from_any(cls, v: Any) -> "BBox | None":
        if not v:
            return None
        if isinstance(v, dict):
            return cls(float(v["south"]), float(v["west"]), float(v["north"]), float(v["east"]))
        s, w, n, e = (float(x) for x in v)
        return cls(s, w, n, e)


# ── Areas (coverage) ────────────────────────────────────────────────────────

@dataclass
class Area:
    id: str
    name: str
    lat: float
    lon: float
    radius_km: float = 10.0
    county: str | None = None
    city: str | None = None
    vocab: list[str] = field(default_factory=list)

    def contains(self, lat: float | None, lon: float | None) -> bool:
        if lat is None or lon is None:
            return False
        return haversine_km(self.lat, self.lon, lat, lon) <= self.radius_km


# Wayne + Oakland + Washtenaw + Macomb: the union of their extents in
# api/data/County_Boundaries.geojson (42.033..42.898 N, -84.134..-82.706 E), padded ~2 km.
# Also the default area of the local geocoder index (scripts/build_geocoder_index.py).
DEFAULT_COVERAGE_BBOX = BBox(south=42.01, west=-84.16, north=42.92, east=-82.68)


def coverage_bbox() -> BBox:
    data = load_yaml("areas")
    return BBox.from_any(data.get("coverage_bbox")) or DEFAULT_COVERAGE_BBOX


def load_areas() -> list[Area]:
    out = []
    for a in load_yaml("areas").get("areas", []) or []:
        out.append(Area(
            id=str(a["id"]), name=str(a.get("name", a["id"])),
            lat=float(a["lat"]), lon=float(a["lon"]),
            radius_km=float(a.get("radius_km", 10.0)),
            county=a.get("county"), city=a.get("city"),
            vocab=[str(v) for v in a.get("vocab", []) or []],
        ))
    return out


def default_bias() -> tuple[float, float]:
    data = load_yaml("areas")
    b = data.get("default_bias")
    if b:
        return float(b["lat"]), float(b["lon"])
    bb = coverage_bbox()
    return (bb.south + bb.north) / 2, (bb.west + bb.east) / 2


# ── Channels (SDR inputs) ───────────────────────────────────────────────────

# rail_voice: a railroad voice frequency whose use (road/dispatcher/yard) isn't confirmed on air yet
RAIL_KINDS = {"rail_road", "rail_dispatcher", "rail_yard", "rail_detector", "rail_voice", "eot"}
RAIL_VOICE_KINDS = RAIL_KINDS - {"eot"}

# The receiver a channel is heard on, by kind, unless the channel names one (`receiver:`).
# Its heartbeat is <RECEIVER_STATUS_DIR>/<name>.prom or <name>.heartbeat (core/receivers.py).
DEFAULT_RECEIVER_BY_KIND = {**{k: "rail-airband" for k in RAIL_VOICE_KINDS}, "eot": "traintap-eot"}
DEFAULT_RECEIVER_DEVICES = {
    "rail-airband": "rail-airband: RTL-SDR serial RAIL160 (rtl_airband)",
    "traintap-eot": "traintap-eot: RTL-SDR serial EOT457 (traintap)",
}
_RECEIVER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def norm_line_name(name: str | None) -> str:
    """Normalise a rail line name for matching across datasets (FRA's crossing inventory and NARN
    can name the same track differently): "EXAMPLE (CSXT)", "Example Sub", "EXAMPLE SUBDIVISION"
    -> "example"."""
    s = re.sub(r"\([^)]*\)", " ", (name or "").lower())
    s = re.sub(r"\b(?:subdivision|sub)\b\.?", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


@dataclass
class RailLine:
    """A track a channel hears, with every other name the same track has in other datasets."""
    railroad: str
    name: str
    aliases: list[str] = field(default_factory=list)

    def names(self) -> list[str]:
        return [self.name, *self.aliases]

    def to_public(self) -> dict:
        return {"railroad": self.railroad, "name": self.name, "aliases": list(self.aliases)}


def names_match(hint: str | None, names: list[str], exact: bool = False) -> bool:
    """True when `hint` names one of `names` (normalised; substring unless exact)."""
    h = norm_line_name(hint)
    if not h:
        return False
    for n in names:
        nn = norm_line_name(n)
        if nn and (h == nn or (not exact and h in nn)):
            return True
    return False


@dataclass
class Channel:
    id: str
    label: str
    kind: str = "public_safety"           # police|fire|ems|public_safety|rail_*|eot|other
    freq_mhz: float | None = None
    templates: list[str] = field(default_factory=list)   # rtl_airband filename_template(s)
    talkgroups: list[int] = field(default_factory=list)  # trunk-recorder sidecar JSON
    agency: str | None = None
    unit_prefix: str | None = None        # namespace for unit IDs, e.g. "AAFD"
    county: str | None = None
    city: str | None = None
    area_id: str | None = None
    railroad: str | None = None
    subdivision: str | None = None
    enabled: bool = True
    priority: int = 2
    bias_lat: float | None = None
    bias_lon: float | None = None
    vocab: list[str] = field(default_factory=list)
    receiver: str | None = None           # receiver name; None => DEFAULT_RECEIVER_BY_KIND
    example: bool = False                 # shipped placeholder (also any id starting "example_")
    coverage_area: str | None = None      # `coverage:` block: what this channel hears
    coverage_radius_km: float | None = None
    coverage_lines: list[RailLine] | None = None   # None = not given (derived from the rail plan)

    @property
    def is_rail(self) -> bool:
        return self.kind in RAIL_KINDS

    @property
    def is_example(self) -> bool:
        """Shipped example/placeholder feeds never count as coverage."""
        return self.example or self.id.startswith("example_")

    @property
    def receiver_name(self) -> str | None:
        return self.receiver or DEFAULT_RECEIVER_BY_KIND.get(self.kind)

    def to_public(self) -> dict:
        return {"id": self.id, "label": self.label, "kind": self.kind, "freq_mhz": self.freq_mhz,
                "agency": self.agency, "county": self.county, "city": self.city,
                "railroad": self.railroad, "subdivision": self.subdivision,
                "enabled": self.enabled, "is_rail": self.is_rail}


def _channel_from(d: dict) -> Channel:
    tmpl = d.get("templates") or ([d["template"]] if d.get("template") else [])
    if any(isinstance(t, bool) for t in tmpl):
        # YAML 1.1 turns bare on/off/yes/no into booleans; template names must be quoted
        raise ValueError(f"channel {d.get('id')}: quote template names like on/off/yes/no")
    bias = d.get("bias") or {}
    receiver = d.get("receiver")
    if receiver is not None and not (isinstance(receiver, str) and _RECEIVER_NAME.fullmatch(receiver)):
        raise ValueError(f"channel {d.get('id')}: receiver must match [A-Za-z0-9][A-Za-z0-9_.-]*")
    cov = d.get("coverage") or {}
    lines = None
    if cov.get("lines") is not None:
        lines = [RailLine(railroad=str(ln.get("railroad") or d.get("railroad") or ""), name=str(ln["name"]),
                          aliases=[str(a) for a in ln.get("aliases") or []])
                 for ln in cov["lines"] if ln and ln.get("name")]
    return Channel(
        id=str(d["id"]), label=str(d.get("label", d["id"])), kind=str(d.get("kind", "public_safety")),
        freq_mhz=float(d["freq_mhz"]) if d.get("freq_mhz") is not None else None,
        templates=[str(t) for t in tmpl], talkgroups=[int(t) for t in d.get("talkgroups", []) or []],
        agency=d.get("agency"), unit_prefix=d.get("unit_prefix"), county=d.get("county"),
        city=d.get("city"), area_id=d.get("area_id"), railroad=d.get("railroad"),
        subdivision=d.get("subdivision"), enabled=bool(d.get("enabled", True)),
        priority=int(d.get("priority", 2)),
        bias_lat=float(bias["lat"]) if bias.get("lat") is not None else None,
        bias_lon=float(bias["lon"]) if bias.get("lon") is not None else None,
        vocab=[str(v) for v in d.get("vocab", []) or []],
        receiver=receiver, example=bool(d.get("example", False)),
        coverage_area=str(cov["area"]) if cov.get("area") else None,
        coverage_radius_km=float(cov["radius_km"]) if cov.get("radius_km") is not None else None,
        coverage_lines=lines,
    )


def load_channels(include_disabled: bool = True) -> list[Channel]:
    chans = [_channel_from(c) for c in (load_yaml("channels").get("channels") or [])]
    return chans if include_disabled else [c for c in chans if c.enabled]


def load_receiver_devices() -> dict[str, str]:
    """Receiver name -> device description: the defaults plus `receivers:` in channels(.local).yaml."""
    out = dict(DEFAULT_RECEIVER_DEVICES)
    for name, r in (load_yaml("channels").get("receivers") or {}).items():
        if (r or {}).get("device"):
            out[str(name)] = str(r["device"])
    return out


def channel_by_id(channel_id: str) -> Channel | None:
    for c in load_channels():
        if c.id == channel_id:
            return c
    return None


def match_channel(template: str | None = None, freq_hz: int | None = None,
                  talkgroup: int | None = None) -> Channel | None:
    """Find the channel for an SDR file: template name first, then talkgroup, then frequency."""
    chans = load_channels()
    if template:
        for c in chans:
            if template in c.templates or template == c.id:
                return c
    if talkgroup is not None:
        for c in chans:
            if talkgroup in c.talkgroups:
                return c
    if freq_hz:
        for c in chans:
            if c.freq_mhz and abs(c.freq_mhz * 1e6 - freq_hz) < 3000:   # within 3 kHz
                return c
    return None


# ── Rail channel plan ───────────────────────────────────────────────────────

@dataclass
class RailChannel:
    railroad: str
    subdivision: str
    freq_mhz: float | None
    aar_channel: str | None
    use: str                    # road|dispatcher|yard|detector
    verified: bool
    source: str | None
    channel_id: str | None = None


@dataclass
class Subdivision:
    railroad: str
    name: str
    bbox: BBox | None = None
    center: tuple[float, float] | None = None
    radius_km: float = 15.0
    channels: list[RailChannel] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)   # the same track's names in other datasets

    def names(self) -> list[str]:
        return [self.name, *self.aliases]

    def matches(self, hint: str | None, exact: bool = False) -> bool:
        return names_match(hint, self.names(), exact=exact)

    def near(self, lat: float, lon: float) -> bool:
        if self.bbox and self.bbox.contains(lat, lon):
            return True
        if self.center:
            return haversine_km(self.center[0], self.center[1], lat, lon) <= self.radius_km
        return False


def load_subdivisions() -> list[Subdivision]:
    data = load_yaml("rail_channels")
    out: list[Subdivision] = []
    for rr_name, rr in (data.get("railroads") or {}).items():
        for sub_name, sub in ((rr or {}).get("subdivisions") or {}).items():
            sub = sub or {}
            center = sub.get("center")
            sd = Subdivision(
                railroad=str(rr_name), name=str(sub_name),
                bbox=BBox.from_any(sub.get("bbox")),
                center=(float(center["lat"]), float(center["lon"])) if center else None,
                radius_km=float(sub.get("radius_km", 15.0)),
                aliases=[str(a) for a in sub.get("aliases") or []],
            )
            for ch in sub.get("channels") or []:
                sd.channels.append(RailChannel(
                    railroad=str(rr_name), subdivision=str(sub_name),
                    freq_mhz=float(ch["freq_mhz"]) if ch.get("freq_mhz") is not None else None,
                    aar_channel=str(ch["aar_channel"]) if ch.get("aar_channel") is not None else None,
                    use=str(ch.get("use", "road")), verified=bool(ch.get("verified", False)),
                    source=ch.get("source"), channel_id=ch.get("channel_id"),
                ))
            out.append(sd)
    return out
