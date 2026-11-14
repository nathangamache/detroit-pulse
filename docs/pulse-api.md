# Pulse HTTP API (127.0.0.1:7710)

Pulse implements the gateway's contract, `docs/pulse-api-contract.md` in
`godseye-detroit-connector` (branch `initial-gateway`, v1, with the additive v1.2
`verdicts` on `rail_activity_near`). This page records what Pulse actually returns,
including the few additions.

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
2. **A channel counts for the point only when its own receiver area covers it**
   (`core/receivers.py` `channel_covers_point`), never because a name matches and never by
   a fixed distance:
   * the channel's own area, when it has one: its `bias` point and/or its `area_id` area's
     center, with `coverage.radius_km` (else the area's radius). It decides alone: a
     subdivision never stretches it;
   * otherwise a subdivision it is on (linked by `channel_id` in the rail plan, or one of its
     `subdivision` / `coverage.lines` names or aliases is the subdivision's) whose
     center+radius or bbox contains the point, and no farther from that subdivision's center
     than the channel's own `coverage.radius_km` when it gives one;
   * otherwise no point: a `bias` without a radius, or a radius without a center, reaches
     nothing.

   Voice channels are every `rail_*` channel that covers the point (the `railroad` hint
   keeps that railroad's). The same rule places `eot` channels (any railroad's): one with
   `bias` and `coverage.radius_km: 3` covers 3 km around its bias, not 25 km, and an eot
   channel with no area on record covers no point (its telemetry is no longer "shown
   unfiltered"). Transcripts, recordings, detector readouts and packets from a channel that
   doesn't cover the point are never reported for it. So a point outside every covered area
   gets `covered: false`, no channels, transcripts, detector readouts or `eot_hot`, and, when
   the `subdivision` hint names a monitored line, a note that no receiver for that line covers
   this point.
3. Each `coverage.channels[]` entry has an extra `receiver_state` (`not_installed` /
   `offline` / `online`, as in `feeds_status` below). `coverage.covered` is true only when a
   receiver has reported for at least one enabled, non-example matched channel (state
   `online` or `offline`): a configured channel with no receiver is not coverage. `note`
   says which case applies: no voice channel for the line ("No monitored rail voice
   channel ..."), configured but no receiver ever reported (no transcripts means no
   coverage, not a quiet line), receiver offline (with the last time it was heard), or
   receiver online with nothing heard; the same for EOT/HOT, and "No EOT/HOT receiver covers
   this point" when no eot channel's area reaches it. `receiver_state: "online"` means
   *listening now*, not *quiet*: the note calls channels quiet only when the `verdicts`
   proof (item 8) holds for them for the whole window, and otherwise says the receiver "is
   online now, but Pulse can't vouch for these channels across the whole window". When a
   point is only partly listened to (say 2 of 11 channels in rtl_airband's stats file), the
   note says the receiver is "online for 2 of 11 channels on this line (<labels>)", that those
   were quiet if they are proven and nothing was heard, and "Not covered: 9 channels with no
   receiver ... and K channels whose receiver is offline (last heard ...)". `covered` keeps
   its meaning above. Detector readouts are the ones heard on a covering channel (and old
   rows with no channel on record that name one of the point's subdivisions or aliases).
   **Any recording on those channels in the window is traffic, never quiet**, text or not:
   one still `queued`/`transcribing`, one whose transcription failed (`error`), or one whisper
   found no speech in (`done` with an empty transcript) isn't in `transcripts` but is
   counted in `coverage.recordings_without_text` (`{"pending", "error", "no_speech"}`, only
   when one of them is non-zero), and the note says e.g. "5 recordings not transcribed yet
   ... That is radio traffic, not a quiet line." instead of "quiet".
4. `transcripts`: rail traffic on those channels, newest first, each with `reason_label`.
5. `detector_readouts` from `rail_detector_events`; `defects` lists alarm types (`[]` = no
   defects; `["integrity_failure"]` when the detector reported a failure).
6. `eot_hot` from `rail_telemetry` (EOT and HOT; DPU omitted), packets of the covering eot
   channels only (`rail_telemetry.channel` is the channel id, `TRAINTAP_CHANNEL`).
   Additions: `status` (`moving`, `stopped_service`, `emergency_stopped`, `no_air`, ...) and
   `emergency`.
7. `reason_labels`: label weights over the window (Claude-labelled transmissions count 1.0,
   rules 0.5, detector alarms 0.5, an EOT emergency 2.0), normalised to `confidence`.
8. `verdicts` (contract v1.2, additive; `api/rail_verdicts.py`): per kind, may the point be
   called quiet? The gateway relays `state`, `reasons` and `counts`; it may downgrade
   `quiet` to `unknown` when its own checks disagree, never upgrade anything to `quiet` or
   `activity`, and it must never say quiet for a Pulse without `verdicts`.

   ```ts
   verdicts: { rail_voice: V; eot: V }
   V = {
     state: "activity" | "quiet" | "not_covered" | "unknown";
     reasons: string[];            // short, plain sentences ("csx_road: no receiver has ever reported")
     window_start: string;         // ISO 8601 UTC: now - since_minutes
     window_end: string;           // now; for quiet, where the proof ends (see below)
     listening_since?: string;     // since when every covering channel and the ingest stage are proven
     counts: { transcripts: number; detector_readouts: number;
               recordings_without_text: number; eot_packets: number };   // covering channels only
     channels: { feed_id: string; receiver_state: "not_installed" | "offline" | "online";
                 listening_since?: string; covers_point: boolean }[];    // every enabled, non-example
   }                                                                     // channel of the kind (voice:
                                                                         // the railroad hint's)
   ```

   * `activity`: anything in the window on a channel that covers the point: a transcript, a
     detector readout, a recording with no text (queued, transcribing, failed, no speech, or
     still being written at the ingest stage's last beat) or an EOT/HOT/DPU packet. Channels
     that don't cover the point never count.
   * `not_covered`: no channel of the kind covers the point (item 2), or none that does was
     listened to at any time in the window (no receiver, offline, or a stats file that names
     the channel on another frequency).
   * `quiet`, only on positive proof for the **whole** window, all of:
     1. at least one channel of the kind covers the point, and **every** covering channel
        is proven (2, 3 and 6);
     2. its receiver was listening since before `window_start`: the receiver's heartbeat
        history (the `liveness` table, [sdr-ingest.md](sdr-ingest.md#8-heartbeat-history-and-the-ingest-heartbeat))
        shows one unbroken run from before `window_start` to now, and its heartbeat file and
        start marker agree right now. A run breaks at a gap longer than a missed beat or two
        of that receiver (45 s for rtl_airband's 15 s stats file, 105 s for traintap's
        minute), at an rtl_airband counter that went down, and at every restart, however
        quickly it came back: the receiver's entrypoint writes a start marker
        (`<name>.started`, a new boot id) before each start, and a different one ends the run.
        A receiver that writes no start marker never proves a window (its restarts can't all
        be seen: traintap's heartbeat is a bare touch, rtl_airband's counters may all be 0).
        A receiver that came back during the window is listening only since then. What a
        heartbeat can't show is a receiver that stalls without restarting for less than its
        max gap, counted from its last write: up to about 45 s (rtl_airband) or 105 s
        (traintap);
     3. the receiver demodulates the configured frequency and files it under a channel that
        covers the point: its stats file names the channel (label = channel id) with the
        channel's `freq_mhz` within 3 kHz, during that run and now, or the frequency is
        demodulated once under another enabled channel that covers the point too. A label on
        another frequency, or the frequency filed under a disabled channel (a rename without
        re-rendering `rail.local.conf`), means it is *not* listening to that channel;
     4. the ingest stage (pulse-transcribe's `sdr_files` for voice, `traintap` for EOT) beat
        since before `window_start`, within the last 35 s (two missed beats end its run), and
        could see its input (watch directory / CSV);
     5. nothing was heard on the covering channels, and nothing in the window is left
        unattributed: no rail-band recording that no configured channel claims
        (`unmapped:*`) or that the ingest stage dropped because its channel is disabled, no
        recording ingested in the window but stamped before it (a transmission across the
        window start, or `SDR_FILENAME_TZ` not matching rtl_airband's file names), no
        detector readout without a channel, no packet that no configured eot channel claims;
     6. rail voice: the receiver heard nothing Pulse didn't get. rtl_airband's stats file
        counts each channel's squelch openings (`channel_squelch_counter`), the transmissions
        it dropped as shorter than `split_tx_drop_shorter_than`
        (`channel_split_tx_dropped_counter`) and the output loops with the squelch open
        (`channel_activity_counter`). While a channel's file output works, every opening it
        doesn't drop starts or continues a recording, and the squelch is never open without
        one being written. rtl_airband turns a channel's file output off for good after one
        failed open or short write (a full disk, the recordings volume's permissions) and
        keeps demodulating. The ingest stage records how those counters grew at each sample
        (`receiver_squelch`), and the stats file read now adds what it counted since; a
        covering channel with no recording at all in the window (with text or not, still being
        written, or stamped before the window) is `unknown` when, in the window, its squelch
        opened more often than it dropped ("the receiver opened squelch N times on <channel>
        in the window but no recording reached Pulse"), or was open with no opening and no
        drop ("the receiver's squelch was open on <channel> in the window (a transmission that
        began before it) but no recording reached Pulse"). Quiet also needs the squelch
        counter: a stats file without it for a covering channel, or a plain heartbeat, can't
        show this, and the channel isn't proven.

     The quiet claim then covers `window_start` to `window_end`, which is **not** the request
     time but where the proof ends: the ingest stage's newest beat (stamped with when its
     poll began, so it has looked at nothing later) and the covering receivers' newest
     heartbeats, whichever is earliest. That is normally under 20 s before `generated_at` for
     rail voice and up to about a minute for EOT/HOT (traintap's heartbeat is once a minute),
     at most the receiver's max gap (45 s, 105 s); the reason says how far ("up to
     <window_end> (N s before this answer ...)"). A gateway relays it as "quiet from
     <window_start> to <window_end>", and a check of the window's length must allow that much.
     `sdr_files` beats at once when a transmission starts, so a transmission after
     `window_end` shows up within seconds.
   * `unknown`: nothing was heard, but one of those isn't shown; `reasons` says which
     (e.g. "receiver listening only since …, after the window start", "the receiver writes no
     start marker (traintap-eot.started), so a restart in the window can't be ruled out",
     "the receiver opened squelch 5 times on csx_road in the window but no recording reached
     Pulse (…)", "ingest (sdr_files) has
     no current heartbeat", "the receiver reports this channel on 161.1 MHz, not the
     configured 160.215 MHz").

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
  * `online`: fresher than that. A fresh heartbeat with 0 chunks means a receiver is
    listening *now*, not that the channel was quiet over a window: that is what
    `/v1/rail/activity` `verdicts` decide, from the heartbeat history.
  * rtl_airband's heartbeat names the channels it demodulates (label = channel id, with its
    frequency); a feed it doesn't list, or lists on another frequency than `freq_mhz` (e.g.
    `rail.local.conf` wasn't re-rendered after adding or changing it), gets no heartbeat from
    it and stays `not_installed` / `offline`. A feed it doesn't label is heard through the
    channel its frequency is filed under: an enabled one (a detector on the road channel's
    frequency) counts, a disabled one (an id renamed, the old entry kept `enabled: false`,
    `rail.local.conf` not re-rendered) doesn't ([sdr-ingest.md](sdr-ingest.md#6-receiver-heartbeats)).
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
