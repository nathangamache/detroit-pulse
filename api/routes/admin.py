import json
import os
import structlog
import redis
from fastapi import APIRouter, HTTPException
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

load_dotenv()
log = structlog.get_logger()

REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://detroit:detroit@localhost:5432/detroitpulse")

router = APIRouter(prefix="/admin", tags=["admin"])

_redis = None
_engine = None
_Session = None


def get_redis():
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def get_db():
    global _engine, _Session
    if _engine is None:
        _engine  = create_engine(DATABASE_URL)
        _Session = sessionmaker(bind=_engine)
    return _Session()


# ── Redis overview ────────────────────────────────────────────────────────

@router.get("/redis/overview")
def redis_overview():
    r = get_redis()
    info = r.info("memory")
    return {
        "used_memory_human":   info.get("used_memory_human"),
        "connected_clients":   r.info("clients").get("connected_clients"),
        "queues": {
            "transcription": r.llen("queue:transcription"),
            "normalization":  r.llen("queue:normalization"),
            "unassociated":   r.llen("queue:unassociated"),
        },
        "active_units":     len(r.keys("unit:*")),
        "active_incidents": len(r.smembers("index:active_incidents")),
        "geocache_entries": len(r.keys("geocache:*")),
        "debug_history":    r.llen("detroit-pulse:debug-history"),
    }


# ── Active incidents (Redis) ──────────────────────────────────────────────

@router.get("/redis/incidents")
def list_redis_incidents():
    from correlation.incident_store import get_all_active
    incidents = get_all_active()
    return {
        "count":     len(incidents),
        "incidents": incidents,
    }


@router.delete("/redis/incidents/{incident_id}")
def delete_redis_incident(incident_id: str):
    r = get_redis()
    key = f"incident:{incident_id}"
    if not r.exists(key):
        raise HTTPException(status_code=404, detail="Incident not found in Redis")
    r.delete(key)
    r.srem("index:active_incidents", incident_id)

    # Also release any units assigned to this incident
    unit_keys = r.keys("unit:*")
    released  = []
    for uk in unit_keys:
        if r.get(uk) == incident_id:
            r.delete(uk)
            released.append(uk.replace("unit:", ""))

    log.info("Redis incident deleted", incident_id=incident_id, units_released=released)
    return {"deleted": incident_id, "units_released": released}


@router.delete("/redis/incidents")
def delete_all_redis_incidents():
    r = get_redis()
    incident_ids = r.smembers("index:active_incidents")
    count = 0
    for iid in incident_ids:
        r.delete(f"incident:{iid}")
        count += 1
    r.delete("index:active_incidents")

    # Release all units
    unit_keys = r.keys("unit:*")
    for uk in unit_keys:
        r.delete(uk)

    log.info("All Redis incidents cleared", count=count, units=len(unit_keys))
    return {"deleted_incidents": count, "released_units": len(unit_keys)}


# ── Unit state ────────────────────────────────────────────────────────────

@router.get("/redis/units")
def list_units():
    from correlation.unit_store import active_units
    units = active_units()
    return {"count": len(units), "units": units}


@router.delete("/redis/units/{unit_id}")
def delete_unit(unit_id: str):
    r = get_redis()
    key = f"unit:{unit_id.upper()}"
    if not r.exists(key):
        raise HTTPException(status_code=404, detail="Unit not found")
    r.delete(key)
    return {"deleted": unit_id.upper()}


@router.delete("/redis/units")
def delete_all_units():
    r = get_redis()
    keys = r.keys("unit:*")
    for k in keys:
        r.delete(k)
    return {"deleted": len(keys)}


# ── Queues ────────────────────────────────────────────────────────────────

@router.get("/redis/queues")
def list_queues():
    r = get_redis()
    queues = {
        "transcription": [],
        "normalization":  [],
        "unassociated":   [],
    }
    for q_name, key in [
        ("transcription", "queue:transcription"),
        ("normalization",  "queue:normalization"),
        ("unassociated",   "queue:unassociated"),
    ]:
        items = r.lrange(key, 0, 49)  # max 50 per queue
        parsed = []
        for item in items:
            try:
                parsed.append(json.loads(item))
            except json.JSONDecodeError:
                parsed.append({"raw": item})
        queues[q_name] = parsed
    return queues


