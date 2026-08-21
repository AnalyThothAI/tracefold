"""Review v2: append-only judgments, external misses, and the narrow review task view.

The legacy mutually-exclusive label table is hard-cut here.  Its rows, if
any, are copied byte-for-byte into non-release-eligible legacy review records;
the migration verifies both count and canonical payload hash before dropping
the old table.  No v2 rubric values are guessed from a v1 label.

Revision ID: 20260821_0285
Revises: 20260821_0284
"""

from __future__ import annotations

from alembic import op

revision = "20260821_0285"
down_revision = "20260821_0284"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_review') THEN
            RAISE EXCEPTION 'tracefold_review_role_missing:provision_runtime_role_before_migration';
          END IF;
        END $$
        """
    )
    op.execute(
        """
        CREATE TABLE news_external_miss_snapshots (
          snapshot_id       text   PRIMARY KEY,
          evidence_sha256   text   NOT NULL,
          source_url        text   NOT NULL,
          title             text   NOT NULL,
          body              text   NOT NULL DEFAULT '',
          occurred_at_ms    bigint NOT NULL,
          observed_at_ms    bigint NOT NULL,
          provenance        text   NOT NULL,
          snapshot          jsonb  NOT NULL,
          created_by        text   NOT NULL,
          created_at_ms     bigint NOT NULL,
          CONSTRAINT news_external_miss_id_sha CHECK (snapshot_id ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_external_miss_evidence_sha CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_external_miss_source_url_nonempty CHECK (btrim(source_url) <> ''),
          CONSTRAINT news_external_miss_title_nonempty CHECK (btrim(title) <> ''),
          CONSTRAINT news_external_miss_time_order CHECK (observed_at_ms >= occurred_at_ms),
          CONSTRAINT news_external_miss_snapshot_object CHECK (jsonb_typeof(snapshot) = 'object')
        )
        """
    )
    op.execute("CREATE INDEX ix_news_external_miss_created ON news_external_miss_snapshots (created_at_ms DESC)")

    op.execute(
        """
        CREATE TABLE news_reviews (
          review_id               text    PRIMARY KEY,
          idempotency_key         text,
          idempotency_request_sha text,
          review_kind             text    NOT NULL,
          subject_kind            text    NOT NULL,
          task_id                 text    NOT NULL,
          task_version            text    NOT NULL,
          event_id                text,
          evidence_version        integer,
          external_snapshot_id    text,
          pairwise_case_id        text,
          rubric_version          text    NOT NULL,
          reader_contract_version text    NOT NULL,
          reviewer                text    NOT NULL,
          should_push             text,
          dimensions              jsonb   NOT NULL DEFAULT '{}'::jsonb,
          novelty                 jsonb   NOT NULL DEFAULT '{}'::jsonb,
          first_bad_owner         text,
          evidence_refs           jsonb   NOT NULL DEFAULT '[]'::jsonb,
          expected_correction     text    NOT NULL DEFAULT '',
          note                    text    NOT NULL DEFAULT '',
          selection               jsonb   NOT NULL DEFAULT '{}'::jsonb,
          payload                 jsonb   NOT NULL DEFAULT '{}'::jsonb,
          supersedes_review_id    text,
          accepts_review_id       text,
          release_eligible        boolean NOT NULL DEFAULT true,
          created_at_ms           bigint  NOT NULL,
          CONSTRAINT news_reviews_id_sha CHECK (review_id ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_reviews_idempotency_request_sha CHECK (
            idempotency_request_sha IS NULL OR idempotency_request_sha ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT news_reviews_kind_check CHECK (review_kind IN ('judgment', 'acceptance', 'legacy')),
          CONSTRAINT news_reviews_subject_check
            CHECK (subject_kind IN ('event', 'external_miss', 'pairwise', 'legacy_label')),
          CONSTRAINT news_reviews_should_push_check CHECK (
            should_push IS NULL OR should_push IN
              ('must_push', 'should_push', 'should_hold', 'must_hold', 'uncertain')
          ),
          CONSTRAINT news_reviews_owner_check CHECK (
            first_bad_owner IS NULL OR first_bad_owner IN
              ('receiver', 'deduper', 'event_evidence', 'gate', 'retrieval', 'storyline',
               'triage_prompt', 'model', 'policy', 'delivery', 'taxonomy', 'unknown')
          ),
          CONSTRAINT news_reviews_dimensions_object CHECK (jsonb_typeof(dimensions) = 'object'),
          CONSTRAINT news_reviews_novelty_object CHECK (jsonb_typeof(novelty) = 'object'),
          CONSTRAINT news_reviews_evidence_refs_array CHECK (jsonb_typeof(evidence_refs) = 'array'),
          CONSTRAINT news_reviews_selection_object CHECK (jsonb_typeof(selection) = 'object'),
          CONSTRAINT news_reviews_payload_object CHECK (jsonb_typeof(payload) = 'object'),
          CONSTRAINT news_reviews_event_subject CHECK (
            subject_kind <> 'event' OR (event_id IS NOT NULL AND evidence_version IS NOT NULL)
          ),
          CONSTRAINT news_reviews_external_subject CHECK (
            subject_kind <> 'external_miss' OR external_snapshot_id IS NOT NULL
          ),
          CONSTRAINT news_reviews_pairwise_subject CHECK (
            subject_kind <> 'pairwise' OR pairwise_case_id IS NOT NULL
          ),
          CONSTRAINT news_reviews_acceptance_ref CHECK (
            (review_kind = 'acceptance') = (accepts_review_id IS NOT NULL)
          )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_news_reviews_idempotency ON news_reviews (reviewer, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_news_reviews_event_created ON news_reviews (event_id, created_at_ms DESC)")
    op.execute(
        "CREATE INDEX ix_news_reviews_external_created ON news_reviews (external_snapshot_id, created_at_ms DESC)"
    )
    op.execute("CREATE INDEX ix_news_reviews_task_created ON news_reviews (task_id, created_at_ms DESC)")
    op.execute(
        "CREATE INDEX ix_news_reviews_accepted ON news_reviews (accepts_review_id, created_at_ms DESC) "
        "WHERE review_kind = 'acceptance'"
    )

    # The review login can revalidate a virtual task without being able to read
    # any News business table directly. The task follows the latest immutable
    # evidence snapshot while separately naming the evidence version used by
    # the latest verdict. Stronger post-verdict evidence is therefore a new
    # task and cannot inherit an acceptance for the old question.
    op.execute(
        """
        CREATE VIEW news_review_task_source_v1 WITH (security_barrier = true) AS
        SELECT e.event_id,
               s.evidence_version,
               s.evidence_sha256,
               s.release_eligible AS evidence_release_eligible,
               s.snapshot AS evidence_snapshot,
               e.opened_at_ms,
               e.admission,
               e.priority,
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
               reaction.max_abs_return_1h_bps
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
        """
    )
    # The narrow HTTP writer can fold append-only judgments and re-read an
    # external snapshot without gaining SELECT on either base table.  These
    # views contain no News business rows or candidate arm mapping.
    op.execute(
        """
        CREATE VIEW news_review_records_v1 WITH (security_barrier = true) AS
        SELECT review_id, idempotency_key, idempotency_request_sha, review_kind,
               subject_kind, task_id, task_version, event_id, evidence_version,
               external_snapshot_id, pairwise_case_id, rubric_version,
               reader_contract_version, reviewer, should_push, dimensions,
               novelty, first_bad_owner, evidence_refs, expected_correction,
               note, selection, payload, supersedes_review_id,
               accepts_review_id, release_eligible, created_at_ms
          FROM news_reviews
        """
    )
    op.execute(
        """
        CREATE VIEW news_review_external_source_v1 WITH (security_barrier = true) AS
        SELECT snapshot_id, evidence_sha256, source_url, title, body,
               occurred_at_ms, observed_at_ms, provenance, snapshot,
               created_at_ms
          FROM news_external_miss_snapshots
        """
    )

    # Preserve legacy rows without pretending that an enum label supplies the
    # multi-dimensional v2 rubric.  Both hashes are over exactly the old rows.
    op.execute(
        """
        INSERT INTO news_reviews (
          review_id, review_kind, subject_kind, task_id, task_version,
          event_id, rubric_version, reader_contract_version, reviewer,
          selection, payload, release_eligible, created_at_ms
        )
        SELECT encode(sha256(convert_to('legacy-label:' || l.label_id, 'UTF8')), 'hex'),
               'legacy', 'legacy_label', 'legacy:' || l.label_id, 'legacy:' || l.label_id,
               l.event_id, 'news_label_v1_legacy', 'unknown', l.labeled_by,
               '{}'::jsonb, to_jsonb(l), false, l.created_at_ms
          FROM news_event_labels l
        """
    )
    op.execute(
        """
        DO $$
        DECLARE source_count bigint; target_count bigint; source_hash text; target_hash text;
        BEGIN
          SELECT count(*), encode(sha256(convert_to(
                   COALESCE(string_agg(to_jsonb(l)::text, '' ORDER BY l.label_id), ''), 'UTF8')), 'hex')
            INTO source_count, source_hash FROM news_event_labels l;
          SELECT count(*), encode(sha256(convert_to(
                   COALESCE(string_agg(r.payload::text, '' ORDER BY r.payload ->> 'label_id'), ''), 'UTF8')), 'hex')
            INTO target_count, target_hash FROM news_reviews r WHERE r.review_kind = 'legacy';
          IF source_count <> target_count OR source_hash <> target_hash THEN
            RAISE EXCEPTION 'news_label_v1_migration_mismatch:%:%:%:%',
              source_count, target_count, source_hash, target_hash;
          END IF;
        END $$
        """
    )
    op.execute("DROP TABLE news_event_labels")

    op.execute(
        """
        CREATE FUNCTION reject_news_review_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'news_review_append_only';
        END;
        $$
        """
    )
    for table in ("news_reviews", "news_external_miss_snapshots"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_news_review_mutation()"
        )

    op.execute("GRANT SELECT ON news_reviews, news_external_miss_snapshots TO tracefold_serve")
    op.execute("GRANT SELECT ON news_reviews, news_external_miss_snapshots TO tracefold_workers")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON news_reviews, news_external_miss_snapshots FROM tracefold_workers")
    op.execute("GRANT SELECT ON news_review_task_source_v1 TO tracefold_serve, tracefold_workers")
    op.execute("GRANT INSERT ON news_reviews, news_external_miss_snapshots TO tracefold_review")
    op.execute(
        "GRANT SELECT ON news_review_task_source_v1, news_review_records_v1, "
        "news_review_external_source_v1 TO tracefold_review"
    )
    op.execute(
        "GRANT SELECT ON news_review_records_v1, news_review_external_source_v1 TO tracefold_serve, tracefold_workers"
    )
    op.execute("REVOKE SELECT ON news_reviews, news_external_miss_snapshots FROM tracefold_review")
    op.execute("REVOKE UPDATE, DELETE ON news_reviews, news_external_miss_snapshots FROM tracefold_review")


def downgrade() -> None:
    raise RuntimeError("20260821_0285 is an irreversible Review v2 hard cut")
