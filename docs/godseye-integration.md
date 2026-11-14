# Running Pulse in the godseye stack

`deploy/docker-compose.yml` runs Pulse as hardened containers next to the godseye gateway
(`godseye-detroit-connector/deploy/docker-compose.yml`). The gateway reaches Pulse at
`PULSE_URL=http://pulse-api:7710` over a dedicated network; nothing is published on the host.

```
                         godseye-edge (172.30.77.0/24, connector)
  cloudflared ──▶ tailscale sidecar ◀── gateway (network_mode: service:tailscale)
                        │ .20
                        │ godseye_backend  172.30.80.0/24 (external, --internal: no egress)
                        ▼
                    pulse-api :7710 ──────────┐
                        │ pulse_internal  172.30.81.0/24 (internal: no egress)
     pulse-postgis ─────┼───── pulse-redis    │
     pulse-transcribe ──┤                     │
     pulse-extract ─────┘                     │
            │ pulse_egress  172.30.82.0/24 (Anthropic, Census/Photon, first Whisper download)
            ▼ internet only (host firewall: no host, LAN or tailnet)
  pulse_frontend volume ──(read-only)──▶ gateway PULSE_STATIC_DIR=/pulse-ui
```

Only `pulse-api` and the tailscale sidecar (whose network namespace the gateway shares) are on
`godseye_backend`, so cloudflared and the GEV containers can't reach Pulse directly. Postgres and
Redis are on `pulse_internal` only.

## Networks, DNS and the host firewall

Every subnet is pinned, so the godseye host firewall (`godseye-detroit-connector`
`deploy/host-firewall/godseye-firewall.sh`, its `docs/host-firewall.md`) can name it:

| network | subnet | members | egress |
|---|---|---|---|
| `godseye_backend` | `172.30.80.0/24` (gateway `.1`) | `pulse-api`, the connector's tailscale sidecar `.20` | none (`--internal`, created by `setup.sh`) |
| `pulse_internal` | `172.30.81.0/24` | postgis, redis, api, transcribe, extract (and debug) | none (`internal: true`) |
| `pulse_egress` | `172.30.82.0/24` | `pulse-transcribe`, `pulse-extract` | internet only |
| `pulse_debug` | `172.30.83.0/24` | `pulse-debug` (profile `debug`) | internet only (unused: it exists for the 127.0.0.1 ports) |

Even an internal bridge gives the host an address on it (the `.1` gateway), and `pulse_egress`
routes through the host, so without the firewall a Pulse container could open connections to every
host service listening on `0.0.0.0` and, from `pulse_egress`, to the LAN and the tailnet. For
traffic **from** each of the four subnets the firewall drops new connections to the host itself
(`INPUT`, replies allowed) and, in `DOCKER-USER`, everything to RFC 1918, `100.64.0.0/10`
(the tailnet, including `100.100.100.100`), link-local, multicast and reserved space. Members of
one subnet still reach each other (`-s S -d S`), no other network is let into these subnets, and
what's left for `pulse_egress` (and `pulse_debug`) is the public internet. Re-install the
firewall whenever these subnets change (`sudo install ... godseye-firewall.sh` and
`sudo systemctl reload godseye-firewall.service`, see the connector's `docs/host-firewall.md`).

A network that compose created before the subnets were pinned keeps its old subnet:
`docker-compose -p pulse down` (named volumes are kept; `godseye_backend` is external and stays),
then `docker-compose -p pulse up -d`. `setup.sh` warns about such a network, and refuses to go on
if `godseye_backend` exists without `172.30.80.0/24` / `.1` / `--internal`.

**DNS.** Every service has `dns: [1.1.1.1, 9.9.9.9]` (override with `PULSE_DNS_1` /
`PULSE_DNS_2`; keep them public) and `dns_search: ["."]`, the same as the connector's containers.
Docker's embedded resolver (`127.0.0.11`) still resolves the service names; names it doesn't know
go to the public resolvers instead of the host's (the LAN router or MagicDNS, which the firewall
drops), and the host's tailnet search domain is not copied into `/etc/resolv.conf`, so a tailnet
device named like a service can't capture its traffic. Containers on internal networks only
(postgis, redis, api) have no route to the resolvers and so can't resolve external names at all.

