-- Detroit Pulse schema reference (generated with pg_dump --schema-only after
-- 'alembic upgrade head'). Alembic in db/migrations is authoritative; this file is
-- for reading and diffing only. PostGIS/tiger/topology objects are omitted.


-- Name: public; Type: SCHEMA; Schema: -; Owner: -

CREATE SCHEMA public;

-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -

COMMENT ON SCHEMA public IS 'standard public schema';

-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);

-- Name: geocode_cache; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.geocode_cache (
    key character varying(64) NOT NULL,
    query jsonb NOT NULL,
    source character varying(30) NOT NULL,
    found boolean NOT NULL,
    lat double precision,
    lng double precision,
    formatted text,
    confidence character varying(20),
    hits integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);

-- Name: incidents; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.incidents (
    incident_id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    feed_id character varying(100) NOT NULL,
    county character varying(50) DEFAULT ''::character varying NOT NULL,
    status character varying(20) DEFAULT 'ACTIVE'::character varying NOT NULL,
    opened_at timestamp with time zone DEFAULT now() NOT NULL,
    last_updated timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    incident_type character varying(50) DEFAULT 'UNKNOWN'::character varying NOT NULL,
    priority character varying(20) DEFAULT 'UNKNOWN'::character varying NOT NULL,
    address_raw text,
    address_full text,
    city character varying(100),
    lat double precision,
    lng double precision,
    location public.geometry(Point,4326),
    units text[] DEFAULT '{}'::text[] NOT NULL,
    units_cleared text[] DEFAULT '{}'::text[] NOT NULL,
    summary text,
    precinct character varying(10),
    battalion character varying(10),
    nearest_stations jsonb,
    channel_id character varying(100),
    loc_kind character varying(20),
    loc_street1 text,
    loc_street2 text,
    loc_number character varying(20),
    loc_city character varying(100),
    loc_raw text,
    geocode_source character varying(30),
    geocode_confidence character varying(20),
    is_location_inferred boolean DEFAULT false NOT NULL,
    is_rail boolean DEFAULT false NOT NULL,
    rail jsonb,
    short_summary text,
    summary_updated_at timestamp with time zone
);

-- Name: liveness; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.liveness (
    component character varying(120) NOT NULL,
    up_since timestamp with time zone NOT NULL,
    last_seen timestamp with time zone NOT NULL,
    fingerprint text DEFAULT ''::text NOT NULL,
    detail jsonb
);

-- Name: llm_usage; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.llm_usage (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    workload character varying(30) NOT NULL,
    model character varying(60) NOT NULL,
    served_model character varying(60),
    items integer DEFAULT 0 NOT NULL,
    input_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    cache_creation_input_tokens integer DEFAULT 0 NOT NULL,
    cache_creation_5m integer DEFAULT 0 NOT NULL,
    cache_creation_1h integer DEFAULT 0 NOT NULL,
    cache_read_input_tokens integer DEFAULT 0 NOT NULL,
    stop_reason character varying(30),
    stop_category character varying(40),
    request_id text,
    latency_ms integer,
    cost_usd numeric(14,8) DEFAULT 0 NOT NULL,
    price_version character varying(20),
    error text
);

-- Name: llm_usage_id_seq; Type: SEQUENCE; Schema: public; Owner: -

CREATE SEQUENCE public.llm_usage_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

-- Name: llm_usage_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -

ALTER SEQUENCE public.llm_usage_id_seq OWNED BY public.llm_usage.id;

-- Name: lora_model_registry; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.lora_model_registry (
    id integer NOT NULL,
    version text NOT NULL,
    task text NOT NULL,
    base_model text DEFAULT 'Qwen/Qwen2.5-7B-Instruct'::text NOT NULL,
    adapter_path text NOT NULL,
    ollama_model_name text,
    train_examples integer,
    human_reviewed integer,
    eval_exact_match real,
    eval_no_location_f1 real,
    eval_hallucination_rate real,
    baseline_exact_match real,
    baseline_no_location_f1 real,
    baseline_hallucination_rate real,
    status text DEFAULT 'trained'::text NOT NULL,
    deployed_at timestamp with time zone,
    retired_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone DEFAULT now()
);

-- Name: lora_model_registry_id_seq; Type: SEQUENCE; Schema: public; Owner: -

CREATE SEQUENCE public.lora_model_registry_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

-- Name: lora_model_registry_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -

ALTER SEQUENCE public.lora_model_registry_id_seq OWNED BY public.lora_model_registry.id;

