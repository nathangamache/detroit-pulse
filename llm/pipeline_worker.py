import json
import os
import re
import time
import uuid
import structlog
import redis
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

# Thread pool for parallel geocoding across workers
_geo_pool = ThreadPoolExecutor(max_workers=8)

REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379/0")
QUEUE_KEY      = "queue:normalization"
DEBUG_CHANNEL  = "detroit-pulse:debug"
PUBSUB_CHANNEL = "detroit-pulse:events"

MAX_SAME_FEED_INCIDENTS  = int(os.getenv("MAX_SAME_FEED_INCIDENTS", "20"))
MAX_CROSS_FEED_INCIDENTS = int(os.getenv("MAX_CROSS_FEED_INCIDENTS", "5"))
SUMMARY_REGEN_EVERY_N    = int(os.getenv("SUMMARY_REGEN_EVERY_N", "3"))

_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


# ── Module-level DB engine singleton ─────────────────────────────────────
_db_engine  = None
_db_session = None


def _get_db_session():
    """Return a SQLAlchemy sessionmaker, creating the engine once."""
    global _db_engine, _db_session
    if _db_engine is None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        _db_engine  = create_engine(
            os.getenv("DATABASE_URL"),
            pool_size     = 5,
            max_overflow  = 5,
            pool_pre_ping = True,
        )
        _db_session = sessionmaker(bind=_db_engine)
    return _db_session


# ── Address validation ────────────────────────────────────────────────────

_STREET_CONTEXT_INDICATORS = {
    "at", "on", "to", "from", "near", "corner", "block", "address",
    "location", "reported", "scene", "respond", "heading", "area",
    "box", "rung",
}

_STREET_SUFFIXES = {
    "avenue", "ave", "street", "st", "road", "rd", "boulevard", "blvd",
    "drive", "dr", "lane", "ln", "highway", "hwy", "court", "ct",
    "place", "pl", "way", "mile",
}

_DIRECTIONALS = {
    "north", "south", "east", "west", "n", "s", "e", "w",
    "ne", "nw", "se", "sw",
}


def _validate_extraction(transcript: str, normalized: str) -> str:
    """
    Fix #2 — validates that the street name extracted from normalized
    appears in the transcript in an address-like context.
    """
    if not normalized or normalized == "NO_LOCATION":
        return normalized

    transcript_lower = transcript.lower()
    parts      = normalized.split(",")
    addr_part  = parts[0].strip()
    addr_words = addr_part.lower().split()

    street_words = [
        w for w in addr_words
        if not w.isdigit() and w not in _DIRECTIONALS
    ]
    key_words = [w for w in street_words if w not in _STREET_SUFFIXES]

    if not key_words:
        return normalized

    primary = max(key_words, key=len)

    if primary not in transcript_lower:
        log.warning("Address hallucination: street name absent from transcript",
                    normalized=normalized, street_word=primary,
                    transcript=transcript[:100])
        return "NO_LOCATION"

    positions = [m.start() for m in re.finditer(
        rf"\b{re.escape(primary)}\b", transcript_lower
    )]
    in_address_context = False
    for pos in positions:
        window       = transcript_lower[max(0, pos - 50): pos + 50]
        window_words = set(window.split())
        if window_words & _STREET_CONTEXT_INDICATORS:
            in_address_context = True
            break
        nearby = transcript_lower[max(0, pos - 30): pos + 30]
        if re.search(r"\b\d{3,5}\b", nearby):
            in_address_context = True
            break

    if not in_address_context:
        log.info("Street name found but not in clear address context — keeping",
                 normalized=normalized, street_word=primary,
                 transcript=transcript[:100])
    return normalized


# ── Active incident context cap ───────────────────────────────────────────

def _build_active_incidents_context(
    feed_id:    str,
    all_active: list[dict],
) -> list[dict]:
    """Fix #10 — cap incident count passed to LLM."""
    same  = [i for i in all_active if i.get("feed_id") == feed_id]
    cross = [i for i in all_active if i.get("feed_id") != feed_id]
    same  = sorted(same,  key=lambda x: x.get("last_updated", ""),
                   reverse=True)[:MAX_SAME_FEED_INCIDENTS]
    cross = sorted(cross, key=lambda x: x.get("last_updated", ""),
                   reverse=True)[:MAX_CROSS_FEED_INCIDENTS]
    return same + cross


# ── Geo enrichment guard ──────────────────────────────────────────────────

