"""News EventUpdate facts, semantic work, notification intents and Trading source amendments (#706).

Migration evidence:
- category: new News/Trading tables, two SQL identity functions, in-place intent identity for the
  delivery queue and ledger, one widened outbox kind and one review view join.
- why_database_must_change: the News Agent now adopts exact EventUpdate revisions (claims, evidence,
  changes) and notifies claim-scoped intents. A delivery keyed `(event_id, kind)` can say only "the
  first card of this Event", so every later update was either invisible or encoded in a kind string.
  The intent identity is the key instead; historical rows keep their state and payload under a
  deterministic legacy intent id `news_identity('legacy_intent', [event_id, kind])`, which equals
  `tracefold.news.updates.identity.identity("legacy_intent", event_id, kind)`.
- current_source_revision: 20260926_0403
- minimum_supported_source_revision: 20260926_0403
- lock_level_and_order: ACCESS EXCLUSIVE on news_delivery_queue, then news_deliveries (column adds,
  backfill, primary key swap, checks), then news_trade_events (CHECK swap) and the review view;
  SHARE ROW EXCLUSIVE on news_events/news_items for the new foreign keys.
- statement_timeout: 300s set locally; lock_timeout: 5s set locally.
- estimated_rows: news_deliveries grows ~300-500 rows/day (tens of thousands in production);
  news_delivery_queue is the in-flight set plus dead rows (hundreds at most). New tables start empty.
- estimated_bytes: one text intent id per delivery row (~80 bytes) plus one btree over it and one
  over (event_id, kind); the backfill rewrites each delivery row once.
- rewrite_or_index_build: UPDATE of every queue and ledger row for the intent id; primary-key index
  rebuild on both; one new (event_id, kind) index on the ledger; new tables' indexes are built empty.
- preflight_and_maintenance_boundary: stop Workers and serve (the usual migration gate). An old image
  writes `(event_id, kind)` conflicts that no longer exist and must not run against this schema.
- archive_current_compatibility: every existing delivery keeps state, card, receipt and history;
  nothing is re-sent. `news_verdicts` receives no new writes and is not changed. The
  `news_items.provider_params_conflict_*` columns are retained by this revision.
- role_and_grant_impact: none; the single `tracefold` login owns the new objects.
- failure_state: transactional DDL rolls back completely.
- roll_forward_or_verified_backup_restore: forward-only; restore the verified pre-0404 backup and
  the matching image to go back.
- validation_environment: isolated PostgreSQL 18 testcontainers, never the live ledger.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260926_0404
Revises: 20260926_0403
"""

from __future__ import annotations

from alembic import op

revision = "20260926_0404"
down_revision = "20260926_0403"
branch_labels = None
depends_on = None

_HEX64 = "'^[0-9a-f]{64}$'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")
    _identity_functions()
    _item_revisions()
    _semantic_tables()
    _event_update_tables()
    _notification_work()
    _delivery_queue_intents()
    _delivery_ledger_intents()
    _trade_outbox_kind()
    _trading_source_amendments()
    _review_task_source_join()


def _identity_functions() -> None:
    """The SQL twins of `tracefold.news.updates.identity.identity` and `digest(str)`.

    `news_canonical_jsonb` is already the proven twin of the Python canonical JSON. Identities are
    code-generated ASCII; the text digest normalizes to NFC exactly as the Python digest does.
    """

    op.execute(
        """
        CREATE FUNCTION public.news_identity(kind text, parts jsonb) RETURNS text
            LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
            AS $$ SELECT kind || ':' || encode(
                     sha256(convert_to(public.news_canonical_jsonb(parts), 'UTF8')), 'hex') $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.news_text_digest(value text) RETURNS text
            LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
            AS $$ SELECT encode(sha256(convert_to(
                     public.news_canonical_jsonb(to_jsonb(normalize(value, NFC))), 'UTF8')), 'hex') $$
        """
    )


