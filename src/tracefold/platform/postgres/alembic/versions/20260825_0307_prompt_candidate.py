"""Admit the single Prompt candidate, and close every candidate registered under the compile chain.

Issue #202 collapses two candidate generation lifecycles into one. Until now release eligibility came
from *where* a candidate was produced: inside a sealed compiler image against a metered proxy, recorded
as a ``news_program_compile_record_v1`` carrying a sandbox launch receipt, a proxy call ledger, a
three-party build attestation and a tariff. None of that said anything about the two advisory
instructions being registered, and it meant an instruction a person wrote could not be evaluated at all
without a container reproducing it first.

Two database facts follow:

1. ``news_learning_artifacts`` gains ``prompt_candidate`` as a kind — one ``news_prompt_candidate_v1``
   document, stored under its own root, holding the typed write-set and what it was optimized against.
   The retired ``compile_receipt`` and ``compile_record`` kinds stay in the constraint: existing rows are
   append-only audit history and must remain readable (#202 §10.3).
2. Any candidate registered under the compile chain names a ``compile_record`` row that
   ``CandidateManifest`` no longer parses — ``target: program | policy`` is gone — so an activation
   pointing at one can no longer be evaluated or re-armed. Those activations are tripped here rather than
   at worker startup, so the reason is durable and legible.

The ``program_v7`` epoch is again not re-opened: accepted ``news_review_v4`` truth is unaffected by how a
candidate is serialized, and #199's frozen bundle keeps its start.

Revision ID: 20260825_0307
Revises: 20260825_0306
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260825_0307"
down_revision = "20260825_0306"
branch_labels = None
depends_on = None

TRIP_REASON = "prompt_candidate_v1_hard_cut"


def upgrade() -> None:
    op.execute("ALTER TABLE news_learning_artifacts DROP CONSTRAINT news_learning_artifact_kind")
    op.execute(
        """
        ALTER TABLE news_learning_artifacts
        ADD CONSTRAINT news_learning_artifact_kind CHECK (kind IN (
          'candidate_registration', 'proposal', 'candidate', 'dataset', 'evaluation_report', 'release_evidence',
          'active_agent', 'shadow_observation', 'canary_observation', 'deployment_receipt', 'rollback_receipt',
          'program_artifact', 'compile_receipt', 'compile_record', 'prompt_candidate', 'epoch_reset'
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
    raise RuntimeError("20260825_0307 is an irreversible single-candidate hard cut")