-- Name: pipeline_events; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.pipeline_events (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    kind character varying(40) NOT NULL,
    detail jsonb
);

-- Name: pipeline_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -

CREATE SEQUENCE public.pipeline_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

-- Name: pipeline_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -

ALTER SEQUENCE public.pipeline_events_id_seq OWNED BY public.pipeline_events.id;

-- Name: pipeline_metrics; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.pipeline_metrics (
    bucket timestamp with time zone NOT NULL,
    model_version text DEFAULT 'base'::text NOT NULL,
    normalize_total integer DEFAULT 0 NOT NULL,
    normalize_no_loc integer DEFAULT 0 NOT NULL,
    geocode_high integer DEFAULT 0 NOT NULL,
    geocode_medium integer DEFAULT 0 NOT NULL,
    geocode_failed integer DEFAULT 0 NOT NULL,
    corr_new integer DEFAULT 0 NOT NULL,
    corr_update integer DEFAULT 0 NOT NULL,
    corr_unassociated integer DEFAULT 0 NOT NULL,
    norm_latency_ms_sum bigint DEFAULT 0 NOT NULL
);

-- Name: rail_detector_events; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.rail_detector_events (
    id bigint NOT NULL,
    chunk_id uuid,
    channel_id character varying(100),
    ts timestamp with time zone NOT NULL,
    railroad character varying(50),
    subdivision character varying(100),
    detector_name text,
    milepost numeric(8,2),
    track character varying(20),
    direction character varying(20),
    axle_count integer,
    speed_mph integer,
    temperature_f integer,
    result character varying(30) NOT NULL,
    alarms jsonb,
    integrity_failure boolean DEFAULT false NOT NULL,
    parse_source character varying(20),
    raw_text text,
    lat double precision,
    lng double precision
);

-- Name: rail_detector_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -

CREATE SEQUENCE public.rail_detector_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

-- Name: rail_detector_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -

ALTER SEQUENCE public.rail_detector_events_id_seq OWNED BY public.rail_detector_events.id;

-- Name: rail_telemetry; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.rail_telemetry (
    id bigint NOT NULL,
    ts timestamp with time zone NOT NULL,
    channel character varying(50),
    source character varying(10) NOT NULL,
    freq_hz bigint,
    unit_id bigint,
    brake_pipe_psig integer,
    motion boolean,
    marker_light boolean,
    emergency boolean DEFAULT false NOT NULL,
    status character varying(30),
    arm_status character varying(20),
    message_type character varying(8),
    batt_cond character varying(20),
    batt_charge_pct integer,
    turbine boolean,
    valve_ckt boolean,
    valid boolean,
    bch_corrected integer,
    data_block character varying(64),
    raw jsonb,
    dedupe_key text,
    received_at timestamp with time zone DEFAULT now()
);

-- Name: rail_telemetry_id_seq; Type: SEQUENCE; Schema: public; Owner: -

CREATE SEQUENCE public.rail_telemetry_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

-- Name: rail_telemetry_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -

ALTER SEQUENCE public.rail_telemetry_id_seq OWNED BY public.rail_telemetry.id;

-- Name: shadow_log; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.shadow_log (
    id bigint NOT NULL,
    chunk_id uuid,
    feed_id text NOT NULL,
    transcript text NOT NULL,
    base_result text,
    lora_result text,
    base_geo_conf text,
    lora_geo_conf text,
    base_geo_source text,
    lora_geo_source text,
    base_lat double precision,
    base_lng double precision,
    lora_lat double precision,
    lora_lng double precision,
    base_ms integer,
    lora_ms integer,
    agreed boolean GENERATED ALWAYS AS ((base_result = lora_result)) STORED,
    created_at timestamp with time zone DEFAULT now()
);

-- Name: shadow_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -

CREATE SEQUENCE public.shadow_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

-- Name: shadow_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -

ALTER SEQUENCE public.shadow_log_id_seq OWNED BY public.shadow_log.id;

