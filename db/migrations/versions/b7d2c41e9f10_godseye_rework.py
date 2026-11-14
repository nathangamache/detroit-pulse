"""godseye rework: SDR queue columns, structured locations, rail, cost meter, geocode cache

Revision ID: b7d2c41e9f10
Revises: a3a395ae2585
Create Date: 2026-11-14

Adds (all idempotent so it can run on the restored archive too):
  transcript_chunks  durable queue status, capture time, channel/frequency, structured
                     extraction fields, generated full-text tsvector
  incidents          channel_id, structured location fields, geocode provenance, rail fields
  rail_telemetry, rail_detector_events, llm_usage, geocode_cache, pipeline_events
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b7d2c41e9f10"
down_revision: Union[str, None] = "a3a395ae2585"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CHUNK_COLS = [
    ("status", "varchar(20) NOT NULL DEFAULT 'done'"),
    ("source", "varchar(30)"),
    ("source_key", "text"),
    ("channel_id", "varchar(100)"),
    ("freq_hz", "bigint"),
    ("captured_at", "timestamptz"),
    ("duration_s", "real"),
    ("audio_path", "text"),
    ("attempts", "integer NOT NULL DEFAULT 0"),
    ("error", "text"),
    ("created_at", "timestamptz DEFAULT now()"),
    ("transcribed_at", "timestamptz"),
    ("extracted_at", "timestamptz"),
    ("prefilter", "varchar(30)"),
    ("extract_source", "varchar(20)"),
    ("has_incident", "boolean"),
    ("has_location", "boolean"),
    ("loc_kind", "varchar(20)"),
    ("loc_street1", "text"),
    ("loc_street2", "text"),
    ("loc_number", "varchar(20)"),
    ("loc_city", "varchar(100)"),
    ("loc_raw", "text"),
    ("incident_type", "varchar(50)"),
    ("priority", "varchar(20)"),
    ("units", "text[]"),
    ("is_rail", "boolean"),
    ("rail", "jsonb"),
    ("short_summary", "text"),
    ("extraction", "jsonb"),
    ("llm_usage_id", "bigint"),
    ("lat", "double precision"),
    ("lng", "double precision"),
    ("location", "geometry(Point, 4326)"),
]

INCIDENT_COLS = [
    ("channel_id", "varchar(100)"),
    ("loc_kind", "varchar(20)"),
    ("loc_street1", "text"),
    ("loc_street2", "text"),
    ("loc_number", "varchar(20)"),
    ("loc_city", "varchar(100)"),
    ("loc_raw", "text"),
    ("geocode_source", "varchar(30)"),
    ("geocode_confidence", "varchar(20)"),
    ("is_location_inferred", "boolean NOT NULL DEFAULT false"),
    ("is_rail", "boolean NOT NULL DEFAULT false"),
    ("rail", "jsonb"),
    ("short_summary", "text"),
    ("summary_updated_at", "timestamptz"),
]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    for name, ddl in CHUNK_COLS:
        op.execute(f"ALTER TABLE transcript_chunks ADD COLUMN IF NOT EXISTS {name} {ddl}")
    op.execute("""
        ALTER TABLE transcript_chunks ADD COLUMN IF NOT EXISTS tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', coalesce(raw_transcript, ''))) STORED""")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_chunks_source_key ON transcript_chunks (source_key)")
    op.execute("""CREATE INDEX IF NOT EXISTS ix_chunks_work_queue ON transcript_chunks (status, created_at)
                  WHERE status IN ('queued', 'transcribing', 'transcribed', 'extracting')""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chunks_channel_captured ON transcript_chunks (channel_id, captured_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chunks_captured ON transcript_chunks (captured_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chunks_tsv ON transcript_chunks USING gin (tsv)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chunks_location ON transcript_chunks USING gist (location)")

    for name, ddl in INCIDENT_COLS:
        op.execute(f"ALTER TABLE incidents ADD COLUMN IF NOT EXISTS {name} {ddl}")
    op.execute("ALTER TABLE incidents ALTER COLUMN county SET DEFAULT ''")
    op.execute("CREATE INDEX IF NOT EXISTS ix_incidents_units ON incidents USING gin (units)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_incidents_status_updated ON incidents (status, last_updated DESC)")
    # incidents.location was never populated by the old pipeline; backfill from lat/lng.
    op.execute("""UPDATE incidents SET location = ST_SetSRID(ST_MakePoint(lng, lat), 4326)
                  WHERE location IS NULL AND lat IS NOT NULL AND lng IS NOT NULL""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS rail_telemetry (
        id bigserial PRIMARY KEY,
        ts timestamptz NOT NULL,
        channel varchar(50),
        source varchar(10) NOT NULL,
        freq_hz bigint,
        unit_id bigint,
        brake_pipe_psig integer,
        motion boolean,
        marker_light boolean,
        emergency boolean NOT NULL DEFAULT false,
        status varchar(30),
        arm_status varchar(20),
        message_type varchar(8),
        batt_cond varchar(20),
        batt_charge_pct integer,
        turbine boolean,
        valve_ckt boolean,
        valid boolean,
        bch_corrected integer,
        data_block varchar(64),
        raw jsonb,
        dedupe_key text UNIQUE,
        received_at timestamptz DEFAULT now()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_rail_telemetry_ts ON rail_telemetry (ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_rail_telemetry_unit ON rail_telemetry (unit_id, ts DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS rail_detector_events (
        id bigserial PRIMARY KEY,
        chunk_id uuid REFERENCES transcript_chunks (chunk_id) ON DELETE SET NULL,
        channel_id varchar(100),
        ts timestamptz NOT NULL,
        railroad varchar(50),
        subdivision varchar(100),
        detector_name text,
        milepost numeric(8, 2),
        track varchar(20),
        direction varchar(20),
        axle_count integer,
        speed_mph integer,
        temperature_f integer,
        result varchar(30) NOT NULL,
        alarms jsonb,
        integrity_failure boolean NOT NULL DEFAULT false,
        parse_source varchar(20),
        raw_text text,
        lat double precision,
        lng double precision
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_rail_detector_ts ON rail_detector_events (ts DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS llm_usage (
        id bigserial PRIMARY KEY,
        ts timestamptz NOT NULL DEFAULT now(),
        workload varchar(30) NOT NULL,
        model varchar(60) NOT NULL,
        served_model varchar(60),
        items integer NOT NULL DEFAULT 0,
        input_tokens integer NOT NULL DEFAULT 0,
        output_tokens integer NOT NULL DEFAULT 0,
        cache_creation_input_tokens integer NOT NULL DEFAULT 0,
        cache_creation_5m integer NOT NULL DEFAULT 0,
        cache_creation_1h integer NOT NULL DEFAULT 0,
        cache_read_input_tokens integer NOT NULL DEFAULT 0,
        stop_reason varchar(30),
        stop_category varchar(40),
        request_id text,
        latency_ms integer,
        cost_usd numeric(14, 8) NOT NULL DEFAULT 0,
        price_version varchar(20),
        error text
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_usage_ts ON llm_usage (ts)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS geocode_cache (
        key varchar(64) PRIMARY KEY,
        query jsonb NOT NULL,
        source varchar(30) NOT NULL,
        found boolean NOT NULL,
        lat double precision,
        lng double precision,
        formatted text,
        confidence varchar(20),
        hits integer NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_geocode_cache_expires ON geocode_cache (expires_at)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS pipeline_events (
        id bigserial PRIMARY KEY,
        ts timestamptz NOT NULL DEFAULT now(),
        kind varchar(40) NOT NULL,
        detail jsonb
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_pipeline_events_ts ON pipeline_events (ts DESC)")


def downgrade() -> None:
    for t in ("pipeline_events", "geocode_cache", "llm_usage", "rail_detector_events", "rail_telemetry"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
    for name, _ in reversed(INCIDENT_COLS):
        op.execute(f"ALTER TABLE incidents DROP COLUMN IF EXISTS {name}")
    op.execute("ALTER TABLE transcript_chunks DROP COLUMN IF EXISTS tsv")
    for name, _ in reversed(CHUNK_COLS):
        op.execute(f"ALTER TABLE transcript_chunks DROP COLUMN IF EXISTS {name}")
