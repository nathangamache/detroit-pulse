"""Layer B builder: house numbers.

    points   key -> columns of address points (official city/county layers + OSM addr:*)
             num int32, lat/lon float32, dir/sfx/city/src small codes; one row per
             (number, direction, type, city, source), units averaged
    tiger    key -> [(from, to, parity, coords[N,2], zip, fullname, suffix)]   TIGER/Line ADDRFEAT
             address ranges, one entry per side; interpolated where no point exists

Point rows come from geocode.local.sources (already normalized to dicts) so this module has no
network code. TIGER shapefiles are read straight from the Census zip files with pyshp.
"""
from __future__ import annotations

import time
from array import array
from collections import defaultdict
from typing import Callable, Iterable

import numpy as np

from geocode.local.normalize import core_key, leading_dir, street_suffix

DIR_CODES = ["", "n", "s", "e", "w", "ne", "nw", "se", "sw"]
SRC_CODES = ["detroit", "oakland", "annarbor", "osm"]       # order = trust when numbers tie
SUFFIX_CODES = ["", "street", "avenue", "road", "boulevard", "drive", "lane", "court", "place",
                "parkway", "highway", "freeway", "terrace", "circle", "way", "trail", "square",
                "row", "alley", "crescent", "service", "frontage"]
_DIR_IDX = {d: i for i, d in enumerate(DIR_CODES)}
_SFX_IDX = {s: i for i, s in enumerate(SUFFIX_CODES)}


