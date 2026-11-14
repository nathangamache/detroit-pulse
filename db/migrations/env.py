"""Alembic environment. The URL comes from core.settings (DATABASE_URL or POSTGRES_*),
or from `-x db=<name>` to target another database on the same server, e.g.

    uv run alembic upgrade head                       # live DB (detroitpulse)
    uv run alembic -x db=detroitpulse_test upgrade head
    uv run alembic -x db=detroitpulse_archive upgrade head
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.settings import get_settings  # noqa: E402
from db.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# PostGIS / tiger tables live in the same DB; never let autogenerate touch them
# (the 2026-03 autogenerate migration tried to DROP spatial_ref_sys and the tiger schema).
_OURS = {"incidents", "transcript_chunks", "rail_telemetry", "rail_detector_events", "llm_usage",
         "geocode_cache", "pipeline_events", "liveness", "receiver_squelch", "shadow_log", "pipeline_metrics",
         "lora_model_registry"}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in _OURS
    return True


def _url() -> str:
    x = context.get_x_argument(as_dictionary=True)
    s = get_settings()
    return s.db_url_for(x["db"]) if x.get("db") else s.db_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      include_object=include_object, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
