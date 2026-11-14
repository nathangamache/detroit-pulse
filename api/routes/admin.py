"""Admin API. Every route requires X-Pulse-Admin-Token (PULSE_ADMIN_TOKEN); unset -> 404.

The old /admin routes (including DELETE /admin/reset/all) were unauthenticated and
operated on Redis keyspaces that no longer exist. This is a smaller, Postgres-backed
set. Destructive calls additionally need an explicit ?confirm= value.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from api.security import require_admin
from core import bus
from core.db import get_engine
from correlation.engine import incident_dict
from extract.cost import CostMeter

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)],
                   include_in_schema=False)


@router.get("/overview")
def overview():
    with get_engine().connect() as c:
        queue = dict(c.execute(text("SELECT status, count(*) FROM transcript_chunks GROUP BY status")).all())
        incidents = dict(c.execute(text("SELECT status, count(*) FROM incidents GROUP BY status")).all())
        events = [dict(r) for r in c.execute(text(
            "SELECT ts, kind, detail FROM pipeline_events ORDER BY ts DESC LIMIT 20")).mappings()]
        sources = dict(c.execute(text("""SELECT coalesce(extract_source, 'none'), count(*) FROM transcript_chunks
                                         WHERE extracted_at >= now() - interval '24 hours' GROUP BY 1""")).all())
    return {"queue": queue, "incidents": incidents, "extract_sources_24h": sources,
            "llm": CostMeter().state(), "recent_events": events}


@router.get("/llm/usage")
def llm_usage(days: int = Query(30, ge=1, le=366)):
    with get_engine().connect() as c:
        daily = [dict(r) for r in c.execute(text("""
            SELECT date_trunc('day', ts) AS day, workload, count(*) AS calls, sum(items) AS items,
                   sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
                   sum(cache_read_input_tokens) AS cache_read, sum(cache_creation_input_tokens) AS cache_write,
                   sum(cost_usd)::float AS cost_usd
              FROM llm_usage WHERE ts >= now() - make_interval(days => :d)
             GROUP BY 1, 2 ORDER BY 1 DESC, 2"""), {"d": days}).mappings()]
    return {"state": CostMeter().state(), "daily": daily}


@router.get("/transmissions/errors")
def errors(limit: int = Query(50, ge=1, le=500)):
    with get_engine().connect() as c:
        return [dict(r) for r in c.execute(text("""SELECT chunk_id::text, channel_id, captured_at, attempts, error
                                                    FROM transcript_chunks WHERE status = 'error'
                                                    ORDER BY captured_at DESC LIMIT :n"""), {"n": limit}).mappings()]


@router.post("/transmissions/{chunk_id}/requeue")
def requeue(chunk_id: str, stage: str = Query("extract", pattern="^(extract|transcribe)$")):
    """Re-run a transmission through extraction (or transcription when audio is still on disk)."""
    new = "transcribed" if stage == "extract" else "queued"
    with get_engine().begin() as c:
        n = c.execute(text("UPDATE transcript_chunks SET status = :s, attempts = 0, error = NULL "
                           "WHERE chunk_id = CAST(:id AS uuid)"), {"s": new, "id": chunk_id}).rowcount
    if not n:
        raise HTTPException(404, "no such transmission")
    bus.ring("extract" if stage == "extract" else "transcribe")
    return {"chunk_id": chunk_id, "status": new}


@router.post("/incidents/{incident_id}/resolve")
def resolve(incident_id: str):
    with get_engine().begin() as c:
        n = c.execute(text("UPDATE incidents SET status = 'RESOLVED', resolved_at = now() "
                           "WHERE incident_id = CAST(:id AS uuid) AND status = 'ACTIVE'"), {"id": incident_id}).rowcount
        inc = incident_dict(c, incident_id) if n else None
    if not n:
        raise HTTPException(404, "no such active incident")
    bus.publish_event("incident:resolve", inc)
    return inc


@router.delete("/incidents/{incident_id}")
def delete_incident(incident_id: str):
    with get_engine().begin() as c:
        n = c.execute(text("DELETE FROM incidents WHERE incident_id = CAST(:id AS uuid)"), {"id": incident_id}).rowcount
    if not n:
        raise HTTPException(404, "no such incident")
    bus.publish_event("incident:delete", {"incident_id": incident_id})
    return {"deleted": incident_id}


class MergeBody(BaseModel):
    source_id: str
    target_id: str


@router.post("/incidents/merge")
def merge(body: MergeBody):
    if body.source_id == body.target_id:
        raise HTTPException(400, "source and target are the same")
    with get_engine().begin() as c:
        src = c.execute(text("SELECT units, opened_at, last_updated FROM incidents WHERE incident_id = CAST(:id AS uuid)"),
                        {"id": body.source_id}).first()
        tgt = c.execute(text("SELECT 1 FROM incidents WHERE incident_id = CAST(:id AS uuid)"), {"id": body.target_id}).first()
        if src is None or tgt is None:
            raise HTTPException(404, "incident not found")
        c.execute(text("UPDATE transcript_chunks SET incident_id = CAST(:t AS uuid) WHERE incident_id = CAST(:s AS uuid)"),
                  {"t": body.target_id, "s": body.source_id})
        c.execute(text("""UPDATE incidents SET units = (SELECT array_agg(DISTINCT u) FROM unnest(units || CAST(:u AS text[])) u),
                                 opened_at = LEAST(opened_at, :o), last_updated = GREATEST(last_updated, :l)
                           WHERE incident_id = CAST(:t AS uuid)"""),
                  {"u": list(src[0] or []), "o": src[1], "l": src[2], "t": body.target_id})
        c.execute(text("DELETE FROM incidents WHERE incident_id = CAST(:s AS uuid)"), {"s": body.source_id})
        inc = incident_dict(c, body.target_id)
    bus.publish_event("incident:delete", {"incident_id": body.source_id})
    bus.publish_event("incident:update", inc)
    return inc


@router.delete("/reset/all")
def reset_all(confirm: str = Query(..., description="must be exactly RESET-LIVE-DATA")):
    """Wipe live incidents and transmissions (not llm_usage: the cost ledger is kept for the caps)."""
    if confirm != "RESET-LIVE-DATA":
        raise HTTPException(400, "pass ?confirm=RESET-LIVE-DATA")
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE rail_detector_events, transcript_chunks, incidents, rail_telemetry, geocode_cache "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("INSERT INTO pipeline_events (kind, detail) VALUES ('admin_reset', '{}'::jsonb)"))
    return {"reset": True}
