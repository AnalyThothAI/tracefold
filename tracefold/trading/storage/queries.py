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

# The admission funnel's own top, and the two counts above are its bottom: every frame the lane looked
# at in the window, grouped by the answer admission gave it and the word that answer carries. It is a
# `count(*)` per `(status, reason)` pair, not the 400-row `decisions[]` download #589 PR-2 deleted --
# that read published one row per frame with its whole evidence blob, and this one publishes at most a
# dozen pairs whatever the window holds.
#
# Keyed on the *frame's* own observation clock, like every other read of this ledger: a restarted
# runner re-reading a backlog re-evaluates yesterday's frames today, and keying on
# `last_evaluated_at_ms` would move them into today's total. It is also the ledger's only indexed
# clock (`ix_trading_candidate_gate_observed`), so the window is an index scan rather than a pass over
# the 90-day retention.
TRADING_GATE_COUNTS_SQL = (
    "SELECT status, reason, count(*) AS n "
    "FROM trading_candidate_gate_decisions WHERE source_observed_at_ms >= %s "
    "GROUP BY status, reason ORDER BY n DESC, status, reason"
)


_CASE_COLUMNS = """
    case_id, underlying_key, trigger_kind, primary_source_key, manifest,
    manifest_sha256, state, policy_decision, policy_reason, policy_checks,
    observed_at_ms, created_at_ms AS case_created_at_ms, decided_at_ms
"""

# `GET /api/trading/cases?case_id=<id>`: the drawer's whole Case read, by primary key.
#
# The route used to answer with the newest 100 frozen Cases on every 15 s poll whether a drawer was
# open or not, and the desk rendered at most one of them -- so the 553 `NO_TRADE` Cases an operator
# most wants to open were the ones the page could not reach, and the payload it did download was
# read by nothing (#604 T3, audit E). Bounded by identity rather than by a window: a Case older than
# 24 h is exactly the one a reader followed a link to, and one primary-key row is a smaller read
# than any page of the window it lives in.
CONSOLE_CASE_BY_ID_SQL = f"""
    SELECT {_CASE_COLUMNS}
      FROM trading_cases
     WHERE case_id = %(case_id)s
"""  # noqa: S608 -- a module-owned column list; the identity stays bound


def console_cases_statement(
    *,
    since_ms: int,
    states: tuple[str, ...] = (),
    limit: int,
    to_ms: int = 2**63 - 1,
    asset: str | None = None,
    reason: str | None = None,
    source_item_id: str | None = None,
    cursor_at_ms: int | None = None,
    cursor_id: str = "",
    count_only: bool = False,
) -> tuple[str, dict[str, Any]]:
    predicates = ["created_at_ms >= %(since)s", "created_at_ms < %(to_ms)s"]
    params: dict[str, Any] = {"since": since_ms, "to_ms": to_ms, "limit": limit}
    for expression, key, value in (
        ("state = ANY(%(states)s)", "states", list(states) if states else None),
        ("underlying_key = %(asset)s", "asset", f"crypto:{asset}" if asset else None),
        ("policy_reason = %(reason)s", "reason", reason),
        ("manifest #>> '{contexts,oi,source_item_id}' = %(source_item_id)s", "source_item_id", source_item_id),
    ):
        if value is not None:
            predicates.append(expression)
            params[key] = value
    if cursor_at_ms is not None and not count_only:
        predicates.append("(created_at_ms, case_id) < (%(cursor_at)s, %(cursor_id)s)")
        params.update(cursor_at=cursor_at_ms, cursor_id=cursor_id)
    columns = "count(*) AS total" if count_only else _CASE_COLUMNS
    order = "" if count_only else "ORDER BY created_at_ms DESC, case_id DESC LIMIT %(limit)s"
    sql = f"SELECT {columns} FROM trading_cases WHERE {' AND '.join(predicates)} {order}"  # noqa: S608
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


