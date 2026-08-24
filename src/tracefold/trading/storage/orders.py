"""Persistence for the Trading order lifecycle and capital-write invariants."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from ..contracts import ACTIVE_ORDER_STATES, TRADING_LIVE_APPROVAL_MARKER, TRADING_LIVE_APPROVAL_TTL_MS
from .sql_values import _dumps

_ACTIVE_SQL: Final = ", ".join(f"'{state}'" for state in ACTIVE_ORDER_STATES)
# Mirrored in the schema CHECK and in the runner. The exit gets a bounded retry rather than the
# entry's single irrevocable shot, because a read can prove a close did not take effect.
_MAX_EXIT_ATTEMPTS: Final = 3


class OrderStorage:
    conn: Any

    def active_underlyings(self) -> list[str]:
        rows = self.conn.execute(
            f"SELECT DISTINCT underlying_key FROM trading_orders WHERE state IN ({_ACTIVE_SQL})"
        ).fetchall()
        return [str(row["underlying_key"]) for row in rows]

    def last_close_at_ms(self, *, underlying_key: str) -> int | None:
        """The last time a *position* actually closed. Four paths write `closed_at_ms`; only one is an exit.

        Keying the cooldown on row terminalisation made a transient venue rejection — which never held
        exposure — block the symbol for the full cooldown.
        """

        row = self.conn.execute(
            "SELECT max(position_closed_at_ms) AS closed_at_ms FROM trading_orders WHERE underlying_key = %s",
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

    def claim_attempt(self, *, order_id: str, kind: str, now_ms: int) -> str:
        """Record one capital write *before* the network call, and only ever once for that leg.

        The counter predicate plus the schema CHECK is what makes a second write unrecordable: a caller
        that somehow tried again finds zero rows updated and must not call. Entry and exit have
        separate counters because by the time a position can be closed the entry has already spent
        its one — a shared counter would leave the exit with no protection at all.
        """

        if kind == "entry":
            sql = """
                UPDATE trading_orders
                   SET state = 'SUBMITTING',
                       provider_attempt_count = provider_attempt_count + 1,
                       updated_at_ms = %s
                 WHERE order_id = %s
                   AND provider_attempt_count = 0
                   AND state IN ('PREPARED', 'APPROVED')
                   AND EXISTS (
                       SELECT 1 FROM trading_runtime_state
                        WHERE id = 1 AND control = 'RUNNING'
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM trading_symbol_blacklist b
                        WHERE b.base_symbol = split_part(trading_orders.underlying_key, ':', 2)
                          AND (b.expires_at_ms IS NULL OR b.expires_at_ms > %s)
                   )
            """
        elif kind == "exit":
            sql = """
                UPDATE trading_orders
                   SET state = 'SAFETY_CLOSING',
                       exit_attempt_count = exit_attempt_count + 1,
                       exit_attempt_total = exit_attempt_total + 1,
                       updated_at_ms = %s
                 WHERE order_id = %s
                   AND exit_attempt_count = 0
                   AND exit_attempt_total < 3
                   AND state IN ('ACKNOWLEDGED', 'OPEN', 'PARTIAL', 'UNPROTECTED')
            """
        else:  # pragma: no cover - the caller passes a literal
            raise ValueError(f"trading_attempt_kind_invalid:{kind}")
        params = (int(now_ms), order_id, int(now_ms)) if kind == "entry" else (int(now_ms), order_id)
        cursor = self.conn.execute(sql, params)
        if int(getattr(cursor, "rowcount", 0) or 0) > 0:
            return "claimed"
        row = self.conn.execute(
            "SELECT o.state, o.provider_attempt_count, o.exit_attempt_count, o.exit_attempt_total, r.control, "
            "EXISTS (SELECT 1 FROM trading_symbol_blacklist b "
            "WHERE b.base_symbol = split_part(o.underlying_key, ':', 2) "
            "AND (b.expires_at_ms IS NULL OR b.expires_at_ms > %s)) AS blacklisted "
            "FROM trading_orders o CROSS JOIN trading_runtime_state r WHERE o.order_id = %s AND r.id = 1",
            (int(now_ms), order_id),
        ).fetchone()
        if row is None:
            return "missing"
        if kind == "exit" and int(row["exit_attempt_total"]) >= _MAX_EXIT_ATTEMPTS:
            return "exhausted"
        if kind == "entry" and int(row["provider_attempt_count"]) > 0:
            return "already_spent"
        if kind == "entry" and str(row["control"]) != "RUNNING":
            return "control_blocked"
        if kind == "entry" and bool(row["blacklisted"]):
            return "blacklisted"
        if kind == "exit" and int(row["exit_attempt_count"]) > 0:
            return "already_spent"
        # The counter is free but the row is not in a state this leg may be claimed from.
        return "wrong_state"

    def release_exit_attempt(self, *, order_id: str, now_ms: int) -> bool:
        """Let the exit be attempted again, because a read proved the position is still open.

        This is the only thing that makes the exit's one-attempt claim recoverable. It is safe for the
        exact reason the entry has no equivalent: the read has proven the previous close did not take
        effect, so re-issuing it cannot double-close. `exit_attempt_total` still caps the retries.
        """

        cursor = self.conn.execute(
            "UPDATE trading_orders SET exit_attempt_count = 0, updated_at_ms = %s "
            f"WHERE order_id = %s AND exit_attempt_total < {_MAX_EXIT_ATTEMPTS}",
            (int(now_ms), order_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def resolve_manual_review(
        self,
        *,
        order_id: str,
        outcome: str,
        reason: str,
        remote_order_id: str | None = None,
        now_ms: int,
    ) -> bool:
        """The operator drain for `MANUAL_REVIEW_REQUIRED`. Without it the state is a permanent wedge.

        Five reconcile paths escalate here and the state sits inside the active-underlying index, so
        two unresolved orders halt the lane with no remedy. `--closed` says the operator has confirmed
        at the venue that nothing is outstanding; `--open` hands the order back to the reconciler.
        """

        if outcome == "closed":
            cursor = self.conn.execute(
                """
                UPDATE trading_orders
                   SET state = 'CLOSED',
                       state_reason = %s,
                       position_closed_at_ms = coalesce(position_closed_at_ms, %s),
                       closed_at_ms = coalesce(closed_at_ms, %s),
                       next_reconcile_at_ms = NULL,
                       updated_at_ms = %s
                 WHERE order_id = %s AND state = 'MANUAL_REVIEW_REQUIRED'
                """,
                (f"operator_resolved:{reason}", int(now_ms), int(now_ms), int(now_ms), order_id),
            )
        elif outcome == "open":
            # `exit_attempt_total` is reset too, not only the per-state counter. The ceiling exists to
            # stop an *unattended* loop from re-issuing a close forever, and `release_exit_attempt`
            # rightly refuses to lift its own. A human who has checked the venue is the actor the
            # ceiling should defer to — without this reset, `resolve <id> open` after exhaustion put
            # the row straight back into MANUAL_REVIEW_REQUIRED on the next turn with no explanation,
            # and the only escape left was asserting `closed` about a position that is still open.
            remote_id = None if remote_order_id is None else str(remote_order_id).strip() or None
            cursor = self.conn.execute(
                """
                UPDATE trading_orders
                   SET state = 'OPEN',
                       state_reason = %s,
                       remote_order_id = coalesce(%s, remote_order_id),
                       exit_attempt_count = 0,
                       exit_attempt_total = 0,
                       next_reconcile_at_ms = %s,
                       updated_at_ms = %s
                 WHERE order_id = %s
                   AND state = 'MANUAL_REVIEW_REQUIRED'
                   AND (mode = 'paper' OR remote_order_id IS NOT NULL OR %s)
                """,
                (f"operator_resolved:{reason}", remote_id, int(now_ms), int(now_ms), order_id, remote_id is not None),
            )
        else:  # pragma: no cover - the CLI constrains the choices
            raise ValueError(f"trading_manual_resolution_invalid:{outcome}")
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def promote_acknowledged(
        self,
        *,
        order_id: str,
        remote_order_id: str | None,
        filled_quantity: str,
        average_price: str,
        position_opened_at_ms: int,
        must_close_at_ms: int,
        now_ms: int,
    ) -> bool:
        """`ACKNOWLEDGED -> OPEN`, guarded on the state it expects like every other transition here.

        The reconcile loop is sequential and this is the only handler for ACKNOWLEDGED, so an
        unguarded write would be safe today — but "guarded unless there is a reason not to be" is the
        rule the rest of this class follows, and the unguarded exception is what a future concurrent
        reader would copy.
        """

        cursor = self.conn.execute(
            """
            UPDATE trading_orders
               SET state = 'OPEN',
                   state_reason = 'paper_fill_observed',
                   remote_order_id = %s,
                   filled_quantity = %s,
                   average_price = %s,
                   position_opened_at_ms = %s,
                   must_close_at_ms = %s,
                   updated_at_ms = %s
             WHERE order_id = %s AND state = 'ACKNOWLEDGED'
            """,
            (
                remote_order_id,
                filled_quantity,
                average_price,
                int(position_opened_at_ms),
                int(must_close_at_ms),
                int(now_ms),
                order_id,
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def reschedule_order(self, *, order_id: str, expected_state: str, next_reconcile_at_ms: int, now_ms: int) -> bool:
        """Push a due order out without touching its state.

        `update_order` writes `state` unconditionally, so a deferral computed from a state read at the
        top of a 32-row batch could blind-write a stale state back over one the commit path had since
        advanced — a live position whose row says `PREPARED` is invisible to `_manage_open` forever.
        An APPROVED row keeps `updated_at_ms` as its durable approval time; reconciliation uses that
        instant to give a valid late approval one full runner cadence without extending approval TTL.
        """

        cursor = self.conn.execute(
            """
            UPDATE trading_orders
               SET next_reconcile_at_ms = %s,
                   updated_at_ms = CASE WHEN state = 'APPROVED' THEN updated_at_ms ELSE %s END
             WHERE order_id = %s AND state = %s
            """,
            (int(next_reconcile_at_ms), int(now_ms), order_id, expected_state),
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
        position_closed_at_ms: int | None = None,
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
                   position_closed_at_ms = coalesce(%s, position_closed_at_ms),
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
                position_closed_at_ms,
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
               SET state = 'APPROVED',
                   state_reason = %s,
                   next_reconcile_at_ms = %s,
                   updated_at_ms = %s
             WHERE order_id = %s
               AND payload_sha256 = %s
               AND state = 'AWAITING_APPROVAL'
               AND created_at_ms BETWEEN %s AND %s
            """,
            (
                TRADING_LIVE_APPROVAL_MARKER,
                int(now_ms),
                int(now_ms),
                order_id,
                payload_sha256,
                int(now_ms) - TRADING_LIVE_APPROVAL_TTL_MS,
                int(now_ms),
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def reject_unsubmitted(self, *, order_id: str, expected_state: str, reason: str, now_ms: int) -> bool:
        """Reject only the exact unsubmitted state the caller read.

        Approval and reconciliation are concurrent database clients. A stale AWAITING_APPROVAL scan
        must not overwrite an APPROVED row, and a stale APPROVED scan must not overwrite a claimed
        provider attempt. The state and counter predicates make either advancement authoritative.
        """

        if expected_state not in {"AWAITING_APPROVAL", "APPROVED"}:
            raise ValueError(f"trading_unsubmitted_state_invalid:{expected_state}")
        cursor = self.conn.execute(
            """
            UPDATE trading_orders
               SET state = 'REJECTED',
                   state_reason = %s,
                   closed_at_ms = %s,
                   next_reconcile_at_ms = NULL,
                   updated_at_ms = %s
             WHERE order_id = %s
               AND state = %s
               AND provider_attempt_count = 0
            """,
            (reason, int(now_ms), int(now_ms), order_id, expected_state),
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


__all__ = ["OrderStorage"]
