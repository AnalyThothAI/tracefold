"""Short-transaction persistence for the venue-reconciled execution process."""

from __future__ import annotations

import json
from typing import Any, cast

from tracefold.trading.executor.core import SignalV4
from tracefold.trading.operator_control import CONTROL_EFFECTS, ENTRY_STOP_ACTIONS, PreparedOperatorIntent

SIGNAL_LEDGER_SQL = (
    "SELECT request FROM trading_entries WHERE source='signal' AND requested_at_ns>=%s "
    "ORDER BY created_at_ns DESC,entry_id DESC LIMIT %s"
)
FILL_LEDGER_SQL = (
    "SELECT f.environment,f.native_symbol,f.trade_id,o.entry_id,f.client_order_id,"
    "f.venue_order_id,f.quantity,f.price,f.realized_pnl,f.fee,f.fee_asset,f.traded_at_ns "
    "FROM trading_fills f LEFT JOIN trading_orders o USING(client_order_id) "
    "WHERE f.traded_at_ns>=%s ORDER BY f.traded_at_ns DESC,f.trade_id DESC LIMIT %s"
)
OPERATOR_INTENTS_SQL = (
    "SELECT command_id,seq,account_slot,action,scope,reason,operator_identity,authentication_identity,"
    "requested_at_ns,expires_at_ns,payload,disposition,disposition_reason,decided_at_ns "
    "FROM trading_operator_intents WHERE requested_at_ns>=%s AND (%s::text IS NULL OR action=%s) "
    "ORDER BY seq DESC LIMIT %s"
)
EXECUTION_ENTRIES_SQL = (
    "SELECT e.entry_id,CASE WHEN e.source='signal' THEN e.entry_id END AS signal_id,"
    "e.command_id,e.account_slot,e.native_symbol,e.side,e.quantity,e.stop_bps,e.tp_bps,e.max_hold_s,"
    "e.state,e.opened_at_ns,e.terminal_at_ns,e.terminal_reason,e.pnl_status,e.realized_pnl,"
    "e.fees,e.net_pnl,e.updated_at_ns,e.case_id,CASE WHEN e.source='signal' THEN "
    "e.requested_at_ns END AS decided_at_ns,"
    "CASE WHEN e.source='signal' THEN e.expires_at_ns END AS expires_at_ns,i.requested_at_ns FROM trading_entries e "
    "LEFT JOIN trading_operator_intents i ON i.command_id=e.command_id "
    "WHERE e.state IN ('accepted','open','closing','terminal') AND e.updated_at_ns>=%s "
    "AND (%s::text IS NULL OR e.case_id=%s) ORDER BY e.updated_at_ns DESC,e.entry_id DESC LIMIT %s"
)
EXECUTION_REFUSALS_SQL = """
    SELECT input_kind,input_id,reason,decided_at_ns,case_id,native_symbol,signal_payload,
           expires_at_ns,intent_payload,requested_at_ns,action FROM (
      SELECT 'signal' AS input_kind,entry_id AS input_id,reason,requested_at_ns AS decided_at_ns,case_id,
             native_symbol,request AS signal_payload,expires_at_ns,NULL::jsonb AS intent_payload,
             NULL::bigint AS requested_at_ns,NULL::text AS action,disposed_at_ns
      FROM trading_entries WHERE source='signal' AND state IN ('refused','expired')
      UNION ALL
      SELECT 'intent',command_id,disposition_reason,NULL,NULL,NULL,NULL,NULL,
             payload,requested_at_ns,action,decided_at_ns
      FROM trading_operator_intents WHERE action='manual_entry' AND disposition IN ('refused','expired')
    ) r WHERE disposed_at_ns>=%s AND (%s::text IS NULL OR case_id=%s)
    ORDER BY disposed_at_ns DESC LIMIT %s
    """
