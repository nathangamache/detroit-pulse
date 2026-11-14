# Running Pulse in the godseye stack

`deploy/docker-compose.yml` runs Pulse as hardened containers next to the godseye gateway
(`godseye-detroit-connector/deploy/docker-compose.yml`). The gateway reaches Pulse at
`PULSE_URL=http://pulse-api:7710` over a dedicated network; nothing is published on the host.

```
                         godseye network (172.30.77.0/24, connector)
  cloudflared ──▶ tailscale sidecar ◀── gateway (network_mode: service:tailscale)
                        │
                        │ godseye_backend  (external, --internal: no egress)
                        ▼
                    pulse-api :7710 ──────────┐
                        │ pulse_internal (internal: no egress)
     pulse-postgis ─────┼───── pulse-redis    │
     pulse-transcribe ──┤                     │
     pulse-extract ─────┘                     │
            │ pulse_egress (Anthropic, Census/Photon, first Whisper download)
            ▼ internet
  pulse_frontend volume ──(read-only)──▶ gateway PULSE_STATIC_DIR=/pulse-ui
```

Only `pulse-api` and the tailscale sidecar (whose network namespace the gateway shares) are on
`godseye_backend`, so cloudflared and the GEV containers can't reach Pulse directly. Postgres and
Redis are on `pulse_internal` only.

## Services

