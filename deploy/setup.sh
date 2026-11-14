#!/usr/bin/env bash
# One-time, idempotent host setup for deploy/docker-compose.yml. No sudo needed.
#
#   ./setup.sh                          # network, shared volumes, random secrets, rail.local.conf
#   ./setup.sh --from-dotenv ../.env    # reuse POSTGRES_PASSWORD / REDIS_PASSWORD / ANTHROPIC_API_KEY /
#                                       # PULSE_API_TOKEN from the dev .env (required to reuse the dev
#                                       # data volumes: Postgres keeps the password it was created with)
#
# Creates (only if missing):
#   network  godseye_backend   bridge, --internal (no egress), 172.30.80.0/24 (gateway .1); pulse-api
#                              and the connector's tailscale sidecar (172.30.80.20) join it. An
#                              existing one must have that subnet (the godseye host firewall covers it).
#   volumes  detroit-pulse_pulse_pgdata, detroit-pulse_pulse_redisdata (the dev stack's data), pulse_frontend
#   secrets  $PULSE_SECRETS_DIR (default ./secrets), dir 700, files 600:
#              postgres.env  POSTGRES_PASSWORD      redis.env      REDIS_PASSWORD
#              api.env       PULSE_API_TOKEN        anthropic.env  ANTHROPIC_API_KEY (empty = rules-only)
#   sdr/rail.local.conf      copy of the empty-channel sample (fill with scripts/render_rail_conf.py)
# Existing files are never overwritten. Secret values are never printed. Also checks that Pulse's own
# networks (if they exist) have their pinned subnets, and that the extractor (uid 10010) can read
# config/fewshots.local.jsonl; it only prints a hint for either and changes neither.
set -euo pipefail
cd "$(dirname "$0")"
umask 077

DOTENV=""
if [[ "${1:-}" == "--from-dotenv" ]]; then
  DOTENV="${2:?usage: setup.sh --from-dotenv PATH}"
  [[ -r "$DOTENV" ]] || { echo "cannot read $DOTENV" >&2; exit 1; }
fi

NET="${GODSEYE_BACKEND_NETWORK:-godseye_backend}"
# Pinned: the connector gives its tailscale sidecar 172.30.80.20 here, and the godseye host firewall
# (connector deploy/host-firewall/godseye-firewall.sh, GODSEYE_PULSE_SUBNETS) covers this subnet.
NET_SUBNET="${GODSEYE_BACKEND_SUBNET:-172.30.80.0/24}"
NET_GATEWAY="${GODSEYE_BACKEND_GATEWAY:-172.30.80.1}"
net_ipam() { docker network inspect "$1" --format '{{range .IPAM.Config}}{{.Subnet}} {{.Gateway}}{{end}}'; }
if docker network inspect "$NET" >/dev/null 2>&1; then
  have="$(net_ipam "$NET")"
  internal="$(docker network inspect "$NET" --format '{{.Internal}}')"
  if [[ "$have" != "$NET_SUBNET $NET_GATEWAY" || "$internal" != "true" ]]; then
    cat >&2 <<EOF
network $NET exists but is not what Pulse and the godseye stack expect:
  have:   subnet/gateway ${have:-none} internal=$internal
  expect: subnet/gateway $NET_SUBNET $NET_GATEWAY internal=true
The connector pins its tailscale sidecar at 172.30.80.20 on this network and the godseye host
firewall only covers $NET_SUBNET. Recreate it: remove the containers on it
  docker network inspect $NET --format '{{range .Containers}}{{.Name}} {{end}}'
(here: docker-compose -p pulse down; in the connector: docker-compose -p godseye rm -sf gateway tailscale),
then  docker network rm $NET,  run ./setup.sh again and bring both stacks back up.
EOF
    exit 1
  fi
  echo "network $NET exists ($NET_SUBNET, internal)"
else
  docker network create --driver bridge --internal --subnet "$NET_SUBNET" --gateway "$NET_GATEWAY" \
    --label org.detroit-pulse.shared=true "$NET" >/dev/null
  echo "created network $NET ($NET_SUBNET, internal)"
fi

