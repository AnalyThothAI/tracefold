"""Current PostgreSQL schema baseline after the 0210 hard cut."""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "20260728_0210"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "current_schema_20260728_0210.sql"
    connection = op.get_bind().connection.driver_connection
    if connection is None:
        raise RuntimeError("PostgreSQL driver connection is unavailable")
    connection.execute(schema_path.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError("20260728_0210 is the irreversible current-schema baseline; restore a backup to downgrade")
