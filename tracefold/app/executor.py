"""Single-process DEMO executor: short PostgreSQL transactions and REST reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import suppress
from dataclasses import asdict
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Literal, cast

import httpx
from psycopg.errors import UniqueViolation

from tracefold.app.repository_session import postgres_connection
from tracefold.integrations.trading.binance import BinanceFailure, DemoBinance
from tracefold.trading.executor.core import (
    EntryFacts,
    EntryRequest,
    EntryVerdict,
    PlanFacts,
    SignalV4,
    admit,
    client_order_id,
    step,
)
from tracefold.trading.storage.executor import ExecutorStorage

_LOG = logging.getLogger(__name__)
_NS_PER_SECOND = 1_000_000_000
_ORDER_WINDOW_NS = 7 * _NS_PER_SECOND


def _now_ns() -> int:
    return time.time_ns()


def _advisory_key(account_slot: str) -> int:
    digest = hashlib.sha256(("tracefold-executor|" + account_slot).encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def _symbol_rules(catalogue: dict[str, Any], symbol: str) -> dict[str, Decimal] | None:
    for item in catalogue.get("symbols", ()):
        if item.get("symbol") != symbol or item.get("status") != "TRADING" or item.get("contractType") != "PERPETUAL":
            continue
        filters = {row.get("filterType"): row for row in item.get("filters", ())}
        lot = filters.get("MARKET_LOT_SIZE") or {}
        price = filters.get("PRICE_FILTER") or {}
        notional = filters.get("MIN_NOTIONAL") or {}
        try:
            return {
                "min_qty": Decimal(str(lot["minQty"])),
                "max_qty": Decimal(str(lot["maxQty"])),
                "step": Decimal(str(lot["stepSize"])),
                "min_notional": Decimal(str(notional.get("notional", "5"))),
                "tick": Decimal(str(price["tickSize"])),
            }
        except (KeyError, ValueError):
            return None
    return None


def _trigger_price(*, entry_price: Decimal, bps: int, side: str, leg: str, tick: Decimal) -> Decimal:
    sign = -1 if (side == "long") == (leg == "sl") else 1
    raw = entry_price * (1 + Decimal(sign * bps) / 10_000)
    rounding = ROUND_DOWN if sign < 0 else ROUND_UP
    return (raw / tick).to_integral_value(rounding=rounding) * tick


def _ownership(
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    algos: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    client_ids: set[str],
) -> tuple[set[str], set[str], set[str]]:
    by_symbol = {plan["native_symbol"]: plan for plan in plans}
    symbols = {row["symbol"] for row in positions if Decimal(str(row["positionAmt"])) != 0} | {
        row["symbol"] for row in (*orders, *algos)
    }
    foreign_orders = {
        str(row.get("clientOrderId") or "") for row in orders if str(row.get("clientOrderId") or "") not in client_ids
    } | {str(row.get("clientAlgoId") or "") for row in algos if str(row.get("clientAlgoId") or "") not in client_ids}
    incompatible = {
        str(row["symbol"])
        for row in positions
        if Decimal(str(row["positionAmt"])) != 0
        and (plan := by_symbol.get(str(row["symbol"]))) is not None
        and (
            (Decimal(str(row["positionAmt"])) > 0) != (plan["side"] == "long")
            or abs(Decimal(str(row["positionAmt"]))) > Decimal(str(plan["quantity"]))
            or row.get("positionSide", "BOTH") != "BOTH"
        )
    }
    return symbols - set(by_symbol), foreign_orders, incompatible


class ExecutorRunner:
    def __init__(self, *, settings: Any, conn: Any, venue: DemoBinance) -> None:
        if settings.trading.execution.binance.environment != "DEMO":
            raise ValueError("executor_demo_only")
        self.settings = settings
        self.conn = conn
        self.db = ExecutorStorage(conn)
        self.venue = venue
        self.account_slot = settings.trading.execution.account_slot
        self._last_active_read_ns = 0
        self._last_full_read_ns = 0

    async def tick(self) -> None:
        now = _now_ns()
        with self.conn.transaction():
            self.db.heartbeat(account_slot=self.account_slot, now_ns=now)
        if now - self._last_active_read_ns >= 5 * _NS_PER_SECOND:
            await self._reconcile(now)
            self._last_active_read_ns = now
        if now - self._last_full_read_ns >= 60 * _NS_PER_SECOND:
            try:
                await self._full_account_check(now)
            except (httpx.HTTPError, BinanceFailure) as exc:
                with self.conn.transaction():
                    self.db.set_fault(
                        account_slot=self.account_slot, key="account", reason=type(exc).__name__, now_ns=now
                    )
                _LOG.warning("executor_account_reconcile_deferred: %s", type(exc).__name__)
            self._last_full_read_ns = now
        await self._one_intent(now)
        await self._one_signal(now)
        with self.conn.transaction():
            self.db.clear_fault(account_slot=self.account_slot, key="cycle")
            for plan in self.db.pending_pnl_plans(self.account_slot):
                self.db.settle_pnl(plan=plan, now_ns=now)

    async def _one_signal(self, now: int) -> None:
        state = self.db.state(self.account_slot)
        if state is None:
            raise RuntimeError("executor_state_missing")
        signal = self.db.next_signal(account_slot=self.account_slot, after_seq=int(state["last_signal_seq"]))
        if signal is None:
            return
        existing = self.db.disposition(kind="signal", input_id=signal.signal_id)
        if existing is not None:
            with self.conn.transaction():
                self.db.advance_cursor(account_slot=self.account_slot, kind="signal", seq=signal.seq)
            return
        if now >= signal.expires_at_ns:
            self._refuse_signal(signal, now=now, disposition="expired", reason="expired")
            return
        try:
            facts = await self._entry_facts(signal)
        except (httpx.HTTPError, BinanceFailure, KeyError, ValueError) as exc:
            _LOG.warning("executor_admission_unavailable: %s", type(exc).__name__)
            return
        if facts is None:
            self._refuse_signal(signal, now=now, disposition="refused", reason="execution_venue_unlisted")
            return
        verdict = admit(signal, facts)
        if not verdict.accepted or verdict.quantity is None:
            disposition = "expired" if verdict.reason == "expired" else "refused"
            self._refuse_signal(
                signal,
                now=now,
                disposition=disposition,
                reason=verdict.reason,
                admission_snapshot=self._admission_snapshot(facts, verdict, signal),
            )
            return
        await self._enter(signal, facts, verdict, kind="signal", input_id=signal.signal_id, seq=signal.seq)

    async def _enter(
        self,
        signal: EntryRequest,
        facts: EntryFacts,
        verdict: EntryVerdict,
        *,
        kind: Literal["signal", "intent"],
        input_id: str,
        seq: int,
    ) -> None:
        """One transaction owns plan, risk reservation, order intent and disposition."""
        if verdict.quantity is None or verdict.reserved_margin_usdt is None:
            raise ValueError("entry_reservation_missing")
        now = facts.now_ns
        snapshot = self._admission_snapshot(facts, verdict, signal)
        entry_id = client_order_id(namespace=self.account_slot, entry_id=input_id, leg="entry", attempt=1)
        executable = facts.ask if signal.side == "long" else facts.bid
        try:
            with self.conn.transaction():
                self.db.create_plan(
                    plan_id=input_id,
                    signal_id=input_id if kind == "signal" else None,
                    command_id=input_id if kind == "intent" else None,
                    account_slot=self.account_slot,
                    native_symbol=signal.native_symbol,
                    side=signal.side,
                    quantity=str(verdict.quantity),
                    reference_price=str(signal.reference_price),
                    stop_bps=signal.stop_bps,
                    tp_bps=signal.tp_bps,
                    max_hold_s=signal.max_hold_s,
                    now_ns=now,
                    admission_snapshot=snapshot,
                    reserved_margin=str(verdict.reserved_margin_usdt),
                    reserved_notional=str(verdict.quantity * executable),
                )
                self.db.reserve_order(
                    client_id=entry_id,
                    plan_id=input_id,
                    native_symbol=signal.native_symbol,
                    leg="entry",
                    attempt=1,
                    now_ns=now,
                )
                self.db.record_disposition(
                    kind=kind,
                    input_id=input_id,
                    account_slot=self.account_slot,
                    disposition="accepted",
                    reason="accepted",
                    plan_id=input_id,
                    now_ns=now,
                    admission_snapshot=snapshot,
                )
                self.db.advance_cursor(account_slot=self.account_slot, kind=kind, seq=seq)
        except UniqueViolation:
            with self.conn.transaction():
                self.db.record_disposition(
                    kind=kind,
                    input_id=input_id,
                    account_slot=self.account_slot,
                    disposition="refused",
                    reason="symbol_exposure",
                    now_ns=now,
                    admission_snapshot=snapshot,
                )
                self.db.advance_cursor(account_slot=self.account_slot, kind=kind, seq=seq)
            return
        await self._send_market(
            plan_id=input_id,
            symbol=signal.native_symbol,
            side="BUY" if signal.side == "long" else "SELL",
            quantity=verdict.quantity,
            client_id=entry_id,
            reduce_only=False,
            now=now,
        )

    @staticmethod
    def _admission_snapshot(facts: EntryFacts, verdict: EntryVerdict, request: EntryRequest) -> dict[str, Any]:
        return {
            "version": "entry_admission_v2",
            "entry_request": request.model_dump(mode="json"),
            "price_comparison": {
                "live_reference_at_ns": request.reference_at_ns,
                "time_change_bps": str((facts.live_mid_price / request.reference_price - 1) * 10000)
                if facts.live_mid_price.is_finite() and facts.live_mid_price > 0
                else None,
                "demo_basis_bps": str(
                    ((facts.ask if request.side == "long" else facts.bid) / facts.live_mid_price - 1) * 10000
                )
                if facts.live_mid_price.is_finite() and facts.live_mid_price > 0
                else None,
            },
            "facts": {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(facts).items()},
            "verdict": {
                key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(verdict).items()
            },
        }

    def _refuse_signal(
        self,
        signal: SignalV4,
        *,
        now: int,
        disposition: str,
        reason: str,
        admission_snapshot: dict[str, Any] | None = None,
    ) -> None:
        with self.conn.transaction():
            self.db.record_disposition(
                kind="signal",
                input_id=signal.signal_id,
                account_slot=self.account_slot,
                disposition=disposition,
                reason=reason,
                now_ns=now,
                admission_snapshot=admission_snapshot,
            )
            self.db.advance_cursor(account_slot=self.account_slot, kind="signal", seq=signal.seq)

    async def _entry_facts(self, signal: EntryRequest) -> EntryFacts | None:
        quote_requested_at_ns = _now_ns()
        positions, orders, algos, account, mode, quote, catalogue, margin, mark, fee, live_quote = await asyncio.gather(
            self.venue.positions(),
            self.venue.open_orders(),
            self.venue.open_algo_orders(),
            self.venue.account(),
            self.venue.account_config(),
            self.venue.book_ticker(signal.native_symbol),
            self.venue.exchange_info(),
            self.venue.symbol_config(signal.native_symbol),
            self.venue.mark_price(signal.native_symbol),
            self.venue.commission_rate(signal.native_symbol),
            self.venue.live_book_ticker(signal.native_symbol),
        )
        positions = cast(list[dict[str, Any]], positions)
        orders = cast(list[dict[str, Any]], orders)
        algos = cast(list[dict[str, Any]], algos)
        account = cast(dict[str, Any], account)
        mode = cast(dict[str, Any], mode)
        quote = cast(dict[str, Any], quote)
        catalogue = cast(dict[str, Any], catalogue)
        rules = _symbol_rules(catalogue, signal.native_symbol)
        if rules is None:
            return None
        active = self.db.active_plans(self.account_slot)
        ownership = _ownership(positions, orders, algos, active, self.db.active_client_ids(self.account_slot))
        unexpected = any(ownership)
        if unexpected:
            with self.conn.transaction():
                self.db.set_control(
                    account_slot=self.account_slot,
                    paused=True,
                    halted=bool(self.db.control(self.account_slot)["emergency_halted"]),
                    now_ns=_now_ns(),
                )
                self.db.record_full_reconciliation(
                    account_slot=self.account_slot,
                    now_ns=_now_ns(),
                    unexpected=True,
                )
        position_by_symbol = {row["symbol"]: row for row in positions if Decimal(str(row["positionAmt"])) != 0}
        notional = sum(
            (
                abs(Decimal(str(row["positionAmt"])) * Decimal(str(row["markPrice"])))
                for row in position_by_symbol.values()
            ),
            Decimal(0),
        )
        notional += sum(plan["reserved_notional"] for plan in active if plan["native_symbol"] not in position_by_symbol)
        risk = self.settings.trading.execution.risk
        represented_symbols = set(position_by_symbol) | {str(row["symbol"]) for row in orders}
        pending = [plan for plan in active if plan["native_symbol"] not in represented_symbols]
        pending_margin = (
            None
            if any(plan["reserved_margin"] is None for plan in pending)
            else sum((Decimal(str(plan["reserved_margin"])) for plan in pending), Decimal(0))
        )
        stopped_at = self.db.last_stop_at_ns(self.account_slot, signal.native_symbol)
        limit = str(margin["maxNotionalValue"])
        if any(
            key not in mode or not isinstance(mode[key], bool)
            for key in ("dualSidePosition", "multiAssetsMargin", "canTrade")
        ):
            raise ValueError("account_mode_evidence_invalid")
        control = self.db.control(self.account_slot)
        state = self.db.state(self.account_slot)
        return EntryFacts(
            now_ns=_now_ns(),
            entries_paused=bool(control["entries_paused"])
            or unexpected
            or bool(state and (state["unexpected_exposure"] or state["faults"])),
            emergency_halted=bool(control["emergency_halted"]),
            symbol_position=Decimal(str(position_by_symbol.get(signal.native_symbol, {}).get("positionAmt", "0"))),
            symbol_regular_orders=sum(row["symbol"] == signal.native_symbol for row in orders),
            symbol_algo_orders=sum(row["symbol"] == signal.native_symbol for row in algos),
            active_plans=len(active),
            symbol_active_plans=sum(plan["native_symbol"] == signal.native_symbol for plan in active),
            max_plans=risk.max_plans,
            equity_usdt=Decimal(str(account["totalMarginBalance"])),
            active_notional_usdt=notional,
            max_leverage=risk.max_leverage,
            risk_fraction=risk.risk_fraction_per_trade,
            available_margin_usdt=Decimal(str(account["availableBalance"])),
            pending_margin_usdt=pending_margin,
            venue_leverage=int(margin["leverage"]),
            margin_mode=str(margin["marginType"]),
            mark_price=cast(Decimal, mark),
            fee_bps=cast(Decimal, fee) * 10_000,
            price_buffer_bps=risk.entry_price_buffer_bps,
            max_spread_fraction_of_stop=risk.max_spread_fraction_of_stop,
            live_mid_price=(Decimal(str(live_quote["bidPrice"])) + Decimal(str(live_quote["askPrice"]))) / 2,
            live_quote_at_ns=quote_requested_at_ns,
            account_at_ns=quote_requested_at_ns,
            account_max_age_ns=int(risk.market_stale_after_seconds * _NS_PER_SECOND),
            cooldown_until_ns=0
            if stopped_at is None
            else stopped_at + risk.post_stop_cooldown_seconds * _NS_PER_SECOND,
            bid=Decimal(str(quote["bidPrice"])),
            ask=Decimal(str(quote["askPrice"])),
            quote_at_ns=quote_requested_at_ns,
            quote_max_age_ns=int(risk.market_stale_after_seconds * _NS_PER_SECOND),
            market_min_qty=rules["min_qty"],
            market_max_qty=rules["max_qty"],
            market_step=rules["step"],
            min_notional=rules["min_notional"],
            venue_max_notional_usdt=None if limit == "INF" else Decimal(limit),
            multi_assets_mode=mode["multiAssetsMargin"],
            can_trade=mode["canTrade"],
            hedge_mode=mode["dualSidePosition"] or any(row.get("positionSide", "BOTH") != "BOTH" for row in positions),
        )

    async def _send_market(
        self,
        *,
        plan_id: str | None,
        symbol: str,
        side: Literal["BUY", "SELL"],
        quantity: Decimal,
        client_id: str,
        reduce_only: bool,
        now: int,
    ) -> None:
        now = max(now, _now_ns())
        with self.conn.transaction():
            self.db.update_order(
                client_id=client_id,
                status="submitted",
                now_ns=now,
                evidence={
                    "submission": "started",
                    "request": {
                        "symbol": symbol,
                        "side": side,
                        "quantity": str(quantity),
                        "reduce_only": reduce_only,
                    },
                },
            )
        try:
            result = await self.venue.market_order(
                symbol=symbol, side=side, quantity=quantity, client_id=client_id, reduce_only=reduce_only
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            now = max(now, _now_ns())
            with self.conn.transaction():
                self.db.update_order(client_id=client_id, status="unknown", now_ns=now)
            _LOG.warning("executor_order_unknown %s: %s", client_id, type(exc).__name__)
            return
        except BinanceFailure as exc:
            now = max(now, _now_ns())
            status = "unknown" if exc.transient else "rejected"
            with self.conn.transaction():
                self.db.update_order(
                    client_id=client_id, status=status, now_ns=now, error_code=exc.code, evidence=exc.evidence()
                )
                if plan_id is not None and not reduce_only and status == "rejected":
                    self.db.set_plan_status(
                        plan_id=plan_id, status="terminal", now_ns=now, terminal_reason="entry_rejected"
                    )
            return
        now = max(now, _now_ns())
        status = "filled" if result.get("status") == "FILLED" else "working"
        with self.conn.transaction():
            self.db.update_order(
                client_id=client_id,
                status=status,
                now_ns=now,
                venue_order_id=str(result["orderId"]),
                evidence=result,
            )
            if plan_id is not None and not reduce_only and status == "filled":
                self.db.set_plan_status(plan_id=plan_id, status="open", now_ns=now, opened_at_ns=now)

    async def _one_intent(self, now: int) -> None:
        state = self.db.state(self.account_slot)
        if state is None:
            raise RuntimeError("executor_state_missing")
        intent = self.db.next_intent(account_slot=self.account_slot, after_seq=int(state["last_intent_seq"]))
        if intent is None:
            return
        command_id = str(intent["command_id"])
        if self.db.disposition(kind="intent", input_id=command_id) is not None:
            with self.conn.transaction():
                self.db.advance_cursor(account_slot=self.account_slot, kind="intent", seq=int(intent["seq"]))
            return
        if now >= int(intent["expires_at_ns"]):
            with self.conn.transaction():
                self.db.record_disposition(
                    kind="intent",
                    input_id=command_id,
                    account_slot=self.account_slot,
                    disposition="expired",
                    reason="expired",
                    now_ns=now,
                )
                self.db.advance_cursor(account_slot=self.account_slot, kind="intent", seq=int(intent["seq"]))
            return
        action = intent["action"]
        control = self.db.control(self.account_slot)
        if action in ("pause_entries", "resume_entries", "emergency_halt"):
            halted = action == "emergency_halt" or (action != "resume_entries" and bool(control["emergency_halted"]))
            paused = action != "resume_entries" or halted
            with self.conn.transaction():
                self.db.set_control(account_slot=self.account_slot, paused=paused, halted=halted, now_ns=now)
                self.db.record_disposition(
                    kind="intent",
                    input_id=command_id,
                    account_slot=self.account_slot,
                    disposition="accepted",
                    reason="control_applied",
                    now_ns=now,
                )
                self.db.advance_cursor(account_slot=self.account_slot, kind="intent", seq=int(intent["seq"]))
            return
        if action == "flatten":
            with self.conn.transaction():
                self.db.request_flatten(account_slot=self.account_slot, command_id=command_id, now_ns=now)
                self.db.record_disposition(
                    kind="intent",
                    input_id=command_id,
                    account_slot=self.account_slot,
                    disposition="accepted",
                    reason="flatten_requested",
                    now_ns=now,
                )
                self.db.advance_cursor(account_slot=self.account_slot, kind="intent", seq=int(intent["seq"]))
            return
        if action == "manual_entry":
            await self._manual_entry(intent, now)
            return
        raise ValueError("operator_action_invalid")

    async def _manual_entry(self, intent: dict[str, Any], now: int) -> None:
        command_id = str(intent["command_id"])
        payload = intent["payload"]
        market = str(payload.get("market_key") or "")
        side = payload.get("direction")
        parts = market.split(":")
        if len(parts) != 4 or parts[0:2] != ["crypto", "perp"] or parts[3] != "USDT" or side not in ("long", "short"):
            self._refuse_intent(intent, now=now, reason="manual_market_invalid")
            return
        symbol = parts[2] + "USDT"
        try:
            live = await self.venue.live_book_ticker(symbol)
            mid = (Decimal(str(live["bidPrice"])) + Decimal(str(live["askPrice"]))) / 2
            risk = self.settings.trading.execution.risk
            exit_policy = self.settings.trading.execution.exit_policy
            signal = EntryRequest(
                account_slot=self.account_slot,
                asset_id="crypto:" + parts[2],
                native_symbol=symbol,
                side=side,
                reference_price=mid,
                reference_at_ns=_now_ns(),
                max_drift_bps=risk.max_drift_bps,
                stop_bps=risk.stop_distance_bps,
                tp_bps=exit_policy.take_profit_bps if exit_policy else 2 * risk.stop_distance_bps,
                max_hold_s=exit_policy.max_holding_seconds if exit_policy else 14400,
                geometry_version="manual_v1",
                decided_at_ns=int(intent["requested_at_ns"]),
                expires_at_ns=int(intent["expires_at_ns"]),
            )
            facts = await self._entry_facts(signal)
        except (httpx.HTTPError, BinanceFailure, KeyError, ValueError) as exc:
            _LOG.warning("executor_manual_admission_unavailable: %s", type(exc).__name__)
            return
        if facts is None:
            self._refuse_intent(intent, now=now, reason="execution_venue_unlisted")
            return
        verdict = admit(signal, facts)
        if not verdict.accepted or verdict.quantity is None:
            self._refuse_intent(
                intent,
                now=now,
                reason=verdict.reason,
                admission_snapshot=self._admission_snapshot(facts, verdict, signal),
            )
            return
        await self._enter(signal, facts, verdict, kind="intent", input_id=command_id, seq=int(intent["seq"]))

    def _refuse_intent(
        self, intent: dict[str, Any], *, now: int, reason: str, admission_snapshot: dict[str, Any] | None = None
    ) -> None:
        with self.conn.transaction():
            self.db.record_disposition(
                kind="intent",
                input_id=str(intent["command_id"]),
                account_slot=self.account_slot,
                disposition="refused",
                reason=reason,
                now_ns=now,
                admission_snapshot=admission_snapshot,
            )
            self.db.advance_cursor(account_slot=self.account_slot, kind="intent", seq=int(intent["seq"]))

    async def _full_account_check(self, now: int) -> None:
        positions, orders, algos, account = await asyncio.gather(
            self.venue.positions(), self.venue.open_orders(), self.venue.open_algo_orders(), self.venue.account()
        )
        active_plans = self.db.active_plans(self.account_slot)
        active_symbols = {plan["native_symbol"] for plan in active_plans}
        known_client_ids = self.db.active_client_ids(self.account_slot)
        unexpected_symbols, unexpected_orders, incompatible_positions = _ownership(
            positions,
            orders,
            algos,
            active_plans,
            known_client_ids,
        )
        unexpected = bool(unexpected_symbols or unexpected_orders or incompatible_positions)
        observed_at_ns = _now_ns()
        nonzero_positions = [row for row in positions if Decimal(str(row["positionAmt"])) != 0]
        snapshot = {
            "observed_at_ns": observed_at_ns,
            "equity_usdt": str(account["totalMarginBalance"]),
            "positions_total": len(nonzero_positions),
            "orders_total": len(orders),
            "algos_total": len(algos),
            "complete": len(nonzero_positions) <= 100 and len(orders) <= 100 and len(algos) <= 100,
            "positions": [
                {
                    **{
                        key: row.get(key)
                        for key in (
                            "symbol",
                            "positionSide",
                            "positionAmt",
                            "entryPrice",
                            "markPrice",
                            "unRealizedProfit",
                        )
                    },
                    "owned": row["symbol"] in active_symbols,
                }
                for row in nonzero_positions[:100]
            ],
            "orders": [
                {
                    **{
                        key: row.get(key)
                        for key in ("symbol", "clientOrderId", "side", "origQty", "reduceOnly", "status")
                    },
                    "owned": str(row.get("clientOrderId") or "") in known_client_ids,
                }
                for row in orders[:100]
            ],
            "algos": [
                {
                    **{
                        key: row.get(key)
                        for key in (
                            "symbol",
                            "clientAlgoId",
                            "orderType",
                            "triggerPrice",
                            "quantity",
                            "closePosition",
                            "reduceOnly",
                            "algoStatus",
                        )
                    },
                    "owned": str(row.get("clientAlgoId") or "") in known_client_ids,
                }
                for row in algos[:100]
            ],
        }
        with self.conn.transaction():
            if unexpected:
                self.db.set_control(
                    account_slot=self.account_slot,
                    paused=True,
                    halted=bool(self.db.control(self.account_slot)["emergency_halted"]),
                    now_ns=observed_at_ns,
                )
            self.db.record_full_reconciliation(
                account_slot=self.account_slot,
                now_ns=observed_at_ns,
                unexpected=unexpected,
                account_snapshot=snapshot,
            )
            self.db.clear_fault(account_slot=self.account_slot, key="account")
        if unexpected:
            _LOG.error(
                "executor_unclaimed_venue_exposure: symbols=%s orders=%s",
                sorted(unexpected_symbols | incompatible_positions),
                sorted(unexpected_orders),
            )

    async def _reconcile(self, now: int) -> None:
        plans = self.db.active_plans(self.account_slot)
        awaiting_fills = self.db.plans_awaiting_fills(self.account_slot)
        control = self.db.control(self.account_slot)
        if control.get("flatten_command_id"):
            try:
                await self._reconcile_account_flatten(str(control["flatten_command_id"]), plans, now)
            except (httpx.HTTPError, BinanceFailure) as exc:
                with self.conn.transaction():
                    self.db.set_fault(
                        account_slot=self.account_slot,
                        key=str(control["flatten_command_id"]) + ":account",
                        reason=type(exc).__name__,
                        now_ns=now,
                    )
                _LOG.warning("executor_account_flatten_deferred: %s", type(exc).__name__)
        if not plans:
            for symbol in sorted({plan["native_symbol"] for plan in awaiting_fills}):
                await self._sync_trades_safely(symbol, now)
            return
        positions = await self.venue.positions()
        try:
            open_algos = await self.venue.open_algo_orders()
        except (httpx.HTTPError, BinanceFailure) as exc:
            # Individual queries still resolve known IDs. Missing snapshot is
            # never proof that a protection order was cancelled.
            open_algos = []
            _LOG.warning("executor_algo_snapshot_deferred: %s", type(exc).__name__)
        positions_by_symbol = {row["symbol"]: row for row in positions}
        algos_by_client_id = {row["clientAlgoId"]: row for row in open_algos}

        async def manage_symbol(symbol_plans: list[dict[str, Any]]) -> None:
            # Keep same-symbol writes ordered; independent positions must not
            # wait for another symbol's slow order evidence.
            for plan in symbol_plans:
                try:
                    await self._refresh_plan_orders(plan, algos_by_client_id, now)
                except (httpx.HTTPError, BinanceFailure) as exc:
                    _LOG.warning("executor_order_refresh_deferred %s: %s", plan["plan_id"], type(exc).__name__)
                position = positions_by_symbol.get(plan["native_symbol"])
                if position is not None and Decimal(str(position["positionAmt"])) != 0:
                    await self._step_plan_safely(plan, position, algos_by_client_id, control, now)

        grouped: dict[str, list[dict[str, Any]]] = {}
        for plan in plans:
            grouped.setdefault(plan["native_symbol"], []).append(plan)
        # Actual exposure gets protection/exit before any historical fill I/O.
        async with asyncio.TaskGroup() as management:
            for symbol_plans in grouped.values():
                management.create_task(manage_symbol(symbol_plans))
        for symbol in sorted({plan["native_symbol"] for plan in (*plans, *awaiting_fills)}):
            await self._sync_trades_safely(symbol, now)
        # Flat settlement may now use newly attributed native exit evidence.
        for plan in plans:
            position = positions_by_symbol.get(plan["native_symbol"])
            if position is not None and Decimal(str(position["positionAmt"])) != 0:
                continue
            await self._step_plan_safely(plan, position, algos_by_client_id, control, now)

    async def _step_plan_safely(
        self,
        plan: dict[str, Any],
        position: dict[str, Any] | None,
        algos: dict[str, dict[str, Any]],
        control: dict[str, Any],
        now: int,
    ) -> None:
        try:
            await self._step_plan(
                plan=plan,
                position=position,
                open_algos=algos,
                force_flatten=bool(control.get("flatten_command_id")),
                now=now,
            )
        except (httpx.HTTPError, BinanceFailure) as exc:
            _LOG.warning("executor_reconcile_deferred %s: %s", plan["plan_id"], type(exc).__name__)

    async def _sync_trades_safely(self, symbol: str, now: int) -> None:
        try:
            await self._sync_trades(symbol, now)
        except (httpx.HTTPError, BinanceFailure) as exc:
            _LOG.warning("executor_fills_deferred %s: %s", symbol, type(exc).__name__)

    async def _reconcile_account_flatten(self, command_id: str, plans: list[dict[str, Any]], now: int) -> None:
        positions, orders, algos = await asyncio.gather(
            self.venue.positions(), self.venue.open_orders(), self.venue.open_algo_orders()
        )
        if any(
            Decimal(str(row["positionAmt"])) != 0 and row.get("positionSide", "BOTH") != "BOTH" for row in positions
        ):
            with self.conn.transaction():
                self.db.set_fault(
                    account_slot=self.account_slot,
                    key=command_id + ":account",
                    reason="flatten_requires_one_way_mode",
                    now_ns=now,
                )
            _LOG.critical("executor_flatten_requires_one_way_position_mode")
            return
        # Ordinary resting orders may increase exposure after a flatten. Cancel and verify them first.
        for symbol in sorted({str(row["symbol"]) for row in orders}):
            await self.venue.cancel_symbol_orders(symbol)
        if orders and await self.venue.open_orders():
            return

        owned = {str(plan["native_symbol"]) for plan in plans}
        for position in positions:
            amount = Decimal(str(position["positionAmt"]))
            symbol = str(position["symbol"])
            if amount == 0 or symbol in owned:
                continue
            prior = self.db.external_flatten_orders(command_id, symbol)
            if prior:
                latest = prior[-1]
                if (
                    latest["status"] == "reserved"
                    and latest["submitted_at_ns"] is None
                    and (latest["evidence"] or {}).get("submission") == "not_started"
                ):
                    await self._send_market(
                        plan_id=None,
                        symbol=symbol,
                        side="SELL" if amount > 0 else "BUY",
                        quantity=abs(amount),
                        client_id=latest["client_order_id"],
                        reduce_only=True,
                        now=now,
                    )
                    await self._sync_trades_safely(symbol, now)
                    continue
                if latest["status"] in ("reserved", "unknown", "submitted", "working"):
                    evidence = await self.venue.query_order(symbol, latest["client_order_id"])
                    if evidence is not None:
                        remote = str(evidence.get("status", ""))
                        status = (
                            "filled"
                            if remote == "FILLED"
                            else "rejected"
                            if remote == "REJECTED"
                            else "cancelled"
                            if remote in ("CANCELED", "EXPIRED")
                            else "working"
                        )
                        with self.conn.transaction():
                            self.db.update_order(
                                client_id=latest["client_order_id"],
                                status=status,
                                now_ns=now,
                                venue_order_id=str(evidence["orderId"]),
                                evidence=evidence,
                            )
                        if status in ("rejected", "cancelled"):
                            with self.conn.transaction():
                                self.db.set_fault(
                                    account_slot=self.account_slot,
                                    key=command_id + ":" + symbol,
                                    reason="external_flatten_rejected",
                                    now_ns=now,
                                )
                            _LOG.critical("executor_external_flatten_terminal_without_flat %s", symbol)
                            continue
                        if status != "filled":
                            continue
                    elif now - latest["updated_at_ns"] >= _ORDER_WINDOW_NS:
                        with self.conn.transaction():
                            self.db.set_fault(
                                account_slot=self.account_slot,
                                key=command_id + ":" + symbol,
                                reason="flatten_submission_unresolved",
                                now_ns=now,
                            )
                        continue
                    else:
                        continue
                elif latest["status"] == "rejected":
                    with self.conn.transaction():
                        self.db.set_fault(
                            account_slot=self.account_slot,
                            key=command_id + ":" + symbol,
                            reason="external_flatten_rejected",
                            now_ns=now,
                        )
                    _LOG.critical("executor_external_flatten_rejected %s", symbol)
                    continue
            attempt = len(prior) + 1
            if attempt > 3:
                with self.conn.transaction():
                    self.db.set_fault(
                        account_slot=self.account_slot,
                        key=command_id + ":" + symbol,
                        reason="external_flatten_exhausted",
                        now_ns=now,
                    )
                _LOG.critical("executor_external_flatten_exhausted %s", symbol)
                continue
            client_id = client_order_id(
                namespace=self.account_slot,
                entry_id=f"{command_id}|{symbol}",
                leg="account_flatten",
                attempt=attempt,
            )
            with self.conn.transaction():
                self.db.reserve_external_flatten(
                    client_id=client_id, command_id=command_id, symbol=symbol, attempt=attempt, now_ns=now
                )
            await self._send_market(
                plan_id=None,
                symbol=symbol,
                side="SELL" if amount > 0 else "BUY",
                quantity=abs(amount),
                client_id=client_id,
                reduce_only=True,
                now=now,
            )
            await self._sync_trades_safely(symbol, now)

        if any(Decimal(str(row["positionAmt"])) != 0 for row in positions):
            return
        for symbol in sorted({str(row["symbol"]) for row in algos}):
            await self.venue.cancel_symbol_algo_orders(symbol)
        positions, orders, algos = await asyncio.gather(
            self.venue.positions(), self.venue.open_orders(), self.venue.open_algo_orders()
        )
        if not any(Decimal(str(row["positionAmt"])) != 0 for row in positions) and not orders and not algos:
            with self.conn.transaction():
                self.db.clear_flatten(account_slot=self.account_slot)
                self.db.clear_flatten_faults(account_slot=self.account_slot, command_id=command_id)

    async def _sync_trades(self, symbol: str, now: int) -> None:
        cursor = self.db.trade_cursor(symbol)
        if cursor in (None, 0):
            with self.conn.transaction():
                start = self.db.initialize_trade_window(symbol=symbol, now_ns=now)
            trades = await self.venue.user_trades(symbol, start_time_ms=start // 1000000)
        else:
            trades = await self.venue.user_trades(symbol, from_id=cursor)
        with self.conn.transaction():
            for trade in trades:
                self.db.record_fill(symbol=symbol, trade=trade)
            self.db.attribute_unbound_fills(symbol=symbol, now_ns=now)
            if trades:
                self.db.advance_trade_cursor(
                    symbol=symbol, next_id=max(int(row["id"]) for row in trades) + 1, now_ns=now
                )

    async def _refresh_plan_orders(self, plan: dict[str, Any], open_algos: dict[str, dict[str, Any]], now: int) -> None:
        for order in self.db.plan_orders(plan["plan_id"]):
            client_id = order["client_order_id"]
            if order["leg"] in ("sl", "tp"):
                evidence = open_algos.get(client_id)
                if evidence is None:
                    evidence = await self.venue.query_algo(client_id)
                if evidence is None:
                    status = None
                else:
                    remote = str(evidence.get("algoStatus", ""))
                    status = (
                        "working"
                        if remote in ("NEW", "PARTIALLY_FILLED", "TRIGGERING")
                        else "filled"
                        if remote in ("TRIGGERED", "FINISHED", "FILLED")
                        else "cancelled"
                        if remote in ("CANCELED", "EXPIRED")
                        else "rejected"
                    )
                    venue_id = evidence.get("actualOrderId") or None
            else:
                evidence = await self.venue.query_order(plan["native_symbol"], client_id)
                if evidence is None:
                    status = None
                else:
                    remote = str(evidence.get("status", ""))
                    status = (
                        "filled"
                        if remote == "FILLED"
                        else "cancelled"
                        if remote in ("CANCELED", "EXPIRED")
                        else "rejected"
                        if remote == "REJECTED"
                        else "working"
                    )
                    venue_id = evidence.get("orderId")
            if evidence is not None:
                if status is None:
                    raise RuntimeError("order_status_missing_from_venue_evidence")
                with self.conn.transaction():
                    self.db.update_order(
                        client_id=client_id,
                        status=status,
                        now_ns=now,
                        venue_order_id=None if venue_id is None else str(venue_id),
                        evidence=evidence,
                    )
                    self.db.clear_fault(account_slot=self.account_slot, key=plan["plan_id"] + ":" + order["leg"])
            elif (
                order["status"] in ("reserved", "unknown", "submitted")
                and now - order["updated_at_ns"] >= _ORDER_WINDOW_NS
            ):
                never_sent = (
                    order["status"] == "reserved"
                    and order["submitted_at_ns"] is None
                    and (order["evidence"] or {}).get("submission") == "not_started"
                )
                with self.conn.transaction():
                    if never_sent:
                        self.db.update_order(
                            client_id=client_id,
                            status="not_submitted",
                            now_ns=now,
                            evidence={"submission": "not_started", "resolution": "local_dispatch_checkpoint"},
                        )
                    else:
                        self.db.set_fault(
                            account_slot=self.account_slot,
                            key=plan["plan_id"] + ":" + order["leg"],
                            reason="submission_unresolved",
                            now_ns=now,
                        )

    async def _step_plan(
        self,
        *,
        plan: dict[str, Any],
        position: dict[str, Any] | None,
        open_algos: dict[str, dict[str, Any]],
        force_flatten: bool,
        now: int,
    ) -> None:
        current = self.db.plan(plan["plan_id"])
        if current is None or current["terminal_at_ns"] is not None:
            return
        orders = self.db.plan_orders(plan["plan_id"])
        entry = next((order for order in orders if order["leg"] == "entry"), None)
        if entry is None:
            raise ValueError("plan_entry_order_missing")
        amount = Decimal(str(position["positionAmt"])) if position is not None else Decimal(0)
        if amount and current["opened_at_ns"] is None:
            with self.conn.transaction():
                self.db.set_plan_status(plan_id=plan["plan_id"], status="open", now_ns=now, opened_at_ns=now)
            current = self.db.plan(plan["plan_id"])
            if current is None:
                raise RuntimeError("plan_disappeared")
        latest: dict[str, dict[str, Any]] = {}
        for order in orders:
            if order["leg"] not in latest or order["attempt"] > latest[order["leg"]]["attempt"]:
                latest[order["leg"]] = order

        def status(leg: str) -> str | None:
            order = latest.get(leg)
            if order is None:
                return None
            return {
                "working": "NEW",
                "filled": "FILLED",
                "cancelled": "CANCELED",
                "rejected": "REJECTED",
                "unknown": "unknown",
                "reserved": "unknown",
                "submitted": "unknown",
                "not_submitted": "NOT_SUBMITTED",
            }.get(order["status"])

        if force_flatten and amount:
            if entry["status"] in ("reserved", "unknown", "submitted", "working"):
                await self._cancel_entry(current, entry, now)
                if status("sl") not in ("NEW", "PARTIALLY_FILLED", "FILLED"):
                    await self._protect(current, position, leg="sl", now=now)
                    return
            await self._flatten(current, amount, reason="operator_flatten", now=now)
            return
        unresolved_stop = latest.get("sl")
        if (
            amount
            and unresolved_stop is not None
            and unresolved_stop["status"] in ("unknown", "submitted")
            and now - unresolved_stop["updated_at_ns"] >= _ORDER_WINDOW_NS
            and not any(
                order["leg"] in ("safety_flatten", "time_exit")
                and order["status"] in ("unknown", "submitted", "working")
                for order in orders
            )
        ):
            await self._flatten(current, amount, reason="protection_submission_unresolved", now=now)
            return
        last_flatten = latest.get("safety_flatten") or latest.get("time_exit")
        facts = PlanFacts(
            now_ns=now,
            entered_at_ns=current["opened_at_ns"],
            max_hold_s=current["max_hold_s"],
            position_amount=amount,
            entry_order_status=status("entry"),
            entry_submission_unknown=entry["status"] in ("unknown", "reserved", "submitted"),
            sl_status=status("sl"),
            tp_status=status("tp"),
            sl_attempts=sum(order["leg"] == "sl" for order in orders),
            tp_attempts=sum(order["leg"] == "tp" for order in orders),
            sl_submission_unknown=latest.get("sl", {}).get("status") in ("unknown", "reserved", "submitted"),
            tp_submission_unknown=latest.get("tp", {}).get("status") in ("unknown", "reserved", "submitted"),
            flatten_status=None if last_flatten is None else status(last_flatten["leg"]),
            exit_fill_client_id=self.db.exit_fill_client_id(plan["plan_id"]),
            sl_client_ids=frozenset(order["client_order_id"] for order in orders if order["leg"] == "sl"),
            tp_client_ids=frozenset(order["client_order_id"] for order in orders if order["leg"] == "tp"),
        )
        action = step(facts)
        if action.action in ("await_entry", "await_venue", "query_entry", "query_sl", "query_tp", "query_flatten"):
            return
        if action.action in {"submit_sl", "submit_tp"}:
            await self._protect(current, position, leg="sl" if action.action == "submit_sl" else "tp", now=now)
        elif action.action == "flatten":
            await self._flatten(current, amount, reason=action.reason, now=now)
        elif action.action == "cancel_entry":
            await self._cancel_entry(current, entry, now)
        elif action.action == "cancel_protection":
            await self._cancel_protection(current, open_algos, now)
        elif action.action == "terminal":
            with self.conn.transaction():
                self.db.set_plan_status(
                    plan_id=plan["plan_id"],
                    status="terminal",
                    now_ns=now,
                    terminal_reason=current["terminal_reason"] or action.reason,
                )
                self.db.clear_fault(account_slot=self.account_slot, key=plan["plan_id"])

    async def _cancel_entry(self, plan: dict[str, Any], entry: dict[str, Any], now: int) -> None:
        """Cancel a partial entry before it can increase exposure during exits."""
        evidence: dict[str, Any] | None
        try:
            evidence = await self.venue.cancel_order(plan["native_symbol"], entry["client_order_id"])
        except BinanceFailure as exc:
            if exc.code != -2011:
                raise
            evidence = await self.venue.query_order(plan["native_symbol"], entry["client_order_id"])
            if evidence is None:
                return
        remote = str(evidence.get("status", ""))
        status = "filled" if remote == "FILLED" else "cancelled" if remote in ("CANCELED", "EXPIRED") else "working"
        with self.conn.transaction():
            self.db.update_order(
                client_id=entry["client_order_id"],
                status=status,
                now_ns=now,
                venue_order_id=str(evidence["orderId"]),
                evidence=evidence,
            )

    async def _protect(
        self, plan: dict[str, Any], position: dict[str, Any] | None, *, leg: Literal["sl", "tp"], now: int
    ) -> None:
        if position is None or Decimal(str(position["positionAmt"])) == 0:
            return
        orders = [order for order in self.db.plan_orders(plan["plan_id"]) if order["leg"] == leg]
        if orders and orders[-1]["status"] == "rejected":
            previous = orders[-1]
            if previous["error_code"] not in (-1102, -4136):
                await self._flatten(plan, Decimal(str(position["positionAmt"])), reason="protection_failed", now=now)
                return
        attempt = len(orders) + 1
        if attempt > 3:
            await self._flatten(plan, Decimal(str(position["positionAmt"])), reason="protection_failed", now=now)
            return
        catalogue = await self.venue.exchange_info()
        rules = _symbol_rules(catalogue, plan["native_symbol"])
        if rules is None or rules["tick"] <= 0:
            await self._flatten(plan, Decimal(str(position["positionAmt"])), reason="protection_rules_missing", now=now)
            return
        entry_price = Decimal(str(position.get("entryPrice") or plan["reference_price"]))
        trigger = _trigger_price(
            entry_price=entry_price,
            bps=plan["stop_bps"] if leg == "sl" else plan["tp_bps"],
            side=plan["side"],
            leg=leg,
            tick=rules["tick"],
        )
        client_id = client_order_id(namespace=self.account_slot, entry_id=plan["plan_id"], leg=leg, attempt=attempt)
        quantity = None if attempt == 1 else abs(Decimal(str(position["positionAmt"])))
        side: Literal["BUY", "SELL"] = "SELL" if plan["side"] == "long" else "BUY"
        with self.conn.transaction():
            self.db.reserve_order(
                client_id=client_id,
                plan_id=plan["plan_id"],
                native_symbol=plan["native_symbol"],
                leg=leg,
                attempt=attempt,
                now_ns=now,
            )
        with self.conn.transaction():
            self.db.update_order(
                client_id=client_id,
                status="submitted",
                now_ns=max(now, _now_ns()),
                evidence={
                    "submission": "started",
                    "request": {
                        "symbol": plan["native_symbol"],
                        "side": side,
                        "leg": leg,
                        "trigger_price": str(trigger),
                        "quantity": None if quantity is None else str(quantity),
                        "close_position": quantity is None,
                    },
                },
            )
        try:
            result = await self.venue.protection_order(
                symbol=plan["native_symbol"],
                side=side,
                leg=leg,
                trigger_price=trigger,
                client_id=client_id,
                quantity=quantity,
            )
        except (httpx.TransportError, httpx.TimeoutException):
            now = max(now, _now_ns())
            with self.conn.transaction():
                self.db.update_order(client_id=client_id, status="unknown", now_ns=now)
            return
        except BinanceFailure as exc:
            now = max(now, _now_ns())
            with self.conn.transaction():
                self.db.update_order(
                    client_id=client_id,
                    status="unknown" if exc.transient else "rejected",
                    now_ns=now,
                    error_code=exc.code,
                    evidence=exc.evidence(),
                )
            if exc.code == -2021:
                await self._flatten(
                    plan, Decimal(str(position["positionAmt"])), reason="protection_trigger_immediate", now=now
                )
            elif not exc.transient and exc.code not in (-1102, -4136):
                await self._flatten(plan, Decimal(str(position["positionAmt"])), reason="protection_failed", now=now)
            return
        with self.conn.transaction():
            self.db.update_order(client_id=client_id, status="working", now_ns=max(now, _now_ns()), evidence=result)

    async def _flatten(self, plan: dict[str, Any], amount: Decimal, *, reason: str, now: int) -> None:
        if amount == 0:
            return
        leg: Literal["time_exit", "safety_flatten"] = (
            "time_exit" if reason == "time_exit" or plan["terminal_reason"] == "time_exit" else "safety_flatten"
        )
        prior = [order for order in self.db.plan_orders(plan["plan_id"]) if order["leg"] == leg]
        if prior and prior[-1]["status"] not in ("filled", "rejected", "not_submitted"):
            return
        attempt = len(prior) + 1
        if attempt > 3:
            with self.conn.transaction():
                self.db.set_fault(
                    account_slot=self.account_slot, key=plan["plan_id"], reason="flatten_exhausted", now_ns=now
                )
            _LOG.critical("executor_flatten_exhausted %s", plan["plan_id"])
            return
        client_id = client_order_id(namespace=self.account_slot, entry_id=plan["plan_id"], leg=leg, attempt=attempt)
        with self.conn.transaction():
            self.db.reserve_order(
                client_id=client_id,
                plan_id=plan["plan_id"],
                native_symbol=plan["native_symbol"],
                leg=leg,
                attempt=attempt,
                now_ns=now,
            )
            self.db.set_plan_status(plan_id=plan["plan_id"], status="closing", now_ns=now, terminal_reason=reason)
        await self._send_market(
            plan_id=plan["plan_id"],
            symbol=plan["native_symbol"],
            side="SELL" if amount > 0 else "BUY",
            quantity=abs(amount),
            client_id=client_id,
            reduce_only=True,
            now=now,
        )

    async def _cancel_protection(self, plan: dict[str, Any], open_algos: dict[str, dict[str, Any]], now: int) -> None:
        for order in self.db.plan_orders(plan["plan_id"]):
            client_id = order["client_order_id"]
            if client_id not in open_algos:
                continue
            try:
                result = await self.venue.cancel_algo(client_id)
            except BinanceFailure as exc:
                if exc.code != -2013:
                    raise
                continue
            with self.conn.transaction():
                self.db.update_order(client_id=client_id, status="cancelled", now_ns=now, evidence=result)


async def run_executor(settings: Any, stop: asyncio.Event | None = None) -> None:
    execution = settings.trading.execution
    if not settings.trading.enabled or not execution.enabled or execution.binance.environment != "DEMO":
        raise ValueError("executor_not_enabled_for_demo")
    key_path = settings.trading_binance_usdm_api_key_file()
    secret_path = settings.trading_binance_usdm_api_secret_file()
    if key_path is None or secret_path is None:
        raise ValueError("executor_credentials_missing")
    venue = DemoBinance(
        environment="DEMO", api_key=key_path.read_text().strip(), api_secret=secret_path.read_text().strip()
    )
    try:
        await venue.sync_time()
        with postgres_connection(settings, application_name="tracefold_executor", long_lived=True) as conn:
            lock = conn.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (_advisory_key(execution.account_slot),)
            ).fetchone()
            if not lock or not lock["acquired"]:
                raise RuntimeError("executor_account_slot_already_owned")
            runner = ExecutorRunner(settings=settings, conn=conn, venue=venue)
            while stop is None or not stop.is_set():
                try:
                    await runner.tick()
                except Exception as exc:
                    _LOG.error("executor_tick_failed: %s", type(exc).__name__)
                    with conn.transaction():
                        failed_at = _now_ns()
                        runner.db.heartbeat(
                            account_slot=runner.account_slot, now_ns=failed_at, error=type(exc).__name__
                        )
                        runner.db.set_fault(
                            account_slot=runner.account_slot,
                            key="cycle",
                            reason=type(exc).__name__,
                            now_ns=failed_at,
                        )
                if stop is None:
                    await asyncio.sleep(1)
                else:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=1)
    finally:
        await venue.aclose()


__all__ = ["ExecutorRunner", "run_executor"]
