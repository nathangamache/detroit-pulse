"""Build the local geocoder index: download -> parse -> derive -> write -> atomic swap.

Layout under the data directory (default ~/.local/share/detroit-pulse/geocoder, never the repo):

    downloads/                 michigan-latest.osm.pbf, tiger/*.zip, points_<source>.csv.gz
    index/                     osm.pkl.gz, addr.pkl.gz, manifest.json   <- GEOCODER_LOCAL_INDEX
    index.new/, index.old/     only while a build is swapping

The running worker keeps the index it loaded; restart it to pick up a rebuild.
"""
from __future__ import annotations

import gc
import os
import resource
import shutil
import time
from pathlib import Path
from typing import Callable

from geocode.local import build_addr, build_osm, sources, store


def default_data_dir() -> Path:
    """GEOCODER_DATA_DIR (env or .env), else $XDG_DATA_HOME/detroit-pulse/geocoder."""
    from core.settings import get_settings
    return get_settings().geocoder_data_dir


def peak_rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 0)


def build_index(data_dir: Path, bbox: dict, counties: dict[str, str], point_sources: list[str],
                max_age_days: float = 25.0, osm_pbf: Path | None = None, download_only: bool = False,
                eval_data: bool = False, log: Callable[[str], None] = print) -> dict:
    t0 = time.time()
    data_dir = Path(data_dir).expanduser().resolve()
    dl = data_dir / "downloads"
    dl.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}

    # 1. downloads
    t = time.time()
    pbf = Path(osm_pbf) if osm_pbf else sources.fetch_osm(dl, max_age_days, log)
    addrfeat = [sources.fetch_tiger("ADDRFEAT", f, dl, max_age_days, log) for f in counties]
    point_files = {sid: sources.fetch_points(sources.POINT_SOURCES[sid], dl, max_age_days, log)
                   for sid in point_sources}
    if eval_data:   # TIGER all-roads + area landmarks: ground truth for scripts/eval_geocoder.py only
        for f in counties:
            sources.fetch_tiger("ROADS", f, dl, max_age_days, log)
        sources.fetch_tiger("AREALM", "26", dl, max_age_days, log)
    timings["download_s"] = round(time.time() - t, 1)
    if download_only:
        return {"download_s": timings["download_s"], "data_dir": str(data_dir)}

    # 2. OSM layer, written as soon as it is derived so its memory can be released
    new = data_dir / "index.new"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    rss: dict[str, float] = {}
    raw = build_osm.parse_pbf(str(pbf), bbox, log)
    timings["osm_parse_s"] = raw["parse_s"]
    timings["osm_areas_s"] = raw["areas_s"]
    rss["after_osm_parse_mb"] = peak_rss_mb()
    osm_index, osm_stats = build_osm.derive(raw, bbox, log)
    timings["osm_derive_s"] = osm_stats["derive_s"]
    rss["after_osm_derive_mb"] = peak_rss_mb()
    t = time.time()
    sizes = {"osm_bytes": store.save(osm_index, new / store.OSM_FILE)}
    timings["write_s"] = round(time.time() - t, 1)
    osm_addr, admin = raw["addr"], raw["admin"]
    del raw, osm_index
    gc.collect()

    # 3. address layer
    t = time.time()
    tiger, tiger_stats = build_addr.tiger_ranges(addrfeat, log)
    city_of = build_osm.CityTagger(admin)

    def rows():
        for sid, path in point_files.items():
            yield from sources.read_points(path, sid)
        yield from build_addr.osm_addr_rows(osm_addr)
    points, cities, pt_stats = build_addr.build_points(rows(), city_of, log)
    timings["addr_s"] = round(time.time() - t, 1)
    rss["after_addr_mb"] = peak_rss_mb()
    t = time.time()
    sizes["addr_bytes"] = store.save({"points": points, "cities": cities, "tiger": tiger}, new / store.ADDR_FILE)
    timings["write_s"] += round(time.time() - t, 1)

    # 4. manifest, then swap index.new -> index
    manifest = {
        "format": store.INDEX_FORMAT,
        "built": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "bbox": bbox,
        "counties": counties,
        "tiger_year": sources.TIGER_YEAR,
        "point_sources": point_sources,
        "inputs": {"osm_pbf": {"file": pbf.name, "bytes": pbf.stat().st_size,
                               "mtime": time.strftime("%Y-%m-%d", time.localtime(pbf.stat().st_mtime))},
                   "tiger_addrfeat": [p.name for p in addrfeat],
                   "points": {k: p.name for k, p in point_files.items()}},
        "sources": sources.source_manifest(point_sources),
        "attribution": "© OpenStreetMap contributors (ODbL); US Census Bureau TIGER/Line; "
                       + "; ".join(sources.POINT_SOURCES[s].attribution for s in point_sources),
        "stats": {**osm_stats, **tiger_stats, **pt_stats, "osm_addr_points": len(osm_addr), **sizes},
        "timings": {**timings, "total_s": round(time.time() - t0, 1)},
        "peak_rss_mb": peak_rss_mb(),
        "peak_rss_by_phase": rss,
    }
    store.write_manifest(new, manifest)
    live = _swap(data_dir)
    log(f"index written to {live} ({sum(sizes.values()) / 1e6:.1f} MB) in {manifest['timings']['total_s']:.0f}s, "
        f"peak RSS {manifest['peak_rss_mb']:.0f} MB")
    return manifest


def _swap(data_dir: Path) -> Path:
    new, live, old = data_dir / "index.new", data_dir / "index", data_dir / "index.old"
    shutil.rmtree(old, ignore_errors=True)
    if live.exists():
        os.replace(live, old)
    os.replace(new, live)
    shutil.rmtree(old, ignore_errors=True)
    return live


def write_index(data_dir: Path, osm_index: dict, addr_index: dict, manifest: dict) -> tuple[Path, dict]:
    """Write a complete index from in-memory parts (tests, re-derives) and swap it in."""
    data_dir = Path(data_dir)
    new = data_dir / "index.new"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    sizes = {"osm_bytes": store.save(osm_index, new / store.OSM_FILE),
             "addr_bytes": store.save(addr_index, new / store.ADDR_FILE)}
    store.write_manifest(new, {**manifest, "format": store.INDEX_FORMAT, "stats": {**manifest.get("stats", {}), **sizes}})
    return _swap(data_dir), sizes
