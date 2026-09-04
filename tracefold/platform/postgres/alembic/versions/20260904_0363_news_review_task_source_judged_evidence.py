"""Join the review task source to the evidence version its verdict judged, not to the newest one (#548).

Migration evidence:

- category: view definition change; no table, column, row, constraint or index changes
- why_database_must_change: `news_review_task_source_v1` picks the newest evidence snapshot per Event
  and then requires `s.evidence_version = v.evidence_version` against the newest model triage verdict.
  A member joining an existing Event appends a new snapshot (`news/storage/events.py`) but does not
  re-run triage for that Event (`news/pipeline/admission.py`), so an Event with a `v2` snapshot and a
  `v1` verdict satisfies neither side of that equality and disappears from the view entirely — with its
  accepted review, its delivery and its verdict.

  The view is what `freeze` projects (`accepted_event_reviews_in_window` joins it on the *review's* own
  `evidence_version`), while `load_case` reads a version-agnostic snapshot query
  (`review_task_source`). The two therefore disagreed about the same accepted review: the freeze could
  not see it, and replay could. #534 lost four accepted Gold cases exactly this way.

  The judgment is a fact about the snapshot it read, and a later member join does not retract it. The
  snapshot lateral is therefore keyed to `v.evidence_version` rather than to `max(evidence_version)`.
  `(event_id, evidence_version)` is the snapshots table's primary key, so the lateral still yields at
  most one row and the view still yields at most one row per Event; the `ORDER BY ... LIMIT 1` that used
  to pick the newest is redundant under an equality on that key and goes with it.

  The result is a strict superset of the old one. When the newest snapshot *is* the judged one — every
  Event that has not gained a member since it was judged — both forms select the same row and every
  column is byte-identical. Only the Events the old form dropped are added, each carrying the version
  its verdict named. No consumer therefore loses a row: the ReviewDesk queue, coverage, duplicate hints
  and market cohort read the judged version instead of nothing, `_event_task_statement`'s version-free
  form still has exactly one row to order, and its explicit `evidence_version = N` form still answers
  only for the version asked. Unjudged newer evidence remains unprojected, which is the property that
  made the view current-only in the first place.
- current_source_revision: 20260904_0362
- minimum_supported_source_revision: 20260904_0362
- lock_level_and_order: one `ACCESS EXCLUSIVE` catalog lock on `public.news_review_task_source_v1` for
  the duration of a `CREATE OR REPLACE VIEW`; the four tables it reads are not locked for write and no
  other object is touched
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: 0 read or written. A view definition is a `pg_rewrite` update; no heap is scanned
- estimated_bytes: one catalog rule tuple
- rewrite_or_index_build: neither
- preflight_and_maintenance_boundary: ordinary canonical migration stop. Nothing writes through this
  view — it is read-only projection — so a reader on the old definition stays correct against the new
  one; it simply sees fewer Events than it will after
- archive_current_compatibility: fully compatible. Column names, types and order are unchanged, which
  is what `CREATE OR REPLACE VIEW` itself enforces, and the `security_barrier` property is restated
- role_and_grant_impact: none; the single `tracefold` login is unchanged
- failure_state: the transaction rolls back completely and the old definition stands
- roll_forward_or_verified_backup_restore: `downgrade` restores the previous definition exactly. This
  revision changes a rule, not data, so the reversal loses nothing — a database that downgrades simply
  stops projecting the Events whose evidence has moved on
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260904_0363
Revises: 20260904_0362
Create Date: 2026-09-04 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260904_0363"
down_revision = "20260904_0362"
branch_labels = None
depends_on = None


def _view(snapshot_source: str) -> str:
    """The whole view, with the one clause that differs between the two definitions passed in."""

    # S608: the only interpolation is one of the two module-owned literal join clauses below; no value
    # is bound and nothing here reaches user input.
    return f"""
        CREATE OR REPLACE VIEW public.news_review_task_source_v1 WITH (security_barrier = 'true') AS
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
            v.policy_version,
            v.model,
            d.state AS delivery_state,
            d.card AS delivery_card,
            d.settled_at_ms,
            d.error_code AS delivery_error_code,
            reaction.max_abs_return_1h_bps,
            v.program_version,
            v.program_sha256,
            v.judgment_contract_version,
            v.judgment_origin,
            v.editorial AS model_editorial,
            v.scored_judgment_sha256 AS judgment_sha256,
            v.runtime_manifest_sha,
            e.event_kind
           FROM public.news_events e
             JOIN LATERAL (
                   SELECT x.event_id,
                          x.stage,
                          x.policy_version,
                          x.rule_baseline_decision,
                          x.final_decision,
                          x.override_rule,
                          x.throttled_by,
                          x.verdict,
                          x.model,
                          x.prompt_version,
                          x.degraded,
                          x.error_code,
                          x.trace,
                          x.published_at_ms,
                          x.created_at_ms,
                          x.evidence_version,
                          x.evidence_sha256,
                          x.focus_fact_id,
                          x.program_version,
                          x.program_sha256,
                          x.editorial,
                          x.scored_judgment_sha256,
                          x.runtime_manifest_sha,
                          x.latency_ms,
                          x.queue_lag_ms,
                          x.reasked_after_told_change,
                          x.seen_scope,
                          x.judgment_contract_version,
                          x.judgment_origin
                     FROM public.news_verdicts x
                    WHERE x.event_id = e.event_id
                      AND x.stage = 'triage'
                      AND x.judgment_contract_version = 'news_judgment_v2'
                      AND x.judgment_origin = 'model'
                    ORDER BY x.created_at_ms DESC
                    LIMIT 1
             ) v ON true
             {snapshot_source} s
               ON (s.provenance = 'observed'
                   AND s.release_eligible
                   AND (s.snapshot ->> 'schema_version') = 'news_event_evidence_v3'
                   AND s.evidence_version = v.evidence_version
                   AND s.evidence_sha256 = v.evidence_sha256
                   AND s.focus_fact_id = v.focus_fact_id)
             LEFT JOIN public.news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
             LEFT JOIN LATERAL (
                   SELECT max(abs(x.return_1h_bps)) AS max_abs_return_1h_bps
                     FROM public.news_event_reactions x
                    WHERE x.event_id = e.event_id
                      AND x.metric_version = 'reaction_v1'
                      AND x.is_primary
             ) reaction ON true
          WHERE e.event_kind = 'news'
    """  # noqa: S608


# The snapshot the verdict actually judged. `(event_id, evidence_version)` is the primary key, so this
# lateral returns at most one row and needs no ordering to be deterministic.
_JUDGED_SNAPSHOT = """JOIN LATERAL (
                   SELECT x.event_id,
                          x.evidence_version,
                          x.focus_fact_id,
                          x.evidence_sha256,
                          x.provenance,
                          x.release_eligible,
                          x.snapshot,
                          x.created_at_ms
                     FROM public.news_event_evidence_snapshots x
                    WHERE x.event_id = e.event_id
                      AND x.evidence_version = v.evidence_version
             )"""

# The newest snapshot, whether or not any verdict judged it. This is the definition that dropped the
# whole Event when a member join moved the snapshot past the verdict.
_NEWEST_SNAPSHOT = """JOIN LATERAL (
                   SELECT x.event_id,
                          x.evidence_version,
                          x.focus_fact_id,
                          x.evidence_sha256,
                          x.provenance,
                          x.release_eligible,
                          x.snapshot,
                          x.created_at_ms
                     FROM public.news_event_evidence_snapshots x
                    WHERE x.event_id = e.event_id
                    ORDER BY x.evidence_version DESC
                    LIMIT 1
             )"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    op.execute(_view(_JUDGED_SNAPSHOT))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    # Reversible on purpose: this revision changes a projection rule, not a fact. Restoring the old
    # definition only makes the freeze blind again to reviews whose Event has since gained a member.
    op.execute(_view(_NEWEST_SNAPSHOT))