def _should_enrich(
    action:   str,
    incident: dict,
    new_lat:  float | None,
    new_lng:  float | None,
) -> bool:
    """Fix #35 — only run polygon scan on NEW or coordinate-changing UPDATE."""
    if action == "NEW":
        return True
    if action != "UPDATE" or new_lat is None or new_lng is None:
        return False
    old_lat = incident.get("lat")
    old_lng = incident.get("lng")
    if old_lat is None or old_lng is None:
        return True
    return (abs(float(new_lat) - float(old_lat)) > 0.001 or
            abs(float(new_lng) - float(old_lng)) > 0.001)


# ── Summary throttle ──────────────────────────────────────────────────────

def _should_regenerate_summary(incident: dict) -> bool:
    """Fix #30 — throttle summary to every N chunks."""
    n = len(incident.get("chunk_ids", []))
    return n <= 1 or (n % SUMMARY_REGEN_EVERY_N == 0)


# ── Publishing ────────────────────────────────────────────────────────────

def publish_debug(event: str, feed_id: str, data: dict) -> None:
    r = get_redis()
    try:
        r.publish(DEBUG_CHANNEL, json.dumps({
            "event":   f"debug:{event}",
            "feed_id": feed_id,
            "data":    data,
            "ts":      time.time(),
        }))
    except Exception:
        pass


def publish_incident_event(event: str, incident: dict) -> None:
    r = get_redis()
    try:
        r.publish(PUBSUB_CHANNEL, json.dumps({
            "event": event,
            "data":  incident,
        }))
    except Exception as e:
        log.error("Failed to publish incident event", event=event, error=str(e))


# ── DB write ──────────────────────────────────────────────────────────────