def _item_revisions() -> None:
    """A later body or attribution of one provider record, retained beside its first version."""

    op.execute(
        f"""
        CREATE TABLE public.news_item_revisions (
            item_id text NOT NULL,
            body_sha256 text NOT NULL,
            evidence_text text NOT NULL,
            provider_params jsonb DEFAULT '{{}}'::jsonb NOT NULL,
            received_at_ms bigint NOT NULL,
            CONSTRAINT news_item_revisions_pkey PRIMARY KEY (item_id, body_sha256),
            CONSTRAINT news_item_revisions_item_fkey
                FOREIGN KEY (item_id) REFERENCES public.news_items(item_id) ON DELETE CASCADE,
            CONSTRAINT news_item_revisions_shape_check CHECK (
                body_sha256 ~ {_HEX64}
                AND jsonb_typeof(provider_params) = 'object'
                AND received_at_ms >= 0)
        )
        """
    )


def _semantic_tables() -> None:
    """Durable semantic work per Event, its insert-only stage checkpoints and observations.

    Pending is `wanted_revision > COALESCE(done_revision, 0)`. Attempts are bounded per wanted
    revision; the third failure stays visible (`last_outcome = 'failed'`) until a new revision.
    `attached_evidence`/`focus_claim_refs` are the material one optional read attached for the wanted
    revision of the same lineage; a new organic revision starts a new lineage and clears them.
    Checkpoints are keyed by the code-owned work identity only: the port writes them before any
    Event-scoped observation exists, so they are a retention-purged cache rather than an Event child.
    """

    op.execute(
        """
        CREATE TABLE public.news_semantic_work (
            event_id text NOT NULL,
            wanted_revision integer NOT NULL,
            done_revision integer,
            lineage_id text NOT NULL,
            attempts integer DEFAULT 0 NOT NULL,
            next_attempt_at_ms bigint NOT NULL,
            leased_until_ms bigint,
            lease_token text,
            published_at_ms bigint,
            last_outcome text,
            last_error_code text,
            extra_read_state text,
            extra_read_target_ref text,
            attached_evidence jsonb,
            focus_claim_refs jsonb,
            updated_at_ms bigint NOT NULL,
            CONSTRAINT news_semantic_work_pkey PRIMARY KEY (event_id),
            CONSTRAINT news_semantic_work_event_fkey
                FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE,
            CONSTRAINT news_semantic_work_revision_check CHECK (
                wanted_revision >= 1
                AND (done_revision IS NULL OR done_revision BETWEEN 1 AND wanted_revision)),
            CONSTRAINT news_semantic_work_attempts_check CHECK (attempts BETWEEN 0 AND 3),
            CONSTRAINT news_semantic_work_lease_check CHECK ((leased_until_ms IS NULL) = (lease_token IS NULL)),
            CONSTRAINT news_semantic_work_lineage_check CHECK (lineage_id <> ''),
            CONSTRAINT news_semantic_work_extra_read_check CHECK (
                extra_read_state IS NULL OR extra_read_state = ANY (ARRAY[
                    'reserved'::text, 'attached'::text, 'no_material'::text,
                    'unavailable_or_budget_exhausted'::text])),
            CONSTRAINT news_semantic_work_attached_check CHECK (
                (attached_evidence IS NULL) = (focus_claim_refs IS NULL)
                AND (attached_evidence IS NULL
                     OR (jsonb_typeof(attached_evidence) = 'array' AND jsonb_array_length(attached_evidence) > 0
                         AND jsonb_typeof(focus_claim_refs) = 'array')))
        )
        """
    )
    # The repair scan and the worker's claim: due pending work, oldest first.
    op.execute(
        """
        CREATE INDEX ix_news_semantic_work_pending
            ON public.news_semantic_work (next_attempt_at_ms, event_id)
         WHERE done_revision IS NULL OR done_revision < wanted_revision
        """
    )
    # One optional read per lineage: the reservation is addressed by lineage, never by Event.
    op.execute("CREATE UNIQUE INDEX ux_news_semantic_work_lineage ON public.news_semantic_work (lineage_id)")

    op.execute(
        """
        CREATE TABLE public.news_semantic_checkpoints (
            work_id text NOT NULL,
            stage text NOT NULL,
            document jsonb NOT NULL,
            created_at_ms bigint NOT NULL,
            CONSTRAINT news_semantic_checkpoints_pkey PRIMARY KEY (work_id, stage),
            CONSTRAINT news_semantic_checkpoints_stage_check
                CHECK (stage = ANY (ARRAY['extraction'::text, 'understanding'::text])),
            CONSTRAINT news_semantic_checkpoints_document_check CHECK (jsonb_typeof(document) = 'object')
        )
        """
    )
    op.execute("CREATE INDEX ix_news_semantic_checkpoints_created ON public.news_semantic_checkpoints (created_at_ms)")

    op.execute(
        f"""
        CREATE TABLE public.news_semantic_observations (
            result_id text NOT NULL,
            work_id text NOT NULL,
            event_id text NOT NULL,
            input_revision integer NOT NULL,
            input_sha256 text NOT NULL,
            program_identity text NOT NULL,
            completed_at_ms bigint NOT NULL,
            understanding jsonb NOT NULL,
            CONSTRAINT news_semantic_observations_pkey PRIMARY KEY (result_id),
            CONSTRAINT news_semantic_observations_event_fkey
                FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE,
            CONSTRAINT news_semantic_observations_shape_check CHECK (
                input_revision >= 1 AND input_sha256 ~ {_HEX64} AND program_identity <> ''
                AND completed_at_ms >= 0 AND jsonb_typeof(understanding) = 'object')
        )
        """
    )
    op.execute("CREATE INDEX ix_news_semantic_observations_work ON public.news_semantic_observations (work_id)")
    op.execute(
        "CREATE INDEX ix_news_semantic_observations_event "
        "ON public.news_semantic_observations (event_id, input_revision)"
    )

    op.execute(
        """
        CREATE TABLE public.news_judgment_cache (
            cache_key text NOT NULL,
            answer jsonb NOT NULL,
            created_at_ms bigint NOT NULL,
            CONSTRAINT news_judgment_cache_pkey PRIMARY KEY (cache_key),
            CONSTRAINT news_judgment_cache_answer_check CHECK (jsonb_typeof(answer) = 'object')
        )
        """
    )
    # The 14-day retention purge.
    op.execute("CREATE INDEX ix_news_judgment_cache_created ON public.news_judgment_cache (created_at_ms)")


