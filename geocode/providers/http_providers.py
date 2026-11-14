"""Free HTTP geocoders: US Census (addresses), public/self-hosted Nominatim, Photon.

All send a generic User-Agent (GEOCODER_USER_AGENT; no personal contact details) and
are throttled; results are cached in Postgres by the chain, so each distinct query is
sent at most once per cache TTL.

* Census Geocoder (geocoding.geo.census.gov): free, public domain, street addresses
  only (number + street). Self-imposed 1 request/second.
* Nominatim: public instance is limited to 1 request/second by the OSMF usage policy,
  needs a valid User-Agent, and its results (ODbL) may be cached. Disabled unless
  "nominatim" is listed in GEOCODER_PROVIDERS. Point NOMINATIM_URL at a self-hosted
  instance to lift the limit.
* Photon (photon.komoot.io or self-hosted PHOTON_URL): OSM-based, good for named
  places and streets; intersections come back as a street (LOW confidence).
"""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import structlog

from core.config_files import coverage_bbox, default_bias
from core.settings import get_settings
from geocode.base import GeoQuery, GeoResult

log = structlog.get_logger()


class _Throttle:
    """At most one request per `interval` seconds across threads (sleeps, never drops)."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


class _HttpProvider:
    name = "http"
    interval_s = 1.0

    def __init__(self, client: httpx.Client | None = None, base_url: str | None = None):
        s = get_settings()
        self.client = client or httpx.Client(timeout=s.geocoder_timeout_s,
                                              headers={"User-Agent": s.geocoder_user_agent})
        self.base_url = (base_url or self.default_url(s)).rstrip("/")
        self.throttle = _Throttle(self.interval_s)

    def default_url(self, s) -> str:  # pragma: no cover - overridden
        return ""

    def _get(self, path: str, params: dict[str, Any]) -> Any | None:
        self.throttle.wait()
        try:
            r = self.client.get(self.base_url + path, params=params)
            if r.status_code != 200:
                log.info("geocoder http error", provider=self.name, status=r.status_code)
                return None
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.info("geocoder request failed", provider=self.name, error=str(e)[:200])
            return None


class CensusProvider(_HttpProvider):
    name = "census"
    interval_s = 1.0

    def default_url(self, s) -> str:
        return s.census_url

    def supports(self, kind: str) -> bool:
        return kind in ("address", "block")

    def geocode(self, q: GeoQuery) -> GeoResult | None:
        if not (q.number and q.street1):
            return None
        number = q.number
        if q.kind == "block" and number.isdigit():
            number = str(int(number) + 50)        # middle of the hundred block
        line = f"{number} {q.street1}, {q.city + ', ' if q.city else ''}MI"
        data = self._get("/geocoder/locations/onelineaddress",
                         {"address": line, "benchmark": "Public_AR_Current", "format": "json"})
        try:
            m = data["result"]["addressMatches"]
        except (TypeError, KeyError):
            return None
        if not m:
            return None
        c = m[0]["coordinates"]
        return GeoResult(float(c["y"]), float(c["x"]), m[0].get("matchedAddress"),
                         "MEDIUM", self.name)


class NominatimProvider(_HttpProvider):
    name = "nominatim"

    def __init__(self, client: httpx.Client | None = None, base_url: str | None = None):
        s = get_settings()
        self.interval_s = max(1.0, s.nominatim_min_interval_s) if "openstreetmap.org" in (base_url or s.nominatim_url) \
            else s.nominatim_min_interval_s
        super().__init__(client, base_url)

    def default_url(self, s) -> str:
        return s.nominatim_url

    def supports(self, kind: str) -> bool:
        return kind in ("address", "block", "landmark")

    def geocode(self, q: GeoQuery) -> GeoResult | None:
        bb = coverage_bbox()
        data = self._get("/search", {"q": q.oneline(), "format": "jsonv2", "limit": 1, "countrycodes": "us",
                                     "bounded": 1, "viewbox": f"{bb.west},{bb.north},{bb.east},{bb.south}"})
        if not data:
            return None
        hit = data[0]
        conf = "HIGH" if hit.get("addresstype") in ("house", "building") or hit.get("type") == "house" else "MEDIUM"
        return GeoResult(float(hit["lat"]), float(hit["lon"]), hit.get("display_name"), conf, self.name)


class PhotonProvider(_HttpProvider):
    name = "photon"
    interval_s = 1.0

    def default_url(self, s) -> str:
        return s.photon_url

    def supports(self, kind: str) -> bool:
        return kind in ("address", "block", "landmark", "intersection", "highway")

    def geocode(self, q: GeoQuery) -> GeoResult | None:
        bb = coverage_bbox()
        lat, lon = q.bias or default_bias()
        data = self._get("/api/", {"q": q.oneline(), "limit": 1, "lat": lat, "lon": lon,
                                   "bbox": f"{bb.west},{bb.south},{bb.east},{bb.north}"})
        try:
            feat = data["features"][0]
        except (TypeError, KeyError, IndexError):
            return None
        lon_, lat_ = feat["geometry"]["coordinates"][:2]
        p = feat.get("properties", {})
        osm_type = p.get("type") or p.get("osm_value")
        if q.kind in ("intersection", "highway"):
            conf = "LOW"                          # Photon returns one of the streets, not the crossing
        elif osm_type in ("house",) or p.get("housenumber"):
            conf = "HIGH"
        elif osm_type in ("street",):
            conf = "LOW"
        else:
            conf = "MEDIUM"
        name = ", ".join(x for x in [p.get("name") or " ".join(filter(None, [p.get("housenumber"), p.get("street")])),
                                     p.get("city")] if x)
        return GeoResult(float(lat_), float(lon_), name or None, conf, self.name)
