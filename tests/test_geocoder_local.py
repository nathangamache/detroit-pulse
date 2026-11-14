"""Local geocoder (geocode/local): normalization, index build on a tiny synthetic fixture, lookups,
the safe index loader and the `local` provider wiring. No network, no real index."""
from __future__ import annotations

import gzip
import pickle

import pytest

from geocode.base import GeoQuery
from geocode.chain import Geocoder
from geocode.local import store
from geocode.local.index import IndexMissing, LocalGeocoder, hav
from geocode.local.normalize import city_match, core_key, expand_aliases, full_key, leading_dir
from geocode.providers.local_index import LocalIndexProvider
from tests import geocoder_fixture as F


@pytest.mark.parametrize("name,key", [
    ("W 7 Mile Rd", "7 mile"), ("Seven Mile", "7 mile"), ("Eight Mile Road", "8 mile"),
    ("East Jefferson Avenue", "jefferson"), ("I-94", "i 94"), ("i94", "i 94"), ("the Lodge", "m 10"),
    ("Edsel Ford Freeway", "i 94"), ("Southfield Freeway", "m 39"), ("Southfield Rd", "southfield"),
    ("Ford Freeway", "i 94"), ("Ford Road", "ford"), ("Fisher", "i 75"), ("Fisher Rd", "fisher"),
    ("E I-94 Service Drive", "i 94"), ("Grashit", "gratiot"), ("Second Avenue", "2nd"),
    ("St Antoine", "saint antoine"), ("Fourteen Mile", "14 mile"), ("Ann Arbor Trail", "ann arbor"),
    ("Ann Arbor Rd W", "ann arbor"), ("S State St", "state"), ("Huron Parkway", "huron"), ("US-23", "us 23"),
    ("M-14", "m 14"), ("Northwestern Hwy", "northwestern"), ("Washtenah", "washtenaw"),
])
def test_core_key(name, key):
    assert core_key(name) == key


def test_full_key_and_aliases():
    assert full_key("West Ann Arbor Trail") == "ann arbor trail"
    assert full_key("Ann Arbor Rd W") == "ann arbor road"
    assert full_key("Ann Arbor") is None and full_key("I-275") is None
    assert expand_aliases("6 mile") == ["6 mile", "mcnichols"]
    assert "maple" in expand_aliases("15 mile") and "big beaver" in expand_aliases("16 mile")
    assert leading_dir("E Warren Ave") == "e" and leading_dir("Warren") is None
    assert city_match("Plymouth Township", "Plymouth Charter Township") and not city_match("Canton", "Plymouth")
    assert not city_match(None, "Plymouth")


@pytest.fixture(scope="module")
def index_dir(tmp_path_factory):
    return F.build_fixture_index(tmp_path_factory.mktemp("geo"))


@pytest.fixture(scope="module")
def g(index_dir):
    return LocalGeocoder(index_dir)


def near(r, pt, tol_m):
    assert r is not None, "no result"
    d = hav((r["lat"], r["lng"]), pt)
    assert d <= tol_m, f"{d:.0f} m from expected ({r})"


def test_intersections_and_city_tag(g):
    r = g.geocode({"kind": "intersection", "street1": "State St", "street2": "Liberty St", "city": "Ann Arbor"},
                  bias=F.ANN_ARBOR)
    near(r, F.STATE_X_LIBERTY, 15)
    assert r["confidence"] == "HIGH" and r["source"] == "osm_shared_node" and r["city"] == "Ann Arbor"
    # mile road + spoken number, ref alias (M-1 = Woodward), reversed order
    for s1, s2 in (("Woodward Ave", "9 Mile Rd"), ("Nine Mile", "M-1"), ("9 Mile", "Woodward")):
        near(g.geocode({"kind": "intersection", "street1": s1, "street2": s2}, bias=F.FERNDALE),
             F.WOODWARD_X_9MILE, 15)