@router.delete("/redis/queues/{queue_name}")
def flush_queue(queue_name: str):
    valid = {"transcription", "normalization", "unassociated"}
    if queue_name not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown queue: {queue_name}")
    r = get_redis()
    key   = f"queue:{queue_name}"
    count = r.llen(key)
    r.delete(key)
    return {"flushed": queue_name, "items_removed": count}


@router.delete("/redis/queues")
def flush_all_queues():
    r = get_redis()
    results = {}
    for q in ["transcription", "normalization", "unassociated"]:
        key = f"queue:{q}"
        count = r.llen(key)
        r.delete(key)
        results[q] = count
    return {"flushed": results}


# ── Debug history ─────────────────────────────────────────────────────────

@router.get("/redis/debug")
def get_debug_history():
    from api.broadcaster import get_debug_history
    history = get_debug_history()
    return {"count": len(history), "events": history}


@router.delete("/redis/debug")
def clear_debug_history():
    r = get_redis()
    r.delete("detroit-pulse:debug-history")
    return {"cleared": True}


# ── Geocache ──────────────────────────────────────────────────────────────

@router.get("/redis/geocache")
def list_geocache():
    r   = get_redis()
    keys = r.keys("geocache:*")
    entries = []
    for key in keys[:100]:  # cap at 100
        raw = r.get(key)
        ttl = r.ttl(key)
        if raw:
            try:
                entries.append({
                    "key": key,
                    "ttl_hours": round(ttl / 3600, 1) if ttl > 0 else None,
                    **json.loads(raw),
                })
            except json.JSONDecodeError:
                pass
    return {"count": len(keys), "entries": entries}


@router.delete("/redis/geocache")
def clear_geocache():
    r    = get_redis()
    keys = r.keys("geocache:*")
    for k in keys:
        r.delete(k)
    return {"cleared": len(keys)}


# ── Database incidents ────────────────────────────────────────────────────

@router.get("/db/incidents")
def list_db_incidents(limit: int = 50):
    db = get_db()
    try:
        rows = db.execute(text("""
            SELECT incident_id, feed_id, county, status, incident_type,
                   priority, address_full, lat, lng, opened_at,
                   last_updated, units, summary
            FROM incidents
            ORDER BY opened_at DESC
            LIMIT :limit
        """), {"limit": limit}).fetchall()
        return {
            "count": len(rows),
            "incidents": [dict(row._mapping) for row in rows],
        }
    finally:
        db.close()


