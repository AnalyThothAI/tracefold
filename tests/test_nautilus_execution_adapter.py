from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.integrations.nautilus.execution_adapter import (
    AccountReconciliation,
    AuthoritativeExecutionState,
    BinanceExecutionAdapter,
    ExecutionAdapter,
    account_execution_adapter,
    strategy_execution_adapters,
)


class _Ports:
    def __getattr__(self, name: str) -> Any:
        def called(**kwargs: Any) -> Any:
            return name, kwargs

        return called


def test_strategy_adapter_union_is_closed_and_provider_qualified() -> None:
    adapters = strategy_execution_adapters(_Ports())  # type: ignore[arg-type]

    assert set(adapters) == {"BINANCE_USDM"}
    assert isinstance(adapters["BINANCE_USDM"], BinanceExecutionAdapter)
    assert all(isinstance(adapter, ExecutionAdapter) for adapter in adapters.values())
    assert adapters["BINANCE_USDM"].client_id.value == "BINANCE"


def test_catalog_only_hyperliquid_cannot_select_an_execution_adapter() -> None:
    with pytest.raises(ValueError, match="execution_binding_disabled:HYPERLIQUID_PERP"):
        account_execution_adapter("HYPERLIQUID_PERP", object())


def test_adapter_refuses_cross_venue_intent_and_account_scope() -> None:
    adapter = BinanceExecutionAdapter(ports=_Ports())  # type: ignore[arg-type]
    hyper_intent = SimpleNamespace(binding="HYPERLIQUID_PERP")

    with pytest.raises(ValueError, match="execution_adapter_intent_binding_mismatch"):
        adapter.query(hyper_intent)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="execution_adapter_quote_binding_mismatch"):
        adapter.probe_quote("HYPERLIQUID_PERP", "BTC-PERP.HYPERLIQUID")


def test_account_reconciliation_distinguishes_known_empty_from_query_failure() -> None:
    calls: list[object] = []
    client = object()

    async def known_empty(selected: object) -> tuple[list[object], list[object]]:
        calls.append(selected)
        return [], []

    adapter = account_execution_adapter(
        "BINANCE_USDM",
        client,
        account_report_loader=known_empty,
    )
    reconciled = asyncio.run(adapter.reconcile_account("BINANCE_USDM"))

    assert reconciled == AccountReconciliation(
        binding="BINANCE_USDM",
        position_reports=(),
        order_reports=(),
    )
    assert calls == [client]

    async def failed(_selected: object) -> tuple[list[object], list[object]]:
        raise RuntimeError("provider_unavailable")

    failed_adapter = account_execution_adapter(
        "BINANCE_USDM",
        client,
        account_report_loader=failed,
    )
    with pytest.raises(RuntimeError, match="provider_unavailable"):
        asyncio.run(failed_adapter.reconcile_account("BINANCE_USDM"))
    with pytest.raises(ValueError, match="execution_adapter_account_binding_mismatch"):
        asyncio.run(failed_adapter.reconcile_account("HYPERLIQUID_PERP"))


def test_lifecycle_methods_are_real_ports_not_decorative_protocol_members() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class Ports(_Ports):
        def execution_query(self, **kwargs: Any) -> AuthoritativeExecutionState:
            calls.append(("query", kwargs))
            return AuthoritativeExecutionState(
                intent_id=kwargs["intent"].intent_id,
                binding=kwargs["binding"],
                queried_at_ms=1,
            )

    adapter = BinanceExecutionAdapter(ports=Ports())  # type: ignore[arg-type]
    intent = SimpleNamespace(intent_id="intent-1", binding="BINANCE_USDM")

    assert adapter.query(intent).intent_id == "intent-1"  # type: ignore[arg-type]
    assert calls == [
        (
            "query",
            {
                "binding": "BINANCE_USDM",
                "client_id": adapter.client_id,
                "intent": intent,
            },
        )
    ]
