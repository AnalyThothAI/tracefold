"""One durable answer per OI source for "why is there no case" (#264).

`trading_cases` is a complete frozen manifest plus a strategy decision. Most of the reasons an OI frame
never becomes a trade happen *before* a manifest can be frozen — no instrument, no candle, below the
liquidity floor, rank exhausted, an underlying already carrying exposure — so recording them there
would turn the case ledger into a bin of incomplete candidates. `trading_strategy_evaluations` is no
better: it describes what a strategy said about an already-frozen manifest, which is a different
question from whether the source was admitted at all.

Before this table the only record was `trading_runtime_state.funnel`, one JSONB document reset on the
UTC day key. That is why "BTW printed 85M and 91M yesterday and had a native perp — where did it go?"
had no answer: the counters that could have said were overwritten at midnight.

**One row per (source, gate version, gate config digest), not an event log.** A scanner that re-reads
its 65-minute overlap window every two seconds must not append. Re-evaluation bumps
`last_evaluated_at_ms` and `attempt_count` and nothing else; `status` moves only out of `DEFERRED`,
which makes the three terminal states final without a trigger to enforce it. Including the config
digest in the key is what stops a threshold edit from silently reusing a decision taken under the old
one — a new digest is a new row, and the old row stays as the record of what the old rule decided.

Volume is small and known: the OI lane persisted 405 facts in the seven days `news_oi_signals` has
existed, about 90 a day, so a 90-day retention is a few thousand rows. It is bounded by retention
rather than by a partial index because an operator asking "why was there no case last Tuesday" is
exactly the question the table exists to answer.

Revision ID: 20260826_0312
Revises: 20260826_0311
"""

from __future__ import annotations

from alembic import op

revision = "20260826_0312"
down_revision = "20260826_0311"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trading_candidate_gate_decisions (
          source_key            TEXT     NOT NULL,
          gate_version          TEXT     NOT NULL,
          gate_config_digest    CHAR(64) NOT NULL,
          trigger_kind          TEXT     NOT NULL,
          -- NULL only when the source failed before a symbol could be canonicalised at all, which is
          -- the one stage where there is no underlying to name.
          underlying_key        TEXT,
          source_observed_at_ms BIGINT   NOT NULL,
          status                TEXT     NOT NULL,
          stage                 TEXT     NOT NULL,
          reason                TEXT     NOT NULL,
          -- Whether a later scan could reach a different answer. `market_data_unavailable` can;
          -- `no_native_perp` cannot until the catalogue changes, and `oi_value_below_floor` never can
          -- for this frame, because the number it failed on is frozen in the frame itself.
          retryable             BOOLEAN  NOT NULL,
          -- The measurements the decision was taken on, so a threshold argument can be settled from
          -- the row rather than by re-deriving it from three other tables.
          evidence              JSONB    NOT NULL DEFAULT '{}'::jsonb,
          case_id               TEXT     REFERENCES trading_cases(case_id),
          first_evaluated_at_ms BIGINT   NOT NULL,
          last_evaluated_at_ms  BIGINT   NOT NULL,
          attempt_count         INTEGER  NOT NULL DEFAULT 1,
          PRIMARY KEY (source_key, gate_version, gate_config_digest),
          CONSTRAINT trading_candidate_gate_status_check
            CHECK (status IN ('DEFERRED', 'REJECTED', 'CASE_CREATED', 'EXPIRED')),
          CONSTRAINT trading_candidate_gate_stage_check
            CHECK (stage IN ('source', 'eligibility', 'routing', 'market_context', 'freeze')),
          CONSTRAINT trading_candidate_gate_kind_check
            CHECK (trigger_kind IN ('oi', 'news', 'liquidation')),
          -- The link is the whole point of `CASE_CREATED`, and a `case_id` on any other status would
          -- be a claim the ledger cannot support.
          CONSTRAINT trading_candidate_gate_case_link_check
            CHECK ((status = 'CASE_CREATED') = (case_id IS NOT NULL)),
          CONSTRAINT trading_candidate_gate_attempts_check CHECK (attempt_count >= 1),
          CONSTRAINT trading_candidate_gate_clock_check CHECK (last_evaluated_at_ms >= first_evaluated_at_ms)
        )
        """
    )
    # The read model's own axis: "every OI fact in the last 24 h / 7 d, and what happened to it". Keyed
    # on when the *frame* was observed rather than when the gate looked, so a runner restart that
    # re-evaluates a backlog cannot move a fact into a different day.
    op.execute(
        "CREATE INDEX ix_trading_candidate_gate_observed "
        "ON trading_candidate_gate_decisions (source_observed_at_ms DESC)"
    )
    # The expiry sweep, and only the expiry sweep. Partial on the one non-terminal status, so it stays
    # the size of the open set rather than the size of the history.
    op.execute(
        "CREATE INDEX ix_trading_candidate_gate_open "
        "ON trading_candidate_gate_decisions (source_observed_at_ms) "
        "WHERE status = 'DEFERRED'"
    )
    op.execute("GRANT SELECT ON trading_candidate_gate_decisions TO tracefold_serve")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON trading_candidate_gate_decisions TO tracefold_workers")


def downgrade() -> None:
    raise RuntimeError("20260826_0312 is an irreversible candidate-admission ledger")
