"""The geocoder chain: cache -> providers in GEOCODER_PROVIDERS order -> bbox check -> cache.

Default order: local (OSM intersection + address index), census (addresses), photon.
Add `nominatim` to enable public Nominatim (1 req/s). Google is gone: listing "google"
logs an error and is ignored (it was the expensive part of the old pipeline, and its
terms don't allow keeping results beyond 30 days).

The first HIGH/MEDIUM result inside the coverage bbox wins; a LOW result is kept as a
fallback while later providers are tried.
"""
from __future__ import annotations

import threading

import structlog

from core.config_files import BBox, coverage_bbox
from core.settings import get_settings
from geocode.aliases import canonical_landmark
from geocode.base import CONF_RANK, GeoQuery, GeoResult, Provider
from geocode.cache import GeocodeCache

log = structlog.get_logger()


def build_providers(names: list[str] | None = None) -> list[Provider]:
    from geocode.providers.http_providers import CensusProvider, NominatimProvider, PhotonProvider
    from geocode.providers.local_index import LocalIndexProvider

    out: list[Provider] = []
    for n in names or get_settings().geocoder_provider_list:
        if n == "local":
            out.append(LocalIndexProvider())
        elif n == "census":
            out.append(CensusProvider())
        elif n == "nominatim":
            out.append(NominatimProvider())
        elif n == "photon":
            out.append(PhotonProvider())
        elif n == "google":
            log.error("Google geocoding was removed from Detroit Pulse (per-request cost and 30-day caching "
                      "limit). Use local/census/photon/nominatim. Ignoring 'google'.")
        else:
            log.error("unknown geocoder provider", provider=n)
    return out


class Geocoder:
    def __init__(self, providers: list[Provider] | None = None, cache: GeocodeCache | None = None,
                 bbox: BBox | None = None, use_cache: bool = True):
        self.providers = providers if providers is not None else build_providers()
        self.cache = cache if cache is not None else (GeocodeCache() if use_cache else None)
        self.bbox = bbox or coverage_bbox()

    def geocode(self, q: GeoQuery) -> GeoResult:
        if q.kind in (None, "none") or not (q.street1 or q.raw):
            return GeoResult.failed()
        if q.kind == "landmark" and q.street1:
            q.street1 = canonical_landmark(q.street1)
        if self.cache is not None:
            hit = self.cache.get(q)
            if hit is not None:
                return hit
        best: GeoResult | None = None
        for p in self.providers:
            if not p.supports(q.kind):
                continue
            r = p.geocode(q)
            if r is None or not r.found:
                continue
            if not self.bbox.contains(r.lat, r.lng):
                log.info("geocode outside coverage bbox; rejected", provider=p.name, lat=r.lat, lng=r.lng)
                continue
            if best is None or CONF_RANK[r.confidence] > CONF_RANK[best.confidence]:
                best = r
            if CONF_RANK[best.confidence] >= CONF_RANK["MEDIUM"]:
                break
        result = best or GeoResult.failed()
        if self.cache is not None:
            self.cache.put(q, result)
        return result

    def status(self) -> list[dict]:
        out = []
        for p in self.providers:
            st = getattr(p, "status", None)
            out.append(st() if callable(st) else {"provider": p.name, "available": True})
        return out


_default: Geocoder | None = None
_lock = threading.Lock()


def get_geocoder() -> Geocoder:
    global _default
    with _lock:
        if _default is None:
            _default = Geocoder()
    return _default
