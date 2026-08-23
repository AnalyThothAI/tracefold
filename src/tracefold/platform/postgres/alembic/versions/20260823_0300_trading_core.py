"""The `tracefold.trading` bounded context: blacklist, cases, orders, observations, control (#104).

Five tables, one bounded context. News keeps every fact it owns; nothing here writes a News table and
nothing in News reads one of these. The composition root is the only place the two meet.

Three invariants live in the schema rather than in a runner, because a runner's in-memory check is not
an authority across restarts, leases or two venues:

* `trading_cases.primary_source_key` is UNIQUE — one source fact produces one case, however many times
  the scanner re-reads its window.
* `trading_orders.case_id` is UNIQUE — one case authors at most one economic intent.
* a partial UNIQUE index on `trading_orders (underlying_key)` holds for every state that can carry, or
  can still turn out to carry, exposure. The predicate deliberately includes `APPROVED`,
  `RECONCILING`, `MANUAL_REVIEW_REQUIRED` and `UNPROTECTED`: an order whose provider write is ambiguous
  is *more* dangerous than one that is merely open, and "we do not know whether Binance filled it" must
  block a Hyperliquid entry for the same underlying until a human or a read resolves it. Leaving those
  states out would have let the exact race the invariant exists to prevent through the moment
  reconciliation started.

`provider_attempt_count` is CHECKed at one. OpenTrade publishes no client idempotency key, so the only
protection against a double order is that the row cannot record a second attempt.

Money is NUMERIC. Percentages that are thresholds are integer basis points, like `news_event_reactions`
and `news_oi_signals`, so a stored number and the comparison against it cannot disagree by a float.

Revision ID: 20260823_0300
Revises: 20260823_0299
"""

from __future__ import annotations

from alembic import op

revision = "20260823_0300"
down_revision = "20260823_0299"
branch_labels = None
depends_on = None

