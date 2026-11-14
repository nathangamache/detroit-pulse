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

from api import rail_verdicts
from core.config_files import (RAIL_KINDS, RAIL_VOICE_KINDS, Channel, load_areas, load_channels, load_subdivisions,
                               norm_line_name)
from core.db import get_engine
from core.liveness import current, ingest_component, receiver_component, runs_since
from core.receivers import (RECEIVER_STATES, ReceiverView, channel_covers_point, channel_matches_hint, coverage_for,
                            coverage_summary, on_railroad, on_subdivision, own_places, reported_freqs)
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


def _channels_n(n: int) -> str:
    return f"{n} channel" + ("" if n == 1 else "s")


def _recordings_n(n: int) -> str:
    return f"{n} recording" + ("" if n == 1 else "s")


def _unread_note(unread: dict[str, int]) -> str | None:
    """The sentence for recordings in the window that have no text in `transcripts` (still
    queued or transcribing, failed, or whisper found no speech): traffic was heard, so this is
    never a quiet line."""
    parts = []
    if unread.get("pending"):
        parts.append(f"{_recordings_n(unread['pending'])} not transcribed yet")
    if unread.get("error"):
        parts.append(f"{_recordings_n(unread['error'])} whose transcription failed (error)")
    if unread.get("no_speech"):
        parts.append(f"{_recordings_n(unread['no_speech'])} with no speech to transcribe")
    if not parts:
        return None
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return (f"Recorded on this line in the window but not in `transcripts`: {listed}. "
            "That is radio traffic, not a quiet line.")


def _not_proven(n_on: int, n_all: int) -> str:
    """For channels whose receiver is online now but that aren't proven for the whole window."""
    which = "these channels" if n_on == n_all else f"{n_on} of {_channels_n(n_all)} on this line"
    return (f"The rail voice receiver is online now, but Pulse can't vouch for {which} across the whole window "
            "(see `verdicts.rail_voice.reasons`): no transcripts here means no proof, not a quiet line.")


