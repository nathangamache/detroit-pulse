"""Layer A builder: OSM streets, intersections, POIs and city polygons.

parse_pbf() makes two passes over a Geofabrik .osm.pbf with pyosmium (only objects carrying
highway/name/addr tags ever reach Python) and returns plain "raw" data; derive() turns raw data
into the index. derive() is pure Python + numpy, so tests feed it synthetic ways directly.

    streets        key -> [(way_id, highway, coords[N,2] lat/lon)]
    display        key -> up to 6 spellings seen in OSM
    intersections  "coreA|coreB" -> [(lat, lon, n_nodes, city, suffix-qualified keys there)]
                   (clustered within 250 m)
    full_keys      the suffix-qualified keys ("ann arbor trail") for cores used by 2+ street types
    pois           [(name, kind, lat, lon, city)]
    admin          [(name, admin_level, wkb)]                    (city/township polygons)
OSM addr:* points are returned in raw["addr"] for the address builder (build_addr.py).

Data: (c) OpenStreetMap contributors, ODbL 1.0. The derived index is a Derivative Database:
keep it private, or publish it under the ODbL.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Callable

import numpy as np

from geocode.local.normalize import core_key, full_key, ref_keys, street_suffix

HWY_OK = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "living_street", "road", "service",
}
NAME_TAGS = ("name", "alt_name", "old_name", "official_name", "short_name",
             "loc_name", "name_1", "name:en", "alt_name_1", "tiger:name_base")
POI_KEYS = ("amenity", "shop", "leisure", "tourism", "aeroway", "railway",
            "healthcare", "building", "office", "landuse", "historic", "man_made",
            "public_transport", "place")
POI_ALT_NAMES = ("alt_name", "short_name", "official_name")
POI_AMENITY_SKIP = {"parking_space", "bench", "waste_basket", "bicycle_parking",
                    "vending_machine", "post_box", "telephone", "drinking_water",
                    "parking_entrance", "charging_station", "atm", "toilets",
                    "shelter", "recycling", "fountain", "clock", "hunting_stand"}
AREA_KEYS = ("leisure", "aeroway", "amenity", "place", "tourism", "landuse", "shop",
             "historic", "natural", "healthcare", "building")
CLUSTER_M = 250.0
MAX_PAIR_POINTS = 400


def in_bbox(bbox: dict, lat: float, lon: float) -> bool:
    return bbox["south"] <= lat <= bbox["north"] and bbox["west"] <= lon <= bbox["east"]


def poi_kind(tags) -> str | None:
    for k in POI_KEYS:
        v = tags.get(k)
        if v:
            if k == "amenity" and v in POI_AMENITY_SKIP:
                return None
            if k == "place" and v not in ("neighbourhood", "suburb", "quarter", "city", "town",
                                          "village", "hamlet", "locality", "island"):
                return None
            return f"{k}={v}"
    return None


def _poi_rows(tags, kind: str, lat: float, lon: float, rel: bool = False) -> list[tuple]:
    out = [(tags.get("name"), kind, lat, lon, rel)]
    seen = {tags.get("name")}
    for t in POI_ALT_NAMES:
        v = tags.get(t)
        if v and v not in seen:
            seen.add(v)
            out.append((v, kind, lat, lon, rel))
    return out


# ── pass 1+2: read the PBF ──────────────────────────────────────────────────

def parse_pbf(pbf: str, bbox: dict, log: Callable[[str], None] = print) -> dict:
    import osmium   # build-time only dependency
    from shapely import wkb as swkb

    t0 = time.time()
    ways, pois, addr = [], [], []
    fp = (osmium.FileProcessor(pbf, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations("flex_mem")
          .with_filter(osmium.filter.KeyFilter("highway", "name", "addr:housenumber")))
    for o in fp:
        tags = o.tags
        if o.is_node():
            loc = o.location
            if not loc.valid() or not in_bbox(bbox, loc.lat, loc.lon):
                continue
            if "addr:housenumber" in tags and "addr:street" in tags:
                addr.append((tags["addr:housenumber"], tags["addr:street"], tags.get("addr:city", ""),
                             loc.lat, loc.lon))
            if tags.get("name"):
                kind = poi_kind(tags)
                if kind:
                    pois.extend(_poi_rows(tags, kind, loc.lat, loc.lon))
            continue
        hwy = tags.get("highway")
        name = tags.get("name")
        road = hwy in HWY_OK and not (hwy == "service" and not name)
        names = [(t, tags.get(t)) for t in NAME_TAGS if tags.get(t)] if road else []
        ref = tags.get("ref", "") if road else ""
        want_road = road and bool(names or ref)
        has_addr = "addr:housenumber" in tags and "addr:street" in tags
        kind = poi_kind(tags) if (name and not hwy) else None
        if not (want_road or has_addr or kind):
            continue
        coords, nids = [], []
        try:
            for nd in o.nodes:
                coords.append((nd.location.lat, nd.location.lon))
                nids.append(nd.ref)
        except osmium.InvalidLocationError:
            continue
        if not coords:
            continue
        if not (in_bbox(bbox, *coords[len(coords) // 2]) or in_bbox(bbox, *coords[0])
                or in_bbox(bbox, *coords[-1])):
            continue
        if want_road:
            ways.append((o.id, hwy, names, ref, np.asarray(coords, dtype=np.float64),
                         np.asarray(nids, dtype=np.int64)))
        if has_addr or kind:
            la = sum(c[0] for c in coords) / len(coords)
            lo = sum(c[1] for c in coords) / len(coords)
            if has_addr:
                addr.append((tags["addr:housenumber"], tags["addr:street"], tags.get("addr:city", ""), la, lo))
            if kind:
                pois.extend(_poi_rows(tags, kind, la, lo))
    t_parse = time.time() - t0
    log(f"osm pass 1: {len(ways)} named road ways, {len(pois)} POI names, {len(addr)} addr points "
        f"in {t_parse:.0f}s")

    # pass 2: city/township polygons + named multipolygon POIs (parks, airports, campuses)
    t1 = time.time()
    fab = osmium.geom.WKBFactory()
    admin, rel_pois = [], []
    fp2 = (osmium.FileProcessor(pbf)
           .with_areas(osmium.filter.KeyFilter("boundary", *AREA_KEYS))
           .with_filter(osmium.filter.EntityFilter(osmium.osm.AREA)))
    for obj in fp2:
        if not obj.is_area():
            continue
        t = obj.tags
        is_admin = t.get("boundary") == "administrative" and t.get("admin_level") in ("7", "8")
        if not is_admin and (obj.from_way() or not t.get("name")):
            continue
        try:
            g = swkb.loads(fab.create_multipolygon(obj), hex=True)
        except Exception:  # noqa: BLE001 - broken multipolygons are common; skip them
            continue
        if is_admin:
            c = g.representative_point()
            if in_bbox(bbox, c.y, c.x):
                admin.append((t.get("name", ""), t.get("admin_level"), g.wkb))
            continue
        kind = poi_kind(t)
        if kind is None:
            kind = next((f"{k}={t.get(k)}" for k in AREA_KEYS if t.get(k)), None)
        if kind is None:
            continue
        c = g.centroid
        pt = c if g.contains(c) else g.representative_point()
        if in_bbox(bbox, pt.y, pt.x):
            rel_pois.extend(_poi_rows(t, kind, pt.y, pt.x, rel=True))
    t_areas = time.time() - t1
    log(f"osm pass 2: {len(admin)} city/township polygons, {len(rel_pois)} multipolygon POI names "
        f"in {t_areas:.0f}s")
    return {"ways": ways, "pois": pois + rel_pois, "addr": addr, "admin": admin,
            "parse_s": round(t_parse, 1), "areas_s": round(t_areas, 1)}


# ── derive ──────────────────────────────────────────────────────────────────

def hav_m(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(d))


def cluster_groups(points: list[tuple[float, float]], eps_m: float = CLUSTER_M) -> list[list[int]]:
    """Single-linkage clustering of [(lat, lon)] -> groups of indices, biggest first."""
    n = len(points)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(n):
        for j in range(i + 1, n):
            if hav_m(points[i], points[j]) <= eps_m:
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return sorted(groups.values(), key=len, reverse=True)


def cluster(points: list[tuple[float, float]], eps_m: float = CLUSTER_M) -> list[tuple[float, float, int]]:
    """[(lat, lon)] -> [(lat, lon, n)] cluster centres, biggest first."""
    out = []
    for g in cluster_groups(points, eps_m):
        out.append((sum(points[i][0] for i in g) / len(g), sum(points[i][1] for i in g) / len(g), len(g)))
    return out


class CityTagger:
    """Point -> city/township name from the OSM admin polygons (level 8 beats 7)."""

    def __init__(self, admin: list[tuple]):
        from shapely import STRtree
        from shapely import wkb as swkb
        self.adm = [(n, lvl, swkb.loads(g)) for n, lvl, g in admin]
        self.tree = STRtree([a[2] for a in self.adm]) if self.adm else None

    def __call__(self, lat: float, lon: float) -> str:
        if self.tree is None:
            return ""
        from shapely.geometry import Point
        p = Point(lon, lat)
        best = None
        for i in self.tree.query(p):
            nm, lvl, g = self.adm[int(i)]
            if g.contains(p):
                score = (0 if lvl == "8" else 1, g.area)
                if best is None or score < best[0]:
                    best = (score, nm)
        return best[1] if best else ""


def _name_parts(name_pairs) -> list[str]:
    out = []
    for _t, v in name_pairs:
        for part in str(v).split(";"):
            part = part.strip()
            if part:
                out.append(part)
    return out


def derive(raw: dict, bbox: dict, log: Callable[[str], None] = print) -> tuple[dict, dict]:
    t0 = time.time()
    # Core keys whose roads carry two or more street types (Ann Arbor Trail / Ann Arbor Road):
    # those also get suffix-qualified keys so "<street> and Ann Arbor Trail" can't land on Ann Arbor Rd.
    suffixes: dict[str, set] = defaultdict(set)
    for _wid, _hwy, name_pairs, _ref, _c, _n in raw["ways"]:
        for part in _name_parts(name_pairs):
            sfx = street_suffix(part)
            if sfx:
                suffixes[core_key(part)].add(sfx)
    ambiguous = {k for k, s in suffixes.items() if len(s) >= 2}

    ways = []
    for wid, hwy, name_pairs, ref, coords, nids in raw["ways"]:
        names = _name_parts(name_pairs)
        cores, fulls = set(), set()
        for part in names:
            k = core_key(part)
            if k:
                cores.add(k)
                if k in ambiguous:
                    fk = full_key(part)
                    if fk:
                        fulls.add(fk)
        cores.update(ref_keys(ref or ""))
        if cores:
            ways.append((wid, hwy, cores, fulls, names, np.asarray(coords, dtype=np.float64),
                         np.asarray(nids, dtype=np.int64)))

    # streets are indexed under core AND suffix-qualified keys (closest-approach / nearest-point lookups)
    streets: dict[str, list] = defaultdict(list)
    display: dict[str, set] = defaultdict(set)
    for wid, hwy, cores, fulls, names, coords, _nids in ways:
        for k in cores | fulls:
            streets[k].append((wid, hwy, coords))
            if len(display[k]) < 6:
                display[k].update(names[:2])
    full_keys = sorted({fk for w in ways for fk in w[3]})

    # intersections: nodes shared by ways with different core keys. Pairs use core keys only;
    # each cluster lists the suffix-qualified keys present at it ("ann arbor trail"), which the
    # lookup uses as a filter. (Indexing full-key pairs separately tripled the pair count.)
    all_ids = np.concatenate([w[6] for w in ways]) if ways else np.zeros(0, dtype=np.int64)
    uniq, counts = np.unique(all_ids, return_counts=True)
    shared = set(uniq[counts >= 2].tolist())
    node_keys: dict[int, set] = defaultdict(set)
    node_full: dict[int, set] = defaultdict(set)
    node_loc: dict[int, tuple[float, float]] = {}
    for _wid, _hwy, cores, fulls, _names, coords, nids in ways:
        for i, nid in enumerate(nids.tolist()):
            if nid in shared:
                node_keys[nid].update(cores)
                if fulls:
                    node_full[nid].update(fulls)
                node_loc[nid] = (float(coords[i, 0]), float(coords[i, 1]))
    raw_pairs: dict[str, list] = defaultdict(list)
    for nid, keys in node_keys.items():
        if len(keys) < 2:
            continue
        ks = sorted(keys)
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                raw_pairs[f"{ks[i]}|{ks[j]}"].append(nid)
    # two keys sharing >60% of their ways are synonyms (woodward|m 1), not crossing streets
    way_sets: dict[str, set] = defaultdict(set)
    for w in ways:
        for k in w[2]:
            way_sets[k].add(w[0])
    intersections, synonyms = {}, 0
    for pair, nodes in raw_pairs.items():
        a, b = pair.split("|")
        inter = len(way_sets[a] & way_sets[b])
        if inter and inter / max(1, min(len(way_sets[a]), len(way_sets[b]))) > 0.6:
            synonyms += 1
            continue
        nodes = nodes[:MAX_PAIR_POINTS]
        pts = [node_loc[nid] for nid in nodes]
        out = []
        for g in cluster_groups(pts):
            fk = sorted({f for i in g for f in node_full.get(nodes[i], ()) if f.rsplit(" ", 1)[0] in (a, b)})
            out.append((sum(pts[i][0] for i in g) / len(g), sum(pts[i][1] for i in g) / len(g), len(g), tuple(fk)))
        intersections[pair] = out
    t_pairs = time.time() - t0

    t1 = time.time()
    city_of = CityTagger(raw.get("admin") or [])
    for pair, cl in intersections.items():
        intersections[pair] = [(la, lo, n, city_of(la, lo), fk) for la, lo, n, fk in cl]
    pois = [(str(nm), kind, float(la), float(lo), city_of(la, lo)) for nm, kind, la, lo, _rel in raw["pois"] if nm]
    t_city = time.time() - t1

    index = {
        "bbox": dict(bbox),
        "streets": dict(streets),
        "display": {k: sorted(v) for k, v in display.items()},
        "intersections": intersections,
        "full_keys": full_keys,
        "pois": pois,
        "admin": list(raw.get("admin") or []),
    }
    stats = {
        "derive_s": round(time.time() - t0, 1), "pairs_s": round(t_pairs, 1), "city_tag_s": round(t_city, 1),
        "road_ways": len(ways), "street_keys": len(streets), "suffix_qualified_cores": len(ambiguous),
        "intersection_pairs": len(intersections), "synonym_pairs_dropped": synonyms,
        "intersection_clusters": sum(len(v) for v in intersections.values()),
        "poi_names": len(pois), "admin_polygons": len(index["admin"]),
    }
    log(f"osm derive: {stats['street_keys']} street keys, {stats['intersection_pairs']} intersection pairs "
        f"in {stats['derive_s']:.0f}s")
    return index, stats
