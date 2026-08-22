"""Rank ledger for open-interest telemetry (#137).

`news_oi_signals` is a derived read model, not a fact and not a decision: `news_items` stays the
material truth and `news_verdicts` remains the one place a decision is recorded. Every row here is
reproducible by re-parsing the Item that produced it, and its only reader is the rank rule — "how
many frames has this symbol already emitted inside the window".

Percentages are integer basis points for the same reason `news_event_reactions` stores them that way:
a stored number and a threshold comparison should not disagree because of a float.

`telemetry_deterministic` joins the admitted set, so the outbox rescue's partial index widens with it
exactly as `20260820_0279` did for `listing_deterministic`.

Revision ID: 20260822_0297
Revises: 20260822_0296
"""

from __future__ import annotations

from alembic import op

revision = "20260822_0297"
down_revision = "20260822_0296"
branch_labels = None
depends_on = None

_INDEX = "ix_news_events_unpublished"
_ADMITTED = (
    "(admission = 'candidate'::text "
    "OR admission = 'listing_deterministic'::text "
    "OR admission = 'telemetry_deterministic'::text)"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE news_oi_signals (
          event_id              TEXT    NOT NULL,
          metric_version        TEXT    NOT NULL,
          symbol                TEXT    NOT NULL,
          direction             TEXT    NOT NULL,
          oi_change_bps         BIGINT  NOT NULL,
          oi_value_usd          BIGINT  NOT NULL,
          whale_long_profit_bps BIGINT  NOT NULL,
          whale_oi_ratio_bps    BIGINT  NOT NULL,
          observed_at_ms        BIGINT  NOT NULL,
          rank_in_window        INTEGER NOT NULL,
          created_at_ms         BIGINT  NOT NULL,
          PRIMARY KEY (event_id, metric_version),
          -- `news_event_reactions` (0283) declares the same cascade, and `purge_before` relies on the
          -- chain: without it these rows outlive the 30-day purge of the Items they are derived from.
          FOREIGN KEY (event_id) REFERENCES news_events(event_id) ON DELETE CASCADE,
          CONSTRAINT news_oi_signals_direction_check CHECK (direction IN ('rise', 'fall'))
        )
        """
    )
    # The rank read runs on every telemetry frame: this symbol, inside the window, newest first.
    op.execute(
        "CREATE INDEX ix_news_oi_signals_symbol_observed "
        "ON news_oi_signals (metric_version, symbol, observed_at_ms DESC)"
    )
    op.execute("GRANT SELECT ON news_oi_signals TO tracefold_serve")
    op.execute("GRANT SELECT, INSERT ON news_oi_signals TO tracefold_workers")

    # The outbox rescue scans admitted Events; a new admission has to be in the index predicate or the
    # Janitor's 60 s scan falls back to a sequential scan over a table holding 365 days of judged rows.
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute(
        f"CREATE INDEX {_INDEX} ON public.news_events USING btree (opened_at_ms) "
        f"WHERE (published_at_ms IS NULL AND {_ADMITTED})"
    )

    # ReviewDesk and the learning plane are about model judgments.
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
               reaction.max_abs_return_1h_bps,
               v.program_version,
               v.program_sha256
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
         -- #137: deterministic telemetry judgments are arithmetic, not model output. They must not
         -- become ReviewDesk tasks or enter the learning denominators: a reviewer rating one teaches
         -- the optimizer nothing, and counting them would dilute every accepted-share figure.
         WHERE v.program_version IS DISTINCT FROM 'news_oi_signal_v1'
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260822_0297 is an irreversible append-only read model")
