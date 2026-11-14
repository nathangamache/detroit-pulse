# Detroit Pulse

Real-time scanner-radio incidents for Wayne, Oakland and Washtenaw counties. Pulse takes
radio transmissions from your own SDR receivers, transcribes them on the CPU, extracts
locations, incident types, units and railroad context with one batched Claude Haiku 5.5
call, geocodes with free providers, correlates transmissions into incidents, and serves
them to a MapLibre map and to the godseye gateway's MCP tools.

Personal, non-commercial project. Runs on a shared home server without a GPU.

```
rtl_airband / trunk-recorder ──▶ faster-whisper (CPU int8) ──▶ prefilter ──▶ Claude Haiku 5.5 (10 per call)
traintap (EOT/HOT) ──▶ rail_telemetry          rules fallback ◀── caps/errors ──┘        │
                                                $0 geocoders ◀── structured location ◀──┘
                                                correlation (deterministic) ──▶ PostGIS ──▶ API 127.0.0.1:7710
```

Docs: [architecture](docs/architecture.md) · [SDR ingest](docs/sdr-ingest.md) ·
[cost controls](docs/cost-controls.md) · [API](docs/pulse-api.md)

## What changed in the 2026-11 rework

| Before | Now |
|---|---|
| Broadcastify CDN streams via ffmpeg, 25 s chunks | Own receivers: rtl_airband one-file-per-transmission (+ trunk-recorder), traintap EOT/HOT. Broadcastify capture removed (Terms §9 forbid automated capture and AI transcription). |
| Whisper large-v3 on CUDA fp16, torch | faster-whisper small.en (or medium.en) on CPU int8, Silero VAD via onnxruntime, no torch |
| Qwen2.5-7B + LoRA via Ollama, 3-6 calls per chunk | One structured Claude Haiku 5.5 call per 10 transmissions, rule prefilter, daily/monthly caps, rules-only fallback |
| Google geocoding | $0 chain: local index (OSM + TIGER + Detroit/Oakland address points, [docs/geocoder.md](docs/geocoder.md)), US Census, Photon, optional Nominatim |
| Redis-held incidents, three resolvers | Postgres/PostGIS is the source of truth; one resolver |
| Mapbox (token) | MapLibre + OpenFreeMap (keyless) |
| Unauthenticated /admin and /audio/stream | Bearer-token API, admin off unless configured, no audio relay |

## Requirements

