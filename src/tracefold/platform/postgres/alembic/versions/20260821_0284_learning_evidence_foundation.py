"""FactUnit and immutable EventEvidenceSnapshot foundation for #112.

The old Triage read joined the current mutable Event to its first Item.  A
later member could therefore make the detail page describe evidence the model
never saw, and a stronger member could upgrade Gate facts without changing the
leader text.  This revision gives every Event one focused FactUnit and stores
append-only evidence versions.  Every new verdict names the exact version it
read.

Existing Events receive version 0 with ``legacy_reconstructed`` provenance.
It is useful for discovery and UI archaeology but is explicitly not eligible
as release evidence because it cannot reconstruct the historical model input.

Revision ID: 20260821_0284
Revises: 20260820_0283
"""

from __future__ import annotations

from alembic import op

revision = "20260821_0284"
down_revision = "20260820_0283"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE news_events ADD COLUMN focus_fact_id text")
    op.execute("ALTER TABLE news_events ADD COLUMN focus_fact_text text NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE news_events ADD COLUMN focus_fact_context text NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE news_events ADD COLUMN focus_fact_method text NOT NULL DEFAULT 'legacy_reconstructed'")
    op.execute("ALTER TABLE news_events ADD COLUMN focus_span_start integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE news_events ADD COLUMN focus_span_end integer NOT NULL DEFAULT 0")
    op.execute("UPDATE news_events SET focus_fact_id = 'legacy:' || event_id")
    op.execute("ALTER TABLE news_events ALTER COLUMN focus_fact_id SET NOT NULL")
    op.execute("ALTER TABLE news_events ADD CONSTRAINT news_events_focus_fact_id_nonempty CHECK (focus_fact_id <> '')")

    op.execute("ALTER TABLE news_event_members ADD COLUMN fact_id text")
    op.execute("ALTER TABLE news_event_members ADD COLUMN fact_text text NOT NULL DEFAULT ''")
    op.execute(
        "UPDATE news_event_members m SET fact_id = e.focus_fact_id, fact_text = e.leader_title "
        "FROM news_events e WHERE e.event_id = m.event_id"
    )
    op.execute("ALTER TABLE news_event_members ALTER COLUMN fact_id SET NOT NULL")
    op.execute("ALTER TABLE news_event_members DROP CONSTRAINT news_event_members_pkey")
    op.execute(
        "ALTER TABLE news_event_members ADD CONSTRAINT news_event_members_pkey PRIMARY KEY (event_id, item_id, fact_id)"
    )
    op.execute(
        "ALTER TABLE news_event_members ADD CONSTRAINT news_event_members_fact_id_nonempty CHECK (fact_id <> '')"
    )

    op.execute(
        """
        CREATE TABLE news_event_evidence_snapshots (
          event_id          text    NOT NULL,
          evidence_version integer NOT NULL,
          focus_fact_id     text    NOT NULL,
          evidence_sha256   text    NOT NULL,
          provenance        text    NOT NULL,
          release_eligible  boolean NOT NULL DEFAULT true,
          snapshot          jsonb   NOT NULL,
          created_at_ms     bigint  NOT NULL,
          PRIMARY KEY (event_id, evidence_version),
          CONSTRAINT news_event_evidence_version_check CHECK (evidence_version >= 0),
          CONSTRAINT news_event_evidence_sha_check CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_event_evidence_provenance_check
            CHECK (provenance IN ('observed', 'legacy_reconstructed')),
          CONSTRAINT news_event_evidence_focus_nonempty CHECK (focus_fact_id <> '')
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_news_event_evidence_content "
        "ON news_event_evidence_snapshots (event_id, evidence_sha256)"
    )
    op.execute(
        "CREATE INDEX ix_news_event_evidence_created ON news_event_evidence_snapshots (created_at_ms DESC, event_id)"
    )

    # Version 0 is a clearly marked reconstruction of the *current* Event.  It
    # must never be confused with what an older verdict actually read.
    op.execute(
        """
        INSERT INTO news_event_evidence_snapshots (
          event_id, evidence_version, focus_fact_id, evidence_sha256,
          provenance, release_eligible, snapshot, created_at_ms
        )
        SELECT
          e.event_id,
          0,
          e.focus_fact_id,
          encode(sha256(convert_to(jsonb_build_object(
            'schema_version', 'news_event_evidence_v1',
            'event_id', e.event_id,
            'focus_fact', jsonb_build_object(
              'fact_id', e.focus_fact_id,
              'text', e.leader_title,
              'context', e.focus_fact_context,
              'method', e.focus_fact_method,
              'span_start', e.focus_span_start,
              'span_end', e.focus_span_end
            ),
            'card', jsonb_build_object(
              'event_id', e.event_id,
              'leader_title', e.leader_title,
              'leader_description', i.description,
              'leader_url', i.canonical_url,
              'reporting_origin', i.reporting_origin,
              'provider_metadata', i.provider_metadata,
              'provenance', i.provenance,
              'leader_published_at_ms', i.published_at_ms,
              'raw_first_line', i.raw_first_line,
              'family', e.family,
              'admission', e.admission,
              'priority', e.priority,
              'provider_score_max', e.provider_score_max,
              'engine_type', e.engine_type,
              'asset_class', e.asset_class,
              'grounded_assets', e.grounded_assets,
              'watchlist_hits', e.watchlist_hits,
              'macro_lexicon', e.macro_lexicon,
              'storyline_key', e.storyline_key,
              'opened_at_ms', e.opened_at_ms,
              'member_count', e.member_count,
              'ingest_mode', e.ingest_mode
            ),
            'members', COALESCE((
              SELECT jsonb_agg(jsonb_build_object(
                'item_id', m.item_id,
                'fact_id', m.fact_id,
                'fact_text', m.fact_text,
                'joined_at_ms', m.joined_at_ms,
                'match_kind', m.match_kind,
                'jaccard_estimate', m.jaccard_estimate
              ) ORDER BY m.joined_at_ms, m.item_id)
              FROM news_event_members m WHERE m.event_id = e.event_id
            ), '[]'::jsonb),
            'provenance', 'legacy_reconstructed'
          )::text, 'UTF8')), 'hex'),
          'legacy_reconstructed',
          false,
          jsonb_build_object(
            'schema_version', 'news_event_evidence_v1',
            'event_id', e.event_id,
            'focus_fact', jsonb_build_object(
              'fact_id', e.focus_fact_id,
              'text', e.leader_title,
              'context', e.focus_fact_context,
              'method', e.focus_fact_method,
              'span_start', e.focus_span_start,
              'span_end', e.focus_span_end
            ),
            'card', jsonb_build_object(
              'event_id', e.event_id,
              'leader_title', e.leader_title,
              'leader_description', i.description,
              'leader_url', i.canonical_url,
              'reporting_origin', i.reporting_origin,
              'provider_metadata', i.provider_metadata,
              'provenance', i.provenance,
              'leader_published_at_ms', i.published_at_ms,
              'raw_first_line', i.raw_first_line,
              'family', e.family,
              'admission', e.admission,
              'priority', e.priority,
              'provider_score_max', e.provider_score_max,
              'engine_type', e.engine_type,
              'asset_class', e.asset_class,
              'grounded_assets', e.grounded_assets,
              'watchlist_hits', e.watchlist_hits,
              'macro_lexicon', e.macro_lexicon,
              'storyline_key', e.storyline_key,
              'opened_at_ms', e.opened_at_ms,
              'member_count', e.member_count,
              'ingest_mode', e.ingest_mode
            ),
            'members', COALESCE((
              SELECT jsonb_agg(jsonb_build_object(
                'item_id', m.item_id,
                'fact_id', m.fact_id,
                'fact_text', m.fact_text,
                'joined_at_ms', m.joined_at_ms,
                'match_kind', m.match_kind,
                'jaccard_estimate', m.jaccard_estimate
              ) ORDER BY m.joined_at_ms, m.item_id)
              FROM news_event_members m WHERE m.event_id = e.event_id
            ), '[]'::jsonb),
            'provenance', 'legacy_reconstructed'
          ),
          e.updated_at_ms
        FROM news_events e
        JOIN news_items i ON i.item_id = e.leader_item_id
        """
    )

    op.execute("ALTER TABLE news_verdicts ADD COLUMN evidence_version integer")
    op.execute("ALTER TABLE news_verdicts ADD COLUMN evidence_sha256 text")
    op.execute("ALTER TABLE news_verdicts ADD COLUMN focus_fact_id text")
    op.execute(
        """
        UPDATE news_verdicts v
           SET evidence_version = s.evidence_version,
               evidence_sha256 = s.evidence_sha256,
               focus_fact_id = s.focus_fact_id
          FROM news_event_evidence_snapshots s
         WHERE s.event_id = v.event_id AND s.evidence_version = 0
        """
    )
    op.execute(
        "ALTER TABLE news_verdicts ADD CONSTRAINT news_verdicts_evidence_pair_check "
        "CHECK ((evidence_version IS NULL) = (evidence_sha256 IS NULL))"
    )

    op.execute(
        """
        CREATE FUNCTION reject_news_event_evidence_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'news_event_evidence_append_only';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_news_event_evidence_append_only "
        "BEFORE UPDATE OR DELETE ON news_event_evidence_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION reject_news_event_evidence_mutation()"
    )

    op.execute("GRANT SELECT ON news_event_evidence_snapshots TO tracefold_serve")
    op.execute("GRANT SELECT, INSERT ON news_event_evidence_snapshots TO tracefold_workers")
    op.execute("REVOKE UPDATE, DELETE ON news_event_evidence_snapshots FROM tracefold_workers")


def downgrade() -> None:
    raise RuntimeError("20260821_0284 is an irreversible evidence-contract hard cut")