-- Name: transcript_chunks; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.transcript_chunks (
    chunk_id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    incident_id uuid,
    feed_id character varying(100) NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    raw_transcript text,
    normalized_address text,
    correlation_action character varying(20),
    correlation_method character varying(30),
    correlation_confidence character varying(20),
    geocode_source character varying(30),
    geocode_confidence character varying(20),
    whisper_model character varying(50),
    lora_version character varying(50),
    processing_ms integer,
    reviewed timestamp with time zone,
    review_label text,
    review_source text,
    status character varying(20) DEFAULT 'done'::character varying NOT NULL,
    source character varying(30),
    source_key text,
    channel_id character varying(100),
    freq_hz bigint,
    captured_at timestamp with time zone,
    duration_s real,
    audio_path text,
    attempts integer DEFAULT 0 NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now(),
    transcribed_at timestamp with time zone,
    extracted_at timestamp with time zone,
    prefilter character varying(30),
    extract_source character varying(20),
    has_incident boolean,
    has_location boolean,
    loc_kind character varying(20),
    loc_street1 text,
    loc_street2 text,
    loc_number character varying(20),
    loc_city character varying(100),
    loc_raw text,
    incident_type character varying(50),
    priority character varying(20),
    units text[],
    is_rail boolean,
    rail jsonb,
    short_summary text,
    extraction jsonb,
    llm_usage_id bigint,
    lat double precision,
    lng double precision,
    location public.geometry(Point,4326),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, COALESCE(raw_transcript, ''::text))) STORED
);

-- Name: llm_usage id; Type: DEFAULT; Schema: public; Owner: -

ALTER TABLE ONLY public.llm_usage ALTER COLUMN id SET DEFAULT nextval('public.llm_usage_id_seq'::regclass);

-- Name: lora_model_registry id; Type: DEFAULT; Schema: public; Owner: -

ALTER TABLE ONLY public.lora_model_registry ALTER COLUMN id SET DEFAULT nextval('public.lora_model_registry_id_seq'::regclass);

-- Name: pipeline_events id; Type: DEFAULT; Schema: public; Owner: -

ALTER TABLE ONLY public.pipeline_events ALTER COLUMN id SET DEFAULT nextval('public.pipeline_events_id_seq'::regclass);

-- Name: rail_detector_events id; Type: DEFAULT; Schema: public; Owner: -

ALTER TABLE ONLY public.rail_detector_events ALTER COLUMN id SET DEFAULT nextval('public.rail_detector_events_id_seq'::regclass);

-- Name: rail_telemetry id; Type: DEFAULT; Schema: public; Owner: -

ALTER TABLE ONLY public.rail_telemetry ALTER COLUMN id SET DEFAULT nextval('public.rail_telemetry_id_seq'::regclass);

-- Name: shadow_log id; Type: DEFAULT; Schema: public; Owner: -

ALTER TABLE ONLY public.shadow_log ALTER COLUMN id SET DEFAULT nextval('public.shadow_log_id_seq'::regclass);

-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);

-- Name: geocode_cache geocode_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.geocode_cache
    ADD CONSTRAINT geocode_cache_pkey PRIMARY KEY (key);

-- Name: incidents incidents_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.incidents
    ADD CONSTRAINT incidents_pkey PRIMARY KEY (incident_id);

-- Name: liveness liveness_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.liveness
    ADD CONSTRAINT liveness_pkey PRIMARY KEY (component, up_since);

-- Name: llm_usage llm_usage_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.llm_usage
    ADD CONSTRAINT llm_usage_pkey PRIMARY KEY (id);

-- Name: lora_model_registry lora_model_registry_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.lora_model_registry
    ADD CONSTRAINT lora_model_registry_pkey PRIMARY KEY (id);

-- Name: lora_model_registry lora_model_registry_version_key; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.lora_model_registry
    ADD CONSTRAINT lora_model_registry_version_key UNIQUE (version);

-- Name: pipeline_events pipeline_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.pipeline_events
    ADD CONSTRAINT pipeline_events_pkey PRIMARY KEY (id);

-- Name: pipeline_metrics pipeline_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.pipeline_metrics
    ADD CONSTRAINT pipeline_metrics_pkey PRIMARY KEY (bucket, model_version);

-- Name: rail_detector_events rail_detector_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.rail_detector_events
    ADD CONSTRAINT rail_detector_events_pkey PRIMARY KEY (id);

-- Name: rail_telemetry rail_telemetry_dedupe_key_key; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.rail_telemetry
    ADD CONSTRAINT rail_telemetry_dedupe_key_key UNIQUE (dedupe_key);

-- Name: rail_telemetry rail_telemetry_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.rail_telemetry
    ADD CONSTRAINT rail_telemetry_pkey PRIMARY KEY (id);

-- Name: shadow_log shadow_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.shadow_log
    ADD CONSTRAINT shadow_log_pkey PRIMARY KEY (id);

-- Name: transcript_chunks transcript_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.transcript_chunks
    ADD CONSTRAINT transcript_chunks_pkey PRIMARY KEY (chunk_id);