| service | image | runs | limits |
|---|---|---|---|
| `pulse-postgis` | `postgis/postgis:16-3.4@sha256:4412…` | as uid 999 (data owner), no root entrypoint | 4 CPU, 4 GB, 256 pids |
| `pulse-redis` | `redis:7-alpine@sha256:8b81…` | as 999:1000, requirepass via tmpfs config | 1 CPU, 768 MB, 64 pids |
| `pulse-api` | `detroit-pulse:0.2.0` | `alembic upgrade head`, then `python -m api --allow-non-loopback` | 1 CPU, 768 MB, 128 pids |
| `pulse-transcribe` | same | `nice -n 19 run_pipeline.py --stages ingest,transcribe` (8 Whisper threads) | 8 CPU, 4 GB, 256 pids |
| `pulse-extract` | same | `nice -n 19 run_pipeline.py --stages extract,resolver` | 2 CPU, 2 GB, 128 pids |
| `pulse-frontend` (profile `frontend`) | `detroit-pulse-ui:0.2.0` | one-shot: copies the Vite build into `pulse_frontend` | 0.5 CPU, 128 MB, no network |
| `pulse-debug` (profile `debug`) | `detroit-pulse:0.2.0` | TCP forwarder: 127.0.0.1:7710 / 5436 / 6381 | 0.5 CPU, 64 MB |
| `rail-airband`, `traintap-eot` (profile `sdr`) | pinned upstream images | see [SDR](#sdr) | 1.5 CPU each, no network |

Every service: non-root `user`, `read_only: true` root filesystem with tmpfs `/tmp` (and
`/var/run/postgresql`), `cap_drop: [ALL]`, `no-new-privileges`, `init`, memory, swap, pids and CPU
limits, json-file log rotation. No docker socket. The only bind mounts are read-only: the repo's
`config/` (your `*.local.*` files are read at run time, never baked into the image) and the geocoder
index directory. Images are built from `deploy/docker/Dockerfile` (Python 3.12.15 slim, pinned by
digest, runtime dependencies from `uv.lock` only) and `deploy/docker/frontend.Dockerfile`;
`.dockerignore` keeps `.env`, `config/*.local.*`, `deploy/secrets`, `data/`, audio and tests out of
every layer.

## First run

```bash
cd deploy
./setup.sh --from-dotenv ../.env       # network, volumes, mode-600 secrets (reusing the dev passwords)
DOCKER_BUILDKIT=0 nice -n 19 docker build --cpuset-cpus 0-7 --cpu-shares 128 -f docker/Dockerfile -t detroit-pulse:0.2.0 ..
DOCKER_BUILDKIT=0 nice -n 19 docker build --cpuset-cpus 0-7 --cpu-shares 128 -f docker/frontend.Dockerfile -t detroit-pulse-ui:0.2.0 ..
#   (or: docker-compose -p pulse --profile frontend build; the explicit form caps the build's CPU)
docker-compose -p pulse up -d
docker-compose -p pulse --profile frontend up -d pulse-frontend      # refresh the UI volume
```

`setup.sh` creates, only if missing: the `godseye_backend` network (`--internal`), the volumes
`detroit-pulse_pulse_pgdata`, `detroit-pulse_pulse_redisdata` and `pulse_frontend`, and
`deploy/secrets/{postgres,redis,api,anthropic}.env` (dir 700, files 600; see
[deploy/secrets/README.md](../deploy/secrets/README.md)). Copy `PULSE_API_TOKEN` from
`deploy/secrets/api.env` into the connector's `deploy/secrets/gateway.env`.

Settings you may want in `deploy/.env` (gitignored, read by compose for interpolation):
`WHISPER_MODEL` (default `small.en`), `PULSE_LLM_ENABLED`, `GEOCODER_PROVIDERS`,
`PULSE_GEOCODER_INDEX` (default `~/.local/share/detroit-pulse/geocoder/index`),
`PULSE_CONFIG_DIR` (default `../config`), `PULSE_INGEST_SOURCES` (default `sdr_files,traintap`),
`SDR_USB_GID` (default 46 = plugdev), `EOT_SERIAL`, `PULSE_RAIL_CONF`.

The first `pulse-transcribe` start downloads the Whisper model into the `pulse_models` volume
(that is what its `pulse_egress` network is for; after that it could be removed).

## Moving the existing data (dev stack -> containers)

The dev stack (`docker-compose.yml` in the repo root, project `detroit-pulse`) keeps its data in
the volumes `detroit-pulse_pulse_pgdata` and `detroit-pulse_pulse_redisdata`. The container stack
mounts those **same volumes** (declared `external`), so there is no copy:

1. Optional backup: `docker run --rm --network none -v detroit-pulse_pulse_pgdata:/v:ro -v "$PWD":/out alpine tar czf /out/pgdata.tgz -C /v .`
   (with Postgres stopped), or `pg_dump` through the debug profile.
2. Stop the dev processes and containers: `./stop.sh --services`, then `docker-compose rm -f`
   in the repo root. Both stacks use the container names `pulse-postgis` and `pulse-redis`, so
   Docker refuses to run them at the same time; that also stops two Postgres servers from ever
   opening one data directory.
3. `deploy/setup.sh --from-dotenv ../.env`. Postgres stores the role password inside the data
   directory, so the container stack must use the dev `POSTGRES_PASSWORD` (and `REDIS_PASSWORD`).
4. `docker-compose -p pulse up -d`. `pulse-api` runs `alembic upgrade head` (a no-op on a current
   schema). The data directory is owned by uid 999 (checked), which is the user Postgres runs as.

Going back to dev mode: `docker-compose -p pulse down` in `deploy/` (volumes are kept), then
`docker-compose up -d` in the repo root. Different volumes: set `PULSE_PGDATA_VOLUME` /
`PULSE_REDISDATA_VOLUME` before `setup.sh` and `up`.

Tests and the host-run tools (`start.sh`, `pytest`, alembic) expect Postgres/Redis on
127.0.0.1:5436/6381: use the dev stack, or start the container stack's `debug` profile.

## Connector-side change (not applied)

The gateway lives in the tailscale sidecar's network namespace, so the **sidecar** joins
`godseye_backend`; the gateway also mounts the UI volume and turns on its `/pulse/api` proxy.
Against `godseye-detroit-connector` @ `23b5728` (also in
[godseye-connector-compose.patch](godseye-connector-compose.patch); `git apply --check` passes and
`docker-compose config` accepts the result):

```diff
--- a/deploy/docker-compose.yml
+++ b/deploy/docker-compose.yml
@@ -150,6 +150,9 @@
         ipv4_address: 172.30.77.20
         # cloudflared reaches the gateway's PUBLIC listener as http://gateway:7700.
         aliases: [gateway]
+      # Detroit Pulse: the gateway shares this namespace and reaches http://pulse-api:7710 here.
+      # Internal network (no egress); only pulse-api and this sidecar are on it.
+      godseye_backend: {}
 
   gateway:
     <<: *hardening
@@ -181,11 +184,15 @@
       # Only cloudflared's fixed address may set CF-Connecting-IP.
       TRUSTED_PROXY_IPS: 172.30.77.10
       PULSE_URL: ${PULSE_URL:-http://pulse-api:7710}
+      # Pulse UI: static build that Pulse's pulse-frontend service copies into pulse_frontend
+      PULSE_STATIC_DIR: /pulse-ui
+      PULSE_API_PROXY: "true"
       HOME: /tmp
       UV_THREADPOOL_SIZE: "4"
     env_file: [./secrets/gateway.env]
     volumes:
       - gateway-data:/data
+      - pulse-ui:/pulse-ui:ro
     tmpfs: ["/tmp:size=64m,mode=1777"]
     mem_limit: 768m
     memswap_limit: 768m
@@ -217,6 +224,10 @@
   gev-cache:
   gev-logs:
   gev-dotcache:
+  # created by detroit-pulse/deploy/setup.sh, filled by Pulse's pulse-frontend service
+  pulse-ui:
+    external: true
+    name: pulse_frontend
 
 networks:
   godseye:
@@ -225,3 +236,7 @@
     ipam:
       config:
         - subnet: 172.30.77.0/24
+  # created by detroit-pulse/deploy/setup.sh (docker network create --internal godseye_backend)
+  godseye_backend:
+    external: true
+    name: godseye_backend
```

Plus, in the connector's `deploy/secrets/gateway.env`: `PULSE_API_TOKEN=<the value from Pulse's
deploy/secrets/api.env>`. Order: run Pulse's `setup.sh` first (the connector stack refuses to
start while the external network or volume is missing). Notes for the reviewer:

* The sidecar gains a second interface on an internal bridge. The netstack's loopback re-dial
  concern in the connector header doesn't change: nothing new listens in that namespace.
* The gateway's proxy treats `Upgrade` as hop-by-hop, so the UI's WebSocket (`/pulse/api/ws`)
  won't connect through it; the UI falls back to polling the REST API.
* UI files are world-readable (644/755) and owned by uid 10010, so the gateway's uid 10001 can
  read them through the read-only mount.

## SDR

Profile `sdr`, off by default (no hardware yet). Two RTL-SDR Blog V3 dongles:

| service | image | dongle | output |
|---|---|---|---|
| `rail-airband` | `ghcr.io/rtl-airband/rtlsdr-airband:v7.0.1@sha256:e6da…` | serial `RAIL160`, 2.56 MS/s at 160.8975 MHz (`deploy/sdr/rail.conf`) | one MP3 per transmission into `pulse_sdr` (Pulse's `sdr_files` source) |
| `traintap-eot` | `ghcr.io/trickv/traintap:sha-984af85@sha256:fbfb…` | serial `EOT457`, EOT 457.9375 MHz + DPU | `trains.csv`, `passes.csv`, `signal.csv` + heartbeat into `pulse_traintap` |

Both run as non-root users in group 10030 (`pulse-sdr`, shared with `pulse-transcribe`), with
`network_mode: none`, read-only roots, all capabilities dropped, `device_cgroup_rules: ['c 189:* rmw']`
(USB character devices) and `/dev/bus/usb` bind-mounted read-only (device nodes stay writable; a
replugged dongle appears without a restart). They start after `pulse-transcribe`, so the shared
volumes get Pulse's owner and setgid mode first; rtl_airband runs with `umask 0002` so Pulse can
delete recordings when `SDR_DELETE_AFTER_TRANSCRIBE=true`. traintap only selects dongles by index;
`deploy/sdr/traintap_by_serial.py` resolves the serial with pyrtlsdr and execs traintap.

Host prerequisites (Nathan, one-time, needs sudo):

1. udev rule so plugdev (gid 46) can open the dongles, then replug:
   `SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0660"`
   in `/etc/udev/rules.d/20-rtlsdr.rules`; `sudo udevadm control --reload-rules && sudo udevadm trigger`.
2. Keep the DVB-T driver off the dongles: `blacklist dvb_usb_rtl28xxu` in
   `/etc/modprobe.d/blacklist-rtlsdr.conf` (librtlsdr can also detach it at run time).
3. Set the serials, one dongle plugged in at a time (rtl_eeprom ships in the traintap image):
   `docker run --rm --device-cgroup-rule='c 189:* rmw' -v /dev/bus/usb:/dev/bus/usb --group-add 46 --user 10021:10030 --entrypoint rtl_eeprom ghcr.io/trickv/traintap:sha-984af85 -d 0 -s RAIL160`
   (then `-s EOT457` for the second), replug, and check with `rtl_test` or `--list-devices`.

Then:

```bash
uv run python scripts/render_rail_conf.py      # fills deploy/sdr/rail.local.conf from config/channels.local.yaml
docker-compose -p pulse --profile sdr up -d rail-airband traintap-eot
```

`deploy/sdr/rail.conf` (committed) has an **empty channel list**, so no frequencies are in the
public repo; each rendered channel uses `split_on_transmission`, `include_freq`,
`dated_subdirectories`, `split_tx_drop_shorter_than = 0.5` and `split_max_file_time = 60`. Checked
against the real image without hardware: the rendered file parses and rtl_airband stops at
"RTLSDR device with serial number RAIL160 not found"; the empty sample stops at "no channels
configured". The traintap wrapper exits with the serials it can see.

## Verification (2026-11-14)

Run with throwaway secrets from `setup.sh` (random values, not the dev ones), fresh volumes
`pulse_verify_pgdata` / `pulse_verify_redisdata`, and `docker-compose.verify.yml` (fixture source,
`LLM_ENABLED=false`, `GEOCODER_PROVIDERS=local`, Whisper `tiny.en`):

* All five core services started; Postgres initialised and ran as uid 999 on a read-only root;
  migrations ran from `pulse-api`; `docker inspect` showed non-root users, `ReadonlyRootfs`,
  `CapDrop [ALL]`, `no-new-privileges`, limits, and no port bindings. Idle memory: postgis 73 MB,
  redis 4 MB, api 75 MB, transcribe 199 MB (tiny.en), extract 554 MB (geocoder index loaded in 3.8 s).
* From a throwaway `curlimages/curl:8.23.0` container on `godseye_backend`: `GET /v1/health` 200
  without a token; `/v1/feeds/status` 401 without it; `/v1/incidents/near` 200 with the bearer;
  `/admin/*` 404; no DNS or egress from that network; nothing listening on the host's 127.0.0.1:7710.
* `pulse-extract` replayed the 7 fixture transmissions rules-only (3 rules, 4 prefilter) and opened
  a VEHICLE_FIRE incident at "Packard & Stadium, Ann Arbor" via `local:osm_shared_node` (HIGH),
  6 m from the TIGER crossing, returned by `/v1/incidents/near` around Ann Arbor.
* `pulse-transcribe` downloaded and loaded tiny.en over `pulse_egress`; Whisper ran at nice 19 with
  the pulse-sdr group; the shared volume came up `pulse:pulse-sdr 2775`.
* `pulse-frontend` copied the 5-file build into `pulse_frontend` (world-readable) and exited 0.
* The `debug` profile published 7710/5436/6381 on 127.0.0.1 only and `/v1/health` answered.
* Torn down with `down` (no `-v`); named volumes kept (the verify volumes too: remove them with
  `docker volume rm pulse_verify_pgdata pulse_verify_redisdata` when no longer wanted).
