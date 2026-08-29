"""Expose ordinary News kind to Review v5 and exclude structured lanes (#117).

Taxonomy Gold applies only to ``event_kind=news``.  The existing security-barrier
view is the ReviewDesk/learning seam, so filtering it here makes it impossible
for listing, OI, liquidation, or unsupported source contracts to be relabelled
through the generic taxonomy rubric.  Verdict/editorial JSONB already persists
the versioned taxonomy atomically; no parallel truth column or table is added.

The Program output, instruction, envelope, rubric and cohort identity all move.
Existing Review v4 rows remain append-only audit history but are ineligible for
Review v5 denominators. Open canaries are tripped here; worker startup opens the
new runtime-owned bundle epoch when the new image is deployed.

Revision ID: 20260829_0328
Revises: 20260829_0327
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "20260829_0328"
down_revision = "20260829_0327"
branch_labels = None
depends_on = None

TRIP_REASON = "news_taxonomy_v1_hard_cut"
PROGRAM_VERSION = "news_semantic_program_v7"
PROGRAM_SHA256 = "0cabb7c74daa023e30a6433d33425d9d73082c2bd91f9eb1bd1c2c43d6b30d24"
ENVELOPE_SHA256 = "4775cab09894b693fe825afdaec2b27aa2b76b2f206d9412bc790aea4935d90d"
TAXONOMY_VERSION = "news_taxonomy_v1"
CODEBOOK_SHA256 = "6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac"
REVIEW_RUBRIC_VERSION = "news_review_v5"
MIGRATION_RECEIPT = {
    "kind": TRIP_REASON,
    "source_issue": "https://github.com/AnalyThothAI/tracefold/issues/117",
    "program_version": PROGRAM_VERSION,
    "program_sha256": PROGRAM_SHA256,
    "envelope_sha256": ENVELOPE_SHA256,
    "taxonomy_version": TAXONOMY_VERSION,
    "codebook_sha256": CODEBOOK_SHA256,
    "review_rubric_version": REVIEW_RUBRIC_VERSION,
    "prior_evidence_disposition": "news_review_v4_and_prior_program_evidence_audit_only",
    "runtime_epoch_disposition": "new_bundle_epoch_opened_by_worker_startup",
    "activation_disposition": "open_activations_tripped",
}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def migration_receipt_sha() -> str:
    return hashlib.sha256(_canonical({"kind": "epoch_reset", "payload": MIGRATION_RECEIPT}).encode()).hexdigest()


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW news_review_task_source_v1 WITH (security_barrier = true) AS
        SELECT e.event_id,
               s.evidence_version,
               s.evidence_sha256,
               s.release_eligible AS evidence_release_eligible,
               s.snapshot AS evidence_snapshot,
               e.opened_at_ms,
               e.admission,
               e.queue_priority,
               e.storyline_key,
               e.ingest_mode,
               v.created_at_ms AS verdict_created_at_ms,
               v.evidence_version AS verdict_evidence_version,
               v.final_decision,
               v.degraded,
               v.error_code AS verdict_error_code,
               v.override_rule,
               v.throttled_by,
               v.verdict,
               v.trace,
               v.prompt_version,
               v.policy_version,
               v.model,
               d.state AS delivery_state,
               d.card AS delivery_card,
               d.settled_at_ms,
               d.error_code AS delivery_error_code,
               reaction.max_abs_return_1h_bps,
               v.program_version,
               v.program_sha256,
               v.editorial,
               v.scored_judgment_sha256,
               v.runtime_manifest_sha,
               e.event_kind
          FROM news_events e
          LEFT JOIN LATERAL (
            SELECT x.* FROM news_verdicts x
             WHERE x.event_id = e.event_id AND x.stage = 'triage'
             ORDER BY x.created_at_ms DESC LIMIT 1
          ) v ON true
          JOIN LATERAL (
            SELECT x.* FROM news_event_evidence_snapshots x
             WHERE x.event_id = e.event_id
             ORDER BY x.evidence_version DESC LIMIT 1
          ) s ON true
          LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
          LEFT JOIN LATERAL (
            SELECT max(abs(x.return_1h_bps)) AS max_abs_return_1h_bps
              FROM news_event_reactions x
             WHERE x.event_id = e.event_id
               AND x.metric_version = 'reaction_v1'
               AND x.is_primary
          ) reaction ON true
         WHERE e.event_kind = 'news'
        """
    )
    op.execute("GRANT SELECT ON news_review_task_source_v1 TO tracefold_serve, tracefold_workers")
    op.execute(
        sa.text(
            """
            UPDATE news_canary_activations
               SET state = 'tripped', revision = revision + 1, trip_reason = :trip_reason,
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
            created_by="migration_20260829_0328",
        )
    )


def downgrade() -> None:
    raise RuntimeError("20260829_0328 is an irreversible taxonomy Review hard cut")
