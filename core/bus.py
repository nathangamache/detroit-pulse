"""Redis: pub/sub fan-out to the API's WebSocket clients plus small wake-up signals.

Postgres is the durable queue (transcript_chunks.status). Redis only carries
best-effort notifications, so a Redis outage delays the UI but never loses data.
Key names are prefixed with `pulse:` so the dedicated Redis can be inspected safely.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import redis
import structlog

from core.settings import get_settings

log = structlog.get_logger()

EVENTS_CHANNEL = "pulse:events"
DEBUG_CHANNEL = "pulse:debug"
DEBUG_HISTORY_KEY = "pulse:debug-history"
WAKE_PREFIX = "pulse:wake:"          # list used as a doorbell between stages

_lock = threading.Lock()
_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    with _lock:
        if _client is None:
            _client = redis.from_url(get_settings().redis_dsn, decode_responses=True,
                                     socket_timeout=10, socket_connect_timeout=5)
    return _client


def reset_redis() -> None:
    global _client
    with _lock:
        _client = None


def publish_event(event: str, data: Any) -> None:
    """Publish an incident/rail event to WebSocket clients. Never raises."""
    try:
        get_redis().publish(EVENTS_CHANNEL, json.dumps({"event": event, "data": data}, default=str))
    except Exception as e:  # noqa: BLE001 - the UI is best-effort
        # not `event=`: structlog's first argument is already called `event` (TypeError)
        log.warning("publish_event failed", pulse_event=event, error=str(e))


def publish_debug(stage: str, channel_id: str, **data: Any) -> None:
    payload = {"event": "pipeline:debug", "data": {"stage": stage, "channel_id": channel_id, **data},
               "ts": time.time()}
    try:
        r = get_redis()
        raw = json.dumps(payload, default=str)
        r.publish(EVENTS_CHANNEL, raw)
        r.lpush(DEBUG_HISTORY_KEY, raw)
        r.ltrim(DEBUG_HISTORY_KEY, 0, 499)
    except Exception:  # noqa: BLE001
        pass


def ring(stage: str) -> None:
    """Tell a downstream stage that work is waiting (it also polls Postgres)."""
    try:
        r = get_redis()
        r.lpush(WAKE_PREFIX + stage, "1")
        r.ltrim(WAKE_PREFIX + stage, 0, 0)
    except Exception:  # noqa: BLE001
        pass


def wait_for_ring(stage: str, timeout_s: float) -> None:
    """Block until rung or timeout (falls back to sleeping if Redis is down)."""
    try:
        get_redis().brpop([WAKE_PREFIX + stage], timeout=max(1, int(timeout_s)))
    except Exception:  # noqa: BLE001
        time.sleep(min(timeout_s, 5))
