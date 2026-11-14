"""The $0 local geocoder: OSM intersections, address points + TIGER ranges, OSM landmarks.

Input is the structured location the extractor emits plus a bias point (the channel's area):

    {"kind": "intersection", "street1": "Gratiot", "street2": "Seven Mile", "city": "Detroit"}
    {"kind": "block", "number": 19000, "street1": "Kelly"}
    {"kind": "address", "number": 14303, "street1": "East Warren"}
    {"kind": "highway_segment", "street1": "I-94", "street2": "Conner"}
    {"kind": "landmark", "name": "Meijer", "street1": "Eight Mile", "street2": "Woodward"}

Layers (no network):
  A  intersections: nodes shared by both streets in OSM (clustered), else their closest approach
     (60 m; 120 m for freeways); duplicates resolved by city, then exact-over-fuzzy, then bias
  B  numbers: official address points (exact / block mean) -> OSM addr:* -> interpolation between
     neighbouring points -> TIGER ADDRFEAT range interpolation; candidates from every layer compete
     on city and distance to the bias, then on layer quality
  C  landmarks: curated aliases + OSM POI names (rapidfuzz), anchored to cross streets if spoken

Returns {"lat", "lng", "formatted", "confidence", "source", "layer"} or None. Thread-safe: the
extract worker may call it from several threads.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np
from rapidfuzz import fuzz, process
from scipy.spatial import cKDTree

from geocode.local import store
from geocode.local.build_addr import DIR_CODES, SRC_CODES, SUFFIX_CODES
from geocode.local.build_osm import CityTagger
from geocode.local.normalize import (basic, city_match, core_key, expand_aliases, full_key, leading_dir,
                                     street_suffix)

M_LAT = 110540.0
M_LON = 111320.0 * math.cos(math.radians(42.4))
MAX_BIAS_M = 60000          # reject a pick this far from the channel unless the city matched
ADDR_MAX_BIAS_M = 40000
KD_CACHE = 2048             # densified street KD-trees kept in memory

# spoken / short landmark names -> the name searched in the POI table (public places only)
LANDMARK_ALIASES = {
    "mgm": "MGM Grand Detroit", "mgm casino": "MGM Grand Detroit", "mgm grand": "MGM Grand Detroit",
    "greektown": "Hollywood Casino at Greektown", "greektown casino": "Hollywood Casino at Greektown",
    "motorcity casino": "MotorCity Casino", "motor city casino": "MotorCity Casino",
    "dmc": "Detroit Medical Center", "harper": "Harper University Hospital",
    "sinai": "Sinai-Grace Hospital", "sinai grace": "Sinai-Grace Hospital", "grace": "Sinai-Grace Hospital",
    "receiving": "Detroit Receiving Hospital", "detroit receiving": "Detroit Receiving Hospital",
    "henry ford": "Henry Ford Hospital", "childrens": "Children's Hospital of Michigan",
    "ford field": "Ford Field", "lca": "Little Caesars Arena", "little caesars arena": "Little Caesars Arena",
    "comerica": "Comerica Park", "ren cen": "Renaissance Center", "rencen": "Renaissance Center",
    "renaissance center": "Renaissance Center",
    "cobo": "Huntington Place", "cobo hall": "Huntington Place", "huntington place": "Huntington Place",
    "metro airport": "Detroit Metropolitan Airport", "dtw": "Detroit Metropolitan Airport",
    "coleman young": "Coleman A. Young Municipal Airport", "city airport": "Coleman A. Young Municipal Airport",
    "belle isle": "Belle Isle", "wayne state": "Wayne State University", "u of d": "University of Detroit Mercy",
    "st john": "Henry Ford St. John Hospital", "st john hospital": "Henry Ford St. John Hospital",
    "ascension st john hospital": "Henry Ford St. John Hospital",
    "ascension st john": "Henry Ford St. John Hospital",
    "detroit metropolitan wayne county airport": "Detroit Metropolitan Airport",
    "detroit metro airport": "Detroit Metropolitan Airport", "metro": "Detroit Metropolitan Airport",
    "coleman a young international airport": "Coleman A. Young Municipal Airport",
    "coleman young airport": "Coleman A. Young Municipal Airport",
    "rouge park": "River Rouge Park",
    "beaumont royal oak": "Corewell Health William Beaumont University Hospital",
    "beaumont hospital": "Corewell Health William Beaumont University Hospital",
    "beaumont": "Corewell Health William Beaumont University Hospital",
    "eastern market": "Eastern Market", "palmer park": "Palmer Park",
    "chandler park": "Chandler Park", "clark park": "Clark Park", "hart plaza": "Hart Plaza",
    "fairlane": "Fairlane Town Center", "somerset": "Somerset Collection", "twelve oaks": "Twelve Oaks Mall",
    # Ann Arbor / Oakland / western Wayne
    "the big house": "Michigan Stadium", "big house": "Michigan Stadium", "crisler": "Crisler Center",
    "briarwood": "Briarwood Mall", "arborland": "Arborland", "the diag": "The Diag",
    "detroit zoo": "Detroit Zoo", "the zoo": "Detroit Zoo",
    "university of michigan hospital": "University of Michigan Medical Center",
    "u of m hospital": "University of Michigan Medical Center", "uofm hospital": "University of Michigan Medical Center",
    "michigan medicine": "University of Michigan Medical Center", "um hospital": "University of Michigan Medical Center",
}
PRECINCT_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
                  "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}
KIND_PRIORITY = ("amenity=hospital", "amenity=police", "amenity=fire_station", "aeroway=aerodrome",
                 "leisure=park", "leisure=stadium", "amenity=university", "amenity=school",
                 "shop=supermarket", "shop=mall", "amenity=casino", "landuse=retail",
                 "amenity=marketplace", "place=")
STOP = {"the", "of", "at", "on", "and", "detroit", "mi"}
LOW_VALUE_KINDS = {"amenity=bicycle_rental", "amenity=car_rental", "highway=bus_stop",
                   "public_transport=platform", "public_transport=stop_position",
                   "amenity=parking", "railway=tram_stop", "amenity=fuel", "amenity=atm",
                   "amenity=car_wash", "amenity=bus_station"}
GENERIC_TOKENS = {"international": .4, "municipal": .4, "regional": .4, "county": .4,
                  "metropolitan": .4, "metro": .4, "center": .4, "centre": .4, "medical": .4,
                  "hospital": .4, "university": .5, "health": .3, "campus": .3, "ascension": .3,
                  "dmc": .3, "corewell": .3, "henry": 1.0, "station": .5, "state": .5,
                  "casino": .5, "arena": .6, "park": .8, "mall": .5, "school": .6, "high": .7}
LOW_PRI_KINDS = {"landuse=residential", "building=apartments", "building=residential",
                 "building=house", "tourism=information", "place=hamlet", "building=roof",
                 "tourism=attraction", "landuse=industrial", "landuse=commercial"}
# address-layer quality (lower is better)
_RANK = {"points": 0, "osm_addr": 1, "points_interp": 2, "tiger_interp": 3}
_OSM_SRC = SRC_CODES.index("osm")


def _w(t: str) -> float:
    return GENERIC_TOKENS.get(t, 1.0)


def _covered(qtok: str, nt: set) -> str | None:
    """The name token a query token stands for: itself, or a word it abbreviates ('tech' ->
    'technological', 'univ' -> 'university'; 4+ letters)."""
    if qtok in nt:
        return qtok
    if len(qtok) >= 4:
        for t in nt:
            if t.startswith(qtok):
                return t
    return None


def name_score(qt: set, q: str, n: str) -> float:
    """Weighted query-token coverage: 'Sinai-Grace Hospital' ~ 'DMC Sinai-Grace Hospital',
    'Henry Ford Hosp' ~ 'Henry Ford Hospital', but 'Detroit Receiving Hospital' !~ 'Detroit'."""
    nt = set(n.split()) - STOP
    if not nt:
        return 0.0
    if q.replace(" ", "") == n.replace(" ", ""):      # "MotorCity" == "Motor City"
        return 100.0
    hit = {t: _covered(t, nt) for t in qt}
    cov = sum(_w(t) for t, c in hit.items() if c) / sum(_w(t) for t in qt)
    extra = sum(_w(t) for t in nt - set(hit.values()))
    return max(100.0 * cov - 2.0 * extra, 0.92 * fuzz.token_sort_ratio(q, n))


def hav(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(d))


def xy(lat: float, lon: float) -> tuple[float, float]:
    return (lon * M_LON, lat * M_LAT)


def along(coords: np.ndarray, f: float) -> tuple[float, float]:
    """Point at fraction f along a polyline [N,2] (lat, lon)."""
    if len(coords) == 1:
        return float(coords[0, 0]), float(coords[0, 1])
    segs = [hav(coords[i], coords[i + 1]) for i in range(len(coords) - 1)]
    total = sum(segs) or 1.0
    target = max(0.0, min(1.0, f)) * total
    acc = 0.0
    for i, seg_len in enumerate(segs):
        if acc + seg_len >= target:
            t = (target - acc) / (seg_len or 1)
            a, b = coords[i], coords[i + 1]
            return float(a[0] + t * (b[0] - a[0])), float(a[1] + t * (b[1] - a[1]))
        acc += seg_len
    return float(coords[-1, 0]), float(coords[-1, 1])


class IndexMissing(RuntimeError):
    pass


class LocalGeocoder:
    """Backend for geocode.providers.local_index (`GEOCODER_LOCAL_BACKEND=geocode.local:LocalGeocoder`)."""

    def __init__(self, index_dir: str | Path):
        t0 = time.time()
        d = Path(index_dir)
        if not (d / store.OSM_FILE).exists() or not (d / store.ADDR_FILE).exists():
            raise IndexMissing(f"no geocoder index in {d} (run scripts/build_geocoder_index.py)")
        self.manifest = store.read_manifest(d)
        fmt = self.manifest.get("format")
        if fmt is not None and fmt != store.INDEX_FORMAT:
            raise IndexMissing(f"index format {fmt} != {store.INDEX_FORMAT}; rebuild the index")
        osm = store.load(d / store.OSM_FILE)
        addr = store.load(d / store.ADDR_FILE)
        self.streets: dict = osm["streets"]
        self.pairs: dict = osm["intersections"]
        self.full_keys: set = set(osm.get("full_keys") or [])
        self.pois: list = osm["pois"]
        self.city_of = CityTagger(osm.get("admin") or [])
        self.points: dict = addr["points"]
        self.cities: list = addr["cities"]
        self.tiger: dict = addr["tiger"]
        self.street_keys = list(self.streets)
        self.addr_keys = sorted(set(self.points) | set(self.tiger))
        self._kd: OrderedDict = OrderedDict()
        self._kd_lock = threading.Lock()
        self.poi_names = [basic(p[0]) for p in self.pois]
        self.poi_tree = cKDTree(np.array([xy(p[2], p[3]) for p in self.pois])) if self.pois else None
        self.load_s = round(time.time() - t0, 2)

    def info(self) -> dict:
        return {"load_s": self.load_s, "built": self.manifest.get("built"), "bbox": self.manifest.get("bbox"),
                "street_keys": len(self.streets), "intersection_pairs": len(self.pairs),
                "address_keys": len(self.addr_keys), "pois": len(self.pois)}

    # ── streets ────────────────────────────────────────────────────────────
    def resolve_keys(self, name: str | None, limit: int = 3, use_full: bool = True) -> list[tuple[str, float]]:
        if not name:
            return []
        fk = full_key(name) if use_full else None
        if fk and fk in self.streets:            # "Ann Arbor Trail" when Ann Arbor Road also exists
            return [(fk, 100.0)]
        out: list[tuple[str, float]] = []
        for kk in expand_aliases(core_key(name)):
            if kk in self.streets and kk not in [o[0] for o in out]:
                out.append((kk, 100.0))
        if out:
            return out
        k = core_key(name)
        return [(c, float(s)) for c, s, _ in process.extract(k, self.street_keys, scorer=fuzz.ratio,
                                                              limit=limit, score_cutoff=80)]

    def _kdtree(self, key: str):
        with self._kd_lock:
            hit = self._kd.get(key)
            if hit is not None:
                self._kd.move_to_end(key)
                return hit
        pts = []
        for _wid, _hwy, coords in self.streets.get(key, []):
            # densify to ~25 m so closest approach works between sparse vertices
            for i in range(len(coords) - 1):
                a, b = coords[i], coords[i + 1]
                n = max(1, int(hav(a, b) // 25))
                for j in range(n):
                    f = j / n
                    pts.append((a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])))
            pts.append((coords[-1, 0], coords[-1, 1]))
        arr = np.array(pts, dtype=np.float64) if pts else np.zeros((0, 2))
        tree = cKDTree(np.c_[arr[:, 1] * M_LON, arr[:, 0] * M_LAT]) if len(arr) else None
        with self._kd_lock:
            self._kd[key] = (arr, tree)
            while len(self._kd) > KD_CACHE:
                self._kd.popitem(last=False)
        return arr, tree

    def approach(self, k1: str, k2: str, max_m: float = 60.0) -> list[tuple]:
        """Closest-approach points of two streets (grade separations, broken OSM topology)."""
        a, ta = self._kdtree(k1)
        b, tb = self._kdtree(k2)
        if ta is None or tb is None:
            return []
        small, big_t, other = (a, tb, b) if len(a) <= len(b) else (b, ta, a)
        d, j = big_t.query(np.c_[small[:, 1] * M_LON, small[:, 0] * M_LAT], distance_upper_bound=max_m)
        hits = [((small[i][0] + other[j[i]][0]) / 2, (small[i][1] + other[j[i]][1]) / 2, d[i])
                for i in range(len(small)) if np.isfinite(d[i])]
        clusters: list[list] = []
        for h in sorted(hits, key=lambda x: x[2]):
            for c in clusters:
                if hav(h, c[0]) < 400:
                    c.append(h)
                    break
            else:
                clusters.append([h])
        return [(float(c[0][0]), float(c[0][1]), len(c), "") for c in clusters]

    def _key_sets(self, s1, s2):
        """[(k1s, k2s)]: suffix-qualified keys first (if either name has one), then core keys."""
        full = (self.resolve_keys(s1), self.resolve_keys(s2))
        core = (self.resolve_keys(s1, use_full=False), self.resolve_keys(s2, use_full=False))
        return [full] if full == core else [full, core]

    def _core(self, key: str) -> str:
        return key.rsplit(" ", 1)[0] if key in self.full_keys else key

    def _shared(self, k1s, k2s):
        out = []
        for k1, sc1 in k1s:
            for k2, sc2 in k2s:
                c1, c2 = self._core(k1), self._core(k2)
                if c1 == c2:
                    continue
                for la, lo, n, c, fulls in self.pairs.get("|".join(sorted((c1, c2))), []):
                    if (k1 != c1 and k1 not in fulls) or (k2 != c2 and k2 not in fulls):
                        continue        # "Ann Arbor Trail" asked, this crossing is Ann Arbor Road
                    out.append(dict(lat=la, lng=lo, n=n, city=c, fuzzy=min(sc1, sc2) < 100, how="shared_node"))
        return out

    def _approached(self, k1s, k2s, max_m, tag_city=True):
        out = []
        for k1, sc1 in k1s:
            for k2, sc2 in k2s:
                if k1 == k2:
                    continue
                for la, lo, n, _c in self.approach(k1, k2, max_m=max_m):
                    out.append(dict(lat=la, lng=lo, n=n, city=self.city_of(la, lo) if tag_city else "",
                                    fuzzy=min(sc1, sc2) < 100, how=f"approach<{max_m:.0f}m"))
        return out

    def intersection(self, s1, s2, bias, city=None, allow_approach=True, approach_m=60.0):
        sets = self._key_sets(s1, s2)
        for k1s, k2s in sets:
            cands = self._shared(k1s, k2s)
            if cands:
                return self._pick(cands, bias, city, f"{s1} & {s2}")
        if allow_approach:
            for k1s, k2s in sets:
                cands = self._approached(k1s, k2s, approach_m)
                if cands:
                    return self._pick(cands, bias, city, f"{s1} & {s2}")
        return None

    def _pick(self, cands, bias, city, label):
        for c in cands:
            c["dist_bias_m"] = hav((c["lat"], c["lng"]), bias)
            c["city_match"] = city_match(city, c.get("city"))
        # city match first, then exact-over-fuzzy, then distance to the channel's area
        cands.sort(key=lambda c: (not c["city_match"], c["fuzzy"], c["dist_bias_m"]))
        best = cands[0]
        if best["dist_bias_m"] > MAX_BIAS_M and not best["city_match"]:
            return None
        others = [c for c in cands[1:] if hav((c["lat"], c["lng"]), (best["lat"], best["lng"])) > 400]
        conf = "HIGH"
        if best["fuzzy"] or best["how"] != "shared_node":
            conf = "MEDIUM"
        if others and not best["city_match"] and others[0]["dist_bias_m"] < best["dist_bias_m"] * 2:
            conf = "MEDIUM" if conf == "HIGH" else "LOW"
        where = f", {best['city']}" if best.get("city") else ""
        return dict(lat=best["lat"], lng=best["lng"], formatted=f"{label}{where}", confidence=conf,
                    source=f"osm_{best['how']}", layer="A", city=best.get("city") or None)

    def highway_segment(self, fwy, cross, bias, city=None):
        r = self.intersection(fwy, cross, bias, city, allow_approach=False)
        if r:
            r["source"] = "osm_service_drive_node"
            return r
        for k1s, k2s in self._key_sets(fwy, cross):     # grade-separated: closest approach, wider
            cands = self._approached(k1s, k2s, 120.0, tag_city=False)
            if cands:
                r = self._pick(cands, bias, None, f"{fwy} at {cross}")
                if r:
                    r["source"] = "osm_freeway_approach"
                return r
        return None

    def street_point(self, s, bias):
        """Last resort: the point of the named street nearest the channel's area (LOW)."""
        for k, _sc in self.resolve_keys(s):
            arr, t = self._kdtree(k)
            if t is None:
                continue
            d, i = t.query(xy(*bias))
            if d < 15000:
                return dict(lat=float(arr[i][0]), lng=float(arr[i][1]), formatted=s, confidence="LOW",
                            source="osm_street_nearest_bias", layer="A")
        return None

    # ── house numbers ──────────────────────────────────────────────────────
    def _addr_keys(self, street: str) -> list[str]:
        ks = [k for k in expand_aliases(core_key(street)) if k in self.points or k in self.tiger]
        if ks:
            return ks
        return [c for c, _s, _ in process.extract(core_key(street), self.addr_keys, scorer=fuzz.ratio,
                                                  limit=2, score_cutoff=85)]

    def _point_cands(self, key, number, block, want_dir, want_sfx) -> list[dict]:
        d = self.points.get(key)
        if d is None:
            return []
        nums = d["num"]
        base = np.ones(len(nums), bool)
        if want_dir is not None and (d["dir"] == want_dir).any():
            base &= d["dir"] == want_dir
        if want_sfx and ((d["sfx"] == want_sfx) & base).any():
            base &= (d["sfx"] == want_sfx) | (d["sfx"] == 0)
        lo, hi = (number, number + 99) if block else (number, number)
        hit = base & (nums >= lo) & (nums <= hi)
        out = []
        if hit.any():
            for ci in np.unique(d["city"][hit]):
                m = hit & (d["city"] == ci)
                best_src = int(d["src"][m].min())          # official layers beat OSM when both exist
                m &= d["src"] == best_src
                for la, lo_, n in self._spread(d["lat"][m], d["lon"][m]):
                    layer = "osm_addr" if best_src == _OSM_SRC else "points"
                    out.append(dict(lat=la, lng=lo_, city=self.cities[int(ci)], rank=_RANK[layer],
                                    conf="HIGH", source=f"{SRC_CODES[best_src]}_points" +
                                    ("_block_mean" if block else ""), n=n))
            return out
        # interpolate between neighbouring numbers on the same side, city by city
        target = number + 50 if block else number
        for ci in np.unique(d["city"][base]):
            sel = np.nonzero(base & (d["city"] == ci))[0]
            same_side = sel[(nums[sel] % 2) == (target % 2)]
            for idx in (same_side, sel):
                if len(idx) < 2:
                    continue
                order = np.argsort(nums[idx], kind="stable")
                ns, ii = nums[idx][order], idx[order]
                i = int(np.searchsorted(ns, target))
                if not 0 < i < len(ns):
                    continue
                a, b = ii[i - 1], ii[i]
                pa, pb = (float(d["lat"][a]), float(d["lon"][a])), (float(d["lat"][b]), float(d["lon"][b]))
                if ns[i] - ns[i - 1] > 1200 or hav(pa, pb) > 1500:
                    continue
                f = (target - ns[i - 1]) / max(1, ns[i] - ns[i - 1])
                out.append(dict(lat=pa[0] + f * (pb[0] - pa[0]), lng=pa[1] + f * (pb[1] - pa[1]),
                                city=self.cities[int(ci)], rank=_RANK["points_interp"], conf="MEDIUM",
                                source="points_interp", n=2))
                break
        return out

    @staticmethod
    def _spread(lat: np.ndarray, lon: np.ndarray, eps_m: float = 600.0) -> list[tuple[float, float, int]]:
        """Split same-number points that are far apart (two 'Main St's in one township)."""
        groups: list[list[int]] = []
        for i in range(len(lat)):
            p = (float(lat[i]), float(lon[i]))
            for g in groups:
                if hav(p, (float(lat[g[0]]), float(lon[g[0]]))) <= eps_m:
                    g.append(i)
                    break
            else:
                groups.append([i])
        return [(float(lat[g].mean()), float(lon[g].mean()), len(g)) for g in groups]

    def _tiger_cands(self, key, number, block, want_dir, want_sfx_name) -> list[dict]:
        target = number + 50 if block else number
        out = []
        segs = self.tiger.get(key, [])
        if want_sfx_name and any(s[6] == want_sfx_name for s in segs):
            segs = [s for s in segs if s[6] in (want_sfx_name, "")]
        for frm, to, _parity, coords, zp, fn, _sfx in segs:
            lo_, hi_ = min(frm, to), max(frm, to)
            if not (lo_ <= target <= hi_ or (block and lo_ <= number + 99 and hi_ >= number)):
                continue
            if want_dir and leading_dir(fn) and leading_dir(fn) != want_dir:
                continue
            f = 0.5 if hi_ == lo_ else (min(max(target, lo_), hi_) - frm) / (to - frm)
            la, lo = along(coords, f)
            out.append(dict(lat=la, lng=lo, city=None, rank=_RANK["tiger_interp"], conf="MEDIUM",
                            source="tiger_addrfeat_interp", fn=fn, zip=zp))
        return out

    def address(self, number, street, bias, city=None, block=False):
        number = int(number)
        dir_name = leading_dir(street)
        want_dir = DIR_CODES.index(dir_name) if dir_name in DIR_CODES else None
        sfx_name = street_suffix(street)
        want_sfx = SUFFIX_CODES.index(sfx_name) if sfx_name in SUFFIX_CODES else 0
        cands: list[dict] = []
        for k in self._addr_keys(street):
            cands += self._point_cands(k, number, block, want_dir, want_sfx)
            cands += self._tiger_cands(k, number, block, dir_name, sfx_name or "")
        if cands:
            for c in cands:
                c["dist"] = hav((c["lat"], c["lng"]), bias)
            cands.sort(key=lambda c: c["dist"])
            for c in cands[:12]:                         # TIGER rows carry no city: tag the nearest few
                if c["city"] is None:
                    c["city"] = self.city_of(c["lat"], c["lng"])
            pool = cands
            if city:
                matched = [c for c in cands if city_match(city, c.get("city"))]
                pool = matched or cands
            d_min = min(c["dist"] for c in pool)
            pool = [c for c in pool if c["dist"] <= d_min + 5000]
            best = min(pool, key=lambda c: (c["rank"], c["dist"]))
            cm = city_match(city, best.get("city"))
            if block and best["source"].endswith("_block_mean"):
                best = self._snap_to_street(best, street)
            if best["dist"] <= ADDR_MAX_BIAS_M or cm:
                where = best.get("city") or ""
                label = f"{number} {street}" + (f", {where}" if where else "")
                if block:
                    label = f"{number} block of {street}" + (f", {where}" if where else "")
                return dict(lat=float(best["lat"]), lng=float(best["lng"]), formatted=label,
                            confidence=best["conf"], source=best["source"], layer="B", city=where or None)
        return self.street_point(street, bias)

    def _snap_to_street(self, cand: dict, street: str, max_m: float = 500.0) -> dict:
        """A block is a stretch of street: move the mean of its address points (parcel centres can
        sit 200+ m back on big lots) onto the street centreline when that is within max_m."""
        for k, _sc in self.resolve_keys(street):
            arr, t = self._kdtree(k)
            if t is None:
                continue
            d, i = t.query(xy(cand["lat"], cand["lng"]))
            if d <= max_m:
                return {**cand, "lat": float(arr[i][0]), "lng": float(arr[i][1])}
        return cand

    # ── landmarks ──────────────────────────────────────────────────────────
    def landmark(self, name, bias, city=None, street1=None, street2=None):
        nm = re.sub(r"^the ", "", basic(name))
        m = re.search(r"(\d+)(?:st|nd|rd|th)? precinct|(\w+) precinct", nm)
        if m:
            num = int(m.group(1)) if m.group(1) else PRECINCT_WORDS.get(m.group(2), 0)
            if num:
                return self._poi_search([f"{num}th precinct", f"{num}st precinct", f"{num}nd precinct",
                                         f"{num}rd precinct", f"precinct {num}"], bias, kinds=("amenity=police",),
                                        min_score=85)
        anchor = None
        if street1 and street2:
            anchor = self.intersection(street1, street2, bias, city)
        elif street1:
            anchor = self.street_point(street1, bias)
        search = (LANDMARK_ALIASES.get(nm) or LANDMARK_ALIASES.get(re.sub(r"[^a-z0-9 ]", "", nm))
                  or LANDMARK_ALIASES.get(f"the {nm}") or name)
        center = (anchor["lat"], anchor["lng"]) if anchor else bias
        r = self._poi_search(list(dict.fromkeys([search, name])), center, max_m=1500 if anchor else 40000,
                             brand_mode=bool(anchor), city=None if anchor else city)
        if r is None and anchor:
            anchor = dict(anchor)
            anchor["confidence"] = "MEDIUM"
            anchor["source"] += "+landmark_unmatched"
            return anchor
        return r

    def _poi_search(self, names, center, max_m=40000, kinds=None, min_score=85, brand_mode=False, city=None):
        if self.poi_tree is None:
            return None
        idx = self.poi_tree.query_ball_point(xy(*center), r=max_m)
        if not idx:
            return None
        sub_names = [self.poi_names[i] for i in idx]
        best = None
        for q in names:
            qn = re.sub(r"^the ", "", basic(q))
            qt = set(qn.split()) - STOP
            if not qt:
                continue
            for _, _pre, j in process.extract(qn, sub_names, scorer=fuzz.token_set_ratio, limit=60, score_cutoff=70):
                i = idx[j]
                nm, kind, la, lo, c = self.pois[i]
                if kinds and kind not in kinds:
                    continue
                if kind in LOW_VALUE_KINDS or (not brand_mode and kind.startswith("shop=")
                                               and kind not in ("shop=mall", "shop=supermarket")):
                    continue
                if kind.startswith("place=") and kind.split("=")[1] in ("city", "town", "village") and len(qt) > 1:
                    continue
                score = name_score(qt, qn, self.poi_names[i])
                if score < min_score:
                    continue
                dist = hav((la, lo), center)
                pri = next((n for n, kp in enumerate(KIND_PRIORITY) if kind.startswith(kp)), len(KIND_PRIORITY))
                cm = city_match(city, c)
                bucket = 2 if score >= 94 else (1 if score >= 85 else 0)
                key = (-bucket, not cm, kind in LOW_PRI_KINDS, score < 99.5, pri, -score, dist)
                if best is None or key < best[0]:
                    best = (key, dict(lat=la, lng=lo, formatted=f"{nm}{', ' + c if c else ''}",
                                      confidence="HIGH" if score >= 95 else "MEDIUM",
                                      source=f"poi:{kind}", layer="C", city=c or None))
        return best[1] if best else None

    # ── entry ──────────────────────────────────────────────────────────────
    def geocode(self, s: dict, bias=(42.33, -83.05)) -> dict | None:
        kind = s.get("kind")
        city = s.get("city")
        bias = (float(bias[0]), float(bias[1]))
        if kind == "intersection" and s.get("street1") and s.get("street2"):
            return (self.intersection(s["street1"], s["street2"], bias, city)
                    or self.street_point(s["street1"], bias))
        if kind in ("highway_segment", "highway") and s.get("street1") and s.get("street2"):
            return self.highway_segment(s["street1"], s["street2"], bias, city)
        if kind in ("block", "address") and s.get("street1"):
            if s.get("number") is None:
                return self.street_point(s["street1"], bias)
            return self.address(s["number"], s["street1"], bias, city, block=(kind == "block"))
        if kind == "landmark" and (s.get("name") or s.get("street1")):
            return self.landmark(s.get("name") or s["street1"], bias, city, s.get("street1") if s.get("name") else None,
                                 s.get("street2") if s.get("name") else None)
        if kind in ("street", "intersection", "highway_segment", "highway") and s.get("street1"):
            return self.street_point(s["street1"], bias)
        return None
