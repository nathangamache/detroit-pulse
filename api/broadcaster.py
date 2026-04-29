import json
import os
import time
import uuid
import structlog
import redis
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PUBSUB_CHANNEL  = "detroit-pulse:events"
DEBUG_LIST_KEY  = "detroit-pulse:debug-history"
DEBUG_MAX_ITEMS = 1000

_redis = None


def get_redis():
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def publish(event_type: str, data: dict):
    from api.debug_logger import log_event
    r = get_redis()
    # Attach a unique event_uuid so the frontend can deduplicate
    # events that arrive both via live pub/sub AND debug history replay
    if "event_uuid" not in data:
        data = {**data, "event_uuid": str(uuid.uuid4())}
    message = json.dumps({"event": event_type, "data": data})
    r.publish(PUBSUB_CHANNEL, message)
    log.debug("Event published", event_type=event_type)
    # Write to rolling text log file for grep/search
    log_event(event_type, data)


def persist_debug(event_type: str, data: dict):
    """
    Write event to the persistent Redis debug history list.
    Capped at DEBUG_MAX_ITEMS. Survives page reloads and reconnects.
    """
    r = get_redis()
    entry = json.dumps({
        "event": event_type,
        "data":  data,
        "ts":    time.time(),
    })
    r.lpush(DEBUG_LIST_KEY, entry)
    r.ltrim(DEBUG_LIST_KEY, 0, DEBUG_MAX_ITEMS - 1)


def get_debug_history() -> list[dict]:
    """
    Return recent debug history in chronological order (oldest first).
    Called on WebSocket connect to replay history to new clients.
    """
    r = get_redis()
    raw_items = r.lrange(DEBUG_LIST_KEY, 0, -1)
    items = []
    for raw in reversed(raw_items):  # reverse so oldest first
        try:
            items.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return items


def publish_debug(stage: str, feed_id: str, **kwargs):
    data = {"stage": stage, "feed_id": feed_id, **kwargs}
    publish("pipeline:debug", data)
    persist_debug("pipeline:debug", data)


def publish_incident_new(incident: dict):
    publish("incident:new", incident)
    persist_debug("incident:new", incident)


def publish_incident_update(incident: dict):
    publish("incident:update", incident)
    persist_debug("incident:update", incident)


def publish_incident_resolve(incident: dict):
    publish("incident:resolve", incident)
    persist_debug("incident:resolve", incident)


def publish_unassociated(chunk_id: str, feed_id: str, transcript: str):
    data = {"chunk_id": chunk_id, "feed_id": feed_id, "transcript": transcript}
    publish("incident:unassociated", data)
    persist_debug("incident:unassociated", data)
