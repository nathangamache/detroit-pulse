"""Runtime settings for every Pulse process.

All values come from the environment, with `.env` in the repo root loaded as a
fallback (pydantic-settings reads it; nothing is copied into os.environ).
Every variable is documented in `.env.example`.

Secrets are SecretStr so they never show up in reprs or logs.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_DIR = Path(__file__).resolve().parent.parent


def _default_geocoder_data_dir() -> Path:
    """$XDG_DATA_HOME/detroit-pulse/geocoder (~/.local/share/...): geocoder data never lives in the repo."""
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg).expanduser() / "detroit-pulse" / "geocoder"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Paths ────────────────────────────────────────────────────────────
    config_dir: Path = REPO_DIR / "config"
    data_dir: Path = REPO_DIR / "data"          # gitignored scratch for spool/logs

    # ── Postgres (PostGIS container on 127.0.0.1:5436) ───────────────────
    postgres_user: str = "pulse"
    postgres_password: SecretStr | None = None
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5436
    postgres_db: str = "detroitpulse"
    database_url: SecretStr | None = None       # overrides the parts above

    # ── Redis (127.0.0.1:6381, requirepass) ──────────────────────────────
    redis_password: SecretStr | None = None
    redis_host: str = "127.0.0.1"
    redis_port: int = 6381
    redis_db: int = 0
    redis_url: SecretStr | None = None          # overrides the parts above

    # ── API ──────────────────────────────────────────────────────────────
    pulse_api_host: str = "127.0.0.1"
    pulse_api_port: int = 7710
    pulse_root_path: str = ""                   # e.g. "/pulse/api" behind the gateway
    pulse_api_token: SecretStr | None = None    # set => every API route needs "Authorization: Bearer <token>"
    pulse_admin_token: SecretStr | None = None  # unset => /admin is disabled (404)
    pulse_cors_origins: str = ""                # comma-separated; empty => same-origin only
    pulse_docs_enabled: bool = False            # /docs and /openapi.json

    # ── Ingest ───────────────────────────────────────────────────────────
    ingest_sources: str = "sdr_files"           # comma list: sdr_files,traintap,fixture,broadcastify_legacy
    sdr_watch_dir: Path = REPO_DIR / "data" / "sdr"
    sdr_filename_tz: str = "UTC"                # rtl_airband uses UTC unless `localtime = true`
    sdr_min_file_age_s: float = 1.5             # file must be stable this long before ingest
    sdr_poll_interval_s: float = 1.0
    sdr_delete_after_transcribe: bool = False   # keep own recordings by default
    traintap_csv: Path = REPO_DIR / "data" / "traintap" / "trains.csv"
    traintap_channel: str = "eot_hot"
    fixture_dir: Path = REPO_DIR / "tests" / "fixtures"
    # Receiver heartbeats (core/receivers.py): <name>.prom (rtl_airband stats_filepath) or
    # <name>.heartbeat (traintap --heartbeat). Older than receiver_stale_s => the receiver is offline.
    receiver_status_dir: Path = REPO_DIR / "data" / "receivers"
    receiver_stale_s: float = 300.0
    broadcastify_legacy_enabled: bool = False   # see ingest/sources/broadcastify_legacy.py (ToS)
    broadcastify_tos_acknowledged: bool = False

    # ── Transcription (faster-whisper on CPU) ────────────────────────────
    whisper_model: str = "small.en"             # medium.en is the accuracy option
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_cpu_threads: int = 8
    whisper_num_workers: int = 1
    whisper_beam_size: int = 5
    whisper_download_root: Path | None = None   # HF cache dir; default ~/.cache/huggingface
    whisper_vad_filter: bool = True
    whisper_context_window_s: int = 300         # previous-transmission context per channel

    # ── Extraction (Claude Haiku 5.5) ────────────────────────────────────
    anthropic_api_key: SecretStr | None = None
    llm_enabled: bool = True                    # false => rules-only extraction
    llm_model: str = "claude-haiku-5-5"
    llm_effort: str = "medium"
    llm_batch_size: int = 10
    llm_batch_max_wait_s: float = 45.0
    llm_cache_ttl: str = "1h"                   # "1h" or "5m"
    llm_max_retries: int = 2
    llm_timeout_s: float = 60.0
    llm_fewshot_file: Path = REPO_DIR / "config" / "fewshots.local.jsonl"
    llm_daily_cap_usd: float = 1.00
    llm_monthly_cap_usd: float = 15.00
    llm_price_file: Path = REPO_DIR / "config" / "llm_prices.yaml"
    summary_mode: str = "on_close"              # on_close | rate_limited | off
    summary_min_interval_s: int = 900           # for rate_limited mode
    summary_max_chunks: int = 15                # summaries read at most the last N transmissions

    # ── Correlation / lifecycle ──────────────────────────────────────────
    incident_idle_resolve_s: int = 4 * 3600     # one TTL for Postgres + live state
    incident_candidate_window_s: int = 8 * 3600
    correlation_match_threshold: float = 0.50
    resolver_interval_s: int = 120

    # ── Geocoding ($0 providers only) ────────────────────────────────────
    geocoder_providers: str = "local,census,photon"   # order; add "nominatim" to enable it
    # scripts/build_geocoder_index.py writes <data dir>/index; GEOCODER_LOCAL_INDEX overrides it
    geocoder_data_dir: Path = Field(default_factory=_default_geocoder_data_dir)
    geocoder_local_index: Path | None = None
    geocoder_local_backend: str = "geocode.local:LocalGeocoder"   # "module:Class" with geocode(struct, bias)
    geocoder_user_agent: str = "detroit-pulse-geocoder/0.2 (self-hosted)"
    geocoder_timeout_s: float = 6.0
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    nominatim_min_interval_s: float = 1.0
    photon_url: str = "https://photon.komoot.io"
    census_url: str = "https://geocoding.geo.census.gov"
    geocode_cache_ttl_days: int = 30
    geocode_fail_cache_ttl_hours: int = 24
    google_geocoding_enabled: bool = False      # removed; kept only to fail loudly if set

    # ── Logging ──────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_json: bool = False

    # ── Derived values ───────────────────────────────────────────────────
    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url.get_secret_value()
        pw = self.postgres_password.get_secret_value() if self.postgres_password else ""
        auth = quote(self.postgres_user, safe="") + (":" + quote(pw, safe="") if pw else "")
        return f"postgresql+psycopg://{auth}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    def db_url_for(self, dbname: str) -> str:
        base, _, _ = self.db_url.rpartition("/")
        return f"{base}/{dbname}"

    @property
    def redis_dsn(self) -> str:
        if self.redis_url:
            return self.redis_url.get_secret_value()
        pw = self.redis_password.get_secret_value() if self.redis_password else ""
        auth = f":{quote(pw, safe='')}@" if pw else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def geocoder_index_dir(self) -> Path:
        return self.geocoder_local_index or (self.geocoder_data_dir / "index")

    @property
    def source_list(self) -> list[str]:
        return [s.strip() for s in self.ingest_sources.split(",") if s.strip()]

    @property
    def geocoder_provider_list(self) -> list[str]:
        return [s.strip() for s in self.geocoder_providers.split(",") if s.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.pulse_cors_origins.split(",") if o.strip()]

    whisper_model_options: list[str] = Field(
        default_factory=lambda: ["tiny.en", "base.en", "small.en", "medium.en"])

    @model_validator(mode="after")
    def _resolve_relative_paths(self):
        """`~` is expanded; other relative paths in .env are relative to the repo root, not the CWD."""
        for name, fld in type(self).model_fields.items():
            v = getattr(self, name)
            if isinstance(v, Path):
                v = v.expanduser()
                if not v.is_absolute():
                    v = (REPO_DIR / v).resolve()
                object.__setattr__(self, name, v)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Tests call this after monkeypatching the environment."""
    get_settings.cache_clear()
