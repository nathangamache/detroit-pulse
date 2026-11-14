"""Redis is best-effort: publishing must never raise, so a Redis outage can't stop ingest or the resolver."""
from __future__ import annotations

from core import bus


def test_publish_never_raises_when_redis_is_down(monkeypatch):
    def down():
        raise ConnectionError("redis unreachable")
    monkeypatch.setattr(bus, "get_redis", down)
    bus.publish_event("incident:resolve", {"incident_id": "x"})
    bus.publish_debug("extract", "ch", note="x")
    bus.ring("extract")
