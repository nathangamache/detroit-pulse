"""Postgres geocode cache (table geocode_cache).

One row per normalised structured query, with the provider that answered (`source`,
or "none" for a cached miss). Hits live GEOCODE_CACHE_TTL_DAYS (30), misses
GEOCODE_FAIL_CACHE_TTL_HOURS (24) so a transient outage doesn't pin a failure. Only
$0 providers are cached here (Census: public domain, Nominatim/Photon: ODbL); Google
results, which may not be kept beyond 30 days, are never produced.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from core.db import get_engine
from core.settings import get_settings
from geocode.base import GeoQuery, GeoResult


class GeocodeCache:
    def get(self, q: GeoQuery) -> GeoResult | None:
        with get_engine().begin() as c:
            row = c.execute(text("""
                UPDATE geocode_cache SET hits = hits + 1
                 WHERE key = :k AND expires_at > now()
             RETURNING found, lat, lng, formatted, confidence, source"""), {"k": q.key()}).first()
        if row is None:
            return None
        if not row[0]:
            return GeoResult(None, None, None, "FAILED", row[5], extra={"cached": True})
        return GeoResult(row[1], row[2], row[3], row[4], row[5], extra={"cached": True})

    def put(self, q: GeoQuery, r: GeoResult) -> None:
        s = get_settings()
        ttl = timedelta(days=s.geocode_cache_ttl_days) if r.found else timedelta(hours=s.geocode_fail_cache_ttl_hours)
        with get_engine().begin() as c:
            c.execute(text("""
                INSERT INTO geocode_cache (key, query, source, found, lat, lng, formatted, confidence, created_at, expires_at)
                VALUES (:k, CAST(:q AS jsonb), :src, :f, :lat, :lng, :fmt, :conf, now(), :exp)
                ON CONFLICT (key) DO UPDATE SET query = EXCLUDED.query, source = EXCLUDED.source,
                    found = EXCLUDED.found, lat = EXCLUDED.lat, lng = EXCLUDED.lng,
                    formatted = EXCLUDED.formatted, confidence = EXCLUDED.confidence,
                    created_at = now(), expires_at = EXCLUDED.expires_at"""), {
                "k": q.key(), "q": json.dumps(q.to_json()), "src": r.source if r.found else "none",
                "f": r.found, "lat": r.lat, "lng": r.lng, "fmt": r.formatted, "conf": r.confidence,
                "exp": datetime.now(timezone.utc) + ttl})

    def purge_expired(self) -> int:
        with get_engine().begin() as c:
            return c.execute(text("DELETE FROM geocode_cache WHERE expires_at < now()")).rowcount or 0
