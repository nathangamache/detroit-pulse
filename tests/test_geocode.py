"""Geocoder interface: chain ordering, bbox, Postgres cache, $0 providers (mocked HTTP), local adapter."""
from __future__ import annotations

import json

import httpx
import pytest

from geocode import chain as chain_mod
from geocode.base import GeoQuery, GeoResult
from geocode.chain import Geocoder, build_providers
from geocode.providers import http_providers as hp
from geocode.providers.local_index import LocalIndexProvider


class Fixed:
    def __init__(self, name, result, kinds=("intersection", "address", "block", "landmark", "highway")):
        self.name, self.result, self.kinds, self.calls = name, result, kinds, 0

    def supports(self, kind):
        return kind in self.kinds

    def geocode(self, q):
        self.calls += 1
        return self.result


Q = GeoQuery(kind="intersection", street1="Packard Street", street2="Stadium Boulevard", city="Ann Arbor",
             bias=(42.28, -83.74))


def test_chain_prefers_first_medium_and_keeps_low_as_fallback():
    low = Fixed("a", GeoResult(42.27, -83.73, "Packard St", "LOW", "a"))
    med = Fixed("b", GeoResult(42.265, -83.731, "Packard & Stadium", "MEDIUM", "b"))
    never = Fixed("c", GeoResult(42.0, -83.0, "x", "HIGH", "c"))
    g = Geocoder(providers=[low, med, never], use_cache=False)
    r = g.geocode(GeoQuery(**{**Q.__dict__}))
    assert r.source == "b" and r.confidence == "MEDIUM" and never.calls == 0
    g2 = Geocoder(providers=[low], use_cache=False)
    assert g2.geocode(GeoQuery(**{**Q.__dict__})).confidence == "LOW"


def test_chain_rejects_results_outside_coverage_bbox():
    far = Fixed("far", GeoResult(40.7, -74.0, "New York", "HIGH", "far"))      # outside Wayne/Oakland/Washtenaw
    r = Geocoder(providers=[far], use_cache=False).geocode(GeoQuery(**{**Q.__dict__}))
    assert not r.found and r.confidence == "FAILED"


def test_none_kind_never_queries():
    p = Fixed("p", GeoResult(42.3, -83.0, "x", "HIGH", "p"))
    assert not Geocoder(providers=[p], use_cache=False).geocode(GeoQuery(kind="none")).found
    assert p.calls == 0


def test_postgres_cache_with_source_column(db):
    from sqlalchemy import text
    p = Fixed("photon", GeoResult(42.265, -83.731, "Packard & Stadium", "MEDIUM", "photon"))
    g = Geocoder(providers=[p])
    r1 = g.geocode(GeoQuery(**{**Q.__dict__}))
    r2 = g.geocode(GeoQuery(**{**Q.__dict__}))
    assert r1.found and r2.found and p.calls == 1 and r2.extra.get("cached")
    miss = Fixed("census", None)
    gm = Geocoder(providers=[miss])
    q2 = GeoQuery(kind="address", number="99999", street1="Nowhere Road", bias=(42.3, -83.0))
    assert not gm.geocode(q2).found and not gm.geocode(GeoQuery(**{**q2.__dict__})).found
    assert miss.calls == 1                                  # misses are cached too (for 24 h)
    with db.connect() as c:
        rows = c.execute(text("SELECT source, found, query->>'kind' FROM geocode_cache ORDER BY created_at")).all()
    assert [(r[0], r[1], r[2]) for r in rows] == [("photon", True, "intersection"), ("none", False, "address")]


def test_google_is_ignored():
    assert [p.name for p in build_providers(["google", "census"])] == ["census"]


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "detroit-pulse-geocoder/test"})


