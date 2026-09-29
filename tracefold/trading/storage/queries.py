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
    "cost_unknown_reason,settled,final_manifest_ref,termination_reason "
    "FROM trading_case_attempts WHERE case_id=%s "
    "ORDER BY claim_attempt DESC LIMIT 128"
)
TRADING_CASE_MODEL_CALLS_SQL = (
    "SELECT claim_attempt,call_index,status,started_at_ms,finished_at_ms,timeout_ms,"
    "remaining_deadline_ms,reserved_cost_microusd,request_ref,response_ref,input_tokens,"
    "output_tokens,cost_microusd,cost_unknown_reason,phase,endpoint,requested_model,served_model "
    "FROM trading_model_calls "
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
        "(trigger_id IS NULL OR run_kind IS NULL OR run_kind='initial')",
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
        return result


__all__ = [
    "CONSOLE_CASE_BY_ID_SQL",
    "TRADING_CASE_COUNTS_SQL",
    "TRADING_CASE_DECISION_COUNTS_SQL",
    "TRADING_CASE_LIST_LATEST_SQL",
    "TRADING_GATE_COUNTS_SQL",
    "QueryStorage",
    "console_cases_statement",
]