def write_to_db(
    chunk_id:               str,
    feed_id:                str,
    county:                 str,
    transcript:             str,
    normalized_address:     str,
    geocode_result:         dict,
    structured:             dict,
    correlation_result:     dict,
    processing_ms:          int,
    unit_inferred_location: dict | None = None,
) -> None:
    """
    Write incident (first) then chunk (second) to satisfy FK constraint.
    Fix #29 — all sessions closed in finally blocks.
    Fix #31 — inferred coords flagged with is_location_inferred.
    Fix #30 — summary regeneration throttled.
    Fix #35 — geo enrichment only when warranted.
    ADDR  — retroactive coordinate and address promotion.
    """
    from sqlalchemy import text
    from db.models import Incident as IncidentModel, TranscriptChunk

    Session = _get_db_session()

    action        = correlation_result.get("action", "UNASSOCIATED")
    incident_dict = correlation_result.get("incident")
    incident_id   = correlation_result.get("incident_id")

    lat                  = geocode_result.get("lat")
    lng                  = geocode_result.get("lng")
    location_is_inferred = False

    if lat is None and unit_inferred_location:
        lat                  = unit_inferred_location.get("lat")
        lng                  = unit_inferred_location.get("lng")
        location_is_inferred = True

    # ── Incident write FIRST (chunk FK depends on incident existing) ───
    if action != "UNASSOCIATED" and incident_id:
        db2 = Session()
        try:
            existing = db2.get(IncidentModel, uuid.UUID(incident_id))

            if action == "NEW" and existing is None:
                row = IncidentModel(
                    incident_id   = uuid.UUID(incident_id),
                    feed_id       = feed_id,
                    county        = county,
                    status        = "ACTIVE",
                    incident_type = structured.get("incident_type", "UNKNOWN"),
                    priority      = structured.get("priority", "UNKNOWN"),
                    address_raw   = normalized_address,
                    address_full  = (geocode_result.get("formatted") or
                                     normalized_address),
                    city          = (incident_dict.get("city", "Unknown")
                                     if incident_dict else "Unknown"),
                    lat           = lat,
                    lng           = lng,
                    units         = (incident_dict.get("units", [])
                                     if incident_dict else []),
                    units_cleared = [],
                    summary       = (incident_dict.get("summary", "")
                                     if incident_dict else ""),
                )
                if hasattr(row, "is_location_inferred"):
                    row.is_location_inferred = location_is_inferred
                db2.add(row)
                db2.commit()
                log.info("Incident created",
                         address_full  = row.address_full,
                         incident_id   = str(incident_id)[:8],
                         incident_type = structured.get("incident_type"))

            elif action in ("UPDATE", "RESOLVE") and existing:
                if incident_dict:
                    existing.units         = incident_dict.get("units",
                                                               existing.units)
                    existing.units_cleared = incident_dict.get("units_cleared",
                                                               existing.units_cleared)
                    existing.priority      = incident_dict.get("priority",
                                                               existing.priority)
                    existing.last_updated  = time.strftime(
                        "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()
                    )

                    # Retroactive coordinate promotion
                    if lat and lng:
                        old_is_inferred = getattr(existing,
                                                   "is_location_inferred", False)
                        has_no_coords   = (existing.lat is None or
                                           existing.lng is None)
                        if has_no_coords or (not location_is_inferred or
                                              old_is_inferred):
                            existing.lat = lat
                            existing.lng = lng
                            if hasattr(existing, "is_location_inferred"):
                                existing.is_location_inferred = location_is_inferred
                            if has_no_coords:
                                log.info("Retroactively promoted coordinates",
                                         incident_id = str(incident_id)[:8],
                                         lat=lat, lng=lng,
                                         source=geocode_result.get("source"))

                    # Retroactive address promotion
                    new_formatted = geocode_result.get("formatted", "")
                    new_conf      = geocode_result.get("confidence", "LOW")
                    existing_addr = existing.address_full or ""

                    def _is_city_level(addr: str) -> bool:
                        import re as _re
                        parts = [p.strip() for p in addr.split(",")]
                        if parts and not _re.match(r"^\d+\s", parts[0]):
                            if len(parts) <= 3 and not any(
                                w in parts[0].lower()
                                for w in ("road", "ave", "street", "drive",
                                          "blvd", "lane", "court", "way", "hwy")
                            ):
                                return True
                        return False

                    should_promote = (
                        existing_addr in (None, "", "NO_LOCATION") or
                        (new_formatted and
                         new_conf in ("HIGH", "MEDIUM") and
                         _is_city_level(existing_addr) and
                         not _is_city_level(new_formatted))
                    )
                    if should_promote and new_formatted and \
                            new_conf in ("HIGH", "MEDIUM"):
                        existing.address_full = new_formatted
                        log.info("Promoted address_full on incident",
                                 incident_id = str(incident_id)[:8],
                                 old_address = existing_addr[:50],
                                 new_address = new_formatted[:60],
                                 confidence  = new_conf)

                if action == "RESOLVE":
                    existing.status      = "RESOLVED"
                    existing.resolved_at = time.strftime(
                        "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()
                    )
                db2.commit()

        except Exception as e:
            db2.rollback()
            log.error("DB incident write failed",
                      action=action, error=str(e),
                      incident_id=str(incident_id or "")[:8])
            return
        finally:
            db2.close()

    # ── Chunk write SECOND (after incident exists in DB) ──────────────
    db1 = Session()
    try:
        db1.add(TranscriptChunk(
            chunk_id               = uuid.UUID(chunk_id),
            incident_id            = uuid.UUID(incident_id) if incident_id else None,
            feed_id                = feed_id,
            timestamp              = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                                   time.gmtime()),
            raw_transcript         = transcript,
            normalized_address     = normalized_address,
            correlation_action     = action,
            correlation_method     = "engine_v4",
            correlation_confidence = structured.get("priority", "UNKNOWN"),
            geocode_source         = geocode_result.get("source", "none"),
            geocode_confidence     = geocode_result.get("confidence", "FAILED"),
            whisper_model          = os.getenv("WHISPER_MODEL", "large-v3"),
            lora_version           = os.getenv("LORA_VERSION", "base"),
            processing_ms          = processing_ms,
        ))
        db1.commit()
        log.info("DB write OK", action=action,
                 incident_id=str(incident_id or "")[:8])
    except Exception as e:
        db1.rollback()
        log.error("DB chunk write failed", error=str(e))
        return
    finally:
        db1.close()

    if action == "UNASSOCIATED" or not incident_id:
        return

    # ── Geo enrichment ────────────────────────────────────────────────
    if lat and lng and incident_dict and _should_enrich(
            action, incident_dict, lat, lng):
        db3 = Session()
        try:
            from api.geo_enrichment import enrich_incident
            enrichment = enrich_incident(lat, lng,
                                          incident_dict.get("units", []))
            if enrichment and incident_id:
                db3.execute(text("""
                    UPDATE incidents
                    SET precinct         = :precinct,
                        battalion        = :battalion,
                        nearest_stations = :stations
                    WHERE incident_id = :iid
                """), {
                    "precinct":  enrichment.get("precinct"),
                    "battalion": enrichment.get("battalion"),
                    "stations":  json.dumps(
                        enrichment.get("nearest_stations", [])
                    ),
                    "iid":       incident_id,
                })
                db3.commit()
                log.debug("Geo enrichment applied",
                          incident_id = str(incident_id)[:8],
                          precinct    = enrichment.get("precinct"),
                          battalion   = enrichment.get("battalion"))
        except Exception as e:
            db3.rollback()
            log.warning("Geo enrichment failed", error=str(e))
        finally:
            db3.close()

    # ── Summary regeneration ──────────────────────────────────────────
    if action in ("NEW", "UPDATE") and incident_dict:
        if _should_regenerate_summary(incident_dict):
            db4 = Session()
            try:
                from db.models import TranscriptChunk as TC
                from sqlalchemy import select as _select
                from llm.structure import generate_incident_summary

                chunks = db4.execute(
                    _select(TC)
                    .where(TC.incident_id == uuid.UUID(incident_id))
                    .order_by(TC.timestamp)
                ).scalars().all()

                if chunks:
                    summary = generate_incident_summary(
                        incident_dict,
                        [c.to_dict() for c in chunks],
                        feed_id=feed_id,
                    )
                    if summary:
                        db4.execute(text(
                            "UPDATE incidents SET summary = :s "
                            "WHERE incident_id = :iid"
                        ), {"s": summary, "iid": incident_id})
                        db4.commit()

                        r   = get_redis()
                        raw = r.get(f"incident:{incident_id}")
                        if raw:
                            try:
                                inc = json.loads(raw)
                                inc["summary"] = summary
                                ttl = r.ttl(f"incident:{incident_id}")
                                r.setex(f"incident:{incident_id}",
                                        ttl if ttl > 0 else 14400,
                                        json.dumps(inc))
                            except Exception:
                                pass

                        log.info("Summary regenerated",
                                 incident_id = str(incident_id)[:8],
                                 chunk_count = len(chunks))
            except Exception as e:
                db4.rollback()
                log.warning("Summary regeneration failed", error=str(e))
            finally:
                db4.close()
        else:
            log.debug("Summary regen skipped (throttled)",
                      incident_id = str(incident_id)[:8],
                      chunk_n     = len(incident_dict.get("chunk_ids", [])))