@router.delete("/db/incidents/{incident_id}")
def delete_db_incident(incident_id: str):
    db = get_db()
    try:
        db.execute(text(
            "DELETE FROM transcript_chunks WHERE incident_id = :id"
        ), {"id": incident_id})
        result = db.execute(text(
            "DELETE FROM incidents WHERE incident_id = :id"
        ), {"id": incident_id})
        db.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Incident not found in DB")
        return {"deleted": incident_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.delete("/db/incidents")
def delete_all_db_incidents():
    db = get_db()
    try:
        chunks   = db.execute(text("DELETE FROM transcript_chunks")).rowcount
        incidents = db.execute(text("DELETE FROM incidents")).rowcount
        db.commit()
        return {"deleted_incidents": incidents, "deleted_chunks": chunks}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ── Nuclear option ────────────────────────────────────────────────────────

@router.delete("/reset/all")
def reset_everything():
    """
    Wipe all Redis state and all DB incidents.
    Use this to start fresh between test runs.
    """
    r  = get_redis()
    db = get_db()

    # Redis
    patterns = ["incident:*", "unit:*", "queue:*",
                "geocache:*", "index:*", "detroit-pulse:*"]
    redis_deleted = 0
    for pattern in patterns:
        keys = r.keys(pattern)
        for k in keys:
            r.delete(k)
            redis_deleted += 1

    # DB
    try:
        chunks    = db.execute(text("DELETE FROM transcript_chunks")).rowcount
        incidents = db.execute(text("DELETE FROM incidents")).rowcount
        db.commit()
    except Exception as e:
        db.rollback()
        incidents = chunks = 0
    finally:
        db.close()

    log.info("Full reset complete",
             redis_keys=redis_deleted,
             db_incidents=incidents,
             db_chunks=chunks)

    return {
        "redis_keys_deleted":   redis_deleted,
        "db_incidents_deleted": incidents,
        "db_chunks_deleted":    chunks,
    }

# ── Chunk reprocessing ────────────────────────────────────────────────────

@router.post("/reprocess/chunk/{chunk_id}")
def reprocess_chunk(chunk_id: str):
    """
    Re-run a transcript chunk through the full pipeline:
    normalize -> geocode -> structure -> correlate -> DB update -> broadcast.
    Uses current Redis active incidents state so cross-feed associations
    that previously failed may now succeed.
    """
    import uuid
    import time
    from llm.normalize          import normalize_address
    from llm.structure          import structure_transcript
    from geocode.geocoder       import geocode
    from correlation.engine     import correlate
    from correlation.incident_store import get_all_active
    from api.broadcaster        import (
        publish_debug, publish_incident_new,
        publish_incident_update, publish_incident_resolve,
        publish_unassociated,
    )

    db = get_db()

    try:
        # Load the chunk — cast to text for UUID comparison
        row = db.execute(text("""
            SELECT chunk_id::text, feed_id, raw_transcript,
                   incident_id::text, normalized_address, geocode_source
            FROM transcript_chunks
            WHERE chunk_id::text = :id
        """), {"id": str(chunk_id)}).fetchone()

        if not row:
            raise HTTPException(status_code=404,
                                detail=f"Chunk not found: {chunk_id}")

        feed_id    = row.feed_id
        transcript = row.raw_transcript

        if not transcript:
            raise HTTPException(status_code=400,
                                detail="Chunk has no transcript to reprocess")

        publish_debug("normalization", feed_id,
                      status="reprocessing", chunk_id=chunk_id)

        # Stage 1 — normalize
        normalized = normalize_address(transcript, feed_id=feed_id)

        # Stage 2 — geocode
        geo = geocode(normalized)

        # Stage 3 — structure with current active incidents context
        same_feed = get_all_active(feed_id=feed_id)
        all_active = get_all_active(feed_id=None)
        same_ids   = {i["incident_id"] for i in same_feed}
        cross_feed = sorted(
            [i for i in all_active if i["incident_id"] not in same_ids],
            key=lambda x: x.get("opened_at", ""), reverse=True
        )[:5]
        active = same_feed + cross_feed

        structured = structure_transcript(
            transcript         = transcript,
            normalized_address = normalized,
            geocoded_address   = geo.get("formatted") or normalized,
            lat                = geo.get("lat"),
            lng                = geo.get("lng"),
            county             = _get_county(feed_id),
            feed_id            = feed_id,
            active_incidents   = active,
        )

        # Stage 4 — correlate
        new_chunk_id = str(uuid.uuid4())
        correlation  = correlate(
            chunk_id           = new_chunk_id,
            feed_id            = feed_id,
            county             = _get_county(feed_id),
            transcript         = transcript,
            structured         = structured,
            normalized_address = normalized,
            geocode_result     = geo,
        )

        # Stage 5 — update DB chunk record
        db.execute(text("""
            UPDATE transcript_chunks SET
                normalized_address   = :normalized,
                correlation_action   = :action,
                geocode_source       = :geo_source,
                geocode_confidence   = :geo_conf
            WHERE chunk_id::text = :id
        """), {
            "normalized":  normalized,
            "action":      correlation["action"],
            "geo_source":  geo.get("source"),
            "geo_conf":    geo.get("confidence"),
            "id":          chunk_id,
        })

        # If chunk was previously unassociated and now correlates,
        # update its incident_id linkage
        if correlation["incident_id"] and not row.incident_id:
            db.execute(text("""
                UPDATE transcript_chunks
                SET incident_id = :inc_id::uuid
                WHERE chunk_id::text = :id
            """), {"inc_id": correlation["incident_id"], "id": str(chunk_id)})

        # If geocoding succeeded and the parent incident has no location,
        # update the incident record with the new coordinates and address.
        # This is the key step that makes the map pin appear correctly.
        if (geo.get("lat") and geo.get("confidence") != "FAILED"
                and correlation["incident_id"]):
            db.execute(text("""
                UPDATE incidents SET
                    lat          = :lat,
                    lng          = :lng,
                    address_full = :address_full,
                    address_raw  = COALESCE(NULLIF(address_raw, 'NO_LOCATION'),
                                            address_raw, :address_raw),
                    last_updated = NOW()
                WHERE incident_id::text = :inc_id
                AND   (lat IS NULL OR address_full IS NULL
                       OR address_full = 'NO_LOCATION'
                       OR address_raw  = 'NO_LOCATION')
            """), {
                "lat":          geo.get("lat"),
                "lng":          geo.get("lng"),
                "address_full": geo.get("formatted") or normalized,
                "address_raw":  normalized,
                "inc_id":       correlation["incident_id"],
            })
            log.info("Incident location updated via reprocess",
                     incident_id=correlation["incident_id"],
                     address=geo.get("formatted"))

        db.commit()

        # Regenerate full incident summary after reprocess
        if (correlation["action"] in ("NEW", "UPDATE")
                and correlation["incident_id"]):
            try:
                from llm.structure import generate_incident_summary
                chunk_rows = db.execute(text("""
                    SELECT raw_transcript, correlation_action,
                           timestamp::text
                    FROM transcript_chunks
                    WHERE incident_id::text = :iid
                    ORDER BY timestamp ASC
                """), {"iid": correlation["incident_id"]}).fetchall()

                chunks_for_summary = [
                    {
                        "raw_transcript":    r.raw_transcript,
                        "correlation_action":r.correlation_action,
                        "timestamp":         r.timestamp,
                    }
                    for r in chunk_rows
                    if r.raw_transcript
                ]

                new_summary = generate_incident_summary(
                    incident = correlation["incident"],
                    chunks   = chunks_for_summary,
                    feed_id  = feed_id,
                )

                if new_summary:
                    db.execute(text("""
                        UPDATE incidents SET summary = :summary
                        WHERE incident_id::text = :iid
                    """), {
                        "summary": new_summary,
                        "iid":     correlation["incident_id"],
                    })
                    db.commit()
                    if correlation["incident"]:
                        correlation["incident"]["summary"] = new_summary

            except Exception as _e:
                log.warning("Summary regeneration failed in reprocess",
                            error=str(_e))

        # Stage 6 — broadcast updated incident to map
        if correlation["action"] in ("NEW", "UPDATE", "RESOLVE") \
                and correlation["incident"]:
            inc = correlation["incident"]
            frontend_incident = {
                "incident_id":   inc["incident_id"],
                "feed_id":       inc["feed_id"],
                "county":        inc["county"],
                "status":        inc["status"],
                "opened_at":     inc.get("opened_at"),
                "last_updated":  inc.get("last_updated"),
                "incident_type": inc["incident_type"],
                "priority":      inc["priority"],
                "address_raw":   inc.get("address_raw"),
                "address_full":  inc.get("address_full"),
                "city":          inc.get("city"),
                "lat":           inc.get("lat"),
                "lng":           inc.get("lng"),
                "units":         inc.get("units", []),
                "units_cleared": inc.get("units_cleared", []),
                "summary":       inc.get("summary"),
                "chunk_count":   1,
            }
            if correlation["action"] == "NEW":
                publish_incident_new(frontend_incident)
            elif correlation["action"] == "UPDATE":
                publish_incident_update(frontend_incident)
            elif correlation["action"] == "RESOLVE":
                publish_incident_resolve(frontend_incident)
        elif correlation["action"] == "UNASSOCIATED":
            publish_unassociated(chunk_id, feed_id, transcript)

        return {
            "chunk_id":          chunk_id,
            "feed_id":           feed_id,
            "normalized":        normalized,
            "geocode_confidence": geo.get("confidence"),
            "geocode_source":    geo.get("source"),
            "correlation_action": correlation["action"],
            "incident_id":       correlation["incident_id"],
            "has_incident":      structured.get("has_incident"),
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


def _get_county(feed_id: str) -> str:
    mapping = {
        "wayneco_downriver":             "wayne",
        "wayneco_detroit_police_fire":   "wayne",
        "wayneco_detroit_police_dispatch":"wayne",
        "wayneco_detroit_fire":          "wayne",
        "wayneco_public_safety":         "wayne",
        "wayneco_westland_gardencity":   "wayne",
        "wayneco_dearborn":              "wayne",
        "wayneco_grossepointe":          "wayne",
        "wayneco_plymouthnorthville":    "wayne",
        "wayneco_southwestern":          "wayne",
        "wayneco_detroit_ems":           "wayne",
        "wayneco_romulus":               "wayne",
        "wayneco_northville_plymouth_city": "wayne",
        "wayneco_franklin_bingham":      "wayne",
        "oaklandco_royaloak_fire":       "oakland",
        "washtenaw_metro":               "washtenaw",
        "washtenaw_livingston":          "livingston",
    }
    return mapping.get(feed_id, "unknown")


# ── Incident merge ────────────────────────────────────────────────────────

@router.post("/merge/incidents")
def merge_incidents(body: dict):
    """
    Merge source_incident_id into target_incident_id.
    All chunks from source are re-linked to target.
    Source incident is deleted from DB and Redis.
    Target incident is updated on the map.
    """
    from api.broadcaster import publish_incident_update

    source_id = body.get("source_id")
    target_id = body.get("target_id")

    if not source_id or not target_id:
        raise HTTPException(status_code=400,
                            detail="source_id and target_id required")
    if source_id == target_id:
        raise HTTPException(status_code=400,
                            detail="source and target cannot be the same")

    db = get_db()
    r  = get_redis()

    try:
        # Verify both exist in DB
        source = db.execute(text(
            "SELECT * FROM incidents WHERE incident_id = :id"
        ), {"id": source_id}).fetchone()
        target = db.execute(text(
            "SELECT * FROM incidents WHERE incident_id = :id"
        ), {"id": target_id}).fetchone()

        if not source:
            raise HTTPException(status_code=404,
                                detail=f"Source incident {source_id} not found")
        if not target:
            raise HTTPException(status_code=404,
                                detail=f"Target incident {target_id} not found")

        source = dict(source._mapping)
        target = dict(target._mapping)

        # Re-link all chunks from source to target
        chunk_count = db.execute(text("""
            UPDATE transcript_chunks
            SET incident_id = :target
            WHERE incident_id = :source
        """), {"target": target_id, "source": source_id}).rowcount

        # Merge units arrays
        source_units   = source.get("units")   or []
        target_units   = target.get("units")   or []
        source_cleared = source.get("units_cleared") or []
        target_cleared = target.get("units_cleared") or []

        merged_units   = list(set(source_units)   | set(target_units))
        merged_cleared = list(set(source_cleared) | set(target_cleared))

        # Update target incident
        db.execute(text("""
            UPDATE incidents SET
                units         = :units,
                units_cleared = :cleared,
                last_updated  = NOW()
            WHERE incident_id = :id
        """), {
            "units":   merged_units,
            "cleared": merged_cleared,
            "id":      target_id,
        })

        # Delete source incident from DB
        db.execute(text(
            "DELETE FROM incidents WHERE incident_id = :id"
        ), {"id": source_id})

        db.commit()

        # Clean up Redis — remove source incident, update unit assignments
        r.delete(f"incident:{source_id}")
        r.srem("index:active_incidents", source_id)

        # Re-assign any units from source to target in Redis
        unit_keys = r.keys("unit:*")
        reassigned = []
        for uk in unit_keys:
            if r.get(uk) == source_id:
                r.set(uk, target_id)
                reassigned.append(uk.replace("unit:", ""))

        # Update target in Redis if it exists there
        from correlation.incident_store import get as get_redis_incident
        redis_target = get_redis_incident(target_id)
        if redis_target:
            redis_target["units"]         = merged_units
            redis_target["units_cleared"] = merged_cleared
            import json as _json
            import os
            ttl = int(os.getenv("INCIDENT_TTL_SECONDS", "14400"))
            r.setex(f"incident:{target_id}", ttl,
                    _json.dumps(redis_target))

        # Broadcast updated target to map
        updated_target = db.execute(text("""
            SELECT incident_id, feed_id, county, status, incident_type,
                   priority, address_full, address_raw, city, lat, lng,
                   units, units_cleared, summary, opened_at, last_updated
            FROM incidents WHERE incident_id = :id
        """), {"id": target_id}).fetchone()

        if updated_target:
            ut = dict(updated_target._mapping)
            publish_incident_update({
                "incident_id":   str(ut["incident_id"]),
                "feed_id":       ut["feed_id"],
                "county":        ut["county"],
                "status":        ut["status"],
                "incident_type": ut["incident_type"],
                "priority":      ut["priority"],
                "address_full":  ut["address_full"],
                "address_raw":   ut["address_raw"],
                "city":          ut["city"],
                "lat":           ut["lat"],
                "lng":           ut["lng"],
                "units":         ut["units"] or [],
                "units_cleared": ut["units_cleared"] or [],
                "summary":       ut["summary"],
                "opened_at":     str(ut["opened_at"]) if ut["opened_at"] else None,
                "last_updated":  str(ut["last_updated"]) if ut["last_updated"] else None,
            })

        return {
            "merged":             True,
            "source_id":          source_id,
            "target_id":          target_id,
            "chunks_relinked":    chunk_count,
            "units_reassigned":   reassigned,
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ── Unassociated chunks ───────────────────────────────────────────────────

@router.get("/redis/unassociated")
def list_unassociated():
    """
    Return all chunks in the unassociated queue — these are chunks
    the pipeline could not correlate to any incident.
    """
    r = get_redis()
    items = r.lrange("queue:unassociated", 0, 99)
    parsed = []
    for item in items:
        try:
            parsed.append(json.loads(item))
        except json.JSONDecodeError:
            pass
    return {"count": len(parsed), "chunks": parsed}


@router.get("/db/unassociated")
def list_db_unassociated(limit: int = 50):
    """
    Return transcript chunks in the DB with no incident association.
    These are candidates for manual reprocessing or merging.
    """
    db = get_db()
    try:
        rows = db.execute(text("""
            SELECT chunk_id, feed_id, timestamp, raw_transcript,
                   normalized_address, correlation_action,
                   geocode_source, geocode_confidence
            FROM transcript_chunks
            WHERE incident_id IS NULL
            ORDER BY timestamp DESC
            LIMIT :limit
        """), {"limit": limit}).fetchall()
        return {
            "count":  len(rows),
            "chunks": [dict(r._mapping) for r in rows],
        }
    finally:
        db.close()

# ── Log file viewer ───────────────────────────────────────────────────────

@router.get("/logs/debug")
def get_debug_log(lines: int = 200, search: str = None):
    """
    Return the last N lines of the pipeline debug log.
    Optional search parameter filters lines containing the search string.
    Useful for quick lookups without SSH access to the log file.
    """
    from pathlib import Path
    log_path = Path("logs/pipeline_debug.log")

    if not log_path.exists():
        return {"lines": [], "total": 0, "log_file": str(log_path)}

    with open(log_path, "r", encoding="utf-8") as f:
        all_lines = f.readlines()

    if search:
        filtered = [l.rstrip() for l in all_lines
                    if search.lower() in l.lower()]
    else:
        filtered = [l.rstrip() for l in all_lines]

    # Return last N lines
    result = filtered[-lines:] if len(filtered) > lines else filtered

    return {
        "lines":    result,
        "total":    len(filtered),
        "showing":  len(result),
        "log_file": str(log_path),
        "search":   search,
    }


@router.get("/logs/debug/download")
def download_debug_log():
    """Stream the full debug log file for download."""
    from pathlib import Path
    from fastapi.responses import FileResponse
    log_path = Path("logs/pipeline_debug.log")
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")
    return FileResponse(
        path         = str(log_path),
        filename     = "detroit_pulse_debug.log",
        media_type   = "text/plain",
    )
