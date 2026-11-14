"""The gateway's Pulse API contract, v1 (godseye-detroit-connector docs/pulse-api-contract.md).

Every free-text field that came from radio audio is named *_untrusted. Errors use
{"error": {"code": "bad_request|not_found|unavailable|internal|unauthorized", "message"}}.
Times are UTC ISO 8601, distances metres, coordinates WGS84.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from core.config_files import (RAIL_KINDS, RAIL_VOICE_KINDS, Channel, haversine_km, load_areas, load_channels,
                               load_subdivisions, norm_line_name)
from core.db import get_engine
from core.receivers import (ReceiverView, channel_matches_hint, coverage_for, coverage_summary,
                            on_subdivision)
from core.settings import get_settings

router = APIRouter(prefix="/v1", tags=["v1"])

# internal reason labels (extraction schema) -> contract ReasonLabel
_REASON_MAP = {"signal_problem": "signal_stop", "defect_detector": "defect_detector_stop",
               "crew_change_hos": "crew_change", "mechanical": "mechanical", "crossing_incident": "blocked_crossing",
               "switching_yard": "switching", "unknown": "unknown"}
_MEET = re.compile(r"\bmeet\b|\bpass(?:ing)?\b|opposing|for\s+the\s+(?:east|west|north|south)bound|take\s+the\s+siding", re.I)
_TRACK_WORK = re.compile(r"track\s+(?:and\s+)?time|form\s+b|work\s+limits|foreman|maintenance\s+of\s+way|\bmow\b|"
                         r"hi[-\s]?rail|track\s+gang|slow\s+order", re.I)


def contract_reason(label: str | None, text_: str, priority: str | None) -> str:
    if label is None:
        return "unknown"
    if _TRACK_WORK.search(text_ or ""):
        return "track_work"
    if label == "meet_or_hold":
        return "meet_pass" if _MEET.search(text_ or "") else "dispatcher_hold"
    if label == "trespasser":
        return "emergency" if priority == "HIGH" else "unknown"
    return _REASON_MAP.get(label, "unknown")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso(v) -> str | None:
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bad(msg: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": "bad_request", "message": msg})


def _type(t: str | None) -> str:
    t = (t or "UNKNOWN").upper()
    if t == "TRAFFIC_ACCIDENT":
        return "crash"
    if t.startswith("RAIL"):
        return "rail"
    return t.lower()


def _channels() -> dict[str, Channel]:
    return {c.id: c for c in load_channels()}


def incident_out(r: dict, distance_m: float | None = None) -> dict:
    conf = (r.get("geocode_confidence") or "").lower()
    out = {
        "incident_id": str(r["incident_id"]),
        "type": _type(r.get("incident_type")),
        "status": "resolved" if r.get("status") == "RESOLVED" else "active",
        "opened_at": _iso(r.get("opened_at")),
        "last_updated": _iso(r.get("last_updated")),
        "feed_id": r.get("channel_id") or r.get("feed_id"),
        "chunk_count": int(r.get("chunk_count") or 0),
        "units": list(r.get("units") or []),
    }
    if (r.get("priority") or "UNKNOWN") != "UNKNOWN":
        out["priority"] = r["priority"].lower()
    addr = r.get("address_full") or r.get("address_raw")
    if addr:
        out["address"] = addr
    if r.get("lat") is not None and r.get("lng") is not None:
        out["lat"], out["lon"] = float(r["lat"]), float(r["lng"])
    if conf in ("high", "medium", "low"):
        out["location_confidence"] = conf
    if distance_m is not None:
        out["distance_m"] = round(float(distance_m), 1)
    summary = r.get("summary") or r.get("short_summary")
    if summary:
        out["summary_untrusted"] = summary
    return out


def transcript_out(r: dict, chans: dict[str, Channel]) -> dict:
    cid = r.get("channel_id") or r.get("feed_id")
    out = {"chunk_id": str(r["chunk_id"]), "ts": _iso(r.get("captured_at") or r.get("timestamp")),
           "feed_id": cid, "text_untrusted": r.get("raw_transcript") or ""}
    if cid in chans:
        out["channel"] = chans[cid].label
    if r.get("incident_id"):
        out["incident_id"] = str(r["incident_id"])
    return out


_INC_COLS = """i.incident_id, i.feed_id, i.channel_id, i.status, i.opened_at, i.last_updated, i.incident_type,
               i.priority, i.address_full, i.address_raw, i.lat, i.lng, i.geocode_confidence, i.units,
               i.summary, i.short_summary,
               (SELECT count(*) FROM transcript_chunks t WHERE t.incident_id = i.incident_id) AS chunk_count"""


@router.get("/health")
def health():
    try:
        v = version("detroit-pulse")
    except PackageNotFoundError:
        v = "0.2.0"
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"ok": db_ok, "version": v, "generated_at": now_iso()}


@router.get("/incidents/near")
def incidents_near(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
                   radius_km: float = Query(3.0), since_minutes: int = Query(60), limit: int = Query(25)):
    if not 0.1 <= radius_km <= 50:
        raise _bad("radius_km must be between 0.1 and 50")
    if not 1 <= since_minutes <= 1440:
        raise _bad("since_minutes must be between 1 and 1440")
    if not 1 <= limit <= 100:
        raise _bad("limit must be between 1 and 100")
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    with get_engine().connect() as c:
        rows = c.execute(text(f"""
            WITH p AS (SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography AS g)
            SELECT {_INC_COLS}, ST_Distance(i.location::geography, p.g) AS distance_m
              FROM incidents i, p
             WHERE i.location IS NOT NULL
               AND ST_DWithin(i.location::geography, p.g, :radius_m)
               AND i.last_updated >= :since
             ORDER BY distance_m ASC LIMIT :lim"""),
                         {"lat": lat, "lon": lon, "radius_m": radius_km * 1000, "since": since, "lim": limit}).mappings().all()
    return {"generated_at": now_iso(), "center": {"lat": lat, "lon": lon}, "radius_km": radius_km,
            "since_minutes": since_minutes, "incidents": [incident_out(dict(r), r["distance_m"]) for r in rows]}


@router.get("/incidents/{incident_id}")
def incident_detail(incident_id: str):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", incident_id):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "no such incident"})
    with get_engine().connect() as c:
        r = c.execute(text(f"SELECT {_INC_COLS} FROM incidents i WHERE i.incident_id = :id"),
                      {"id": incident_id}).mappings().first()
        if r is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "no such incident"})
        tx = c.execute(text("""SELECT chunk_id, channel_id, feed_id, captured_at, "timestamp", raw_transcript, incident_id
                                 FROM transcript_chunks WHERE incident_id = :id
                                 ORDER BY coalesce(captured_at, "timestamp") ASC LIMIT 100"""),
                       {"id": incident_id}).mappings().all()
    chans = _channels()
    return {"generated_at": now_iso(), "incident": incident_out(dict(r)),
            "transcripts": [transcript_out(dict(t), chans) for t in tx]}


def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/transcripts/search")
def search_transcripts(q: str = Query(...), since_minutes: int = Query(60), channel: str | None = None,
                       limit: int = Query(25)):
    q = q.strip()
    if not 1 <= len(q) <= 200:
        raise _bad("q must be 1-200 characters")
    if not 1 <= since_minutes <= 1440:
        raise _bad("since_minutes must be between 1 and 1440")
    if not 1 <= limit <= 100:
        raise _bad("limit must be between 1 and 100")
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    params = {"q": q, "like": f"%{_like_escape(q)}%", "since": since, "lim": limit, "ch": channel}
    chan_sql = "AND (channel_id = :ch OR feed_id = :ch)" if channel else ""
    with get_engine().connect() as c:
        rows = c.execute(text(f"""
            SELECT chunk_id, channel_id, feed_id, captured_at, "timestamp", raw_transcript, incident_id
              FROM transcript_chunks
             WHERE coalesce(captured_at, "timestamp") >= :since {chan_sql}
               AND raw_transcript IS NOT NULL
               AND (tsv @@ websearch_to_tsquery('english', :q) OR raw_transcript ILIKE :like)
             ORDER BY coalesce(captured_at, "timestamp") DESC LIMIT :lim"""), params).mappings().all()
    chans = _channels()
    return {"generated_at": now_iso(), "query": q, "results": [transcript_out(dict(r), chans) for r in rows]}


def _sub_names(subs: list) -> list[str]:
    """Lower-cased names, aliases and their normalised forms, to match detector readouts."""
    out: set[str] = set()
    for sd in subs:
        for n in sd.names():
            out.add(n.lower())
            if norm_line_name(n):
                out.add(norm_line_name(n))
    return sorted(out)


def _contract_kind(ch: Channel) -> str:
    return {"rail_dispatcher": "dispatcher", "rail_detector": "detector", "eot": "eot"}.get(ch.kind, "road")


@router.get("/rail/activity")
def rail_activity_near(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
                       since_minutes: int = Query(30), subdivision: str | None = None, railroad: str | None = None):
    if not 1 <= since_minutes <= 720:
        raise _bad("since_minutes must be between 1 and 720")
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    all_subs = load_subdivisions()
    subs = [sd for sd in all_subs if sd.near(lat, lon)]
    if subdivision:
        # the hint may use another dataset's name for the track (NARN vs FRA): aliases count
        subs += [sd for sd in all_subs if sd.matches(subdivision) and sd not in subs]
    if railroad:
        subs = [sd for sd in subs if sd.railroad.upper() == railroad.upper()] or subs
    chans = _channels()
    voice: dict[str, Channel] = {}
    for sd in subs:
        for rc in sd.channels:
            if rc.channel_id and rc.channel_id in chans and chans[rc.channel_id].kind != "eot":
                voice[rc.channel_id] = chans[rc.channel_id]
        for c in chans.values():
            if c.kind in RAIL_VOICE_KINDS and on_subdivision(c, sd):
                voice[c.id] = c
    if subdivision and not subs:
        for c in chans.values():
            if c.kind in RAIL_VOICE_KINDS and channel_matches_hint(c, subdivision):
                voice[c.id] = c
    eot_chans = [c for c in chans.values() if c.kind == "eot" and c.enabled and
                 (c.bias_lat is None or haversine_km(c.bias_lat, c.bias_lon, lat, lon) <= 25)]
    sub_name = subs[0].name if subs else subdivision
    rr_name = subs[0].railroad if subs else railroad
    ids = list(voice)
    with get_engine().connect() as c:
        last_sdr = dict(c.execute(text("""
            SELECT channel_id, max(coalesce(captured_at, "timestamp")) FROM transcript_chunks
             WHERE channel_id = ANY(CAST(:ids AS text[])) AND source = 'sdr_files'
               AND coalesce(captured_at, "timestamp") >= now() - interval '7 days'
             GROUP BY 1"""), {"ids": ids}).all()) if ids else {}
        eot_seen = c.execute(text("SELECT max(ts) FROM rail_telemetry")).scalar() if eot_chans else None
        tx = c.execute(text("""
            SELECT chunk_id, channel_id, feed_id, captured_at, "timestamp", raw_transcript, incident_id,
                   rail->>'reason_label' AS reason, priority, extract_source
              FROM transcript_chunks
             WHERE channel_id = ANY(CAST(:ids AS text[])) AND coalesce(captured_at, "timestamp") >= :since
               AND raw_transcript IS NOT NULL AND raw_transcript <> ''
             ORDER BY coalesce(captured_at, "timestamp") DESC LIMIT 100"""),
                       {"ids": ids, "since": since}).mappings().all() if ids else []
        det = c.execute(text("""
            SELECT ts, detector_name, railroad, subdivision, milepost, track, axle_count, speed_mph, temperature_f,
                   result, alarms, raw_text
              FROM rail_detector_events
             WHERE ts >= :since AND (channel_id = ANY(CAST(:ids AS text[])) OR lower(subdivision) = ANY(CAST(:subs AS text[])))
             ORDER BY ts DESC LIMIT 100"""),
                        {"since": since, "ids": ids, "subs": _sub_names(subs)}).mappings().all()
        eot = c.execute(text("""
            SELECT ts, source, unit_id, brake_pipe_psig, motion, marker_light, batt_cond, arm_status, emergency, status
              FROM rail_telemetry WHERE ts >= :since AND source IN ('EOT', 'HOT')
             ORDER BY ts DESC LIMIT 100"""), {"since": since}).mappings().all() if eot_chans else []

    transcripts, weights = [], {}
    for r in tx:
        t = transcript_out(dict(r), chans)
        if r["reason"]:
            label = contract_reason(r["reason"], r["raw_transcript"], r["priority"])
            t["reason_label"] = label
            w = 1.0 if r["extract_source"] == "claude" else 0.5
            agg = weights.setdefault(label, {"w": 0.0, "ids": []})
            agg["w"] += w
            agg["ids"].append(str(r["chunk_id"]))
        transcripts.append(t)
    readouts = []
    for d in det:
        alarms = d["alarms"] or []
        defects = [a.get("type") if isinstance(a, dict) else str(a) for a in alarms]
        if d["result"] == "integrity_failure":
            defects = defects or ["integrity_failure"]
        ro = {"ts": _iso(d["ts"]), "detector": d["detector_name"] or "detector", "railroad": d["railroad"],
              "subdivision": d["subdivision"], "milepost": None if d["milepost"] is None else f"{float(d['milepost']):g}",
              "track": d["track"], "axles": d["axle_count"], "speed_mph": d["speed_mph"],
              "temperature_f": d["temperature_f"], "defects": defects, "raw_untrusted": d["raw_text"] or ""}
        readouts.append({k: v for k, v in ro.items() if v is not None})
        if defects:
            agg = weights.setdefault("defect_detector_stop", {"w": 0.0, "ids": []})
            agg["w"] += 0.5
    eot_out = []
    battery = {"OK": "ok", "Low": "weak", "Very Low": "low"}
    emergency_seen = False
    for e in eot:
        row = {"ts": _iso(e["ts"]), "device": e["source"], "unit_id": str(e["unit_id"]) if e["unit_id"] is not None else "",
               "brake_pipe_psi": e["brake_pipe_psig"], "motion": e["motion"], "marker_light": e["marker_light"],
               "battery": battery.get(e["batt_cond"] or ""), "armed": (e["arm_status"] == "Armed") if e["arm_status"] else None,
               "status": e["status"], "emergency": e["emergency"]}
        eot_out.append({k: v for k, v in row.items() if v is not None})
        emergency_seen = emergency_seen or bool(e["emergency"])
    if emergency_seen:
        weights.setdefault("emergency", {"w": 0.0, "ids": []})["w"] += 2.0
    total = sum(v["w"] for v in weights.values()) or 1.0
    reason_labels = sorted(({"label": k, "confidence": round(min(1.0, v["w"] / total), 2), "from_chunk_ids": v["ids"][:20]}
                            for k, v in weights.items()), key=lambda x: -x["confidence"])

    # Coverage means a receiver has reported for one of these channels (heartbeat or output), not
    # just that the channel is configured: otherwise "no transcripts" can't be told from "quiet".
    rv = ReceiverView()
    rstate = {cid: rv.status(ch, last_sdr.get(cid)) for cid, ch in voice.items()}
    rstate.update({ch.id: rv.status(ch, eot_seen) for ch in eot_chans})
    coverage_channels = [{"channel": c.label, "frequency_mhz": c.freq_mhz, "railroad": c.railroad,
                          "subdivision": c.subdivision, "kind": _contract_kind(c),
                          "receiver_state": rstate[c.id]["state"]}
                         for c in list(voice.values()) + eot_chans]
    coverage_channels = [{k: v for k, v in x.items() if v is not None} for x in coverage_channels]
    real_voice = [c for c in voice.values() if c.enabled and not c.is_example]
    real_eot = [c for c in eot_chans if not c.is_example]
    notes = []
    vstates = {rstate[c.id]["state"] for c in real_voice}
    if not real_voice:
        notes.append("No monitored rail voice channel for this location yet; add it to config/channels.local.yaml "
                     "and config/rail_channels.local.yaml once a receiver covers it.")
    elif "online" in vstates:
        if not transcripts:
            notes.append("The rail voice receiver is online and nothing was heard on these channels in the window: "
                         "they were quiet.")
    elif "offline" in vstates:
        last = max(rstate[c.id].get("last_seen_at") or "" for c in real_voice)
        notes.append(f"The rail voice receiver for this line is offline (last heard {last}): no transcripts here "
                     "means no coverage, not a quiet line.")
    else:
        notes.append("Rail voice channels for this line are configured, but no receiver has ever reported for them "
                     "(the SDR isn't installed or running yet): no transcripts here means no coverage, not a quiet line.")
    estates = {rstate[c.id]["state"] for c in real_eot}
    if real_eot and "online" not in estates:
        notes.append("The EOT/HOT receiver is offline: an empty eot_hot list means no coverage." if "offline" in estates
                     else "No EOT/HOT receiver has reported yet: an empty eot_hot list means no coverage.")
    if eot_chans and any(c.bias_lat is None for c in eot_chans):
        notes.append("EOT/HOT receiver location not configured; telemetry shown unfiltered.")
    covered = any(rstate[c.id]["state"] != "not_installed" for c in real_voice + real_eot)
    out = {"generated_at": now_iso(),
           "coverage": {"covered": covered, "channels": coverage_channels},
           "transcripts": transcripts, "detector_readouts": readouts, "eot_hot": eot_out,
           "reason_labels": reason_labels}
    if notes:
        out["coverage"]["note"] = " ".join(notes)
    if sub_name:
        out["subdivision"] = sub_name
    if rr_name:
        out["railroad"] = rr_name
    return out


@router.get("/feeds/status")
def feeds_status():
    """Feeds plus, per feed, `receiver` (is anything listening?), `coverage` (what it hears) and
    `example`, and a top-level `coverage_summary` (docs/pulse-api.md#feeds_status)."""
    s = get_settings()
    now = datetime.now(timezone.utc)
    with get_engine().connect() as c:
        stats = {r["cid"]: dict(r) for r in c.execute(text("""
            SELECT coalesce(channel_id, feed_id) AS cid, max(coalesce(captured_at, "timestamp")) AS last_at,
                   count(*) FILTER (WHERE coalesce(captured_at, "timestamp") >= now() - interval '1 hour') AS last_hour,
                   count(*) FILTER (WHERE status IN ('queued', 'transcribing', 'transcribed', 'extracting')) AS backlog,
                   max(source) AS source,
                   max(coalesce(captured_at, "timestamp")) FILTER (WHERE source = 'sdr_files') AS last_sdr_at
              FROM transcript_chunks
             WHERE coalesce(captured_at, "timestamp") >= now() - interval '7 days' OR status <> 'done'
             GROUP BY 1""")).mappings()}
        backlog = dict(c.execute(text("""SELECT status, count(*) FROM transcript_chunks
                                         WHERE status IN ('queued','transcribing','transcribed','extracting','error')
                                         GROUP BY status""")).all())
        oldest = c.execute(text("""SELECT EXTRACT(EPOCH FROM now() - min(created_at)) FROM transcript_chunks
                                   WHERE status IN ('queued','transcribing','transcribed','extracting')""")).scalar()
        eot_last = c.execute(text("SELECT max(ts), count(*) FILTER (WHERE ts >= now() - interval '1 hour') "
                                  "FROM rail_telemetry")).first()
    feeds, summary_items = [], []
    chans = load_channels()
    rv = ReceiverView(s, now)
    subs, areas = load_subdivisions(), load_areas()
    seen = set()
    for ch in chans:
        seen.add(ch.id)
        st = stats.get(ch.id, {})
        if ch.kind == "eot":
            last_at, last_hour = eot_last[0], int(eot_last[1] or 0)
            hb = rv.heartbeat_age_s(ch.receiver_name or "traintap-eot")
            state = "down" if hb is None else ("up" if hb < 180 else ("degraded" if hb < 900 else "down"))
            source = "sdr:traintap"
            evidence = eot_last[0]
        else:
            last_at, last_hour = st.get("last_at"), int(st.get("last_hour") or 0)
            state = _state_from_last(last_at, now)
            source = "sdr:" + ("rtl_airband" if (st.get("source") or "sdr_files") == "sdr_files" else st.get("source"))
            evidence = st.get("last_sdr_at")
        receiver = rv.status(ch, evidence)
        summary_items.append((ch, receiver["state"]))
        f = {"feed_id": ch.id, "name": ch.label, "category": "rail" if ch.kind in RAIL_KINDS else "public_safety",
             "county": ch.county, "enabled": ch.enabled, "state": state if ch.enabled else "down",
             "last_chunk_at": _iso(last_at), "chunks_last_hour": last_hour, "source": source,
             "backlog": int(st.get("backlog") or 0), "example": ch.is_example,
             "receiver": receiver, "coverage": coverage_for(ch, subs, areas)}
        feeds.append({k: v for k, v in f.items() if v is not None})
    for cid, st in stats.items():
        if cid in seen or not cid:
            continue
        feeds.append({"feed_id": cid, "name": cid, "category": "public_safety", "enabled": True,
                      "state": _state_from_last(st.get("last_at"), now), "last_chunk_at": _iso(st.get("last_at")),
                      "chunks_last_hour": int(st.get("last_hour") or 0), "source": f"sdr:{st.get('source') or 'unknown'}",
                      "backlog": int(st.get("backlog") or 0), "unmapped": True, "example": False,
                      "receiver": rv.status(None, st.get("last_sdr_at")), "coverage": {}})
    return {"generated_at": now_iso(), "feeds": feeds,
            "coverage_summary": coverage_summary(summary_items),
            "backlog": {"queued": int(backlog.get("queued", 0)), "transcribing": int(backlog.get("transcribing", 0)),
                        "transcribed": int(backlog.get("transcribed", 0)), "extracting": int(backlog.get("extracting", 0)),
                        "error": int(backlog.get("error", 0)),
                        "oldest_waiting_s": None if oldest is None else round(float(oldest), 1)}}


def _state_from_last(last_at, now) -> str:
    if last_at is None:
        return "down"
    age = (now - last_at).total_seconds()
    return "up" if age <= 2 * 3600 else ("degraded" if age <= 24 * 3600 else "down")
