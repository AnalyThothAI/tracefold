"""Short-transaction persistence for the venue-reconciled execution process."""

from __future__ import annotations

import json
from typing import Any, cast

from tracefold.trading.executor.core import SignalV4
from tracefold.trading.operator_control import PreparedOperatorIntent

SIGNAL_LEDGER_SQL = "SELECT seq,payload FROM trading_signals WHERE decided_at_ns>=%s ORDER BY seq DESC LIMIT %s"
FILL_LEDGER_SQL = (
    "SELECT f.environment,f.native_symbol,f.trade_id,a.plan_id,a.client_order_id,"
    "f.venue_order_id,f.quantity,f.price,f.realized_pnl,f.fee,f.fee_asset,f.traded_at_ns "
    "FROM trading_fills f LEFT JOIN trading_fill_attributions a "
    "ON (a.environment,a.native_symbol,a.trade_id)=(f.environment,f.native_symbol,f.trade_id) "
    "WHERE f.traded_at_ns>=%s ORDER BY f.traded_at_ns DESC,f.trade_id DESC LIMIT %s"
)
OPERATOR_INTENTS_SQL = (
    "SELECT i.*,d.disposition,d.reason AS disposition_reason,d.decided_at_ns "
    "FROM trading_operator_intents i LEFT JOIN trading_dispositions d "
    "ON d.input_kind='intent' AND d.input_id=i.command_id "
    "WHERE i.requested_at_ns>=%s AND (%s::text IS NULL OR i.action=%s) "
    "ORDER BY i.seq DESC LIMIT %s"
)
EXECUTION_PLANS_SQL = (
    "SELECT p.plan_id,p.signal_id,p.command_id,p.account_slot,p.native_symbol,p.side,"
    "p.quantity,p.stop_bps,p.tp_bps,p.max_hold_s,p.status,p.opened_at_ns,"
    "p.terminal_at_ns,p.terminal_reason,p.pnl_status,p.realized_pnl,p.fees,p.net_pnl,"
    "p.updated_at_ns,s.case_id,s.decided_at_ns,s.expires_at_ns,i.requested_at_ns "
    "FROM trading_plans p "
    "LEFT JOIN trading_signals s ON s.signal_id=p.signal_id "
    "LEFT JOIN trading_operator_intents i ON i.command_id=p.command_id "
    "WHERE p.updated_at_ns>=%s AND (%s::text IS NULL OR s.case_id=%s) "
    "ORDER BY p.updated_at_ns DESC,p.plan_id DESC LIMIT %s"
)
EXECUTION_REFUSALS_SQL = """
    SELECT d.input_kind,d.input_id,d.reason,d.decided_at_ns,
           s.case_id,s.native_symbol,s.payload AS signal_payload,
           s.decided_at_ns,s.expires_at_ns,i.payload AS intent_payload,
           i.requested_at_ns,i.action
    FROM trading_dispositions d
    LEFT JOIN trading_signals s ON d.input_kind='signal' AND d.input_id=s.signal_id
    LEFT JOIN trading_operator_intents i ON d.input_kind='intent' AND d.input_id=i.command_id
    WHERE d.plan_id IS NULL AND d.decided_at_ns>=%s
      AND (%s::text IS NULL OR s.case_id=%s)
      AND (d.input_kind='signal' OR i.action='manual_entry')
    ORDER BY d.decided_at_ns DESC LIMIT %s
    """
