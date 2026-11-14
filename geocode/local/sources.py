"""Data sources for the local geocoder index: where they come from, their licences, downloaders.

Every source is free and needs no account or key. See docs/geocoder.md for the licence review.

    osm            Geofabrik Michigan extract                 ODbL 1.0, attribution required
    tiger_addrfeat Census TIGER/Line ADDRFEAT, per county     US public domain
    detroit        City of Detroit Base Units addresses       public domain (OpenAddresses)
    oakland        Oakland County "OC Site Address" points    Oakland County Open Data Terms of Use
    annarbor       City of Ann Arbor Address Points           OFF by default (see LICENSE_NOTES)
    tiger_roads, tiger_arealm                                 eval ground truth only, not indexed

Downloads land in <data_dir>/downloads/. Nothing here ever writes inside the repo.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import os
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

TIGER_YEAR = 2026
# Wayne, Oakland, Washtenaw, Macomb
DEFAULT_COUNTIES = {"26163": "Wayne", "26125": "Oakland", "26161": "Washtenaw", "26099": "Macomb"}
USER_AGENT = "detroit-pulse-geocoder-build/0.3 (self-hosted index build)"
GEOFABRIK_MICHIGAN = "https://download.geofabrik.de/north-america/us/michigan-latest.osm.pbf"


def tiger_url(layer: str, fips: str, year: int = TIGER_YEAR) -> str:
    # layer: ADDRFEAT | ROADS (county files) ; AREALM is a state file (fips = "26")
    return f"https://www2.census.gov/geo/tiger/TIGER{year}/{layer.upper()}/tl_{year}_{fips}_{layer.lower()}.zip"


@dataclass(frozen=True)
class PointSource:
    id: str
    name: str
    url: str                      # ArcGIS layer URL, or CKAN package API URL
    protocol: str                 # "arcgis" | "ckan_geojson"
    license: str
    attribution: str
    default_enabled: bool
    # number / city: a field name or alternatives (first non-empty wins); street_parts are joined
    # ("E" "Warren" "Ave"), street lists alternatives for a ready-made full name
    fields: dict = field(default_factory=dict)
    city: str | None = None       # fixed city when the layer has no city field
    notes: str = ""


POINT_SOURCES = {
    "detroit": PointSource(
        id="detroit", name="City of Detroit Base Units: Addresses",
        url="https://services2.arcgis.com/qvkbeam7Wirps6zC/arcgis/rest/services/BaseUnitFeatures/FeatureServer/0",
        protocol="arcgis",
        license="Public domain (no licence on the City item; OpenAddresses lists it as public domain, no attribution)",
        attribution="City of Detroit", default_enabled=True,
        fields={"number": "street_number", "street_parts": ["street_prefix", "street_name", "street_type"]},
        city="Detroit",
        notes="~487k rows incl. units (units sit at the building centre)."),
    "oakland": PointSource(
        id="oakland", name="Oakland County: OC Site Address",
        url="https://gisservices.oakgov.com/arcgis/rest/services/Enterprise/EnterpriseOpenParcelDataMapService/MapServer/0",
        protocol="arcgis",
        license=("Oakland County Open Data Terms of Use (item 32a0c4826b8f44bd9a33712320626ef3): 'You may "
                 "retrieve, download, copy, modify, translate, adapt, distribute, sort, search, and reuse "
                 "Open Data for any lawful purpose.' Includes an as-is disclaimer and an indemnification "
                 "clause; downloading accepts the terms."),
        attribution="Oakland County, Michigan", default_enabled=True,
        fields={"number": "SITESTREETNUMBER",
                "street_parts": ["SITEPREFIXONE", "SITESTREETNAME", "SITESTREETTYPE", "SITESUFFIXONE"],
                "city": "SITECITY"},
        notes="~560k parcel points (centre of the tax parcel, not the rooftop)."),
    "annarbor": PointSource(
        id="annarbor", name="City of Ann Arbor: Address Points",
        url="https://ckan.a2gov.org/api/3/action/package_show?id=address-points",
        protocol="ckan_geojson",
        license=("City of Ann Arbor terms (a2gov.org/policies-and-notices): City data is public domain "
                 "unless stated otherwise, BUT anyone who modifies, uses or presents it 'in a manner "
                 "other than originally presented' must notify the City IT Director (734.794.6550) and "
                 "carry the City's modified-data disclaimer. A geocoder index is such a use, so this "
                 "source is OFF until that notice has been given."),
        attribution="City of Ann Arbor", default_enabled=False,
        fields={"number": ["ADDRNUM", "ADDNUM", "HOUSENUM", "STREETNUM", "ADDRESSNUMBER"],
                "street": ["FULLNAME", "STREETNAME", "FULLSTREET", "STNAME"]},
        city="Ann Arbor"),
}

LICENSE_NOTES = {
    "osm": "(c) OpenStreetMap contributors, ODbL 1.0. Show the attribution wherever results are displayed; "
           "the built index is a derivative database (keep it private or publish it under the ODbL).",
    "tiger": "US Census Bureau TIGER/Line: public domain (US government work), no attribution required.",
    "not_used": {
        "washtenaw_county": "Washtenaw County's open-data portal returns 401 (private ArcGIS org) and its old "
                            "ParcelPublish service is gone (404); no published licence found. Not used: "
                            "Washtenaw relies on TIGER ranges + OSM addr:* (+ Ann Arbor points if enabled).",
        "wayne_county": "Wayne County's parcel layer has no licence statement and carries owner names. Not used: "
                        "Wayne suburbs rely on TIGER ranges + OSM addr:*.",
        "semcog": "SEMCOG buildings/addresses: licence text says 'Reproduction or Use Without Permission is "
                  "Prohibited'. Not used.",
        "openaddresses": "Mirrors need a login (HTTP 403 without one). Not used; its source files were only read "
                         "for licence metadata.",
    },
}


# ── HTTP helpers ────────────────────────────────────────────────────────────

def _client():
    import httpx
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(120.0, connect=20.0),
                        follow_redirects=True)


def _retry(fn: Callable, tries: int = 5, what: str = ""):
    delay = 2.0
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise RuntimeError(f"{what}: {e}") from e
            time.sleep(delay)
            delay *= 2


def download(url: str, dest: Path, log: Callable[[str], None] = print, md5: str | None = None) -> Path:
    """Stream url -> dest (via dest.part), verify md5 if given."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    def _go():
        h = hashlib.md5()
        n = 0
        with _client() as c, c.stream("GET", url) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
        if md5 and h.hexdigest() != md5:
            raise ValueError(f"md5 mismatch for {url}: {h.hexdigest()} != {md5}")
        return n
    t0 = time.time()
    n = _retry(_go, what=f"download {url}")
    os.replace(part, dest)
    log(f"downloaded {url} -> {dest.name} ({n / 1e6:.1f} MB in {time.time() - t0:.0f}s)")
    return dest