def test_suffix_distinguishes_ann_arbor_trail_from_ann_arbor_road(g):
    trail = g.geocode({"kind": "intersection", "street1": "Main St", "street2": "Ann Arbor Trail",
                       "city": "Plymouth"}, bias=F.PLYMOUTH)
    road = g.geocode({"kind": "intersection", "street1": "Main Street", "street2": "Ann Arbor Road",
                      "city": "Plymouth"}, bias=F.PLYMOUTH)
    near(trail, F.MAIN_X_AA_TRAIL, 15)
    near(road, F.MAIN_X_AA_ROAD, 15)
    # no suffix spoken: either is plausible, the one nearer the channel's area wins
    either = g.geocode({"kind": "intersection", "street1": "Main", "street2": "Ann Arbor"}, bias=F.MAIN_X_AA_ROAD)
    near(either, F.MAIN_X_AA_ROAD, 15)


def test_same_name_resolved_by_bias_and_city(g):
    aa = g.geocode({"kind": "intersection", "street1": "Main", "street2": "Liberty"}, bias=F.ANN_ARBOR)
    near(aa, F.AA_MAIN_X_LIBERTY, 15)
    # "Main" exists in both towns; with no Liberty in Plymouth, the Ann Arbor crossing still wins
    near(g.geocode({"kind": "intersection", "street1": "Main", "street2": "Liberty", "city": "Ann Arbor"},
                   bias=F.PLYMOUTH), F.AA_MAIN_X_LIBERTY, 15)


def test_fuzzy_whisper_spelling(g):
    r = g.geocode({"kind": "intersection", "street1": "Libberty", "street2": "State"}, bias=F.ANN_ARBOR)
    near(r, F.STATE_X_LIBERTY, 15)
    assert r["confidence"] == "MEDIUM"          # fuzzy name match is never HIGH


def test_freeway_crossing_uses_closest_approach(g):
    r = g.geocode({"kind": "highway_segment", "street1": "M-14", "street2": "Main Street"}, bias=F.PLYMOUTH)
    near(r, F.M14_OVER_MAIN, 60)
    assert r["source"] == "osm_freeway_approach"


def test_unknown_cross_street_falls_back_to_low_street_point(g):
    r = g.geocode({"kind": "intersection", "street1": "State Street", "street2": "Nonexistent Boulevard"},
                  bias=F.ANN_ARBOR)
    assert r["confidence"] == "LOW" and r["source"] == "osm_street_nearest_bias"


def test_address_points_exact_and_block(g):
    r = g.geocode({"kind": "address", "number": 22850, "street1": "Woodward Avenue", "city": "Ferndale"},
                  bias=F.FERNDALE)
    near(r, (F.WOODWARD_X_9MILE[0] + 0.0024, F.WOODWARD_X_9MILE[1] - 0.0012), 5)
    assert r["confidence"] == "HIGH" and r["source"] == "oakland_points"
    b = g.geocode({"kind": "block", "number": 22800, "street1": "Woodward"}, bias=F.FERNDALE)
    assert b["source"] == "oakland_points_block_mean" and b["confidence"] == "HIGH"
    # mean of the block's points, snapped onto the Woodward centreline (the fixture points sit beside it)
    near(b, (F.WOODWARD_X_9MILE[0] + 0.0022, F.WOODWARD_X_9MILE[1] - 0.0011), 20)
    on_street = min(hav((b["lat"], b["lng"]), tuple(p)) for p in g._kdtree("woodward")[0])
    assert on_street < 1
    # between two points (22855 isn't one): interpolated on the same side
    i = g.geocode({"kind": "address", "number": 22855, "street1": "Woodward"}, bias=F.FERNDALE)
    assert i["source"] == "points_interp" and i["confidence"] == "MEDIUM"


def test_tiger_interpolation_and_osm_addr(g):
    r = g.geocode({"kind": "block", "number": 500, "street1": "South Main", "city": "Plymouth"}, bias=F.PLYMOUTH)
    assert r["source"] == "tiger_addrfeat_interp" and r["city"] == "Plymouth"
    expected = (F.MAIN_X_AA_TRAIL[0] + (F.MAIN_X_AA_ROAD[0] - F.MAIN_X_AA_TRAIL[0]) * (550 - 100) / 898,
                F.MAIN_X_AA_TRAIL[1])
    near(r, expected, 40)
    o = g.geocode({"kind": "address", "number": 150, "street1": "W Ann Arbor Trail"}, bias=F.PLYMOUTH)
    assert o["source"] == "osm_points" and o["confidence"] == "HIGH"
    # a number nobody has -> LOW street point, never a made-up house
    x = g.geocode({"kind": "address", "number": 99999, "street1": "Liberty Street"}, bias=F.ANN_ARBOR)
    assert x["confidence"] == "LOW"


