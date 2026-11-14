"""Test setup.

* Never talks to the real Claude API: ANTHROPIC_API_KEY is blanked for the whole
  session and every extraction test injects a mock client.
* DB tests use a separate database, detroitpulse_test, on the same PostGIS
  container (127.0.0.1:5436); it is created and migrated on first use and its
  tables are truncated before every DB test. If Postgres isn't reachable, DB
  tests are skipped (not failed).
* Redis DB 1 is used for best-effort pub/sub; tests never depend on it.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEST_DB = "detroitpulse_test"

os.environ["POSTGRES_DB"] = TEST_DB
os.environ["REDIS_DB"] = "1"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.pop("DATABASE_URL", None)
os.environ["PULSE_ADMIN_TOKEN"] = ""
os.environ["GEOCODER_PROVIDERS"] = "local"      # no network geocoders in tests unless a test opts in
os.environ["LOG_LEVEL"] = "WARNING"
# Tests read a pristine copy of the committed config (never a developer's *.local.* files).
_SESSION_CFG = Path(tempfile.mkdtemp(prefix="pulse-test-config-")) / "config"
shutil.copytree(REPO / "config", _SESSION_CFG, ignore=shutil.ignore_patterns("*.local.*"))
os.environ["CONFIG_DIR"] = str(_SESSION_CFG)
os.environ["LLM_FEWSHOT_FILE"] = str(_SESSION_CFG / "fewshots.local.jsonl")   # absent on purpose

from core import config_files  # noqa: E402
from core.db import reset_engine  # noqa: E402
from core.settings import get_settings, reset_settings_cache  # noqa: E402

TABLES = ["rail_detector_events", "transcript_chunks", "incidents", "rail_telemetry", "llm_usage",
          "geocode_cache", "pipeline_events"]


def _db_available() -> bool:
    import psycopg
    s = get_settings()
    try:
        url = s.db_url_for("postgres").replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(url, connect_timeout=3, autocommit=True) as c:
            exists = c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone()
            if not exists:
                c.execute(f'CREATE DATABASE "{TEST_DB}" TEMPLATE template0')
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def migrated_db():
    if not _db_available():
        pytest.skip("PostGIS test database not reachable on 127.0.0.1:5436")
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "db" / "migrations"))
    command.upgrade(cfg, "head")
    reset_engine()
    yield get_settings().db_url


@pytest.fixture()
def db(migrated_db):
    """Empty tables before each DB test."""
    from sqlalchemy import text

    from core.db import get_engine
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"))
    yield get_engine()


@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch):
    """A writable copy of config/ so tests can drop in *.local.yaml files."""
    d = tmp_path / "config"
    shutil.copytree(REPO / "config", d, ignore=shutil.ignore_patterns("*.local.*"))
    monkeypatch.setenv("CONFIG_DIR", str(d))
    reset_settings_cache()
    config_files.clear_cache()
    yield d
    monkeypatch.setenv("CONFIG_DIR", str(_SESSION_CFG))
    reset_settings_cache()
    config_files.clear_cache()


@pytest.fixture(autouse=True)
def _fresh_settings():
    reset_settings_cache()
    config_files.clear_cache()
    yield
    reset_settings_cache()
