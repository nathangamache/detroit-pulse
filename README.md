# Detroit Pulse

A real-time public safety dashboard for metro Detroit. It pulls live audio from 15+ Broadcastify scanner feeds across Wayne, Oakland, and Washtenaw counties, runs it through a local ML pipeline, and puts incidents on a map as they happen.

Built as a personal project alongside school. Non-commercial.

---

## How it works

The system listens to scanner radio continuously. When a call comes in, it transcribes the audio, figures out the address, geocodes it, classifies the incident, and correlates it with any other transmissions about the same event. The whole thing takes a few seconds from when the dispatcher speaks to when the dot appears on the map.

The hardest part is not the transcription. It's figuring out that two separate radio transmissions 9 seconds apart are both about the same structure fire on 8 Mile, not two different incidents.

---

## Pipeline

```
Broadcastify streams
        |
  1. Audio Ingest        -- 30-second chunks from 15+ CDN streams
        |
  2. Whisper large-v3    -- local transcription
        |
  3. Address Normalize   -- Qwen2.5-7B + LoRA adapter
        |
  4. Geocoding           -- Photon (primary), Google (fallback)
        |
  5. Incident Structure  -- type, priority, units extracted via LLM
        |
  6. Correlation Engine  -- signal scoring + LLM judge
        |
  7. WebSocket           -- live events to React/Mapbox frontend
```

---

## Stack

| Layer | Tech |
|-------|------|
| Transcription | Whisper large-v3 |
| LLM | Qwen2.5-7B-Instruct via Ollama |
| Address normalization | Qwen2.5-7B + QLoRA (custom trained) |
| Geocoding | Photon, Google Geocoding API |
| Backend | FastAPI, Python 3.12 |
| Database | PostgreSQL |
| Cache / pub-sub | Redis |
| Frontend | React, Mapbox GL JS, Vite |

---

## The LoRA adapter

Address normalization is where I spent the most time. Scanner radio is not natural language. It's full of unit callsigns, signal codes, floor numbers, and round counts that a general LLM reads as street addresses. The base Qwen model hallucinated an address in about half the cases where the right answer was `NO_LOCATION`.

I built a QLoRA fine-tuning pipeline to fix this. The training data comes from a combination of auto-labeled high-confidence geocodes, Claude Sonnet reviewed corrections, and hand-crafted hard negatives targeting the specific failure modes.

The most interesting finding: removing the auto-labeled examples where Qwen had already gotten the address right and training only on the cases where the model had something to learn produced better results with 10x less data. Training on what the model already knows does nothing.

The labeling pipeline uses Claude Sonnet to auto-review ambiguous transcripts at scale, targeting mile-road hallucinations, MEDIUM confidence geocodes, and NO_LOCATION predictions from high-activity feeds.

Training infrastructure lives in `lora/` and supports automatic GPU profile detection, quality-gated dataset export, offline eval, shadow mode deployment, and gated promotion with instant rollback.

---

## Correlation engine

Two-layer approach:

**Signal scoring** -- six weighted signals produce a score per candidate incident: address token overlap, geocode proximity, unit ID match, incident type match, burst window (same feed within 30s), same feed bonus. Above 0.85 merges automatically. Below 0.20 creates a new incident.

**LLM judge** -- scores between 0.20 and 0.85 go to Qwen with both transcripts for a binary same/different decision. The judge is prompted conservatively; it needs explicit evidence to merge, not just absence of evidence to split.

High-volume feeds (Detroit Police/Fire, Wayne County) get stricter treatment because they see the most duplicate transmissions.

---

## Feeds

**Wayne County:** Detroit Police/Fire, DPD Dispatch, Detroit Fire, Detroit EMS, Plymouth-Northville, Northville City, Downriver, Wayne County Public Safety

**Oakland County:** Oakland County dispatch

**Washtenaw County:** Washtenaw County dispatch

---

## Setup

### Prerequisites

- Python 3.12
- Node.js 20+
- PostgreSQL 15+
- Redis 7+
- Ollama with `qwen2.5:7b-instruct` pulled
- Broadcastify premium account
- Google Geocoding API key (optional)

### Environment

Copy `.env.example` to `.env`:

```env
DATABASE_URL=postgresql://user:pass@localhost:5432/detroitpulse
REDIS_URL=redis://localhost:6379/0
OLLAMA_BASE_URL=http://localhost:11434
GOOGLE_GEOCODING_API_KEY=
BROADCASTIFY_USERNAME=
BROADCASTIFY_PASSWORD=

LORA_BASE_MODEL=qwen2.5:7b-instruct
LORA_NORMALIZE_MODEL=detroit-normalize-v4
LORA_NORMALIZE_ENABLED=false
LORA_SHADOW_MODE=false
```

### Install

```bash
# Database
psql $DATABASE_URL -f db/schema.sql

# Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Frontend
cd frontend && npm install && npm run build
```

### Run

```bash
./start.sh
./stop.sh
```

---

## Training your own adapter

```bash
# Label data
python lora/auto_label.py --limit 500

# Export dataset
python lora/export_dataset.py

# Train
python lora/train_lora.py

# Export for Ollama
python lora/export_gguf.py --version normalize-v1

# Eval
python lora/eval_baseline.py --lora-model detroit-normalize-v1

# Shadow mode then promote
python lora/promote_model.py --version normalize-v1
```

GPU profile is auto-detected. Override with `GPU_PROFILE=a100` or `GPU_PROFILE=4070`.

---

## Notes

Not affiliated with any law enforcement agency, municipality, or Broadcastify. Non-commercial use only.

Scanner data is public by nature. These are unencrypted radio transmissions. The dashboard does not store audio or provide any capability beyond what anyone with a radio scanner already has access to.