# Pulse's own networks are created by compose with pinned subnets (docker-compose.yml); one left
# over from before the pinning keeps its old subnet, which the host firewall doesn't cover.
for spec in pulse_internal=172.30.81.0/24 pulse_egress=172.30.82.0/24 pulse_debug=172.30.83.0/24; do
  n="${spec%%=*}"; want="${spec#*=}"
  if docker network inspect "$n" >/dev/null 2>&1; then
    have="$(net_ipam "$n")"
    if [[ "${have%% *}" != "$want" ]]; then
      echo "warning: network $n has subnet ${have%% *}, not $want: run 'docker-compose -p pulse down'" \
           "(volumes are kept), then 'docker-compose -p pulse up -d' to recreate it" >&2
    fi
  fi
done

for v in "${PULSE_PGDATA_VOLUME:-detroit-pulse_pulse_pgdata}" "${PULSE_REDISDATA_VOLUME:-detroit-pulse_pulse_redisdata}" \
         "${PULSE_FRONTEND_VOLUME:-pulse_frontend}"; do
  if docker volume inspect "$v" >/dev/null 2>&1; then
    echo "volume $v exists"
  else
    docker volume create "$v" >/dev/null && echo "created volume $v"
  fi
done

SECRETS="${PULSE_SECRETS_DIR:-./secrets}"
mkdir -p "$SECRETS"
chmod 700 "$SECRETS"

rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
dotenv_get() {   # value of KEY in $DOTENV (last one wins), quotes stripped; empty if absent
  [[ -n "$DOTENV" ]] || return 0
  local v
  v=$(grep -E "^[[:space:]]*$1=" "$DOTENV" | tail -n 1 | cut -d= -f2- || true)
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v"
}
pick() { local v; v=$(dotenv_get "$1"); if [[ -n "$v" ]]; then printf '%s' "$v"; else "${@:2}"; fi; }
write_secret() {  # file KEY=VALUE...
  local file="$SECRETS/$1"; shift
  if [[ -e "$file" ]]; then echo "keep $file"; return; fi
  printf '%s\n' "$@" > "$file"
  chmod 600 "$file"
  echo "wrote $file"
}

write_secret postgres.env "POSTGRES_PASSWORD=$(pick POSTGRES_PASSWORD rand)"
write_secret redis.env "REDIS_PASSWORD=$(pick REDIS_PASSWORD rand)"
write_secret api.env "PULSE_API_TOKEN=$(pick PULSE_API_TOKEN rand)" "PULSE_ADMIN_TOKEN="
write_secret anthropic.env "ANTHROPIC_API_KEY=$(pick ANTHROPIC_API_KEY true)"

# The extractor runs as uid 10010 and reads the gitignored few-shot file through the read-only
# config mount. Grant that one user read access with an ACL; the file's mode stays as it is.
FEWSHOTS="${PULSE_CONFIG_DIR:-../config}/fewshots.local.jsonl"
readable_by_pulse() {   # owner 10010 with u+r, o+r, or an ACL entry for uid 10010 that the mask keeps
  local m; m=$(stat -c %a "$1")
  [[ "$(stat -c %u "$1")" == 10010 ]] && (( 8#$m & 8#400 )) && return 0
  (( 8#$m & 8#4 )) && return 0
  command -v getfacl >/dev/null 2>&1 || return 1
  getfacl -n -p -- "$1" 2>/dev/null | grep -Eq '^user:10010:r..([[:space:]]+#effective:r..)?$'
}
if [[ -f "$FEWSHOTS" ]] && ! readable_by_pulse "$FEWSHOTS"; then
  echo "hint: pulse-extract (uid 10010) can't read $FEWSHOTS; without changing its mode:" >&2
  echo "        setfacl -m u:10010:r $FEWSHOTS" >&2
fi

if [[ ! -e sdr/rail.local.conf ]]; then
  cp sdr/rail.conf sdr/rail.local.conf && chmod 644 sdr/rail.local.conf
  echo "wrote sdr/rail.local.conf (empty channel list: run scripts/render_rail_conf.py)"
fi

cat <<EOF

Next:
  docker-compose -p pulse build && docker-compose -p pulse up -d
  Gateway: put the same PULSE_API_TOKEN (from $SECRETS/api.env) in the connector's
  deploy/secrets/gateway.env; its compose file already joins $NET and mounts pulse_frontend.
  Host firewall: the connector's deploy/host-firewall/godseye-firewall.sh covers 172.30.80.0/24
  through 172.30.83.0/24 (docs/godseye-integration.md#host-firewall).
EOF
