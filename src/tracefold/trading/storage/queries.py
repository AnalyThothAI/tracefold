"""Trading order observations and aggregate status reads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from ..contracts import ACTIVE_ORDER_STATES, utc_day_key
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
    def status_counts(
        self,
        *,
        since_ms: int,
        now_ms: int,
        day_key: str | None,
    ) -> dict[str, Any]:
        try:
            resolved_day_key = str(day_key or "")
            if len(resolved_day_key) != 10:
                raise ValueError
            day_start_ms = int(datetime.fromisoformat(resolved_day_key).replace(tzinfo=UTC).timestamp() * 1000)
        except ValueError:
            resolved_day_key = utc_day_key(now_ms)
            day_start_ms = int(datetime.fromisoformat(resolved_day_key).replace(tzinfo=UTC).timestamp() * 1000)
        day_end_ms = day_start_ms + 86_400_000
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
        # The same question `policy_allowed_today` answers, on the window every other funnel level
        # uses. A console funnel whose middle bar is a UTC day while the bars around it are a rolling
        # 24 h is two intervals impersonating one, and the reader has no way to see the seam (#273).
        policy_allowed_window = self.conn.execute(
            "SELECT count(*) AS n FROM trading_cases WHERE created_at_ms >= %s "
            "AND policy_decision IN ('long', 'short')",
            (int(since_ms),),
        ).fetchone()
        cases_today = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_cases "
            "WHERE created_at_ms >= %s AND created_at_ms < %s GROUP BY state",
            (day_start_ms, day_end_ms),
        ).fetchall()
        policy_allowed_today = self.conn.execute(
            "SELECT count(*) AS n FROM trading_cases WHERE created_at_ms >= %s AND created_at_ms < %s "
            "AND policy_decision IN ('long', 'short')",
            (day_start_ms, day_end_ms),
        ).fetchone()
        closed_today = self.conn.execute(
            "SELECT count(*) AS n FROM trading_orders "
            "WHERE state = 'CLOSED' AND position_closed_at_ms IS NOT NULL "
            "AND position_closed_at_ms >= %s AND position_closed_at_ms < %s",
            (day_start_ms, day_end_ms),
        ).fetchone()
        active = self.conn.execute(
            "SELECT count(*) AS n FROM trading_orders WHERE state = ANY(%s)",
            (list(ACTIVE_ORDER_STATES),),
        ).fetchone()
        return {
            # #264: "nothing today" and "nothing ever" are different operational answers, and every
            # count above returns the same empty document for both.
            **self.latest_lifecycle_milestones(),
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
            "cases_today_by_state": {str(row["state"]): int(row["n"]) for row in cases_today},
            "policy_allowed_today": 0 if policy_allowed_today is None else int(policy_allowed_today["n"]),
            "policy_allowed_24h": 0 if policy_allowed_window is None else int(policy_allowed_window["n"]),
            "closed_orders_today": 0 if closed_today is None else int(closed_today["n"]),
            "active_orders": 0 if active is None else int(active["n"]),
            "funnel_day_key": resolved_day_key,
        }

    def latest_lifecycle_milestones(self) -> dict[str, int | None]:
        """When the lane last reached each stage past admission. Unbounded in time on purpose.

        "Nothing today" and "nothing ever" are different operational answers and a 24 h window returns
        the same empty document for both. These four say which — and read together with the two gate
        milestones they place the break exactly: a lane with a recent source and no recent case has an
        admission problem, one with a recent case and no order has a strategy or a risk problem.
        """

        cases = self.conn.execute("SELECT max(created_at_ms) AS created FROM trading_cases").fetchone()
        orders = self.conn.execute(
            "SELECT max(created_at_ms) AS prepared, max(position_opened_at_ms) AS opened, "
            "max(position_closed_at_ms) AS closed FROM trading_orders"
        ).fetchone()

        def _at(row: Any, key: str) -> int | None:
            value = None if row is None else row[key]
            return None if value is None else int(value)

        return {
            "latest_case_created_at_ms": _at(cases, "created"),
            "latest_order_prepared_at_ms": _at(orders, "prepared"),
            "latest_position_opened_at_ms": _at(orders, "opened"),
            "latest_position_closed_at_ms": _at(orders, "closed"),
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
        closed_from_ms: int | None = None,
        closed_until_ms: int | None = None,
        underlying_key: str | None = None,
        states: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Orders with the case that authored them, for a read-only operator surface (#207 PR-W4).

        Deliberately a named projection rather than `SELECT *`. `trading_orders.payload` is the frozen
        provider request body and does not leave the store at all. `trading_cases.manifest` is the frozen
        decision input and leaves only as the three named slices below — `contexts.market.pre_move_bps`,
        `strategy_config` and `contexts.regime.reason` (#282) — because a case's own frozen thresholds and
        the recorded reason for its quadrant are the only honest way for a console to explain the
        decision; the whole document still does not, and a `SELECT *` here would put
        both documents there the next time a column is added. `account_ref` and `remote_order_id` stay
        behind for the same reason — they name things outside this system and add nothing the page renders.

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

        A supplied closed interval replaces that terminal-row window with one half-open UTC budget day;
        active states remain unbounded. The HTTP workbench uses this projection instead of rebuilding a
        day from row timestamps in the browser.

        That also keeps this list agreeing with `status_counts`, whose realised counts are bounded by
        `position_closed_at_ms`: an order created 30 h ago and closed 2 h ago is in both, or in neither.
        """

        # `PREPARED` … `SAFETY_CLOSING`, verbatim from `ux_trading_active_underlying` (`20260823_0300`).
        if closed_from_ms is not None and closed_until_ms is not None:
            recency = (
                "(o.state = ANY(%s) OR (o.state = 'CLOSED' AND o.position_closed_at_ms >= %s "
                "AND o.position_closed_at_ms < %s))"
            )
            params: list[Any] = [
                list(ACTIVE_ORDER_STATES),
                int(closed_from_ms),
                int(closed_until_ms),
            ]
        else:
            recency = "(o.state = ANY(%s) OR coalesce(o.position_closed_at_ms, o.closed_at_ms, o.created_at_ms) >= %s)"
            params = [list(ACTIVE_ORDER_STATES), int(since_ms)]
        where = [recency]
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
                   c.observed_at_ms AS case_observed_at_ms,
                   -- The same two frozen case facts `console_cases_without_orders` returns, and for the
                   -- same reason (#282). A case that authored an order was losing both here, so the one
                   -- population that got furthest was the one a console could say least about: it could
                   -- name the pre-move for a refused case and not for a filled one, and explain a
                   -- rejection against its own frozen thresholds while explaining a fill against
                   -- whatever is configured today.
                   (c.manifest -> 'contexts' -> 'market' ->> 'pre_move_bps')::int AS pre_move_bps,
                   -- (No per-cent sign in this comment on purpose: psycopg scans comments for
                   -- placeholders, and a bare one splits the multibyte characters after it.)
                   c.manifest -> 'strategy_config' AS strategy_config,
                   -- Why the quadrant came out as it did, frozen at the cutoff. `regime.assess()` reaches
                   -- `unclear` four ways and `policy_reason` is the *strategy's* later answer, not this
                   -- one: the smart-money lane accepts a move between the shared 600 bps ceiling and its
                   -- own 1000, so a traded Case routinely carries `regime='unclear'` beside a
                   -- `policy_reason` that says nothing about the quadrant. Without this column a console
                   -- has to invent a cause or claim the ledger recorded none.
                   (c.manifest -> 'contexts' -> 'regime' ->> 'reason') AS regime_reason
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
                   -- Same columns, same reasons, both projections: `_case()` is shared, so a field
                   -- present on one route and structurally null on the other is a half-truth (#273).
                   (c.manifest -> 'contexts' -> 'market' ->> 'pre_move_bps')::int AS pre_move_bps,
                   c.manifest -> 'strategy_config' AS strategy_config,
                   (c.manifest -> 'contexts' -> 'regime' ->> 'reason') AS regime_reason,
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
                   -- The one frozen number the two price rules are about (#273). Read out of the
                   -- manifest rather than recomputed: a console that rebuilds a pre-move from
                   -- today's candles is describing a different measurement from the one the case
                   -- was decided by. Every writer stores it as a JSON integer, so the cast is total.
                   (c.manifest -> 'contexts' -> 'market' ->> 'pre_move_bps')::int AS pre_move_bps,
                   -- And the thresholds it was decided *against*. Without these a console explains a
                   -- case using whatever configuration is running today, so a 700 bps frame refused
                   -- under the old 1000 bps floor renders as "700 did not reach 500" - an
                   -- impossibility on screen, and the wrong bottleneck named in the headline.
                   -- (No per-cent sign in this comment on purpose: psycopg scans comments for
                   -- placeholders, and a bare one splits the multibyte characters after it.)
                   c.manifest -> 'strategy_config' AS strategy_config,
                   -- And why the quadrant came out as it did, for the same reason `_case()` needs the
                   -- other two: it is shared, so a field on one route and structurally null on the
                   -- other is a half-truth (#273).
                   (c.manifest -> 'contexts' -> 'regime' ->> 'reason') AS regime_reason,
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