EXECUTION_ORDERS_SQL = (
    "SELECT client_order_id,plan_id,leg,attempt,status,error_code,evidence FROM trading_orders "
    "WHERE plan_id=ANY(%s) ORDER BY plan_id,attempt"
)
EXECUTION_FILLS_SQL = (
    "SELECT a.plan_id,a.client_order_id,f.quantity,f.price,f.traded_at_ns,f.trade_id "
    "FROM trading_fill_attributions a JOIN trading_fills f "
    "ON (f.environment,f.native_symbol,f.trade_id)=(a.environment,a.native_symbol,a.trade_id) "
    "WHERE a.plan_id=ANY(%s) ORDER BY f.traded_at_ns,f.trade_id"
)
REALIZED_TOTALS_SQL = """
    SELECT
      COUNT(*) FILTER (WHERE terminal_at_ns>=%s AND terminal_at_ns<%s) AS closed_today,
      COUNT(*) AS closed_total,
      COUNT(*) FILTER (WHERE pnl_status='complete'
                         AND terminal_at_ns>=%s AND terminal_at_ns<%s) AS known_today,
      COUNT(*) FILTER (WHERE pnl_status='complete') AS known_total,
      COALESCE(SUM(realized_pnl) FILTER (WHERE pnl_status='complete'
          AND terminal_at_ns>=%s AND terminal_at_ns<%s),0) AS realized_today,
      COALESCE(SUM(realized_pnl) FILTER (WHERE pnl_status='complete'),0) AS realized_total,
      COALESCE(SUM(net_pnl) FILTER (WHERE pnl_status='complete'
          AND terminal_at_ns>=%s AND terminal_at_ns<%s),0) AS net_today,
      COALESCE(SUM(net_pnl) FILTER (WHERE pnl_status='complete'),0) AS net_total
    FROM trading_plans WHERE account_slot=%s AND status='terminal'
    """


