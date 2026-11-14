"""receiver_squelch: per-sample squelch openings of rtl_airband's channels, for /v1/rail/activity verdicts

Revision ID: d8b3f6a1c2e4
Revises: c5e2a9d7f3b1
Create Date: 2026-11-14

One row per sample of a receiver's stats file (core/liveness.py observe_receiver) and channel
whose counters grew since the previous sample: squelch openings, transmissions dropped as too
short, output loops with the squelch open. rtl_airband stops writing a channel's recordings
after one failed open or write while it keeps demodulating, and the verdict compares these with
the recordings in the window. Idempotent.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d8b3f6a1c2e4"
down_revision: Union[str, None] = "c5e2a9d7f3b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS receiver_squelch (
        component varchar(120) NOT NULL,
        at timestamptz NOT NULL,
        label text NOT NULL DEFAULT '',
        freq_mhz double precision NOT NULL,
        opens integer NOT NULL DEFAULT 0,
        dropped integer NOT NULL DEFAULT 0,
        active integer NOT NULL DEFAULT 0,
        PRIMARY KEY (component, at, label, freq_mhz)
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_receiver_squelch_at ON receiver_squelch (at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS receiver_squelch")
