"""Rules-only extraction: the $0 fallback when Claude is disabled, capped or failing.

Deliberately simple and conservative: regexes for units, house numbers, blocks,
intersections of known streets and freeway segments; keyword tables for incident
type, priority and the rail reason label. Output has the same shape as the Claude
path (extract.schema.ItemResult) with source="rules".
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from core.config_files import Channel, load_areas, load_channels
from core.settings import get_settings
from extract.schema import ItemResult, Location
from rail import detector as det

SUFFIXES = {"street", "st", "avenue", "ave", "road", "rd", "drive", "dr", "boulevard", "blvd", "lane", "ln",
            "court", "ct", "highway", "hwy", "parkway", "pkwy", "place", "pl", "way", "trail", "freeway",
            "expressway", "circle", "terrace"}
DIRS = {"north", "south", "east", "west", "n", "s", "e", "w"}
UNIT_WORDS = ("engine", "medic", "ladder", "truck", "squad", "rescue", "tower", "chief", "battalion", "car",
              "unit", "ambulance", "patrol", "deputy", "sergeant", "lieutenant", "captain", "brush", "tanker")

_UNIT_RE = re.compile(r"\b(" + "|".join(UNIT_WORDS) + r")\s*[#-]?\s*(\d{1,4}(?:-\d{1,3})?)\b", re.I)
_TRAIN_RE = re.compile(r"\b(?:(csx|ns|cn|cp|cpkc|amtrak|conrail)\s+(\d{2,5})|([qmlkhgu]\d{3})(?:-\d{2})?)\b", re.I)
_HWY_RE = re.compile(r"\b(?P<dir>north|south|east|west)?\s*(?:bound\s+)?"
                     r"(?P<hwy>i[\s-]?\d{2,3}|interstate\s+\d{2,3}|m[\s-]?\d{1,3}|us[\s-]?\d{1,3}|the\s+lodge|lodge|"
                     r"southfield\s+freeway|jeffries|fisher|chrysler|reuther|edsel\s+ford|davison)"
                     r"\s+(?:freeway\s+)?(?:at|near|by|and|approaching|past)\s+(?P<cross>[a-z][a-z' ]{2,30}?)"
                     r"(?=[,.;]|\s+(?:for|with|on|in|is|we|i|there|reported|accident)\b|$)", re.I)
_BLOCK_RE = re.compile(r"\b(?P<n>\d{1,5}00)\s+block\s+(?:of\s+)?(?P<s>[a-z][a-z' ]{2,30}?)"
                       r"(?=[,.;]|\s+(?:for|with|on|in|is|and|we|i)\b|$)", re.I)
_ADDR_RE = re.compile(r"\b(?P<n>\d{3,6})\s+(?P<s>(?:(?:north|south|east|west)\s+)?[a-z][a-z']+(?:\s+[a-z][a-z']+){0,2})", re.I)
_MILE_RE = re.compile(r"\b(\d{1,2}|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen)[\s-]*mile\b", re.I)

INCIDENT_KEYWORDS = [
    ("SHOOTING", r"shots?\s+fired|shooting|gunshot|been\s+shot|shot\s+(?:at|in)|rounds?\s+fired"),
    ("STABBING", r"stabb(?:ed|ing)|knife\s+wound"),
    ("STRUCTURE_FIRE", r"structure\s+fire|house\s+fire|working\s+fire|fully\s+involved|smoke\s+(?:showing|from\s+the\s+(?:house|building|roof))|box\s+alarm|commercial\s+fire|residential\s+fire|apartment\s+fire"),
    ("VEHICLE_FIRE", r"(?:car|vehicle|truck|auto)\s+fire|vehicle\s+on\s+fire"),
    ("OTHER_FIRE", r"brush\s+fire|grass\s+fire|dumpster\s+fire|rubbish\s+fire|bonfire|trash\s+fire"),
    ("FIRE_ALARM", r"fire\s+alarm|smoke\s+detector|alarm\s+sounding"),
    ("TRAFFIC_ACCIDENT", r"\bpi\s+accident|personal\s+injury|accident|crash|rollover|mvc|vehicles?\s+in\s+the\s+ditch|hit\s+by\s+a\s+(?:car|vehicle)"),
    ("PURSUIT", r"pursuit|fleeing|chase"),
    ("ROBBERY", r"robbery|armed\s+robbery|carjack"),
    ("BURGLARY", r"breaking\s+and\s+entering|\bb\s*(?:and|&)\s*e\b|burglary|break[\s-]?in"),
    ("THEFT", r"larceny|retail\s+fraud|stolen|theft|shoplift"),
    ("DOMESTIC", r"domestic|\bdv\b"),
    ("ASSAULT", r"assault|fight|beaten|battery"),
    ("HAZMAT", r"hazmat|chemical\s+spill|fuel\s+spill"),
    ("GAS_LEAK", r"gas\s+leak|smell\s+of\s+gas|odor\s+of\s+gas|natural\s+gas"),
    ("WATER_RESCUE", r"water\s+rescue|swift\s+water|ice\s+rescue|in\s+the\s+water"),
    ("MEDICAL", r"unresponsive|not\s+breathing|cpr|overdose|difficulty\s+breathing|chest\s+pains?|fall\b|fell|"
                r"seizure|diabetic|bleeding|unconscious|cardiac|stroke|injured|medical|sick\s+person|lift\s+assist"),
    ("WELFARE_CHECK", r"welfare\s+check|check\s+(?:on\s+the\s+)?well[\s-]?being"),
    ("SUSPICIOUS", r"suspicious"),
    ("DISTURBANCE", r"disturbance|loud\s+music|noise\s+complaint"),
    ("TRAFFIC_HAZARD", r"debris\s+in\s+the\s+road|traffic\s+hazard|signal\s+out|lights?\s+out|tree\s+down|wires?\s+down"),
    ("BOMB_THREAT", r"bomb\s+threat|suspicious\s+package"),
]
HIGH_PRIORITY = re.compile(r"shots?\s+fired|shooting|been\s+shot|stabb|cpr|not\s+breathing|unresponsive|working\s+fire|"
                           r"fully\s+involved|entrap|pinned|pursuit|struck\s+by\s+(?:a\s+)?train|person\s+(?:hit|struck)|"
                           r"in\s+emergency|went\s+into\s+emergency|officer\s+down|mayday|rollover", re.I)

RAIL_REASON_RULES = [
    ("defect_detector", r"detector|no\s+defects|hot\s*box|dragging\s+equipment|axle\s+count"),
    ("crossing_incident", r"crossing.{0,40}(?:struck|hit|vehicle|car|truck|gates?\s+down\s+on)|(?:struck|hit)\s+.{0,20}crossing"),
    ("trespasser", r"trespass|people\s+on\s+the\s+(?:tracks|bridge)|person\s+on\s+the\s+tracks|kids\s+on\s+the|someone\s+on\s+the\s+tracks"),
    ("signal_problem", r"(?:dark|red|restricting)\s+signal|signal\s+(?:is\s+)?(?:dark|out|problem|trouble)|gates?\s+(?:are\s+)?(?:malfunction|stuck|down\s+and)|signal\s+maintainer|track\s+circuit|stop\s+and\s+proceed"),
    ("crew_change_hos", r"crew\s+change|recrew|hours\s+of\s+service|dog\s+law|\bhos\b|crew\s+van|taxi\s+for\s+the\s+crew|outlawed"),
    ("mechanical", r"knuckle|air\s+hose|lost\s+(?:our\s+)?air|undesired\s+emergency|went\s+into\s+emergency|locomotive\s+(?:shut|died|failure)|"
                   r"\beot\b|\bfred\b|\bhot\b.{0,10}(?:fail|trouble)|broken\s+rail|hot\s+bearing\s+on"),
    ("meet_or_hold", r"\bhold(?:ing)?\b|\bmeet\b|for\s+the\s+(?:east|west|north|south)bound|track\s+warrant|\bauthority\b|"
                     r"take\s+the\s+siding|lined\s+(?:up\s+)?(?:for|into)|wait(?:ing)?\s+(?:on|for)"),
    ("switching_yard", r"\byard\b|shove|couple|set\s+out|pick\s+up|switch(?:ing)?\b|industry|cut\s+of\s+cars|car\s+lengths"),
]


@lru_cache(maxsize=4)
def _vocab_from(paths: tuple[str, ...]) -> frozenset[str]:
    names: set[str] = set()
    for p in paths:
        path = Path(p)
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip() and not line.startswith("#"):
                    for t in line.split(","):
                        t = t.strip().lower()
                        if t:
                            names.add(_core(t))
    return frozenset(n for n in names if n)


def _core(name: str) -> str:
    """'East Warren Avenue' -> 'warren'; '7 Mile Road' -> '7 mile'."""
    toks = [t for t in re.split(r"[\s.]+", name.lower()) if t]
    toks = [t for t in toks if t not in SUFFIXES]
    if toks and toks[0] in DIRS and len(toks) > 1:
        toks = toks[1:]
    return " ".join(toks)


def known_streets() -> frozenset[str]:
    cdir = Path(get_settings().config_dir)
    base = _vocab_from((str(cdir / "vocab" / "dispatch.txt"),))
    extra = {_core(v) for a in load_areas() for v in a.vocab}
    extra |= {_core(v) for c in load_channels() for v in c.vocab}
    return base | frozenset(extra)


def _looks_like_street(name: str, streets: frozenset[str]) -> bool:
    n = name.strip().lower()
    if not n:
        return False
    toks = n.split()
    if toks[-1] in SUFFIXES or _MILE_RE.search(n):
        return True
    core = _core(n)
    return core in streets or any(core.startswith(s + " ") or core == s for s in streets if len(s) > 3)


def _intersection(text: str, streets: frozenset[str]) -> Location | None:
    # "<A> and|&|at <B>" where both sides look like streets
    for m in re.finditer(r"(?P<a>(?:\d{1,2}[\s-]*mile)|[A-Za-z][A-Za-z']+(?:\s+[A-Za-z][A-Za-z']+){0,2})"
                         r"\s+(?:and|&|at)\s+"
                         r"(?P<b>(?:\d{1,2}[\s-]*mile)|[A-Za-z][A-Za-z']+(?:\s+[A-Za-z][A-Za-z']+){0,2})", text):
        a, b = _trim_street(m["a"]), _trim_street(m["b"])
        if a and b and _looks_like_street(a, streets) and _looks_like_street(b, streets):
            return Location(kind="intersection", street1=_title(a), street2=_title(b), raw=m.group(0))
    return None


_LEAD_NOISE = {"respond", "to", "at", "on", "of", "the", "for", "a", "in", "near", "from", "go", "out", "we're",
               "were", "i'm", "area", "corner", "and", "by"}


def _trim_street(s: str) -> str:
    toks = s.split()
    while toks and toks[0].lower() in _LEAD_NOISE:
        toks.pop(0)
    while toks and toks[-1].lower() in _LEAD_NOISE:
        toks.pop()
    return " ".join(toks)


def _title(s: str) -> str:
    return " ".join(w if w.isupper() and len(w) <= 3 else w.capitalize() for w in s.split())


def extract_location(text: str) -> Location:
    streets = known_streets()
    m = _HWY_RE.search(text)
    if m:
        hwy = re.sub(r"\s+", " ", m["hwy"]).strip()
        hwy = re.sub(r"^i[\s-]?(\d+)$", r"I-\1", hwy, flags=re.I)
        direction = f" {m['dir'].capitalize()}bound" if m["dir"] else ""
        return Location(kind="highway", street1=_title(hwy) + direction if not hwy.startswith("I-") else hwy + direction,
                        street2=_title(_trim_street(m["cross"])), raw=m.group(0).strip())
    m = _BLOCK_RE.search(text)
    if m and _looks_like_street(_trim_street(m["s"]), streets):
        return Location(kind="block", number=m["n"], street1=_title(_trim_street(m["s"])), raw=m.group(0))
    for m in _ADDR_RE.finditer(text):
        street = _trim_street(m["s"])
        # "5 rounds", "12th floor", unit numbers etc. never match because a street must look like one
        if street and _looks_like_street(street, streets) and not re.match(r"^(?:mile|miles)\b", street, re.I):
            # house numbers right after a unit word ("Medic 1433") are unit ids, not addresses
            pre = text[max(0, m.start() - 12): m.start()].lower()
            if any(pre.rstrip().endswith(w) for w in UNIT_WORDS):
                continue
            return Location(kind="address", number=m["n"], street1=_title(street), raw=m.group(0))
    loc = _intersection(text, streets)
    if loc:
        return loc
    return Location(kind="none")


def extract_units(text: str) -> list[str]:
    out: list[str] = []
    for m in _UNIT_RE.finditer(text):
        u = f"{m.group(1).capitalize()} {m.group(2)}"
        if u not in out:
            out.append(u)
    for m in _TRAIN_RE.finditer(text):
        u = f"{m.group(1).upper()} {m.group(2)}" if m.group(1) else m.group(3).upper()
        if u not in out:
            out.append(u)
    return out[:20]


def classify_incident(text: str) -> str:
    low = text.lower()
    for itype, pat in INCIDENT_KEYWORDS:
        if re.search(pat, low):
            return itype
    return "UNKNOWN"


def rail_reason(text: str) -> str:
    low = text.lower()
    for label, pat in RAIL_REASON_RULES:
        if re.search(pat, low):
            return label
    return "unknown"


def extract(item_id: str, transcript: str, channel: Channel | None = None,
            detector_reading: det.DetectorReading | None = None) -> ItemResult:
    loc = extract_location(transcript)
    units = extract_units(transcript)
    is_rail = bool(channel and channel.is_rail) or bool(detector_reading) or bool(
        re.search(r"\b(?:dispatcher|milepost|track\s+warrant|siding|yardmaster|detector|main\s+(?:one|two|1|2))\b",
                  transcript, re.I))
    itype = classify_incident(transcript)
    rail = None
    if is_rail:
        reason = "defect_detector" if detector_reading else rail_reason(transcript)
        if itype in ("UNKNOWN", "MEDICAL", "TRAFFIC_ACCIDENT") and reason in ("crossing_incident", "trespasser"):
            itype = "RAIL_INCIDENT"
        elif itype == "UNKNOWN":
            itype = "RAIL_OPERATIONS"
        rail = {"railroad": (detector_reading.railroad if detector_reading else None) or (channel.railroad if channel else None),
                "subdivision": channel.subdivision if channel else None,
                "train_symbol": next((u for u in units if re.fullmatch(r"[QMLKHGU]\d{3}", u)), None),
                "milepost": (f"{detector_reading.milepost:g}" if detector_reading and detector_reading.milepost is not None else None),
                "reason_label": reason,
                "detector": _detector_dict(detector_reading)}
    priority = "HIGH" if HIGH_PRIORITY.search(transcript) else ("MEDIUM" if itype not in ("UNKNOWN", "RAIL_OPERATIONS") else "LOW")
    if itype == "UNKNOWN" and loc.kind == "none":
        itype = "NONE"
        priority = "LOW"
    summary = _summary(itype, loc, units)
    return ItemResult(id=item_id, has_location=loc.usable, location=loc, incident_type=itype, priority=priority,
                      units=units, is_rail=is_rail, rail=rail, short_summary=summary, source="rules")


def _detector_dict(r: det.DetectorReading | None) -> dict | None:
    if r is None:
        return None
    return {"result": r.result, "track": r.track, "direction": r.direction, "axle_count": r.axle_count,
            "speed_mph": r.speed_mph, "temperature_f": r.temperature_f,
            "alarms": [a.type for a in r.alarms]}


def _summary(itype: str, loc: Location, units: list[str]) -> str:
    label = itype.replace("_", " ").lower()
    parts = [label.capitalize() if itype not in ("NONE", "UNKNOWN") else "Radio traffic"]
    if loc.usable:
        parts.append(f"at {loc.display()}")
    if units:
        parts.append(f"({', '.join(units[:4])})")
    return " ".join(parts)[:200]
