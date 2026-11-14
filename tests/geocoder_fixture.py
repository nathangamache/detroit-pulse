"""A tiny synthetic geocoder index built from a few PUBLIC streets (approximate, straight-line geometry).

Nothing here comes from OSM or any personal data: the streets are hand-drawn straight lines near
their real public locations, good enough to exercise normalization, intersection derivation,
city tagging, address interpolation and landmark search.

    Plymouth   S/N Main Street (N-S) x W/E Ann Arbor Trail and x Ann Arbor Road (two different roads
               whose core key is "ann arbor"), M-14 freeway crossing Main without a shared node
    Ann Arbor  S State Street x E Liberty Street, S Main Street x W Liberty Street
    Ferndale   Woodward Avenue (ref M 1) x W/E 9 Mile Road
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from shapely.geometry import box

PLYMOUTH = (42.3714, -83.4702)
ANN_ARBOR = (42.2808, -83.7430)
FERNDALE = (42.4606, -83.1346)

# expected crossings (where the straight lines meet)
MAIN_X_AA_TRAIL = (42.3707, -83.4705)
MAIN_X_AA_ROAD = (42.3540, -83.4705)
STATE_X_LIBERTY = (42.2797, -83.7403)
AA_MAIN_X_LIBERTY = (42.2797, -83.7485)
WOODWARD_X_9MILE = (42.4605, -83.1349)
M14_OVER_MAIN = (42.3900, -83.4705)


def _line(a, b, n=6):
    return [(a[0] + (b[0] - a[0]) * i / (n - 1), a[1] + (b[1] - a[1]) * i / (n - 1)) for i in range(n)]


class _Net:
    def __init__(self):
        self.ways, self.nid, self.wid = [], 1000, 1
        self.shared: dict[tuple, int] = {}

    def node(self, p, share=True):
        key = (round(p[0], 6), round(p[1], 6))
        if share and key in self.shared:
            return self.shared[key]
        self.nid += 1
        if share:
            self.shared[key] = self.nid
        return self.nid

    def way(self, pts, hwy, names, ref="", share=True):
        coords = np.array(pts, dtype=np.float64)
        nids = np.array([self.node(p, share) for p in pts], dtype=np.int64)
        self.wid += 1
        self.ways.append((self.wid, hwy, [("name", n) for n in names], ref, coords, nids))


def raw_osm() -> dict:
    n = _Net()
    # Plymouth: Main runs north-south through both Ann Arbor Trail and Ann Arbor Road
    n.way(_line((42.3800, -83.4705), MAIN_X_AA_TRAIL, 4), "primary", ["North Main Street"])
    n.way(_line(MAIN_X_AA_TRAIL, MAIN_X_AA_ROAD, 6), "primary", ["South Main Street"])
    n.way(_line((42.3707, -83.4850), MAIN_X_AA_TRAIL, 4), "secondary", ["West Ann Arbor Trail"])
    n.way(_line(MAIN_X_AA_TRAIL, (42.3707, -83.4550), 4), "secondary", ["East Ann Arbor Trail"])
    n.way(_line((42.3540, -83.4900), MAIN_X_AA_ROAD, 4), "primary", ["Ann Arbor Road"])
    n.way(_line(MAIN_X_AA_ROAD, (42.3540, -83.4500), 4), "primary", ["Ann Arbor Road"])
    # M-14 passes over Main (no shared node: a grade separation), Main extended north under it
    n.way(_line((42.3800, -83.4705), (42.4000, -83.4705), 9), "primary", ["North Main Street"])
    n.way(_line((42.3900, -83.4900), (42.3900, -83.4500), 33), "motorway", [], ref="M 14", share=False)
    # Ann Arbor
    n.way(_line((42.2900, -83.7403), STATE_X_LIBERTY, 4), "secondary", ["North State Street"])
    n.way(_line(STATE_X_LIBERTY, (42.2700, -83.7403), 4), "secondary", ["South State Street"])
    n.way(_line(AA_MAIN_X_LIBERTY, STATE_X_LIBERTY, 5), "tertiary", ["East Liberty Street"])
    n.way(_line((42.2797, -83.7550), AA_MAIN_X_LIBERTY, 4), "tertiary", ["West Liberty Street"])
    n.way(_line((42.2900, -83.7485), AA_MAIN_X_LIBERTY, 4), "secondary", ["North Main Street"])
    n.way(_line(AA_MAIN_X_LIBERTY, (42.2700, -83.7485), 4), "secondary", ["South Main Street"])
    # Ferndale: Woodward (also ref M 1) x 9 Mile
    n.way(_line((42.4500, -83.1290), WOODWARD_X_9MILE, 5), "primary", ["Woodward Avenue"], ref="M 1")
    n.way(_line(WOODWARD_X_9MILE, (42.4700, -83.1400), 5), "primary", ["Woodward Avenue"], ref="M 1")
    n.way(_line((42.4605, -83.1600), WOODWARD_X_9MILE, 5), "secondary", ["West 9 Mile Road"])
    n.way(_line(WOODWARD_X_9MILE, (42.4605, -83.1100), 5), "secondary", ["East 9 Mile Road"])
    admin = [("Plymouth", "8", box(-83.4950, 42.3450, -83.4450, 42.4050).wkb),
             ("Ann Arbor", "8", box(-83.8000, 42.2200, -83.6800, 42.3200).wkb),
             ("Ferndale", "8", box(-83.1650, 42.4450, -83.1050, 42.4750).wkb)]
    pois = [("Kellogg Park", "leisure=park", 42.3712, -83.4710, False),
            ("Michigan Stadium", "leisure=stadium", 42.2658, -83.7487, False),
            ("The Big House", "leisure=stadium", 42.2658, -83.7487, False),
            ("Ann Arbor Bus Stop 12", "highway=bus_stop", 42.2797, -83.7410, False)]
    addr = [("150", "West Ann Arbor Trail", "Plymouth", 42.37075, -83.4712)]
    return {"ways": n.ways, "pois": pois, "addr": addr, "admin": admin}


BBOX = {"south": 42.20, "west": -83.80, "north": 42.48, "east": -83.10}


def write_tiger_zip(path: Path) -> Path:
    """An ADDRFEAT-shaped shapefile (zipped) for S Main St, Plymouth: 100-998 from Ann Arbor Trail south."""
    import zipfile

    import shapefile
    stem = path.with_suffix("")
    w = shapefile.Writer(str(stem), shapeType=shapefile.POLYLINE)
    for f, t, s in (("FULLNAME", "C", 100), ("LFROMHN", "C", 12), ("LTOHN", "C", 12), ("RFROMHN", "C", 12),
                    ("RTOHN", "C", 12), ("PARITYL", "C", 1), ("PARITYR", "C", 1), ("ZIPL", "C", 5), ("ZIPR", "C", 5)):
        w.field(f, t, s)
    w.line([[(MAIN_X_AA_TRAIL[1], MAIN_X_AA_TRAIL[0]), (MAIN_X_AA_ROAD[1], MAIN_X_AA_ROAD[0])]])
    w.record("S Main St", "101", "999", "100", "998", "O", "E", "48170", "48170")
    w.line([[(-83.1600, 42.4605), (WOODWARD_X_9MILE[1], WOODWARD_X_9MILE[0])]])
    w.record("W 9 Mile Rd", "1", "999", "2", "998", "O", "E", "48220", "48220")
    w.close()
    with zipfile.ZipFile(path, "w") as z:
        for ext in ("shp", "shx", "dbf"):
            z.write(f"{stem}.{ext}", f"{stem.name}.{ext}")
    return path


def point_rows() -> list[dict]:
    """Official-style address points: 22800-22898 Woodward Ave (Ferndale), evens on one side."""
    rows = []
    for i, num in enumerate(range(22800, 22900, 10)):
        rows.append({"number": str(num), "street": "Woodward Ave", "city": "Ferndale",
                     "lat": WOODWARD_X_9MILE[0] + 0.0004 * (i + 1), "lon": WOODWARD_X_9MILE[1] - 0.0002 * (i + 1),
                     "src": "oakland"})
    return rows


def build_fixture_index(tmp: Path) -> Path:
    from geocode.local import build_addr, build_osm
    from geocode.local.build import write_index
    raw = raw_osm()
    osm_index, _ = build_osm.derive(raw, BBOX, log=lambda s: None)
    tiger, _ = build_addr.tiger_ranges([write_tiger_zip(tmp / "tl_fixture_addrfeat.zip")], log=lambda s: None)
    rows = point_rows() + list(build_addr.osm_addr_rows(raw["addr"]))
    points, cities, _ = build_addr.build_points(rows, build_osm.CityTagger(raw["admin"]), log=lambda s: None)
    live, _ = write_index(tmp / "geocoder", osm_index, {"points": points, "cities": cities, "tiger": tiger},
                          {"built": "fixture", "bbox": BBOX})
    return live


def osm_xml(raw: dict) -> str:
    """The same synthetic network as OSM XML, to exercise the pyosmium parser."""
    nodes, lines = {}, []
    for _wid, _hwy, _names, _ref, coords, nids in raw["ways"]:
        for (la, lo), nid in zip(coords.tolist(), nids.tolist()):
            nodes[nid] = (la, lo)
    lines.append('<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6" generator="pulse-tests">')
    for nid, (la, lo) in sorted(nodes.items()):
        lines.append(f' <node id="{nid}" lat="{la:.7f}" lon="{lo:.7f}" version="1"/>')
    poi_id = 900000
    for nm, kind, la, lo, _rel in raw["pois"]:
        poi_id += 1
        k, v = kind.split("=")
        lines.append(f' <node id="{poi_id}" lat="{la}" lon="{lo}" version="1"><tag k="name" v="{nm}"/>'
                     f'<tag k="{k}" v="{v}"/></node>')
    for wid, hwy, names, ref, _coords, nids in raw["ways"]:
        tags = [f'<tag k="highway" v="{hwy}"/>'] + [f'<tag k="name" v="{v}"/>' for _t, v in names]
        if ref:
            tags.append(f'<tag k="ref" v="{ref}"/>')
        nds = "".join(f'<nd ref="{x}"/>' for x in nids.tolist())
        lines.append(f' <way id="{wid}" version="1">{nds}{"".join(tags)}</way>')
    lines.append("</osm>")
    return "\n".join(lines) + "\n"