Check after `up`, and after a firewall re-install:

```bash
docker network inspect pulse_internal pulse_egress pulse_debug godseye_backend \
  --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}}{{end}} internal={{.Internal}}'
docker exec pulse-api cat /etc/resolv.conf              # nameserver 127.0.0.11, no "search" line
# Socket probe: "blocked" = dropped or no route. The host's LAN address and the LAN router:
LAN=$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')
ROUTER=$(ip -4 route show default | awk '{print $3; exit}')
probe() { c=$1; shift; docker exec "$c" python -c '
import socket, sys
for t in sys.argv[1:]:
    want, h, p = t.split(":")
    try: socket.create_connection((h, int(p)), timeout=4).close(); got = "open"
    except (socket.timeout, OSError) as e: got = "refused" if isinstance(e, ConnectionRefusedError) else "blocked"
    print("PASS" if got == want else "FAIL", want, h, p, got)' "$@"; }
probe pulse-api blocked:172.30.81.1:22 blocked:172.30.80.1:22 blocked:$LAN:22
probe pulse-transcribe blocked:172.30.82.1:22 blocked:172.30.81.1:22 blocked:$LAN:22 \
  blocked:$ROUTER:53 blocked:100.100.100.100:53 open:1.1.1.1:443
probe pulse-extract blocked:172.30.82.1:22 blocked:$LAN:22 open:api.anthropic.com:443
```

`refused` for a `blocked` target means the packet got through (nothing listens there): the
firewall isn't covering that subnet.

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