def parse_number(v) -> int | None:
    """'1234' -> 1234; '1234A', '1234-1236' -> 1234; anything else -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        return n if n > 0 else None
    s = str(v).strip()
    digits = ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits and int(digits) > 0 else None


def street_label(*parts) -> str:
    return " ".join(str(p).strip() for p in parts if p is not None and str(p).strip())


def _name_codes():
    """Memoized street string -> (core key, dir code, suffix code): ~30k distinct names, 1M+ rows."""
    cache: dict[str, tuple[str, int, int]] = {}

    def get(street: str) -> tuple[str, int, int]:
        hit = cache.get(street)
        if hit is None:
            hit = cache[street] = (core_key(street), _DIR_IDX.get(leading_dir(street) or "", 0),
                                   _SFX_IDX.get(street_suffix(street) or "", 0))
        return hit
    return get


def build_points(rows: Iterable[dict], city_of: Callable[[float, float], str] | None = None,
                 log: Callable[[str], None] = print) -> tuple[dict, list[str], dict]:
    """rows: {"number", "street", "city", "lat", "lon", "src"} -> (points, city table, stats).

    Rows are collected in typed arrays and grouped with numpy (a dict per row would cost ~1 GB
    for the 1.2M rows of the default sources)."""
    t0 = time.time()
    codes = _name_codes()
    cities: list[str] = [""]
    city_idx: dict[str, int] = {"": 0}
    keys: list[str] = []
    key_idx: dict[str, int] = {}
    cols = {c: array(t) for c, t in (("key", "i"), ("num", "i"), ("dir", "b"), ("sfx", "b"), ("city", "i"),
                                       ("src", "b"), ("lat", "d"), ("lon", "d"))}
    n_in, by_src = 0, defaultdict(int)
    src_idx = {s: i for i, s in enumerate(SRC_CODES)}
    for r in rows:
        n_in += 1
        num = parse_number(r.get("number"))
        street = r.get("street") or ""
        lat, lon = r.get("lat"), r.get("lon")
        if num is None or not street or lat is None or lon is None or num > 2_000_000_000:
            continue
        k, d, sfx = codes(street)
        if not k:
            continue
        city = (r.get("city") or "").strip()
        if not city and city_of is not None:
            city = city_of(lat, lon)
        city = city.title()
        ci = city_idx.get(city)
        if ci is None:
            ci = city_idx[city] = len(cities)
            cities.append(city)
        ki = key_idx.get(k)
        if ki is None:
            ki = key_idx[k] = len(keys)
            keys.append(k)
        for c, v in (("key", ki), ("num", num), ("dir", d), ("sfx", sfx), ("city", ci),
                     ("src", src_idx[r["src"]]), ("lat", float(lat)), ("lon", float(lon))):
            cols[c].append(v)
        by_src[r["src"]] += 1
    a = {c: np.frombuffer(v, dtype={"i": np.int32, "b": np.int8, "d": np.float64}[v.typecode]) if len(v)
         else np.zeros(0, dtype={"i": np.int32, "b": np.int8, "d": np.float64}[v.typecode]) for c, v in cols.items()}
    n = len(a["key"])
    points: dict[str, dict] = {}
    if n:
        group_cols = ("key", "num", "dir", "sfx", "city", "src")
        order = np.lexsort(tuple(a[c] for c in reversed(group_cols)))
        g = {c: a[c][order] for c in a}
        change = np.zeros(n, bool)
        change[0] = True
        for c in group_cols:
            change[1:] |= g[c][1:] != g[c][:-1]
        starts = np.nonzero(change)[0]
        cnt = np.diff(np.append(starts, n))
        u = {c: g[c][starts] for c in group_cols}
        u["lat"] = (np.add.reduceat(g["lat"], starts) / cnt).astype(np.float32)
        u["lon"] = (np.add.reduceat(g["lon"], starts) / cnt).astype(np.float32)
        cdt = np.int16 if len(cities) < 32000 else np.int32
        kb = np.nonzero(np.r_[True, u["key"][1:] != u["key"][:-1]])[0]
        ke = np.r_[kb[1:], len(u["key"])]
        for b, e in zip(kb.tolist(), ke.tolist()):
            points[keys[int(u["key"][b])]] = {
                "num": u["num"][b:e].copy(), "dir": u["dir"][b:e].copy(), "sfx": u["sfx"][b:e].copy(),
                "city": u["city"][b:e].astype(cdt), "src": u["src"][b:e].copy(),
                "lat": u["lat"][b:e].copy(), "lon": u["lon"][b:e].copy()}
    stats = {"point_rows_in": n_in, "point_rows_used": n, "point_keys": len(points),
             "point_unique": int(sum(len(v["num"]) for v in points.values())),
             "point_rows_by_source": dict(by_src), "cities": len(cities), "points_s": round(time.time() - t0, 1)}
    log(f"address points: {n} rows -> {stats['point_unique']} unique over {len(points)} street keys "
        f"in {stats['points_s']:.0f}s")
    return points, cities, stats


def osm_addr_rows(addr: list[tuple]) -> Iterable[dict]:
    for hn, st, city, la, lo in addr:
        yield {"number": hn, "street": st, "city": city, "lat": la, "lon": lo, "src": "osm"}


def tiger_ranges(zip_paths: Iterable[str], log: Callable[[str], None] = print) -> tuple[dict, dict]:
    import shapefile   # build-time only dependency
    t0 = time.time()
    segs: dict[str, list] = defaultdict(list)
    names: dict[str, tuple[str, str]] = {}
    n = 0
    for zp in zip_paths:
        with shapefile.Reader(str(zp)) as r:
            for sr in r.iterShapeRecords():
                rec = sr.record.as_dict()
                fn = rec.get("FULLNAME")
                if not fn or not sr.shape.points:
                    continue
                coords = np.array([(y, x) for x, y in sr.shape.points], dtype=np.float64)
                if fn not in names:
                    names[fn] = (core_key(fn), street_suffix(fn) or "")
                k, sfx = names[fn]
                for side in ("L", "R"):
                    a, b = rec.get(f"{side}FROMHN"), rec.get(f"{side}TOHN")
                    if a and b and str(a).isdigit() and str(b).isdigit():
                        segs[k].append((int(a), int(b), rec.get(f"PARITY{side}") or "", coords,
                                        rec.get(f"ZIP{side}") or "", fn, sfx))
                        n += 1
    stats = {"tiger_side_ranges": n, "tiger_keys": len(segs), "tiger_s": round(time.time() - t0, 1)}
    log(f"tiger ranges: {n} side-ranges over {len(segs)} street keys in {stats['tiger_s']:.0f}s")
    return dict(segs), stats
