"""Admit the single compile record, and close every candidate written against the old chain.

Issue #193 PR-B replaces the seven content-addressed compile receipts, their chain root, the runner
receipt, the optimizer provenance record and the machine diff with one ``news_program_compile_record_v1``
document.  Between them those five documents carried the same four identities — parent Program,
dataset, runtime manifest, patch — up to four times each, cross-bound by hashes that every party
computed from payloads it already held.

Two database facts follow:

1. ``news_learning_artifacts`` gains ``compile_record`` as a kind.  The retired ``compile_receipt``
   kind stays in the constraint: existing rows are audit history and must remain readable.
2. Any candidate registered under the old chain names a ``compile_receipt`` row that no longer
   validates, and any activation pointing at one can no longer be evaluated.  Those activations are
   tripped here rather than at worker startup, so the reason is durable and legible.

The ``program_v7`` epoch is again not re-opened: accepted ``news_review_v4`` truth is unaffected by how
a compile is serialized.

Revision ID: 20260825_0305
Revises: 20260824_0304
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260825_0305"
down_revision = "20260824_0304"
branch_labels = None
depends_on = None

TRIP_REASON = "compile_record_v1_hard_cut"


def upgrade() -> None:
    op.execute("ALTER TABLE news_learning_artifacts DROP CONSTRAINT news_learning_artifact_kind")
    op.execute(
        """
        ALTER TABLE news_learning_artifacts
        ADD CONSTRAINT news_learning_artifact_kind CHECK (kind IN (
          'candidate_registration', 'proposal', 'candidate', 'dataset', 'evaluation_report', 'release_evidence',
          'active_agent', 'shadow_observation', 'canary_observation', 'deployment_receipt', 'rollback_receipt',
          'program_artifact', 'compile_receipt', 'compile_record', 'epoch_reset'
        ))
        """
    )
    op.execute(
        sa.text(
            """
            UPDATE news_canary_activations
               SET state = 'tripped',
                   revision = revision + 1,
                   trip_reason = :trip_reason,
                   tripped_at_ms = floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint
             WHERE state IN ('armed', 'active')
            """
        ).bindparams(trip_reason=TRIP_REASON)
    )


def downgrade() -> None:
    raise RuntimeError("20260825_0305 is an irreversible compile-record hard cut")
