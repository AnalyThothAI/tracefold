"""Bounded read projections for Cases, Signals, Observations, and executions.

Each page is one statement builder plus the method that runs it. The query-plan audit calls the same
builder with representative predicates, so what it EXPLAINs is the statement the route executes rather
than a copy of it that an edit can leave behind (`docs/MIGRATIONS.md`, database standard 3).

The `console_` prefix names a statement one of the four browser routes runs. `signal_ledger` and
`observation_ledger` lost it with the two `GET` routes that were their only browser readers: they are
`tracefold trading signals | observations` now, and each takes exactly the window and bound that
caller passes rather than the market, slot, kind and cursor predicates no caller ever set (#537 PR-5).
"""

from __future__ import annotations

from typing import Any

# Keyed on `created_at_ms`: when the Case formed, which is what "the lane produced N cases today"
# means. The admission ledger's own counts key on `source_observed_at_ms` instead, so a restarted
# runner re-reading a backlog cannot move yesterday's frames into today's total; a Case is created
# once and has no such backlog.
#
# `/api/trading/status` carried two more counts beside these -- one `count(*)` over `trading_cases`
# and one over `trading_trade_signals`, on every 15 s poll of every route -- and the only surface that
# ever printed them was the chrome figure strip #537 PR-5 deleted.
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
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """`GET /api/trading/cases`, with whichever of its two optional predicates the caller sent.

    There is no keyset predicate any more: the response published a `next_cursor` no reader ever sent
    back, and the desk opens one Case from `?case=<id>` rather than paging a list (#537 PR-5).
    """

    predicates = ["created_at_ms >= %(since)s"]
    params: dict[str, Any] = {"since": int(since_ms), "limit": int(limit)}
    if underlying_key is not None:
        predicates.append("underlying_key = %(underlying)s")
        params["underlying"] = underlying_key
    if states:
        predicates.append("state = ANY(%(states)s)")
        params["states"] = list(states)
    sql = f"""
        SELECT case_id, underlying_key, trigger_kind, primary_source_key, manifest,
               manifest_sha256, state, policy_decision, policy_reason, policy_checks,
               observed_at_ms, created_at_ms AS case_created_at_ms, decided_at_ms
          FROM trading_cases
         WHERE {" AND ".join(predicates)}
         ORDER BY created_at_ms DESC, case_id DESC
         LIMIT %(limit)s
    """  # noqa: S608 -- predicates are fixed fragments; all values remain bound
    return sql, params


def signal_ledger_statement(*, since_ns: int, limit: int) -> tuple[str, dict[str, Any]]:
    """`tracefold trading signals`: one bounded window of the engine-neutral Signal ledger."""

    sql = """
        SELECT seq, signal_id, case_id, market_key, direction,
               observed_at_ns, expires_at_ns
          FROM trading_trade_signals
         WHERE observed_at_ns >= %(since)s
         ORDER BY observed_at_ns DESC, signal_id DESC
         LIMIT %(limit)s
    """
    return sql, {"since": int(since_ns), "limit": int(limit)}


def observation_ledger_statement(*, since_ns: int, limit: int) -> tuple[str, dict[str, Any]]:
    """`tracefold trading observations`: one bounded window of the append-only Runtime stream."""

    sql = """
        SELECT seq, event_id, account_slot, execution_strategy,
               signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
               native_identity_references, summary
          FROM trading_execution_observations
         WHERE observed_at_ns >= %(since)s
         ORDER BY observed_at_ns DESC, event_id DESC
         LIMIT %(limit)s
    """
    return sql, {"since": int(since_ns), "limit": int(limit)}


