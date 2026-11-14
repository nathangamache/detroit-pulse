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

1. Subdivisions are taken from `config/rail_channels(.local).yaml`: only those whose
   center+radius or bbox contains the point. The `railroad` hint keeps that railroad's; when
   it matches none, none is left (never all of them). The `subdivision` hint never adds a line
   the point is outside: it puts the line it names first, and it matches a name **or alias**
   exactly once both are normalised (case, punctuation, a trailing "(CSXT)" owner tag and the
   word "Sub"/"Subdivision" ignored; never a substring, so "est" names nothing), so the
   gateway's NARN-based hint finds a line that FRA names differently when the alias is
   configured.
2. Monitored voice channels are the ones linked from those subdivisions (`channel_id`) or
   whose `subdivision` or `coverage.lines` names in `config/channels(.local).yaml` match one
   of the subdivision's names (railroad permitting). A channel the `subdivision` hint names
   (its own line names, or a subdivision the hint names) is added only when its own receiver
   area contains the point: its `bias` point or `area_id` area, with `coverage.radius_km`
   (else the area's radius), or a subdivision it is on whose area contains the point. A name
   match alone is never coverage. So a point outside every monitored area gets
   `covered: false`, no voice channels, transcripts or detector readouts, and, when the hint
   names a monitored line, a note that no receiver for that line covers this point. EOT/HOT
   telemetry is included when an `eot` channel exists and either has no `bias` point (shown
   unfiltered, with a note) or is within 25 km.
3. Each `coverage.channels[]` entry has an extra `receiver_state` (`not_installed` /
   `offline` / `online`, as in `feeds_status` below). `coverage.covered` is true only when a
   receiver has reported for at least one enabled, non-example matched channel (state
   `online` or `offline`): a configured channel with no receiver is not coverage. `note`
   says which case applies: no voice channel for the line ("No monitored rail voice
   channel ..."), configured but no receiver ever reported (no transcripts means no
   coverage, not a quiet line), receiver offline (with the last time it was heard), or
   receiver online with nothing heard (the channels were quiet); the same for EOT/HOT, and
   "EOT/HOT receiver location not configured" when the eot channel has no `bias`. Only a
   channel whose `receiver_state` is `online` is ever called quiet: when a line is only
   partly listened to (say 2 of 11 channels in rtl_airband's stats file), the note says the
   receiver is "online for 2 of 11 channels on this line (<labels>)", that those were quiet
   if nothing was heard, and "Not covered: 9 channels with no receiver ... and K channels
   whose receiver is offline (last heard ...)". `covered` keeps its meaning above.
   Detector readouts also match the subdivision's aliases.
   **Any recording on those channels in the window is traffic, never quiet**, text or not:
   one still `queued`/`transcribing`, one whose transcription failed (`error`), or one whisper
   found no speech in (`done` with an empty transcript) isn't in `transcripts` but is
   counted in `coverage.recordings_without_text` (`{"pending", "error", "no_speech"}`, only
   when one of them is non-zero), and the note says e.g. "5 recordings not transcribed yet
   ... That is radio traffic, not a quiet line." instead of "quiet".
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

**Receivers and coverage** (shared with the gateway; additive, so an older gateway ignores
them, and a gateway talking to an older Pulse treats their absence as "unknown"). `state`
alone can't tell "nothing was said" from "nothing was listening"; these fields can:

```ts
feeds[]: {
  ...                                    // every field above, unchanged
  example: boolean;                      // shipped placeholder: id "example_*" or `example: true`
  receiver: {
    state: "not_installed" | "offline" | "online";
    last_seen_at?: string;               // ISO 8601 UTC: newest heartbeat or receiver output
    device?: string;                     // e.g. "rail-airband: RTL-SDR serial RAIL160 (rtl_airband)"
  };
  coverage: {                            // from config; every key optional, {} = unknown
    area?: string;
    lines?: { railroad: string; name: string; aliases: string[] }[];   // rail: what it hears
    radius_km?: number;
  };
}
coverage_summary: {                      // top level
  rail_radio: S; eot: S; public_safety: S;
}   // S = "no_receiver" | "receiver_offline" | "listening" | "not_configured"
```

* `receiver.state`, per feed. Each feed has a receiver: `receiver:` on the channel, else by
  kind (`rail_*` voice channels: `rail-airband`; `eot`: `traintap-eot`; others: none).
  Evidence is that receiver's heartbeat file (see [sdr-ingest.md](sdr-ingest.md#6-receiver-heartbeats))
  and the feed's own output: recordings ingested by `sdr_files` (7 days) or traintap packets.
  Fixture replays and legacy imports are not evidence.
  * `not_installed`: no receiver has ever reported for this feed (no heartbeat file, no output).
    This is the state of every feed until the SDR hardware is plugged in and the `sdr` profile runs.
  * `offline`: the newest evidence is older than `RECEIVER_STALE_S` (default 300 s).
  * `online`: fresher than that. **A fresh heartbeat with 0 chunks means the channel is
    quiet, not uncovered.**
  * rtl_airband's heartbeat names the channels it demodulates; a feed it doesn't list (e.g.
    `rail.local.conf` wasn't re-rendered after adding it) gets no heartbeat from it and stays
    `not_installed` / `offline`.
* `coverage`: the channel's `coverage:` block (`area`, `radius_km`, `lines` with `aliases`:
  every other name the same track has in other datasets, e.g. FRA vs NARN). Without one,
  `lines` come from the rail plan (subdivisions linked to the channel, with their `aliases`)
  and `area`/`radius_km` from the channel's `area_id` (else `area` = its `city`).
* `example: true` for the shipped placeholder feeds in the committed `config/channels.yaml`
  (ids starting `example_`, or `example: true`). They never count as coverage.
* `coverage_summary`, per group (`rail_radio`: every `rail_*` voice kind; `eot`;
  `public_safety`: everything else), over **enabled, non-example, configured** feeds
  (unmapped ones don't count): `listening` if any receiver is online, else
  `receiver_offline` if any is offline, else `no_receiver`; `not_configured` when the group
  has no such feed (or only examples).

With only the committed config (no `channels.local.yaml`) the summary is
`{"rail_radio": "not_configured", "eot": "no_receiver", "public_safety": "not_configured"}`:
the generic `eot_hot` feed is real (EOT frequencies are standard), the rest are examples.

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
