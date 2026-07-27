"""Remove the retired custom PostgreSQL observability runtime."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260727_0208"
down_revision = "20260727_0207"
branch_labels = None
depends_on = None

_RETIRED_EXTENSIONS = (
    "pg_stat_kcache",
    "pg_qualstats",
    "pg_wait_sampling",
)
_RETIRED_SYSTEM_SETTINGS = (
    "powa.coalesce",
    "powa.frequency",
)


def upgrade() -> None:
    bind = op.get_bind()
    for extension_name in _RETIRED_EXTENSIONS:
        installed = bind.execute(
            sa.text(
                """
                SELECT EXISTS (
                  SELECT 1
                  FROM pg_extension
                  WHERE extname = :extension_name
                )
                """
            ),
            {"extension_name": extension_name},
        ).scalar_one()
        if installed:
            op.execute(f'DROP EXTENSION "{extension_name}"')

    powa_database_exists = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'powa')")
    ).scalar_one()
    with op.get_context().autocommit_block():
        for setting_name in _RETIRED_SYSTEM_SETTINGS:
            op.execute(f'ALTER SYSTEM RESET "{setting_name}"')
        op.execute("SELECT pg_reload_conf()")
        if powa_database_exists:
            op.execute('DROP DATABASE "powa" WITH (FORCE)')


def downgrade() -> None:
    pass