def test_census_provider(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["ua"] = req.headers["user-agent"]
        return httpx.Response(200, json={"result": {"addressMatches": [
            {"matchedAddress": "16850 GRAND RIVER AVE, DETROIT, MI, 48227", "coordinates": {"x": -83.21, "y": 42.40}}]}})

    p = hp.CensusProvider(client=_client(handler), base_url="https://geocoding.geo.census.gov")
    monkeypatch.setattr(p.throttle, "wait", lambda: None)
    r = p.geocode(GeoQuery(kind="block", number="16800", street1="Grand River Avenue", city="Detroit"))
    assert r.found and r.lat == 42.40 and r.source == "census" and r.confidence == "MEDIUM"
    assert "16850+Grand+River" in seen["url"] or "16850%20Grand%20River" in seen["url"]   # middle of the block
    assert "@" not in seen["ua"]                                                          # generic UA, no contact
    assert not p.supports("intersection")


def test_nominatim_throttles_to_one_per_second(monkeypatch):
    sleeps = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(hp.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(hp.time, "sleep", lambda s: sleeps.append(s))

    def handler(req):
        return httpx.Response(200, json=[{"lat": "42.2808", "lon": "-83.743", "display_name": "Ann Arbor",
                                          "addresstype": "amenity"}])

    p = hp.NominatimProvider(client=_client(handler), base_url="https://nominatim.openstreetmap.org")
    q = GeoQuery(kind="landmark", street1="Michigan Stadium", city="Ann Arbor")
    for _ in range(3):
        assert p.geocode(q).found
    assert sleeps == [pytest.approx(1.0), pytest.approx(2.0)]   # 3 calls in the same instant -> spaced 1 s apart


def test_photon_intersection_is_low_confidence(monkeypatch):
    def handler(req):
        assert "lat=" in str(req.url) and "bbox=" in str(req.url)
        return httpx.Response(200, json={"features": [{"geometry": {"coordinates": [-83.731, 42.265]},
                                                       "properties": {"name": "Packard Street", "type": "street",
                                                                      "city": "Ann Arbor"}}]})
    p = hp.PhotonProvider(client=_client(handler), base_url="https://photon.example")
    monkeypatch.setattr(p.throttle, "wait", lambda: None)
    r = p.geocode(GeoQuery(**{**Q.__dict__}))
    assert r.found and r.confidence == "LOW" and r.source == "photon"


def test_http_failure_is_none(monkeypatch):
    p = hp.PhotonProvider(client=_client(lambda req: httpx.Response(503)), base_url="https://photon.example")
    monkeypatch.setattr(p.throttle, "wait", lambda: None)
    assert p.geocode(GeoQuery(**{**Q.__dict__})) is None


def test_local_index_adapter(tmp_path):
    unavailable = LocalIndexProvider(index_dir=None, backend="")
    assert unavailable.geocode(GeoQuery(**{**Q.__dict__})) is None and not unavailable.available
    (tmp_path / "index").mkdir()
    p = LocalIndexProvider(index_dir=tmp_path / "index", backend="tests.fake_geocoder_backend:FakeIndex")
    r = p.geocode(GeoQuery(**{**Q.__dict__}))
    assert r.found and r.confidence == "HIGH" and r.source == "local:osm_intersection"
    from tests.fake_geocoder_backend import FakeIndex
    assert FakeIndex.calls[-1]["kind"] == "intersection" and FakeIndex.calls[-1]["street2"] == "Stadium Boulevard"
    lm = p.geocode(GeoQuery(kind="landmark", street1="Michigan Stadium", street2="Main Street"))
    assert lm is None and FakeIndex.calls[-1] == {"kind": "landmark", "street1": "Main Street", "street2": None,
                                                  "number": None, "city": None, "name": "Michigan Stadium"}


def test_query_key_is_stable_and_case_insensitive():
    a = GeoQuery(kind="address", number="100", street1="Main Street", city="Ann Arbor", bias=(42.2801, -83.7432))
    b = GeoQuery(kind="address", number="100", street1="main street ", city="ann arbor", bias=(42.2799, -83.7429),
                 raw="one hundred main")
    assert a.key() == b.key()
    assert json.loads(json.dumps(a.to_json()))["bias"] == [42.2801, -83.7432]


def test_landmark_alias(monkeypatch):
    p = Fixed("x", GeoResult(42.2658, -83.7486, "Michigan Stadium", "MEDIUM", "x"), kinds=("landmark",))
    q = GeoQuery(kind="landmark", street1="the big house")
    Geocoder(providers=[p], use_cache=False).geocode(q)
    assert q.street1 == "Michigan Stadium"
    assert chain_mod.get_geocoder() is chain_mod.get_geocoder()
