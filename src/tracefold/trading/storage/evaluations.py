"""Immutable shadow-strategy evaluations and their later point-in-time outcomes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .sql_values import _dumps


class EvaluationStorage:
    conn: Any

    def register_strategy(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        strategy_config_digest: str,
        strategy_config: Mapping[str, Any],
        permission: str,
        now_ms: int,
    ) -> int:
        """Return the immutable first-registration instant for one exact strategy identity."""

        row = self.conn.execute(
            """
            INSERT INTO trading_strategy_registrations (
              strategy_id, strategy_version, strategy_config_digest, strategy_config,
              permission, registered_at_ms
            ) VALUES (%s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (strategy_id, strategy_version, strategy_config_digest) DO UPDATE
               SET strategy_id = EXCLUDED.strategy_id
            RETURNING registered_at_ms, strategy_config, permission
            """,
            (
                strategy_id,
                strategy_version,
                strategy_config_digest,
                _dumps(dict(strategy_config)),
                permission,
                int(now_ms),
            ),
        ).fetchone()
        if row is None:  # pragma: no cover - INSERT .. RETURNING always returns one row
            raise RuntimeError("trading_strategy_registration_missing")
        if dict(row["strategy_config"]) != dict(strategy_config) or str(row["permission"]) != permission:
            raise ValueError("trading_strategy_registration_identity_collision")
        return int(row["registered_at_ms"])

    def strategy_registrations(self) -> dict[tuple[str, str, str], int]:
        rows = self.conn.execute(
            "SELECT strategy_id, strategy_version, strategy_config_digest, registered_at_ms "
            "FROM trading_strategy_registrations"
        ).fetchall()
        return {
            (str(row["strategy_id"]), str(row["strategy_version"]), str(row["strategy_config_digest"])): int(
                row["registered_at_ms"]
            )
            for row in rows
        }

    def strategy_evaluation_identities_since(self, *, cutoff_ms: int) -> list[tuple[str, str, str, str]]:
        rows = self.conn.execute(
            "SELECT trigger_source_key, strategy_id, strategy_version, strategy_config_digest "
            "FROM trading_strategy_evaluations WHERE cutoff_ms >= %s",
            (int(cutoff_ms),),
        ).fetchall()
        return [
            (
                str(row["trigger_source_key"]),
                str(row["strategy_id"]),
                str(row["strategy_version"]),
                str(row["strategy_config_digest"]),
            )
            for row in rows
        ]

    def insert_strategy_evaluation(
        self,
        *,
        evaluation_id: str,
        trigger_source_key: str,
        underlying_key: str,
        trigger_kind: str,
        strategy_id: str,
        strategy_version: str,
        strategy_config_digest: str,
        manifest: Mapping[str, Any],
        manifest_sha256: str,
        decision: str,
        rule: str,
        setup: str,
        invalidation: str,
        expected_horizon: str,
        permission: str,
        strategy_registered_at_ms: int,
        research_partition: str,
        cutoff_ms: int,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT INTO trading_strategy_evaluations (
              evaluation_id, trigger_source_key, underlying_key, trigger_kind,
              strategy_id, strategy_version, strategy_config_digest, manifest, manifest_sha256,
              decision, rule, setup, invalidation, expected_horizon, permission,
              strategy_registered_at_ms, research_partition, cutoff_ms, created_at_ms
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (trigger_source_key, strategy_id, strategy_version, strategy_config_digest)
            DO NOTHING
            """,
            (
                evaluation_id,
                trigger_source_key,
                underlying_key,
                trigger_kind,
                strategy_id,
                strategy_version,
                strategy_config_digest,
                _dumps(dict(manifest)),
                manifest_sha256,
                decision,
                rule,
                setup,
                invalidation,
                expected_horizon,
                permission,
                int(strategy_registered_at_ms),
                research_partition,
                int(cutoff_ms),
                int(now_ms),
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def pending_strategy_outcomes(self, *, before_cutoff_ms: int, limit: int = 32) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT evaluation_id, strategy_id, decision, manifest, cutoff_ms
              FROM trading_strategy_evaluations
             WHERE completed_at_ms IS NULL AND cutoff_ms <= %s
             ORDER BY cutoff_ms, evaluation_id
             LIMIT %s
            """,
            (int(before_cutoff_ms), int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def complete_strategy_outcome(
        self,
        *,
        evaluation_id: str,
        market_outcome: Mapping[str, Any],
        market_outcome_version: str,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE trading_strategy_evaluations
               SET market_outcome = %s::jsonb,
                   market_outcome_version = %s,
                   completed_at_ms = %s
             WHERE evaluation_id = %s AND completed_at_ms IS NULL
            """,
            (_dumps(dict(market_outcome)), market_outcome_version, int(now_ms), evaluation_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def console_strategy_evaluations(self, *, since_ms: int, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT evaluation_id, trigger_source_key, underlying_key, trigger_kind,
                   strategy_id, strategy_version, decision, rule, expected_horizon,
                   permission, strategy_registered_at_ms, research_partition,
                   cutoff_ms, created_at_ms, market_outcome,
                   market_outcome_version, completed_at_ms
              FROM trading_strategy_evaluations
             WHERE created_at_ms >= %s
             ORDER BY created_at_ms DESC, evaluation_id DESC
             LIMIT %s
            """,
            (int(since_ms), int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]


__all__ = ["EvaluationStorage"]
