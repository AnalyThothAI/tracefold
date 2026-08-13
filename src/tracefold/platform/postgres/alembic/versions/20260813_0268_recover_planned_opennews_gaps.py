"""Recover planned OpenNews gaps after the next live connection.

Revision ID: 20260813_0268
Revises: 20260813_0267
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0268"
down_revision = "20260813_0267"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        WITH planned_boundaries AS (
          SELECT incident.incident_id,
                 min(item.first_observed_at_ms) AS at_ms
            FROM news_opennews_incidents incident
            JOIN news_items item
              ON item.source_id = incident.source_id
             AND item.first_ingest_mode = 'live'
             AND item.first_observed_at_ms > incident.opened_at_ms
           WHERE incident.cause_class = 'planned_shutdown'
             AND incident.planned
             AND incident.recovery_status = 'not_required'
           GROUP BY incident.incident_id
        )
        UPDATE news_opennews_incidents incident
           SET reconnected_at_ms = boundary.at_ms,
               closed_at_ms = boundary.at_ms,
               recovery_status = 'pending',
               recovery_from_at_ms = incident.opened_at_ms,
               recovery_to_at_ms = boundary.at_ms,
               last_error_code = NULL,
               updated_at_ms = greatest(incident.updated_at_ms, boundary.at_ms)
          FROM planned_boundaries boundary
         WHERE incident.incident_id = boundary.incident_id;

        UPDATE news_opennews_incidents
           SET reconnected_at_ms = NULL,
               closed_at_ms = NULL,
               recovery_status = 'pending',
               recovery_from_at_ms = opened_at_ms,
               recovery_to_at_ms = NULL,
               last_error_code = NULL
         WHERE cause_class = 'planned_shutdown'
           AND planned
           AND recovery_status = 'not_required';

        DROP INDEX ix_news_opennews_incidents_recovery;
        CREATE INDEX ix_news_opennews_incidents_recovery
          ON news_opennews_incidents(recovery_status, opened_at_ms, incident_id)
          WHERE recovery_status IN ('pending', 'partial', 'unavailable');
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        DROP INDEX ix_news_opennews_incidents_recovery;
        CREATE INDEX ix_news_opennews_incidents_recovery
          ON news_opennews_incidents(recovery_status, opened_at_ms, incident_id)
          WHERE planned = false
            AND recovery_status IN ('pending', 'partial', 'unavailable');

        UPDATE news_opennews_incidents
           SET reconnected_at_ms = opened_at_ms,
               closed_at_ms = opened_at_ms,
               recovery_status = 'not_required',
               recovery_from_at_ms = opened_at_ms,
               recovery_to_at_ms = opened_at_ms,
               recovered_count = 0,
               last_error_code = NULL
         WHERE cause_class = 'planned_shutdown'
           AND planned;
        """
    )
