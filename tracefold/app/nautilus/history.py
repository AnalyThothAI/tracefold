"""Bounded historical native evidence preview/apply; no TradingNode or Cache exists here."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from decimal import Decimal
from typing import Any, Literal

from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.factories import get_cached_binance_http_client
from nautilus_trader.adapters.binance.futures.http.account import BinanceFuturesAccountHttpAPI
from nautilus_trader.common.component import LiveClock

from tracefold.app.repository_session import repositories
from tracefold.integrations.nautilus.oi_runtime.entry import initial_plan_order_bindings
from tracefold.integrations.nautilus.oi_runtime.journal import ExecutionJournal, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.observations import offer_native_evidence
from tracefold.integrations.nautilus.oi_runtime.order_evidence import OrderEvidenceRequest, read_order_evidence
from tracefold.integrations.nautilus.oi_runtime.venue import read_failure
from tracefold.platform.config.models import Settings
from tracefold.platform.config.secret_file import read_secure_secret_text
from tracefold.trading.execution_contracts import (
    EXECUTION_STRATEGY_ID,
    MAX_OBSERVATION_APPEND_BATCH,
    ExecutionObservationV1,
)
from tracefold.trading.storage.execution_stream import prepare_execution_observations
from tracefold.trading.trade_plan import PlanOrderBinding, TradePlan

_MAX_ORDERS = 16
_MAX_ROWS = 10_000


def history_bindings(
    plan: TradePlan, namespace: str, recorded: tuple[PlanOrderBinding, ...]
) -> tuple[PlanOrderBinding, ...]:
    bindings = {
        value.client_order_id: value
        for value in initial_plan_order_bindings(plan, namespace=namespace)
        if value.leg != "exit" or value.exit_reason == plan.exit_reason
    }
    for value in recorded:
        if (value.account_slot, value.entry_id, value.source, value.instrument_id) != (
            plan.account_slot,
            plan.entry_id,
            plan.source,
            plan.instrument_id,
        ):
            raise ValueError("historical_binding_scope_conflict")
        previous = bindings.get(value.client_order_id)
        if previous is not None and previous != value:
            raise ValueError("historical_binding_identity_conflict")
        bindings[value.client_order_id] = value
    if len(bindings) > _MAX_ORDERS:
        raise ValueError("historical_order_scope_limit")
    return tuple(bindings.values())


async def collect_execution_evidence(
    account: BinanceFuturesAccountHttpAPI,
    *,
    plan: TradePlan,
    bindings: tuple[PlanOrderBinding, ...],
    environment: BinanceEnvironment,
) -> tuple[tuple[ExecutionObservationV1, ...], tuple[dict[str, Any], ...]]:
    """Reuse the adapter reader and journal translator, without any native execution consumer."""
    if len(bindings) > _MAX_ORDERS:
        raise ValueError("historical_order_scope_limit")
    journal = ExecutionJournal(factory=ObservationFactory(plan.account_slot, EXECUTION_STRATEGY_ID), max_rows=_MAX_ROWS)
    outcomes: list[dict[str, Any]] = []
    for binding in bindings:
        outcome: dict[str, Any] = {"client_order_id": binding.client_order_id, "leg": binding.leg}
        try:
            conditional: Literal["STOP_MARKET", "TAKE_PROFIT_MARKET"] | None = (
                "STOP_MARKET"
                if binding.leg == "stop"
                else "TAKE_PROFIT_MARKET"
                if binding.leg == "take_profit"
                else None
            )
            request = OrderEvidenceRequest(
                symbol=plan.instrument_id.removesuffix("-PERP.BINANCE"),
                client_order_id=binding.client_order_id,
                conditional_type=conditional,
            )
            evidence = await asyncio.wait_for(
                read_order_evidence(
                    account,
                    request=request,
                    observed_at_ns=time.time_ns(),
                    max_trade_requests=8,
                ),
                timeout=10,
            )
            order = evidence.order
            expected_side = "BUY" if (plan.direction == "long") == (binding.leg == "entry") else "SELL"
            if order is not None and (
                order.side is None
                or order.origQty is None
                or order.time is None
                or order.side.value != expected_side
                or order.reduceOnly != (binding.leg != "entry")
                or Decimal(order.origQty) > plan.entry_quantity
                or order.time < plan.created_at_ns // 1_000_000
            ):
                raise ValueError("historical_order_intent_conflict")
            if evidence.parent is not None and evidence.parent.side != expected_side:
                raise ValueError("historical_order_intent_conflict")
            accepted = offer_native_evidence(
                journal, evidence, environment=environment, observed_at_ns=time.time_ns(), binding=binding
            )
            outcome.update(
                complete=evidence.complete,
                queued=accepted,
                venue_order_id=None if order is None else str(order.orderId),
                parent_algo_id=None if evidence.parent is None else str(evidence.parent.algoId),
                trades=0 if evidence.history is None else len(evidence.history.trades),
                remaining=[] if evidence.history is None else [asdict(c) for c in evidence.history.remaining],
            )
            if not accepted:
                outcome["failure"] = "historical_evidence_row_limit"
                outcomes.append(outcome)
                break
        except Exception as exc:
            outcome.update(complete=False, failure=str(exc) if isinstance(exc, ValueError) else read_failure(exc))
        outcomes.append(outcome)
    return tuple(
        row.value for row in journal.due(float("inf")) if isinstance(row.value, ExecutionObservationV1)
    ), tuple(outcomes)


def _account(settings: Settings, environment: BinanceEnvironment) -> BinanceFuturesAccountHttpAPI:
    key_file = settings.trading_binance_usdm_api_key_file()
    secret_file = settings.trading_binance_usdm_api_secret_file()
    if key_file is None or secret_file is None:
        raise ValueError("historical_venue_credentials_missing")
    clock = LiveClock()
    return BinanceFuturesAccountHttpAPI(
        client=get_cached_binance_http_client(
            clock=clock,
            account_type=BinanceAccountType.USDT_FUTURES,
            environment=environment,
            api_key=read_secure_secret_text(key_file),
            api_secret=read_secure_secret_text(secret_file),
        ),
        clock=clock,
        account_type=BinanceAccountType.USDT_FUTURES,
    )


def verify_execution_history(
    settings: Settings, *, entry_id: str, account_slot: str, environment: str, apply: bool = False
) -> dict[str, Any]:
    """Default preview is SELECT-only. Explicit apply appends evidence, never edits a Plan/control."""
    configured = settings.trading.execution
    if account_slot != configured.account_slot or environment != (configured.binance.environment or "LIVE"):
        raise ValueError("historical_execution_scope_mismatch")
    with repositories(settings, application_name="tracefold_history_preview") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        repos.conn.execute("SET LOCAL statement_timeout='3s'")
        row = repos.trading.trade_plan(entry_id)
        if row is None:
            raise ValueError("historical_plan_missing")
        plan = TradePlan.model_validate(row)
        if plan.account_slot != account_slot or plan.status != "closed":
            raise ValueError("historical_plan_scope_or_lifecycle_invalid")
        control = repos.trading.execution_runtime_control_state(account_slot)
        if control is None:
            raise ValueError("historical_execution_namespace_missing")
        state = repos.trading.execution_runtime_state(account_slot)
        if state is not None and state.connection != environment:
            raise ValueError("historical_execution_connection_mismatch")
        recorded: list[PlanOrderBinding] = []
        after_seq = 0
        for _ in range(8):
            rows = repos.trading.trade_plan_order_bindings(
                account_slot=account_slot,
                entry_ids=(entry_id,),
                after_seq=after_seq,
                observed_before_ns=time.time_ns(),
                limit=256,
            )
            recorded.extend(
                PlanOrderBinding.model_validate({k: v for k, v in row.items() if k != "seq"}) for row in rows
            )
            if len(rows) < 256:
                break
            after_seq = int(rows[-1]["seq"])
        else:
            raise ValueError("historical_binding_read_limit")
        bindings = history_bindings(plan, control.execution_namespace, tuple(recorded))
        original_result = repos.trading.execution_result(entry_id=entry_id)

    # The read session and its transaction are closed before signed external I/O.
    async def read():
        return await collect_execution_evidence(
            _account(settings, BinanceEnvironment(environment)),
            plan=plan,
            bindings=bindings,
            environment=BinanceEnvironment(environment),
        )

    observations, outcomes = asyncio.run(read())
    batches = tuple(
        prepare_execution_observations(observations[start : start + MAX_OBSERVATION_APPEND_BATCH])
        for start in range(0, len(observations), MAX_OBSERVATION_APPEND_BATCH)
    )
    payload = json.dumps([row.model_dump(mode="json") for row in observations], separators=(",", ":"))
    with (
        repositories(
            settings, application_name="tracefold_history_apply" if apply else "tracefold_history_preview"
        ) as repos,
        repos.transaction(),
    ):
        repos.conn.execute("SET LOCAL statement_timeout='3s'")
        if not apply:
            # Must precede the first SELECT, and never an INSERT rolled back to imitate a preview.
            repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        if TradePlan.model_validate(repos.trading.trade_plan(entry_id)) != plan:
            raise ValueError("historical_plan_changed")
        projected = repos.trading.preview_execution_evidence(entry_id=entry_id, payload_json=payload)
        if apply:
            for batch in batches:
                receipts = repos.trading.append_execution_observations(batch)
                if len(receipts) != batch.count:
                    raise ValueError("historical_evidence_write_unconfirmed")
            projected = repos.trading.execution_result(entry_id=entry_id)
    return {
        "mode": "apply" if apply else "preview",
        "account_slot": account_slot,
        "environment": environment,
        "entry_id": entry_id,
        "original_plan": plan.model_dump(mode="json"),
        "before": original_result,
        "projected": projected,
        "order_reads": outcomes,
        "observation_count": len(observations),
        "append_candidates": [row.model_dump(mode="json") for row in observations],
        "impact": {
            "history_only": True,
            "plan_mutated": False,
            "cache_applied": False,
            "control_mutated": False,
            "venue_orders_sent": 0,
        },
    }
