# Detroit Pulse architecture (godseye rework, 2026-11)

Pulse turns scanner and railroad radio into structured, mapped incidents for Wayne,
Oakland and Washtenaw counties. It runs CPU-only on a shared home server (
2x Xeon E5-2687W v2, AVX without AVX2, no GPU) next to other services, so everything
binds to 127.0.0.1, runs at `nice 19`, and caps its threads.

## Pipeline

```
 own RTL-SDR receivers                                     Claude Haiku 5.5 (1 call / batch)
 ┌──────────────┐  one file per     ┌───────────┐  text   ┌─────────────────────────────────┐
 │ rtl_airband  │─ transmission ───▶│ transcribe│────────▶│ extract                          │
 │ trunk-rec.   │  (mp3/wav)        │ faster-   │         │  prefilter (rules, $0)           │
 └──────────────┘        ▲          │ whisper   │         │  micro-batch ≤10 or ≤45 s        │
        sdr_files source │          │ CPU int8  │         │  structured output, cached prompt│
 ┌──────────────┐  CSV   │          └───────────┘         │  rules fallback (caps/errors)    │
 │ traintap     │───────▶ rail_telemetry (EOT/HOT)        │  geocode ($0 chain) → correlate  │
 └──────────────┘                                          └──────────────┬──────────────────┘
                                                                          ▼
                        Postgres/PostGIS 127.0.0.1:5436  ◀── incidents, transcript_chunks,
                        (single source of truth)              rail_detector_events, llm_usage …
                                  │                                       │ Redis 127.0.0.1:6381
                                  ▼                                       ▼ (pub/sub, doorbells)
                        API 127.0.0.1:7710  ── /v1 contract ──▶ gateway (MCP tools, /pulse UI)
                                            ── /ws events ────▶ React/MapLibre UI
```

### Stages

All stages run as threads of `run_pipeline.py` (or separately with `--stages`). They
communicate only through `transcript_chunks.status`, a durable queue claimed with
`FOR UPDATE SKIP LOCKED`; Redis only rings doorbells and fans events out to WebSocket
clients, so a Redis outage delays the UI but never loses data.

```
queued ──transcribe──▶ transcribed ──extract──▶ done
   └─ after 3 failed attempts ─▶ error            (crashed claims are re-queued after 15 min)
```

| Stage | Module | What it does |
|---|---|---|
| ingest | `ingest/sources/*` | `sdr_files` watches `SDR_WATCH_DIR` for closed rtl_airband/trunk-recorder files and maps them to channels; `traintap` tails traintap's CSV into `rail_telemetry`; `fixture` replays synthetic JSONL; `broadcastify_legacy` is a disabled stub (Terms §9). See [sdr-ingest.md](sdr-ingest.md). |
| transcribe | `transcribe/worker.py` | faster-whisper, default `small.en`/cpu/int8/8 threads (`medium.en` is the accuracy option), Silero VAD via onnxruntime, PyAV decoding (no torch, no ffmpeg). One pass per transmission; the vocabulary prompt (`transcribe/vocab.py`) fits Whisper's 223-token window with the most specific names last, then the channel's previous transmission. |
| extract | `extract/worker.py` | Prefilter, micro-batch, one structured Claude call, rules fallback, geocode, correlate, store, publish. Details below. |
| resolver | `correlation/resolver.py` | Every 2 min: resolves incidents idle for `INCIDENT_IDLE_RESOLVE_S` (4 h) in Postgres, writes the summary, broadcasts; re-queues stuck rows; purges expired geocode cache. |
| API | `api/` | FastAPI on 127.0.0.1:7710. See [pulse-api.md](pulse-api.md). |

### Extraction

1. **Prefilter** (`extract/prefilter.py`, $0): Whisper hallucinations, pure chatter
   (acknowledgements, status phrases, unit IDs, codes, short numbers) and fully parsed
   defect-detector readouts are finished without an API call. It's tuned for recall:
   any non-lexicon word, 3+ digit number or 3+ number-word run keeps a transmission.
   Measured recall on location-bearing data: TRAIN 2659/2659, archive Claude-reviewed
   2963/2963, archive geocoded 48954/48995 (99.92%).
2. **Micro-batch**: up to `LLM_BATCH_SIZE` (10) transmissions, or whatever has waited
   `LLM_BATCH_MAX_WAIT_S` (45 s). Items are finished in capture order, so status traffic
   never overtakes the dispatch it refers to.
3. **One Claude call** (`extract/claude.py`, eval "Config C"): `claude-haiku-5-5`,
   structured output (`output_config.format` JSON schema, `extract/schema.py`),
   thinking disabled, effort medium, system prompt + few-shots cached with the 1-hour
   TTL. Per transmission it returns `has_location`, a structured `location`
   (`kind` intersection|block|address|landmark|highway|none, `street1`, `street2`,
   `number`, `city`, `raw`), `incident_type`, `priority`, `units`, `is_rail`, `rail`
   (`railroad`, `subdivision`, `train_symbol`, `milepost`, `reason_label`, `detector`) and
   `short_summary`. Few-shots come from the TRAIN split only and stay in a gitignored local
   file.
4. **Fallback**: no key, a cap hit, an API error, a refusal, truncation that survives one
   split, or a missing id all send those items through `extract/rules.py`.
   `transcript_chunks.extract_source` records `claude`, `rules` or `prefilter`.
5. **Geocode** (`geocode/`): the structured location goes to a $0 chain: the local index
   (`geocode/local/`: OSM intersections, address points + TIGER ranges, OSM landmarks; built by
   `scripts/build_geocoder_index.py`, see docs/geocoder.md), US Census, Photon,
   optional Nominatim (1 req/s). Results outside the coverage bbox are rejected; hits and
   misses are cached in `geocode_cache` with the answering provider. Google is removed.
