#!/usr/bin/env bash
# One-time, idempotent host setup for deploy/docker-compose.yml. No sudo needed.
#
#   ./setup.sh                          # network, shared volumes, random secrets, rail.local.conf
#   ./setup.sh --from-dotenv ../.env    # reuse POSTGRES_PASSWORD / REDIS_PASSWORD / ANTHROPIC_API_KEY /
#                                       # PULSE_API_TOKEN from the dev .env (required to reuse the dev
#                                       # data volumes: Postgres keeps the password it was created with)
#
# Creates (only if missing):
#   network  godseye_backend   bridge, --internal (no egress); pulse-api and the gateway join it
#   volumes  detroit-pulse_pulse_pgdata, detroit-pulse_pulse_redisdata (the dev stack's data), pulse_frontend
#   secrets  $PULSE_SECRETS_DIR (default ./secrets), dir 700, files 600:
#              postgres.env  POSTGRES_PASSWORD      redis.env      REDIS_PASSWORD
#              api.env       PULSE_API_TOKEN        anthropic.env  ANTHROPIC_API_KEY (empty = rules-only)
#   sdr/rail.local.conf      copy of the empty-channel sample (fill with scripts/render_rail_conf.py)
# Existing files are never overwritten. Secret values are never printed.
set -euo pipefail
cd "$(dirname "$0")"
umask 077

DOTENV=""
if [[ "${1:-}" == "--from-dotenv" ]]; then
  DOTENV="${2:?usage: setup.sh --from-dotenv PATH}"
  [[ -r "$DOTENV" ]] || { echo "cannot read $DOTENV" >&2; exit 1; }
fi

NET="${GODSEYE_BACKEND_NETWORK:-godseye_backend}"
if docker network inspect "$NET" >/dev/null 2>&1; then
  echo "network $NET exists"
else
  docker network create --driver bridge --internal --label org.detroit-pulse.shared=true "$NET" >/dev/null
  echo "created network $NET (internal)"
fi

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

if [[ ! -e sdr/rail.local.conf ]]; then
  cp sdr/rail.conf sdr/rail.local.conf && chmod 644 sdr/rail.local.conf
  echo "wrote sdr/rail.local.conf (empty channel list: run scripts/render_rail_conf.py)"
fi

cat <<EOF

Next:
  docker-compose -p pulse build && docker-compose -p pulse up -d
  Gateway: put the same PULSE_API_TOKEN (from $SECRETS/api.env) in the connector's
  deploy/secrets/gateway.env and apply the compose patch in docs/godseye-integration.md.
EOF
