"""Admit the local-only v12 Program under existing judgment constraints (#668).

Based on 0384; historical rows, webpages and executions are untouched. Stop writers
under the migration gate. ACCESS EXCLUSIVE on news_verdicts, lock_timeout=5s,
statement_timeout=600s; validate the existing ledger without rewriting rows.
Build a GiST trigram index on news_events (SHARE lock; no row rewrite); the
25,001-row query audit showed similarity scanning 6,401 recent Events to return
64 candidates without it. Larger production build duration is unmeasured.
Preflight requires the v11 guard; failure rolls back atomically. Roll forward or
restore a verified backup. PostgreSQL 18 is the test target; no deployment implied.
"""

from alembic import op

revision = "20260920_0385"
down_revision = "20260919_0384"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '600s'")
    op.execute("CREATE INDEX ix_news_events_evidence_title ON news_events USING gist (comparison_title gist_trgm_ops)")
    op.execute("""
        DO $migration$
        DECLARE definition text;
        BEGIN
          SELECT pg_get_constraintdef(oid) INTO STRICT definition FROM pg_constraint
            WHERE conrelid='news_verdicts'::regclass AND conname='news_verdicts_current_judgment_check';
          IF position('news_semantic_program_v11' in definition) = 0 THEN
            RAISE EXCEPTION 'unexpected_news_judgment_constraint';
          END IF;
          definition := replace(definition,
            '''news_semantic_program_v11''::text]))',
            '''news_semantic_program_v11''::text, ''news_semantic_program_v12''::text]))');
          definition := replace(definition,
            '''news_semantic_program_v11''::text)',
            '''news_semantic_program_v11''::text AND program_version <> ''news_semantic_program_v12''::text)');
          ALTER TABLE news_verdicts DROP CONSTRAINT news_verdicts_current_judgment_check;
          EXECUTE 'ALTER TABLE news_verdicts ADD CONSTRAINT news_verdicts_current_judgment_check ' || definition;
        END $migration$;
    """)


def downgrade() -> None:
    raise RuntimeError("news_local_evidence_forward_only: restore a verified backup")
