"""Bounded read projections for Cases, Signals, Observations, and readiness."""

from __future__ import annotations

import re
from typing import Any

from tracefold.platform.postgres.client import require_transaction

from .query_sql import TRADING_STATUS_CASE_COUNTS_SQL, TRADING_STATUS_SIGNAL_COUNTS_SQL


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
        rows = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY state",
            (int(since_ms),),
        ).fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    def case_reason_counts(self, *, since_ms: int) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT coalesce(policy_reason, 'undecided') AS reason, count(*) AS n "
            "FROM trading_cases WHERE created_at_ms >= %s GROUP BY reason",
            (int(since_ms),),
        ).fetchall()
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
        rows = self.conn.execute(
            f"""
            SELECT case_id, underlying_key, trigger_kind, primary_source_key, manifest,
                   manifest_sha256, state, policy_decision, policy_reason, policy_checks,
                   observed_at_ms, created_at_ms AS case_created_at_ms, decided_at_ms,
                   strategy_id, strategy_version, strategy_config_digest
              FROM trading_cases
             WHERE {" AND ".join(predicates)}
             ORDER BY created_at_ms DESC, case_id DESC
             LIMIT %(limit)s
            """,  # noqa: S608 -- predicates are fixed fragments; all values remain bound
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def console_signals(
        self,
        *,
        since_ns: int,
        market_key: str | None,
        before: tuple[int, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        predicates = ["observed_at_ns >= %(since)s"]
        params: dict[str, Any] = {"since": int(since_ns), "limit": int(limit)}
        if market_key is not None:
            predicates.append("market_key = %(market_key)s")
            params["market_key"] = market_key
        if before is not None:
            predicates.append("(observed_at_ns, signal_id) < (%(before_ns)s, %(before_id)s)")
            params["before_ns"], params["before_id"] = before
        rows = self.conn.execute(
            f"""
            SELECT seq, signal_id, case_id, alpha_contract_sha256, market_key, direction,
                   observed_at_ns, expires_at_ns, evidence_sha256, alpha_metadata
              FROM trading_trade_signals
             WHERE {" AND ".join(predicates)}
             ORDER BY observed_at_ns DESC, signal_id DESC
             LIMIT %(limit)s
            """,  # noqa: S608 -- predicates are fixed fragments; all values remain bound
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def console_execution_observations(
        self,
        *,
        since_ns: int,
        runtime_profile_id: str | None,
        normalized_kind: str | None,
        before: tuple[int, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        predicates = ["observed_at_ns >= %(since)s"]
        params: dict[str, Any] = {"since": int(since_ns), "limit": int(limit)}
        if runtime_profile_id is not None:
            predicates.append("runtime_profile_id = %(profile)s")
            params["profile"] = runtime_profile_id
        if normalized_kind is not None:
            predicates.append("normalized_kind = %(kind)s")
            params["kind"] = normalized_kind
        if before is not None:
            predicates.append("(observed_at_ns, event_id) < (%(before_ns)s, %(before_id)s)")
            params["before_ns"], params["before_id"] = before
        rows = self.conn.execute(
            f"""
            SELECT seq, event_id, runtime_profile_id, runtime_release, execution_strategy,
                   signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                   native_identity_references, summary, payload_digest
              FROM trading_execution_observations
             WHERE {" AND ".join(predicates)}
             ORDER BY observed_at_ns DESC, event_id DESC
             LIMIT %(limit)s
            """,  # noqa: S608 -- predicates are fixed fragments; all values remain bound
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def console_operator_intents(
        self,
        *,
        since_ns: int,
        runtime_profile_id: str | None,
        action: str | None,
        before: tuple[int, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        predicates = ["command.requested_at_ns >= %(since)s"]
        params: dict[str, Any] = {"since": int(since_ns), "limit": int(limit)}
        if runtime_profile_id is not None:
            predicates.append("command.target_profile_id = %(profile)s")
            params["profile"] = runtime_profile_id
        if action is not None:
            predicates.append("command.action = %(action)s")
            params["action"] = action
        if before is not None:
            predicates.append("(command.requested_at_ns, command.command_id) < (%(before_ns)s, %(before_id)s)")
            params["before_ns"], params["before_id"] = before
        rows = self.conn.execute(
            f"""
            SELECT command.seq, command.command_id, command.target_profile_id, command.action,
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
            """,  # noqa: S608 -- predicates are fixed fragments; all values remain bound
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def next_execution_notification(self, target_sha256: str) -> dict[str, Any] | None:
        """Read the first notifiable observation without creating a mutable delivery cursor."""

        if re.fullmatch(r"[0-9a-f]{64}", target_sha256) is None:
            raise ValueError("execution_notification_target_invalid")
        row = self.conn.execute(
            """
            WITH delivered AS (
              SELECT COALESCE(max(observation_seq), 0) AS watermark
                FROM trading_execution_notification_deliveries
               WHERE target_sha256 = %s
            )
            SELECT observation.seq, observation.event_id, observation.runtime_profile_id,
                   observation.runtime_release, observation.execution_strategy,
                   observation.signal_id, observation.command_id, observation.normalized_kind,
                   observation.occurred_at_ns, observation.observed_at_ns,
                   observation.native_identity_references, observation.summary,
                   observation.payload_digest
              FROM trading_execution_observations observation
              CROSS JOIN delivered
             WHERE observation.seq > delivered.watermark
               AND observation.normalized_kind IN (
                     'signal_disposition', 'control_disposition', 'fill', 'audit_gap',
                     'readiness', 'order', 'reconciliation'
                   )
               AND (
                     observation.normalized_kind IN ('signal_disposition', 'control_disposition', 'fill', 'audit_gap')
                  OR (
                       observation.normalized_kind = 'readiness'
                       AND observation.summary ->> 'control_stage' = 'runtime_accepted'
                     )
                  OR (
                       observation.normalized_kind = 'order'
                       AND observation.summary ->> 'status' IN (
                         'accepted', 'rejected', 'denied', 'expired', 'submitted', 'submitted_or_unknown'
                       )
                     )
                  OR (
                       observation.normalized_kind = 'reconciliation'
                       AND observation.summary ->> 'state' = 'flat'
                     )
                   )
             ORDER BY observation.seq
             LIMIT 1
            """,
            (target_sha256,),
        ).fetchone()
        return None if row is None else dict(row)

    def append_execution_notification_delivery(
        self,
        *,
        target_sha256: str,
        observation_seq: int,
        message_id: int,
        delivered_at_ns: int,
    ) -> dict[str, Any]:
        """Append one delivery receipt; retries never mutate an earlier receipt."""

        require_transaction(self.conn, operation="append_execution_notification_delivery")
        if re.fullmatch(r"[0-9a-f]{64}", target_sha256) is None:
            raise ValueError("execution_notification_target_invalid")
        if observation_seq <= 0:
            raise ValueError("execution_notification_observation_invalid")
        if isinstance(message_id, bool) or message_id <= 0 or delivered_at_ns <= 0:
            raise ValueError("execution_notification_delivery_invalid")
        existing = self.conn.execute(
            """
            SELECT target_sha256, observation_seq, message_id, delivered_at_ns
              FROM trading_execution_notification_deliveries
             WHERE target_sha256 = %s AND observation_seq = %s
            """,
            (target_sha256, observation_seq),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        expected = self.next_execution_notification(target_sha256)
        if expected is None or int(expected["seq"]) != observation_seq:
            raise ValueError("execution_notification_delivery_out_of_order")
        row = self.conn.execute(
            """
            INSERT INTO trading_execution_notification_deliveries (
              target_sha256, observation_seq, message_id, delivered_at_ns
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (target_sha256, observation_seq) DO NOTHING
            RETURNING target_sha256, observation_seq, message_id, delivered_at_ns
            """,
            (target_sha256, observation_seq, message_id, delivered_at_ns),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                """
                SELECT target_sha256, observation_seq, message_id, delivered_at_ns
                  FROM trading_execution_notification_deliveries
                 WHERE target_sha256 = %s AND observation_seq = %s
                """,
                (target_sha256, observation_seq),
            ).fetchone()
        if row is None:
            raise RuntimeError("execution_notification_delivery_missing")
        return dict(row)


__all__ = ["QueryStorage"]