def _event_update_tables() -> None:
    """Insert-only adopted revisions and the one CAS head per Event."""

    op.execute(
        f"""
        CREATE TABLE public.news_event_updates (
            event_id text NOT NULL,
            content_revision text NOT NULL,
            input_revision integer NOT NULL,
            previous_content_revision text,
            adopted_at_ms bigint NOT NULL,
            observation_result_id text NOT NULL,
            document jsonb NOT NULL,
            CONSTRAINT news_event_updates_pkey PRIMARY KEY (event_id, content_revision),
            CONSTRAINT news_event_updates_event_fkey
                FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE,
            CONSTRAINT news_event_updates_observation_fkey
                FOREIGN KEY (observation_result_id)
                REFERENCES public.news_semantic_observations(result_id) ON DELETE CASCADE,
            CONSTRAINT news_event_updates_shape_check CHECK (
                content_revision ~ {_HEX64}
                AND input_revision >= 1
                AND adopted_at_ms >= 0
                AND (previous_content_revision IS NULL
                     OR (previous_content_revision ~ {_HEX64}
                         AND previous_content_revision <> content_revision))),
            CONSTRAINT news_event_updates_document_check CHECK (
                jsonb_typeof(document) = 'object'
                AND document ->> 'schema_version' = 'news_event_update_v1'
                AND document ->> 'event_id' = event_id
                AND document ->> 'content_revision' = content_revision
                AND document -> 'input_revision' = to_jsonb(input_revision)
                AND document ->> 'previous_content_revision' IS NOT DISTINCT FROM previous_content_revision)
        )
        """
    )
    op.execute("CREATE INDEX ix_news_event_updates_observation ON public.news_event_updates (observation_result_id)")
    op.execute(
        """
        CREATE TABLE public.news_event_update_heads (
            event_id text NOT NULL,
            content_revision text NOT NULL,
            input_revision integer NOT NULL,
            update_ref text NOT NULL,
            adopted_at_ms bigint NOT NULL,
            CONSTRAINT news_event_update_heads_pkey PRIMARY KEY (event_id),
            CONSTRAINT news_event_update_heads_event_fkey
                FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE,
            CONSTRAINT news_event_update_heads_update_fkey
                FOREIGN KEY (event_id, content_revision)
                REFERENCES public.news_event_updates(event_id, content_revision) ON DELETE CASCADE,
            CONSTRAINT news_event_update_heads_ref_check CHECK (
                input_revision >= 1 AND adopted_at_ms >= 0
                AND update_ref = public.news_identity('update', jsonb_build_array(event_id, content_revision)))
        )
        """
    )
    # A notification plan names its head by `update_ref` alone; the plan CAS resolves it here.
    op.execute("CREATE UNIQUE INDEX ux_news_event_update_heads_ref ON public.news_event_update_heads (update_ref)")