-- Name: idx_pipeline_metrics_bucket; Type: INDEX; Schema: public; Owner: -

CREATE INDEX idx_pipeline_metrics_bucket ON public.pipeline_metrics USING btree (bucket DESC);

-- Name: idx_shadow_log_agreed; Type: INDEX; Schema: public; Owner: -

CREATE INDEX idx_shadow_log_agreed ON public.shadow_log USING btree (agreed);

-- Name: idx_shadow_log_chunk; Type: INDEX; Schema: public; Owner: -

CREATE INDEX idx_shadow_log_chunk ON public.shadow_log USING btree (chunk_id);

-- Name: idx_shadow_log_created; Type: INDEX; Schema: public; Owner: -

CREATE INDEX idx_shadow_log_created ON public.shadow_log USING btree (created_at);

-- Name: idx_shadow_log_feed; Type: INDEX; Schema: public; Owner: -

CREATE INDEX idx_shadow_log_feed ON public.shadow_log USING btree (feed_id);

-- Name: ix_chunks_captured; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_chunks_captured ON public.transcript_chunks USING btree (captured_at DESC);

-- Name: ix_chunks_channel_captured; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_chunks_channel_captured ON public.transcript_chunks USING btree (channel_id, captured_at DESC);

-- Name: ix_chunks_feed_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_chunks_feed_id ON public.transcript_chunks USING btree (feed_id);

-- Name: ix_chunks_incident_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_chunks_incident_id ON public.transcript_chunks USING btree (incident_id);

-- Name: ix_chunks_location; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_chunks_location ON public.transcript_chunks USING gist (location);

-- Name: ix_chunks_timestamp; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_chunks_timestamp ON public.transcript_chunks USING btree ("timestamp");

-- Name: ix_chunks_tsv; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_chunks_tsv ON public.transcript_chunks USING gin (tsv);

-- Name: ix_chunks_work_queue; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_chunks_work_queue ON public.transcript_chunks USING btree (status, created_at) WHERE ((status)::text = ANY ((ARRAY['queued'::character varying, 'transcribing'::character varying, 'transcribed'::character varying, 'extracting'::character varying])::text[]));

-- Name: ix_geocode_cache_expires; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_geocode_cache_expires ON public.geocode_cache USING btree (expires_at);

-- Name: ix_incidents_feed_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_incidents_feed_id ON public.incidents USING btree (feed_id);

-- Name: ix_incidents_incident_type; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_incidents_incident_type ON public.incidents USING btree (incident_type);

-- Name: ix_incidents_location; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_incidents_location ON public.incidents USING gist (location);

-- Name: ix_incidents_opened_at; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_incidents_opened_at ON public.incidents USING btree (opened_at);

-- Name: ix_incidents_status_county; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_incidents_status_county ON public.incidents USING btree (status, county);

-- Name: ix_incidents_status_updated; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_incidents_status_updated ON public.incidents USING btree (status, last_updated DESC);

-- Name: ix_incidents_units; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_incidents_units ON public.incidents USING gin (units);

-- Name: ix_liveness_last_seen; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_liveness_last_seen ON public.liveness USING btree (last_seen);

-- Name: ix_llm_usage_ts; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_llm_usage_ts ON public.llm_usage USING btree (ts);

-- Name: ix_pipeline_events_ts; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_pipeline_events_ts ON public.pipeline_events USING btree (ts DESC);

-- Name: ix_rail_detector_ts; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_rail_detector_ts ON public.rail_detector_events USING btree (ts DESC);

-- Name: ix_rail_telemetry_ts; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_rail_telemetry_ts ON public.rail_telemetry USING btree (ts DESC);

-- Name: ix_rail_telemetry_unit; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_rail_telemetry_unit ON public.rail_telemetry USING btree (unit_id, ts DESC);

-- Name: ux_chunks_source_key; Type: INDEX; Schema: public; Owner: -

CREATE UNIQUE INDEX ux_chunks_source_key ON public.transcript_chunks USING btree (source_key);

-- Name: rail_detector_events rail_detector_events_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.rail_detector_events
    ADD CONSTRAINT rail_detector_events_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.transcript_chunks(chunk_id) ON DELETE SET NULL;

-- Name: transcript_chunks transcript_chunks_incident_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.transcript_chunks
    ADD CONSTRAINT transcript_chunks_incident_id_fkey FOREIGN KEY (incident_id) REFERENCES public.incidents(incident_id) ON DELETE SET NULL;