def console_executions_statement(
    *, since_ns: int, limit: int, case_id: str | None = None
) -> tuple[str, dict[str, Any]]:
    """One row per entry identity: plans supply lifecycle, observations supply known history."""

    sql = """
        WITH signal_entry AS (
          SELECT 'signal'::text AS source,
                 signal_id AS entry_id,
                 case_id,
                 market_key,
                 direction,
                 observed_at_ns,
                 expires_at_ns
            FROM trading_trade_signals
           WHERE (%(case_id)s::text IS NOT NULL OR observed_at_ns >= %(since)s)
             AND (%(case_id)s::text IS NULL OR case_id = %(case_id)s)
             AND NOT EXISTS (SELECT 1 FROM trading_trade_plans plan WHERE plan.entry_id = signal_id)
           ORDER BY observed_at_ns DESC, signal_id DESC
           LIMIT %(limit)s
        ),
        manual_entry AS (
          SELECT 'manual'::text AS source,
                 command_id AS entry_id,
                 NULL::text AS case_id,
                 market_key,
                 direction,
                 requested_at_ns AS observed_at_ns,
                 NULL::bigint AS expires_at_ns
            FROM trading_operator_intents
           WHERE action = 'manual_entry' AND %(case_id)s::text IS NULL
             AND requested_at_ns >= %(since)s
             AND NOT EXISTS (SELECT 1 FROM trading_trade_plans plan WHERE plan.entry_id = command_id)
           ORDER BY requested_at_ns DESC, command_id DESC
           LIMIT %(limit)s
        ),
        planned_entry AS (
          SELECT source, entry_id, case_id, market_key, direction, created_at_ns AS observed_at_ns,
                 entry_expires_at_ns AS expires_at_ns
            FROM trading_trade_plans
           WHERE (%(case_id)s::text IS NOT NULL OR created_at_ns >= %(since)s
                  OR terminal_at_ns >= %(since)s OR terminal_at_ns IS NULL)
             AND (%(case_id)s::text IS NULL OR case_id = %(case_id)s)
           ORDER BY created_at_ns DESC, entry_id DESC LIMIT %(limit)s
        ),
        entry_window AS (
          SELECT source, entry_id, case_id, market_key, direction, observed_at_ns, expires_at_ns
            FROM signal_entry
          UNION ALL
          SELECT source, entry_id, case_id, market_key, direction, observed_at_ns, expires_at_ns
            FROM manual_entry
          UNION ALL
          SELECT source, entry_id, case_id, market_key, direction, observed_at_ns, expires_at_ns
            FROM planned_entry
        ),
        folded AS (
          SELECT entry.source,
                 entry.entry_id,
                 entry.case_id,
                 entry.market_key,
                 entry.direction,
                 entry.observed_at_ns,
                 entry.expires_at_ns,
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
                 (array_agg(observation.summary ->> 'reason' ORDER BY observation.seq DESC)
                    FILTER (WHERE observation.normalized_kind = 'order'
                              AND observation.summary ->> 'leg' = 'entry'
                              AND observation.summary ->> 'status' = 'rejected'))[1]
                   AS order_reject_reason,
                 min(observation.occurred_at_ns)
                    FILTER (WHERE observation.normalized_kind = 'fill'
                              AND observation.summary ->> 'leg' = 'entry')
                   AS entry_filled_at_ns,
                 min(observation.occurred_at_ns)
                    FILTER (WHERE observation.normalized_kind = 'position'
                              AND observation.summary ->> 'status' = 'closed')
                   AS position_closed_at_ns,
                 sum((observation.summary ->> 'last_quantity')::numeric)
                    FILTER (WHERE observation.normalized_kind = 'fill'
                              AND observation.summary ->> 'leg' = 'entry')
                   AS fill_quantity,
                 sum((observation.summary ->> 'last_quantity')::numeric
                     * (observation.summary ->> 'last_price')::numeric)
                    FILTER (WHERE observation.normalized_kind = 'fill'
                              AND observation.summary ->> 'leg' = 'entry')
                   AS fill_notional,
                 sum((observation.summary ->> 'last_quantity')::numeric)
                    FILTER (WHERE observation.normalized_kind = 'fill'
                              AND observation.summary ->> 'leg' IN ('exit', 'protection')) AS exit_fill_quantity,
                 (array_agg(observation.account_slot ORDER BY observation.seq DESC)
                    FILTER (WHERE observation.account_slot IS NOT NULL))[1] AS observed_account_slot,
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
                    entry.observed_at_ns, entry.expires_at_ns
        )
        SELECT folded.source, folded.entry_id, folded.case_id, folded.market_key, folded.direction,
               folded.observed_at_ns, folded.expires_at_ns, disposition_reason, order_status, order_reject_reason,
               coalesce(entry_filled_at_ns, plan.opened_at_ns) AS entry_filled_at_ns,
               coalesce(position_closed_at_ns, plan.terminal_at_ns) AS position_closed_at_ns,
               trim_scale(fill_quantity)::text AS fill_quantity,
               trim_scale(fill_notional / NULLIF(fill_quantity, 0))::text AS fill_avg_price,
               stop_trigger_price,
               CASE WHEN plan.status = 'closed' THEN 'closed'
                    ELSE position_summary ->> 'status' END AS position_status,
               position_summary ->> 'exit_price' AS exit_price,
               position_summary ->> 'realized_pnl_usd' AS realized_pnl_usd,
               position_summary ->> 'realized_pnl_usd' IS NOT NULL AS pnl_known,
               coalesce(plan.exit_reason, position_summary ->> 'exit_reason') AS exit_reason,
               plan.status AS plan_status, plan.stop_distance_bps, plan.exit_policy_id,
               plan.take_profit_bps, plan.max_holding_ns,
               plan.account_slot, plan.runtime_mode_at_creation, plan.instrument_id,
               plan.entry_client_order_id, trim_scale(plan.risk_budget_usd)::text AS risk_budget_usd,
               plan.max_leverage_at_creation,
               integrity.gap_reason IS NULL AS history_complete, integrity.gap_reason,
               CASE WHEN coalesce(position_closed_at_ns, plan.terminal_at_ns) IS NOT NULL
                         AND coalesce(entry_filled_at_ns, plan.opened_at_ns) IS NOT NULL
                    THEN greatest(0, coalesce(position_closed_at_ns, plan.terminal_at_ns)
                         - coalesce(entry_filled_at_ns, plan.opened_at_ns)) END AS duration_ns
          FROM folded
          LEFT JOIN trading_trade_plans plan ON plan.entry_id = folded.entry_id
          CROSS JOIN LATERAL (
            SELECT CASE
              WHEN plan.history_gap_reason IS NOT NULL THEN plan.history_gap_reason
              WHEN EXISTS (SELECT 1 FROM trading_execution_observations gap
                    WHERE gap.account_slot = coalesce(plan.account_slot, folded.observed_account_slot)
                      AND gap.normalized_kind = 'audit_gap'
                      AND gap.observed_at_ns >= coalesce(plan.created_at_ns, folded.observed_at_ns)
                      AND gap.occurred_at_ns <= coalesce(
                           position_closed_at_ns, plan.terminal_at_ns, 9223372036854775807))
                THEN 'audit_gap'
              WHEN (plan.opened_at_ns IS NOT NULL OR position_summary IS NOT NULL)
                    AND (fill_quantity IS NULL OR fill_quantity <= 0) THEN 'entry_fill_missing'
              WHEN plan.status = 'closed' AND position_closed_at_ns IS NULL THEN 'close_observation_missing'
              WHEN position_closed_at_ns IS NOT NULL
                    AND (exit_fill_quantity IS NULL OR exit_fill_quantity <> fill_quantity)
                THEN 'exit_fills_incomplete'
              ELSE NULL END AS gap_reason
          ) integrity
         ORDER BY folded.observed_at_ns DESC, folded.entry_id DESC
         LIMIT %(limit)s
    """
    return sql, {"since": int(since_ns), "limit": int(limit), "case_id": case_id}


