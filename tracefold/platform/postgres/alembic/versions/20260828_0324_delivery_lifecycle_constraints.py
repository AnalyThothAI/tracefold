"""Make delivery edit/delete lifecycle shape constraints two-valued and fail closed.

Revision ID: 20260828_0324
Revises: 20260828_0323
"""

from __future__ import annotations

from alembic import op

revision = "20260828_0324"
down_revision = "20260828_0323"
branch_labels = None
depends_on = None


_EDIT_SHAPE = """
(
  edit_state IS NULL
  AND pending_card IS NULL
  AND edit_error_code IS NULL
  AND edit_attempted_at_ms IS NULL
  AND edit_settled_at_ms IS NULL
) OR (
  edit_state IS NOT NULL
  AND (
    (
      edit_state = 'editing'
      AND pending_card IS NOT NULL
      AND edit_error_code IS NULL
      AND edit_attempted_at_ms IS NOT NULL
      AND edit_settled_at_ms IS NULL
    ) OR (
      edit_state = 'edited'
      AND pending_card IS NULL
      AND edit_error_code IS NULL
      AND edit_attempted_at_ms IS NOT NULL
      AND edit_settled_at_ms IS NOT NULL
    ) OR (
      edit_state = 'ambiguous'
      AND pending_card IS NOT NULL
      AND edit_error_code IS NOT NULL
      AND edit_attempted_at_ms IS NOT NULL
      AND edit_settled_at_ms IS NOT NULL
    )
  )
)
"""

_DELETE_SHAPE = """
(
  delete_state IS NULL
  AND delete_evidence IS NULL
  AND delete_reason IS NULL
  AND delete_error_code IS NULL
  AND delete_attempted_at_ms IS NULL
  AND delete_settled_at_ms IS NULL
) OR (
  delete_state IS NOT NULL
  AND (
    (
      delete_state = 'deleting'
      AND delete_evidence IS NOT NULL
      AND delete_reason IS NOT NULL
      AND delete_error_code IS NULL
      AND delete_attempted_at_ms IS NOT NULL
      AND delete_settled_at_ms IS NULL
    ) OR (
      delete_state = 'deleted'
      AND delete_evidence IS NOT NULL
      AND delete_reason IS NOT NULL
      AND delete_error_code IS NULL
      AND delete_attempted_at_ms IS NOT NULL
      AND delete_settled_at_ms IS NOT NULL
    ) OR (
      delete_state = 'ambiguous'
      AND delete_evidence IS NOT NULL
      AND delete_reason IS NOT NULL
      AND delete_error_code IS NOT NULL
      AND delete_attempted_at_ms IS NOT NULL
      AND delete_settled_at_ms IS NOT NULL
    )
  )
)
"""


def upgrade() -> None:
    op.execute(
        f"""
        DO $migration$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM news_deliveries
             WHERE ({_EDIT_SHAPE}) IS NOT TRUE
                OR ({_DELETE_SHAPE}) IS NOT TRUE
          ) THEN
            RAISE EXCEPTION 'news_delivery_lifecycle_shape_invalid';
          END IF;

          ALTER TABLE news_deliveries
            DROP CONSTRAINT news_deliveries_edit_shape_check,
            DROP CONSTRAINT news_deliveries_delete_shape_check,
            ADD CONSTRAINT news_deliveries_edit_shape_check CHECK ({_EDIT_SHAPE}),
            ADD CONSTRAINT news_deliveries_delete_shape_check CHECK ({_DELETE_SHAPE});
        END
        $migration$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260828_0324 owns fail-closed delivery lifecycle constraints and cannot be downgraded")