def test_landmarks(g):
    r = g.geocode({"kind": "landmark", "name": "the big house"}, bias=F.ANN_ARBOR)
    near(r, (42.2658, -83.7487), 10)
    k = g.geocode({"kind": "landmark", "name": "Kellogg Park", "city": "Plymouth"}, bias=F.PLYMOUTH)
    assert k["source"] == "poi:leisure=park"
    # bus stops never answer a landmark query
    assert g.geocode({"kind": "landmark", "name": "Ann Arbor Bus Stop 12"}, bias=F.ANN_ARBOR) is None
    # an unmatched landmark with spoken cross streets returns the crossing (MEDIUM)
    a = g.geocode({"kind": "landmark", "name": "Some Store", "street1": "State", "street2": "Liberty"},
                  bias=F.ANN_ARBOR)
    near(a, F.STATE_X_LIBERTY, 15)
    assert a["confidence"] == "MEDIUM"


def test_none_kind_and_far_bias(g):
    assert g.geocode({"kind": "none"}) is None
    # more than 60 km from the channel and no city named: rejected rather than guessed
    assert g.geocode({"kind": "intersection", "street1": "State", "street2": "Liberty"},
                     bias=(42.95, -82.45)) is None


def test_index_files_and_manifest(index_dir):
    m = store.read_manifest(index_dir)
    assert m["format"] == store.INDEX_FORMAT and m["stats"]["osm_bytes"] > 0
    assert (index_dir / store.OSM_FILE).exists() and not (index_dir.parent / "index.new").exists()


def test_safe_loader_refuses_code(tmp_path):
    class Evil:
        def __reduce__(self):
            import os
            return (os.system, ("echo pwned",))
    p = tmp_path / "evil.pkl.gz"
    with gzip.open(p, "wb") as f:
        pickle.dump({"x": Evil()}, f)
    with pytest.raises(pickle.UnpicklingError):
        store.load(p)
    import numpy as np
    store.save({"a": np.arange(3, dtype=np.int32), "b": {1, 2}, "c": (1.5, "x")}, tmp_path / "ok.pkl.gz")
    back = store.load(tmp_path / "ok.pkl.gz")
    assert back["a"].tolist() == [0, 1, 2] and back["b"] == {1, 2}


def test_missing_index_raises(tmp_path):
    with pytest.raises(IndexMissing):
        LocalGeocoder(tmp_path)


def test_provider_and_chain_use_the_index(index_dir):
    p = LocalIndexProvider(index_dir=index_dir, backend="geocode.local:LocalGeocoder")
    st = p.status()
    assert st["available"] and st["index"]["intersection_pairs"] > 0
    chain = Geocoder(providers=[p], use_cache=False)
    r = chain.geocode(GeoQuery(kind="intersection", street1="State Street", street2="Liberty Street",
                               city="Ann Arbor", bias=F.ANN_ARBOR))
    assert r.found and r.confidence == "HIGH" and r.source == "local:osm_shared_node"
    hw = chain.geocode(GeoQuery(kind="highway", street1="M-14", street2="Main", bias=F.PLYMOUTH))
    assert hw.found and hw.source.startswith("local:osm_freeway")
    lm = chain.geocode(GeoQuery(kind="landmark", street1="Michigan Stadium", bias=F.ANN_ARBOR))
    assert lm.found and lm.source.startswith("local:poi:")
    blk = chain.geocode(GeoQuery(kind="block", number="22800", street1="Woodward Avenue", bias=F.FERNDALE))
    assert blk.found and blk.confidence == "HIGH"


def test_default_provider_points_at_data_dir(monkeypatch, tmp_path):
    from core.settings import reset_settings_cache
    monkeypatch.delenv("GEOCODER_LOCAL_INDEX", raising=False)
    monkeypatch.setenv("GEOCODER_DATA_DIR", str(tmp_path / "gd"))
    reset_settings_cache()
    try:
        p = LocalIndexProvider()
        assert p.index_dir == tmp_path / "gd" / "index" and p.backend_spec == "geocode.local:LocalGeocoder"
        assert not p.available and "build_geocoder_index" in p.status()["reason"]
    finally:
        reset_settings_cache()
