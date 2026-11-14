# deploy/secrets

Mode-600 env files read by `deploy/docker-compose.yml`. Everything here except this README is
gitignored. Create them with `../setup.sh` (random values) or `../setup.sh --from-dotenv ../../.env`
(reuse the dev stack's passwords so the existing Postgres/Redis volumes keep working).

| file | keys | used by |
|---|---|---|
| `postgres.env` | `POSTGRES_PASSWORD` | pulse-postgis, pulse-api, pulse-transcribe, pulse-extract |
| `redis.env` | `REDIS_PASSWORD` | pulse-redis, pulse-api, pulse-transcribe, pulse-extract |
| `api.env` | `PULSE_API_TOKEN` (same value in the gateway's `gateway.env`), optional `PULSE_ADMIN_TOKEN` | pulse-api |
| `anthropic.env` | `ANTHROPIC_API_KEY` (empty = rules-only extraction) | pulse-extract |

Compose passes these into the containers' environment, so anyone who can run `docker inspect` on
this host can read them; keep the docker group to trusted users.