* Ubuntu 22.04 (or similar), Docker with `docker-compose` (v1 or v2), [uv](https://docs.astral.sh/uv/),
  Node 20.19+ for the UI build.
* Ports on 127.0.0.1: 5436 (PostGIS), 6381 (Redis), 7710 (API).
* For live data: RTL-SDR receivers running rtl_airband and/or traintap (see
  [docs/sdr-ingest.md](docs/sdr-ingest.md)). Without them, use the fixture source.

## Setup

```bash
cp .env.example .env && chmod 600 .env   # set ANTHROPIC_API_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD,
                                          # PULSE_API_TOKEN (shared with the gateway)
uv sync                                   # Python 3.12, CPU-only dependencies from uv.lock
docker-compose up -d                      # PostGIS 127.0.0.1:5436, Redis 127.0.0.1:6381
uv run alembic upgrade head               # fresh schema

# local, gitignored config (the committed files are generic examples):
cp config/areas.yaml config/areas.local.yaml          # your coverage areas + street vocab
cp config/channels.yaml config/channels.local.yaml    # your SDR channels
uv run python scripts/import_fcc_uls.py --uls-dir /path/to/l_LMpriv   # rail plan from FCC ULS
uv run python scripts/build_fewshots.py --train /path/to/normalize_train.jsonl \
    --eval /path/to/normalize_eval.jsonl                               # TRAIN-only few-shots
nice -n 19 uv run python scripts/build_geocoder_index.py   # local geocoder index -> ~/.local/share/detroit-pulse/geocoder

# UI (served by the gateway from frontend/dist at /pulse/)
cd frontend && npm ci && nice -n 19 npx vite build && cd ..
```

Optional: restore the old database for analysis only (never into the live DB):
`scripts/restore_archive.sh /path/to/detroitpulse.dump` creates `detroitpulse_archive`.

## Run

```bash
./start.sh                    # compose + migrations + API + pipeline, all at nice 19
./start.sh api                # API only
STAGES=extract,resolver ./start.sh
./stop.sh                     # stop API + pipeline (add --services to stop the containers)
```

Or by hand:

```bash
nice -n 19 uv run python -m api                                   # 127.0.0.1:7710
nice -n 19 uv run python run_pipeline.py                          # ingest, transcribe, extract, resolver
uv run python run_pipeline.py --stages ingest,extract --sources fixture   # demo without radios
```

Gateway side (`godseye-detroit-connector`): `PULSE_URL=http://127.0.0.1:7710`, the same
`PULSE_API_TOKEN`, `PULSE_STATIC_DIR=<repo>/frontend/dist`, `PULSE_API_PROXY=true`.

### In containers (godseye stack)

```bash
cd deploy && ./setup.sh --from-dotenv ../.env     # internal network, volumes, mode-600 secrets
docker-compose -p pulse build && docker-compose -p pulse up -d
```

Hardened containers (non-root, read-only roots, no capabilities, limits, no published ports):
pulse-api on the `godseye_backend` network as `http://pulse-api:7710`, two pipeline workers,
PostGIS and Redis on the existing data volumes, optional UI volume for the gateway, and an `sdr`
profile for rtl_airband/traintap. The connector needs a small compose change (a patch, not yet
applied). Everything is in [docs/godseye-integration.md](docs/godseye-integration.md).
For UI development, `cd frontend && npm run dev` proxies `/pulse/api` to the local API.

## Tests and evals

```bash
uv run pytest -m "not whisper"          # unit + DB tests (uses detroitpulse_test; mocked Claude)
nice -n 19 uv run pytest -m whisper     # real faster-whisper run from a local model cache
uv run python scripts/measure_prefilter.py --train …/normalize_train.jsonl --archive-db detroitpulse_archive
uv run python scripts/eval_extract.py --eval …/normalize_eval.jsonl --mock rules   # $0
uv run python scripts/eval_extract.py --eval …/normalize_eval.jsonl --real --limit 10   # one real batch, <= $0.25 meter
uv run python scripts/eval_geocoder.py  # local geocoder vs 105 public phrases with ground truth (no network)
```

No test ever calls the real Claude API; the eval's real mode runs exactly one batch under
its own spend meter.

## Privacy and licensing

* This repository is public. Personal coverage points, real channel plans and few-shot
  transcripts go only in gitignored `config/*.local.*` files; never commit `.env`, audio,
  dumps or `data/`.
* No Broadcastify audio, no RadioReference data (its terms forbid AI/ML use), no Google
  geocoding results.
* Map tiles: © OpenStreetMap contributors, OpenMapTiles, served by OpenFreeMap.
* Geocoding: OpenStreetMap (ODbL, attribution shown on the map; the built index stays private),
  TIGER/Line and City of Detroit data (public domain), Oakland County Open Data (terms of use).
  The index and its downloads live outside the repo. Licences: [docs/geocoder.md](docs/geocoder.md).

## Repository layout

| Path | |
|---|---|
| `core/` | settings, DB, Redis bus, YAML config loaders, durable work queue |
| `ingest/` | sources: `sdr_files`, `traintap`, `fixture`, disabled `broadcastify_legacy` |
| `transcribe/` | faster-whisper worker and vocabulary prompt |
| `extract/` | prefilter, batched Claude extractor, schema, prompt, cost meter, rules fallback, summaries, worker |
| `rail/` | defect-detector parser, EOT interpretation, spoken-number parsing |
| `geocode/` | provider interface, local index (`geocode/local/`), Census/Nominatim/Photon, Postgres cache, chain |
| `correlation/` | deterministic correlation engine and resolver |
| `api/` | FastAPI app: `/v1` gateway contract, UI routes, admin, WebSocket |
| `db/` | ORM models, alembic migrations, `schema.sql` reference |
| `frontend/` | React + MapLibre UI (Vite, base `/pulse/`) |
| `scripts/` | archive restore, FCC ULS import, few-shot builder, geocoder index build, rail.conf renderer, prefilter/extraction/geocoder evals |
| `deploy/` | container stack: Dockerfiles, compose (+ verify override), setup script, SDR configs, secrets README |
| `lora/` | legacy QLoRA/Ollama tooling from the GPU era; not used by this pipeline and its dependencies aren't installed |
