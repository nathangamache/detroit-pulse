import os
import time
import json
import threading
import structlog
from datetime import datetime, timezone, timedelta

log = structlog.get_logger()

STALE_THRESHOLD_HOURS  = int(os.getenv("INCIDENT_STALE_HOURS", "48"))
CHECK_INTERVAL_SECONDS = int(os.getenv("AUTO_RESOLVER_INTERVAL_S", "120"))


# Module-level singletons — created once
_ar_engine = None
_ar_redis  = None

def _get_ar_resources():
    global _ar_engine, _ar_redis
    if _ar_engine is None:
        from sqlalchemy import create_engine
        import redis as _redis
        _ar_engine = create_engine(
            os.getenv("DATABASE_URL"),
            pool_size=2,
            max_overflow=2,
            pool_pre_ping=True,
        )
        _ar_redis = _redis.from_url(os.getenv("REDIS_URL"), decode_responses=True)
    return _ar_engine, _ar_redis


def _check_and_resolve_stale():
    from sqlalchemy import text

    engine, r = _get_ar_resources()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=STALE_THRESHOLD_HOURS)

    with engine.connect() as conn:
        stale = conn.execute(text("""
            SELECT incident_id, last_updated, incident_type, address_full
            FROM incidents
            WHERE status = 'ACTIVE'
              AND last_updated < :cutoff
        """), {"cutoff": cutoff}).fetchall()

    if not stale:
        return

    log.info("Auto-resolver found stale incidents", count=len(stale))

    resolved_count = 0
    failed_count   = 0
    now_str        = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for row in stale:
        iid = str(row.incident_id)

        # Fix #34 — individual transaction per incident
        try:
            with engine.connect() as conn:
                result = conn.execute(text("""
                    UPDATE incidents
                    SET status       = 'RESOLVED',
                        resolved_at  = NOW(),
                        last_updated = NOW()
                    WHERE incident_id = :iid
                      AND status = 'ACTIVE'
                """), {"iid": iid})
                conn.commit()

            if result.rowcount == 0:
                continue  # Already resolved

            # Fix #25 — update Redis record if present
            redis_key = f"incident:{iid}"
            raw = r.get(redis_key)
            if raw:
                try:
                    inc = json.loads(raw)
                    inc["status"]      = "RESOLVED"
                    inc["resolved_at"] = now_str
                    r.setex(redis_key, 1800, json.dumps(inc))
                except Exception:
                    r.delete(redis_key)

            # Fix #25 — srem always, even if Redis key was already gone
            r.srem("index:active_incidents", iid)

            # Broadcast to frontend via Redis pubsub
            try:
                r.publish("detroit-pulse:events", json.dumps({
                    "event": "incident:resolve",
                    "data": {
                        "incident_id":  iid,
                        "status":       "RESOLVED",
                        "resolved_at":  now_str,
                        "last_updated": now_str,
                    },
                }))
            except Exception:
                pass

            log.info("Auto-resolved stale incident",
                     incident_id   = iid[:8],
                     last_updated  = str(row.last_updated)[:19],
                     incident_type = row.incident_type)
            resolved_count += 1

        except Exception as e:
            log.warning("Failed to resolve stale incident",
                        incident_id=iid[:8], error=str(e))
            failed_count += 1

    log.info("Auto-resolver cycle complete",
             resolved=resolved_count,
             failed=failed_count,
             total=len(stale))


def run_auto_resolver():
    log.info("Auto-resolver starting",
             threshold_hours  = STALE_THRESHOLD_HOURS,
             check_interval_s = CHECK_INTERVAL_SECONDS)
    while True:
        try:
            _check_and_resolve_stale()
        except Exception as e:
            log.error("Auto-resolver cycle failed", error=str(e))
        time.sleep(CHECK_INTERVAL_SECONDS)


def start_auto_resolver():
    t = threading.Thread(target=run_auto_resolver, daemon=True)
    t.start()
    return t