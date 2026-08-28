"""Case -> Intent -> Outcome projections for CLI, HTTP, and the operator console."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from ..intent import ACTIVE_INTENT_STATES
from ..research.event_study import summarize_evaluation_rows

_STAGES: Final[tuple[tuple[str, str], ...]] = (
    ("source_observed_to_verdict_persisted", "c.trigger_persisted_at_ms - c.source_observed_at_ms"),
    ("verdict_persisted_to_case_created", "c.created_at_ms - c.trigger_persisted_at_ms"),
    ("case_created_to_case_decided", "c.decided_at_ms - c.created_at_ms"),
    ("case_created_to_intent_emitted", "i.created_at_ms - c.created_at_ms"),
    ("intent_emitted_to_entry_fenced", "i.entry_fenced_at_ms - i.created_at_ms"),
    ("entry_fenced_to_position_opened", "i.opened_at_ms - i.entry_fenced_at_ms"),
    ("position_opened_to_closed_flat", "i.flat_verified_at_ms - i.opened_at_ms"),
)


class QueryStorage:
    conn: Any

    def status_counts(
        self,
        *,
        since_ms: int,
        now_ms: int,
        day_key: str | None,
    ) -> dict[str, Any]:
        day_start_ms, resolved_day_key = _day_start(day_key, now_ms=now_ms)
        day_end_ms = day_start_ms + 86_400_000
        cases = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY state",
            (int(since_ms),),
        ).fetchall()
        intents = self.conn.execute(
            "SELECT execution_state, count(*) AS n FROM trading_intents "
            "WHERE created_at_ms >= %s GROUP BY execution_state",
            (int(since_ms),),
        ).fetchall()
        outcomes = self.conn.execute(
            "SELECT terminal_outcome, count(*) AS n FROM trading_intents "
            "WHERE terminal_outcome IS NOT NULL AND updated_at_ms >= %s GROUP BY terminal_outcome",
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
        policy_allowed_window = self.conn.execute(
            "SELECT count(*) AS n FROM trading_cases WHERE created_at_ms >= %s AND policy_decision = 'long'",
            (int(since_ms),),
        ).fetchone()
        cases_today = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_cases "
            "WHERE created_at_ms >= %s AND created_at_ms < %s GROUP BY state",
            (day_start_ms, day_end_ms),
        ).fetchall()
        policy_allowed_today = self.conn.execute(
            "SELECT count(*) AS n FROM trading_cases "
            "WHERE created_at_ms >= %s AND created_at_ms < %s AND policy_decision = 'long'",
            (day_start_ms, day_end_ms),
        ).fetchone()
        entries_today = self.conn.execute(
            "SELECT count(*) AS n FROM trading_intents WHERE entry_fenced_at_ms >= %s AND entry_fenced_at_ms < %s",
            (day_start_ms, day_end_ms),
        ).fetchone()
        closed_today = self.conn.execute(
            "SELECT count(*) AS n FROM trading_intents "
            "WHERE terminal_outcome = 'CLOSED_FLAT' AND closed_at_ms >= %s AND closed_at_ms < %s",
            (day_start_ms, day_end_ms),
        ).fetchone()
        active = self.conn.execute(
            "SELECT count(*) AS n FROM trading_intents WHERE execution_state = ANY(%s)",
            (list(ACTIVE_INTENT_STATES),),
        ).fetchone()
        return {
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
            "intents_by_state": {str(row["execution_state"]): int(row["n"]) for row in intents},
            "outcomes_by_state": {str(row["terminal_outcome"]): int(row["n"]) for row in outcomes},
            "cases_today_by_state": {str(row["state"]): int(row["n"]) for row in cases_today},
            "policy_allowed_today": _count(policy_allowed_today),
            "policy_allowed_24h": _count(policy_allowed_window),
            "entries_today": _count(entries_today),
            "closed_intents_today": _count(closed_today),
            "active_intents": _count(active),
            "funnel_day_key": resolved_day_key,
        }

    def latest_lifecycle_milestones(self) -> dict[str, int | None]:
        cases = self.conn.execute("SELECT max(created_at_ms) AS created FROM trading_cases").fetchone()
        intents = self.conn.execute(
            "SELECT max(created_at_ms) AS emitted, max(entry_fenced_at_ms) AS fenced, "
            "max(opened_at_ms) AS opened, max(closed_at_ms) AS closed FROM trading_intents"
        ).fetchone()
        return {
            "latest_case_created_at_ms": _at(cases, "created"),
            "latest_intent_emitted_at_ms": _at(intents, "emitted"),
            "latest_entry_fenced_at_ms": _at(intents, "fenced"),
            "latest_position_opened_at_ms": _at(intents, "opened"),
            "latest_position_closed_at_ms": _at(intents, "closed"),
        }

    def stage_latency_ms(self, *, since_ms: int) -> dict[str, dict[str, int]]:
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
              LEFT JOIN trading_intents i ON i.case_id = c.case_id
             WHERE c.created_at_ms >= %s
            """,
            (int(since_ms),),
        ).fetchone()
        if row is None:  # pragma: no cover - aggregate queries always return one row
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

    def console_intents(
        self,
        *,
        since_ms: int,
        closed_from_ms: int | None = None,
        closed_until_ms: int | None = None,
        underlying_key: str | None = None,
        states: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if closed_from_ms is not None and closed_until_ms is not None:
            recency = (
                "(i.execution_state = ANY(%s) OR "
                "(i.execution_state = 'TERMINAL' AND i.closed_at_ms >= %s AND i.closed_at_ms < %s))"
            )
            params: list[Any] = [list(ACTIVE_INTENT_STATES), int(closed_from_ms), int(closed_until_ms)]
        else:
            recency = (
                "(i.execution_state = ANY(%s) OR "
                "coalesce(i.closed_at_ms, i.flat_verified_at_ms, i.updated_at_ms, i.created_at_ms) >= %s)"
            )
            params = [list(ACTIVE_INTENT_STATES), int(since_ms)]
        where = [recency]
        if underlying_key:
            where.append("c.underlying_key = %s")
            params.append(underlying_key)
        if states:
            where.append("i.execution_state = ANY(%s)")
            params.append(list(states))
        params.append(int(limit))
        rows = self.conn.execute(
            f"""
            SELECT i.*, c.underlying_key, c.primary_source_key, c.trigger_kind,
                   c.strategy_id, c.strategy_version, c.regime,
                   c.policy_decision, c.policy_reason, c.state AS case_state,
                   c.observed_at_ms AS case_observed_at_ms,
                   (c.manifest -> 'contexts' -> 'market' ->> 'pre_move_bps')::int AS pre_move_bps,
                   c.manifest -> 'strategy_config' AS strategy_config,
                   (c.manifest -> 'contexts' -> 'regime' ->> 'reason') AS regime_reason
              FROM trading_intents i
              JOIN trading_cases c ON c.case_id = i.case_id
             WHERE {" AND ".join(where)}
             ORDER BY i.created_at_ms DESC
             LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def console_case_for_source_key(self, *, primary_source_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT c.case_id, c.underlying_key, c.primary_source_key,
                   c.trigger_kind, c.strategy_id, c.strategy_version,
                   c.mode, c.state, c.regime,
                   (c.manifest -> 'contexts' -> 'market' ->> 'pre_move_bps')::int AS pre_move_bps,
                   c.manifest -> 'strategy_config' AS strategy_config,
                   (c.manifest -> 'contexts' -> 'regime' ->> 'reason') AS regime_reason,
                   c.policy_decision, c.policy_reason, c.observed_at_ms,
                   c.created_at_ms AS case_created_at_ms, c.decided_at_ms,
                   i.intent_id, i.execution_environment, i.instrument_id, i.side,
                   i.target_notional_usd, i.reference_price, i.valid_until_ms,
                   i.execution_state,
                   i.execution_phase, i.terminal_outcome, i.reason_code,
                   i.entry_fenced_at_ms, i.actual_quantity, i.protected_quantity,
                   i.avg_entry_price, i.avg_exit_price, i.stop_price,
                   i.opened_at_ms, i.protected_at_ms, i.closed_at_ms,
                   i.flat_verified_at_ms, i.realized_pnl_amount, i.realized_pnl_currency,
                   i.commissions_by_currency, i.created_at_ms, i.updated_at_ms,
                   c.state AS case_state, c.observed_at_ms AS case_observed_at_ms
              FROM trading_cases c
              LEFT JOIN trading_intents i ON i.case_id = c.case_id
             WHERE c.primary_source_key = %s
            """,
            (primary_source_key,),
        ).fetchone()
        return dict(row) if row is not None else None

    def console_cases_without_intents(
        self, *, since_ms: int, underlying_key: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        where = [
            "c.created_at_ms >= %s",
            "NOT EXISTS (SELECT 1 FROM trading_intents i WHERE i.case_id = c.case_id)",
        ]
        params: list[Any] = [int(since_ms)]
        if underlying_key:
            where.append("c.underlying_key = %s")
            params.append(underlying_key)
        params.append(int(limit))
        rows = self.conn.execute(
            f"""
            SELECT c.case_id, c.underlying_key, c.primary_source_key,
                   c.trigger_kind, c.strategy_id, c.strategy_version,
                   c.mode, c.state, c.regime,
                   (c.manifest -> 'contexts' -> 'market' ->> 'pre_move_bps')::int AS pre_move_bps,
                   c.manifest -> 'strategy_config' AS strategy_config,
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


def _day_start(day_key: str | None, *, now_ms: int) -> tuple[int, str]:
    try:
        resolved = str(day_key or "")
        if len(resolved) != 10:
            raise ValueError
        start = int(datetime.fromisoformat(resolved).replace(tzinfo=UTC).timestamp() * 1000)
    except ValueError:
        current = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
        resolved = current.strftime("%Y-%m-%d")
        start = int(datetime.fromisoformat(resolved).replace(tzinfo=UTC).timestamp() * 1000)
    return start, resolved


def _count(row: Any) -> int:
    return 0 if row is None else int(row["n"])


def _at(row: Any, key: str) -> int | None:
    value = None if row is None else row[key]
    return None if value is None else int(value)


__all__ = ["QueryStorage"]