def fresh(path: Path, max_age_days: float) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age_days * 86400


def fetch_osm(dl_dir: Path, max_age_days: float, log=print) -> Path:
    dest = dl_dir / "michigan-latest.osm.pbf"
    if fresh(dest, max_age_days):
        log(f"osm: using {dest.name} (younger than {max_age_days:g} days)")
        return dest

    def _md5():
        with _client() as c:
            r = c.get(GEOFABRIK_MICHIGAN + ".md5")
            r.raise_for_status()
            return r.text.split()[0]
    md5 = _retry(_md5, what="geofabrik md5")
    return download(GEOFABRIK_MICHIGAN, dest, log, md5=md5)


def fetch_tiger(layer: str, fips: str, dl_dir: Path, max_age_days: float, log=print) -> Path:
    url = tiger_url(layer, fips)
    dest = dl_dir / "tiger" / url.rsplit("/", 1)[1]
    if fresh(dest, max_age_days):
        return dest
    download(url, dest, log)
    with zipfile.ZipFile(dest) as z:
        bad = z.testzip()
        if bad:
            dest.unlink()
            raise RuntimeError(f"corrupt TIGER zip {dest.name} ({bad})")
    return dest


# ── address point layers (normalized to gz CSV: number,street,city,lat,lon) ─

POINT_COLUMNS = ["number", "street", "city", "lat", "lon"]


def _pick(props: dict, spec) -> str:
    if isinstance(spec, str):
        v = props.get(spec)
        return "" if v is None else str(v).strip()
    for s in spec:                       # first present field (numbers) / joined parts (streets)
        v = props.get(s)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _street(props: dict, fields: dict) -> str:
    if "street_parts" in fields:
        return " ".join(str(props[f]).strip() for f in fields["street_parts"] if props.get(f) not in (None, ""))
    return _pick(props, fields["street"])


