"""ORM models. The DDL itself lives in db/migrations (alembic) and db/schema.sql.

Table roles:
  transcript_chunks   one row per transmission; also the durable work queue (status)
  incidents           correlated incidents (Postgres is the single source of truth)
  rail_telemetry      decoded EOT/HOT/DPU packets from traintap
  rail_detector_events  parsed wayside defect-detector announcements
  llm_usage           one row per Claude API response (cost meter)
  geocode_cache       results from the $0 geocoders, with the provider that answered
  pipeline_events     cap hits, degradations, source health
  liveness            receiver and ingest heartbeat runs (core/liveness.py), for rail verdicts
  receiver_squelch    squelch openings per receiver stats sample and channel, for rail verdicts
Legacy tables kept for the archive: shadow_log, pipeline_metrics, lora_model_registry.
"""
from __future__ import annotations

import uuid

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer,
    Numeric, String, Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


def _iso(v):
    return v.isoformat() if v is not None else None


# Transmission lifecycle (transcript_chunks.status)
ST_QUEUED = "queued"              # audio known, waiting for whisper
ST_TRANSCRIBING = "transcribing"
ST_TRANSCRIBED = "transcribed"    # waiting for extraction
ST_EXTRACTING = "extracting"
ST_DONE = "done"
ST_ERROR = "error"


class Incident(Base):
    __tablename__ = "incidents"

    incident_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    feed_id = Column(String(100), nullable=False)          # == channel_id for SDR rows
    county = Column(String(50), nullable=False, default="")
    status = Column(String(20), nullable=False, default="ACTIVE")
    opened_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_updated = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at = Column(DateTime(timezone=True))
    incident_type = Column(String(50), nullable=False, default="UNKNOWN")
    priority = Column(String(20), nullable=False, default="UNKNOWN")
    address_raw = Column(Text)
    address_full = Column(Text)
    city = Column(String(100))
    lat = Column(Float)
    lng = Column(Float)
    location = Column(Geometry("POINT", srid=4326))
    units = Column(ARRAY(Text), nullable=False, default=list)
    units_cleared = Column(ARRAY(Text), nullable=False, default=list)
    summary = Column(Text)
    precinct = Column(String(10))
    battalion = Column(String(10))
    nearest_stations = Column(JSONB)
    # rework
    channel_id = Column(String(100))
    loc_kind = Column(String(20))
    loc_street1 = Column(Text)
    loc_street2 = Column(Text)
    loc_number = Column(String(20))
    loc_city = Column(String(100))
    loc_raw = Column(Text)
    geocode_source = Column(String(30))
    geocode_confidence = Column(String(20))
    is_location_inferred = Column(Boolean, nullable=False, default=False)
    is_rail = Column(Boolean, nullable=False, default=False)
    rail = Column(JSONB)
    short_summary = Column(Text)
    summary_updated_at = Column(DateTime(timezone=True))

    chunks = relationship("TranscriptChunk", back_populates="incident",
                          order_by="TranscriptChunk.timestamp", lazy="noload")

    def to_dict(self) -> dict:
        return {
            "incident_id": str(self.incident_id),
            "feed_id": self.feed_id,
            "channel_id": self.channel_id or self.feed_id,
            "county": self.county,
            "status": self.status,
            "opened_at": _iso(self.opened_at),
            "last_updated": _iso(self.last_updated),
            "resolved_at": _iso(self.resolved_at),
            "incident_type": self.incident_type,
            "priority": self.priority,
            "address_raw": self.address_raw,
            "address_full": self.address_full,
            "city": self.city,
            "lat": self.lat,
            "lng": self.lng,
            "location": {
                "kind": self.loc_kind, "street1": self.loc_street1, "street2": self.loc_street2,
                "number": self.loc_number, "city": self.loc_city, "raw": self.loc_raw,
            },
            "geocode_source": self.geocode_source,
            "geocode_confidence": self.geocode_confidence,
            "is_location_inferred": bool(self.is_location_inferred),
            "units": list(self.units or []),
            "units_cleared": list(self.units_cleared or []),
            "summary": self.summary,
            "short_summary": self.short_summary,
            "precinct": self.precinct,
            "battalion": self.battalion,
            "nearest_stations": self.nearest_stations,
            "is_rail": bool(self.is_rail),
            "rail": self.rail,
        }


