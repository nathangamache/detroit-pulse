"""Wayside defect-detector announcement parser (rules first; the LLM batch is the fallback).

Detectors key up the road channel after each train and read a canned message,
e.g. (synthetic, typical of CSX/NS/CN voices):

  "CSX equipment detector, milepost 123.4, track 1. No defects. No defects.
   Total axles 412. Train speed 47 miles per hour. Temperature 38 degrees. Detector out."
  "NS detector mile post two eight five point three, south track, hot box,
   axle 46, left side. Repeat, hot box axle 46 left side. Speed 42. Axle count 312."
  "Equipment detector milepost 61.3, integrity failure, integrity failure."

parse() returns a DetectorReading; `complete` says whether the rules got the key
fields (milepost + a result). Incomplete readings are sent to Claude with the
rest of the batch and merged back (extract/worker.py).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from rail.numbers import NUM_RUN, parse_number

ALARM_TYPES = {
    "hot_box": [r"hot\s*box(?:es)?", r"hot\s*bearing", r"hot\s*journal", r"excessive\s+(?:journal\s+)?heat"],
    "hot_wheel": [r"hot\s*wheel", r"hot\s*brake"],
    "cold_wheel": [r"cold\s*wheel"],
    "dragging_equipment": [r"dragging\s+equipment", r"dragger", r"dragging\s+(?:object|hose)"],
    "high_wide": [r"high\s*(?:and\s*)?wide", r"high\s*car", r"wide\s*load", r"shifted\s+load",
                  r"high\s+load"],
    "excessive_alarms": [r"(?:excessive|too\s+many)\s+alarms?"],
}
INTEGRITY = [r"integrity\s+failure", r"system\s+failure", r"detector\s+(?:malfunction|failure|failed|inoperative)",
             r"power\s+failure", r"no\s+temperature", r"low\s+battery", r"analy[sz]er\s+(?:failure|malfunction)"]
NO_DEFECTS = r"no\s+defects?(?:\s+found)?|no\s+alarms?"

RAILROADS = {
    "CSX": r"\bcsx\b", "NS": r"\b(?:ns|norfolk\s+southern)\b", "CN": r"\b(?:cn|canadian\s+national|grand\s+trunk)\b",
    "CPKC": r"\b(?:cp|cpkc|canadian\s+pacific)\b", "CSAO": r"\bconrail\b", "UP": r"\b(?:up|union\s+pacific)\b",
    "BNSF": r"\bbnsf\b", "AMTK": r"\bamtrak\b",
}

_DET_CUE = re.compile(r"\b(?:defect|equipment|hot\s*box|dragging)?\s*detector\b|\bno\s+defects?\b|\bdetector\s+out\b"
                      r"|\b(?:total\s+)?axles?\s+(?:count\s+)?\d", re.I)
_MILEPOST = re.compile(rf"\b(?:mile\s*post|mp|mile\s+marker|mile)\s*(?:number\s+)?(?P<n>{NUM_RUN})", re.I)
# "total axles 412" / "axle count 312" / "total axle count 220" / "axles 96" (never the alarm's "axle 46")
_AXLES = re.compile(rf"\b(?:total\s+axles?(?:\s+count)?|axles?\s+count|axles)\s*(?:is|of|:)?\s*(?P<n>{NUM_RUN})", re.I)
_SPEED = re.compile(rf"\b(?:train\s+)?speed\s*(?:is)?\s*(?P<n>{NUM_RUN})(?:\s*(?:miles\s+per\s+hour|mph))?", re.I)
_TEMP = re.compile(rf"\btemp(?:erature)?\s*(?:is)?\s*(?P<neg>minus\s+|negative\s+)?(?P<n>{NUM_RUN})(?:\s*degrees?)?", re.I)
_TRACK = re.compile(rf"\b(?:track|main(?:\s+track)?)\s*(?:number\s+)?(?P<n>{NUM_RUN})\b"
                    r"|\b(?P<side>north|south|east|west|single)\s+(?:main|track)\b", re.I)
_DIRECTION = re.compile(r"\b(north|south|east|west)\s*bound\b", re.I)
_AXLE_N = re.compile(rf"\baxle\s+(?:number\s+)?(?P<n>{NUM_RUN})", re.I)
_SIDE = re.compile(r"\b(left|right|north|south|east|west)\s+(?:side|rail)\b", re.I)


@dataclass
class Alarm:
    type: str
    axle: int | None = None
    side: str | None = None


@dataclass
class DetectorReading:
    result: str = "unknown"                 # no_defects | alarm | integrity_failure | unknown
    milepost: float | None = None
    track: str | None = None
    direction: str | None = None
    axle_count: int | None = None
    speed_mph: int | None = None
    temperature_f: int | None = None
    alarms: list[Alarm] = field(default_factory=list)
    integrity_failure: bool = False
    railroad: str | None = None
    detector_name: str | None = None

    @property
    def complete(self) -> bool:
        return self.milepost is not None and self.result != "unknown"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["complete"] = self.complete
        return d


def looks_like_detector(text: str) -> bool:
    t = text.lower()
    if "detector" in t or re.search(NO_DEFECTS, t):
        return True
    # a milepost plus an axle count or alarm word, without the word "detector"
    if _MILEPOST.search(t) and (_AXLES.search(t) or any(re.search(p, t) for ps in ALARM_TYPES.values() for p in ps)):
        return True
    return False


def _int(m: re.Match | None, group: str = "n") -> int | None:
    if not m:
        return None
    raw = m.group(group) if group in m.groupdict() and m.group(group) else None
    if raw is None:
        for g in m.groupdict():
            if m.group(g):
                raw = m.group(g)
                break
    v = parse_number(raw) if raw else None
    return int(v) if v is not None else None


def parse(text: str) -> DetectorReading:
    t = " ".join(text.replace("\n", " ").split())
    low = t.lower()
    r = DetectorReading()

    for name, pat in RAILROADS.items():
        if re.search(pat, low):
            r.railroad = name
            break
    m = re.search(r"\b((?:[a-z]+\s+){0,3}(?:defect|equipment|hot\s*box)?\s*detector)\b", low)
    if m:
        r.detector_name = m.group(1).strip()

    m = _MILEPOST.search(t)
    if m:
        v = parse_number(m.group("n"))
        if v is not None and v < 2000:
            r.milepost = round(v, 2)
    m = _AXLES.search(t)
    if m:
        r.axle_count = _int(m)
    m = _SPEED.search(t)
    if m:
        r.speed_mph = _int(m)
    m = _TEMP.search(t)
    if m:
        v = _int(m)
        if v is not None:
            r.temperature_f = -v if m.group("neg") else v
    m = _TRACK.search(t)
    if m:
        if m.group("side"):
            r.track = m.group("side").lower()
        else:
            v = parse_number(m.group("n"))
            r.track = str(int(v)) if v is not None else None
    m = _DIRECTION.search(t)
    if m:
        r.direction = m.group(1).lower() + "bound"

    # alarms, with the axle/side spoken right after each alarm phrase
    seen: set[tuple] = set()
    for atype, pats in ALARM_TYPES.items():
        for p in pats:
            for am in re.finditer(p, low):
                tail = t[am.end(): am.end() + 60]
                ax = _AXLE_N.search(tail)
                axle = None
                if ax and ax.start() < 25:
                    v = parse_number(ax.group("n"))
                    axle = int(v) if v is not None else None
                sd = _SIDE.search(tail)
                side = sd.group(1).lower() if sd and sd.start() < 45 else None
                key = (atype, axle, side)
                if key not in seen:
                    seen.add(key)
                    r.alarms.append(Alarm(atype, axle, side))
    # "repeat, hot box axle 46" duplicates collapse via `seen`; drop axle-less duplicates of a typed alarm
    typed_with_axle = {a.type for a in r.alarms if a.axle is not None}
    r.alarms = [a for a in r.alarms if a.axle is not None or a.type not in typed_with_axle]

    r.integrity_failure = any(re.search(p, low) for p in INTEGRITY)
    if r.alarms:
        r.result = "alarm"
    elif r.integrity_failure:
        r.result = "integrity_failure"
    elif re.search(NO_DEFECTS, low):
        r.result = "no_defects"
    return r
