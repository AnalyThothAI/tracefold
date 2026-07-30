"""Persist the compact Macro projection input fingerprint."""

from __future__ import annotations

from alembic import op

revision = "20260730_0219"
down_revision = "20260730_0218"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE macro_projection_state (
          singleton_key text PRIMARY KEY
            CHECK (singleton_key = 'current'),
          input_fingerprint text NOT NULL
            CHECK (btrim(input_fingerprint) <> ''),
          feature_count integer NOT NULL
            CHECK (feature_count >= 0),
          module_count integer NOT NULL
            CHECK (module_count = 6),
          projected_at_ms bigint NOT NULL
            CHECK (projected_at_ms >= 0)
        );
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260730_0219 is an irreversible Macro projection-state hard cut")
