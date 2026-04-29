import json
import os
import time
import uuid
import structlog
import redis
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

REDIS_URL            = os.getenv("REDIS_URL", "redis://localhost:6379/0")
INCIDENT_TTL_SECONDS = int(os.getenv("INCIDENT_TTL_SECONDS", "180000"))  # 50h
KEY_PREFIX           = "incident:"
INDEX_KEY            = "index:active_incidents"

# Fix #26 — cap chunk_ids stored in Redis to avoid unbounded growth.
# Full chunk history is always available in PostgreSQL.
MAX_CHUNK_IDS_IN_REDIS = 500

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def create(
    feed_id:       str,
    county:        str,
    incident_type: str,
    priority:      str,
    address_raw:   str,
    address_full:  str,
    city:          str,
    lat:           float | None,
    lng:           float | None,
    units:         list[str],
    summary:       str,
    chunk_id:      str,
) -> dict:
    r           = get_redis()
    incident_id = str(uuid.uuid4())
    now         = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    incident = {
        "incident_id":   incident_id,
        "feed_id":       feed_id,
        "county":        county,
        "status":        "ACTIVE",
        "opened_at":     now,
        "last_updated":  now,
        "resolved_at":   None,
        "incident_type": incident_type,
        "priority":      priority,
        "address_raw":   address_raw,
        "address_full":  address_full,
        "city":          city,
        "lat":           lat,
        "lng":           lng,
        "units":         units,
        "units_cleared": [],
        "summary":       summary,
        "chunk_ids":     [chunk_id],  # starts small, capped on update
    }

    key = f"{KEY_PREFIX}{incident_id}"
    r.setex(key, INCIDENT_TTL_SECONDS, json.dumps(incident))
    r.sadd(INDEX_KEY, incident_id)
    r.expire(INDEX_KEY, INCIDENT_TTL_SECONDS)

    log.info("Incident created",
             incident_id   = incident_id,
             incident_type = incident_type,
             address_full  = address_full,
             units         = units)
    return incident


def get(incident_id: str) -> dict | None:
    r   = get_redis()
    key = f"{KEY_PREFIX}{incident_id}"
    raw = r.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def update(
    incident_id:   str,
    units_added:   list[str],
    units_cleared: list[str],
    summary:       str,
    chunk_id:      str,
    priority:      str | None = None,
) -> dict | None:
    r        = get_redis()
    incident = get(incident_id)
    if incident is None:
        log.warning("Update called on unknown incident", incident_id=incident_id)
        return None

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    existing_units = set(incident.get("units", []))
    cleared_units  = set(incident.get("units_cleared", []))

    for uid in units_added:
        existing_units.add(uid)
    for uid in units_cleared:
        existing_units.discard(uid)
        cleared_units.add(uid)

    incident["units"]         = list(existing_units)
    incident["units_cleared"] = list(cleared_units)
    incident["last_updated"]  = now
    # Only update summary if the incoming value is non-empty
    # to avoid wiping a good summary when a chunk has no summary_update
    if summary:
        incident["summary"] = summary

    # Fix #26 — cap chunk_ids to MAX_CHUNK_IDS_IN_REDIS
    # Keep the most recent entries; full history is in PostgreSQL
    chunk_ids = incident.get("chunk_ids", [])
    chunk_ids.append(chunk_id)
    if len(chunk_ids) > MAX_CHUNK_IDS_IN_REDIS:
        chunk_ids = chunk_ids[-MAX_CHUNK_IDS_IN_REDIS:]
    incident["chunk_ids"] = chunk_ids

    if priority and priority != "UNKNOWN":
        incident["priority"] = priority

    key = f"{KEY_PREFIX}{incident_id}"
    r.setex(key, INCIDENT_TTL_SECONDS, json.dumps(incident))

    log.info("Incident updated",
             incident_id   = incident_id,
             units_added   = units_added,
             units_cleared = units_cleared)
    return incident


def resolve(incident_id: str, chunk_id: str) -> dict | None:
    r        = get_redis()
    incident = get(incident_id)
    if incident is None:
        return None

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    incident["status"]       = "RESOLVED"
    incident["resolved_at"]  = now
    incident["last_updated"] = now

    # Fix #26 — cap on resolve too
    chunk_ids = incident.get("chunk_ids", [])
    chunk_ids.append(chunk_id)
    if len(chunk_ids) > MAX_CHUNK_IDS_IN_REDIS:
        chunk_ids = chunk_ids[-MAX_CHUNK_IDS_IN_REDIS:]
    incident["chunk_ids"] = chunk_ids

    key = f"{KEY_PREFIX}{incident_id}"
    r.setex(key, 1800, json.dumps(incident))  # 30 min for frontend fade-out
    r.srem(INDEX_KEY, incident_id)

    log.info("Incident resolved", incident_id=incident_id)
    return incident


def get_all_active(feed_id: str | None = None) -> list[dict]:
    r            = get_redis()
    incident_ids = r.smembers(INDEX_KEY)
    incidents    = []

    for iid in incident_ids:
        incident = get(iid)
        if incident is None:
            r.srem(INDEX_KEY, iid)
            continue
        if incident.get("status") != "ACTIVE":
            r.srem(INDEX_KEY, iid)
            continue
        if feed_id and incident.get("feed_id") != feed_id:
            continue
        incidents.append(incident)

    return sorted(incidents, key=lambda x: x.get("opened_at", ""))