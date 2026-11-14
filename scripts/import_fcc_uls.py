#!/usr/bin/env python3
"""Build a railroad channel plan from FCC ULS public license data.

    # 1. get the weekly Land Mobile - Private dump (public domain, ~GBs unzipped):
    #    https://data.fcc.gov/download/pub/uls/complete/l_LMpriv.zip   -> unzip into a directory
    # 2. import:
    uv run python scripts/import_fcc_uls.py --uls-dir /path/to/l_LMpriv \
        [--out config/rail_channels.local.yaml] [--bbox south,west,north,east] [--state MI]

Reads the pipe-delimited ULS tables directly (no network):
  HD.dat  license header      [1] unique system id  [4] call sign  [5] status (A = active)  [6] radio service
  EN.dat  entity              [1] usi  [5] entity type (L = licensee)  [7] entity name
  LO.dat  locations           [1] usi  [6] location type  [8] location number  [12] city  [13] county
                              [14] state  [19-22] lat d/m/s/hemisphere  [23-26] lon d/m/s/hemisphere
  FR.dat  frequencies         [1] usi  [6] location number  [7] antenna number  [8] station class
                              [10] frequency assigned (MHz)

Keeps active licenses whose licensee NAME matches CSX, CN / Grand Trunk, Norfolk Southern,
Conrail Shared Assets, or Amtrak, with frequencies in the 160.110-161.565 MHz railroad pool,
at fixed sites inside the coverage bbox (config/areas coverage_bbox by default). ULS has no
subdivision field, so channels are grouped under "<County> County (ULS)" pseudo-subdivisions
with a center at the mean site location; reassign them to real subdivisions by hand.
Everything is written with verified: false and a per-entry FCC source string.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

LICENSEES = [
    ("CSX", re.compile(r"\bCSX\b", re.I)),
    ("CN", re.compile(r"GRAND TRUNK|CANADIAN NATIONAL|WISCONSIN CENTRAL|ILLINOIS CENTRAL RAILROAD", re.I)),
    ("NS", re.compile(r"NORFOLK SOUTHERN", re.I)),
    ("CSAO", re.compile(r"CONRAIL|CONSOLIDATED RAIL", re.I)),
    ("AMTK", re.compile(r"NATIONAL RAILROAD PASSENGER|AMTRAK", re.I)),
]
POOL_LO, POOL_HI = 160.110, 161.565
FIXED_CLASSES = {"FB", "FB2", "FB4", "FB6", "FB8", "FX1", "FX", "FXO"}


def rows(path: Path):
    with open(path, encoding="latin-1", newline="") as f:
        for line in f:
            yield line.rstrip("\r\n").split("|")


def dms(deg: str, mi: str, se: str, hemi: str) -> float | None:
    try:
        v = float(deg) + float(mi or 0) / 60 + float(se or 0) / 3600
    except ValueError:
        return None
    return -v if hemi in ("W", "S") else v


def aar_channel(f: float) -> str | None:
    if not (POOL_LO - 0.004 <= f <= POOL_HI + 0.004):
        return None
    ch = (f - POOL_LO) / 0.015
    if abs(ch - round(ch)) < 0.02:
        return f"{int(round(ch)):02d}"
    return f"{ch:04.1f}"            # 7.5 kHz narrowband splinter, e.g. "07.5"


def railroad_for(name: str) -> str | None:
    for code, pat in LICENSEES:
        if pat.search(name):
            return code
    return None


def build_plan(uls_dir: Path, bbox: tuple[float, float, float, float], state: str | None) -> dict:
    s, w, n, e = bbox
    active = {}
    for r in rows(uls_dir / "HD.dat"):
        if len(r) > 6 and r[5] == "A":
            active[r[1]] = r[4]
    rr = {}
    for r in rows(uls_dir / "EN.dat"):
        if len(r) > 7 and r[1] in active and r[5] == "L":
            code = railroad_for(r[7])
            if code:
                rr[r[1]] = (code, r[7])
    sites: dict[tuple[str, str], dict] = {}
    for r in rows(uls_dir / "LO.dat"):
        if len(r) < 27 or r[1] not in rr:
            continue
        lat = dms(r[19], r[20], r[21], r[22]) if r[19] else None
        lon = dms(r[23], r[24], r[25], r[26]) if r[23] else None
        if lat is None or lon is None or not (s <= lat <= n and w <= lon <= e):
            continue
        if state and r[14] and r[14] != state:
            continue
        sites[(r[1], r[8])] = {"lat": lat, "lon": lon, "city": r[12].title(), "county": r[13].title()}
    plan: dict = defaultdict(lambda: defaultdict(lambda: {"sites": [], "channels": {}}))
    for r in rows(uls_dir / "FR.dat"):
        if len(r) < 11 or (r[1], r[6]) not in sites:
            continue
        try:
            f = round(float(r[10]), 5)
        except ValueError:
            continue
        aar = aar_channel(f)
        if aar is None:
            continue
        cls = r[8].strip().upper()
        site = sites[(r[1], r[6])]
        code, licensee = rr[r[1]]
        bucket = plan[code][f"{site['county'] or 'Unknown'} County (ULS)"]
        bucket["sites"].append((site["lat"], site["lon"]))
        key = (f, aar)
        if key not in bucket["channels"]:
            bucket["channels"][key] = {
                "freq_mhz": f, "aar_channel": aar,
                "use": "road",                     # ULS can't tell road from dispatcher; set it by ear
                "verified": False,
                "source": f"FCC ULS {active[r[1]]} ({licensee.title()}), {cls or 'class?'} at "
                          f"{site['city']}, {site['county']} Co.",
            }
    out: dict = {"railroads": {}}
    for code, subs in sorted(plan.items()):
        out["railroads"][code] = {"subdivisions": {}}
        for sub, b in sorted(subs.items()):
            lat = sum(p[0] for p in b["sites"]) / len(b["sites"])
            lon = sum(p[1] for p in b["sites"]) / len(b["sites"])
            out["railroads"][code]["subdivisions"][sub] = {
                "center": {"lat": round(lat, 4), "lon": round(lon, 4)}, "radius_km": 20,
                "channels": sorted(b["channels"].values(), key=lambda c: c["freq_mhz"]),
            }
    return out


def main() -> None:
    from core.config_files import coverage_bbox
    from core.settings import get_settings

    ap = argparse.ArgumentParser()
    ap.add_argument("--uls-dir", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=get_settings().config_dir / "rail_channels.local.yaml")
    ap.add_argument("--bbox", default=None, help="south,west,north,east (default: coverage_bbox)")
    ap.add_argument("--state", default="MI")
    a = ap.parse_args()
    if a.bbox:
        bbox = tuple(float(x) for x in a.bbox.split(","))
    else:
        bb = coverage_bbox()
        bbox = (bb.south, bb.west, bb.north, bb.east)
    plan = build_plan(a.uls_dir, bbox, a.state or None)
    header = (f"# Generated by scripts/import_fcc_uls.py on {date.today().isoformat()} from FCC ULS public data\n"
              f"# (l_LMpriv). Pseudo-subdivisions are per county; reassign, set `use`, and mark `verified`\n"
              f"# only after hearing each channel yourself. This file is gitignored.\n")
    a.out.write_text(header + yaml.safe_dump(plan, sort_keys=False, width=120))
    n = sum(len(sd["channels"]) for rr in plan["railroads"].values() for sd in rr["subdivisions"].values())
    print(f"wrote {n} channel entries for {len(plan['railroads'])} railroads to {a.out}")


if __name__ == "__main__":
    main()
