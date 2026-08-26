"""Trading order observations and aggregate status reads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from ..contracts import ACTIVE_ORDER_STATES
from ..research.event_study import summarize_evaluation_rows
from .sql_values import _dumps

# The pipeline stages #211 asks to be able to report, each as the difference between two durable
# timestamps. Named here once so the SELECT list and the returned document cannot drift apart, and
# deliberately keyed by stage rather than by symbol, event or order: this is one bounded document.
#
# Both middle stages are measured from `case_created` rather than chained, because the runner writes
# the order *before* it settles the case — `_place` commits its own transaction and `_settle` is what
# terminalises the case afterwards. `case_decided -> order_prepared` would therefore always be
# negative or zero however the clock is sampled. `order_prepared` is nested inside `case_decided`, and
# the difference between the two is the settle write; the model call is inside both.
_STAGES: Final[tuple[tuple[str, str], ...]] = (
    ("source_observed_to_verdict_persisted", "c.trigger_persisted_at_ms - c.source_observed_at_ms"),
    ("verdict_persisted_to_case_created", "c.created_at_ms - c.trigger_persisted_at_ms"),
    ("case_created_to_order_prepared", "o.created_at_ms - c.created_at_ms"),
    ("case_created_to_case_decided", "c.decided_at_ms - c.created_at_ms"),
    ("order_prepared_to_position_opened", "o.position_opened_at_ms - o.created_at_ms"),
)


class QueryStorage:
    conn: Any

    # ------------------------------------------------------------------ observations
    def record_observation(
        self,
        *,
        order_id: str,
        observation_kind: str,
        content_sha256: str,
        content: Mapping[str, Any],
        now_ms: int,
    ) -> None:
        """An unchanged remote answer bumps a counter; it does not append another row forever."""

        self.conn.execute(
            """
            INSERT INTO trading_order_observations (
              order_id, observation_kind, content_sha256, content, first_seen_at_ms, last_seen_at_ms, seen_count
            ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, 1)
            ON CONFLICT (order_id, observation_kind, content_sha256) DO UPDATE
               SET last_seen_at_ms = EXCLUDED.last_seen_at_ms,
                   seen_count = trading_order_observations.seen_count + 1
            """,
            (order_id, observation_kind, content_sha256, _dumps(dict(content)), int(now_ms), int(now_ms)),
        )

    def observations(self, *, order_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT observation_kind, content_sha256, content, first_seen_at_ms, last_seen_at_ms, seen_count "
            "FROM trading_order_observations WHERE order_id = %s ORDER BY last_seen_at_ms DESC LIMIT %s",
            (order_id, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ status
    def status_counts(self, *, since_ms: int) -> dict[str, Any]:
        cases = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY state",
            (int(since_ms),),
        ).fetchall()
        orders = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_orders WHERE created_at_ms >= %s GROUP BY state",
            (int(since_ms),),
        ).fetchall()
        triggers = self.conn.execute(
            "SELECT trigger_kind, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY trigger_kind",
            (int(since_ms),),
        ).fetchall()
        strategies = self.conn.execute(
            "SELECT strategy_id, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY strategy_id",
            (int(since_ms),),
        ).fetchall()
        shadow_strategies = self.conn.execute(
            "SELECT strategy_id, count(*) AS n FROM trading_strategy_evaluations "
            "WHERE created_at_ms >= %s GROUP BY strategy_id",
            (int(since_ms),),
        ).fetchall()
        shadow_rules = self.conn.execute(
            "SELECT rule, count(*) AS n FROM trading_strategy_evaluations WHERE created_at_ms >= %s GROUP BY rule",
            (int(since_ms),),
        ).fetchall()
        evaluation_rows = self.conn.execute(
            """
            SELECT strategy_id, underlying_key, research_partition, manifest,
                   market_outcome, completed_at_ms
              FROM trading_strategy_evaluations
             WHERE created_at_ms >= %s
            """,
            (int(since_ms),),
        ).fetchall()
        shadow_cohorts, event_study_cohorts = summarize_evaluation_rows([dict(row) for row in evaluation_rows])
        # Only *measured* exits enter the realised-PnL denominator. An operator-resolved order closed
        # a position — so it cools the symbol down — but nobody computed a return for it, and counting
        # it turned one +150 bps winner beside three resolutions into a reported mean of 37.5.
        realized = self.conn.execute(
            "SELECT count(*) AS n, coalesce(sum(realized_bps), 0) AS total_bps "
            "FROM trading_orders WHERE realized_bps IS NOT NULL "
            "AND position_closed_at_ms IS NOT NULL AND position_closed_at_ms >= %s",
            (int(since_ms),),
        ).fetchone()
        return {
            "cases_by_state": {str(row["state"]): int(row["n"]) for row in cases},
            "cases_by_trigger": {str(row["trigger_kind"]): int(row["n"]) for row in triggers},
            "cases_by_strategy": {str(row["strategy_id"]): int(row["n"]) for row in strategies},
            "shadow_by_strategy": {str(row["strategy_id"]): int(row["n"]) for row in shadow_strategies},
            "shadow_by_rule": {str(row["rule"]): int(row["n"]) for row in shadow_rules},
            "shadow_cohorts": shadow_cohorts,
            "event_study_cohorts": event_study_cohorts,
            "liquidation_promotion_ready": False,
            "liquidation_promotion_reason": "source_contract_incomplete",
            "orders_by_state": {str(row["state"]): int(row["n"]) for row in orders},
            "closed_orders": 0 if realized is None else int(realized["n"]),
            "closed_realized_bps": 0 if realized is None else int(realized["total_bps"]),
        }

    def stage_latency_ms(self, *, since_ms: int) -> dict[str, dict[str, int]]:
        """Median and p95 for each pipeline stage over the window, read from the ledger itself.

        A latency report computed from the same rows the audit reads is exactly as replayable as the
        audit — there is no second store to disagree with, and no per-symbol, per-event or per-order
        key anywhere in it. Cases written before the upstream stamps existed simply do not count
        towards the two upstream stages: an ordered-set aggregate skips NULL inputs, so `n` says how
        much evidence each number rests on rather than the number quietly averaging over absence.
        """

        selects = ",\n                   ".join(
            f"count({expression}) AS {name}_n,\n"
            f"                   percentile_disc(0.5) WITHIN GROUP (ORDER BY {expression}) AS {name}_p50,\n"
            f"                   percentile_disc(0.95) WITHIN GROUP (ORDER BY {expression}) AS {name}_p95"
            for name, expression in _STAGES
        )
        row = self.conn.execute(
            f"""
            SELECT {selects}
              FROM trading_cases c
              LEFT JOIN trading_orders o ON o.case_id = c.case_id
             WHERE c.created_at_ms >= %s
            """,
            (int(since_ms),),
        ).fetchone()
        if row is None:  # pragma: no cover - an aggregate query always returns one row
            return {name: {"n": 0} for name, _ in _STAGES}
        report: dict[str, dict[str, int]] = {}
        for name, _ in _STAGES:
            stage: dict[str, int] = {"n": int(row[f"{name}_n"] or 0)}
            for quantile in ("p50", "p95"):
                value = row[f"{name}_{quantile}"]
                if value is not None:
                    stage[quantile] = int(value)
            report[name] = stage
        return report

    def console_orders(
        self,
        *,
        since_ms: int,
        underlying_key: str | None = None,
        states: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Orders with the case that authored them, for a read-only operator surface (#207 PR-W4).

        Deliberately a named projection rather than `SELECT *`. `trading_orders.payload` is the frozen
        provider request body and `trading_cases.manifest` is the frozen decision input; neither belongs in
        a browser, and a `SELECT *` here would put both there the next time a column is added. `account_ref`
        and `remote_order_id` stay behind for the same reason — they name things outside this system and add
        nothing the page renders.

        `state` is returned verbatim. `ACKNOWLEDGED` is the venue answering, not a fill; `OPEN` is the only
        state that has proven both a position and a native stop covering it (#185). A caller that collapses
        them is asserting something the ledger does not.

        **The window is not a creation window.** An order that still holds, or may yet turn out to hold,
        exposure is current no matter when it was written: a `MANUAL_REVIEW_REQUIRED` order waiting two days
        for an operator is exactly the row that must not vanish from 当前暴露, and bounding it by
        `created_at_ms` would have hidden unresolved capital. Active states are therefore unbounded in time —
        the unique-underlying index keeps that set to at most one row per underlying — and everything else is
        bounded by the lifecycle timestamp that makes it recent: `position_closed_at_ms` for a close, and
        `created_at_ms` only for a row that never opened a position and never will.

        That also keeps this list agreeing with `status_counts`, whose realised counts are bounded by
        `position_closed_at_ms`: an order created 30 h ago and closed 2 h ago is in both, or in neither.
        """

        # `PREPARED` … `SAFETY_CLOSING`, verbatim from `ux_trading_active_underlying` (`20260823_0300`).
        recency = "(o.state = ANY(%s) OR coalesce(o.position_closed_at_ms, o.closed_at_ms, o.created_at_ms) >= %s)"
        where = [recency]
        params: list[Any] = [list(ACTIVE_ORDER_STATES), int(since_ms)]
        if underlying_key:
            where.append("o.underlying_key = %s")
            params.append(str(underlying_key))
        if states:
            where.append("o.state = ANY(%s)")
            params.append(list(states))
        params.append(int(limit))
        rows = self.conn.execute(
            f"""
            SELECT o.order_id, o.case_id, o.underlying_key, o.exchange_id, o.provider_symbol,
                   o.mode, o.side, o.notional_usd, o.quantity, o.entry_reference, o.stop_price,
                   o.take_profit_price, o.state, o.state_reason, o.provider_attempt_count,
                   o.exit_attempt_total, o.filled_quantity, o.average_price, o.exit_price,
                   o.exit_reason, o.realized_bps, o.position_opened_at_ms, o.position_closed_at_ms,
                   o.must_close_at_ms, o.created_at_ms, o.updated_at_ms,
                   c.primary_source_key, c.trigger_kind, c.strategy_id, c.strategy_version,
                   c.regime, c.policy_decision, c.policy_reason, c.state AS case_state,
                   c.observed_at_ms AS case_observed_at_ms
              FROM trading_orders o
              JOIN trading_cases c ON c.case_id = o.case_id
             WHERE {" AND ".join(where)}
             ORDER BY o.created_at_ms DESC
             LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def console_case_for_source_key(self, *, primary_source_key: str) -> dict[str, Any] | None:
        """The case one source fact authored, if it authored one, with the order it prepared.

        `primary_source_key` is UNIQUE, so this is an index lookup with at most one row — which is the whole
        invariant restated: one source fact can become at most one case, and one case at most one intent.

        Only callers that can *construct* the key belong here. The deterministic OI lane's key is
        `oi:{event_id}:{metric_version}`, so a News Event on that lane can ask. The model lane's key is a
        content hash of an artifact and a fingerprint (#154), which no `event_id` reconstructs — a caller
        holding only an Event id genuinely cannot ask, and inventing a join by symbol and time would be the
        console asserting a link the ledger does not record.
        """

        row = self.conn.execute(
            """
            SELECT c.case_id, c.underlying_key, c.primary_source_key,
                   c.trigger_kind, c.strategy_id, c.strategy_version,
                   c.mode, c.state, c.regime,
                   c.policy_decision, c.policy_reason, c.observed_at_ms, c.created_at_ms, c.decided_at_ms,
                   o.order_id, o.state AS order_state, o.state_reason AS order_state_reason,
                   o.side, o.notional_usd, o.entry_reference, o.stop_price, o.exit_price,
                   o.exit_reason, o.realized_bps, o.position_opened_at_ms, o.position_closed_at_ms
              FROM trading_cases c
              LEFT JOIN trading_orders o ON o.case_id = c.case_id
             WHERE c.primary_source_key = %s
            """,
            (str(primary_source_key),),
        ).fetchone()
        return dict(row) if row is not None else None

    def console_cases_without_orders(
        self, *, since_ms: int, underlying_key: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Cases that stopped before authoring an intent, and the rule they stopped on.

        The funnel counts these; the page has to be able to name them. A `POLICY_REJECTED` case is the most
        informative row on the surface — it is where the capital lane's floors actually bite — and it has no
        order to join through, so listing orders alone would make the whole rejected population invisible.
        """

        where = ["c.created_at_ms >= %s", "NOT EXISTS (SELECT 1 FROM trading_orders o WHERE o.case_id = c.case_id)"]
        params: list[Any] = [int(since_ms)]
        if underlying_key:
            where.append("c.underlying_key = %s")
            params.append(str(underlying_key))
        params.append(int(limit))
        rows = self.conn.execute(
            f"""
            SELECT c.case_id, c.underlying_key, c.primary_source_key,
                   c.trigger_kind, c.strategy_id, c.strategy_version,
                   c.mode, c.state, c.regime,
                   c.policy_decision, c.policy_reason, c.observed_at_ms, c.created_at_ms, c.decided_at_ms
              FROM trading_cases c
             WHERE {" AND ".join(where)}
             ORDER BY c.created_at_ms DESC
             LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]


__all__ = ["QueryStorage"]