`setup.sh` creates, only if missing: the `godseye_backend` network (`--internal`, `--subnet
172.30.80.0/24 --gateway 172.30.80.1`; an existing one must match, see
[Networks](#networks-dns-and-the-host-firewall)), the volumes
`detroit-pulse_pulse_pgdata`, `detroit-pulse_pulse_redisdata` and `pulse_frontend`, and
`deploy/secrets/{postgres,redis,api,anthropic}.env` (dir 700, files 600; see
[deploy/secrets/README.md](../deploy/secrets/README.md)). Copy `PULSE_API_TOKEN` from
`deploy/secrets/api.env` into the connector's `deploy/secrets/gateway.env`.

### Few-shot file (read access for uid 10010)

`pulse-extract` runs as uid 10010 and reads the gitignored `config/fewshots.local.jsonl`
(`LLM_FEWSHOT_FILE`, through the read-only `config/` mount). The file is private (mode 640 or
600, owned by you), so grant that one uid read access with an ACL instead of changing its mode:

```bash
setfacl -m u:10010:r config/fewshots.local.jsonl
getfacl -p config/fewshots.local.jsonl        # user:10010:r--, no "#effective:---"
```

`config/` itself must stay searchable by others (mode 755 or 775). Without access the
extractor fails when it builds its prompt (`PermissionError`: the file exists, so there is no
fallback to the built-in synthetic examples). `setup.sh` prints this hint when the file exists and
uid 10010 can't read it. `scripts/build_fewshots.py` rewrites the file in place, which keeps the
ACL; anything that replaces it (a new file moved over it) drops the ACL, so run `setfacl` again.

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

## Connector side

The connector's `deploy/docker-compose.yml` already has everything Pulse needs:

* the tailscale **sidecar** joins `godseye_backend` (external, under the key `pulse`) at the fixed
  address `172.30.80.20`, so `up` fails on a network created without the pinned subnet; the
  gateway shares the sidecar's network namespace and reaches `PULSE_URL=http://pulse-api:7710`.
  Nothing else in that stack joins it.
* the gateway mounts the `pulse_frontend` volume read-only at `/pulse-ui` and sets
  `PULSE_STATIC_DIR=/pulse-ui` and `PULSE_API_PROXY=true` (UI at `/pulse` on the UI listener,
  `/pulse/api` proxied to Pulse with the token).
* its `deploy/secrets/gateway.env` holds `PULSE_API_TOKEN=<the value from Pulse's
  deploy/secrets/api.env>`.

Order: Pulse's `setup.sh` first (the connector stack refuses to start while the external network
or volume is missing). Notes:

* The gateway's proxy treats `Upgrade` as hop-by-hop, so the UI's WebSocket (`/pulse/api/ws`)
  won't connect through it; the UI falls back to polling the REST API.
* `GET /v1/feeds/status` carries, per feed, `receiver` (`not_installed` / `offline` / `online`),
  `coverage` (area, radius, rail lines with their FRA/NARN aliases) and `example`, plus a top-level
  `coverage_summary` per source (`no_receiver` / `receiver_offline` / `listening` /
  `not_configured`), so the gateway can say "no receiver yet" instead of "quiet"
  ([pulse-api.md](pulse-api.md#feeds_status)). The fields are additive: the gateway treats their
  absence (an older Pulse) as unknown. `/v1/rail/activity` reports `covered: false` until a
  receiver has reported for one of the line's channels.
* UI files are world-readable (644/755) and owned by uid 10010, so the gateway's uid 10001 can
  read them through the read-only mount.

## SDR

Profile `sdr`, off by default (no hardware yet). Two RTL-SDR Blog V3 dongles:

| service | image | dongle | output |
|---|---|---|---|
| `rail-airband` | `ghcr.io/rtl-airband/rtlsdr-airband:v7.0.1@sha256:e6da…` | serial `RAIL160`, 2.56 MS/s at 160.8975 MHz (`deploy/sdr/rail.conf`) | one MP3 per transmission into `pulse_sdr` (Pulse's `sdr_files` source); stats file `rail-airband.prom` every 15 s into `pulse_receivers` |
| `traintap-eot` | `ghcr.io/trickv/traintap:sha-984af85@sha256:fbfb…` | serial `EOT457`, EOT 457.9375 MHz + DPU | `trains.csv`, `passes.csv`, `signal.csv` into `pulse_traintap`; `traintap-eot.heartbeat` (about once a minute) into `pulse_receivers` |

**Receiver heartbeats.** The two heartbeat files are how `/v1/feeds/status` tells "receiver
online, channel quiet" from "no receiver" (`receiver.state`, `coverage_summary`; see
[pulse-api.md](pulse-api.md#feeds_status) and [sdr-ingest.md](sdr-ingest.md#6-receiver-heartbeats)).
They go into their own small volume, `pulse_receivers`: the SDR containers mount it at `/status`
(rtl_airband's global `stats_filepath`, traintap's `--heartbeat`), and `pulse-api` mounts it
read-only at `/data/receivers` (`RECEIVER_STATUS_DIR`), so the API container never sees the
recordings. Nothing gains a network or a port: the SDR containers stay `network_mode: none`. The
empty volume takes its owner and mode (`pulse:pulse-sdr`, `2775`) from the image's
`/data/receivers` when `pulse-api` first mounts it (checked: this also happens for a read-only
mount). Until the `sdr` profile runs, no file exists and every feed is `not_installed`.

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

and check that the receivers report (details and troubleshooting:
[sdr-ingest.md §7](sdr-ingest.md#7-from-not_installed-to-listening)):

```bash
docker run --rm --network none -v pulse_receivers:/v:ro --entrypoint ls detroit-pulse:0.2.0 -lan --time-style=+%T /v
#   .  10010 10030 drwxrwsr-x;  rail-airband.prom (fresh every 15 s);  traintap-eot.heartbeat (every ~60 s)
# GET /v1/feeds/status: receiver.state "online" for the rail and eot feeds, and
# coverage_summary {"rail_radio": "listening", "eot": "listening", ...} even with no traffic yet
```

The `pulse_receivers` mount on `pulse-api` came with the receiver heartbeat change (2026-11-14):
after pulling it, rebuild `detroit-pulse:0.2.0` (the image creates `/data/receivers`) and run
`docker-compose -p pulse up -d`, which recreates `pulse-api` (and the workers, whose image changed)
and creates the volume.

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
