"""Close every pre-cut activation for the Program strategy-artifact hard cut.

Issue #193 replaces the two-file ``news_semantic_program_artifact_v2`` manifest/state document —
QualityKernel, route spec, execution contract, RulePacks, DemoBank and an embedded compile receipt —
with a single ``news_program_strategy_artifact_v1`` document carrying a factory id and the two
advisory instructions.  ``factory_v5`` becomes ``factory_v6``, prompt bytes change, and every
``program_sha256`` in the old shape is unloadable in the new image.

Two consequences, and only two, are database facts:

1. Any armed or active canary points at a candidate this image can no longer execute, so it is
   tripped here rather than at worker startup.  The startup gate still fails closed; this makes the
   reason durable and legible instead of a repeated runtime trip.
2. The migration itself is recorded once, as evidence, in the append-only learning ledger.

The ``program_v7`` epoch is deliberately NOT re-opened.  This is a serialization and identity
migration, not an evidence reset: accepted ``news_review_v4`` truth stays eligible, and the epoch row
keeps naming the factory, schema and baseline sha the epoch was *opened* with — the same way
``baseline_program_sha256`` already did across the #173/#174 and #190 re-issues.

No column is added or dropped.  The artifact is JSON carried by the image, not a table.

Revision ID: 20260824_0304
Revises: 20260824_0303
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "20260824_0304"
down_revision = "20260824_0303"
branch_labels = None
depends_on = None

MIGRATION_RECEIPT = {
    "kind": "program_strategy_artifact_hard_cut",
    "source_issue": "https://github.com/AnalyThothAI/tracefold/issues/193",
    "epoch_id": "program_v7",
    "from_artifact_schema_version": "news_semantic_program_artifact_v2",
    "to_artifact_schema_version": "news_program_strategy_artifact_v1",
    "from_program_factory_id": "tracefold.news.program.factory_v5",
    "to_program_factory_id": "tracefold.news.program.factory_v6",
    "program_version": "news_semantic_program_v5",
    "prior_evidence_disposition": "accepted_review_v4_remains_eligible",
    "activation_disposition": "open_activations_tripped",
}
TRIP_REASON = "program_strategy_artifact_v1_hard_cut"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def migration_receipt_sha() -> str:
    """The artifact identity the learning ledger stores this receipt under.

    Computed exactly the way ``CandidateEvaluator._persist_artifact`` computes one, so the row this
    migration writes is indistinguishable from a receipt the application would have written.
    """

    return hashlib.sha256(_canonical({"kind": "epoch_reset", "payload": MIGRATION_RECEIPT}).encode()).hexdigest()


def upgrade() -> None:
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
    op.execute(
        sa.text(
            """
            INSERT INTO news_learning_artifacts (
              artifact_sha, kind, parent_sha, payload, created_by, created_at_ms
            )
            SELECT :artifact_sha, 'epoch_reset', NULL, CAST(:payload AS jsonb), :created_by,
                   floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint
            ON CONFLICT (artifact_sha) DO NOTHING
            """
        ).bindparams(
            artifact_sha=migration_receipt_sha(),
            payload=_canonical(MIGRATION_RECEIPT),
            created_by="migration_20260824_0304",
        )
    )


def downgrade() -> None:
    raise RuntimeError("20260824_0304 is an irreversible Program strategy-artifact hard cut")
