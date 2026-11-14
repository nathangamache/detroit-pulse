"""Geocoder index build: the pyosmium parser on a tiny OSM XML file and the build script's guard."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests import geocoder_fixture as F


def test_osm_parser_reads_a_tiny_extract(tmp_path):
    pytest.importorskip("osmium")
    from geocode.local import build_osm
    raw = F.raw_osm()
    xml = tmp_path / "tiny.osm"
    xml.write_text(F.osm_xml(raw))
    parsed = build_osm.parse_pbf(str(xml), F.BBOX, log=lambda s: None)
    assert len(parsed["ways"]) == len(raw["ways"])
    assert {p[0] for p in parsed["pois"]} >= {"Kellogg Park", "Michigan Stadium"}
    idx, stats = build_osm.derive({**parsed, "admin": raw["admin"]}, F.BBOX, log=lambda s: None)
    trail = [c for c in idx["intersections"]["ann arbor|main"] if "ann arbor trail" in c[4]]
    road = [c for c in idx["intersections"]["ann arbor|main"] if "ann arbor road" in c[4]]
    assert len(trail) == 1 and len(road) == 1 and "ann arbor trail" in idx["full_keys"]
    assert stats["suffix_qualified_cores"] >= 1


def test_build_script_refuses_repo_dir(tmp_path):
    repo = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, str(repo / "scripts" / "build_geocoder_index.py"), "--data-dir",
                          str(repo / "data" / "geocoder"), "--download-only"], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode != 0 and "inside the repo" in out.stderr
