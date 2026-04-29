import json
import os
import sys
import time
import uuid
import subprocess
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

STREAM_URL = "https://broadcastify.cdnstream1.com/43889"
CHUNK_SECS = 25
AUDIO_PATH = f"/tmp/detroit_pulse_test_{int(time.time())}.wav"
FEED_ID    = "wayneco_plymouthnorthville"
COUNTY     = "wayne"
CHUNK_ID   = str(uuid.uuid4())

def section(title):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")

def ok(msg):   print(f"  ✓  {msg}")
def warn(msg): print(f"  ⚠  {msg}")
def err(msg):  print(f"  ✗  {msg}")
def info(msg): print(f"     {msg}")

print("=" * 60)
print("  DETROIT PULSE — FULL PIPELINE TEST")
print("=" * 60)

from api.broadcaster import publish_debug, publish_incident_new, publish_incident_update, publish_unassociated

# ── Stage 1: Pull audio ───────────────────────────────────────────────────
section("STAGE 1 — Audio Ingest")
publish_debug("ingest", FEED_ID, status="pulling", stream_url=STREAM_URL)

cmd = [
    "ffmpeg", "-y", "-i", STREAM_URL,
    "-t", str(CHUNK_SECS),
    "-ar", "16000", "-ac", "1", "-f", "wav", AUDIO_PATH,
]
result = subprocess.run(cmd, capture_output=True, timeout=CHUNK_SECS + 30)

if not Path(AUDIO_PATH).exists() or Path(AUDIO_PATH).stat().st_size < 1000:
    err("Failed to pull audio chunk")
    sys.exit(1)

size_kb = Path(AUDIO_PATH).stat().st_size // 1024
ok(f"Audio chunk saved ({size_kb}KB, {CHUNK_SECS}s @ 16kHz mono)")
publish_debug("ingest", FEED_ID, status="complete", size_kb=size_kb)

# ── Stage 2: VAD ─────────────────────────────────────────────────────────
section("STAGE 2 — Voice Activity Detection")
from ingest.vad import has_speech, get_speech_ratio

ratio  = get_speech_ratio(AUDIO_PATH)
speech = has_speech(AUDIO_PATH)
ok(f"Speech ratio: {round(ratio * 100, 1)}%  |  Has speech: {speech}")
publish_debug("vad", FEED_ID, speech_ratio=round(ratio * 100, 1), has_speech=speech)

if not speech:
    warn("No speech detected — feed is silent right now")
    warn("Using synthetic transcript to test remaining stages")

# ── Stage 3: Whisper ─────────────────────────────────────────────────────
section("STAGE 3 — Whisper Transcription")
publish_debug("transcription", FEED_ID, status="starting")

from transcribe.whisper_worker import transcribe_chunk, load_vocab_hints
vocab = load_vocab_hints()
t0 = time.time()
transcript = transcribe_chunk(AUDIO_PATH, vocab)
elapsed = round(time.time() - t0, 1)
Path(AUDIO_PATH).unlink(missing_ok=True)

if not transcript:
    warn(f"Empty transcript ({elapsed}s) — using synthetic")
    transcript = "Units respond Plymouth Road and Forest, structure fire, Engine 4 en route code 3"
    source = "SYNTHETIC"
else:
    ok(f"Transcribed in {elapsed}s")
    source = "REAL AUDIO"

info(f"Transcript: \"{transcript}\"")
publish_debug("transcription", FEED_ID,
    status="complete", elapsed_s=elapsed,
    transcript=transcript, source=source)

# ── Stage 4: Address normalization ────────────────────────────────────────
section("STAGE 4 — Address Normalization (Qwen)")
publish_debug("normalization", FEED_ID, status="starting", transcript=transcript)

from llm.normalize import normalize_address
t0 = time.time()
normalized = normalize_address(transcript, feed_id=FEED_ID)
elapsed = round(time.time() - t0, 1)

ok(f"Normalized in {elapsed}s")
info(f"Raw:        \"{transcript[:70]}\"")
info(f"Normalized: \"{normalized}\"")
publish_debug("normalization", FEED_ID,
    status="complete", elapsed_s=elapsed, normalized=normalized)

# ── Stage 5: Geocoding ────────────────────────────────────────────────────
section("STAGE 5 — Geocoding")
publish_debug("geocoding", FEED_ID, status="starting", address=normalized)

from geocode.geocoder import geocode
t0 = time.time()
geo = geocode(normalized)
elapsed = round(time.time() - t0, 1)

if geo["confidence"] == "FAILED":
    warn(f"Geocoding failed ({elapsed}s) — no coordinates")
else:
    ok(f"Geocoded in {elapsed}s")
    info(f"Address:    {geo.get('formatted')}")
    info(f"Coords:     {geo.get('lat')}, {geo.get('lng')}")
    info(f"Confidence: {geo.get('confidence')}  |  Source: {geo.get('source')}")

publish_debug("geocoding", FEED_ID,
    status="complete", elapsed_s=elapsed,
    lat=geo.get("lat"), lng=geo.get("lng"),
    confidence=geo.get("confidence"), source=geo.get("source"))

# ── Stage 5b: LLM Structuring ─────────────────────────────────────────────
section("STAGE 5b — LLM Structuring (Qwen)")
publish_debug("structuring", FEED_ID, status="starting")

from llm.structure import structure_transcript
from correlation.incident_store import get_all_active

active = get_all_active(feed_id=FEED_ID)
info(f"Active incidents in context: {len(active)}")

