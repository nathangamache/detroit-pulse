#!/usr/bin/env python3
"""Download the free data sources and build the local geocoder index (outside the repo).

    nice -n 19 uv run python scripts/build_geocoder_index.py                  # everything, default dirs
    nice -n 19 uv run python scripts/build_geocoder_index.py --eval-data      # + TIGER roads/landmarks for the eval
    uv run python scripts/build_geocoder_index.py --download-only
    uv run python scripts/build_geocoder_index.py --bbox 42.01,-84.16,42.92,-82.68 --counties 26163,26125

Defaults
  --data-dir   $GEOCODER_DATA_DIR, else $XDG_DATA_HOME/detroit-pulse/geocoder
               (~/.local/share/detroit-pulse/geocoder). The index goes to <data-dir>/index, which is
               the default GEOCODER_LOCAL_INDEX. Refuses a directory inside the repo.
  --bbox       coverage_bbox from config/areas(.local).yaml (Wayne + Oakland + Washtenaw + Macomb)
  --counties   TIGER ADDRFEAT counties: 26163 Wayne, 26125 Oakland, 26161 Washtenaw, 26099 Macomb
  --points     address-point layers whose licence allows this use: detroit, oakland.
               annarbor is off by default (its terms need a notice to the City first; docs/geocoder.md)

Downloads younger than --max-age-days are reused. A rebuild writes index.new/ and swaps it in;
restart the extract worker to load it. On this shared box: run under nice, and keep the OSM
decoder to a few threads (--threads, default 4). Needs the geocoder-build dependency group
(osmium, pyshp), which `uv sync` installs by default.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _bbox(s: str | None) -> dict:
    if s:
        south, west, north, east = (float(x) for x in s.split(","))
    else:
        from core.config_files import coverage_bbox
        bb = coverage_bbox()
        south, west, north, east = bb.south, bb.west, bb.north, bb.east
    if not (south < north and west < east):
        sys.exit(f"bad bbox {s!r}: want south,west,north,east")
    return {"south": south, "west": west, "north": north, "east": east}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--bbox", default=None, help="south,west,north,east (default: coverage_bbox from config)")
    ap.add_argument("--counties", default=None, help="comma list of county FIPS (default: 4 counties)")
    ap.add_argument("--points", default=None, help="comma list of point layers (default: detroit,oakland)")
    ap.add_argument("--max-age-days", type=float, default=25.0, help="re-download sources older than this")
    ap.add_argument("--refresh", action="store_true", help="re-download everything")
    ap.add_argument("--osm-pbf", type=Path, default=None, help="use this Michigan .osm.pbf instead of downloading")
    ap.add_argument("--download-only", action="store_true")
    ap.add_argument("--eval-data", action="store_true", help="also fetch TIGER roads + area landmarks (eval truth)")
    ap.add_argument("--threads", type=int, default=4, help="libosmium decoder threads")
    a = ap.parse_args()

    os.environ["OSMIUM_POOL_THREADS"] = str(max(1, a.threads))   # read when osmium starts its pool
    from geocode.local import sources
    from geocode.local.build import build_index, default_data_dir

    data_dir = (a.data_dir or default_data_dir()).expanduser().resolve()
    if data_dir == REPO or REPO in data_dir.parents:
        sys.exit(f"refusing to write geocoder data inside the repo ({data_dir}); the index must stay out of git")
    counties = {f.strip(): sources.DEFAULT_COUNTIES.get(f.strip(), f.strip())
                for f in (a.counties.split(",") if a.counties else sources.DEFAULT_COUNTIES)}
    pts = ([p.strip() for p in a.points.split(",") if p.strip()] if a.points is not None
           else [k for k, s in sources.POINT_SOURCES.items() if s.default_enabled])
    unknown = [p for p in pts if p not in sources.POINT_SOURCES]
    if unknown:
        sys.exit(f"unknown point layer(s) {unknown}; known: {sorted(sources.POINT_SOURCES)}")
    if "annarbor" in pts:
        print("NOTE: annarbor points enabled. Its terms require notifying the City of Ann Arbor IT Director "
              "and carrying its modified-data disclaimer (docs/geocoder.md).", file=sys.stderr)
    bbox = _bbox(a.bbox)
    print(f"data dir {data_dir}\nbbox {bbox}\ncounties {counties}\npoint layers {pts}", flush=True)
    m = build_index(data_dir, bbox, counties, pts, max_age_days=0.0 if a.refresh else a.max_age_days,
                    osm_pbf=a.osm_pbf, download_only=a.download_only, eval_data=a.eval_data,
                    log=lambda s: print(s, flush=True))
    print(json.dumps({k: m[k] for k in ("timings", "peak_rss_mb", "peak_rss_by_phase") if k in m} or m, indent=2))


if __name__ == "__main__":
    main()