6. **Correlate** (`correlation/engine.py`, deterministic): unit continuity on the same
   channel within 30 min, then Layer-1 signal scores (address overlap, distance, shared
   units, type, burst, same channel, age) against active incidents, one threshold
   (0.50). Status traffic joins the incident timeline; "clear / in service" releases
   units and resolves the incident when none remain. Routine rail operations stay
   unassociated and are served by `/v1/rail/activity`.
7. **Store and publish**: every transmission ends `done` with its full extraction JSON;
   incidents get PostGIS `location`, structured location fields, geocode provenance and
   precinct/battalion enrichment; events go to Redis `pulse:events`.

### Rail

* `rail/detector.py`: rule-based defect-detector parser (milepost, track, direction,
  axles, speed, temperature, no defects vs hot box / hot wheel / dragging equipment /
  high-wide, integrity failures; spelled and digit-by-digit numbers). Incomplete parses
  are completed from the batch call's `rail.detector`.
* `rail/eot.py`: EOT status: moving / normal stop / emergency from rear brake-pipe
  pressure, motion and the unit's recent history.
* Reason labels: `meet_or_hold`, `signal_problem`, `defect_detector`, `crew_change_hos`,
  `mechanical`, `crossing_incident`, `trespasser`, `switching_yard`, `unknown` (Claude,
  or keyword rules when degraded); mapped to the gateway's enum in the API.
* `config/rail_channels(.local).yaml` + `scripts/import_fcc_uls.py` (FCC ULS public data).
  Subdivisions and channels carry `aliases` (the same track's names in other datasets, e.g.
  FRA vs NARN) so a lookup by either name finds the line.
* Receivers (`core/receivers.py`): rtl_airband's stats file and traintap's heartbeat, written
  even while the channels are silent, give every feed a `receiver.state` (`not_installed` /
  `offline` / `online`) in `/v1/feeds/status` ([sdr-ingest.md](sdr-ingest.md#6-receiver-heartbeats)).
  That is "listening now". `/v1/rail/activity` `verdicts` (`api/rail_verdicts.py`) call a point
  quiet only on proof for the whole window: every channel whose own area covers it was listened
  to on its configured frequency since before the window, by the heartbeat history the ingest
  stage records in `liveness` (`core/liveness.py`, [sdr-ingest.md](sdr-ingest.md#8-heartbeat-history-and-the-ingest-heartbeat)),
  with no gap longer than a missed beat or two of that receiver and no restart (each receiver
  writes a start marker before every start), whatever rtl_airband's squelch counters show it
  heard became a recording, the ingest stage itself beat as long, and nothing was heard. The quiet window ends at the last moment
  those heartbeats cover, never at the request time.

## Data model

| Table | Purpose |
|---|---|
| `transcript_chunks` | One row per transmission: queue status, capture time, channel, frequency, audio path, transcript, generated `tsvector`, prefilter label, extraction source, structured location, type/priority/units, rail JSON, coordinates, correlation, `llm_usage_id`. |
| `incidents` | Correlated incidents with PostGIS `location`, structured location, geocode source/confidence, units, summaries, rail fields. |
| `rail_telemetry` | traintap packets with interpreted `status` and `emergency`. |
| `rail_detector_events` | Parsed detector readouts. |
| `llm_usage` | One row per Claude response: tokens by kind, cost, stop reason, request id. |
| `geocode_cache` | $0 geocoder results and misses, with `source`. |
| `pipeline_events` | Cap hits, admin resets. |
| `liveness` | Runs of receiver and ingest heartbeats (`up_since` .. `last_seen`, the channel list as a fingerprint, the receiver's start marker), written by the ingest stage; `/v1/rail/activity` `verdicts` read them. |
| `receiver_squelch` | Per rtl_airband stats sample and channel: squelch openings, transmissions dropped as too short and output loops with the squelch open since the previous sample; `verdicts` compare them with the recordings in the window. |
| `shadow_log`, `pipeline_metrics`, `lora_model_registry` | Legacy tables kept so the archive and the live schema match. |

Migrations: `db/migrations` (alembic). The baseline keeps the original revision id, so
the restored archive (`detroitpulse_archive`) upgrades in place. `db/schema.sql` is a
generated reference.

## Privacy and licensing rules baked in

* The repository is public. Committed config (`config/*.yaml`) holds generic examples
  only; personal areas, real channel plans and few-shot transcripts live in gitignored
  `*.local.*` files. Tests copy the committed config and never read local files.
* No Broadcastify audio or capture (Terms §9); no RadioReference data (AI/ML use
  forbidden); no Google geocoding (cost, 30-day caching limit).
* The old database is only ever restored into `detroitpulse_archive`.
* Radio text reaches the gateway only in `*_untrusted` fields; prompts tell Claude that
  transcripts are untrusted data.

## Fixed bugs from the old code

* Geo enrichment was called as `enrich_incident(lat, lng, units)` against a
  `enrich_incident(incident)` signature, so precinct/battalion were never set.
* The Redis resolver never updated Postgres, and `INCIDENT_TTL_SECONDS` defaulted to
  180000 in one module and 14400 in another; now one Postgres resolver, one setting.
* Silent data loss: the chunker dropped 2-10 s per cycle, `has_incident=false`
  transcripts were never stored, RETRY items expired unwritten, and LLM errors became
  `has_incident=false`. Now every transmission is stored and finished.
* `/admin/*` (including `DELETE /admin/reset/all`) and the `/audio/stream` relay were
  unauthenticated; admin is now token-gated and the relay is gone.
* Mapbox token requirement, missing lockfiles, torch pulling CUDA wheels, hard-coded
  dev passwords and DSNs, `0.0.0.0:8080`, and temperature parameters the current API rejects.
