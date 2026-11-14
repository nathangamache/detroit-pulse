"""Local, $0 geocoder: OSM intersections + address points/TIGER ranges + OSM landmarks.

Build the index once (outside the repo) and point the `local` provider at it:

    nice -n 19 uv run python scripts/build_geocoder_index.py      # -> ~/.local/share/detroit-pulse/geocoder/index
    GEOCODER_LOCAL_INDEX=~/.local/share/detroit-pulse/geocoder/index   # the default

Modules: normalize (street-name keys), build_osm / build_addr (index builders), sources (downloads
and licences), store (safe index I/O), index (the LocalGeocoder lookups). See docs/geocoder.md.
"""
from __future__ import annotations

from geocode.local.index import IndexMissing, LocalGeocoder

__all__ = ["IndexMissing", "LocalGeocoder"]