def _voice_notes(chans: list[Channel], rstate: dict[str, dict], heard: bool,
                 unread: dict[str, int] | None = None, proven: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """`coverage.note` sentences for a point's enabled, non-example voice channels. Only a channel
    in `proven` (listened to for the whole window, receiver and ingest: api/rail_verdicts.py) can
    be called quiet, and only when nothing was recorded in the window: `heard` (any transcript)
    or `unread` (recordings with no text yet, by kind) means traffic. A channel that is merely
    `online` now is not proof. When the point is only partly listened to, the channels with no
    receiver (`not_installed`) or an `offline` one are counted as not covered."""
    if not chans:
        return ["No monitored rail voice channel for this location yet; add it to config/channels.local.yaml "
                "and config/rail_channels.local.yaml once a receiver covers it."]
    unread_note = _unread_note(unread or {})
    heard = heard or unread_note is not None
    by_state = {s: [c for c in chans if rstate[c.id]["state"] == s] for s in RECEIVER_STATES}
    on, off, missing = by_state["online"], by_state["offline"], by_state["not_installed"]
    last_off = max((rstate[c.id].get("last_seen_at") or "" for c in off), default="")
    notes, gap = [], None
    if not off and not missing:
        if not heard and all(c.id in proven for c in on):
            notes.append("The rail voice receiver is online and nothing was heard on these channels in the "
                         "window: they were quiet.")
        elif not heard:
            notes.append(_not_proven(len(on), len(chans)))
    elif not on and not off:
        notes.append("Rail voice channels for this line are configured, but no receiver has ever reported for them "
                     "(the SDR isn't installed or running yet): no transcripts here means no coverage, not a quiet "
                     "line.")
    elif not on and not missing:
        notes.append(f"The rail voice receiver for this line is offline (last heard {last_off}): no transcripts here "
                     "means no coverage, not a quiet line.")
    else:
        # a mix: say "quiet" of the online channels only, and name the gap
        quiet = [c for c in on if c.id in proven]
        if not on:
            notes.append("No rail voice receiver for this line is online.")
        elif not heard and quiet:
            one = len(quiet) == 1
            were = "that channel was" if one else "those channels were"
            notes.append(f"The rail voice receiver is online for {len(quiet)} of {_channels_n(len(chans))} on "
                         f"this line ({', '.join(c.label for c in quiet)}) and nothing was heard on "
                         f"{'it' if one else 'them'} in the window: {were} quiet.")
            if len(quiet) < len(on):
                notes.append(_not_proven(len(on) - len(quiet), len(chans)))
        elif not heard:
            notes.append(_not_proven(len(on), len(chans)))
        gaps = []
        if missing:
            gaps.append(f"{_channels_n(len(missing))} with no receiver (none has ever reported for "
                        f"{'it' if len(missing) == 1 else 'them'})")
        if off:
            gaps.append(f"{_channels_n(len(off))} whose receiver is offline (last heard {last_off})")
        gap = (f"Not covered: {' and '.join(gaps)}. No transcripts on "
               f"{'that channel' if len(missing) + len(off) == 1 else 'those channels'} means no coverage, "
               "not a quiet line.")
    return notes + [n for n in (unread_note, gap) if n]


def _line_label(railroad: str | None, name: str) -> str:
    return f"{railroad} {name}" if railroad else name


@router.get("/rail/activity")
def rail_activity_near(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
                       since_minutes: int = Query(30), subdivision: str | None = None, railroad: str | None = None):
    if not 1 <= since_minutes <= 720:
        raise _bad("since_minutes must be between 1 and 720")
    s = get_settings()
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=since_minutes)
    rr = (railroad or "").strip().upper()
    all_subs = load_subdivisions()
    # A railroad hint keeps that railroad's lines; when it names none, none is left (never all).
    rr_subs = [sd for sd in all_subs if not rr or sd.railroad.upper() == rr]
    # Lines whose monitored area (center+radius or bbox) contains the point name the answer. The
    # subdivision hint never adds a line the point is outside: it puts the line it names first.
    subs = [sd for sd in rr_subs if sd.near(lat, lon)]
    if subdivision:
        subs.sort(key=lambda sd: not sd.matches(subdivision, exact=True))
    chans = _channels()
    areas = load_areas()
    # The channels for this point are the ones whose own receiver area covers it (its bias point
    # or area with a radius, else a subdivision area it is on: channel_covers_point), never a
    # channel whose name matches and never by a fixed distance. Messages from any other channel
    # never count for this point.
    voice_cands = [c for c in chans.values() if c.kind in RAIL_VOICE_KINDS and on_railroad(c, railroad, rr_subs)]
    voice: dict[str, Channel] = {c.id: c for c in voice_cands if channel_covers_point(c, lat, lon, rr_subs, areas)}
    hinted: list[Channel] = []
    if subdivision:
        # The hint may use another dataset's name for the track (NARN vs FRA): aliases count, but
        # only exactly (normalised). It only decides the note for a named line that doesn't reach here.
        named = [sd for sd in rr_subs if sd.matches(subdivision, exact=True)]
        hinted = [c for c in voice_cands if channel_matches_hint(c, subdivision, railroad)
                  or any(on_subdivision(c, sd) for sd in named)]
    # EOT/HOT is any railroad's: no railroad filter, but the same coverage rule (its radius).
    eot_cands = [c for c in chans.values() if c.kind == "eot" and c.enabled]
    eot_chans = [c for c in eot_cands if channel_covers_point(c, lat, lon, all_subs, areas)]
    sub_name = subs[0].name if subs else subdivision
    rr_name = subs[0].railroad if subs else railroad
    ids = list(voice)
    real_voice_cands = [c for c in voice_cands if c.enabled and not c.is_example]
    real_eot_cands = [c for c in eot_cands if not c.is_example]
    cand_ids = sorted(set(ids) | {c.id for c in real_voice_cands})
    eot_ids = [c.id for c in eot_chans]
    receivers = sorted({c.receiver_name for c in real_voice_cands + real_eot_cands if c.receiver_name})
    components = ([receiver_component(n) for n in receivers]
                  + [ingest_component("sdr_files"), ingest_component("traintap")])
    with get_engine().connect() as c:
        last_sdr = dict(c.execute(text("""
            SELECT channel_id, max(coalesce(captured_at, "timestamp")) FROM transcript_chunks
             WHERE channel_id = ANY(CAST(:ids AS text[])) AND source = 'sdr_files'
               AND coalesce(captured_at, "timestamp") >= now() - interval '7 days'
             GROUP BY 1"""), {"ids": cand_ids}).all()) if cand_ids else {}
        eot_seen = dict(c.execute(text("""
            SELECT channel, max(ts) FROM rail_telemetry
             WHERE channel = ANY(CAST(:ids AS text[])) AND ts >= now() - interval '7 days' GROUP BY 1"""),
                                  {"ids": [x.id for x in eot_cands]}).all()) if eot_cands else {}
        tx = c.execute(text("""
            SELECT chunk_id, channel_id, feed_id, captured_at, "timestamp", raw_transcript, incident_id,
                   rail->>'reason_label' AS reason, priority, extract_source
              FROM transcript_chunks
             WHERE channel_id = ANY(CAST(:ids AS text[])) AND coalesce(captured_at, "timestamp") >= :since
               AND raw_transcript IS NOT NULL AND raw_transcript <> ''
             ORDER BY coalesce(captured_at, "timestamp") DESC LIMIT 100"""),
                       {"ids": ids, "since": since}).mappings().all() if ids else []
        # Every recording in the window, by channel: with text (transcripts) or without (still
        # queued or transcribing, failed, no speech). Both are traffic, never quiet.
        rec = c.execute(text("""
            SELECT channel_id,
                   CASE WHEN coalesce(raw_transcript, '') <> '' THEN 'text'
                        WHEN status = 'error' THEN 'error' WHEN status = 'done' THEN 'no_speech'
                        ELSE 'pending' END AS kind, count(*) AS n
              FROM transcript_chunks
             WHERE channel_id = ANY(CAST(:ids AS text[])) AND coalesce(captured_at, "timestamp") >= :since
             GROUP BY 1, 2"""), {"ids": ids, "since": since}).mappings().all() if ids else []
        # Rail-band recordings no configured channel claims: traffic Pulse can't place.
        unmapped = c.execute(text("""
            SELECT count(*) FROM transcript_chunks
             WHERE source = 'sdr_files' AND channel_id LIKE 'unmapped:%'
               AND coalesce(captured_at, "timestamp") >= :since
               AND (freq_hz IS NULL OR freq_hz BETWEEN 160100000 AND 161580000)"""), {"since": since}).scalar()
        # Recordings ingested in the window but stamped before it: a transmission across the window
        # start, or file names in another time zone than SDR_FILENAME_TZ (new traffic would then
        # land hours outside every window). Either way not a quiet window.
        skewed = c.execute(text("""
            SELECT count(*) FROM transcript_chunks
             WHERE channel_id = ANY(CAST(:ids AS text[])) AND source = 'sdr_files' AND created_at >= :since
               AND coalesce(captured_at, "timestamp") < :since"""), {"ids": ids, "since": since}).scalar() if ids else 0
        det_where = """ts >= :since AND (channel_id = ANY(CAST(:ids AS text[]))
                       OR (channel_id IS NULL AND lower(subdivision) = ANY(CAST(:subs AS text[]))))"""
        det_params = {"since": since, "ids": ids, "subs": _sub_names(subs)}
        det = c.execute(text(f"""
            SELECT ts, detector_name, railroad, subdivision, milepost, track, axle_count, speed_mph, temperature_f,
                   result, alarms, raw_text
              FROM rail_detector_events WHERE {det_where}
             ORDER BY ts DESC LIMIT 100"""), det_params).mappings().all()
        det_n = dict(c.execute(text(f"SELECT channel_id, count(*) FROM rail_detector_events WHERE {det_where} "
                                    "GROUP BY 1"), det_params).all())
        eot = c.execute(text("""
            SELECT ts, source, unit_id, brake_pipe_psig, motion, marker_light, batt_cond, arm_status, emergency, status
              FROM rail_telemetry
             WHERE ts >= :since AND source IN ('EOT', 'HOT') AND channel = ANY(CAST(:ids AS text[]))
             ORDER BY ts DESC LIMIT 100"""), {"since": since, "ids": eot_ids}).mappings().all() if eot_ids else []
        # every packet (DPU too) by channel, and packets no configured eot channel claims
        eot_n = dict(c.execute(text("""
            SELECT coalesce(channel, ''), count(*) FROM rail_telemetry WHERE ts >= :since GROUP BY 1"""),
                               {"since": since}).all())
        runs = runs_since(c, components, since)

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
    rv = ReceiverView(s, now)
    rstate = {cid: rv.status(chans[cid], last_sdr.get(cid)) for cid in cand_ids}
    rstate.update({ch.id: rv.status(ch, eot_seen.get(ch.id)) for ch in eot_cands})
    coverage_channels = [{"channel": c.label, "frequency_mhz": c.freq_mhz, "railroad": c.railroad,
                          "subdivision": c.subdivision, "kind": _contract_kind(c),
                          "receiver_state": rstate[c.id]["state"]}
                         for c in list(voice.values()) + eot_chans]
    coverage_channels = [{k: v for k, v in x.items() if v is not None} for x in coverage_channels]
    real_voice = [c for c in voice.values() if c.enabled and not c.is_example]
    real_eot = [c for c in eot_chans if not c.is_example]

    # ── verdicts (api/rail_verdicts.py): may this point be called quiet? ─────────────────────
    max_gap = s.liveness_max_gap_s

    def ingest_proof(source: str) -> tuple[rail_verdicts.IngestProof, dict]:
        cur = current(runs.get(ingest_component(source), []), now, max_gap)
        if cur is None:
            return rail_verdicts.IngestProof(source), {}
        if cur.fingerprint.startswith("blind:"):
            return rail_verdicts.IngestProof(source, blind=cur.fingerprint[len("blind:"):]), {}
        return rail_verdicts.IngestProof(source, up_since=cur.up_since), cur.detail

    def channel_proof(ch: Channel, covers: bool) -> rail_verdicts.ChannelProof:
        st = rstate[ch.id]
        hb = rv.heartbeat(ch.receiver_name)
        rruns = runs.get(receiver_component(ch.receiver_name), []) if ch.receiver_name else []
        cur = current(rruns, now, max_gap)
        # listening since the current run began, if that run listened to this channel and the
        # heartbeat file still agrees right now
        hb_ok = hb is not None and hb.covers(ch) and (now - hb.at).total_seconds() <= max_gap
        return rail_verdicts.ChannelProof(
            ch=ch, covers=covers, receiver_state=st["state"], last_seen_at=st.get("last_seen_at"),
            listening_since=cur.up_since if cur is not None and hb_ok and cur.covers(ch) else None,
            listened_in_window=st["state"] == "online" or any(r.covers(ch) for r in rruns),
            reported_freqs=reported_freqs(hb.pairs, ch) if hb is not None else [])

    def has_place(ch: Channel) -> bool:
        return bool(own_places(ch, areas)) or any(on_subdivision(ch, sd) and (sd.center or sd.bbox)
                                                  for sd in all_subs)

    sdr_ingest, sdr_detail = ingest_proof("sdr_files")
    eot_ingest, _ = ingest_proof("traintap")
    by_ch: dict[str, dict[str, int]] = {}
    for r in rec:
        by_ch.setdefault(r["channel_id"], {})[r["kind"]] = int(r["n"])
    # Files still being written at the ingest stage's last beat are recordings too.
    recording_now = {k: int(v) for k, v in (sdr_detail.get("recording_now") or {}).items() if k in voice}
    for cid, n in recording_now.items():
        by_ch.setdefault(cid, {})["pending"] = by_ch.get(cid, {}).get("pending", 0) + n
    unread: dict[str, int] = {}
    for kinds in by_ch.values():
        for k, n in kinds.items():
            if k != "text":
                unread[k] = unread.get(k, 0) + n
    real_ids = {c.id for c in real_voice}
    voice_counts = {"transcripts": sum(by_ch.get(cid, {}).get("text", 0) for cid in real_ids),
                    "detector_readouts": sum(int(det_n.get(cid, 0)) for cid in real_ids),
                    "recordings_without_text": sum(n for cid in real_ids for k, n in by_ch.get(cid, {}).items()
                                                   if k != "text")}
    voice_unattributed = []
    if unmapped:
        voice_unattributed.append(f"{_recordings_n(int(unmapped))} on rail frequencies in the window that no "
                                  "configured channel claims (unmapped)")
    if det_n.get(None):
        voice_unattributed.append(f"{int(det_n[None])} detector readout(s) in the window with no channel on record")
    if skewed:
        voice_unattributed.append(f"{_recordings_n(int(skewed))} ingested in the window but stamped before it "
                                  "(a transmission across the window start, or a file name time zone other than "
                                  "SDR_FILENAME_TZ)")
    voice_proofs = [channel_proof(c, c.id in voice) for c in real_voice_cands]
    v_voice = rail_verdicts.build("rail_voice", voice_proofs, voice_counts, sdr_ingest, since, now,
                                  voice_unattributed, no_place=[c for c in real_voice_cands if not has_place(c)])
    # the channels `coverage.note` may call quiet: listened to for the whole window, ingest too
    sdr_ok = not sdr_ingest.blind and sdr_ingest.up_since is not None and sdr_ingest.up_since <= since
    proven = {p.ch.id for p in voice_proofs if p.covers and p.listening_since is not None
              and p.listening_since <= since} if sdr_ok and not voice_unattributed else set()

    eot_known = {c.id for c in eot_cands}
    real_eot_ids = {c.id for c in real_eot}
    stray = sum(int(n) for ch_id, n in eot_n.items() if ch_id not in eot_known)
    eot_unattributed = ([f"{stray} EOT/HOT packet(s) in the window not attributed to a configured eot channel"]
                        if stray else [])
    v_eot = rail_verdicts.build("eot", [channel_proof(c, c.id in real_eot_ids) for c in real_eot_cands],
                                {"eot_packets": sum(int(eot_n.get(cid, 0)) for cid in real_eot_ids)},
                                eot_ingest, since, now, eot_unattributed,
                                no_place=[c for c in real_eot_cands if not has_place(c)])

    notes = _voice_notes(real_voice, rstate, heard=bool(transcripts), unread=unread, proven=proven)
    real_hinted = [c for c in hinted if c.enabled and not c.is_example]
    if real_hinted and not any(c.id in voice for c in real_hinted):
        sd = next((sd for sd in rr_subs if sd.matches(subdivision, exact=True)), None)
        c0 = real_hinted[0]
        line = _line_label(sd.railroad, sd.name) if sd else _line_label(c0.railroad, c0.subdivision or c0.label)
        notes.append(f"Pulse monitors the {line} line, but no receiver for it covers this point (it is outside "
                     "that line's monitored area): nothing from that line is reported here, and no transcripts "
                     "here means no coverage, not a quiet line.")
    estates = {rstate[c.id]["state"] for c in real_eot}
    if real_eot and "online" not in estates:
        notes.append("The EOT/HOT receiver is offline: an empty eot_hot list means no coverage." if "offline" in estates
                     else "No EOT/HOT receiver has reported yet: an empty eot_hot list means no coverage.")
    elif real_eot and v_eot["state"] == "unknown":
        notes.append("The EOT/HOT receiver is online now, but Pulse can't vouch for the whole window (see "
                     "`verdicts.eot.reasons`): an empty eot_hot list means no proof, not a quiet line.")
    elif not real_eot and real_eot_cands:
        if any(not has_place(c) for c in real_eot_cands):
            notes.append("No EOT/HOT receiver covers this point: an eot channel needs a receiver area on record "
                         "(a `bias` point with `coverage.radius_km`, or an `area_id`) before its telemetry is "
                         "attributed to any point.")
        else:
            notes.append("No EOT/HOT receiver covers this point (it is outside every eot channel's coverage "
                         "radius): an empty eot_hot list means no coverage.")
    covered = any(rstate[c.id]["state"] != "not_installed" for c in real_voice + real_eot)
    out = {"generated_at": now_iso(),
           "coverage": {"covered": covered, "channels": coverage_channels},
           "transcripts": transcripts, "detector_readouts": readouts, "eot_hot": eot_out,
           "reason_labels": reason_labels,
           "verdicts": {"rail_voice": v_voice, "eot": v_eot}}
    if any(unread.values()):
        out["coverage"]["recordings_without_text"] = {k: unread.get(k, 0) for k in ("pending", "error", "no_speech")}
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