# ── Main processing function ──────────────────────────────────────────────

def process_chunk(chunk: dict, retry_attempt: int = 0) -> None:
    """
    Run a single chunk through the full pipeline:
      1. Normalization
      2. Validation
      3. Geocoding
      4. Active incident context
      5. LLM structuring
      5b. Unit-location inference
      6. Correlation (Phase 1 engine_v4)
      7. DB write
      8. Broadcast to frontend
    """
    t_start    = time.time()
    chunk_id   = chunk.get("chunk_id", str(uuid.uuid4()))
    feed_id    = chunk.get("feed_id", "")
    county     = chunk.get("county", "")
    transcript = chunk.get("transcript", "")

    if not transcript.strip():
        return

    if retry_attempt > 0:
        log.info("Re-processing retry chunk",
                 chunk_id=chunk_id[:8], feed_id=feed_id,
                 attempt=retry_attempt)

    # Stage 1 — Normalization
    from llm.normalize import normalize_address
    t0         = time.time()
    normalized = normalize_address(transcript, feed_id=feed_id, chunk_id=chunk_id)
    norm_ms    = int((time.time() - t0) * 1000)

    # Stage 2 — Validation
    normalized = _validate_extraction(transcript, normalized)

    # Stage 3 — Geocoding (runs in thread pool — non-blocking)
    from geocode.geocoder import geocode
    t0             = time.time()
    try:
        geo_future     = _geo_pool.submit(geocode, normalized, feed_id)
        geocode_result = geo_future.result(timeout=15)
    except Exception as e:
        log.warning("Geocode timed out or failed", error=str(e))
        geocode_result = {}
    geo_ms         = int((time.time() - t0) * 1000)

    lat = geocode_result.get("lat")
    lng = geocode_result.get("lng")

    # Stage 4 — Active incident context
    from correlation.incident_store import get_all_active
    all_active     = get_all_active(feed_id=None)
    active_context = _build_active_incidents_context(feed_id, all_active)

    # Stage 5 — LLM structuring
    from llm.structure import structure_transcript
    t0         = time.time()
    structured = structure_transcript(
        transcript         = transcript,
        normalized_address = normalized,
        geocoded_address   = geocode_result.get("formatted", ""),
        lat                = lat,
        lng                = lng,
        county             = county,
        feed_id            = feed_id,
        active_incidents   = active_context,
    )
    struct_ms = int((time.time() - t0) * 1000)

    publish_debug("normalization", feed_id, {
        "chunk_id":      chunk_id,
        "transcript":    transcript[:200],
        "normalized":    normalized,
        "geocoded":      geocode_result.get("formatted", ""),
        "structured":    structured,
        "norm_ms":       norm_ms,
        "geo_ms":        geo_ms,
        "struct_ms":     struct_ms,
        "retry_attempt": retry_attempt,
    })

    if not structured.get("has_incident"):
        return

    # Stage 5b — Unit-location inference if geocoding failed
    unit_inferred_location = None
    if lat is None:
        units_added = structured.get("units_added", [])
        if units_added:
            try:
                from api.geo_enrichment import infer_location_from_units
                unit_inferred_location = infer_location_from_units(units_added)
                if unit_inferred_location:
                    log.info("Unit-inferred location",
                             units = units_added,
                             lat   = unit_inferred_location.get("lat"),
                             lng   = unit_inferred_location.get("lng"))
            except Exception:
                pass

    # Stage 6 — Correlation (Phase 1: engine_v4)
    from correlation.engine import correlate
    t0                 = time.time()
    correlation_result = correlate(
        chunk_id               = chunk_id,
        feed_id                = feed_id,
        county                 = county,
        transcript             = transcript,
        structured             = structured,
        normalized_address     = normalized,
        geocode_result         = geocode_result,
        unit_inferred_location = unit_inferred_location,
        retry_attempt          = retry_attempt,
    )
    corr_ms = int((time.time() - t0) * 1000)

    action = correlation_result.get("action", "UNASSOCIATED")
    log.info("Chunk correlated",
             action        = action,
             feed_id       = feed_id,
             chunk_id      = chunk_id,
             corr_ms       = corr_ms,
             retry_attempt = retry_attempt)

    # Stage 7 — DB write
    write_to_db(
        chunk_id               = chunk_id,
        feed_id                = feed_id,
        county                 = county,
        transcript             = transcript,
        normalized_address     = normalized,
        geocode_result         = geocode_result,
        structured             = structured,
        correlation_result     = correlation_result,
        processing_ms          = int((time.time() - t_start) * 1000),
        unit_inferred_location = unit_inferred_location,
    )

    # Stage 8 — Broadcast to frontend
    incident = correlation_result.get("incident")
    if incident:
        if action == "NEW":
            publish_incident_event("incident:new", incident)
        elif action == "UPDATE":
            publish_incident_event("incident:update", incident)
        elif action == "RESOLVE":
            publish_incident_event("incident:resolve", incident)


