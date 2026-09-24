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
TRADING_CASE_DECISION_COUNTS_SQL = (
    "SELECT decision.action, decision.publish_status, count(*) AS n "
    "FROM trading_case_decisions decision "
    "JOIN trading_cases case_row ON case_row.case_id = decision.case_id "
    "WHERE case_row.created_at_ms >= %s "
    "GROUP BY decision.action, decision.publish_status ORDER BY decision.action, decision.publish_status"
)
TRADING_CASE_LIST_LATEST_SQL = (
    "SELECT DISTINCT ON (c.trigger_id) c.trigger_id,c.case_id,c.state,c.analysis_status,"
    "c.policy_reason,c.decided_at_ms,d.action,d.publish_status,d.decision ->> 'side' AS side "
    "FROM trading_cases c LEFT JOIN trading_case_decisions d USING (case_id) "
    "WHERE c.trigger_id=ANY(%s) "
    "ORDER BY c.trigger_id,c.recheck_seq DESC,c.created_at_ms DESC,c.case_id DESC"
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
    observed_at_ms, created_at_ms AS case_created_at_ms, decided_at_ms,
    trigger_id, run_kind, recheck_seq, root_expires_at_ms,
    target_asset_id, target_selection, entry_scope_id,
    mapping_semantics_digest, analysis_status, evidence_ref,
    (SELECT source.payload ->> 'evidence_ref' FROM trading_triggers source
      WHERE source.trigger_id = trading_cases.trigger_id AND source.kind = 'oi') AS source_item_id
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

TRADING_CASE_DECISION_SQL = (
    "SELECT decision_id, policy_id, policy_version, assessment_ref, "
    "action, decision, publish_status, publish_reason, decided_at_ms, valid_until_ms "
    "FROM trading_case_decisions WHERE case_id=%s"
)
TRADING_CASE_OUTCOMES_SQL = (
    "SELECT axis,horizon_seconds,label_version,status,return_bps,available_at_ms,"
    "labeled_at_ms,path_ref FROM trading_case_outcomes WHERE case_id=%s "
    "ORDER BY axis,horizon_seconds"
)
TRADING_CASE_ATTEMPTS_SQL = (
    "SELECT case_id,claim_attempt,brief_ref,evidence_ref,assessment_ref,model_name,"
    "prompt_sha,started_at_ms,ended_at_ms,provider_status,analysis_status,error_code,"
    "validation_errors,physical_call_count,input_tokens,output_tokens,cost_microusd,"
    "known_cost_microusd,unknown_cost_calls,cost_upper_estimate_microusd,"
    "cost_unknown_reason,settled FROM trading_case_attempts WHERE case_id=%s "
    "ORDER BY claim_attempt DESC LIMIT 128"
)
TRADING_CASE_MODEL_CALLS_SQL = (
    "SELECT claim_attempt,call_index,status,started_at_ms,finished_at_ms,timeout_ms,"
    "remaining_deadline_ms,reserved_cost_microusd,request_ref,response_ref,input_tokens,"
    "output_tokens,cost_microusd,cost_unknown_reason FROM trading_model_calls "
    "WHERE case_id=%s ORDER BY claim_attempt DESC,call_index LIMIT 256"
)
TRADING_CASE_WATCH_SQL = (
    "SELECT parent_case_id,trigger_id,condition,status,last_observation_status,"
    "last_observed_at_ms,last_observation_ref,"
    "trigger_side,"
    "last_observed_value,next_check_at_ms,expires_at_ms,child_case_id,created_at_ms,"
    "updated_at_ms FROM trading_watch_observations WHERE parent_case_id=%s"
)
TRADING_CASE_CHAIN_SQL = (
    "SELECT c.case_id,c.run_kind,c.recheck_seq,c.state,c.analysis_status,c.created_at_ms,"
    "c.decided_at_ms,d.action,d.publish_status,d.decision ->> 'side' AS side "
    "FROM trading_cases c LEFT JOIN trading_case_decisions d USING (case_id) "
    "WHERE c.trigger_id=%s ORDER BY c.recheck_seq,c.created_at_ms,c.case_id LIMIT 8"
)
TRADING_CASE_EVALUATIONS_SQL = (
    "SELECT source,evaluation_version,status,reason,decision_at_ms,scheduled_at_ms,"
    "due_at_ms,decision_quote_ref,planned_quote_ref,mark_path_ref,funding_ref,"
    "venue_receipt_ref,result,evaluated_at_ms FROM trading_case_evaluations "
    "WHERE case_id=%s ORDER BY source,evaluation_version"
)


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
    predicates = [
        "created_at_ms >= %(since)s",
        "created_at_ms < %(to_ms)s",
        "(trigger_id IS NULL OR run_kind='initial')",
    ]
    params: dict[str, Any] = {"since": since_ms, "to_ms": to_ms, "limit": limit}
    for expression, key, value in (
        ("state = ANY(%(states)s)", "states", list(states) if states else None),
        ("underlying_key = %(asset)s", "asset", f"crypto:{asset}" if asset else None),
        ("policy_reason = %(reason)s", "reason", reason),
        (
            "(manifest #>> '{contexts,oi,source_item_id}' = %(source_item_id)s "
            "OR EXISTS (SELECT 1 FROM trading_triggers source "
            "WHERE source.trigger_id = trading_cases.trigger_id AND source.kind = 'oi' "
            "AND source.payload ->> 'evidence_ref' = %(source_item_id)s))",
            "source_item_id",
            source_item_id,
        ),
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


# A fill's contribution to realized PnL, in one place for both reads below. A fill carries its own
# commission since #680; a fill without one, or with one charged in anything but the quote currency,
# makes that entry's realized result unknown rather than silently gross.
_FILL_FOLD = """
                 sum((fill.summary ->> 'last_quantity')::numeric)
                    FILTER (WHERE fill.summary ->> 'leg' = 'entry') AS entry_quantity,
                 sum((fill.summary ->> 'last_quantity')::numeric * (fill.summary ->> 'last_price')::numeric)
                    FILTER (WHERE fill.summary ->> 'leg' = 'entry') AS entry_notional,
                 min(fill.occurred_at_ns) FILTER (WHERE fill.summary ->> 'leg' = 'entry') AS entry_filled_at_ns,
                 max(fill.occurred_at_ns) FILTER (WHERE fill.summary ->> 'leg' <> 'entry') AS exit_filled_at_ns,
                 sum((fill.summary ->> 'last_quantity')::numeric)
                    FILTER (WHERE fill.summary ->> 'leg' <> 'entry') AS exit_quantity,
                 sum((fill.summary ->> 'last_quantity')::numeric * (fill.summary ->> 'last_price')::numeric)
                    FILTER (WHERE fill.summary ->> 'leg' <> 'entry') AS exit_notional,
                 sum((fill.summary ->> 'commission')::numeric) AS fees,
                 bool_and(fill.summary ->> 'commission_currency' = 'USDT'
                          AND fill.summary ->> 'commission' IS NOT NULL) AS fees_known
"""


def _realized_pnl(direction: str, fills: str) -> str:
    """Fee-adjusted realized PnL; PAPER funding is folded separately below.

    Known only for a fully closed entry whose every fill carries a quote-currency commission.
    """

    return f"""CASE WHEN {fills}.entry_quantity > 0
                     AND {fills}.exit_quantity = {fills}.entry_quantity
                     AND {fills}.fees_known
                THEN (CASE WHEN {direction} = 'short' THEN -1 ELSE 1 END)
                     * ({fills}.exit_notional - {fills}.entry_notional) - {fills}.fees
           END"""


# Signed account income has no entry identity. It can be attributed only to a sole
# PAPER plan for that symbol and account during the actual fill-to-fill interval.
# A successful complete signed-income read proves zero funding as well as nonzero
# cashflows. range_agg joins overlapping scan windows; a gap leaves net unknown.
_PAPER_FUNDING_FOLD = """
    LEFT JOIN LATERAL (
      SELECT CASE WHEN plan.runtime_mode_at_creation = 'paper'
                       AND fills.entry_filled_at_ns IS NOT NULL
                       AND fills.exit_filled_at_ns IS NOT NULL
                       AND fills.exit_filled_at_ns >= fills.entry_filled_at_ns
                       AND coverage.covered @> int8range(
                         fills.entry_filled_at_ns, fills.exit_filled_at_ns, '[]')
                       AND NOT EXISTS (
                         SELECT 1 FROM trading_trade_plans other_plan
                          WHERE other_plan.account_slot = plan.account_slot
                            AND other_plan.entry_id <> plan.entry_id
                            AND other_plan.instrument_id = plan.instrument_id
                            AND other_plan.opened_at_ns <= fills.exit_filled_at_ns
                            AND (other_plan.terminal_at_ns IS NULL
                                 OR other_plan.terminal_at_ns >= fills.entry_filled_at_ns))
                       AND coalesce(income.invalid_count, 0) = 0
                  THEN coalesce(income.amount_usd, 0::numeric) END AS funding_usd
        FROM (
          SELECT range_agg(int8range(
                   (observation.summary ->> 'start_at_ns')::bigint,
                   (observation.summary ->> 'end_at_ns')::bigint, '[]')) AS covered
            FROM trading_execution_observations observation
           WHERE observation.account_slot = plan.account_slot
             AND observation.normalized_kind = 'funding_coverage'
             AND observation.summary ->> 'source' = 'signed_income_v1'
             AND observation.summary ->> 'status' = 'complete'
             AND observation.summary ->> 'start_at_ns' ~ '^[0-9]+$'
             AND observation.summary ->> 'end_at_ns' ~ '^[0-9]+$'
             AND observation.occurred_at_ns >= fills.entry_filled_at_ns
        ) coverage
        CROSS JOIN LATERAL (
          SELECT sum(CASE WHEN observation.summary ->> 'asset' = 'USDT'
                           AND observation.summary ->> 'amount_decimal' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                          THEN (observation.summary ->> 'amount_decimal')::numeric END) AS amount_usd,
                 count(*) FILTER (WHERE observation.summary ->> 'asset' IS DISTINCT FROM 'USDT'
                                     OR coalesce(observation.summary ->> 'amount_decimal', '')
                                        !~ '^-?[0-9]+(\\.[0-9]+)?$')
                   AS invalid_count
            FROM trading_execution_observations observation
           WHERE observation.account_slot = plan.account_slot
             AND observation.normalized_kind = 'funding'
             AND observation.summary ->> 'source' = 'signed_income_v1'
             AND observation.summary ->> 'symbol' = split_part(plan.instrument_id, '-', 1)
             AND observation.occurred_at_ns BETWEEN fills.entry_filled_at_ns AND fills.exit_filled_at_ns
        ) income
    ) paper_funding ON true
"""


def console_executions_statement(
    *, since_ns: int, limit: int, case_id: str | None = None
) -> tuple[str, dict[str, Any]]:
    """One row per entry identity: plans supply lifecycle, observations supply what the venue did."""

    sql = f"""
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
                              AND observation.summary ->> 'status' IN ('rejected', 'denied')))[1]
                   AS order_reject_reason,
                 min(observation.occurred_at_ns)
                    FILTER (WHERE observation.normalized_kind = 'position'
                              AND observation.summary ->> 'status' = 'closed')
                   AS position_closed_at_ns,
                 (array_agg(observation.summary ->> 'trigger_price' ORDER BY observation.seq DESC)
                    FILTER (WHERE observation.normalized_kind = 'protection'
                              AND observation.summary ->> 'trigger_price' IS NOT NULL
                              AND observation.summary ->> 'leg' IS DISTINCT FROM 'take_profit'))[1]
                   AS stop_trigger_price,
                 (array_agg(observation.summary ->> 'trigger_price' ORDER BY observation.seq DESC)
                    FILTER (WHERE observation.normalized_kind = 'protection'
                              AND observation.summary ->> 'trigger_price' IS NOT NULL
                              AND observation.summary ->> 'leg' = 'take_profit'))[1]
                   AS take_profit_trigger_price,
                 (array_agg(observation.summary ->> 'status' ORDER BY observation.seq DESC)
                    FILTER (WHERE observation.normalized_kind = 'position'))[1]
                   AS position_status
            FROM entry_window entry
            LEFT JOIN trading_execution_observations observation
                   ON (observation.signal_id = entry.entry_id OR observation.command_id = entry.entry_id)
                  AND observation.normalized_kind
                      IN ('signal_disposition', 'control_disposition', 'order', 'protection', 'position')
           GROUP BY entry.source, entry.entry_id, entry.case_id, entry.market_key, entry.direction,
                    entry.observed_at_ns, entry.expires_at_ns
        )
        SELECT folded.source, folded.entry_id, folded.case_id, folded.market_key, folded.direction,
               folded.observed_at_ns, folded.expires_at_ns, disposition_reason, order_status, order_reject_reason,
               coalesce(fills.entry_filled_at_ns, plan.opened_at_ns) AS entry_filled_at_ns,
               coalesce(position_closed_at_ns, plan.terminal_at_ns) AS position_closed_at_ns,
               trim_scale(fills.entry_quantity)::text AS fill_quantity,
               trim_scale(fills.entry_notional / NULLIF(fills.entry_quantity, 0))::text AS fill_avg_price,
               stop_trigger_price, take_profit_trigger_price,
               CASE WHEN plan.status = 'closed' THEN 'closed' ELSE position_status END AS position_status,
               trim_scale(fills.exit_notional / NULLIF(fills.exit_quantity, 0))::text AS exit_price,
               trim_scale({_realized_pnl("folded.direction", "fills")})::text AS realized_pnl_usd,
               CASE WHEN fills.fees_known THEN trim_scale(fills.fees)::text END AS fees_usd,
               trim_scale(paper_funding.funding_usd)::text AS funding_usd,
               CASE WHEN paper_funding.funding_usd IS NOT NULL
                    THEN trim_scale({_realized_pnl("folded.direction", "fills")}
                                    + paper_funding.funding_usd)::text END AS paper_net_pnl_usd,
               plan.exit_reason,
               plan.status AS plan_status, plan.stop_distance_bps, plan.exit_policy_id,
               plan.take_profit_bps, plan.max_holding_ns,
               plan.account_slot, plan.runtime_mode_at_creation, plan.instrument_id,
               plan.entry_client_order_id, trim_scale(plan.risk_budget_usd)::text AS risk_budget_usd,
               plan.max_leverage_at_creation,
               CASE WHEN coalesce(position_closed_at_ns, plan.terminal_at_ns) IS NOT NULL
                         AND coalesce(fills.entry_filled_at_ns, plan.opened_at_ns) IS NOT NULL
                    THEN greatest(0, coalesce(position_closed_at_ns, plan.terminal_at_ns)
                         - coalesce(fills.entry_filled_at_ns, plan.opened_at_ns)) END AS duration_ns
          FROM folded
          LEFT JOIN trading_trade_plans plan ON plan.entry_id = folded.entry_id
          CROSS JOIN LATERAL (
            SELECT {_FILL_FOLD}
              FROM trading_execution_observations fill
             WHERE (fill.signal_id = folded.entry_id OR fill.command_id = folded.entry_id)
               AND fill.normalized_kind = 'fill'
          ) fills
          {_PAPER_FUNDING_FOLD}
         ORDER BY folded.observed_at_ns DESC, folded.entry_id DESC
         LIMIT %(limit)s
    """  # noqa: S608 -- module-owned fragments; every value stays bound
    return sql, {"since": int(since_ns), "limit": int(limit), "case_id": case_id}


def console_realized_totals_statement(
    *, account_slot: str, day_start_ns: int, day_end_ns: int
) -> tuple[str, dict[str, Any]]:
    """Realized PnL folded from the fill journal, over every plan this slot opened and closed."""

    sql = f"""
        WITH closed AS (
          SELECT plan.terminal_at_ns AS closed_at_ns,
                 plan.runtime_mode_at_creation AS runtime_mode,
                 {_realized_pnl("plan.direction", "fills")} AS pnl,
                 CASE WHEN paper_funding.funding_usd IS NOT NULL
                      THEN {_realized_pnl("plan.direction", "fills")}
                           + paper_funding.funding_usd END AS paper_net_pnl
            FROM trading_trade_plans plan
            CROSS JOIN LATERAL (
              SELECT {_FILL_FOLD}
                FROM trading_execution_observations fill
               WHERE fill.account_slot = plan.account_slot
                 AND (fill.signal_id = plan.entry_id OR fill.command_id = plan.entry_id)
                 AND fill.normalized_kind = 'fill'
            ) fills
            {_PAPER_FUNDING_FOLD}
           WHERE plan.account_slot = %(slot)s AND plan.terminal_at_ns IS NOT NULL AND plan.opened_at_ns IS NOT NULL
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
               trim_scale(sum(paper_net_pnl) FILTER (
                 WHERE closed_at_ns >= %(day_start)s AND closed_at_ns < %(day_end)s))::text
                 AS paper_net_known_today_usd,
               trim_scale(sum(paper_net_pnl))::text AS paper_net_known_total_usd,
               count(paper_net_pnl) FILTER (WHERE closed_at_ns >= %(day_start)s AND closed_at_ns < %(day_end)s)
                 AS paper_net_known_today,
               count(paper_net_pnl) AS paper_net_known_total,
               count(*) FILTER (WHERE paper_net_pnl IS NULL
                                 AND runtime_mode = 'paper'
                                 AND closed_at_ns >= %(day_start)s AND closed_at_ns < %(day_end)s)
                 AS paper_net_missing_today,
               count(*) FILTER (WHERE paper_net_pnl IS NULL AND runtime_mode = 'paper') AS paper_net_missing_total,
               count(*) FILTER (WHERE runtime_mode = 'paper'
                                 AND closed_at_ns >= %(day_start)s AND closed_at_ns < %(day_end)s)
                 AS paper_closed_today,
               count(*) FILTER (WHERE runtime_mode = 'paper') AS paper_closed_total
          FROM closed
    """  # noqa: S608 -- module-owned fragments; every value stays bound
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

    def case_decision_counts(self, *, since_ms: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(TRADING_CASE_DECISION_COUNTS_SQL, (int(since_ms),)).fetchall()
        return [
            {"action": str(row["action"]), "publish_status": str(row["publish_status"]), "count": int(row["n"])}
            for row in rows
        ]

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
        rows = [dict(row) for row in self.conn.execute(sql, params).fetchall()]
        triggers = [str(row["trigger_id"]) for row in rows if row.get("trigger_id")]
        if triggers:
            latest = self.conn.execute(TRADING_CASE_LIST_LATEST_SQL, (triggers,)).fetchall()
            by_trigger = {str(row["trigger_id"]): row for row in latest}
            for row in rows:
                member = by_trigger.get(str(row.get("trigger_id")))
                if member is not None:
                    row["latest_case_id"] = member["case_id"]
                    row["state"] = member["state"]
                    row["analysis_status"] = member["analysis_status"]
                    row["policy_reason"] = member["policy_reason"]
                    row["decided_at_ms"] = member["decided_at_ms"]
                    row["analysis_action"] = member["action"]
                    row["analysis_publish_status"] = member["publish_status"]
                    row["analysis_side"] = member["side"]
        return rows

    def console_case_total(self, *, since_ms: int, **filters: Any) -> int:
        sql, params = console_cases_statement(since_ms=since_ms, count_only=True, limit=1, **filters)
        return int(self.conn.execute(sql, params).fetchone()["total"])

    def console_case(self, *, case_id: str) -> dict[str, Any] | None:
        """The one frozen Case behind `?case_id=<id>`. There is at most one: it is the primary key."""

        row = self.conn.execute(CONSOLE_CASE_BY_ID_SQL, {"case_id": str(case_id)}).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result["trigger_id"] is not None:
            decision = self.conn.execute(TRADING_CASE_DECISION_SQL, (case_id,)).fetchone()
            result["analysis_decision"] = dict(decision) if decision is not None else None
            result["analysis_outcomes"] = [
                dict(item)
                for item in self.conn.execute(
                    TRADING_CASE_OUTCOMES_SQL,
                    (case_id,),
                ).fetchall()
            ]
            result["analysis_attempts"] = [
                dict(item) for item in self.conn.execute(TRADING_CASE_ATTEMPTS_SQL, (case_id,)).fetchall()
            ]
            calls = [dict(item) for item in self.conn.execute(TRADING_CASE_MODEL_CALLS_SQL, (case_id,)).fetchall()]
            for attempt in result["analysis_attempts"]:
                attempt["physical_calls"] = [
                    call for call in calls if call["claim_attempt"] == attempt["claim_attempt"]
                ]
            watch = self.conn.execute(TRADING_CASE_WATCH_SQL, (case_id,)).fetchone()
            result["watch_observation"] = dict(watch) if watch is not None else None
            result["root_chain"] = [
                dict(item) for item in self.conn.execute(TRADING_CASE_CHAIN_SQL, (result["trigger_id"],)).fetchall()
            ]
            result["analysis_evaluations"] = [
                dict(item) for item in self.conn.execute(TRADING_CASE_EVALUATIONS_SQL, (case_id,)).fetchall()
            ]
        return result

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
    "TRADING_CASE_DECISION_COUNTS_SQL",
    "TRADING_CASE_LIST_LATEST_SQL",
    "TRADING_GATE_COUNTS_SQL",
    "QueryStorage",
    "console_cases_statement",
    "console_executions_statement",
    "console_operator_intents_statement",
    "console_realized_totals_statement",
    "observation_ledger_statement",
    "signal_ledger_statement",
]