EXECUTION_ORDERS_SQL = (
    "SELECT client_order_id,entry_id,leg,attempt,status,error_code,evidence FROM trading_orders "
    "WHERE entry_id=ANY(%s) ORDER BY entry_id,attempt"
)
EXECUTION_FILLS_SQL = (
    "SELECT o.entry_id,f.client_order_id,f.quantity,f.price,f.traded_at_ns,f.trade_id "
    "FROM trading_fills f JOIN trading_orders o USING(client_order_id) "
    "WHERE o.entry_id=ANY(%s) ORDER BY f.traded_at_ns,f.trade_id"
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
    FROM trading_entries WHERE account_slot=%s AND state='terminal'
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

    def ensure_account(self, account_slot: str) -> None:
        self.conn.execute(
            "INSERT INTO trading_accounts(account_slot,environment) VALUES (%s,'DEMO') "
            "ON CONFLICT(account_slot) DO NOTHING",
            (account_slot,),
        )

    def account(self, account_slot: str) -> dict[str, Any] | None:
        return cast(
            dict[str, Any] | None,
            self.conn.execute("SELECT * FROM trading_accounts WHERE account_slot=%s", (account_slot,)).fetchone(),
        )

    def control(self, account_slot: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM trading_accounts WHERE account_slot=%s", (account_slot,)).fetchone()
        return row or {"entries_paused": True, "emergency_halted": False, "flatten_command_id": None}

    def set_control(self, *, account_slot: str, paused: bool, halted: bool, now_ns: int) -> None:
        self.conn.execute(
            "UPDATE trading_accounts SET entries_paused=%s,emergency_halted=%s,control_updated_at_ns=%s "
            "WHERE account_slot=%s",
            (paused, halted, now_ns, account_slot),
        )

    def apply_control(self, *, account_slot: str, action: str, command_id: str, now_ns: int) -> None:
        effect = CONTROL_EFFECTS[action]
        self.conn.execute(
            "UPDATE trading_accounts SET entries_paused=COALESCE(%s,entries_paused),"
            "emergency_halted=COALESCE(%s,emergency_halted),"
            "flatten_command_id=CASE WHEN %s THEN %s ELSE flatten_command_id END,control_updated_at_ns=%s "
            "WHERE account_slot=%s",
            (effect.paused, effect.halted, effect.flatten, command_id, now_ns, account_slot),
        )

    def clear_flatten(self, *, account_slot: str) -> None:
        self.conn.execute(
            "UPDATE trading_accounts SET flatten_command_id=NULL WHERE account_slot=%s",
            (account_slot,),
        )

    def append_signal(self, signal: SignalV4) -> str:
        payload = signal.model_dump(mode="json")
        inserted = self.conn.execute(
            "INSERT INTO trading_entries(entry_id,source,case_id,account_slot,native_symbol,side,request,"
            "requested_at_ns,expires_at_ns,created_at_ns,state,updated_at_ns) "
            "VALUES (%s,'signal',%s,%s,%s,%s,%s::jsonb,%s,%s,%s,'pending',%s) "
            "ON CONFLICT(entry_id) DO NOTHING RETURNING entry_id",
            (
                signal.signal_id,
                signal.case_id,
                signal.account_slot,
                signal.native_symbol,
                signal.side,
                json.dumps(payload),
                signal.decided_at_ns,
                signal.expires_at_ns,
                signal.decided_at_ns,
                signal.decided_at_ns,
            ),
        ).fetchone()
        if inserted is None:
            row = self.conn.execute(
                "SELECT request FROM trading_entries WHERE entry_id=%s", (signal.signal_id,)
            ).fetchone()
            if row is None or row["request"] != payload:
                raise ValueError("signal_identity_conflict")
        return signal.signal_id

    def next_signal(self, *, account_slot: str) -> SignalV4 | None:
        row = self.conn.execute(
            "SELECT request FROM trading_entries WHERE account_slot=%s AND state='pending' "
            "ORDER BY created_at_ns,entry_id LIMIT 1",
            (account_slot,),
        ).fetchone()
        return None if row is None else SignalV4.model_validate_json(json.dumps(row["request"]))

    def next_intent(self, *, account_slot: str) -> dict[str, Any] | None:
        return cast(
            dict[str, Any] | None,
            self.conn.execute(
                "SELECT * FROM trading_operator_intents WHERE account_slot=%s AND disposition IS NULL "
                "ORDER BY seq LIMIT 1",
                (account_slot,),
            ).fetchone(),
        )

    def disposition(self, *, kind: str, input_id: str) -> dict[str, Any] | None:
        if kind == "signal":
            row = self.conn.execute(
                "SELECT state,reason,disposed_at_ns FROM trading_entries WHERE entry_id=%s AND state<>'pending'",
                (input_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "disposition": row["state"] if row["state"] in ("refused", "expired") else "accepted",
                "reason": row["reason"],
                "decided_at_ns": row["disposed_at_ns"],
            }
        return cast(
            dict[str, Any] | None,
            self.conn.execute(
                "SELECT disposition,disposition_reason AS reason,decided_at_ns FROM trading_operator_intents "
                "WHERE command_id=%s AND disposition IS NOT NULL",
                (input_id,),
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
    ) -> None:
        if kind == "signal":
            if disposition == "accepted":
                raise ValueError("accepted_entry_requires_plan")
            self.conn.execute(
                "UPDATE trading_entries SET state=%s,reason=%s,disposed_at_ns=%s,updated_at_ns=%s "
                "WHERE entry_id=%s AND account_slot=%s AND state='pending'",
                (disposition, reason, now_ns, now_ns, input_id, account_slot),
            )
        else:
            self.conn.execute(
                "UPDATE trading_operator_intents SET disposition=%s,disposition_reason=%s,decided_at_ns=%s "
                "WHERE command_id=%s AND account_slot=%s AND disposition IS NULL",
                (disposition, reason, now_ns, input_id, account_slot),
            )
        row = self.disposition(kind=kind, input_id=input_id)
        if row is None or (row["disposition"], row["reason"]) != (disposition, reason):
            raise ValueError("input_disposition_conflict")

    def newer_entry_stop_applied(self, *, account_slot: str, seq: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM trading_operator_intents WHERE account_slot=%s AND seq>%s "
                "AND action=ANY(%s) AND disposition='accepted' LIMIT 1",
                (account_slot, seq, list(ENTRY_STOP_ACTIONS)),
            ).fetchone()
            is not None
        )

    def accept_entry(
        self,
        *,
        entry_id: str,
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
    ) -> bool:
        values = (quantity, reference_price, quantity, reference_price, stop_bps, tp_bps, max_hold_s, now_ns, now_ns)
        if command_id is None:
            row = self.conn.execute(
                "UPDATE trading_entries SET quantity=%s,reference_price=%s,reserved_notional=%s::numeric * %s::numeric,"
                "stop_bps=%s,tp_bps=%s,max_hold_s=%s,state='accepted',pnl_status='pending',reason='accepted',"
                "disposed_at_ns=%s,updated_at_ns=%s WHERE entry_id=%s AND account_slot=%s AND native_symbol=%s "
                "AND side=%s AND state='pending' RETURNING entry_id",
                (*values, entry_id, account_slot, native_symbol, side),
            ).fetchone()
            return row is not None
        intent = self.conn.execute(
            "SELECT requested_at_ns,expires_at_ns,payload FROM trading_operator_intents "
            "WHERE command_id=%s AND account_slot=%s AND action='manual_entry' AND disposition IS NULL FOR UPDATE",
            (command_id, account_slot),
        ).fetchone()
        if intent is None:
            return False
        if (
            entry_id != command_id
            or intent["payload"]["market_key"] != f"crypto:perp:{native_symbol.removesuffix('USDT')}:USDT"
            or intent["payload"]["direction"] != side
        ):
            raise ValueError("manual_entry_identity_conflict")
        self.conn.execute(
            "INSERT INTO trading_entries(entry_id,source,command_id,account_slot,native_symbol,side,requested_at_ns,"
            "expires_at_ns,created_at_ns,quantity,reference_price,reserved_notional,stop_bps,tp_bps,max_hold_s,"
            "state,pnl_status,reason,disposed_at_ns,updated_at_ns) "
            "VALUES (%s,'manual',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::numeric * %s::numeric,%s,%s,%s,"
            "'accepted','pending','accepted',%s,%s)",
            (
                entry_id,
                command_id,
                account_slot,
                native_symbol,
                side,
                intent["requested_at_ns"],
                intent["expires_at_ns"],
                intent["requested_at_ns"],
                *values,
            ),
        )
        self.record_disposition(
            kind="intent",
            input_id=command_id,
            account_slot=account_slot,
            disposition="accepted",
            reason="accepted",
            now_ns=now_ns,
        )
        return True

    def reserve_order(
        self, *, client_id: str, entry_id: str, native_symbol: str, leg: str, attempt: int, now_ns: int
    ) -> dict[str, Any]:
        inserted = self.conn.execute(
            """
            INSERT INTO trading_orders(client_order_id,entry_id,environment,native_symbol,
                                       leg,attempt,status,updated_at_ns)
            VALUES (%s,%s,'DEMO',%s,%s,%s,'reserved',%s)
            ON CONFLICT(entry_id,leg,attempt) DO NOTHING RETURNING *
            """,
            (client_id, entry_id, native_symbol, leg, attempt, now_ns),
        ).fetchone()
        row = (
            inserted
            or self.conn.execute(
                "SELECT * FROM trading_orders WHERE entry_id=%s AND leg=%s AND attempt=%s",
                (entry_id, leg, attempt),
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
              AND (status,venue_order_id,error_code,evidence) IS DISTINCT FROM
                  (%s,COALESCE(%s,venue_order_id),%s,COALESCE(%s::jsonb,evidence))
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
                status,
                venue_order_id,
                error_code,
                None if evidence is None else json.dumps(evidence),
            ),
        )

    def active_entries(self, account_slot: str) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.conn.execute(
                "SELECT * FROM trading_entries WHERE account_slot=%s AND state IN "
                "('accepted','open','closing') ORDER BY updated_at_ns",
                (account_slot,),
            ).fetchall(),
        )

    def console_open_plans(self, account_slot: str) -> list[dict[str, Any]]:
        """Keep the diagnostic wire shape while the owned ledger uses entry identities."""
        return cast(
            list[dict[str, Any]],
            self.conn.execute(
                "SELECT entry_id,CASE WHEN source='signal' THEN entry_id END AS signal_id,"
                "command_id,account_slot,'DEMO' AS environment,native_symbol,side,quantity,reference_price,"
                "reserved_notional,stop_bps,tp_bps,max_hold_s,state,opened_at_ns,terminal_at_ns,"
                "terminal_reason,pnl_status,realized_pnl,fees,net_pnl,pnl_deadline_ns,updated_at_ns "
                "FROM trading_entries WHERE account_slot=%s AND state IN ('accepted','open','closing') "
                "ORDER BY updated_at_ns",
                (account_slot,),
            ).fetchall(),
        )

    def active_client_ids(self, account_slot: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT o.client_order_id FROM trading_orders o LEFT JOIN trading_entries p ON p.entry_id=o.entry_id "
            "LEFT JOIN trading_operator_intents i ON i.command_id=o.command_id "
            "WHERE (p.account_slot=%s AND p.state IN ('accepted','open','closing')) "
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
            "UPDATE trading_accounts SET last_full_reconcile_at_ns=%s,unexpected_exposure=%s,"
            "account_snapshot=COALESCE(%s::jsonb,account_snapshot) "
            "WHERE account_slot=%s",
            (now_ns, unexpected, None if account_snapshot is None else json.dumps(account_snapshot), account_slot),
        )

    def entries_awaiting_fills(self, account_slot: str) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.conn.execute(
                "SELECT * FROM trading_entries WHERE account_slot=%s AND state='terminal' "
                "AND pnl_status='pending' ORDER BY terminal_at_ns LIMIT 100",
                (account_slot,),
            ).fetchall(),
        )

    def entry(self, entry_id: str) -> dict[str, Any] | None:
        return cast(
            dict[str, Any] | None,
            self.conn.execute("SELECT * FROM trading_entries WHERE entry_id=%s", (entry_id,)).fetchone(),
        )

    def entry_orders(self, entry_id: str) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.conn.execute(
                "SELECT * FROM trading_orders WHERE entry_id=%s ORDER BY leg,attempt", (entry_id,)
            ).fetchall(),
        )

    def trade_cursor(self, symbol: str, *, account_slot: str) -> int | None:
        row = self.conn.execute(
            "SELECT trade_cursors->%s AS cursor FROM trading_accounts WHERE account_slot=%s", (symbol, account_slot)
        ).fetchone()
        return None if row is None or row["cursor"] is None else int(row["cursor"]["next_trade_id"])

    def advance_trade_cursor(self, *, account_slot: str, symbol: str, next_id: int, now_ns: int) -> None:
        self.conn.execute(
            "UPDATE trading_accounts SET trade_cursors=jsonb_set(trade_cursors,ARRAY[%s],"
            "jsonb_build_object('next_trade_id',GREATEST(COALESCE((trade_cursors->%s->>'next_trade_id')::bigint,0),%s),"
            "'checked_at_ns',%s)) WHERE account_slot=%s",
            (symbol, symbol, next_id, now_ns, account_slot),
        )

    def set_entry_state(
        self,
        *,
        entry_id: str,
        status: str,
        now_ns: int,
        opened_at_ns: int | None = None,
        terminal_reason: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE trading_entries SET state=%s,
              opened_at_ns=COALESCE(opened_at_ns,%s),
              terminal_at_ns=CASE WHEN %s='terminal' THEN COALESCE(terminal_at_ns,%s) ELSE terminal_at_ns END,
              terminal_reason=COALESCE(terminal_reason,%s),
              pnl_deadline_ns=CASE WHEN %s='terminal' THEN COALESCE(pnl_deadline_ns,%s) ELSE pnl_deadline_ns END,
              updated_at_ns=%s
            WHERE entry_id=%s AND (terminal_at_ns IS NULL OR %s='terminal')
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
                entry_id,
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
        rows = self.conn.execute(
            "UPDATE trading_fills f SET account_slot=COALESCE(e.account_slot,i.account_slot),"
            "client_order_id=o.client_order_id,attributed_at_ns=%s FROM trading_orders o "
            "LEFT JOIN trading_entries e ON e.entry_id=o.entry_id "
            "LEFT JOIN trading_operator_intents i ON i.command_id=o.command_id "
            "WHERE o.environment=f.environment AND o.native_symbol=f.native_symbol AND "
            "o.venue_order_id=f.venue_order_id "
            "AND f.environment='DEMO' AND f.native_symbol=%s AND f.client_order_id IS NULL RETURNING f.trade_id",
            (now_ns, symbol),
        ).fetchall()
        return len(rows)

    def exit_fill_client_id(self, entry_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT f.client_order_id FROM trading_fills f JOIN trading_orders o USING(client_order_id) "
            "WHERE o.entry_id=%s AND o.leg<>'entry' ORDER BY f.trade_id DESC LIMIT 1",
            (entry_id,),
        ).fetchone()
        return None if row is None else str(row["client_order_id"])

    def pending_pnl_entries(self, account_slot: str) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.conn.execute(
                """
            SELECT * FROM trading_entries WHERE account_slot=%s AND state='terminal' AND pnl_status='pending'
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
            LEFT JOIN trading_fills f ON f.client_order_id=o.client_order_id
            WHERE o.entry_id=%s GROUP BY o.leg
            """,
            (plan["entry_id"],),
        ).fetchall()
        entry_qty = sum(row["quantity"] for row in rows if row["leg"] == "entry")
        exit_qty = sum(row["quantity"] for row in rows if row["leg"] != "entry")
        entry_order = self.conn.execute(
            "SELECT status,evidence FROM trading_orders WHERE entry_id=%s AND leg='entry'",
            (plan["entry_id"],),
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
            UPDATE trading_entries SET pnl_status=%s,realized_pnl=%s,fees=%s,net_pnl=%s,updated_at_ns=%s
            WHERE entry_id=%s AND pnl_status='pending'
            """,
            (
                status,
                realized,
                fees if all(row["fees_in_usdt"] for row in rows) else None,
                realized - fees if complete else None,
                now_ns,
                plan["entry_id"],
            ),
        )
        return status

    def signal_ledger(self, *, since_ns: int, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(SIGNAL_LEDGER_SQL, (since_ns, limit)).fetchall()
        return [dict(row["request"]) for row in rows]

    def fill_ledger(self, *, since_ns: int, limit: int) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.conn.execute(FILL_LEDGER_SQL, (since_ns, limit)).fetchall())

    def console_operator_intents(self, *, since_ns: int, action: str | None, limit: int) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]], self.conn.execute(OPERATOR_INTENTS_SQL, (since_ns, action, action, limit)).fetchall()
        )

    def console_executions(self, *, since_ns: int, limit: int, case_id: str | None = None) -> list[dict[str, Any]]:
        from decimal import Decimal

        plans = self.conn.execute(EXECUTION_ENTRIES_SQL, (since_ns, case_id, case_id, limit)).fetchall()
        refused = self.conn.execute(EXECUTION_REFUSALS_SQL, (since_ns, case_id, case_id, limit)).fetchall()
        ids = [plan["entry_id"] for plan in plans]
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
            by_orders.setdefault(order["entry_id"], []).append(order)
        for fill in fills:
            by_fills.setdefault(fill["entry_id"], []).append(fill)
        result = []
        for plan in plans:
            own_orders = by_orders.get(plan["entry_id"], [])
            own_fills = by_fills.get(plan["entry_id"], [])
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
                    "entry_id": plan["entry_id"],
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