# ── Consumer loop ─────────────────────────────────────────────────────────

def run_pipeline_worker():
    """
    Blocking Redis queue consumer.
    Phase 1: Before processing each new chunk, drain the retry queue
    for that feed so pending ambiguous chunks get re-evaluated with
    fresh active incident context.

    Called by run_pipeline.py in its own thread.
    """
    from correlation.retry_queue import pop_retry_queue, all_queue_depths

    r = get_redis()
    log.info("Pipeline worker ready (Phase 1 — engine_v4)", queue=QUEUE_KEY)

    last_retry_depth_log = 0

    while True:
        try:
            item = r.brpop(QUEUE_KEY, timeout=5)
            if item is None:
                # Periodically log retry queue depths for monitoring
                now = time.time()
                if now - last_retry_depth_log > 60:
                    depths = all_queue_depths(r)
                    if depths:
                        log.info("Retry queue depths", depths=depths)
                    last_retry_depth_log = now
                continue

            _, raw = item
            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError as e:
                log.error("Failed to parse chunk from queue", error=str(e))
                continue

            feed_id = chunk.get("feed_id", "")

            # ── Phase 1: Step 0 ─────────────────────────────────────
            # Drain retry queue for this feed BEFORE processing the new
            # chunk. The new chunk's arrival means we have a new
            # transmission from the same feed — retried chunks can now
            # be re-evaluated with updated active incident context.
            if feed_id:
                pending = pop_retry_queue(r, feed_id)
                if pending:
                    log.info("Processing retry queue before new chunk",
                             feed_id       = feed_id,
                             retry_count   = len(pending),
                             new_chunk_id  = chunk.get("chunk_id", "")[:8])
                    for retry_item in pending:
                        retry_chunk   = retry_item["chunk"]
                        retry_attempt = retry_item.get("attempt", 0)
                        try:
                            process_chunk(retry_chunk,
                                          retry_attempt=retry_attempt + 1)
                        except Exception as e:
                            log.error("Retry chunk processing failed",
                                      error    = str(e),
                                      feed_id  = feed_id,
                                      chunk_id = retry_chunk.get(
                                          "chunk_id", "")[:8])

            # ── Normal chunk processing ──────────────────────────────
            try:
                process_chunk(chunk)
            except Exception as e:
                log.error("process_chunk failed",
                          error   = str(e),
                          feed_id = feed_id,
                          chunk   = str(raw)[:120])

        except Exception as e:
            log.error("Pipeline worker loop error", error=str(e))
            time.sleep(1)