def _arcgis_rows(src: PointSource, log, page_delay_s: float = 0.2) -> Iterator[dict]:
    with _client() as c:
        meta = _retry(lambda: c.get(src.url, params={"f": "json"}).raise_for_status().json(),
                      what=f"{src.id} layer metadata")
        page = min(int(meta.get("maxRecordCount") or 1000), 2000)
        ids = _retry(lambda: c.get(src.url + "/query", params={"where": "1=1", "returnIdsOnly": "true",
                                                               "f": "json"}).raise_for_status().json(),
                     what=f"{src.id} object ids")
        oid = ids.get("objectIdFieldName") or "OBJECTID"
        all_ids = sorted(ids.get("objectIds") or [])
        log(f"{src.id}: {len(all_ids)} features, {(len(all_ids) + page - 1) // page} pages")
        out_fields = []
        for v in src.fields.values():
            out_fields += [v] if isinstance(v, str) else list(v)
        for i in range(0, len(all_ids), page):
            lo, hi = all_ids[i], all_ids[min(i + page, len(all_ids)) - 1]
            params = {"where": f"{oid} >= {lo} AND {oid} <= {hi}", "outFields": ",".join(out_fields),
                      "outSR": "4326", "returnGeometry": "true", "f": "geojson"}
            data = _retry(lambda p=params: c.get(src.url + "/query", params=p).raise_for_status().json(),
                          what=f"{src.id} page {i // page}")
            for ft in data.get("features") or []:
                g = ft.get("geometry") or {}
                if g.get("type") != "Point" or len(g.get("coordinates") or []) < 2:
                    continue
                lon, lat = g["coordinates"][:2]
                p = ft.get("properties") or {}
                yield {"number": _pick(p, src.fields["number"]), "street": _street(p, src.fields),
                       "city": _pick(p, src.fields["city"]) if "city" in src.fields else (src.city or ""),
                       "lat": lat, "lon": lon}
            time.sleep(page_delay_s)


def _ckan_geojson_rows(src: PointSource, log) -> Iterator[dict]:
    with _client() as c:
        pkg = _retry(lambda: c.get(src.url).raise_for_status().json(), what=f"{src.id} package")["result"]
        res = next((r for r in pkg.get("resources", []) if (r.get("format") or "").lower() == "geojson"), None)
        if res is None:
            raise RuntimeError(f"{src.id}: no GeoJSON resource in {src.url}")
        data = _retry(lambda: c.get(res["url"]).raise_for_status().json(), what=f"{src.id} geojson")
    for ft in data.get("features") or []:
        g = ft.get("geometry") or {}
        if g.get("type") != "Point" or len(g.get("coordinates") or []) < 2:
            continue
        lon, lat = g["coordinates"][:2]
        p = {k.upper(): v for k, v in (ft.get("properties") or {}).items()}
        yield {"number": _pick(p, src.fields["number"]), "street": _street(p, src.fields),
               "city": src.city or "", "lat": lat, "lon": lon}


def fetch_points(src: PointSource, dl_dir: Path, max_age_days: float, log=print) -> Path:
    dest = dl_dir / f"points_{src.id}.csv.gz"
    if fresh(dest, max_age_days):
        log(f"{src.id}: using {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    t0 = time.time()
    rows = _arcgis_rows(src, log) if src.protocol == "arcgis" else _ckan_geojson_rows(src, log)
    n = 0
    with gzip.open(part, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(POINT_COLUMNS)
        for r in rows:
            w.writerow([r["number"], r["street"], r["city"], f"{r['lat']:.7f}", f"{r['lon']:.7f}"])
            n += 1
    if n == 0:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{src.id}: no rows downloaded")
    os.replace(part, dest)
    log(f"{src.id}: {n} rows -> {dest.name} in {time.time() - t0:.0f}s")
    return dest


def read_points(path: Path, src_id: str) -> Iterator[dict]:
    with gzip.open(path, "rt", newline="") as f:
        for r in csv.DictReader(f):
            try:
                yield {"number": r["number"], "street": r["street"], "city": r["city"],
                       "lat": float(r["lat"]), "lon": float(r["lon"]), "src": src_id}
            except (KeyError, ValueError):
                continue


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_manifest(enabled_points: list[str]) -> dict:
    return {
        "osm": {"url": GEOFABRIK_MICHIGAN, "license": LICENSE_NOTES["osm"]},
        "tiger_addrfeat": {"url": tiger_url("ADDRFEAT", "<county fips>"), "license": LICENSE_NOTES["tiger"]},
        "points": {k: {"name": s.name, "url": s.url, "license": s.license, "attribution": s.attribution,
                       "enabled": k in enabled_points} for k, s in POINT_SOURCES.items()},
        "not_used": LICENSE_NOTES["not_used"],
    }