def _notification_work() -> None:
    """One pending marker per Event and logical channel, holding the last plan with claim decisions."""

    op.execute(
        f"""
        CREATE TABLE public.news_notification_work (
            event_id text NOT NULL,
            channel text NOT NULL,
            content_revision text NOT NULL,
            state text NOT NULL,
            plan jsonb,
            reader_revision text,
            attempts integer DEFAULT 0 NOT NULL,
            next_attempt_at_ms bigint NOT NULL,
            updated_at_ms bigint NOT NULL,
            CONSTRAINT news_notification_work_pkey PRIMARY KEY (event_id, channel),
            CONSTRAINT news_notification_work_event_fkey
                FOREIGN KEY (event_id) REFERENCES public.news_events(event_id) ON DELETE CASCADE,
            CONSTRAINT news_notification_work_channel_check CHECK (channel = 'news'),
            CONSTRAINT news_notification_work_state_check
                CHECK (state = ANY (ARRAY['pending'::text, 'done'::text])),
            CONSTRAINT news_notification_work_attempts_check CHECK (attempts BETWEEN 0 AND 3),
            CONSTRAINT news_notification_work_plan_check CHECK (
                content_revision ~ {_HEX64}
                AND (state = 'pending' OR plan IS NOT NULL)
                AND (plan IS NULL) = (reader_revision IS NULL)
                AND (plan IS NULL OR (jsonb_typeof(plan) = 'object'
                                      AND plan ->> 'reader_revision' = reader_revision
                                      AND plan ->> 'channel' = channel)))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_notification_work_pending
            ON public.news_notification_work (channel, next_attempt_at_ms, event_id)
         WHERE state = 'pending'
        """
    )


