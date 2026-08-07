"""Add exact-bound zh-CN News Story display-title translation state.

Revision ID: 20260807_0245
Revises: 20260806_0244
"""

from __future__ import annotations

from alembic import op

revision = "20260807_0245"
down_revision = "20260806_0244"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE news_story_title_translations (
          story_id text NOT NULL,
          source_title text NOT NULL,
          source_title_fingerprint text NOT NULL,
          source_raw_title_fingerprint text NOT NULL,
          locale text NOT NULL,
          workflow_version text NOT NULL,
          prompt_version text NOT NULL,
          status text NOT NULL,
          result_kind text,
          translated_title text,
          provider text,
          model text,
          attempt_count smallint NOT NULL DEFAULT 0,
          attempts jsonb NOT NULL DEFAULT '[]'::jsonb,
          next_attempt_at_ms bigint,
          lease_owner text,
          lease_token text,
          lease_expires_at_ms bigint,
          last_error text,
          completed_at_ms bigint,
          created_at_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL,
          PRIMARY KEY (
            story_id,
            source_title_fingerprint,
            locale,
            workflow_version,
            prompt_version
          ),
          CONSTRAINT news_story_title_translations_story_id_check
            CHECK (story_id ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_story_title_translations_source_title_check
            CHECK (btrim(source_title) <> ''),
          CONSTRAINT news_story_title_translations_source_fingerprint_check
            CHECK (
              source_title_fingerprint ~ '^[0-9a-f]{64}$'
              AND source_title_fingerprint = encode(
                sha256(convert_to(source_title, 'UTF8')),
                'hex'
              )
            ),
          CONSTRAINT news_story_title_translations_raw_fingerprint_check
            CHECK (source_raw_title_fingerprint ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_story_title_translations_locale_check
            CHECK (locale = 'zh-CN'),
          CONSTRAINT news_story_title_translations_versions_check
            CHECK (
              btrim(workflow_version) <> ''
              AND btrim(prompt_version) <> ''
            ),
          CONSTRAINT news_story_title_translations_status_check
            CHECK (
              status IN (
                'pending', 'running', 'retry_wait',
                'ready', 'failed', 'unavailable'
              )
            ),
          CONSTRAINT news_story_title_translations_result_kind_check
            CHECK (
              result_kind IS NULL
              OR result_kind IN ('translated', 'source_zh')
            ),
          CONSTRAINT news_story_title_translations_attempts_check
            CHECK (
              attempt_count BETWEEN 0 AND 3
              AND jsonb_typeof(attempts) = 'array'
              AND jsonb_array_length(attempts) = attempt_count
              AND (
                status NOT IN ('running', 'retry_wait')
                OR attempt_count >= 1
              )
            ),
          CONSTRAINT news_story_title_translations_next_attempt_check
            CHECK (
              next_attempt_at_ms IS NULL
              OR next_attempt_at_ms >= 0
            ),
          CONSTRAINT news_story_title_translations_lease_check
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
          CONSTRAINT news_story_title_translations_due_check
            CHECK (
              (
                status IN ('pending', 'retry_wait')
                AND next_attempt_at_ms IS NOT NULL
                AND next_attempt_at_ms >= 0
                AND attempt_count < 3
              )
              OR (status NOT IN ('pending', 'retry_wait') AND next_attempt_at_ms IS NULL)
            ),
          CONSTRAINT news_story_title_translations_result_check
            CHECK (
              (
                status = 'ready'
                AND result_kind IS NOT NULL
                AND NULLIF(btrim(translated_title), '') IS NOT NULL
                AND completed_at_ms IS NOT NULL
                AND completed_at_ms >= 0
                AND last_error IS NULL
                AND (
                  (
                    result_kind = 'translated'
                    AND NULLIF(btrim(provider), '') IS NOT NULL
                    AND NULLIF(btrim(model), '') IS NOT NULL
                    AND attempt_count >= 1
                  )
                  OR (
                    result_kind = 'source_zh'
                    AND provider IS NULL
                    AND model IS NULL
                    AND translated_title = source_title
                    AND attempt_count = 0
                  )
                )
              )
              OR (
                status <> 'ready'
                AND result_kind IS NULL
                AND translated_title IS NULL
                AND provider IS NULL
                AND model IS NULL
              )
            ),
          CONSTRAINT news_story_title_translations_terminal_check
            CHECK (
              (
                status IN ('failed', 'unavailable')
                AND NULLIF(btrim(last_error), '') IS NOT NULL
                AND completed_at_ms IS NOT NULL
                AND completed_at_ms >= 0
                AND (status <> 'failed' OR attempt_count = 3)
              )
              OR (
                status NOT IN ('failed', 'unavailable')
                AND completed_at_ms IS NULL
              )
              OR status = 'ready'
            ),
          CONSTRAINT news_story_title_translations_timestamps_check
            CHECK (
              created_at_ms >= 0
              AND updated_at_ms >= created_at_ms
              AND (completed_at_ms IS NULL OR completed_at_ms >= created_at_ms)
            )
        );

        CREATE INDEX ix_news_story_title_translations_current
          ON news_story_title_translations(
            story_id,
            source_raw_title_fingerprint,
            locale,
            workflow_version,
            prompt_version
          );

        CREATE INDEX ix_news_story_title_translations_due
          ON news_story_title_translations(
            next_attempt_at_ms,
            created_at_ms,
            story_id
          )
          WHERE status IN ('pending', 'retry_wait');

        CREATE INDEX ix_news_story_title_translations_expired_lease
          ON news_story_title_translations(lease_expires_at_ms, story_id)
          WHERE status = 'running';

        CREATE INDEX ix_news_story_title_translations_retention
          ON news_story_title_translations(updated_at_ms, story_id);

        ALTER TABLE news_story_title_translations OWNER TO tracefold_owner;
        GRANT SELECT ON news_story_title_translations TO tracefold_serve;
        GRANT SELECT, INSERT, UPDATE, DELETE
          ON news_story_title_translations TO tracefold_workers;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260807_0245 is an irreversible News title-translation migration")
