"""Single-process DEMO executor: short PostgreSQL transactions and REST reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import suppress
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Literal, cast
from uuid import uuid4

import httpx
from psycopg.errors import UniqueViolation

from tracefold.app.repository_session import postgres_connection
from tracefold.integrations.trading.binance import BinanceFailure, DemoBinance
from tracefold.platform.postgres.runtime_processes import RuntimeProcesses
from tracefold.trading.executor.core import EntryFacts, EntryLifecycleFacts, SignalV4, admit, client_order_id, step
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


class ExecutorRunner:
    def __init__(self, *, settings: Any, conn: Any, venue: DemoBinance) -> None:
        if settings.trading.execution.binance.environment != "DEMO":
            raise ValueError("executor_demo_only")
        self.settings = settings
        self.conn = conn
        self.db = ExecutorStorage(conn)
        self.venue = venue
        self.account_slot = settings.trading.execution.account_slot
        self.instance_id = str(uuid4())
        self.runtime = RuntimeProcesses(conn, kind="executor", key=self.account_slot)
        self._runtime_started = False
        self._last_active_read_ns = 0
        self._last_full_read_ns = 0

    def heartbeat(self, *, now_ns: int, fault_code: str | None = None) -> None:
        self.db.ensure_account(self.account_slot)
        now_ms = now_ns // 1_000_000
        if not self._runtime_started:
            if not self.runtime.begin(instance_id=self.instance_id, started_at_ms=now_ms, now_ms=now_ms):
                raise RuntimeError("executor_account_slot_already_owned")
            self.runtime.transition(instance_id=self.instance_id, lifecycle_state="running", now_ms=now_ms)
            self._runtime_started = True
        self.runtime.heartbeat(instance_id=self.instance_id, now_ms=now_ms, fault_code=fault_code)

    def stop_runtime(self) -> None:
        if self._runtime_started:
            self.runtime.transition(
                instance_id=self.instance_id, lifecycle_state="stopped", now_ms=_now_ns() // 1_000_000
            )

    async def tick(self) -> None:
        now = _now_ns()
        with self.conn.transaction():
            self.heartbeat(now_ns=now)
        if now - self._last_full_read_ns >= 60 * _NS_PER_SECOND:
            await self._full_account_check(now)
            self._last_full_read_ns = now
        if now - self._last_active_read_ns >= 5 * _NS_PER_SECOND:
            await self._reconcile(now)
            self._last_active_read_ns = now
        await self._one_intent(now)
        await self._one_signal(now)
        with self.conn.transaction():
            for plan in self.db.pending_pnl_entries(self.account_slot):
                self.db.settle_pnl(plan=plan, now_ns=now)

    async def _one_signal(self, now: int) -> None:
        state = self.db.account(self.account_slot)
        if state is None:
            raise RuntimeError("executor_state_missing")
        signal = self.db.next_signal(account_slot=self.account_slot)
        if signal is None:
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
            self._refuse_signal(signal, now=now, disposition=disposition, reason=verdict.reason)
            return
        entry_id = client_order_id(namespace=self.account_slot, entry_id=signal.signal_id, leg="entry", attempt=1)
        try:
            with self.conn.transaction():
                if not self.db.accept_entry(
                    entry_id=signal.signal_id,
                    command_id=None,
                    account_slot=self.account_slot,
                    native_symbol=signal.native_symbol,
                    side=signal.side,
                    quantity=str(verdict.quantity),
                    reference_price=str(signal.reference_price),
                    stop_bps=signal.stop_bps,
                    tp_bps=signal.tp_bps,
                    max_hold_s=signal.max_hold_s,
                    now_ns=now,
                ):
                    return
                self.db.reserve_order(
                    client_id=entry_id,
                    entry_id=signal.signal_id,
                    native_symbol=signal.native_symbol,
                    leg="entry",
                    attempt=1,
                    now_ns=now,
                )
        except UniqueViolation:
            self._refuse_signal(signal, now=now, disposition="refused", reason="symbol_exposure")
            return
        await self._send_market(
            entry_id=signal.signal_id,
            symbol=signal.native_symbol,
            side="BUY" if signal.side == "long" else "SELL",
            quantity=verdict.quantity,
            client_id=entry_id,
            reduce_only=False,
            now=now,
        )

    def _refuse_signal(self, signal: SignalV4, *, now: int, disposition: str, reason: str) -> None:
        with self.conn.transaction():
            self.db.record_disposition(
                kind="signal",
                input_id=signal.signal_id,
                account_slot=self.account_slot,
                disposition=disposition,
                reason=reason,
                now_ns=now,
            )

    async def _entry_facts(self, signal: SignalV4) -> EntryFacts | None:
        quote_requested_at_ns = _now_ns()
        positions, orders, algos, account, mode, quote, catalogue = await asyncio.gather(
            self.venue.positions(),
            self.venue.open_orders(),
            self.venue.open_algo_orders(),
            self.venue.account(),
            self.venue.position_mode(),
            self.venue.book_ticker(signal.native_symbol),
            self.venue.exchange_info(),
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
        active = self.db.active_entries(self.account_slot)
        position_by_symbol = {row["symbol"]: row for row in positions if Decimal(str(row["positionAmt"])) != 0}
        notional = sum(
            abs(Decimal(str(row["positionAmt"])) * Decimal(str(row["markPrice"])))
            for row in position_by_symbol.values()
        )
        notional += sum(plan["reserved_notional"] for plan in active if plan["native_symbol"] not in position_by_symbol)
        risk = self.settings.trading.execution.risk
        control = self.db.control(self.account_slot)
        state = self.db.account(self.account_slot)
        return EntryFacts(
            now_ns=_now_ns(),
            entries_paused=bool(control["entries_paused"]),
            flatten_in_progress=control["flatten_command_id"] is not None,
            unexpected_exposure=bool(state and state["unexpected_exposure"]),
            emergency_halted=bool(control["emergency_halted"]),
            symbol_position=Decimal(str(position_by_symbol.get(signal.native_symbol, {}).get("positionAmt", "0"))),
            symbol_regular_orders=sum(row["symbol"] == signal.native_symbol for row in orders),
            symbol_algo_orders=sum(row["symbol"] == signal.native_symbol for row in algos),
            active_entries=len(active),
            max_entries=5,
            equity_usdt=Decimal(str(account["totalMarginBalance"])),
            active_notional_usdt=notional,
            max_leverage=risk.max_leverage,
            risk_fraction=risk.risk_fraction_per_trade,
            bid=Decimal(str(quote["bidPrice"])),
            ask=Decimal(str(quote["askPrice"])),
            quote_at_ns=quote_requested_at_ns,
            quote_max_age_ns=int(risk.market_stale_after_seconds * _NS_PER_SECOND),
            market_min_qty=rules["min_qty"],
            market_max_qty=rules["max_qty"],
            market_step=rules["step"],
            min_notional=rules["min_notional"],
            hedge_mode=str(mode["dualSidePosition"]).lower() == "true"
            or any(row.get("positionSide", "BOTH") != "BOTH" for row in positions),
        )

    async def _send_market(
        self,
        *,
        entry_id: str | None,
        symbol: str,
        side: Literal["BUY", "SELL"],
        quantity: Decimal,
        client_id: str,
        reduce_only: bool,
        now: int,
    ) -> None:
        try:
            result = await self.venue.market_order(
                symbol=symbol, side=side, quantity=quantity, client_id=client_id, reduce_only=reduce_only
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            with self.conn.transaction():
                self.db.update_order(client_id=client_id, status="unknown", now_ns=now)
            _LOG.warning("executor_order_unknown %s: %s", client_id, type(exc).__name__)
            return
        except BinanceFailure as exc:
            status = "unknown" if exc.transient else "rejected"
            with self.conn.transaction():
                self.db.update_order(client_id=client_id, status=status, now_ns=now, error_code=exc.code)
                if entry_id is not None and not reduce_only and status == "rejected":
                    self.db.set_entry_state(
                        entry_id=entry_id, status="terminal", now_ns=now, terminal_reason="entry_rejected"
                    )
            return
        status = "filled" if result.get("status") == "FILLED" else "working"
        with self.conn.transaction():
            self.db.update_order(
                client_id=client_id,
                status=status,
                now_ns=now,
                venue_order_id=str(result["orderId"]),
                evidence=result,
            )
            if entry_id is not None and not reduce_only and status == "filled":
                self.db.set_entry_state(entry_id=entry_id, status="open", now_ns=now, opened_at_ns=now)

    async def _one_intent(self, now: int) -> None:
        state = self.db.account(self.account_slot)
        if state is None:
            raise RuntimeError("executor_state_missing")
        intent = self.db.next_intent(account_slot=self.account_slot)
        if intent is None:
            return
        command_id = str(intent["command_id"])
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
            return
        action = intent["action"]
        control = self.db.control(self.account_slot)
        if action in ("pause_entries", "resume_entries", "emergency_halt", "flatten"):
            with self.conn.transaction():
                refusal = None
                if action == "resume_entries" and self.db.newer_entry_stop_applied(
                    account_slot=self.account_slot, seq=int(intent["seq"])
                ):
                    refusal = "superseded"
                elif action == "resume_entries" and control["flatten_command_id"] is not None:
                    refusal = "flatten_in_progress"
                if refusal is not None:
                    self.db.record_disposition(
                        kind="intent",
                        input_id=command_id,
                        account_slot=self.account_slot,
                        disposition="refused",
                        reason=refusal,
                        now_ns=now,
                    )
                    return
                self.db.apply_control(account_slot=self.account_slot, action=action, command_id=command_id, now_ns=now)
                self.db.record_disposition(
                    kind="intent",
                    input_id=command_id,
                    account_slot=self.account_slot,
                    disposition="accepted",
                    reason="flatten_requested" if action == "flatten" else "control_applied",
                    now_ns=now,
                )
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
            signal = SignalV4(
                signal_id=command_id,
                decision_id=command_id,
                case_id="manual:" + command_id,
                account_slot=self.account_slot,
                entry_scope_id=command_id,
                asset_id="crypto:" + parts[2],
                native_symbol=symbol,
                mapping_semantics_digest="0" * 64,
                side=side,
                reference_price=mid,
                max_drift_bps=200,
                stop_bps=risk.stop_distance_bps,
                tp_bps=exit_policy.take_profit_bps if exit_policy else 2 * risk.stop_distance_bps,
                max_hold_s=exit_policy.max_holding_seconds if exit_policy else 14_400,
                policy_id="manual",
                policy_version="v1",
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
            self._refuse_intent(intent, now=now, reason=verdict.reason)
            return
        entry_id = client_order_id(namespace=self.account_slot, entry_id=command_id, leg="entry", attempt=1)
        try:
            with self.conn.transaction():
                if not self.db.accept_entry(
                    entry_id=command_id,
                    command_id=command_id,
                    account_slot=self.account_slot,
                    native_symbol=symbol,
                    side=side,
                    quantity=str(verdict.quantity),
                    reference_price=str(mid),
                    stop_bps=signal.stop_bps,
                    tp_bps=signal.tp_bps,
                    max_hold_s=signal.max_hold_s,
                    now_ns=now,
                ):
                    return
                self.db.reserve_order(
                    client_id=entry_id,
                    entry_id=command_id,
                    native_symbol=symbol,
                    leg="entry",
                    attempt=1,
                    now_ns=now,
                )
        except UniqueViolation:
            self._refuse_intent(intent, now=now, reason="symbol_exposure")
            return
        await self._send_market(
            entry_id=command_id,
            symbol=symbol,
            side="BUY" if side == "long" else "SELL",
            quantity=verdict.quantity,
            client_id=entry_id,
            reduce_only=False,
            now=now,
        )

    def _refuse_intent(self, intent: dict[str, Any], *, now: int, reason: str) -> None:
        with self.conn.transaction():
            self.db.record_disposition(
                kind="intent",
                input_id=str(intent["command_id"]),
                account_slot=self.account_slot,
                disposition="refused",
                reason=reason,
                now_ns=now,
            )

    async def _full_account_check(self, now: int) -> None:
        positions, orders, algos, account = await asyncio.gather(
            self.venue.positions(), self.venue.open_orders(), self.venue.open_algo_orders(), self.venue.account()
        )
        active_entries = self.db.active_entries(self.account_slot)
        active_symbols = {plan["native_symbol"] for plan in active_entries}
        plan_by_symbol = {plan["native_symbol"]: plan for plan in active_entries}
        known_client_ids = self.db.active_client_ids(self.account_slot)
        venue_symbols = {row["symbol"] for row in positions if Decimal(str(row["positionAmt"])) != 0} | {
            row["symbol"] for row in (*orders, *algos)
        }
        unexpected_symbols = venue_symbols - active_symbols
        unexpected_orders = {
            str(row.get("clientOrderId") or "")
            for row in orders
            if str(row.get("clientOrderId") or "") not in known_client_ids
        } | {
            str(row.get("clientAlgoId") or "")
            for row in algos
            if str(row.get("clientAlgoId") or "") not in known_client_ids
        }
        incompatible_positions = {
            str(row["symbol"])
            for row in positions
            if Decimal(str(row["positionAmt"])) != 0
            and (plan := plan_by_symbol.get(str(row["symbol"]))) is not None
            and (
                (Decimal(str(row["positionAmt"])) > 0) != (plan["side"] == "long")
                or abs(Decimal(str(row["positionAmt"]))) > Decimal(str(plan["quantity"]))
                or row.get("positionSide", "BOTH") != "BOTH"
            )
        }
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
        if unexpected:
            _LOG.error(
                "executor_unclaimed_venue_exposure: symbols=%s orders=%s",
                sorted(unexpected_symbols | incompatible_positions),
                sorted(unexpected_orders),
            )

    async def _reconcile(self, now: int) -> None:
        plans = self.db.active_entries(self.account_slot)
        awaiting_fills = self.db.entries_awaiting_fills(self.account_slot)
        control = self.db.control(self.account_slot)
        if control.get("flatten_command_id"):
            await self._reconcile_account_flatten(str(control["flatten_command_id"]), plans, now)
        if not plans:
            for symbol in sorted({plan["native_symbol"] for plan in awaiting_fills}):
                await self._sync_trades(symbol, now)
            return
        positions, open_algos = await asyncio.gather(self.venue.positions(), self.venue.open_algo_orders())
        positions_by_symbol = {row["symbol"]: row for row in positions}
        algos_by_client_id = {row["clientAlgoId"]: row for row in open_algos}
        for plan in plans:
            try:
                await self._refresh_entry_orders(plan, algos_by_client_id, now)
            except (httpx.HTTPError, BinanceFailure) as exc:
                _LOG.warning("executor_order_refresh_deferred %s: %s", plan["entry_id"], type(exc).__name__)
        for symbol in sorted({plan["native_symbol"] for plan in (*plans, *awaiting_fills)}):
            await self._sync_trades(symbol, now)
        for plan in plans:
            try:
                await self._step_plan(
                    plan=plan,
                    position=positions_by_symbol.get(plan["native_symbol"]),
                    open_algos=algos_by_client_id,
                    force_flatten=bool(control.get("flatten_command_id")),
                    now=now,
                )
            except (httpx.HTTPError, BinanceFailure) as exc:
                _LOG.warning("executor_reconcile_deferred %s: %s", plan["entry_id"], type(exc).__name__)

    async def _reconcile_account_flatten(self, command_id: str, plans: list[dict[str, Any]], now: int) -> None:
        positions, orders, algos = await asyncio.gather(
            self.venue.positions(), self.venue.open_orders(), self.venue.open_algo_orders()
        )
        if any(
            Decimal(str(row["positionAmt"])) != 0 and row.get("positionSide", "BOTH") != "BOTH" for row in positions
        ):
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
                            _LOG.critical("executor_external_flatten_terminal_without_flat %s", symbol)
                            continue
                        if status != "filled":
                            continue
                    elif now - latest["updated_at_ns"] >= _ORDER_WINDOW_NS:
                        with self.conn.transaction():
                            self.db.update_order(
                                client_id=latest["client_order_id"], status="not_submitted", now_ns=now
                            )
                    else:
                        continue
                elif latest["status"] == "rejected":
                    _LOG.critical("executor_external_flatten_rejected %s", symbol)
                    continue
            attempt = len(prior) + 1
            if attempt > 3:
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
                entry_id=None,
                symbol=symbol,
                side="SELL" if amount > 0 else "BUY",
                quantity=abs(amount),
                client_id=client_id,
                reduce_only=True,
                now=now,
            )
            await self._sync_trades(symbol, now)

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

    async def _sync_trades(self, symbol: str, now: int) -> None:
        cursor = self.db.trade_cursor(symbol, account_slot=self.account_slot)
        trades = await self.venue.user_trades(symbol, from_id=cursor)
        with self.conn.transaction():
            for trade in trades:
                self.db.record_fill(symbol=symbol, trade=trade)
            self.db.attribute_unbound_fills(symbol=symbol, now_ns=now)
            if trades:
                self.db.advance_trade_cursor(
                    account_slot=self.account_slot,
                    symbol=symbol,
                    next_id=max(int(row["id"]) for row in trades) + 1,
                    now_ns=now,
                )

    async def _refresh_entry_orders(
        self, plan: dict[str, Any], open_algos: dict[str, dict[str, Any]], now: int
    ) -> None:
        for order in self.db.entry_orders(plan["entry_id"]):
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
            elif order["status"] in ("reserved", "unknown") and now - order["updated_at_ns"] >= _ORDER_WINDOW_NS:
                with self.conn.transaction():
                    self.db.update_order(client_id=client_id, status="not_submitted", now_ns=now)

    async def _step_plan(
        self,
        *,
        plan: dict[str, Any],
        position: dict[str, Any] | None,
        open_algos: dict[str, dict[str, Any]],
        force_flatten: bool,
        now: int,
    ) -> None:
        current = self.db.entry(plan["entry_id"])
        if current is None or current["terminal_at_ns"] is not None:
            return
        orders = self.db.entry_orders(plan["entry_id"])
        entry = next((order for order in orders if order["leg"] == "entry"), None)
        if entry is None:
            raise ValueError("plan_entry_order_missing")
        amount = Decimal(str(position["positionAmt"])) if position is not None else Decimal(0)
        if amount and entry["status"] in ("filled", "cancelled") and current["opened_at_ns"] is None:
            with self.conn.transaction():
                self.db.set_entry_state(entry_id=plan["entry_id"], status="open", now_ns=now, opened_at_ns=now)
            current = self.db.entry(plan["entry_id"])
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
                "not_submitted": "NOT_SUBMITTED",
            }.get(order["status"])

        if force_flatten and amount:
            await self._flatten(current, amount, reason="operator_flatten", now=now)
            return
        last_flatten = latest.get("safety_flatten") or latest.get("time_exit")
        facts = EntryLifecycleFacts(
            now_ns=now,
            entered_at_ns=current["opened_at_ns"],
            max_hold_s=current["max_hold_s"],
            position_amount=amount,
            entry_order_status=status("entry"),
            entry_submission_unknown=entry["status"] in ("unknown", "reserved"),
            sl_status=status("sl"),
            tp_status=status("tp"),
            sl_attempts=sum(order["leg"] == "sl" for order in orders),
            tp_attempts=sum(order["leg"] == "tp" for order in orders),
            sl_submission_unknown=latest.get("sl", {}).get("status") in ("unknown", "reserved"),
            tp_submission_unknown=latest.get("tp", {}).get("status") in ("unknown", "reserved"),
            flatten_status=None if last_flatten is None else status(last_flatten["leg"]),
            exit_fill_client_id=self.db.exit_fill_client_id(plan["entry_id"]),
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
        elif action.action == "cancel_protection":
            await self._cancel_protection(current, open_algos, now)
        elif action.action == "terminal":
            with self.conn.transaction():
                self.db.set_entry_state(
                    entry_id=plan["entry_id"],
                    status="terminal",
                    now_ns=now,
                    terminal_reason=current["terminal_reason"] or action.reason,
                )

    async def _protect(
        self, plan: dict[str, Any], position: dict[str, Any] | None, *, leg: Literal["sl", "tp"], now: int
    ) -> None:
        if position is None or Decimal(str(position["positionAmt"])) == 0:
            return
        orders = [order for order in self.db.entry_orders(plan["entry_id"]) if order["leg"] == leg]
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
        client_id = client_order_id(namespace=self.account_slot, entry_id=plan["entry_id"], leg=leg, attempt=attempt)
        with self.conn.transaction():
            self.db.reserve_order(
                client_id=client_id,
                entry_id=plan["entry_id"],
                native_symbol=plan["native_symbol"],
                leg=leg,
                attempt=attempt,
                now_ns=now,
            )
        try:
            result = await self.venue.protection_order(
                symbol=plan["native_symbol"],
                side="SELL" if plan["side"] == "long" else "BUY",
                leg=leg,
                trigger_price=trigger,
                client_id=client_id,
                quantity=None if attempt == 1 else abs(Decimal(str(position["positionAmt"]))),
            )
        except (httpx.TransportError, httpx.TimeoutException):
            with self.conn.transaction():
                self.db.update_order(client_id=client_id, status="unknown", now_ns=now)
            return
        except BinanceFailure as exc:
            with self.conn.transaction():
                self.db.update_order(
                    client_id=client_id,
                    status="unknown" if exc.transient else "rejected",
                    now_ns=now,
                    error_code=exc.code,
                )
            if exc.code == -2021:
                await self._flatten(
                    plan, Decimal(str(position["positionAmt"])), reason="protection_trigger_immediate", now=now
                )
            elif not exc.transient and exc.code not in (-1102, -4136):
                await self._flatten(plan, Decimal(str(position["positionAmt"])), reason="protection_failed", now=now)
            return
        with self.conn.transaction():
            self.db.update_order(client_id=client_id, status="working", now_ns=now, evidence=result)

    async def _flatten(self, plan: dict[str, Any], amount: Decimal, *, reason: str, now: int) -> None:
        if amount == 0:
            return
        leg: Literal["time_exit", "safety_flatten"] = (
            "time_exit" if reason == "time_exit" or plan["terminal_reason"] == "time_exit" else "safety_flatten"
        )
        prior = [order for order in self.db.entry_orders(plan["entry_id"]) if order["leg"] == leg]
        if prior and prior[-1]["status"] not in ("filled", "rejected", "not_submitted"):
            return
        attempt = len(prior) + 1
        if attempt > 3:
            _LOG.critical("executor_flatten_exhausted %s", plan["entry_id"])
            return
        client_id = client_order_id(namespace=self.account_slot, entry_id=plan["entry_id"], leg=leg, attempt=attempt)
        with self.conn.transaction():
            self.db.reserve_order(
                client_id=client_id,
                entry_id=plan["entry_id"],
                native_symbol=plan["native_symbol"],
                leg=leg,
                attempt=attempt,
                now_ns=now,
            )
            self.db.set_entry_state(entry_id=plan["entry_id"], status="closing", now_ns=now, terminal_reason=reason)
        await self._send_market(
            entry_id=plan["entry_id"],
            symbol=plan["native_symbol"],
            side="SELL" if amount > 0 else "BUY",
            quantity=abs(amount),
            client_id=client_id,
            reduce_only=True,
            now=now,
        )

    async def _cancel_protection(self, plan: dict[str, Any], open_algos: dict[str, dict[str, Any]], now: int) -> None:
        for order in self.db.entry_orders(plan["entry_id"]):
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
            try:
                while stop is None or not stop.is_set():
                    try:
                        await runner.tick()
                    except Exception as exc:
                        _LOG.exception("executor_tick_failed")
                        with conn.transaction():
                            runner.heartbeat(now_ns=_now_ns(), fault_code=type(exc).__name__)
                    if stop is None:
                        await asyncio.sleep(1)
                    else:
                        with suppress(TimeoutError):
                            await asyncio.wait_for(stop.wait(), timeout=1)
            finally:
                with conn.transaction():
                    runner.stop_runtime()

    finally:
        await venue.aclose()


__all__ = ["ExecutorRunner", "run_executor"]
