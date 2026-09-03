"""Bounded read projections for Cases, Signals, Observations, and readiness.

Each console page is one statement builder plus the method that runs it. The query-plan audit calls the
same builder with representative predicates, so what it EXPLAINs is the statement the route executes
rather than a copy of it that an edit can leave behind (`docs/MIGRATIONS.md`, database standard 3).
"""

from __future__ import annotations

import re
from typing import Any

from tracefold.platform.postgres.client import require_transaction
from tracefold.trading.notification_policy import (
    COALESCED_KINDS,
    NOTIFICATION_THROTTLE_MS,
    notifiable_policy_rows,
)

# Keyed on `created_at_ms`: when the Case formed, which is what "the lane produced N cases today"
# means. The admission ledger's own counts key on `source_observed_at_ms` instead, so a restarted
# runner re-reading a backlog cannot move yesterday's frames into today's total; a Case is created
# once and has no such backlog.
TRADING_STATUS_CASE_COUNTS_SQL = """
    SELECT
      count(*) FILTER (WHERE created_at_ms >= %(since)s) AS cases_24h,
      count(*) FILTER (WHERE created_at_ms >= %(since)s AND state = 'SIGNAL_EMITTED') AS signals_24h,
      count(*) FILTER (WHERE created_at_ms >= %(since)s AND state = 'NO_TRADE') AS no_trade_24h,
      count(*) FILTER (WHERE created_at_ms >= %(since)s AND state = 'BLOCKED') AS blocked_24h,
      count(*) FILTER (WHERE state IN ('PENDING', 'RUNNING')) AS cases_open
    FROM trading_cases
    WHERE created_at_ms >= %(since)s OR state IN ('PENDING', 'RUNNING')
"""
TRADING_STATUS_SIGNAL_COUNTS_SQL = """
    SELECT count(*) FILTER (WHERE observed_at_ns >= %(since)s) AS signals_24h,
           count(*) FILTER (WHERE expires_at_ns > %(now)s) AS signals_unexpired
      FROM trading_trade_signals
     WHERE observed_at_ns >= %(since)s OR expires_at_ns > %(now)s
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
        SELECT seq, signal_id, case_id, alpha_contract_sha256, market_key, direction,
               observed_at_ns, expires_at_ns, evidence_sha256, alpha_metadata
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
               native_identity_references, summary, payload_digest
          FROM trading_execution_observations
         WHERE {" AND ".join(predicates)}
         ORDER BY observed_at_ns DESC, event_id DESC
         LIMIT %(limit)s
    """  # noqa: S608 -- predicates are fixed fragments; all values remain bound
    return sql, params


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
               command.confirmation_identity IS NOT NULL AS confirmed,
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

    def runtime_summary(self, *, since_ms: int, now_ms: int) -> dict[str, Any]:
        row = self.conn.execute(
            TRADING_STATUS_CASE_COUNTS_SQL,
            {"since": int(since_ms)},
        ).fetchone()
        signal_row = self.conn.execute(
            TRADING_STATUS_SIGNAL_COUNTS_SQL,
            {"since": int(since_ms) * 1_000_000, "now": int(now_ms) * 1_000_000},
        ).fetchone()
        values = dict(row or {})
        values["signals_24h"] = int((signal_row or {}).get("signals_24h") or 0)
        values["signals_unexpired"] = int((signal_row or {}).get("signals_unexpired") or 0)
        return {key: int(value or 0) for key, value in values.items()}

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

    def next_execution_notification(self, target_sha256: str, *, now_ns: int) -> dict[str, Any] | None:
        """Read the next notifiable observation without creating a mutable delivery cursor.

        The watermark is per kind: a single watermark over all kinds silently skips any observation
        whose sequence falls below a later delivery, so one out-of-order turn drops events for good.
        A kind can only ever skip its own past.

        `tracefold.trading.notification_policy` owns both halves of the choice: which observation is
        worth a card, and — for the kinds that arrive on a timer — that only the newest pending one
        is a candidate and only once per throttle window. `now_ns` is a parameter rather than a clock
        read here so the delivery guard can re-derive the same candidate the caller acted on.
        """

        if re.fullmatch(r"[0-9a-f]{64}", target_sha256) is None:
            raise ValueError("execution_notification_target_invalid")
        if now_ns <= 0:
            raise ValueError("execution_notification_clock_invalid")
        notify_kinds, notify_keys, notify_values = notifiable_policy_rows()
        params = {
            "target": target_sha256,
            "notify_kinds": notify_kinds,
            "notify_keys": notify_keys,
            "notify_values": notify_values,
            "coalesced": list(COALESCED_KINDS),
            "throttle_ns": NOTIFICATION_THROTTLE_MS * 1_000_000,
            "now_ns": now_ns,
        }
        row = self.conn.execute(
            """
            WITH delivered AS (
              SELECT sent.normalized_kind AS kind,
                     max(delivery.observation_seq) AS watermark,
                     max(delivery.delivered_at_ns) AS last_delivered_at_ns
                FROM trading_execution_notification_deliveries delivery
                JOIN trading_execution_observations sent ON sent.seq = delivery.observation_seq
               WHERE delivery.target_sha256 = %(target)s
               GROUP BY 1
            ),
            candidate AS (
              SELECT observation.seq, observation.event_id, observation.account_slot,
                     observation.runtime_release, observation.execution_strategy,
                     observation.signal_id, observation.command_id, observation.normalized_kind,
                     observation.occurred_at_ns, observation.observed_at_ns,
                     observation.native_identity_references, observation.summary,
                     observation.payload_digest,
                     -- The Case a Signal card states its reasons from. Read here rather than in a
                     -- second round trip so the rendered text is a pure function of one row, and
                     -- LEFT so a non-Signal observation is still notifiable.
                     signal.case_id, signal.market_key, signal.direction,
                     signal.observed_at_ns AS signal_observed_at_ns,
                     trading_case.policy_decision, trading_case.policy_reason,
                     trading_case.policy_checks, trading_case.manifest,
                     -- The newest pending observation of this kind, so a coalesced kind reports the
                     -- state the account is in now rather than one it has already left.
                     max(observation.seq) OVER (PARTITION BY observation.normalized_kind) AS newest_seq,
                     COALESCE(delivered.last_delivered_at_ns, 0) AS last_delivered_at_ns
                FROM trading_execution_observations observation
                LEFT JOIN delivered ON delivered.kind = observation.normalized_kind
                LEFT JOIN trading_trade_signals signal
                       ON observation.normalized_kind = 'signal_disposition'
                      AND signal.signal_id = observation.signal_id
                LEFT JOIN trading_cases trading_case ON trading_case.case_id = signal.case_id
               WHERE observation.seq > COALESCE(delivered.watermark, 0)
                 AND EXISTS (
                       SELECT 1
                         FROM unnest(
                                %(notify_kinds)s::text[],
                                %(notify_keys)s::text[],
                                %(notify_values)s::text[]
                              ) AS policy(kind, summary_key, summary_value)
                        WHERE policy.kind = observation.normalized_kind
                          AND (
                                policy.summary_key = ''
                             OR observation.summary ->> policy.summary_key = policy.summary_value
                              )
                     )
            )
            SELECT seq, event_id, account_slot, runtime_release, execution_strategy,
                   signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                   native_identity_references, summary, payload_digest,
                   case_id, market_key, direction, signal_observed_at_ns,
                   policy_decision, policy_reason, policy_checks, manifest
              FROM candidate
             WHERE NOT (normalized_kind = ANY(%(coalesced)s))
                OR (seq = newest_seq AND last_delivered_at_ns + %(throttle_ns)s <= %(now_ns)s)
             ORDER BY seq
             LIMIT 1
            """,
            params,
        ).fetchone()
        return None if row is None else dict(row)

    def append_execution_notification_delivery(
        self,
        *,
        target_sha256: str,
        observation_seq: int,
        message_id: int | None,
        delivered_at_ns: int,
        selected_at_ns: int,
    ) -> dict[str, Any]:
        """Append one delivery receipt; retries never mutate an earlier receipt.

        `message_id` is optional because a Feishu custom-bot webhook returns none. The receipt still
        records that this observation reached this target at this instant, which is what
        the watermark and the coverage measure read; only a channel that can address a sent message
        again has an id worth storing.

        `selected_at_ns` is the clock the caller chose this observation with, not the clock it was
        delivered at. The two differ by however long the send took, and a throttle window that
        expires in between would otherwise let the guard pick a different candidate than the one that
        was actually sent.
        """

        require_transaction(self.conn, operation="append_execution_notification_delivery")
        if re.fullmatch(r"[0-9a-f]{64}", target_sha256) is None:
            raise ValueError("execution_notification_target_invalid")
        if observation_seq <= 0:
            raise ValueError("execution_notification_observation_invalid")
        if message_id is not None and (isinstance(message_id, bool) or message_id <= 0):
            raise ValueError("execution_notification_delivery_invalid")
        if delivered_at_ns <= 0 or selected_at_ns <= 0:
            raise ValueError("execution_notification_delivery_invalid")
        existing = self.conn.execute(
            """
            SELECT target_sha256, observation_seq, message_id, delivered_at_ns, result_delivered_at_ns
              FROM trading_execution_notification_deliveries
             WHERE target_sha256 = %s AND observation_seq = %s
            """,
            (target_sha256, observation_seq),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        expected = self.next_execution_notification(target_sha256, now_ns=selected_at_ns)
        if expected is None or int(expected["seq"]) != observation_seq:
            raise ValueError("execution_notification_delivery_out_of_order")
        row = self.conn.execute(
            """
            INSERT INTO trading_execution_notification_deliveries (
              target_sha256, observation_seq, message_id, delivered_at_ns
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (target_sha256, observation_seq) DO NOTHING
            RETURNING target_sha256, observation_seq, message_id, delivered_at_ns, result_delivered_at_ns
            """,
            (target_sha256, observation_seq, message_id, delivered_at_ns),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                """
                SELECT target_sha256, observation_seq, message_id, delivered_at_ns, result_delivered_at_ns
                  FROM trading_execution_notification_deliveries
                 WHERE target_sha256 = %s AND observation_seq = %s
                """,
                (target_sha256, observation_seq),
            ).fetchone()
        if row is None:
            raise RuntimeError("execution_notification_delivery_missing")
        return dict(row)

    def next_execution_notification_result(
        self, target_sha256: str, *, due_at_or_before_ns: int
    ) -> dict[str, Any] | None:
        """The oldest delivered Signal card whose four-hour outcome is due and not yet sent.

        Only `signal_disposition` receipts have an outcome to report: the other observation kinds are
        stages, not positions. `due_at_or_before_ns` is the caller's clock minus the holding period
        plus a settling margin, so the decision about *when* a result is due stays with the worker and
        this read stays a pure function of the row and that instant.
        """

        if re.fullmatch(r"[0-9a-f]{64}", target_sha256) is None:
            raise ValueError("execution_notification_target_invalid")
        row = self.conn.execute(
            """
            SELECT delivery.observation_seq, delivery.delivered_at_ns,
                   signal.signal_id, signal.case_id, signal.market_key, signal.direction,
                   signal.observed_at_ns AS signal_observed_at_ns,
                   trading_case.manifest
              FROM trading_execution_notification_deliveries delivery
              JOIN trading_execution_observations observation
                ON observation.seq = delivery.observation_seq
              JOIN trading_trade_signals signal ON signal.signal_id = observation.signal_id
              JOIN trading_cases trading_case ON trading_case.case_id = signal.case_id
             WHERE delivery.target_sha256 = %s
               AND delivery.result_delivered_at_ns IS NULL
               AND observation.normalized_kind = 'signal_disposition'
               AND delivery.delivered_at_ns <= %s
             ORDER BY delivery.observation_seq
             LIMIT 1
            """,
            (target_sha256, int(due_at_or_before_ns)),
        ).fetchone()
        return None if row is None else dict(row)

    def mark_execution_notification_result(
        self, *, target_sha256: str, observation_seq: int, result_delivered_at_ns: int
    ) -> bool:
        """Record that the outcome message went out. Never overwrites an earlier one."""

        require_transaction(self.conn, operation="mark_execution_notification_result")
        if re.fullmatch(r"[0-9a-f]{64}", target_sha256) is None:
            raise ValueError("execution_notification_target_invalid")
        if observation_seq <= 0 or result_delivered_at_ns <= 0:
            raise ValueError("execution_notification_result_invalid")
        row = self.conn.execute(
            """
            UPDATE trading_execution_notification_deliveries
               SET result_delivered_at_ns = %s
             WHERE target_sha256 = %s AND observation_seq = %s
               AND result_delivered_at_ns IS NULL
             RETURNING observation_seq
            """,
            (int(result_delivered_at_ns), target_sha256, int(observation_seq)),
        ).fetchone()
        return row is not None


__all__ = [
    "TRADING_CASE_COUNTS_SQL",
    "TRADING_CASE_REASON_COUNTS_SQL",
    "TRADING_STATUS_CASE_COUNTS_SQL",
    "TRADING_STATUS_SIGNAL_COUNTS_SQL",
    "QueryStorage",
    "console_cases_statement",
    "console_execution_observations_statement",
    "console_operator_intents_statement",
    "console_signals_statement",
]