class TranscriptChunk(Base):
    __tablename__ = "transcript_chunks"

    chunk_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("incidents.incident_id", ondelete="SET NULL"))
    feed_id = Column(String(100), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    raw_transcript = Column(Text)
    normalized_address = Column(Text)
    correlation_action = Column(String(20))
    correlation_method = Column(String(30))
    correlation_confidence = Column(String(20))
    geocode_source = Column(String(30))
    geocode_confidence = Column(String(20))
    whisper_model = Column(String(50))
    lora_version = Column(String(50))
    processing_ms = Column(Integer)
    reviewed = Column(DateTime(timezone=True))
    review_label = Column(Text)
    review_source = Column(Text)
    # rework: queue + provenance
    status = Column(String(20), nullable=False, default=ST_DONE)
    source = Column(String(30))
    source_key = Column(Text, unique=True)
    channel_id = Column(String(100))
    freq_hz = Column(BigInteger)
    captured_at = Column(DateTime(timezone=True))
    duration_s = Column(Float)
    audio_path = Column(Text)
    attempts = Column(Integer, nullable=False, default=0)
    claimed_at = Column(DateTime(timezone=True))
    error = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    transcribed_at = Column(DateTime(timezone=True))
    extracted_at = Column(DateTime(timezone=True))
    # rework: extraction result (one batched Claude call or the rules fallback)
    prefilter = Column(String(30))
    extract_source = Column(String(20))
    has_incident = Column(Boolean)
    has_location = Column(Boolean)
    loc_kind = Column(String(20))
    loc_street1 = Column(Text)
    loc_street2 = Column(Text)
    loc_number = Column(String(20))
    loc_city = Column(String(100))
    loc_raw = Column(Text)
    incident_type = Column(String(50))
    priority = Column(String(20))
    units = Column(ARRAY(Text))
    is_rail = Column(Boolean)
    rail = Column(JSONB)
    short_summary = Column(Text)
    extraction = Column(JSONB)
    llm_usage_id = Column(BigInteger)
    lat = Column(Float)
    lng = Column(Float)
    location = Column(Geometry("POINT", srid=4326))
    tsv = Column(TSVECTOR)   # generated column, see migration 0002

    incident = relationship("Incident", back_populates="chunks", lazy="noload")

    def to_dict(self) -> dict:
        return {
            "chunk_id": str(self.chunk_id),
            "incident_id": str(self.incident_id) if self.incident_id else None,
            "feed_id": self.feed_id,
            "channel_id": self.channel_id or self.feed_id,
            "timestamp": _iso(self.timestamp),
            "captured_at": _iso(self.captured_at or self.timestamp),
            "status": self.status,
            "source": self.source,
            "freq_hz": self.freq_hz,
            "duration_s": self.duration_s,
            "raw_transcript": self.raw_transcript,
            "normalized_address": self.normalized_address,
            "prefilter": self.prefilter,
            "extract_source": self.extract_source,
            "has_incident": self.has_incident,
            "has_location": self.has_location,
            "location": {
                "kind": self.loc_kind, "street1": self.loc_street1, "street2": self.loc_street2,
                "number": self.loc_number, "city": self.loc_city, "raw": self.loc_raw,
            },
            "incident_type": self.incident_type,
            "priority": self.priority,
            "units": list(self.units or []),
            "is_rail": self.is_rail,
            "rail": self.rail,
            "short_summary": self.short_summary,
            "lat": self.lat,
            "lng": self.lng,
            "correlation_action": self.correlation_action,
            "correlation_method": self.correlation_method,
            "correlation_confidence": self.correlation_confidence,
            "geocode_source": self.geocode_source,
            "geocode_confidence": self.geocode_confidence,
            "whisper_model": self.whisper_model,
            "processing_ms": self.processing_ms,
            "error": self.error,
        }


class RailTelemetry(Base):
    __tablename__ = "rail_telemetry"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False)
    channel = Column(String(50))
    source = Column(String(10), nullable=False)           # EOT | HOT | DPU
    freq_hz = Column(BigInteger)
    unit_id = Column(BigInteger)
    brake_pipe_psig = Column(Integer)
    motion = Column(Boolean)
    marker_light = Column(Boolean)
    emergency = Column(Boolean, nullable=False, default=False)
    status = Column(String(30))                            # interpreted, see rail/eot.py
    arm_status = Column(String(20))
    message_type = Column(String(8))
    batt_cond = Column(String(20))
    batt_charge_pct = Column(Integer)
    turbine = Column(Boolean)
    valve_ckt = Column(Boolean)
    valid = Column(Boolean)
    bch_corrected = Column(Integer)
    data_block = Column(String(64))
    raw = Column(JSONB)
    dedupe_key = Column(Text, unique=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self) -> dict:
        return {
            "id": self.id, "ts": _iso(self.ts), "channel": self.channel, "source": self.source,
            "freq_hz": self.freq_hz, "unit_id": self.unit_id, "brake_pipe_psig": self.brake_pipe_psig,
            "motion": self.motion, "marker_light": self.marker_light, "emergency": self.emergency,
            "status": self.status, "arm_status": self.arm_status, "batt_cond": self.batt_cond,
            "batt_charge_pct": self.batt_charge_pct, "bch_corrected": self.bch_corrected,
        }