def _delivery_queue_intents() -> None:
    """The queue row is an intent: legacy rows get their deterministic id, new rows carry the selection."""

    op.execute(
        """
        ALTER TABLE public.news_delivery_queue
            ADD COLUMN intent_id text,
            ADD COLUMN content_revision text,
            ADD COLUMN claim_refs jsonb,
            ADD COLUMN plan_key boolean,
            ADD COLUMN frozen_card jsonb,
            ADD COLUMN lease_token text
        """
    )
    op.execute(
        "UPDATE public.news_delivery_queue "
        "SET intent_id = public.news_identity('legacy_intent', jsonb_build_array(event_id, kind))"
    )
    op.execute("ALTER TABLE public.news_delivery_queue ALTER COLUMN intent_id SET NOT NULL")
    op.execute("ALTER TABLE public.news_delivery_queue DROP CONSTRAINT news_delivery_queue_pkey")
    op.execute("ALTER TABLE public.news_delivery_queue ADD CONSTRAINT news_delivery_queue_pkey PRIMARY KEY (intent_id)")
    op.execute("ALTER TABLE public.news_delivery_queue DROP CONSTRAINT news_delivery_queue_kind_check")
    op.execute(
        f"""
        ALTER TABLE public.news_delivery_queue
            ADD CONSTRAINT news_delivery_queue_kind_check
                CHECK (kind = ANY (ARRAY['first'::text, 'followup'::text, 'update'::text])),
            ADD CONSTRAINT news_delivery_queue_intent_check CHECK (
                CASE WHEN kind = 'update' THEN
                    intent_id ~ '^intent:[0-9a-f]{{64}}$'
                    AND content_revision IS NOT NULL AND content_revision ~ {_HEX64}
                    AND jsonb_typeof(claim_refs) = 'array' AND jsonb_array_length(claim_refs) > 0
                    AND plan_key IS NOT NULL
                    AND (frozen_card IS NULL OR (jsonb_typeof(frozen_card) = 'object'
                                                 AND frozen_card ->> 'intent_id' = intent_id))
                ELSE
                    intent_id = public.news_identity('legacy_intent', jsonb_build_array(event_id, kind))
                    AND content_revision IS NULL AND claim_refs IS NULL AND plan_key IS NULL
                    AND frozen_card IS NULL AND lease_token IS NULL
                END)
        """
    )
    # The claim order's tie-breaker is now the intent identity: an Event may owe several intents.
    op.execute("DROP INDEX public.ix_news_delivery_queue_due")
    op.execute(
        """
        CREATE INDEX ix_news_delivery_queue_due
            ON public.news_delivery_queue (next_attempt_at_ms, enqueued_at_ms, intent_id)
         WHERE state = 'pending'
        """
    )
    # The primary key used to lead with event_id; the detail and feed reads still find an Event's intents by it.
    op.execute("CREATE INDEX ix_news_delivery_queue_event ON public.news_delivery_queue (event_id)")


def _delivery_ledger_intents() -> None:
    """The ledger row is keyed by intent; an update row retains the exact frozen body it sent.

    Legacy `(event_id, kind)` uniqueness is kept by the identity CHECK beside the primary key: a legacy
    kind's intent id is a function of exactly that pair. `ix_news_deliveries_event` replaces the old
    primary key's `(event_id, kind)` index for every join from an Event to its deliveries.
    """

    op.execute(
        """
        ALTER TABLE public.news_deliveries
            ADD COLUMN intent_id text,
            ADD COLUMN content_revision text,
            ADD COLUMN claim_refs jsonb,
            ADD COLUMN body text,
            ADD COLUMN payload_sha256 text,
            ADD COLUMN plan_key boolean
        """
    )
    op.execute(
        "UPDATE public.news_deliveries "
        "SET intent_id = public.news_identity('legacy_intent', jsonb_build_array(event_id, kind))"
    )
    op.execute("ALTER TABLE public.news_deliveries ALTER COLUMN intent_id SET NOT NULL")
    op.execute("ALTER TABLE public.news_deliveries DROP CONSTRAINT news_deliveries_pkey")
    op.execute("ALTER TABLE public.news_deliveries ADD CONSTRAINT news_deliveries_pkey PRIMARY KEY (intent_id)")
    op.execute("CREATE INDEX ix_news_deliveries_event ON public.news_deliveries (event_id, kind)")
    op.execute(
        """
        ALTER TABLE public.news_deliveries
            DROP CONSTRAINT news_deliveries_kind_check,
            DROP CONSTRAINT news_deliveries_state_check
        """
    )
    op.execute(
        f"""
        ALTER TABLE public.news_deliveries
            ADD CONSTRAINT news_deliveries_kind_check
                CHECK (kind = ANY (ARRAY['first'::text, 'followup'::text, 'update'::text])),
            -- `ambiguous` is an update intent held for reconciliation; legacy rows keep `terminal`.
            ADD CONSTRAINT news_deliveries_state_check CHECK (
                state = ANY (ARRAY['sending'::text, 'sent'::text, 'terminal'::text, 'ambiguous'::text])
                AND (state <> 'ambiguous' OR kind = 'update')),
            ADD CONSTRAINT news_deliveries_intent_check CHECK (
                CASE WHEN kind = 'update' THEN
                    intent_id ~ '^intent:[0-9a-f]{{64}}$'
                    AND content_revision IS NOT NULL AND content_revision ~ {_HEX64}
                    AND jsonb_typeof(claim_refs) = 'array' AND jsonb_array_length(claim_refs) > 0
                    AND body IS NOT NULL AND plan_key IS NOT NULL
                    AND payload_sha256 IS NOT NULL AND payload_sha256 = public.news_text_digest(body)
                ELSE
                    intent_id = public.news_identity('legacy_intent', jsonb_build_array(event_id, kind))
                    AND content_revision IS NULL AND claim_refs IS NULL AND body IS NULL
                    AND payload_sha256 IS NULL AND plan_key IS NULL
                END)
        """
    )