def console_realized_totals_statement(
    *, account_slot: str, day_start_ns: int, day_end_ns: int
) -> tuple[str, dict[str, Any]]:
    """Known realized PnL and missingness, counting closed identities once."""

    sql = """
        WITH closing AS (
          SELECT DISTINCT ON (coalesce(signal_id, command_id))
                 coalesce(signal_id, command_id) AS entry_id, occurred_at_ns, summary
            FROM trading_execution_observations
           WHERE account_slot = %(slot)s AND (signal_id IS NOT NULL OR command_id IS NOT NULL)
             AND normalized_kind = 'position' AND summary ->> 'status' = 'closed'
           ORDER BY coalesce(signal_id, command_id), seq DESC
        ), terminal_plans AS (
          SELECT entry_id, opened_at_ns, created_at_ns, terminal_at_ns, history_gap_reason
            FROM trading_trade_plans
           WHERE account_slot = %(slot)s AND terminal_at_ns IS NOT NULL AND opened_at_ns IS NOT NULL
        ), closed AS (
          SELECT coalesce(plan.entry_id, closing.entry_id) AS entry_id,
                 coalesce(closing.occurred_at_ns, plan.terminal_at_ns) AS closed_at_ns,
                 (closing.summary ->> 'realized_pnl_usd')::numeric AS pnl,
                 plan.history_gap_reason IS NULL AND closing.entry_id IS NOT NULL
                   AND fills.entry_quantity > 0 AND fills.exit_quantity = fills.entry_quantity
                   AND NOT EXISTS (SELECT 1 FROM trading_execution_observations gap
                        WHERE gap.account_slot = %(slot)s AND gap.normalized_kind = 'audit_gap'
                          AND gap.observed_at_ns >= coalesce(plan.created_at_ns, fills.entry_at_ns)
                          AND gap.occurred_at_ns <= coalesce(closing.occurred_at_ns, plan.terminal_at_ns))
                   AS history_complete
            FROM terminal_plans plan FULL JOIN closing ON closing.entry_id = plan.entry_id
            CROSS JOIN LATERAL (
              SELECT min(occurred_at_ns) FILTER (WHERE summary ->> 'leg' = 'entry') AS entry_at_ns,
                     sum((summary ->> 'last_quantity')::numeric)
                       FILTER (WHERE summary ->> 'leg' = 'entry') AS entry_quantity,
                     sum((summary ->> 'last_quantity')::numeric)
                       FILTER (WHERE summary ->> 'leg' IN ('exit', 'protection')) AS exit_quantity
                FROM trading_execution_observations fill
               WHERE fill.account_slot = %(slot)s
                 AND coalesce(fill.signal_id, fill.command_id) = coalesce(plan.entry_id, closing.entry_id)
                 AND fill.normalized_kind = 'fill'
            ) fills
        )
        SELECT trim_scale(sum(pnl) FILTER (WHERE closed_at_ns >= %(day_start)s AND closed_at_ns < %(day_end)s))::text
                 AS realized_known_today_usd,
               trim_scale(sum(pnl))::text AS realized_known_total_usd,
               count(*) FILTER (WHERE closed_at_ns >= %(day_start)s AND closed_at_ns < %(day_end)s) AS closed_today,
               count(*) AS closed_total,
               count(pnl) FILTER (WHERE closed_at_ns >= %(day_start)s AND closed_at_ns < %(day_end)s)
                 AS pnl_known_today,
               count(pnl) AS pnl_known_total,
               count(*) FILTER (WHERE pnl IS NULL AND closed_at_ns >= %(day_start)s AND closed_at_ns < %(day_end)s)
                 AS pnl_missing_today,
               count(*) FILTER (WHERE pnl IS NULL) AS pnl_missing_total,
               coalesce(bool_and(pnl IS NOT NULL AND history_complete)
                 FILTER (WHERE closed_at_ns >= %(day_start)s AND closed_at_ns < %(day_end)s), true)
                 AS pnl_complete_today,
               coalesce(bool_and(pnl IS NOT NULL AND history_complete), true) AS pnl_complete_total
          FROM closed
    """
    return sql, {"slot": str(account_slot), "day_start": int(day_start_ns), "day_end": int(day_end_ns)}


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

    def gate_counts(self, *, since_ms: int) -> list[dict[str, Any]]:
        """One count per admission answer in the window, biggest first."""

        rows = self.conn.execute(TRADING_GATE_COUNTS_SQL, (int(since_ms),)).fetchall()
        return [{"status": str(row["status"]), "reason": row["reason"], "count": int(row["n"])} for row in rows]

    def console_cases(
        self,
        *,
        since_ms: int,
        states: tuple[str, ...],
        limit: int,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        sql, params = console_cases_statement(since_ms=since_ms, states=states, limit=limit, **filters)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def console_case_total(self, *, since_ms: int, **filters: Any) -> int:
        sql, params = console_cases_statement(since_ms=since_ms, count_only=True, limit=1, **filters)
        return int(self.conn.execute(sql, params).fetchone()["total"])

    def console_case(self, *, case_id: str) -> dict[str, Any] | None:
        """The one frozen Case behind `?case_id=<id>`. There is at most one: it is the primary key."""

        row = self.conn.execute(CONSOLE_CASE_BY_ID_SQL, {"case_id": str(case_id)}).fetchone()
        return None if row is None else dict(row)

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

    def console_executions(self, *, since_ns: int, limit: int, case_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = console_executions_statement(since_ns=since_ns, limit=limit, case_id=case_id)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def console_realized_totals(self, *, account_slot: str, day_start_ns: int, day_end_ns: int) -> dict[str, Any]:
        sql, params = console_realized_totals_statement(
            account_slot=account_slot, day_start_ns=day_start_ns, day_end_ns=day_end_ns
        )
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else {}


__all__ = [
    "CONSOLE_CASE_BY_ID_SQL",
    "TRADING_CASE_COUNTS_SQL",
    "TRADING_CASE_REASON_COUNTS_SQL",
    "TRADING_GATE_COUNTS_SQL",
    "QueryStorage",
    "console_cases_statement",
    "console_executions_statement",
    "console_operator_intents_statement",
    "console_realized_totals_statement",
    "observation_ledger_statement",
    "signal_ledger_statement",
]
