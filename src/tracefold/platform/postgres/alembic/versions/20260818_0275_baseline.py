"""Current PostgreSQL schema baseline (schema at 20260818_0275) plus the runtime-role contract.

Revision ID: 20260818_0275
Revises: None

Fresh databases execute the schema dump and the role/grant contract; a database that is already at
20260818_0275 continues with the chained migrations. Downgrade is a backup restore.
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "20260818_0275"
down_revision = None
branch_labels = None
depends_on = None

_ROOT = Path(__file__).resolve().parents[1]


def upgrade() -> None:
    connection = op.get_bind().connection.driver_connection
    if connection is None:
        raise RuntimeError("PostgreSQL driver connection is unavailable")
    connection.execute((_ROOT / "current_schema_20260818_0275.sql").read_text(encoding="utf-8"))
    connection.execute((_ROOT / "runtime_roles.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError("20260818_0275 is the irreversible current-schema baseline; restore a backup to downgrade")