def _trade_outbox_kind() -> None:
    """A source correction is its own public kind; a catalyst delta stays `catalyst`."""

    op.execute("ALTER TABLE public.news_trade_events DROP CONSTRAINT news_trade_events_kind_check")
    op.execute(
        """
        ALTER TABLE public.news_trade_events ADD CONSTRAINT news_trade_events_kind_check
            CHECK (kind = ANY (ARRAY['catalyst'::text, 'oi'::text, 'source_update'::text]))
        """
    )


def _trading_source_amendments() -> None:
    """Trading's idempotent receipt of a News source update; never a trigger or a Case."""

    op.execute(
        f"""
        CREATE TABLE public.trading_source_amendments (
            update_id text NOT NULL,
            source_fact_key text NOT NULL,
            content_revision text NOT NULL,
            affected_claim_refs jsonb NOT NULL,
            retired_claim_refs jsonb NOT NULL,
            payload jsonb NOT NULL,
            payload_sha256 text NOT NULL,
            received_at_ms bigint NOT NULL,
            CONSTRAINT trading_source_amendments_pkey PRIMARY KEY (update_id),
            CONSTRAINT trading_source_amendments_shape_check CHECK (
                source_fact_key <> '' AND content_revision <> ''
                AND jsonb_typeof(affected_claim_refs) = 'array'
                AND jsonb_typeof(retired_claim_refs) = 'array'
                AND jsonb_typeof(payload) = 'object'
                AND payload_sha256 ~ {_HEX64}
                AND received_at_ms >= 0)
        )
        """
    )
    op.execute("CREATE INDEX ix_trading_source_amendments_source ON public.trading_source_amendments (source_fact_key)")


def _review_task_source_join() -> None:
    """Restate the review view in place with one delivery per Event across first and update intents.

    The view joined `kind = 'first'` directly, one row per Event. An Event may now owe several update
    intents, so the join becomes a lateral pick of its earliest reader delivery (a legacy `first`
    before any update) and the view keeps its one-row-per-Event shape and column list.
    """

    op.execute(
        """
        DO $migration$
        DECLARE definition text;
        DECLARE replaced text;
        BEGIN
          SELECT pg_get_viewdef('public.news_review_task_source_v1'::regclass) INTO STRICT definition;
          replaced := replace(definition,
            'LEFT JOIN news_deliveries d ON (((d.event_id = e.event_id) AND (d.kind = ''first''::text)))',
            'LEFT JOIN LATERAL ( SELECT delivery.state, delivery.card, delivery.settled_at_ms, delivery.error_code'
            ' FROM news_deliveries delivery'
            ' WHERE delivery.event_id = e.event_id AND delivery.kind = ANY (ARRAY[''first''::text, ''update''::text])'
            ' ORDER BY (delivery.kind = ''first'') DESC, delivery.created_at_ms, delivery.intent_id'
            ' LIMIT 1) d ON (true)');
          IF replaced = definition THEN
            RAISE EXCEPTION 'unexpected_news_review_task_source_definition';
          END IF;
          EXECUTE 'CREATE OR REPLACE VIEW public.news_review_task_source_v1 WITH (security_barrier = true) AS '
            || replaced;
        END
        $migration$
        """
    )


def downgrade() -> None:
    raise RuntimeError("news_event_updates_forward_only: restore a verified pre-0404 archive")
