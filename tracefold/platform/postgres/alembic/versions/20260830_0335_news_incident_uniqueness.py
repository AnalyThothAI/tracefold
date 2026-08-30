"""One open incident per cause class, enforced by PostgreSQL (#400).

Revision ID: 20260830_0335
Revises: 20260830_0334
"""

from __future__ import annotations

from alembic import op

revision = "20260830_0335"
down_revision = "20260830_0334"
branch_labels = None
depends_on = None

_INDEX = "ux_news_opennews_incidents_open_cause"


def upgrade() -> None:
    # Preflight, not repair. Duplicate open incidents mean two writers disagreed about durable truth;
    # closing or deleting one here would erase the evidence of that, so the migration refuses instead.
    op.execute(
        f"""
        DO $$
        DECLARE
            duplicates text;
        BEGIN
            SELECT string_agg(cause_class || '=' || rows::text, ', ' ORDER BY cause_class)
              INTO duplicates
              FROM (
                    SELECT cause_class, count(*) AS rows
                      FROM news_opennews_incidents
                     WHERE closed_at_ms IS NULL
                     GROUP BY cause_class
                    HAVING count(*) > 1
                   ) AS offenders;
            IF duplicates IS NOT NULL THEN
                RAISE EXCEPTION
                    'news_incident_open_uniqueness_violated: %', duplicates
                    USING HINT = 'close the stale duplicates by hand before applying {_INDEX}';
            END IF;
        END $$
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX {_INDEX}
            ON news_opennews_incidents (cause_class)
         WHERE closed_at_ms IS NULL
        """
    )


def downgrade() -> None:
    raise RuntimeError("news_incident_uniqueness_downgrade_unsupported")
