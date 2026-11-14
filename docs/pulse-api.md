# Pulse HTTP API (127.0.0.1:7710)

Pulse implements the gateway's contract, `docs/pulse-api-contract.md` in
`godseye-detroit-connector` (branch `initial-gateway`, v1). This page records what Pulse
actually returns, including the few additions.

Run it with `uv run python -m api` (or `./start.sh api`). It refuses to bind anything but a
loopback address unless started with `--allow-non-loopback` (for a private container network).

## Auth

| Setting | Effect |
|---|---|
| `PULSE_API_TOKEN` set | Every route except `/v1/health` and `/health` needs `Authorization: Bearer <token>`, including the `/ws` WebSocket handshake (closed with 4401 otherwise). The gateway adds this header when it proxies `/pulse/api/*`. |
| `PULSE_API_TOKEN` unset | Open. For local development only; `python -m api` prints a warning. |
| `PULSE_ADMIN_TOKEN` unset | Every `/admin/*` route returns 404. |
| `PULSE_ADMIN_TOKEN` set | `/admin/*` needs `X-Pulse-Admin-Token: <token>`. The gateway blocks `/admin`, so admin is reachable only on the box. Destructive calls also need `?confirm=`. |

Token comparisons are constant-time. `/docs` and `/openapi.json` are off unless
`PULSE_DOCS_ENABLED=true`. There is no `/audio/stream` any more.

Errors always look like `{"error": {"code": "bad_request|unauthorized|forbidden|not_found|unavailable|internal", "message": "..."}}`.
Validation failures are 400 `bad_request`, not FastAPI's 422.

## Contract endpoints (`/v1`)

All responses carry `generated_at` (ISO 8601 UTC, `Z`). Radio-derived text is in
`*_untrusted` fields only.

| Tool | Route | Notes |
|---|---|---|
| health | `GET /v1/health` | `{"ok", "version", "generated_at"}`; `ok` is false when Postgres is unreachable. |
| `incidents_near` | `GET /v1/incidents/near?lat&lon&radius_km=3&since_minutes=60&limit=25` | PostGIS `ST_DWithin` on `incidents.location`, nearest first; incidents whose `last_updated` falls in the window. Ranges: radius 0.1-50, minutes 1-1440, limit 1-100. |
| `incident_detail` | `GET /v1/incidents/{id}` | Incident plus up to 100 transcripts, chronological. 404 `not_found`. |
| `search_transcripts` | `GET /v1/transcripts/search?q&since_minutes=60&channel&limit=25` | Postgres full-text (`websearch_to_tsquery` on a generated `tsvector`) OR an escaped `ILIKE`, newest first. `channel` matches the channel id. |
| `rail_activity_near` | `GET /v1/rail/activity?lat&lon&since_minutes=30&subdivision&railroad` | See below. |
| `feeds_status` | `GET /v1/feeds/status` | See below. |

Incident mapping: `type` is the lower-cased incident type (`TRAFFIC_ACCIDENT` becomes
`crash`, `RAIL_*` becomes `rail`); `priority`/`location_confidence` are lower-cased and
omitted when unknown; `feed_id` is the channel id; `summary_untrusted` is the summary on
close, else the latest per-transmission short summary.

### `rail_activity_near`

1. Subdivisions are taken from `config/rail_channels(.local).yaml`: those whose center+radius
   or bbox contains the point, plus any whose name contains the `subdivision` hint; the
   `railroad` hint narrows them.
2. Monitored voice channels are the ones linked from those subdivisions (`channel_id`) or
   whose `railroad`/`subdivision` in `config/channels(.local).yaml` match. EOT/HOT
   telemetry is included when an `eot` channel exists and either has no `bias` point or is
   within 25 km.
3. `coverage.covered` is false (with a `note`) when no voice channel covers the line yet.
4. `transcripts`: rail traffic on those channels, newest first, each with `reason_label`.
5. `detector_readouts` from `rail_detector_events`; `defects` lists alarm types (`[]` = no
   defects; `["integrity_failure"]` when the detector reported a failure).
6. `eot_hot` from `rail_telemetry` (EOT and HOT; DPU omitted). Additions: `status`
   (`moving`, `stopped_service`, `emergency_stopped`, `no_air`, ...) and `emergency`.
7. `reason_labels`: label weights over the window (Claude-labelled transmissions count 1.0,
   rules 0.5, detector alarms 0.5, an EOT emergency 2.0), normalised to `confidence`.

Pulse's internal reason labels map to the contract's as follows:

| internal (extraction schema) | contract |
|---|---|
| `meet_or_hold` | `meet_pass` if the text mentions a meet/opposing train/siding, else `dispatcher_hold` |
| `signal_problem` | `signal_stop` |
| `defect_detector` | `defect_detector_stop` |
| `crew_change_hos` | `crew_change` |
| `mechanical` | `mechanical` |
| `crossing_incident` | `blocked_crossing` |
| `trespasser` | `emergency` when the transmission is HIGH priority, else `unknown` |
| `switching_yard` | `switching` |
| `unknown` | `unknown` |
| (any, text mentions track and time / form B / foreman / hi-rail) | `track_work` |
| (EOT emergency in the window) | `emergency` |

### `feeds_status`

One entry per channel in `config/channels(.local).yaml`, plus channels seen in the last 7
days that aren't configured (`"unmapped": true`). `state`: audio channels are `up` with a
transmission in the last 2 h, `degraded` within 24 h, else `down` (a quiet channel can be
healthy, so treat `down` as "silent"); the `eot` channel uses traintap's heartbeat file
(`up` < 3 min, `degraded` < 15 min). `source` is `sdr:rtl_airband`, `sdr:trunk-recorder` or
`sdr:traintap`. Additions: per-feed `backlog`, and a top-level `backlog` with queue counts
by status and `oldest_waiting_s`.

## UI routes

Used by the bundled React app (served by the gateway at `/pulse/`, API at `/pulse/api`):
`GET /incidents/active`, `/incidents/stats/summary`, `/incidents/{id}`,
`/incidents/{id}/chunks`, `/transmissions/recent`, `/channels`, `/rail/recent`,
`/coverage`, `/external/{fire-stations,precincts,battalions,counties,dte-outages}`, and
WebSocket `/ws` (events `incident:new|update|resolve|delete`, `rail:telemetry`,
`rail:detector`, `pipeline:debug`).

## Admin routes (`X-Pulse-Admin-Token`)

`GET /admin/overview`, `GET /admin/llm/usage?days=30`, `GET /admin/transmissions/errors`,
`POST /admin/transmissions/{id}/requeue?stage=extract|transcribe`,
`POST /admin/incidents/{id}/resolve`, `DELETE /admin/incidents/{id}`,
`POST /admin/incidents/merge {source_id, target_id}`,
`DELETE /admin/reset/all?confirm=RESET-LIVE-DATA` (keeps the `llm_usage` ledger so caps
still hold).
