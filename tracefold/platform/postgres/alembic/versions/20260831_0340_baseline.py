"""Current PostgreSQL schema baseline at the terminal pre-cut head.

Revision ID: 20260831_0340
Revises: None

Fresh databases execute the frozen current schema directly. A supported existing
database already stamped at 20260831_0340 remains current through the one-time
single-role cutover. Downgrade is a verified backup restore.
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "20260831_0340"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA = Path(__file__).resolve().parents[1] / "current_schema_20260831_0340.sql"


def upgrade() -> None:
    connection = op.get_bind().connection.driver_connection
    if connection is None:
        raise RuntimeError("PostgreSQL driver connection is unavailable")
    connection.execute(_SCHEMA.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError("20260831_0340 is the irreversible current-schema baseline; restore a backup to downgrade")
