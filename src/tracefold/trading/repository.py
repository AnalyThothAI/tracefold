"""`trading_*` persistence. Every write is idempotent by key; callers own the transaction.

Same contract as the News repository: this class holds a connection and nothing else — no clock, no
settings, no provider. The database, not a runner's memory, is the authority for the three invariants
that matter, so the interesting methods here are the ones that let a unique index reject the caller
rather than the caller checking first and hoping.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from .models import ACTIVE_ORDER_STATES

_JSON_SEPARATORS = (",", ":")
# 'TRDG' — its own advisory-lock namespace, distinct from News' storyline locks and the app's session locks.
_TRADING_LOCK_NAMESPACE: Final = 0x54524447

_ACTIVE_SQL: Final = ", ".join(f"'{state}'" for state in ACTIVE_ORDER_STATES)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=_JSON_SEPARATORS, default=str)


class TradingRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ locks
    def lock_account(self, account_ref: str) -> None:
        """Transaction-scoped advisory lock per account.

        One account may have exactly one write in flight. The lock serialises "read outstanding state ->
        decide -> insert SUBMITTING" across runners and processes; the partial unique index is what
        makes the invariant true even if a lock is somehow not taken.
        """

        self.conn.execute("SET LOCAL lock_timeout = '2500ms'")
        self.conn.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_TRADING_LOCK_NAMESPACE, f"account:{account_ref}")
        )

    # ------------------------------------------------------------------ blacklist
    def blacklist_rows(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT base_symbol, reason, expires_at_ms FROM trading_symbol_blacklist ORDER BY base_symbol"
        ).fetchall()
        return [dict(row) for row in rows]

    def blacklist_upsert(self, *, base_symbol: str, reason: str, expires_at_ms: int | None, now_ms: int) -> None:
        self.conn.execute(
            """
            INSERT INTO trading_symbol_blacklist (base_symbol, reason, expires_at_ms, created_at_ms, updated_at_ms)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (base_symbol) DO UPDATE
               SET reason = EXCLUDED.reason,
                   expires_at_ms = EXCLUDED.expires_at_ms,
                   updated_at_ms = EXCLUDED.updated_at_ms
            """,
            (base_symbol, reason, expires_at_ms, int(now_ms), int(now_ms)),
        )

    def blacklist_delete(self, *, base_symbol: str) -> int:
        cursor = self.conn.execute("DELETE FROM trading_symbol_blacklist WHERE base_symbol = %s", (base_symbol,))
        return int(getattr(cursor, "rowcount", 0) or 0)

    # ------------------------------------------------------------------ runtime state
    def runtime_state(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT control, day_key, orders_today, dspy_calls_today, funnel, updated_at_ms "
            "FROM trading_runtime_state WHERE id = 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def bump_dspy_calls(self, *, day_key: str, now_ms: int) -> int:
        row = self.conn.execute(
            """
            UPDATE trading_runtime_state
               SET dspy_calls_today = CASE WHEN day_key = %s THEN dspy_calls_today + 1 ELSE 1 END,
                   orders_today = CASE WHEN day_key = %s THEN orders_today ELSE 0 END,
                   day_key = %s,
                   updated_at_ms = %s
             WHERE id = 1
         RETURNING dspy_calls_today
            """,
            (day_key, day_key, day_key, int(now_ms)),
        ).fetchone()
        return int(row["dspy_calls_today"]) if row is not None else 0

    def dspy_calls_today(self, *, day_key: str) -> int:
        row = self.conn.execute("SELECT day_key, dspy_calls_today FROM trading_runtime_state WHERE id = 1").fetchone()
        if row is None or str(row["day_key"]) != day_key:
            return 0
        return int(row["dspy_calls_today"])

    def merge_funnel(self, *, day_key: str, counts: Mapping[str, int], now_ms: int) -> None:
        """Accumulate one turn's named rejections into the day's funnel.

        The funnel is the PR-B deliverable ("OI raw -> ... -> paper order"), and it has to survive a
        deploy: an in-memory counter resets exactly when someone is trying to read it. The day key
        resets the document, so the row stays one bounded object rather than growing forever.
        """

        if not counts:
            return
        payload = {str(key): int(value) for key, value in counts.items()}
        self.conn.execute(
            """
            UPDATE trading_runtime_state
               SET funnel = (
                     SELECT coalesce(jsonb_object_agg(k, v), '{}'::jsonb)
                       FROM (
                         SELECT k,
                                sum(v)::bigint AS v
                           FROM (
                             SELECT key AS k, value::text::bigint AS v
                               FROM jsonb_each(CASE WHEN day_key = %s THEN funnel ELSE '{}'::jsonb END)
                             UNION ALL
                             SELECT key AS k, value::text::bigint AS v FROM jsonb_each(%s::jsonb)
                           ) merged
                          GROUP BY k
                       ) totals
                   ),
                   day_key = %s,
                   orders_today = CASE WHEN day_key = %s THEN orders_today ELSE 0 END,
                   dspy_calls_today = CASE WHEN day_key = %s THEN dspy_calls_today ELSE 0 END,
                   updated_at_ms = %s
             WHERE id = 1
            """,
            (day_key, _dumps(payload), day_key, day_key, day_key, int(now_ms)),
        )

    def set_control(self, *, control: str, now_ms: int) -> None:
        self.conn.execute(
            "UPDATE trading_runtime_state SET control = %s, updated_at_ms = %s WHERE id = 1",
            (control, int(now_ms)),
        )

    def bump_orders_today(self, *, day_key: str, now_ms: int) -> int:
        """Increment the daily counter, resetting it when the UTC day rolls. Returns the new count."""

        row = self.conn.execute(
            """
            UPDATE trading_runtime_state
               SET orders_today = CASE WHEN day_key = %s THEN orders_today + 1 ELSE 1 END,
                   day_key = %s,
                   updated_at_ms = %s
             WHERE id = 1
         RETURNING orders_today
            """,
            (day_key, day_key, int(now_ms)),
        ).fetchone()
        return int(row["orders_today"]) if row is not None else 0

    def orders_today(self, *, day_key: str) -> int:
        row = self.conn.execute("SELECT day_key, orders_today FROM trading_runtime_state WHERE id = 1").fetchone()
        if row is None or str(row["day_key"]) != day_key:
            return 0
        return int(row["orders_today"])

    # ------------------------------------------------------------------ cases
    def insert_case(
        self,
        *,
        case_id: str,
        underlying_key: str,
        case_kind: str,
        mode: str,
        primary_source_key: str,
        supplemental_source_keys: Sequence[str],
        manifest: Mapping[str, Any],
        manifest_sha256: str,
        regime: str | None,
        observed_at_ms: int,
        now_ms: int,
    ) -> bool:
        """One source fact, one case. A re-scanned window is a no-op, not a duplicate."""

        cursor = self.conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, case_kind, mode, primary_source_key, supplemental_source_keys,
              manifest, manifest_sha256, state, regime, observed_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, 'PENDING', %s, %s, %s, %s)
            ON CONFLICT (primary_source_key) DO NOTHING
            """,
            (
                case_id,
                underlying_key,
                case_kind,
                mode,
                primary_source_key,
                _dumps(list(supplemental_source_keys)),
                _dumps(dict(manifest)),
                manifest_sha256,
                regime,
                int(observed_at_ms),
                int(now_ms),
                int(now_ms),
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def claim_case(self, *, run_id: str, lease_ms: int, now_ms: int) -> dict[str, Any] | None:
        """Take the oldest claimable case under a short lease.

        A `RUNNING` case whose lease expired may be reclaimed: re-running an undecided case is safe.
        Re-sending a prepared or submitted economic intent is not, and the proposal/order uniqueness —
        not the lease — is what prevents that.
        """

        row = self.conn.execute(
            """
            UPDATE trading_cases
               SET state = 'RUNNING',
                   run_id = %s,
                   lease_expires_at_ms = %s,
                   attempt_count = attempt_count + 1,
                   updated_at_ms = %s
             WHERE case_id = (
                     SELECT case_id FROM trading_cases
                      WHERE state = 'PENDING'
                         OR (state = 'RUNNING' AND coalesce(lease_expires_at_ms, 0) < %s)
                      ORDER BY created_at_ms, case_id
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1
                   )
         RETURNING *
            """,
            (run_id, int(now_ms) + int(lease_ms), int(now_ms), int(now_ms)),
        ).fetchone()
        return dict(row) if row is not None else None

    def settle_case(
        self,
        *,
        case_id: str,
        run_id: str,
        state: str,
        policy_decision: str | None,
        policy_reason: str | None,
        program_version: str | None = None,
        program_sha256: str | None = None,
        program_output: Mapping[str, Any] | None = None,
        regime: str | None = None,
        now_ms: int,
    ) -> bool:
        """Terminalise a claimed case. A stale `run_id` cannot write: its lease belongs to someone else."""

        cursor = self.conn.execute(
            """
            UPDATE trading_cases
               SET state = %s,
                   policy_decision = %s,
                   policy_reason = %s,
                   program_version = coalesce(%s, program_version),
                   program_sha256 = coalesce(%s, program_sha256),
                   program_output = coalesce(%s::jsonb, program_output),
                   regime = coalesce(%s, regime),
                   decided_at_ms = %s,
                   updated_at_ms = %s
             WHERE case_id = %s AND run_id = %s
            """,
            (
                state,
                policy_decision,
                policy_reason,
                program_version,
                program_sha256,
                None if program_output is None else _dumps(dict(program_output)),
                regime,
                int(now_ms),
                int(now_ms),
                case_id,
                run_id,
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def case(self, *, case_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM trading_cases WHERE case_id = %s", (case_id,)).fetchone()
        return dict(row) if row is not None else None

    def cases(self, *, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if state:
            rows = self.conn.execute(
                "SELECT * FROM trading_cases WHERE state = %s ORDER BY created_at_ms DESC LIMIT %s",
                (state, int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM trading_cases ORDER BY created_at_ms DESC LIMIT %s", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def has_source_key(self, *, primary_source_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM trading_cases WHERE primary_source_key = %s LIMIT 1", (primary_source_key,)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ orders
    def active_underlyings(self) -> list[str]:
        rows = self.conn.execute(
            f"SELECT DISTINCT underlying_key FROM trading_orders WHERE state IN ({_ACTIVE_SQL})"
        ).fetchall()
        return [str(row["underlying_key"]) for row in rows]

    def last_close_at_ms(self, *, underlying_key: str) -> int | None:
        row = self.conn.execute(
            "SELECT max(closed_at_ms) AS closed_at_ms FROM trading_orders WHERE underlying_key = %s",
            (underlying_key,),
        ).fetchone()
        value = None if row is None else row["closed_at_ms"]
        return None if value is None else int(value)

    def insert_prepared_order(
        self,
        *,
        order_id: str,
        case_id: str,
        underlying_key: str,
        exchange_id: str,
        provider_symbol: str,
        account_ref: str,
        mode: str,
        side: str,
        notional_usd: str,
        quantity: str,
        entry_reference: str,
        stop_price: str,
        take_profit_price: str | None,
        payload: Mapping[str, Any],
        payload_sha256: str,
        state: str,
        must_close_at_ms: int | None,
        now_ms: int,
    ) -> bool:
        """Freeze the intent. The partial unique index is the authority on "one active per underlying"."""

        cursor = self.conn.execute(
            """
            INSERT INTO trading_orders (
              order_id, case_id, underlying_key, exchange_id, provider_symbol, account_ref, mode, side,
              notional_usd, quantity, entry_reference, stop_price, take_profit_price, payload,
              payload_sha256, state, must_close_at_ms, next_reconcile_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (case_id) DO NOTHING
            """,
            (
                order_id,
                case_id,
                underlying_key,
                exchange_id,
                provider_symbol,
                account_ref,
                mode,
                side,
                notional_usd,
                quantity,
                entry_reference,
                stop_price,
                take_profit_price,
                _dumps(dict(payload)),
                payload_sha256,
                state,
                must_close_at_ms,
                int(now_ms),
                int(now_ms),
                int(now_ms),
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def mark_submitting(self, *, order_id: str, now_ms: int) -> bool:
        """Record the attempt *before* the network call, and only ever once.

        The `provider_attempt_count = 0` predicate plus the schema CHECK is what makes a second write
        unrecordable: a caller that somehow tried again would find zero rows updated and must not call.
        """

        cursor = self.conn.execute(
            """
            UPDATE trading_orders
               SET state = 'SUBMITTING',
                   provider_attempt_count = provider_attempt_count + 1,
                   updated_at_ms = %s
             WHERE order_id = %s
               AND provider_attempt_count = 0
               AND state IN ('PREPARED', 'APPROVED')
            """,
            (int(now_ms), order_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def update_order(
        self,
        *,
        order_id: str,
        state: str,
        state_reason: str | None = None,
        remote_order_id: str | None = None,
        filled_quantity: str | None = None,
        average_price: str | None = None,
        exit_price: str | None = None,
        exit_reason: str | None = None,
        realized_bps: int | None = None,
        position_opened_at_ms: int | None = None,
        must_close_at_ms: int | None = None,
        next_reconcile_at_ms: int | None = None,
        closed_at_ms: int | None = None,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE trading_orders
               SET state = %s,
                   state_reason = coalesce(%s, state_reason),
                   remote_order_id = coalesce(%s, remote_order_id),
                   filled_quantity = coalesce(%s, filled_quantity),
                   average_price = coalesce(%s, average_price),
                   exit_price = coalesce(%s, exit_price),
                   exit_reason = coalesce(%s, exit_reason),
                   realized_bps = coalesce(%s, realized_bps),
                   position_opened_at_ms = coalesce(%s, position_opened_at_ms),
                   must_close_at_ms = coalesce(%s, must_close_at_ms),
                   next_reconcile_at_ms = %s,
                   closed_at_ms = coalesce(%s, closed_at_ms),
                   updated_at_ms = %s
             WHERE order_id = %s
            """,
            (
                state,
                state_reason,
                remote_order_id,
                filled_quantity,
                average_price,
                exit_price,
                exit_reason,
                realized_bps,
                position_opened_at_ms,
                must_close_at_ms,
                next_reconcile_at_ms,
                closed_at_ms,
                int(now_ms),
                order_id,
            ),
        )

    def order(self, *, order_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM trading_orders WHERE order_id = %s", (order_id,)).fetchone()
        return dict(row) if row is not None else None

    def order_for_case(self, *, case_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM trading_orders WHERE case_id = %s", (case_id,)).fetchone()
        return dict(row) if row is not None else None

    def due_orders(self, *, now_ms: int, limit: int = 32) -> list[dict[str, Any]]:
        """Live orders whose next reconcile is due, oldest first."""

        rows = self.conn.execute(
            f"""
            SELECT * FROM trading_orders
             WHERE state IN ({_ACTIVE_SQL})
               AND coalesce(next_reconcile_at_ms, 0) <= %s
             ORDER BY coalesce(next_reconcile_at_ms, 0), created_at_ms
             LIMIT %s
            """,
            (int(now_ms), int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def approve_order(self, *, order_id: str, payload_sha256: str, now_ms: int) -> bool:
        """Operator approval, bound to the exact frozen payload digest and idempotent by state."""

        cursor = self.conn.execute(
            """
            UPDATE trading_orders
               SET state = 'APPROVED', updated_at_ms = %s
             WHERE order_id = %s AND payload_sha256 = %s AND state = 'AWAITING_APPROVAL'
            """,
            (int(now_ms), order_id, payload_sha256),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def reject_order(self, *, order_id: str, payload_sha256: str, reason: str, now_ms: int) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE trading_orders
               SET state = 'REJECTED_BY_OPERATOR', state_reason = %s, closed_at_ms = %s, updated_at_ms = %s
             WHERE order_id = %s AND payload_sha256 = %s AND state = 'AWAITING_APPROVAL'
            """,
            (reason, int(now_ms), int(now_ms), order_id, payload_sha256),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

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
    def status_counts(self, *, since_ms: int) -> dict[str, Any]:
        cases = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY state",
            (int(since_ms),),
        ).fetchall()
        orders = self.conn.execute(
            "SELECT state, count(*) AS n FROM trading_orders WHERE created_at_ms >= %s GROUP BY state",
            (int(since_ms),),
        ).fetchall()
        kinds = self.conn.execute(
            "SELECT case_kind, count(*) AS n FROM trading_cases WHERE created_at_ms >= %s GROUP BY case_kind",
            (int(since_ms),),
        ).fetchall()
        realized = self.conn.execute(
            "SELECT count(*) AS n, coalesce(sum(realized_bps), 0) AS total_bps "
            "FROM trading_orders WHERE closed_at_ms IS NOT NULL AND closed_at_ms >= %s",
            (int(since_ms),),
        ).fetchone()
        return {
            "cases_by_state": {str(row["state"]): int(row["n"]) for row in cases},
            "cases_by_kind": {str(row["case_kind"]): int(row["n"]) for row in kinds},
            "orders_by_state": {str(row["state"]): int(row["n"]) for row in orders},
            "closed_orders": 0 if realized is None else int(realized["n"]),
            "closed_realized_bps": 0 if realized is None else int(realized["total_bps"]),
        }


__all__ = ["TradingRepository"]