def console_executions_statement(*, since_ns: int, limit: int) -> tuple[str, dict[str, Any]]:
    """`GET /api/trading/executions`: one row per entry identity, its whole venue outcome folded in.

    An entry identity is what the Runtime correlates its `order`, `fill`, `protection` and `position`
    observations under (`oi_runtime/observations.py:correlation`): a Signal's `signal_id`, or the
    `command_id` of a `manual_entry` Command. Folding only by `signal_id` (#528 PR-1) meant the one
    ingress an operator can prove the chain with had no row at all -- the CLI manual entry showed up
    in `commands[]` as an instruction and its fills, stop, exit and realized result were nowhere on
    the desk (#528 PR-3). Both windows are the same 24 hours and both fold the same way; `source`
    says which identity a row is, and `entry_id` is that identity.

    The entry leg is what the columns describe. An exit order and its fill share the entry identity,
    so both are filtered out of `order_status` and the fill aggregate; the position's own closed fact
    is where the exit shows up, with the price, the realized result and the reason.

    A Signal's entry verdict is a `signal_disposition` whose summary carries the one reason word; a
    manual entry's is the `control_disposition` the same Runtime path writes, where that word is
    `reason` beside the `accepted` / `rejected` split. One column, and `signal_disposition()` derives
    the split for both from it, because `dispose_command` computes the stored word from exactly the
    frozenset that function reads.
    """

    sql = """
        WITH signal_entry AS (
          SELECT 'signal'::text AS source,
                 signal_id AS entry_id,
                 case_id,
                 market_key,
                 direction,
                 observed_at_ns
            FROM trading_trade_signals
           WHERE observed_at_ns >= %(since)s
           ORDER BY observed_at_ns DESC, signal_id DESC
           LIMIT %(limit)s
        ),
        manual_entry AS (
          SELECT 'manual'::text AS source,
                 command_id AS entry_id,
                 NULL::text AS case_id,
                 market_key,
                 direction,
                 requested_at_ns AS observed_at_ns
            FROM trading_operator_intents
           WHERE action = 'manual_entry'
             AND requested_at_ns >= %(since)s
           ORDER BY requested_at_ns DESC, command_id DESC
           LIMIT %(limit)s
        ),
        entry_window AS (
          SELECT source, entry_id, case_id, market_key, direction, observed_at_ns
            FROM signal_entry
          UNION ALL
          SELECT source, entry_id, case_id, market_key, direction, observed_at_ns
            FROM manual_entry
        ),
        folded AS (
          SELECT entry.source,
                 entry.entry_id,
                 entry.case_id,
                 entry.market_key,
                 entry.direction,
                 entry.observed_at_ns,
                 (array_agg(
                    CASE WHEN observation.normalized_kind = 'control_disposition'
                         THEN observation.summary ->> 'reason'
                         ELSE observation.summary ->> 'disposition' END
                    ORDER BY observation.seq DESC)
                    FILTER (WHERE observation.normalized_kind
                                  IN ('signal_disposition', 'control_disposition')))[1]
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
                   AS position_summary
            FROM entry_window entry
            LEFT JOIN trading_execution_observations observation
                   ON coalesce(observation.signal_id, observation.command_id) = entry.entry_id
                  AND observation.normalized_kind
                      IN ('signal_disposition', 'control_disposition',
                          'order', 'fill', 'protection', 'position')
           GROUP BY entry.source, entry.entry_id, entry.case_id, entry.market_key, entry.direction,
                    entry.observed_at_ns
        )
        SELECT source, entry_id, case_id, market_key, direction, observed_at_ns,
               disposition_reason,
               order_status,
               trim_scale(fill_quantity)::text AS fill_quantity,
               trim_scale(fill_notional / NULLIF(fill_quantity, 0))::text AS fill_avg_price,
               stop_trigger_price,
               position_summary ->> 'status' AS position_status,
               position_summary ->> 'exit_price' AS exit_price,
               position_summary ->> 'realized_pnl_usd' AS realized_pnl_usd,
               position_summary ->> 'exit_reason' AS exit_reason
          FROM folded
         ORDER BY observed_at_ns DESC, entry_id DESC
    """
    return sql, {"since": int(since_ns), "limit": int(limit)}


def console_operator_intents_statement(
    *,
    since_ns: int,
    action: str | None = None,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """The Command ledger beside each Command's disposition observation.

    `GET /api/trading/executions` runs it unfiltered for the desk's ACT block; `tracefold trading
    commands --action` is the one caller that narrows it. The account-slot and cursor predicates went
    with the `GET /api/trading/execution/commands` route nothing in the browser called (#537 PR-5).
    """

    predicates = ["command.requested_at_ns >= %(since)s"]
    params: dict[str, Any] = {"since": int(since_ns), "limit": int(limit)}
    if action is not None:
        predicates.append("command.action = %(action)s")
        params["action"] = action
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
        limit: int,
    ) -> list[dict[str, Any]]:
        sql, params = console_cases_statement(
            since_ms=since_ms,
            underlying_key=underlying_key,
            states=states,
            limit=limit,
        )
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def signal_ledger(self, *, since_ns: int, limit: int) -> list[dict[str, Any]]:
        sql, params = signal_ledger_statement(since_ns=since_ns, limit=limit)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def observation_ledger(self, *, since_ns: int, limit: int) -> list[dict[str, Any]]:
        sql, params = observation_ledger_statement(since_ns=since_ns, limit=limit)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def console_operator_intents(
        self,
        *,
        since_ns: int,
        action: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        sql, params = console_operator_intents_statement(since_ns=since_ns, action=action, limit=limit)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def console_executions(self, *, since_ns: int, limit: int) -> list[dict[str, Any]]:
        sql, params = console_executions_statement(since_ns=since_ns, limit=limit)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]


__all__ = [
    "TRADING_CASE_COUNTS_SQL",
    "TRADING_CASE_REASON_COUNTS_SQL",
    "QueryStorage",
    "console_cases_statement",
    "console_executions_statement",
    "console_operator_intents_statement",
    "observation_ledger_statement",
    "signal_ledger_statement",
]
