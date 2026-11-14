# SDR ingest: rtl_airband and traintap into Pulse

Pulse's audio comes from your own receivers. No hardware is attached yet; everything
below is built and tested against fixtures, and goes live by pointing the SDR software at
the directories Pulse watches. Until then `/v1/feeds/status` reports every feed's receiver
as `not_installed` ([6](#6-receiver-heartbeats), [7](#7-from-not_installed-to-listening)).

```
RTL-SDR #1 ── rtl_airband (multichannel NFM, split_on_transmission) ──▶ data/sdr/…/<template>_<time>.mp3
           └─ stats file every 15 s ─────────────────────────────────▶ data/receivers/rail-airband.prom
RTL-SDR #2 ── traintap (EOT 457.9375 / HOT 452.9375 MHz) ──────────────▶ data/traintap/trains.csv
           └─ heartbeat every ~60 s ─────────────────────────────────▶ data/receivers/traintap-eot.heartbeat
                                                                              │
                     INGEST_SOURCES=sdr_files,traintap  (run_pipeline.py --stages ingest)
                     /v1/feeds/status reads data/receivers (RECEIVER_STATUS_DIR)
```

## 1. Voice channels with rtl_airband

[RTLSDR-Airband](https://github.com/rtl-airband/RTLSDR-Airband) demodulates many
narrowband FM channels inside one dongle's ~2.4 MHz and, with `split_on_transmission`,
writes **one file per transmission**. That is exactly what Pulse wants: no chunking, no
VAD cut points, capture time in the filename.

Example `rtl_airband.conf` (one dongle covering two channels in the 160-161 MHz railroad
pool; frequencies are placeholders, use your own plan):

```
devices: ({
  type = "rtlsdr"; index = 0; gain = 30; centerfreq = 160.8; correction = 0;
  mode = "multichannel";
  channels: (
    { freq = 160.215; modulation = "nfm";
      outputs: ({ type = "file"; directory = "/home/<you>/projects/godseye/detroit-pulse/data/sdr";
                  filename_template = "rail_road"; split_on_transmission = true;
                  dated_subdirectories = true; }); },
    { freq = 161.250; modulation = "nfm";
      outputs: ({ type = "file"; directory = "/home/<you>/projects/godseye/detroit-pulse/data/sdr";
                  filename_template = "rail_dispatcher"; split_on_transmission = true; }); }
  );
});
```

What Pulse relies on (checked against RTLSDR-Airband `src/output.cpp`, master 2026-11-02):

* File name: `<filename_template>[_<tx start>]_<file open>[_<freq Hz>].mp3`, timestamps
  `%Y%m%d_%H%M%S` **in UTC** unless the global `localtime = true` (then set
  `SDR_FILENAME_TZ=America/Detroit`).
* The second and later parts of a transmission longer than `split_max_file_time` carry
  the transmission start first (with `split_include_transmission_start = true`); Pulse
  ingests each part as its own transmission.
* `_<freq Hz>` appears only with `include_freq = true`. Use it in **scan** mode, where
  one output covers several frequencies; Pulse then maps the file by frequency.
* While recording the file is `<name>.mp3.tmp`; Pulse ignores `.tmp` and also waits
  until a file hasn't changed for `SDR_MIN_FILE_AGE_S` (1.5 s).
* `rawfile` outputs (`.cf32` I/Q) are ignored.

Map templates to channels in `config/channels.local.yaml` (gitignored; copy the
structure from `config/channels.yaml`):

```yaml
channels:
  - id: csx_example_road          # stable id used in the API and database
    label: CSX <Sub> road channel
    kind: rail_road               # police|fire|ems|public_safety|rail_road|rail_dispatcher|rail_yard|
                                  # rail_detector|rail_voice (use not confirmed yet)|eot
    templates: [rail_road]        # rtl_airband filename_template(s)
    freq_mhz: 160.215             # also used to match include_freq files
    railroad: CSX
    subdivision: <Sub>
    county: Wayne
    receiver: rail-airband        # optional; the default for rail_* voice kinds (eot: traintap-eot)
    coverage:                     # optional; what /v1/feeds/status reports this channel hears
      area: <area name>
      radius_km: 5
      lines:
        - railroad: CSX
          name: <Sub>                     # as one dataset names it (e.g. FRA's crossing inventory)
          aliases: ["<OTHER NAME (CSXT)>"]  # every other name of the same track (e.g. NARN's)
    vocab: [<public station or CP names along the line>]
```

Use `kind: rail_voice` for a licensed frequency whose use (road, dispatcher, yard) you
haven't confirmed on air yet: it is recorded, transcribed and treated as rail like the
others, and reported as `road` in `/v1/rail/activity`. Change it once you've listened.

Files whose template matches nothing are still ingested as `unmapped:<template>` (with a
warning) so nothing is lost; they show up in `/v1/feeds/status` with `"unmapped": true`.

**Public-safety caveat.** Much of Wayne/Oakland/Washtenaw police and fire dispatch runs
on P25 trunked systems (MPSCS and others), which rtl_airband cannot decode. For those,
use [trunk-recorder](https://github.com/robotastic/trunk-recorder) writing call files into
the same directory: Pulse reads its `<talkgroup>-<epoch>_<freq>.wav|m4a` names and the
`.json` sidecar (talkgroup, freq, start_time) and maps talkgroups via `talkgroups:` in
`channels.local.yaml`. This path is parsed and unit-tested but not yet run against real
trunk-recorder output.

Transcription settings live in `.env` (`WHISPER_MODEL=small.en`, `int8`, 8 threads).
PyAV decodes mp3/m4a/wav in-process; no ffmpeg binary is needed. Set
`SDR_DELETE_AFTER_TRANSCRIBE=true` if you don't want to keep the recordings.

## 2. EOT/HOT telemetry with traintap

[traintap](https://github.com/trickv/traintap) decodes AAR S-9152 EOT/HOT/DPU packets
natively from I/Q. Run it on a second dongle (one dongle can't cover both 457.9375 and
452.9375 MHz at once):

```
traintap --csv /home/<you>/projects/godseye/detroit-pulse/data/traintap/trains.csv --mode eot
```

(or its Docker image with `./data/traintap` mounted at `/data`). It appends one row per
BCH-valid packet (`timestamp, epoch, source, freq_hz, valid, corrected, unit_addr,
pressure_psig, batt_charge_pct, batt_cond, message_type, arm_status, motion,
marker_light, turbine, valve_ckt, data_block, checkbits_rx`) and touches `heartbeat`
next to the CSV every minute (or the file given with `--heartbeat`; the container stack uses
`--heartbeat /status/traintap-eot.heartbeat`, see [6](#6-receiver-heartbeats)).

Pulse's `traintap` source (`TRAINTAP_CSV`) tails that file by byte offset (half-written
lines wait; traintap's header-change rotation to `trains.csv.N` is followed), stores each
packet once in `rail_telemetry`, and interprets it with `rail/eot.py`:

| reading | status |
|---|---|
| moving, pipe >= 75 psig | `moving` |
| moving, pipe 16-74 | `moving_braking` |
| stopped, >= 75 | `stopped_released` |
| stopped, 45-74 | `stopped_service` (normal stop) |
| stopped, 16-44 | `stopped_heavy` |
| <= 15 psig while moving, or after the same unit read >= 60 within 10 min | `emergency_*`, `emergency = true` |
| <= 15 psig with no recent charge | `no_air` (a cold EOT, not an emergency) |

HOT command semantics are provisional in traintap, so HOT packets are flagged as
emergencies only for message types listed in `rail.eot.HOT_EMERGENCY_MESSAGE_TYPES`
(empty until real captures confirm the code). Set the eot channel's `bias:` point to
the receiver location so `/v1/rail/activity` only shows telemetry near it.

## 3. Rail channel plan

`config/rail_channels.local.yaml` says which channels cover which subdivision (and where
that subdivision is), which is how `/v1/rail/activity` decides coverage. Start from FCC
ULS public data:

```
curl -O https://data.fcc.gov/download/pub/uls/complete/l_LMpriv.zip && unzip l_LMpriv.zip -d l_LMpriv
uv run python scripts/import_fcc_uls.py --uls-dir l_LMpriv
```

It keeps active CSX, CN/Grand Trunk, Norfolk Southern, Conrail Shared Assets and Amtrak
licenses at fixed sites inside the coverage bbox, with frequencies in the 160.110-161.565
MHz pool and computed AAR channel numbers. Every entry is `verified: false` with its FCC
call sign as `source`. ULS has no subdivision field, so entries are grouped per county;
move them to real subdivisions, set `use` (road/dispatcher/yard/detector, or `unknown`
until confirmed) and `channel_id`, and mark `verified: true` only after you've heard them.
Do not use RadioReference data.

Give a subdivision `aliases:` when other datasets name the track differently (FRA's
crossing inventory and NARN don't always agree): the gateway passes NARN's name as the
`subdivision` hint, and Pulse matches it exactly (once normalised) against names and aliases.
The hint only picks among lines whose `center` + `radius_km` (or `bbox`) contains the point:
give every subdivision one, sized to what its receiver really hears, or no point counts as
covered by it.

## 4. Defect detectors

Detector voices usually transmit on the road channel. The prefilter recognises readouts
(`rail/detector.py`), parses milepost, track, direction, axles, speed, temperature and
the result (no defects / alarm types / integrity failure), and stores a
`rail_detector_events` row without calling Claude. Readouts the rules can't complete go
to the batch call, whose `rail.detector` fills the gaps (`parse_source = llm`).

## 5. Testing without hardware

* `tests/test_ingest.py` builds rtl_airband-style trees (dated subdirectories, `.tmp`
  files, scan-mode frequencies, unmapped templates) and traintap CSVs.
* `tests/test_receivers.py` covers heartbeats, receiver states, coverage and
  `coverage_summary` without a database; `tests/test_api.py` checks "no receiver" vs "quiet"
  end to end.
* `INGEST_SOURCES=fixture` replays `tests/fixtures/transmissions.jsonl` (synthetic text)
  through the whole pipeline: `uv run python run_pipeline.py --stages ingest,extract --sources fixture`.
* To test audio end to end, drop any mp3/wav named like `rail_road_20261114_153012.mp3`
  into `data/sdr/` with the transcribe stage running.

## 6. Receiver heartbeats

A channel with no transcripts is either quiet or not being listened to. To tell them apart,
every receiver writes a heartbeat that keeps coming while its channels are silent, and
`/v1/feeds/status` turns it into `receiver.state` (`not_installed` / `offline` / `online`)
and `coverage_summary` ([pulse-api.md](pulse-api.md#feeds_status)). The files live in
`RECEIVER_STATUS_DIR` (default `data/receivers`; in the container stack the
`pulse_receivers` volume, written by the SDR containers and mounted read-only into
`pulse-api`), one per receiver, named after it:

| receiver | file | written by | how often |
|---|---|---|---|
| `rail-airband` | `rail-airband.prom` | rtl_airband's stats file: `stats_filepath = "/status/rail-airband.prom";` (global, in `deploy/sdr/rail.conf`) | every `stats_update_interval` (15 s) while the dongle delivers samples |
| `traintap-eot` | `traintap-eot.heartbeat` (or `heartbeat` next to `TRAINTAP_CSV`) | traintap `--heartbeat` | about once a minute while its decode loop runs |
| any other name a channel sets with `receiver:` | `<name>.prom` (rtl_airband stats format) or `<name>.heartbeat` (any file touched periodically) | you | well under `RECEIVER_STALE_S` |

* Only the file's modification time matters; a heartbeat older than `RECEIVER_STALE_S`
  (default 300 s) means `offline`. A missing file (and no recordings / packets for the
  feed in 7 days) means `not_installed`.
* rtl_airband's stats file also lists the channels it demodulates
  (`channel_activity_counter{freq="160.215",label="<channel id>"}`; the renderer sets each
  `label` to the Pulse channel id). A feed that isn't in the list, by label or by frequency
  within 3 kHz, gets no heartbeat from it: re-render `rail.local.conf` after adding channels.
* Without a heartbeat file (e.g. rtl_airband run by hand without `stats_filepath`), Pulse
  can only go by recordings: the feed is `online` for `RECEIVER_STALE_S` after each one and
  then `offline` even if the channel is merely quiet. Configure the heartbeat.
* Devices shown in `receiver.device` default to the serials in the compose file
  (`RAIL160`, `EOT457`); override them under `receivers:` in `channels.local.yaml`:
  `receivers: {rail-airband: {device: "rail-airband: RTL-SDR Blog V3, serial RAIL160"}}`.

## 7. From `not_installed` to `listening`

Until the dongles exist, every configured feed reports `receiver.state: "not_installed"`
and `coverage_summary` says `no_receiver`: the gateway can then say "no receiver yet"
instead of "quiet". When the SDRs arrive:

1. Do the one-time host steps (udev rule, DVB driver blacklist, EEPROM serials `RAIL160`
   and `EOT457`): [godseye-integration.md](godseye-integration.md#sdr).
2. Put the real channels in `config/channels.local.yaml` / `config/rail_channels.local.yaml`
   (enabled, each with `coverage.lines` and `aliases`, the eot channel with its `bias`
   point), then `uv run python scripts/render_rail_conf.py` to fill `deploy/sdr/rail.local.conf`.
3. Plug the antennas and dongles in and start the receivers:
   `docker-compose -p pulse --profile sdr up -d rail-airband traintap-eot`.
4. Check, about a minute later:
   * `docker logs rail-airband` shows the RAIL160 device opening, `docker logs traintap-eot`
     shows `serial EOT457 is device index N`.
   * `docker run --rm --network none -v pulse_receivers:/v:ro --entrypoint ls detroit-pulse:0.2.0 -lan --time-style=+%T /v`
     lists `rail-airband.prom` (rewritten every 15 s) and `traintap-eot.heartbeat`, and `.`
     as `10010 10030` with mode `drwxrwsr-x`.
   * `GET /v1/feeds/status` (through the gateway, or the `debug` profile):
     `receiver.state` is `online` for the rail and eot feeds and `coverage_summary` reads
     `{"rail_radio": "listening", "eot": "listening", ...}`, even before anyone transmits.
5. If a feed stays `not_installed` while others are `online`, it isn't in rtl_airband's
   channel list: re-render `rail.local.conf` and restart `rail-airband`. If everything
   stays `not_installed`, the stats file isn't being written: check `docker logs rail-airband`
   for "Cannot open output file /status/rail-airband.prom" and that the volume is
   `pulse:pulse-sdr 2775` ([godseye-integration.md](godseye-integration.md#sdr)).