# Every state that holds, or may yet turn out to hold, exposure for one underlying. `PREPARED` is in
# because the payload is frozen and about to be written; `REJECTED`, `REJECTED_BY_OPERATOR`, `NO_FILL`
# and `CLOSED` are out because they are proven terminal with nothing outstanding.
_ACTIVE_ORDER_STATES = (
    "PREPARED",
    "AWAITING_APPROVAL",
    "APPROVED",
    "SUBMITTING",
    "AMBIGUOUS",
    "RECONCILING",
    "MANUAL_REVIEW_REQUIRED",
    "ACKNOWLEDGED",
    "PARTIAL",
    "OPEN",
    "UNPROTECTED",
    "SAFETY_CLOSING",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trading_symbol_blacklist (
          base_symbol   TEXT   PRIMARY KEY,
          reason        TEXT   NOT NULL,
          expires_at_ms BIGINT,
          created_at_ms BIGINT NOT NULL,
          updated_at_ms BIGINT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE trading_runtime_state (
          id            SMALLINT PRIMARY KEY,
          control       TEXT     NOT NULL,
          day_key       TEXT     NOT NULL,
          orders_today  INTEGER  NOT NULL DEFAULT 0,
          dspy_calls_today INTEGER NOT NULL DEFAULT 0,
          -- The scanner keeps no cursor: it re-reads a bounded overlap window every turn and lets
          -- `trading_cases.primary_source_key` reject what it has already seen, which is what makes a
          -- crash or a redeploy idempotent without a checkpoint to corrupt. What does need to survive
          -- a restart is the funnel, because "OI raw -> ... -> paper order" is the PR-B deliverable and
          -- an in-memory counter would reset with every deploy. One row, one day, bounded keys.
          funnel        JSONB    NOT NULL DEFAULT '{}'::jsonb,
          updated_at_ms BIGINT   NOT NULL,
          CONSTRAINT trading_runtime_state_singleton CHECK (id = 1),
          CONSTRAINT trading_runtime_state_control_check
            CHECK (control IN ('RUNNING', 'CLOSE_ONLY', 'PAUSED'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE trading_cases (
          case_id                  TEXT    PRIMARY KEY,
          underlying_key           TEXT    NOT NULL,
          case_kind                TEXT    NOT NULL,
          mode                     TEXT    NOT NULL,
          primary_source_key       TEXT    NOT NULL,
          supplemental_source_keys JSONB   NOT NULL DEFAULT '[]'::jsonb,
          manifest                 JSONB   NOT NULL,
          manifest_sha256          TEXT    NOT NULL,
          state                    TEXT    NOT NULL,
          run_id                   TEXT,
          lease_expires_at_ms      BIGINT,
          attempt_count            INTEGER NOT NULL DEFAULT 0,
          regime                   TEXT,
          program_version          TEXT,
          program_sha256           TEXT,
          program_output           JSONB,
          policy_decision          TEXT,
          policy_reason            TEXT,
          observed_at_ms           BIGINT  NOT NULL,
          created_at_ms            BIGINT  NOT NULL,
          decided_at_ms            BIGINT,
          updated_at_ms            BIGINT  NOT NULL,
          CONSTRAINT trading_cases_primary_source_key_unique UNIQUE (primary_source_key),
          CONSTRAINT trading_cases_kind_check
            CHECK (case_kind IN ('oi_only', 'news_only', 'news_oi')),
          CONSTRAINT trading_cases_mode_check
            CHECK (mode IN ('paper', 'live_reviewed', 'live_bounded')),
          CONSTRAINT trading_cases_state_check
            CHECK (state IN ('PENDING', 'RUNNING', 'NO_TRADE', 'POLICY_REJECTED', 'ORDER_PREPARED', 'BLOCKED'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE trading_orders (
          order_id               TEXT    PRIMARY KEY,
          case_id                TEXT    NOT NULL,
          underlying_key         TEXT    NOT NULL,
          exchange_id            TEXT    NOT NULL,
          provider_symbol        TEXT    NOT NULL,
          account_ref            TEXT    NOT NULL,
          mode                   TEXT    NOT NULL,
          side                   TEXT    NOT NULL,
          notional_usd           NUMERIC NOT NULL,
          quantity               NUMERIC NOT NULL,
          entry_reference        NUMERIC NOT NULL,
          stop_price             NUMERIC NOT NULL,
          take_profit_price      NUMERIC,
          payload                JSONB   NOT NULL,
          payload_sha256         TEXT    NOT NULL,
          state                  TEXT    NOT NULL,
          state_reason           TEXT,
          provider_attempt_count INTEGER NOT NULL DEFAULT 0,
          exit_attempt_count     INTEGER NOT NULL DEFAULT 0,
          remote_order_id        TEXT,
          filled_quantity        NUMERIC,
          average_price          NUMERIC,
          exit_price             NUMERIC,
          exit_reason            TEXT,
          realized_bps           INTEGER,
          position_opened_at_ms  BIGINT,
          position_closed_at_ms  BIGINT,
          must_close_at_ms       BIGINT,
          next_reconcile_at_ms   BIGINT,
          closed_at_ms           BIGINT,
          created_at_ms          BIGINT  NOT NULL,
          updated_at_ms          BIGINT  NOT NULL,
          CONSTRAINT trading_orders_case_unique UNIQUE (case_id),
          CONSTRAINT trading_orders_case_fk FOREIGN KEY (case_id)
            REFERENCES trading_cases (case_id) ON DELETE CASCADE,
          CONSTRAINT trading_orders_exchange_check
            CHECK (exchange_id IN ('binance', 'hyperliquid', 'paper')),
          CONSTRAINT trading_orders_side_check CHECK (side IN ('buy', 'sell')),
          CONSTRAINT trading_orders_mode_check
            CHECK (mode IN ('paper', 'live_reviewed', 'live_bounded')),
          -- The one protection against a double write: OpenTrade has no client idempotency key, so the
          -- ledger, not the provider, is what makes a second attempt impossible. The exit needs its own
          -- counter rather than sharing the entry's — the entry has already spent that one by the time
          -- a position can be closed, so a shared counter would leave the exit with no protection at all.
          CONSTRAINT trading_orders_one_attempt CHECK (provider_attempt_count <= 1),
          CONSTRAINT trading_orders_one_exit_attempt CHECK (exit_attempt_count <= 1),
          CONSTRAINT trading_orders_quantity_positive CHECK (quantity > 0),
          CONSTRAINT trading_orders_notional_positive CHECK (notional_usd > 0)
        )
        """
    )
    states = ", ".join(f"'{state}'" for state in _ACTIVE_ORDER_STATES)
    op.execute(
        f"CREATE UNIQUE INDEX ux_trading_active_underlying ON trading_orders (underlying_key) WHERE state IN ({states})"
    )
    op.execute("CREATE INDEX ix_trading_orders_due ON trading_orders (state, next_reconcile_at_ms)")
    op.execute("CREATE INDEX ix_trading_cases_state ON trading_cases (state, created_at_ms)")
    op.execute("CREATE INDEX ix_trading_cases_underlying ON trading_cases (underlying_key, created_at_ms DESC)")
    op.execute(
        """
        CREATE TABLE trading_order_observations (
          order_id         TEXT    NOT NULL,
          observation_kind TEXT    NOT NULL,
          content_sha256   TEXT    NOT NULL,
          content          JSONB   NOT NULL,
          first_seen_at_ms BIGINT  NOT NULL,
          last_seen_at_ms  BIGINT  NOT NULL,
          seen_count       INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY (order_id, observation_kind, content_sha256),
          CONSTRAINT trading_order_observations_order_fk FOREIGN KEY (order_id)
            REFERENCES trading_orders (order_id) ON DELETE CASCADE
        )
        """
    )

    # `tracefold_serve` is the HTTP-facing role and it runs `default_transaction_read_only = on`. It
    # gets SELECT and nothing else on every table here. The deny-list is a *safety control*: the role
    # reachable from the internet must not be able to rewrite or erase it, and the one precedent for a
    # serve write (`news_reviews`) is append-only INSERT for exactly that reason. Operator mutations —
    # deny-list, control state, order approval — run as `tracefold_workers` from the CLI instead.
    op.execute(
        "GRANT SELECT ON trading_symbol_blacklist, trading_runtime_state, trading_cases, "
        "trading_orders, trading_order_observations TO tracefold_serve"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON trading_symbol_blacklist, trading_runtime_state, "
        "trading_cases, trading_orders, trading_order_observations TO tracefold_workers"
    )
    op.execute("GRANT DELETE ON trading_symbol_blacklist TO tracefold_workers")

    op.execute(
        """
        INSERT INTO trading_runtime_state (id, control, day_key, orders_today, dspy_calls_today, funnel, updated_at_ms)
        VALUES (1, 'RUNNING', '', 0, 0, '{}'::jsonb, (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT)
        """
    )
    # #104 seeds the deny-list with the two benchmarks the policy is not trying to trade and the one
    # commodity ticker the Gate has historically grounded as if it were a token. `CL` is kept even
    # though the crypto-class check already rejects it: an explicit operator row is the regression
    # fixture that proves canonicalisation strips the `XYZ-` prefix before the lookup.
    op.execute(
        """
        INSERT INTO trading_symbol_blacklist (base_symbol, reason, expires_at_ms, created_at_ms, updated_at_ms)
        SELECT symbol, reason, NULL,
               (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT,
               (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT
          FROM (VALUES
                  ('BTC', 'benchmark_large_cap'),
                  ('ETH', 'benchmark_large_cap'),
                  ('CL',  'commodity_not_target')
               ) AS seed(symbol, reason)
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260823_0300 owns capital state; dropping it would orphan an open position")