class ExecutorStorage:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def append_operator_intent(self, prepared: PreparedOperatorIntent) -> tuple[int, dict[str, Any]]:
        value = prepared.value
        inserted = self.conn.execute(
            """
            INSERT INTO trading_operator_intents (
                command_id,account_slot,action,scope,reason,operator_identity,
                authentication_identity,requested_at_ns,expires_at_ns,payload
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT(command_id) DO NOTHING RETURNING seq,payload
            """,
            (
                value.command_id,
                value.account_slot,
                value.action,
                value.scope,
                value.reason,
                value.operator_identity,
                value.authentication_identity,
                value.requested_at_ns,
                value.expires_at_ns,
                prepared.payload_json,
            ),
        ).fetchone()
        if inserted is None:
            inserted = self.conn.execute(
                "SELECT seq,payload FROM trading_operator_intents WHERE command_id=%s",
                (value.command_id,),
            ).fetchone()
            if inserted is None or inserted["payload"] != json.loads(prepared.payload_json):
                raise ValueError("operator_command_identity_conflict")
        return int(inserted["seq"]), dict(inserted["payload"])

    def heartbeat(self, *, account_slot: str, now_ns: int, error: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO trading_executor_state(account_slot,environment,heartbeat_at_ns,last_error)
            VALUES (%s,'DEMO',%s,%s)
            ON CONFLICT(account_slot) DO UPDATE SET
              heartbeat_at_ns=EXCLUDED.heartbeat_at_ns,
              last_error=EXCLUDED.last_error
            """,
            (account_slot, now_ns, error),
        )

    def state(self, account_slot: str) -> dict[str, Any] | None:
        return cast(
            dict[str, Any] | None,
            self.conn.execute("SELECT * FROM trading_executor_state WHERE account_slot=%s", (account_slot,)).fetchone(),
        )

    def control(self, account_slot: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM trading_control_state WHERE account_slot=%s", (account_slot,)).fetchone()
        return row or {"entries_paused": True, "emergency_halted": False, "flatten_command_id": None}

    def set_control(self, *, account_slot: str, paused: bool, halted: bool, now_ns: int) -> None:
        self.conn.execute(
            """
            INSERT INTO trading_control_state(account_slot,entries_paused,emergency_halted,updated_at_ns)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT(account_slot) DO UPDATE SET
              entries_paused=EXCLUDED.entries_paused,
              emergency_halted=EXCLUDED.emergency_halted,
              updated_at_ns=EXCLUDED.updated_at_ns
            """,
            (account_slot, paused, halted, now_ns),
        )

    def request_flatten(self, *, account_slot: str, command_id: str, now_ns: int) -> None:
        self.conn.execute(
            """
            INSERT INTO trading_control_state(account_slot,entries_paused,emergency_halted,
                                              flatten_command_id,updated_at_ns)
            VALUES (%s,true,false,%s,%s)
            ON CONFLICT(account_slot) DO UPDATE SET
              entries_paused=true,flatten_command_id=EXCLUDED.flatten_command_id,
              updated_at_ns=EXCLUDED.updated_at_ns
            """,
            (account_slot, command_id, now_ns),
        )

    def clear_flatten(self, *, account_slot: str) -> None:
        self.conn.execute(
            "UPDATE trading_control_state SET flatten_command_id=NULL WHERE account_slot=%s",
            (account_slot,),
        )

    def append_signal(self, signal: SignalV4) -> int:
        payload = signal.model_dump(mode="json", exclude={"seq"})
        packed = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        inserted = self.conn.execute(
            """
            INSERT INTO trading_signals(signal_id,case_id,decision_id,account_slot,native_symbol,
                                        decided_at_ns,expires_at_ns,payload,created_at_ns)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT(signal_id) DO NOTHING RETURNING seq,payload
            """,
            (
                signal.signal_id,
                signal.case_id,
                signal.decision_id,
                signal.account_slot,
                signal.native_symbol,
                signal.decided_at_ns,
                signal.expires_at_ns,
                packed,
                signal.decided_at_ns,
            ),
        ).fetchone()
        if inserted is None:
            existing = self.conn.execute(
                "SELECT seq,payload FROM trading_signals WHERE signal_id=%s", (signal.signal_id,)
            ).fetchone()
            if existing is None or existing["payload"] != payload:
                raise ValueError("signal_identity_conflict")
            return int(existing["seq"])
        return int(inserted["seq"])

    def next_signal(self, *, account_slot: str, after_seq: int) -> SignalV4 | None:
        row = self.conn.execute(
            """
            SELECT seq,payload FROM trading_signals
            WHERE account_slot=%s AND seq>%s ORDER BY seq LIMIT 1
            """,
            (account_slot, after_seq),
        ).fetchone()
        return (
            None
            if row is None
            else SignalV4.model_validate_json(json.dumps({**row["payload"], "seq": int(row["seq"])}))
        )

    def next_intent(self, *, account_slot: str, after_seq: int) -> dict[str, Any] | None:
        return cast(
            dict[str, Any] | None,
            self.conn.execute(
                """
            SELECT * FROM trading_operator_intents
            WHERE account_slot=%s AND seq>%s ORDER BY seq LIMIT 1
            """,
                (account_slot, after_seq),
            ).fetchone(),
        )

    def disposition(self, *, kind: str, input_id: str) -> dict[str, Any] | None:
        return cast(
            dict[str, Any] | None,
            self.conn.execute(
                "SELECT * FROM trading_dispositions WHERE input_kind=%s AND input_id=%s", (kind, input_id)
            ).fetchone(),
        )

    def record_disposition(
        self,
        *,
        kind: str,
        input_id: str,
        account_slot: str,
        disposition: str,
        reason: str,
        now_ns: int,
        plan_id: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO trading_dispositions(input_kind,input_id,account_slot,disposition,reason,plan_id,decided_at_ns)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(input_kind,input_id) DO NOTHING
            """,
            (kind, input_id, account_slot, disposition, reason, plan_id, now_ns),
        )
        row = self.disposition(kind=kind, input_id=input_id)
        if row is None or (row["disposition"], row["reason"], row["plan_id"]) != (disposition, reason, plan_id):
            raise ValueError("input_disposition_conflict")

    def advance_cursor(self, *, account_slot: str, kind: str, seq: int) -> None:
        if kind not in ("signal", "intent"):
            raise ValueError("cursor_kind_invalid")
        if kind == "signal":
            self.conn.execute(
                "UPDATE trading_executor_state SET last_signal_seq=GREATEST(last_signal_seq,%s) WHERE account_slot=%s",
                (seq, account_slot),
            )
        else:
            self.conn.execute(
                "UPDATE trading_executor_state SET last_intent_seq=GREATEST(last_intent_seq,%s) WHERE account_slot=%s",
                (seq, account_slot),
            )

    def create_plan(
        self,
        *,
        plan_id: str,
        signal_id: str | None,
        command_id: str | None,
        account_slot: str,
        native_symbol: str,
        side: str,
        quantity: str,
        reference_price: str,
        stop_bps: int,
        tp_bps: int,
        max_hold_s: int,
        now_ns: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO trading_plans(plan_id,signal_id,command_id,account_slot,environment,native_symbol,
                                      side,quantity,reference_price,reserved_notional,
                                      stop_bps,tp_bps,max_hold_s,status,updated_at_ns)
            VALUES (%s,%s,%s,%s,'DEMO',%s,%s,%s,%s,%s::numeric * %s::numeric,%s,%s,%s,'accepted',%s)
            """,
            (
                plan_id,
                signal_id,
                command_id,
                account_slot,
                native_symbol,
                side,
                quantity,
                reference_price,
                quantity,
                reference_price,
                stop_bps,
                tp_bps,
                max_hold_s,
                now_ns,
            ),
        )

    def reserve_order(
        self, *, client_id: str, plan_id: str, native_symbol: str, leg: str, attempt: int, now_ns: int
    ) -> dict[str, Any]:
        inserted = self.conn.execute(
            """
            INSERT INTO trading_orders(client_order_id,plan_id,environment,native_symbol,
                                       leg,attempt,status,updated_at_ns)
            VALUES (%s,%s,'DEMO',%s,%s,%s,'reserved',%s)
            ON CONFLICT(plan_id,leg,attempt) DO NOTHING RETURNING *
            """,
            (client_id, plan_id, native_symbol, leg, attempt, now_ns),
        ).fetchone()
        row = (
            inserted
            or self.conn.execute(
                "SELECT * FROM trading_orders WHERE plan_id=%s AND leg=%s AND attempt=%s",
                (plan_id, leg, attempt),
            ).fetchone()
        )
        if row is None or row["client_order_id"] != client_id:
            raise ValueError("order_identity_conflict")
        return cast(dict[str, Any], row)

    def reserve_external_flatten(
        self, *, client_id: str, command_id: str, symbol: str, attempt: int, now_ns: int
    ) -> dict[str, Any]:
        inserted = self.conn.execute(
            """
            INSERT INTO trading_orders(client_order_id,command_id,environment,native_symbol,
                                       leg,attempt,status,updated_at_ns)
            VALUES (%s,%s,'DEMO',%s,'account_flatten',%s,'reserved',%s)
            ON CONFLICT(command_id,native_symbol,leg,attempt) DO NOTHING RETURNING *
            """,
            (client_id, command_id, symbol, attempt, now_ns),
        ).fetchone()
        row = (
            inserted
            or self.conn.execute(
                "SELECT * FROM trading_orders WHERE command_id=%s AND native_symbol=%s "
                "AND leg='account_flatten' AND attempt=%s",
                (command_id, symbol, attempt),
            ).fetchone()
        )
        if row is None or row["client_order_id"] != client_id:
            raise ValueError("order_identity_conflict")
        return cast(dict[str, Any], row)

    def external_flatten_orders(self, command_id: str, symbol: str) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.conn.execute(
                "SELECT * FROM trading_orders WHERE command_id=%s AND native_symbol=%s "
                "AND leg='account_flatten' ORDER BY attempt",
                (command_id, symbol),
            ).fetchall(),
        )

    def update_order(
        self,
        *,
        client_id: str,
        status: str,
        now_ns: int,
        venue_order_id: str | None = None,
        error_code: int | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE trading_orders SET status=%s,venue_order_id=COALESCE(%s,venue_order_id),
              error_code=%s,evidence=COALESCE(%s::jsonb,evidence),
              submitted_at_ns=CASE WHEN %s IN ('unknown','submitted','working','filled')
                                   THEN COALESCE(submitted_at_ns,%s) ELSE submitted_at_ns END,
              resolved_at_ns=CASE WHEN %s IN ('filled','cancelled','rejected','not_submitted')
                                  THEN %s ELSE resolved_at_ns END,
              updated_at_ns=%s
            WHERE client_order_id=%s
            """,
            (
                status,
                venue_order_id,
                error_code,
                None if evidence is None else json.dumps(evidence),
                status,
                now_ns,
                status,
                now_ns,
                now_ns,
                client_id,
            ),
        )

    def active_plans(self, account_slot: str) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.conn.execute(
                "SELECT * FROM trading_plans WHERE account_slot=%s AND terminal_at_ns IS NULL ORDER BY updated_at_ns",
                (account_slot,),
            ).fetchall(),
        )

    def active_client_ids(self, account_slot: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT o.client_order_id FROM trading_orders o LEFT JOIN trading_plans p ON p.plan_id=o.plan_id "
            "LEFT JOIN trading_operator_intents i ON i.command_id=o.command_id "
            "WHERE (p.account_slot=%s AND p.terminal_at_ns IS NULL) "
            "OR (i.account_slot=%s AND o.status IN ('reserved','unknown','submitted','working'))",
            (account_slot, account_slot),
        ).fetchall()
        return {str(row["client_order_id"]) for row in rows}

    def record_full_reconciliation(
        self,
        *,
        account_slot: str,
        now_ns: int,
        unexpected: bool,
        account_snapshot: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE trading_executor_state SET last_full_reconcile_at_ns=%s,unexpected_exposure=%s,"
            "account_snapshot=COALESCE(%s::jsonb,account_snapshot) "
            "WHERE account_slot=%s",
            (now_ns, unexpected, None if account_snapshot is None else json.dumps(account_snapshot), account_slot),
        )

    def plans_awaiting_fills(self, account_slot: str) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.conn.execute(
                "SELECT * FROM trading_plans WHERE account_slot=%s AND status='terminal' "
                "AND pnl_status='pending' ORDER BY terminal_at_ns LIMIT 100",
                (account_slot,),
            ).fetchall(),
        )

    def plan(self, plan_id: str) -> dict[str, Any] | None:
        return cast(
            dict[str, Any] | None,
            self.conn.execute("SELECT * FROM trading_plans WHERE plan_id=%s", (plan_id,)).fetchone(),
        )

    def plan_orders(self, plan_id: str) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.conn.execute(
                "SELECT * FROM trading_orders WHERE plan_id=%s ORDER BY leg,attempt", (plan_id,)
            ).fetchall(),
        )

    def trade_cursor(self, symbol: str) -> int | None:
        row = self.conn.execute(
            "SELECT next_trade_id FROM trading_trade_cursors WHERE environment='DEMO' AND native_symbol=%s",
            (symbol,),
        ).fetchone()
        return None if row is None else int(row["next_trade_id"])

    def advance_trade_cursor(self, *, symbol: str, next_id: int, now_ns: int) -> None:
        self.conn.execute(
            """
            INSERT INTO trading_trade_cursors(environment,native_symbol,next_trade_id,checked_at_ns)
            VALUES ('DEMO',%s,%s,%s)
            ON CONFLICT(environment,native_symbol) DO UPDATE SET
              next_trade_id=GREATEST(trading_trade_cursors.next_trade_id,EXCLUDED.next_trade_id),
              checked_at_ns=EXCLUDED.checked_at_ns
            """,
            (symbol, next_id, now_ns),
        )

    def set_plan_status(
        self,
        *,
        plan_id: str,
        status: str,
        now_ns: int,
        opened_at_ns: int | None = None,
        terminal_reason: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE trading_plans SET status=%s,
              opened_at_ns=COALESCE(opened_at_ns,%s),
              terminal_at_ns=CASE WHEN %s='terminal' THEN COALESCE(terminal_at_ns,%s) ELSE terminal_at_ns END,
              terminal_reason=COALESCE(terminal_reason,%s),
              pnl_deadline_ns=CASE WHEN %s='terminal' THEN COALESCE(pnl_deadline_ns,%s) ELSE pnl_deadline_ns END,
              updated_at_ns=%s
            WHERE plan_id=%s AND (terminal_at_ns IS NULL OR %s='terminal')
            """,
            (
                status,
                opened_at_ns,
                status,
                now_ns,
                terminal_reason,
                status,
                now_ns + 60_000_000_000,
                now_ns,
                plan_id,
                status,
            ),
        )

    def record_fill(self, *, symbol: str, trade: dict[str, Any]) -> bool:
        inserted = self.conn.execute(
            """
            INSERT INTO trading_fills(environment,native_symbol,trade_id,venue_order_id,
                                      quantity,price,realized_pnl,fee,fee_asset,traded_at_ns,evidence)
            VALUES ('DEMO',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT(environment,native_symbol,trade_id) DO NOTHING RETURNING trade_id
            """,
            (
                symbol,
                trade["id"],
                str(trade["orderId"]),
                trade["qty"],
                trade["price"],
                trade["realizedPnl"],
                trade["commission"],
                trade["commissionAsset"],
                int(trade["time"]) * 1_000_000,
                json.dumps(trade),
            ),
        ).fetchone()
        return inserted is not None

    def attribute_unbound_fills(self, *, symbol: str, now_ns: int) -> int:
        """Bind native trades after order evidence arrives, without mutating fill facts."""
        rows = self.conn.execute(
            """
            INSERT INTO trading_fill_attributions(
                environment,native_symbol,trade_id,plan_id,command_id,client_order_id,attributed_at_ns
            )
            SELECT f.environment,f.native_symbol,f.trade_id,o.plan_id,o.command_id,o.client_order_id,%s
            FROM trading_fills f JOIN trading_orders o
              ON o.environment=f.environment AND o.native_symbol=f.native_symbol
             AND o.venue_order_id=f.venue_order_id
            WHERE f.environment='DEMO' AND f.native_symbol=%s
            ON CONFLICT(environment,native_symbol,trade_id) DO NOTHING
            RETURNING trade_id
            """,
            (now_ns, symbol),
        ).fetchall()
        return len(rows)

    def exit_fill_client_id(self, plan_id: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT client_order_id FROM trading_fill_attributions
            WHERE plan_id=%s AND client_order_id IN
              (SELECT client_order_id FROM trading_orders WHERE plan_id=%s AND leg<>'entry')
            ORDER BY trade_id DESC LIMIT 1
            """,
            (plan_id, plan_id),
        ).fetchone()
        return None if row is None else str(row["client_order_id"])

    def pending_pnl_plans(self, account_slot: str) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.conn.execute(
                """
            SELECT * FROM trading_plans WHERE account_slot=%s AND status='terminal' AND pnl_status='pending'
            ORDER BY terminal_at_ns LIMIT 100
            """,
                (account_slot,),
            ).fetchall(),
        )

    def settle_pnl(self, *, plan: dict[str, Any], now_ns: int) -> str:
        from decimal import Decimal

        rows = self.conn.execute(
            """
            SELECT o.leg,COALESCE(SUM(f.quantity),0) AS quantity,
                   COALESCE(SUM(f.realized_pnl),0) AS realized_pnl,
                   COALESCE(SUM(f.fee),0) AS fee,
                   COALESCE(BOOL_AND(f.fee_asset='USDT') FILTER (WHERE f.trade_id IS NOT NULL),true)
                       AS fees_in_usdt
            FROM trading_orders o
            LEFT JOIN trading_fill_attributions a ON a.client_order_id=o.client_order_id
            LEFT JOIN trading_fills f ON (f.environment,f.native_symbol,f.trade_id)
              =(a.environment,a.native_symbol,a.trade_id)
            WHERE o.plan_id=%s GROUP BY o.leg
            """,
            (plan["plan_id"],),
        ).fetchall()
        entry_qty = sum(row["quantity"] for row in rows if row["leg"] == "entry")
        exit_qty = sum(row["quantity"] for row in rows if row["leg"] != "entry")
        entry_order = self.conn.execute(
            "SELECT status,evidence FROM trading_orders WHERE plan_id=%s AND leg='entry'",
            (plan["plan_id"],),
        ).fetchone()
        entry_evidence = {} if entry_order is None else entry_order["evidence"] or {}
        expected_entry = Decimal(str(entry_evidence.get("executedQty", plan["quantity"])))
        complete = bool(
            entry_order
            and entry_order["status"] in ("filled", "cancelled", "rejected", "not_submitted")
            and entry_qty >= expected_entry
            and exit_qty >= entry_qty
            and all(row["fees_in_usdt"] for row in rows)
        )
        if not complete and now_ns < plan["pnl_deadline_ns"]:
            return "pending"
        status = "complete" if complete else "evidence_incomplete"
        realized = sum(row["realized_pnl"] for row in rows)
        fees = sum(row["fee"] for row in rows)
        self.conn.execute(
            """
            UPDATE trading_plans SET pnl_status=%s,realized_pnl=%s,fees=%s,net_pnl=%s,updated_at_ns=%s
            WHERE plan_id=%s AND pnl_status='pending'
            """,
            (
                status,
                realized,
                fees if all(row["fees_in_usdt"] for row in rows) else None,
                realized - fees if complete else None,
                now_ns,
                plan["plan_id"],
            ),
        )
        return status

    def signal_ledger(self, *, since_ns: int, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(SIGNAL_LEDGER_SQL, (since_ns, limit)).fetchall()
        return [{**row["payload"], "seq": row["seq"]} for row in rows]

    def fill_ledger(self, *, since_ns: int, limit: int) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.conn.execute(FILL_LEDGER_SQL, (since_ns, limit)).fetchall())

    def console_operator_intents(self, *, since_ns: int, action: str | None, limit: int) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]], self.conn.execute(OPERATOR_INTENTS_SQL, (since_ns, action, action, limit)).fetchall()
        )

    def console_executions(self, *, since_ns: int, limit: int, case_id: str | None = None) -> list[dict[str, Any]]:
        from decimal import Decimal

        plans = self.conn.execute(EXECUTION_PLANS_SQL, (since_ns, case_id, case_id, limit)).fetchall()
        refused = self.conn.execute(EXECUTION_REFUSALS_SQL, (since_ns, case_id, case_id, limit)).fetchall()
        ids = [plan["plan_id"] for plan in plans]
        orders = self.conn.execute(
            EXECUTION_ORDERS_SQL,
            (ids,),
        ).fetchall()
        fills = self.conn.execute(
            EXECUTION_FILLS_SQL,
            (ids,),
        ).fetchall()
        by_orders: dict[str, list[dict[str, Any]]] = {}
        by_fills: dict[str, list[dict[str, Any]]] = {}
        for order in orders:
            by_orders.setdefault(order["plan_id"], []).append(order)
        for fill in fills:
            by_fills.setdefault(fill["plan_id"], []).append(fill)
        result = []
        for plan in plans:
            own_orders = by_orders.get(plan["plan_id"], [])
            own_fills = by_fills.get(plan["plan_id"], [])
            entry = next((row for row in own_orders if row["leg"] == "entry"), None)
            entry_ids = {row["client_order_id"] for row in own_orders if row["leg"] == "entry"}
            entry_fills = [row for row in own_fills if row["client_order_id"] in entry_ids]
            exit_fills = [row for row in own_fills if row["client_order_id"] not in entry_ids]
            entry_qty = sum((row["quantity"] for row in entry_fills), Decimal(0))
            exit_qty = sum((row["quantity"] for row in exit_fills), Decimal(0))
            protection = {
                leg: next((row for row in reversed(own_orders) if row["leg"] == leg), None) for leg in ("sl", "tp")
            }
            symbol = str(plan["native_symbol"])
            base = symbol.removesuffix("USDT")
            terminal = plan["terminal_at_ns"] is not None
            result.append(
                {
                    "source": "signal" if plan["signal_id"] is not None else "manual",
                    "entry_id": plan["plan_id"],
                    "case_id": plan["case_id"],
                    "market_key": f"crypto:perp:{base}:USDT",
                    "direction": plan["side"],
                    "observed_at_ns": plan["decided_at_ns"] or plan["requested_at_ns"],
                    "disposition_reason": "accepted",
                    "fill_quantity": str(entry_qty) if entry_qty else None,
                    "fill_avg_price": (
                        str(sum(row["quantity"] * row["price"] for row in entry_fills) / entry_qty)
                        if entry_qty
                        else None
                    ),
                    "stop_trigger_price": (protection["sl"] or {}).get("evidence", {}).get("triggerPrice")
                    if protection["sl"]
                    else None,
                    "take_profit_trigger_price": (protection["tp"] or {}).get("evidence", {}).get("triggerPrice")
                    if protection["tp"]
                    else None,
                    "entry_filled_at_ns": min((row["traded_at_ns"] for row in entry_fills), default=None),
                    "position_closed_at_ns": plan["terminal_at_ns"],
                    "exit_price": (
                        str(sum(row["quantity"] * row["price"] for row in exit_fills) / exit_qty) if exit_qty else None
                    ),
                    "realized_pnl_usd": plan["realized_pnl"] if plan["pnl_status"] == "complete" else None,
                    "fees_usd": plan["fees"] if plan["pnl_status"] == "complete" else None,
                    "net_pnl_usd": plan["net_pnl"] if plan["pnl_status"] == "complete" else None,
                    "exit_reason": plan["terminal_reason"],
                    "pnl_status": plan["pnl_status"],
                    "plan_status": "closed" if terminal else "open" if plan["opened_at_ns"] else "prepared",
                    "account_slot": plan["account_slot"],
                    "instrument_id": symbol,
                    "entry_client_order_id": None if entry is None else entry["client_order_id"],
                    "entry_error_code": None if entry is None else entry["error_code"],
                    "stop_distance_bps": plan["stop_bps"],
                    "take_profit_bps": plan["tp_bps"],
                    "max_holding_ns": plan["max_hold_s"] * 1_000_000_000,
                    "duration_ns": (plan["terminal_at_ns"] - plan["opened_at_ns"])
                    if terminal and plan["opened_at_ns"]
                    else None,
                    "order_status": None if entry is None else entry["status"].upper(),
                    "position_status": "closed" if terminal and plan["opened_at_ns"] else None,
                    "expires_at_ns": plan["expires_at_ns"],
                }
            )
        for item in refused:
            payload = item["signal_payload"] or item["intent_payload"] or {}
            symbol = item["native_symbol"]
            result.append(
                {
                    "source": "signal" if item["input_kind"] == "signal" else "manual",
                    "entry_id": item["input_id"],
                    "case_id": item["case_id"],
                    "market_key": payload.get("market_key")
                    or (f"crypto:perp:{str(symbol).removesuffix('USDT')}:USDT" if symbol else "unknown"),
                    "direction": payload.get("side") or payload.get("direction") or "long",
                    "observed_at_ns": item["decided_at_ns"] or item["requested_at_ns"],
                    "disposition_reason": item["reason"],
                    "expires_at_ns": item["expires_at_ns"],
                }
            )
        result.sort(key=lambda row: int(row["observed_at_ns"]), reverse=True)
        return result[:limit]

    def console_realized_totals(self, *, account_slot: str, day_start_ns: int, day_end_ns: int) -> dict[str, Any]:
        row = self.conn.execute(
            REALIZED_TOTALS_SQL,
            (
                day_start_ns,
                day_end_ns,
                day_start_ns,
                day_end_ns,
                day_start_ns,
                day_end_ns,
                day_start_ns,
                day_end_ns,
                account_slot,
            ),
        ).fetchone()
        today = int(row["closed_today"])
        total = int(row["closed_total"])
        known_today = int(row["known_today"])
        known_total = int(row["known_total"])
        return {
            "realized_known_today_usd": str(row["realized_today"]) if known_today else None,
            "realized_known_total_usd": str(row["realized_total"]) if known_total else None,
            "net_known_today_usd": str(row["net_today"]) if known_today else None,
            "net_known_total_usd": str(row["net_total"]) if known_total else None,
            "closed_today": today,
            "closed_total": total,
            "pnl_known_today": known_today,
            "pnl_known_total": known_total,
            "pnl_missing_today": today - known_today,
            "pnl_missing_total": total - known_total,
            "net_known_today": known_today,
            "net_known_total": known_total,
            "net_missing_today": today - known_today,
            "net_missing_total": total - known_total,
        }


__all__ = ["ExecutorStorage"]
