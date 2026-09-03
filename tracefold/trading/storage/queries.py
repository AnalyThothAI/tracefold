"""Bounded read projections for Cases, Signals, Observations, and readiness.

Each console page is one statement builder plus the method that runs it. The query-plan audit calls the
same builder with representative predicates, so what it EXPLAINs is the statement the route executes
rather than a copy of it that an edit can leave behind (`docs/MIGRATIONS.md`, database standard 3).
"""

from __future__ import annotations

from typing import Any

# Keyed on `created_at_ms`: when the Case formed, which is what "the lane produced N cases today"
# means. The admission ledger's own counts key on `source_observed_at_ms` instead, so a restarted
# runner re-reading a backlog cannot move yesterday's frames into today's total; a Case is created
# once and has no such backlog.
#
# Two numbers, both rendered. `no_trade_24h`, `blocked_24h`, `cases_open` and `signals_unexpired`
# were four more counts on the same two tables that no surface ever printed (#528).
TRADING_STATUS_CASE_COUNTS_SQL = """
    SELECT count(*) AS cases_24h
      FROM trading_cases
     WHERE created_at_ms >= %(since)s
"""
TRADING_STATUS_SIGNAL_COUNTS_SQL = """
    SELECT count(*) AS signals_24h
      FROM trading_trade_signals
     WHERE observed_at_ns >= %(since)s
"""
TRADING_CASE_COUNTS_SQL = "SELECT state, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY state"
TRADING_CASE_REASON_COUNTS_SQL = (
    "SELECT coalesce(policy_reason, 'undecided') AS reason, count(*) AS n "
    "FROM trading_cases WHERE created_at_ms >= %s GROUP BY reason"
)


