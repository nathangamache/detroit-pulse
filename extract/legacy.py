"""Convert between the old pipeline's single-string locations and structured Location fields.

Old format (normalize.py / LoRA labels):  "16800 East 8 Mile Road, Detroit, MI",
"7 Mile Road and Evergreen Road, Detroit, MI", "Interstate 94 Westbound and Livernois Avenue,
Detroit, MI", or "NO_LOCATION". Used to build few-shots from the TRAIN split and to score
structured output with the original string scorer.
"""
from __future__ import annotations

import re

from extract.schema import Location

_INT = re.compile(r"^(?P<a>.+?)\s+(?:and|&|at|@|/)\s+(?P<b>.+)$", re.I)
_ADDR = re.compile(r"^(?P<n>\d{1,6})\s+(?P<block>block\s+of\s+)?(?P<s>.+)$", re.I)
_HWY = re.compile(r"^(?:interstate|i-|m-|us-)", re.I)
_STREETY = re.compile(r"\b(road|rd|avenue|ave|street|st|drive|dr|blvd|boulevard|highway|hwy|mile|lane|court|"
                      r"place|parkway|freeway|way|trail)\b", re.I)


def parse_legacy(s: str) -> Location:
    if not s or s.strip().upper() == "NO_LOCATION":
        return Location(kind="none")
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if parts and parts[-1].upper() in ("MI", "MICHIGAN"):
        parts = parts[:-1]
    city = parts[-1] if len(parts) >= 2 else None
    head = parts[0] if parts else s
    m = _ADDR.match(head)
    if m and not re.match(r"^\d+\s+mile\b", head, re.I):
        return Location(kind="block" if m["block"] else "address", number=m["n"], street1=m["s"].strip(),
                        city=city, raw=None)
    m = _INT.match(head)
    if m:
        kind = "highway" if _HWY.match(m["a"]) or _HWY.match(m["b"]) else "intersection"
        return Location(kind=kind, street1=m["a"].strip(), street2=m["b"].strip(), city=city)
    if _STREETY.search(head):
        return Location(kind="landmark", street1=head, city=city)   # a bare street: geocoded as a line/centroid
    return Location(kind="landmark", street1=head, city=city)


def to_legacy(loc: Location, default_city: str | None = None) -> str:
    if not loc.usable:
        return "NO_LOCATION"
    city = loc.city or default_city
    if loc.kind in ("intersection", "highway") and loc.street1 and loc.street2:
        head = f"{loc.street1} and {loc.street2}"
    elif loc.kind in ("address", "block") and loc.number and loc.street1:
        head = f"{loc.number} {loc.street1}"
    elif loc.kind == "landmark" and loc.street1 and loc.street2:
        head = f"{loc.street1}, {loc.street2}"
    else:
        head = loc.street1 or loc.raw or ""
    return f"{head}, {city}, MI" if city else f"{head}, MI"
