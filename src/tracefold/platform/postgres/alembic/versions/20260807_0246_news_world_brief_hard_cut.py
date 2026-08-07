"""Hard-cut News facts and Brief state to the public World Brief contract.

Revision ID: 20260807_0246
Revises: 20260807_0245
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op

revision = "20260807_0246"
down_revision = "20260807_0245"
branch_labels = None
depends_on = None

_MAX_HEADLINE_LEN = 500
_MAX_DESCRIPTION_LEN = 400
_MIN_DESCRIPTION_LEN = 40
_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _logical_blocks(value: object) -> tuple[str, ...]:
    """Frozen 0246 copy of the OpenNews plaintext-block adapter."""

    decoded = html.unescape(str(value or "").strip())
    separated = _BREAK_RE.sub("\n", decoded).replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[str] = []
    for raw in separated.split("\n"):
        cleaned = html.unescape(raw)
        cleaned = _TAG_RE.sub(" ", cleaned)
        cleaned = _CONTROL_RE.sub(" ", cleaned)
        cleaned = " ".join(cleaned.split())
        if cleaned:
            blocks.append(cleaned)
    return tuple(blocks)


def _canonical_description(*, explicit: object, remaining_blocks: tuple[str, ...], title: str) -> str:
    explicit_blocks = _logical_blocks(explicit)
    description = " ".join(explicit_blocks or remaining_blocks).strip()
    if len(description) < _MIN_DESCRIPTION_LEN:
        return ""
    if " ".join(description.casefold().split()) == " ".join(title.casefold().split()):
        return ""
    return description[:_MAX_DESCRIPTION_LEN]


def _text(value: object) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        return ""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    return text


def _canonical_reporting_origin(
    *,
    retained_origin: object,
    canonical_url: str | None,
    provider_metadata: object,
) -> str:
    """Frozen 0246 copy of the OpenNews newsType/origin precedence."""

    news_type = _text(retained_origin)
    if news_type.casefold() == "twitter" and isinstance(provider_metadata, Mapping):
        author = _text(provider_metadata.get("source")).lower()
        if author:
            return author
    explicit = news_type.lower()
    if explicit:
        return explicit
    if canonical_url:
        try:
            hostname = str(urlsplit(canonical_url).hostname or "").lower()
        except ValueError:
            hostname = ""
        if hostname:
            return hostname
    return "opennews"


def _content_fingerprint(
    *,
    title: str,
    description: str,
    canonical_url: str | None,
    reporting_origin: str,
    published_at_ms: int,
    language: str,
) -> str:
    payload = {
        "title": title,
        "description": description,
        "canonical_url": canonical_url,
        "reporting_origin": reporting_origin,
        "published_at_ms": published_at_ms,
        "language": language,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _normalize_retained_opennews_facts() -> None:
    """One-time canonicalization without importing mutable runtime code."""

    conn = op.get_bind()
    rows = (
        conn.execute(
            sa.text(
                """
            SELECT item_id, canonical_url, reporting_origin, title, description,
                   lang, published_at_ms, provider_metadata
              FROM news_items
             ORDER BY item_id
            """
            )
        )
        .mappings()
        .all()
    )
    updates: list[dict[str, object]] = []
    rejected_item_count = 0
    for row in rows:
        blocks = _logical_blocks(row["title"])
        title = blocks[0][:_MAX_HEADLINE_LEN].strip() if blocks else ""
        if not title:
            rejected_item_count += 1
            continue
        description = _canonical_description(
            explicit=row["description"],
            remaining_blocks=blocks[1:],
            title=title,
        )
        metadata = row["provider_metadata"]
        canonical_url = str(row["canonical_url"]) if row["canonical_url"] is not None else None
        reporting_origin = _canonical_reporting_origin(
            retained_origin=row["reporting_origin"],
            canonical_url=canonical_url,
            provider_metadata=metadata,
        )
        language = str(row["lang"])
        published_at_ms = int(row["published_at_ms"])
        updates.append(
            {
                "item_id": str(row["item_id"]),
                "title": title,
                "description": description,
                "reporting_origin": reporting_origin,
                "content_fingerprint": _content_fingerprint(
                    title=title,
                    description=description,
                    canonical_url=canonical_url,
                    reporting_origin=reporting_origin,
                    published_at_ms=published_at_ms,
                    language=language,
                ),
            }
        )
    if rejected_item_count:
        raise RuntimeError(f"news_world_brief_hard_cut_unusable_retained_headline:{rejected_item_count}")
    if updates:
        conn.execute(
            sa.text(
                """
                UPDATE news_items
                   SET title = :title,
                       description = :description,
                       reporting_origin = :reporting_origin,
                       content_fingerprint = :content_fingerprint
                 WHERE item_id = :item_id
                """
            ),
            updates,
        )


def upgrade() -> None:
    op.execute(
        """
        DROP TABLE news_story_title_translations;

        DELETE FROM queue_terminal_events
         WHERE owner_key = 'news_brief'
            OR source_table = 'news_brief_runs';

        DROP TABLE news_brief_current;
        DROP TABLE news_brief_selection_current;
        DROP TABLE news_brief_publications;
        DROP TABLE news_brief_runs;

        DELETE FROM news_story_facet_counts;
        DELETE FROM news_source_facet_counts;
        DELETE FROM news_story_members;
        DELETE FROM news_stories;
        """
    )

    _normalize_retained_opennews_facts()

    op.execute(
        r"""
        ALTER TABLE news_items
          DROP COLUMN normalized_title,
          DROP COLUMN brief_excluded;

        UPDATE news_projection_summary
           SET active_item_count = (
                 SELECT count(*) FROM news_items WHERE active
               ),
               active_story_count = 0,
               unmaterialized_item_count = (
                 SELECT count(*) FROM news_items WHERE active
               ),
               invalid_owner_count = 0,
               invalid_story_aggregate_count = 0,
               newest_item_at_ms = (
                 SELECT max(published_at_ms) FROM news_items WHERE active
               ),
               newest_story_at_ms = NULL,
               last_material_change_at_ms = NULL,
               input_fingerprint = NULL,
               projection_version = NULL,
               last_attempt_at_ms = NULL,
               last_error = NULL,
               last_success_at_ms = NULL
         WHERE singleton_key = 'current';

        CREATE TABLE news_brief_selection_current (
          singleton_key boolean PRIMARY KEY DEFAULT true,
          selection_fingerprint text NOT NULL,
          projection_revision text NOT NULL,
          selector_evaluated_at_ms bigint NOT NULL,
          top_stories jsonb NOT NULL,
          selection_stats jsonb NOT NULL,
          selector_version text NOT NULL,
          identity_version text NOT NULL,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT news_brief_selection_current_singleton_check
            CHECK (singleton_key),
          CONSTRAINT news_brief_selection_current_fingerprint_check
            CHECK (selection_fingerprint ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_brief_selection_current_revision_check
            CHECK (btrim(projection_revision) <> ''),
          CONSTRAINT news_brief_selection_current_top_stories_check
            CHECK (
              jsonb_typeof(top_stories) = 'array'
              AND jsonb_array_length(top_stories) <= 8
            ),
          CONSTRAINT news_brief_selection_current_stats_check
            CHECK (jsonb_typeof(selection_stats) = 'object'),
          CONSTRAINT news_brief_selection_current_versions_check
            CHECK (
              btrim(selector_version) <> ''
              AND btrim(identity_version) <> ''
            ),
          CONSTRAINT news_brief_selection_current_clocks_check
            CHECK (
              selector_evaluated_at_ms >= 0
              AND updated_at_ms >= selector_evaluated_at_ms
            )
        );

        CREATE TABLE news_brief_runs (
          run_id text PRIMARY KEY,
          target_fingerprint text NOT NULL UNIQUE,
          selection_fingerprint text NOT NULL,
          status text NOT NULL,
          model_outcome text,
          pointer_action text NOT NULL DEFAULT 'none',
          failure_count integer NOT NULL DEFAULT 0,
          next_due_at_ms bigint,
          lease_owner text,
          lease_token text,
          lease_expires_at_ms bigint,
          last_error_code text,
          last_attempt_at_ms bigint,
          created_at_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL,
          completed_at_ms bigint,
          CONSTRAINT news_brief_runs_id_check
            CHECK (btrim(run_id) <> ''),
          CONSTRAINT news_brief_runs_fingerprints_check
            CHECK (
              target_fingerprint ~ '^[0-9a-f]{64}$'
              AND selection_fingerprint ~ '^[0-9a-f]{64}$'
            ),
          CONSTRAINT news_brief_runs_status_check
            CHECK (status IN ('waiting_input', 'running', 'retry_wait', 'published')),
          CONSTRAINT news_brief_runs_model_outcome_check
            CHECK (model_outcome IS NULL OR model_outcome IN ('ok', 'l2', 'none')),
          CONSTRAINT news_brief_runs_pointer_action_check
            CHECK (
              pointer_action IN (
                'advance_ok', 'advance_degraded', 'preserve_lkg', 'none'
              )
            ),
          CONSTRAINT news_brief_runs_failure_count_check
            CHECK (failure_count BETWEEN 0 AND 100),
          CONSTRAINT news_brief_runs_due_check
            CHECK (
              (status = 'retry_wait' AND next_due_at_ms IS NOT NULL AND next_due_at_ms >= 0)
              OR (status <> 'retry_wait' AND next_due_at_ms IS NULL)
            ),
          CONSTRAINT news_brief_runs_lease_check
            CHECK (
              (
                status = 'running'
                AND NULLIF(btrim(lease_owner), '') IS NOT NULL
                AND NULLIF(btrim(lease_token), '') IS NOT NULL
                AND lease_expires_at_ms IS NOT NULL
                AND lease_expires_at_ms >= 0
              )
              OR (
                status <> 'running'
                AND lease_owner IS NULL
                AND lease_token IS NULL
                AND lease_expires_at_ms IS NULL
              )
            ),
          CONSTRAINT news_brief_runs_error_check
            CHECK (last_error_code IS NULL OR btrim(last_error_code) <> ''),
          CONSTRAINT news_brief_runs_published_check
            CHECK (
              (
                status = 'running'
                AND model_outcome IS NULL
                AND pointer_action = 'none'
                AND completed_at_ms IS NULL
              )
              OR (
                status = 'waiting_input'
                AND model_outcome = 'none'
              )
              OR (
                status = 'retry_wait'
                AND model_outcome IN ('l2', 'none')
              )
              OR (
                status = 'published'
                AND model_outcome = 'ok'
                AND pointer_action = 'advance_ok'
              )
            ),
          CONSTRAINT news_brief_runs_clocks_check
            CHECK (
              created_at_ms >= 0
              AND updated_at_ms >= created_at_ms
              AND (
                last_attempt_at_ms IS NULL
                OR (
                  last_attempt_at_ms >= created_at_ms
                  AND updated_at_ms >= last_attempt_at_ms
                )
              )
              AND (
                completed_at_ms IS NULL
                OR (
                  completed_at_ms >= created_at_ms
                  AND updated_at_ms >= completed_at_ms
                )
              )
            )
        );

        CREATE INDEX ix_news_brief_runs_due
          ON news_brief_runs(next_due_at_ms, updated_at_ms, target_fingerprint)
          WHERE status = 'retry_wait';
        CREATE INDEX ix_news_brief_runs_expired_lease
          ON news_brief_runs(lease_expires_at_ms, target_fingerprint)
          WHERE status = 'running';

        CREATE TABLE news_brief_publications (
          publication_id text PRIMARY KEY,
          selection_fingerprint text NOT NULL,
          target_fingerprint text NOT NULL,
          quality text NOT NULL,
          brief_kind text NOT NULL,
          world_brief text NOT NULL,
          brief_story_lines jsonb NOT NULL,
          top_stories jsonb NOT NULL,
          selected_story_ids jsonb NOT NULL,
          sources jsonb NOT NULL,
          source_age_range jsonb NOT NULL,
          provider text NOT NULL,
          model text NOT NULL,
          prompt_version text NOT NULL,
          workflow_version text NOT NULL,
          composer_version text NOT NULL,
          schema_version text NOT NULL,
          selector_version text NOT NULL,
          identity_version text NOT NULL,
          locale text NOT NULL,
          validation jsonb NOT NULL,
          provenance jsonb NOT NULL,
          published_at_ms bigint NOT NULL,
          created_at_ms bigint NOT NULL,
          CONSTRAINT news_brief_publications_id_check
            CHECK (publication_id ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_brief_publications_fingerprints_check
            CHECK (
              selection_fingerprint ~ '^[0-9a-f]{64}$'
              AND target_fingerprint ~ '^[0-9a-f]{64}$'
            ),
          CONSTRAINT news_brief_publications_quality_check
            CHECK (quality IN ('ok', 'degraded')),
          CONSTRAINT news_brief_publications_kind_check
            CHECK (brief_kind IN ('l1', 'l2', 'none')),
          CONSTRAINT news_brief_publications_json_check
            CHECK (
              jsonb_typeof(brief_story_lines) = 'array'
              AND jsonb_typeof(top_stories) = 'array'
              AND jsonb_array_length(top_stories) BETWEEN 1 AND 8
              AND jsonb_typeof(selected_story_ids) = 'array'
              AND jsonb_array_length(selected_story_ids) = jsonb_array_length(top_stories)
              AND jsonb_typeof(sources) = 'array'
              AND jsonb_typeof(source_age_range) = 'object'
              AND jsonb_typeof(validation) = 'object'
              AND jsonb_typeof(provenance) = 'object'
            ),
          CONSTRAINT news_brief_publications_versions_check
            CHECK (
              btrim(prompt_version) <> ''
              AND btrim(workflow_version) <> ''
              AND btrim(composer_version) <> ''
              AND btrim(schema_version) <> ''
              AND btrim(selector_version) <> ''
              AND btrim(identity_version) <> ''
              AND btrim(locale) <> ''
            ),
          CONSTRAINT news_brief_publications_outcome_check
            CHECK (
              (
                brief_kind = 'l1'
                AND quality = 'ok'
                AND btrim(world_brief) <> ''
                AND btrim(provider) <> ''
                AND btrim(model) <> ''
                AND jsonb_array_length(brief_story_lines) = jsonb_array_length(top_stories)
                AND jsonb_array_length(sources) = jsonb_array_length(top_stories)
              )
              OR (
                brief_kind = 'l2'
                AND quality = 'degraded'
                AND btrim(world_brief) <> ''
                AND btrim(provider) <> ''
                AND btrim(model) <> ''
                AND jsonb_array_length(brief_story_lines) = 0
                AND jsonb_array_length(sources) <= 1
              )
              OR (
                brief_kind = 'none'
                AND quality = 'degraded'
                AND world_brief = ''
                AND provider = ''
                AND model = ''
                AND jsonb_array_length(brief_story_lines) = 0
                AND jsonb_array_length(sources) <= 1
              )
            ),
          CONSTRAINT news_brief_publications_clocks_check
            CHECK (
              created_at_ms >= 0
              AND published_at_ms >= created_at_ms
            )
        );

        CREATE INDEX ix_news_brief_publications_target
          ON news_brief_publications(target_fingerprint, published_at_ms DESC, publication_id);

        CREATE TABLE news_brief_current (
          singleton_key boolean PRIMARY KEY DEFAULT true,
          publication_id text
            REFERENCES news_brief_publications(publication_id) ON DELETE RESTRICT,
          target_fingerprint text,
          latest_run_id text
            REFERENCES news_brief_runs(run_id) ON DELETE SET NULL,
          pending_first_dirty_at_ms bigint,
          pending_due_at_ms bigint,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT news_brief_current_singleton_check
            CHECK (singleton_key),
          CONSTRAINT news_brief_current_target_check
            CHECK (
              target_fingerprint IS NULL
              OR target_fingerprint ~ '^[0-9a-f]{64}$'
            ),
          CONSTRAINT news_brief_current_pending_clock_check
            CHECK (
              (
                pending_first_dirty_at_ms IS NULL
                AND pending_due_at_ms IS NULL
              )
              OR (
                pending_first_dirty_at_ms IS NOT NULL
                AND pending_first_dirty_at_ms >= 0
                AND pending_due_at_ms IS NOT NULL
                AND pending_due_at_ms >= pending_first_dirty_at_ms
              )
            ),
          CONSTRAINT news_brief_current_updated_at_ms_check
            CHECK (updated_at_ms >= 0)
        );

        INSERT INTO news_brief_current(singleton_key, updated_at_ms)
        VALUES (true, 0);

        ALTER TABLE news_brief_selection_current OWNER TO tracefold_owner;
        ALTER TABLE news_brief_runs OWNER TO tracefold_owner;
        ALTER TABLE news_brief_publications OWNER TO tracefold_owner;
        ALTER TABLE news_brief_current OWNER TO tracefold_owner;

        GRANT SELECT ON
          news_brief_selection_current,
          news_brief_runs,
          news_brief_publications,
          news_brief_current
        TO tracefold_serve;

        GRANT SELECT, INSERT, UPDATE, DELETE ON
          news_brief_selection_current,
          news_brief_runs,
          news_brief_current
        TO tracefold_workers;
        GRANT SELECT, INSERT ON news_brief_publications TO tracefold_workers;

        ANALYZE news_items;
        ANALYZE news_projection_summary;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260807_0246 is an irreversible News World Brief hard cut")