def console_cases_statement(
    *,
    since_ms: int,
    underlying_key: str | None = None,
    states: tuple[str, ...] = (),
    before: tuple[int, str] | None = None,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """`GET /api/trading/cases`, with whichever of its three optional predicates the caller sent."""

    predicates = ["created_at_ms >= %(since)s"]
    params: dict[str, Any] = {"since": int(since_ms), "limit": int(limit)}
    if underlying_key is not None:
        predicates.append("underlying_key = %(underlying)s")
        params["underlying"] = underlying_key
    if states:
        predicates.append("state = ANY(%(states)s)")
        params["states"] = list(states)
    if before is not None:
        predicates.append("(created_at_ms, case_id) < (%(before_ms)s, %(before_id)s)")
        params["before_ms"], params["before_id"] = before
    sql = f"""
        SELECT case_id, underlying_key, trigger_kind, primary_source_key, manifest,
               manifest_sha256, state, policy_decision, policy_reason, policy_checks,
               observed_at_ms, created_at_ms AS case_created_at_ms, decided_at_ms,
               strategy_id, strategy_version, strategy_config_digest
          FROM trading_cases
         WHERE {" AND ".join(predicates)}
         ORDER BY created_at_ms DESC, case_id DESC
         LIMIT %(limit)s
    """  # noqa: S608 -- predicates are fixed fragments; all values remain bound
    return sql, params


def console_signals_statement(
    *,
    since_ns: int,
    market_key: str | None = None,
    before: tuple[int, str] | None = None,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """`GET /api/trading/signals`."""

    predicates = ["observed_at_ns >= %(since)s"]
    params: dict[str, Any] = {"since": int(since_ns), "limit": int(limit)}
    if market_key is not None:
        predicates.append("market_key = %(market_key)s")
        params["market_key"] = market_key
    if before is not None:
        predicates.append("(observed_at_ns, signal_id) < (%(before_ns)s, %(before_id)s)")
        params["before_ns"], params["before_id"] = before
    sql = f"""
        SELECT seq, signal_id, case_id, market_key, direction,
               observed_at_ns, expires_at_ns, alpha_metadata
          FROM trading_trade_signals
         WHERE {" AND ".join(predicates)}
         ORDER BY observed_at_ns DESC, signal_id DESC
         LIMIT %(limit)s
    """  # noqa: S608 -- predicates are fixed fragments; all values remain bound
    return sql, params


def console_execution_observations_statement(
    *,
    since_ns: int,
    account_slot: str | None = None,
    normalized_kind: str | None = None,
    before: tuple[int, str] | None = None,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """`GET /api/trading/execution/observations`."""

    predicates = ["observed_at_ns >= %(since)s"]
    params: dict[str, Any] = {"since": int(since_ns), "limit": int(limit)}
    if account_slot is not None:
        predicates.append("account_slot = %(slot)s")
        params["slot"] = account_slot
    if normalized_kind is not None:
        predicates.append("normalized_kind = %(kind)s")
        params["kind"] = normalized_kind
    if before is not None:
        predicates.append("(observed_at_ns, event_id) < (%(before_ns)s, %(before_id)s)")
        params["before_ns"], params["before_id"] = before
    sql = f"""
        SELECT seq, event_id, account_slot, runtime_release, execution_strategy,
               signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
               native_identity_references, summary
          FROM trading_execution_observations
         WHERE {" AND ".join(predicates)}
         ORDER BY observed_at_ns DESC, event_id DESC
         LIMIT %(limit)s
    """  # noqa: S608 -- predicates are fixed fragments; all values remain bound
    return sql, params


def console_executions_statement(*, since_ns: int, limit: int) -> tuple[str, dict[str, Any]]:
    """`GET /api/trading/executions`: one row per Signal, its whole venue outcome folded in.

    The console had no such read. `GET /api/trading/execution/observations` returns the raw stream and
    the page correlated it in the browser by `command_id`, which a flatten close -- carried under the
    entry Signal's own `signal_id` -- never matches (#528 C). Correlating where the rows are is also
    the only way one Signal is one row: the fold is by `signal_id`, which is exactly what every
    `order`, `fill`, `protection` and `position` observation of an entry carries.

    The entry leg is what the columns describe. An exit order and its fill share the Signal's
    identity, so both are filtered out of `order_status` and the fill aggregate; the position's own
    closed fact is where the exit shows up, with the price, the realized result and the reason.
    """

    sql = """
        WITH signal_window AS (
          SELECT signal_id, case_id, market_key, direction, observed_at_ns
            FROM trading_trade_signals
           WHERE observed_at_ns >= %(since)s
           ORDER BY observed_at_ns DESC, signal_id DESC
           LIMIT %(limit)s
        ),
        folded AS (
          SELECT signal.signal_id,
                 signal.case_id,
                 signal.market_key,
                 signal.direction,
                 signal.observed_at_ns,
                 (array_agg(observation.summary ->> 'disposition' ORDER BY observation.seq DESC)
                    FILTER (WHERE observation.normalized_kind = 'signal_disposition'))[1]
                   AS disposition_reason,
                 (array_agg(observation.summary ->> 'status' ORDER BY observation.seq DESC)
                    FILTER (WHERE observation.normalized_kind = 'order'
                              AND observation.summary ->> 'leg' = 'entry'))[1]
                   AS order_status,
                 sum((observation.summary ->> 'last_quantity')::numeric)
                    FILTER (WHERE observation.normalized_kind = 'fill'
                              AND observation.summary ->> 'leg' = 'entry')
                   AS fill_quantity,
                 sum((observation.summary ->> 'last_quantity')::numeric
                     * (observation.summary ->> 'last_price')::numeric)
                    FILTER (WHERE observation.normalized_kind = 'fill'
                              AND observation.summary ->> 'leg' = 'entry')
                   AS fill_notional,
                 (array_agg(observation.summary ->> 'trigger_price' ORDER BY observation.seq DESC)
                    FILTER (WHERE observation.normalized_kind = 'protection'
                              AND observation.summary ->> 'trigger_price' IS NOT NULL))[1]
                   AS stop_trigger_price,
                 (array_agg(observation.summary ORDER BY observation.seq DESC)
                    FILTER (WHERE observation.normalized_kind = 'position'))[1]
                   AS position_summary,
                 max(observation.observed_at_ns) AS last_observation_at_ns
            FROM signal_window signal
            LEFT JOIN trading_execution_observations observation
                   ON observation.signal_id = signal.signal_id
                  AND observation.normalized_kind
                      IN ('signal_disposition', 'order', 'fill', 'protection', 'position')
           GROUP BY signal.signal_id, signal.case_id, signal.market_key, signal.direction,
                    signal.observed_at_ns
        )
        SELECT signal_id, case_id, market_key, direction, observed_at_ns,
               disposition_reason,
               order_status,
               trim_scale(fill_quantity)::text AS fill_quantity,
               trim_scale(fill_notional / NULLIF(fill_quantity, 0))::text AS fill_avg_price,
               stop_trigger_price,
               position_summary ->> 'status' AS position_status,
               position_summary ->> 'exit_price' AS exit_price,
               position_summary ->> 'realized_pnl_usd' AS realized_pnl_usd,
               position_summary ->> 'exit_reason' AS exit_reason,
               greatest(observed_at_ns, coalesce(last_observation_at_ns, 0)) AS last_observed_at_ns
          FROM folded
         ORDER BY observed_at_ns DESC, signal_id DESC
    """
    return sql, {"since": int(since_ns), "limit": int(limit)}


def console_operator_intents_statement(
    *,
    since_ns: int,
    account_slot: str | None = None,
    action: str | None = None,
    before: tuple[int, str] | None = None,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """`GET /api/trading/execution/commands`, each Command beside its disposition observation."""

    predicates = ["command.requested_at_ns >= %(since)s"]
    params: dict[str, Any] = {"since": int(since_ns), "limit": int(limit)}
    if account_slot is not None:
        predicates.append("command.account_slot = %(slot)s")
        params["slot"] = account_slot
    if action is not None:
        predicates.append("command.action = %(action)s")
        params["action"] = action
    if before is not None:
        predicates.append("(command.requested_at_ns, command.command_id) < (%(before_ns)s, %(before_id)s)")
        params["before_ns"], params["before_id"] = before
    sql = f"""
        SELECT command.seq, command.command_id, command.account_slot, command.action,
               command.scope, command.reason, command.operator_identity,
               command.requested_at_ns, command.expires_at_ns,
               command.market_key, command.direction,
               disposition.summary ->> 'disposition' AS disposition,
               disposition.summary ->> 'reason' AS disposition_reason
          FROM trading_operator_intents command
          LEFT JOIN trading_execution_observations disposition
            ON disposition.command_id = command.command_id
           AND disposition.normalized_kind = 'control_disposition'
         WHERE {" AND ".join(predicates)}
         ORDER BY command.requested_at_ns DESC, command.command_id DESC
         LIMIT %(limit)s
    """  # noqa: S608 -- predicates are fixed fragments; all values remain bound
    return sql, params


class QueryStorage:
    conn: Any

    def runtime_summary(self, *, since_ms: int) -> dict[str, int]:
        case_row = self.conn.execute(TRADING_STATUS_CASE_COUNTS_SQL, {"since": int(since_ms)}).fetchone()
        signal_row = self.conn.execute(
            TRADING_STATUS_SIGNAL_COUNTS_SQL,
            {"since": int(since_ms) * 1_000_000},
        ).fetchone()
        return {
            "cases_24h": int((case_row or {}).get("cases_24h") or 0),
            "signals_24h": int((signal_row or {}).get("signals_24h") or 0),
        }

    def case_counts(self, *, since_ms: int) -> dict[str, int]:
        rows = self.conn.execute(TRADING_CASE_COUNTS_SQL, (int(since_ms),)).fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    def case_reason_counts(self, *, since_ms: int) -> dict[str, int]:
        rows = self.conn.execute(TRADING_CASE_REASON_COUNTS_SQL, (int(since_ms),)).fetchall()
        return {str(row["reason"]): int(row["n"]) for row in rows}

    def console_cases(
        self,
        *,
        since_ms: int,
        underlying_key: str | None,
        states: tuple[str, ...],
        before: tuple[int, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        sql, params = console_cases_statement(
            since_ms=since_ms,
            underlying_key=underlying_key,
            states=states,
            before=before,
            limit=limit,
        )
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def console_signals(
        self,
        *,
        since_ns: int,
        market_key: str | None,
        before: tuple[int, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        sql, params = console_signals_statement(
            since_ns=since_ns,
            market_key=market_key,
            before=before,
            limit=limit,
        )
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def console_execution_observations(
        self,
        *,
        since_ns: int,
        account_slot: str | None,
        normalized_kind: str | None,
        before: tuple[int, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        sql, params = console_execution_observations_statement(
            since_ns=since_ns,
            account_slot=account_slot,
            normalized_kind=normalized_kind,
            before=before,
            limit=limit,
        )
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def console_operator_intents(
        self,
        *,
        since_ns: int,
        account_slot: str | None,
        action: str | None,
        before: tuple[int, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        sql, params = console_operator_intents_statement(
            since_ns=since_ns,
            account_slot=account_slot,
            action=action,
            before=before,
            limit=limit,
        )
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def console_executions(self, *, since_ns: int, limit: int) -> list[dict[str, Any]]:
        sql, params = console_executions_statement(since_ns=since_ns, limit=limit)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]


__all__ = [
    "TRADING_CASE_COUNTS_SQL",
    "TRADING_CASE_REASON_COUNTS_SQL",
    "TRADING_STATUS_CASE_COUNTS_SQL",
    "TRADING_STATUS_SIGNAL_COUNTS_SQL",
    "QueryStorage",
    "console_cases_statement",
    "console_execution_observations_statement",
    "console_executions_statement",
    "console_operator_intents_statement",
    "console_signals_statement",
]