t0 = time.time()
structured = structure_transcript(
    transcript=transcript,
    normalized_address=normalized,
    geocoded_address=geo.get("formatted") or normalized,
    lat=geo.get("lat"),
    lng=geo.get("lng"),
    county=COUNTY,
    feed_id=FEED_ID,
    active_incidents=active,
)
elapsed = round(time.time() - t0, 1)

ok(f"Structured in {elapsed}s")
info(f"has_incident:        {structured.get('has_incident')}")
info(f"correlation_action:  {structured.get('correlation_action')}")
info(f"incident_type:       {structured.get('incident_type')}")
info(f"priority:            {structured.get('priority')}")
info(f"units_added:         {structured.get('units_added')}")
info(f"units_cleared:       {structured.get('units_cleared')}")
info(f"summary:             {structured.get('summary_update')}")

publish_debug("structuring", FEED_ID,
    status="complete", elapsed_s=elapsed,
    has_incident=structured.get("has_incident"),
    incident_type=structured.get("incident_type"),
    correlation_action=structured.get("correlation_action"),
    summary=structured.get("summary_update"))

# ── Stage 6: Correlation ──────────────────────────────────────────────────
section("STAGE 6 — Event Correlation Engine")
publish_debug("correlation", FEED_ID, status="starting")

from correlation.engine import correlate
t0 = time.time()
correlation = correlate(
    chunk_id=CHUNK_ID,
    feed_id=FEED_ID,
    county=COUNTY,
    transcript=transcript,
    structured=structured,
    normalized_address=normalized,
    geocode_result=geo,
)
elapsed = round(time.time() - t0, 1)

ok(f"Correlated in {elapsed}s")
info(f"Action:      {correlation['action']}")
info(f"Incident ID: {correlation['incident_id']}")

if correlation["incident"]:
    inc = correlation["incident"]
    info(f"Type:        {inc.get('incident_type')}")
    info(f"Priority:    {inc.get('priority')}")
    info(f"Units:       {inc.get('units')}")
    info(f"Location:    {inc.get('address_full')}")
    info(f"Coords:      {inc.get('lat')}, {inc.get('lng')}")
    info(f"Summary:     {inc.get('summary')}")

publish_debug("correlation", FEED_ID,
    status="complete", elapsed_s=elapsed,
    action=correlation["action"],
    incident_id=correlation["incident_id"])

# ── DB Write + WebSocket broadcast ────────────────────────────────────────
if correlation["action"] in ("NEW", "UPDATE", "RESOLVE") and correlation["incident"]:
    section("STAGE 7 — Database Write + Live Map Broadcast")

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from db.models import Incident, TranscriptChunk

    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://detroit:detroit@localhost:5432/detroitpulse")
    engine  = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    db      = Session()
    inc     = correlation["incident"]

    try:
        if correlation["action"] == "NEW":
            db_incident = Incident(
                incident_id=inc["incident_id"],
                feed_id=inc["feed_id"],
                county=inc["county"],
                status=inc["status"],
                incident_type=inc["incident_type"],
                priority=inc["priority"],
                address_raw=inc.get("address_raw"),
                address_full=inc.get("address_full"),
                city=inc.get("city"),
                lat=inc.get("lat"),
                lng=inc.get("lng"),
                units=inc.get("units", []),
                units_cleared=inc.get("units_cleared", []),
                summary=inc.get("summary"),
            )
            db.add(db_incident)

        chunk = TranscriptChunk(
            chunk_id=CHUNK_ID,
            incident_id=inc["incident_id"],
            feed_id=FEED_ID,
            raw_transcript=transcript,
            normalized_address=normalized,
            correlation_action=correlation["action"],
            correlation_method="UNIT_ID_MATCH" if structured.get("units_added") else "LOCATION",
            correlation_confidence=geo.get("confidence"),
            geocode_source=geo.get("source"),
            geocode_confidence=geo.get("confidence"),
            whisper_model="large-v3",
            lora_version="base-prompted",
        )
        db.add(chunk)
        db.commit()

        count = db.execute(text("SELECT COUNT(*) FROM incidents")).scalar()
        ok(f"Written to database  (total incidents: {count})")

        # Build a serializable incident dict for the frontend
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
            ok("Broadcast incident:new to live map ✓")
        elif correlation["action"] == "UPDATE":
            publish_incident_update(frontend_incident)
            ok("Broadcast incident:update to live map ✓")
        elif correlation["action"] == "RESOLVE":
            publish_incident_resolve(frontend_incident)
            ok("Broadcast incident:resolve to live map ✓")

    except Exception as e:
        db.rollback()
        err(f"Database write failed: {e}")
        import traceback; traceback.print_exc()
    finally:
        db.close()

elif correlation["action"] == "UNASSOCIATED":
    section("STAGE 7 — Unassociated Chunk")
    warn("Chunk could not be correlated to any incident")
    publish_unassociated(CHUNK_ID, FEED_ID, transcript)
    ok("Published to unassociated queue")

# ── Add debug panel to frontend ───────────────────────────────────────────
print("\n" + "=" * 60)
print("  PIPELINE TEST COMPLETE")
print("=" * 60)
info(f"Audio source:  {'REAL' if speech else 'SYNTHETIC'}")
info(f"Transcript:    \"{transcript[:60]}...\"" if len(transcript) > 60 else f"Transcript: \"{transcript}\"")
info(f"Normalized:    {normalized}")
info(f"Geocoded:      {geo.get('confidence')} via {geo.get('source', 'none')}")
info(f"has_incident:  {structured.get('has_incident')}")
info(f"Action:        {correlation['action']}")
if correlation.get("incident_id"):
    info(f"Incident ID:   {correlation['incident_id']}")
print()
