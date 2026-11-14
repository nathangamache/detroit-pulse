"""liveness: receiver and ingest heartbeat runs, for /v1/rail/activity verdicts

Revision ID: c5e2a9d7f3b1
Revises: b7d2c41e9f10
Create Date: 2026-11-14

One row per unbroken run of heartbeats of a component ('ingest:<source>' or
'receiver:<name>'), written by the ingest stage (core/liveness.py). Idempotent.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c5e2a9d7f3b1"
down_revision: Union[str, None] = "b7d2c41e9f10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS liveness (
        component varchar(120) NOT NULL,
        up_since timestamptz NOT NULL,
        last_seen timestamptz NOT NULL,
        fingerprint text NOT NULL DEFAULT '',
        detail jsonb,
        PRIMARY KEY (component, up_since)
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_liveness_last_seen ON liveness (last_seen)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS liveness")
