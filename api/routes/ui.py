"""Routes used by the bundled React UI (served under /pulse, API at /pulse/api via the gateway)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from core.config_files import coverage_bbox, load_channels
from core.db import get_engine

log = structlog.get_logger()
router = APIRouter(tags=["ui"])
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_INC = """i.incident_id, i.feed_id, i.channel_id, i.county, i.status, i.opened_at, i.last_updated, i.resolved_at,
          i.incident_type, i.priority, i.address_raw, i.address_full, i.city, i.lat, i.lng, i.units, i.units_cleared,
          i.summary, i.short_summary, i.precinct, i.battalion, i.nearest_stations, i.loc_kind, i.loc_street1,
          i.loc_street2, i.loc_number, i.loc_city, i.loc_raw, i.geocode_source, i.geocode_confidence,
          i.is_location_inferred, i.is_rail, i.rail,
          (SELECT count(*) FROM transcript_chunks t WHERE t.incident_id = i.incident_id) AS chunk_count"""


def _row(r) -> dict:
    d = dict(r)
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    if "incident_id" in d and d["incident_id"] is not None:
        d["incident_id"] = str(d["incident_id"])
    if "chunk_id" in d and d["chunk_id"] is not None:
        d["chunk_id"] = str(d["chunk_id"])
    return d


@router.get("/incidents/active")
def active_incidents(channel_id: str | None = None):
    sql = f"SELECT {_INC} FROM incidents i WHERE i.status = 'ACTIVE'"
    params = {}
    if channel_id:
        sql += " AND (i.channel_id = :ch OR i.feed_id = :ch)"
        params["ch"] = channel_id
    sql += " ORDER BY i.last_updated DESC LIMIT 500"
    with get_engine().connect() as c:
        return [_row(r) for r in c.execute(text(sql), params).mappings()]


@router.get("/incidents/stats/summary")
def incident_stats():
    with get_engine().connect() as c:
        active = c.execute(text("SELECT count(*) FROM incidents WHERE status = 'ACTIVE'")).scalar()
        day = c.execute(text("SELECT count(*) FROM incidents WHERE opened_at >= now() - interval '24 hours'")).scalar()
        by_type = dict(c.execute(text("""SELECT incident_type, count(*) FROM incidents
                                         WHERE opened_at >= now() - interval '24 hours' GROUP BY 1""")).all())
    return {"active_incidents": active, "incidents_24h": day, "by_type_24h": by_type}


@router.get("/incidents/{incident_id}")
def incident(incident_id: str):
    with get_engine().connect() as c:
        r = c.execute(text(f"SELECT {_INC} FROM incidents i WHERE i.incident_id = CAST(:id AS uuid)"),
                      {"id": incident_id}).mappings().first()
    if r is None:
        raise HTTPException(404, "incident not found")
    return _row(r)


@router.get("/incidents/{incident_id}/chunks")
def incident_chunks(incident_id: str):
    with get_engine().connect() as c:
        rows = [_row(r) for r in c.execute(text("""
            SELECT chunk_id, incident_id, channel_id, feed_id, captured_at, raw_transcript, short_summary,
                   incident_type, priority, units, loc_kind, loc_street1, loc_street2, loc_number, loc_city,
                   normalized_address, correlation_action, correlation_confidence, geocode_source,
                   geocode_confidence, extract_source, prefilter, whisper_model
              FROM transcript_chunks WHERE incident_id = CAST(:id AS uuid)
             ORDER BY captured_at"""), {"id": incident_id}).mappings()]
    return {"incident_id": incident_id, "chunk_count": len(rows), "chunks": rows}


@router.get("/transmissions/recent")
def recent_transmissions(limit: int = Query(50, ge=1, le=200), channel_id: str | None = None):
    sql = """SELECT chunk_id, channel_id, captured_at, status, raw_transcript, short_summary, incident_type,
                    priority, is_rail, prefilter, extract_source, incident_id
               FROM transcript_chunks WHERE raw_transcript IS NOT NULL"""
    params: dict = {"lim": limit}
    if channel_id:
        sql += " AND channel_id = :ch"
        params["ch"] = channel_id
    sql += " ORDER BY captured_at DESC NULLS LAST LIMIT :lim"
    with get_engine().connect() as c:
        return [_row(r) for r in c.execute(text(sql), params).mappings()]


@router.get("/channels")
def channels():
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    with get_engine().connect() as c:
        counts = dict(c.execute(text("SELECT channel_id, count(*) FROM transcript_chunks WHERE captured_at >= :s "
                                     "GROUP BY 1"), {"s": since}).all())
    return [{**ch.to_public(), "transmissions_last_hour": int(counts.get(ch.id, 0))} for ch in load_channels()]


@router.get("/rail/recent")
def rail_recent(minutes: int = Query(120, ge=1, le=1440)):
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with get_engine().connect() as c:
        det = [_row(r) for r in c.execute(text("""SELECT ts, channel_id, railroad, subdivision, milepost::float AS milepost,
                                                  track, axle_count, speed_mph, temperature_f, result, alarms
                                             FROM rail_detector_events WHERE ts >= :s ORDER BY ts DESC LIMIT 100"""),
                                       {"s": since}).mappings()]
        eot = [_row(r) for r in c.execute(text("""SELECT ts, source, unit_id, brake_pipe_psig, motion, status, emergency
                                             FROM rail_telemetry WHERE ts >= :s ORDER BY ts DESC LIMIT 100"""),
                                       {"s": since}).mappings()]
    return {"detectors": det, "eot_hot": eot}


@router.get("/coverage")
def coverage():
    bb = coverage_bbox()
    return {"bbox": {"south": bb.south, "west": bb.west, "north": bb.north, "east": bb.east}}


# ── Static reference layers (public Detroit open data bundled in api/data) ──────

@lru_cache(maxsize=8)
def _static(name: str) -> dict:
    path = DATA_DIR / name
    if not path.exists():
        return {"type": "FeatureCollection", "features": []}
    data = json.loads(path.read_text())
    for feat in data.get("features", []):
        p = feat.setdefault("properties", {})
        if name.startswith("DFD_Fire_Station"):
            p["name"] = p.pop("Firehouse", p.get("name", ""))
            p["address"] = p.pop("Address", p.get("address", ""))
            p["battalion"] = p.pop("Battalion", p.get("battalion", ""))
            for k in ("Lat", "Long", "FID"):
                p.pop(k, None)
        elif name.startswith("DPD_Precincts"):
            p["label"] = f"Precinct {p.get('name', '')}"
        elif name.startswith("DFD_Battalions"):
            p["label"] = p.get("Label_Name", p.get("Battalion", ""))
        else:
            p["label"] = p.get("name", "")
    return data


@router.get("/external/fire-stations")
def fire_stations():
    return _static("DFD_Fire_Station_Locations.geojson")


@router.get("/external/precincts")
def precincts():
    return _static("DPD_Precincts.geojson")


@router.get("/external/battalions")
def battalions():
    return _static("DFD_Battalions.geojson")


@router.get("/external/counties")
def counties():
    return _static("County_Boundaries.geojson")


_DTE = {"at": 0.0, "data": None}
DTE_URL = ("https://outagemap.serv.dteenergy.com/GISRest/services/OMP/OutageLocations/MapServer/2/query"
           "?where=1%3D1&outFields=*&f=geojson&resultRecordCount=500&geometryType=esriGeometryEnvelope"
           "&inSR=4326&spatialRel=esriSpatialRelIntersects")


@router.get("/external/dte-outages")
async def dte_outages():
    """DTE outage polygons, fetched server-side (the browser can't: CORS) and cached 15 minutes."""
    if _DTE["data"] is not None and time.time() - _DTE["at"] < 900:
        return _DTE["data"]
    bb = coverage_bbox()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(DTE_URL + f"&geometry={bb.west}%2C{bb.south}%2C{bb.east}%2C{bb.north}",
                                 headers={"User-Agent": "detroit-pulse/0.2 (self-hosted)"})
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.info("DTE outage fetch failed", error=str(e)[:200])
        return {"type": "FeatureCollection", "features": [], "_error": "unavailable"}
    _DTE.update(at=time.time(), data=data)
    return data