class RailDetectorEvent(Base):
    __tablename__ = "rail_detector_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    chunk_id = Column(UUID(as_uuid=True), ForeignKey("transcript_chunks.chunk_id", ondelete="SET NULL"))
    channel_id = Column(String(100))
    ts = Column(DateTime(timezone=True), nullable=False)
    railroad = Column(String(50))
    subdivision = Column(String(100))
    detector_name = Column(Text)
    milepost = Column(Numeric(8, 2))
    track = Column(String(20))
    direction = Column(String(20))
    axle_count = Column(Integer)
    speed_mph = Column(Integer)
    temperature_f = Column(Integer)
    result = Column(String(30), nullable=False)            # no_defects|alarm|integrity_failure|unknown
    alarms = Column(JSONB)
    integrity_failure = Column(Boolean, nullable=False, default=False)
    parse_source = Column(String(20))                       # rules | llm
    raw_text = Column(Text)
    lat = Column(Float)
    lng = Column(Float)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "chunk_id": str(self.chunk_id) if self.chunk_id else None,
            "channel_id": self.channel_id, "ts": _iso(self.ts), "railroad": self.railroad,
            "subdivision": self.subdivision, "detector_name": self.detector_name,
            "milepost": float(self.milepost) if self.milepost is not None else None,
            "track": self.track, "direction": self.direction, "axle_count": self.axle_count,
            "speed_mph": self.speed_mph, "temperature_f": self.temperature_f, "result": self.result,
            "alarms": self.alarms or [], "integrity_failure": self.integrity_failure,
            "parse_source": self.parse_source, "raw_text": self.raw_text,
        }


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    workload = Column(String(30), nullable=False)          # live_extract | summary | eval | smoke
    model = Column(String(60), nullable=False)
    served_model = Column(String(60))
    items = Column(Integer, nullable=False, default=0)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cache_creation_input_tokens = Column(Integer, nullable=False, default=0)
    cache_creation_5m = Column(Integer, nullable=False, default=0)
    cache_creation_1h = Column(Integer, nullable=False, default=0)
    cache_read_input_tokens = Column(Integer, nullable=False, default=0)
    stop_reason = Column(String(30))
    stop_category = Column(String(40))
    request_id = Column(Text)
    latency_ms = Column(Integer)
    cost_usd = Column(Numeric(14, 8), nullable=False, default=0)
    price_version = Column(String(20))
    error = Column(Text)


class GeocodeCache(Base):
    __tablename__ = "geocode_cache"

    key = Column(String(64), primary_key=True)
    query = Column(JSONB, nullable=False)
    source = Column(String(30), nullable=False)            # provider that answered, or "none"
    found = Column(Boolean, nullable=False)
    lat = Column(Float)
    lng = Column(Float)
    formatted = Column(Text)
    confidence = Column(String(20))
    hits = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)


class PipelineEvent(Base):
    __tablename__ = "pipeline_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    kind = Column(String(40), nullable=False)
    detail = Column(JSONB)


class Liveness(Base):
    """One unbroken run of heartbeats of a receiver or an ingest source (core/liveness.py)."""
    __tablename__ = "liveness"

    component = Column(String(120), primary_key=True)
    up_since = Column(DateTime(timezone=True), primary_key=True)
    last_seen = Column(DateTime(timezone=True), nullable=False)
    fingerprint = Column(Text, nullable=False, server_default="")
    detail = Column(JSONB)


class ReceiverSquelch(Base):
    """How a receiver's counters for one channel grew since its previous stats sample (core/liveness.py)."""
    __tablename__ = "receiver_squelch"

    component = Column(String(120), primary_key=True)
    at = Column(DateTime(timezone=True), primary_key=True)
    label = Column(Text, primary_key=True, server_default="")
    freq_mhz = Column(Float, primary_key=True)
    opens = Column(Integer, nullable=False, server_default="0")
    dropped = Column(Integer, nullable=False, server_default="0")
    active = Column(Integer, nullable=False, server_default="0")


Index("ix_llm_usage_ts", LlmUsage.ts)
Index("ix_liveness_last_seen", Liveness.last_seen)
Index("ix_receiver_squelch_at", ReceiverSquelch.at)
