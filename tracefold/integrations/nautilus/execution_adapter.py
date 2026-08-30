"""The closed execution-adapter seam used by the one Nautilus lifecycle coordinator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from nautilus_trader.adapters.binance import BINANCE, BINANCE_VENUE
from nautilus_trader.adapters.hyperliquid import HYPERLIQUID, HYPERLIQUID_VENUE
from nautilus_trader.model.identifiers import ClientId

from tracefold.trading import (
    ExecutionQuote,
    ExecutionQuoteSnapshotV1,
    SubmissionFenceV1,
    TradeIntent,
    VenueBinding,
)

from .reconciliation import load_complete_account_reports


@dataclass(frozen=True, slots=True)
class AuthoritativeExecutionState:
    """Receipt that a query-first recovery pass was issued for the exact Intent."""

    intent_id: str
    binding: VenueBinding
    queried_at_ms: int


@dataclass(frozen=True, slots=True)
class AccountReconciliation:
    """Complete provider account reports; an empty tuple is known-empty, not query failure."""

    binding: VenueBinding
    position_reports: tuple[Any, ...]
    order_reports: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class BoundedQuoteProbe:
    binding: VenueBinding
    instrument_id: str
    quote: ExecutionQuote | None


@dataclass(frozen=True, slots=True)
class SubmitReceipt:
    intent_id: str
    binding: VenueBinding
    client_order_id: str
    submitted_at_ms: int


@dataclass(frozen=True, slots=True)
class AuthoritativeFill:
    """Coordinator-owned fill/protection instruction; adapters may not resize it."""

    position_id: str
    quantity: Decimal
    avg_entry_price: Decimal | None
    trigger_price: Decimal
    client_order_id: str
    generation: int
    previous_client_order_id: str | None
    submitted_at_ms: int


@dataclass(frozen=True, slots=True)
class ProtectionReceipt:
    intent_id: str
    binding: VenueBinding
    client_order_id: str
    quantity: Decimal
    trigger_price: Decimal
    submitted_at_ms: int


@dataclass(frozen=True, slots=True)
class FlatReceipt:
    """Receipt that the deterministic reduce-only close/reconcile path was started."""

    intent_id: str
    binding: VenueBinding
    client_order_id: str
    position_id: str
    quantity: Decimal
    submitted_at_ms: int


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Frozen provider-neutral interface from #376; the union is closed by factories below."""

    binding: VenueBinding
    client_id: ClientId
    venue: Any

    def query(self, intent: TradeIntent) -> AuthoritativeExecutionState: ...

    async def reconcile_account(self, binding: VenueBinding) -> AccountReconciliation: ...

    def probe_quote(self, binding: VenueBinding, instrument: str) -> BoundedQuoteProbe: ...

    def submit_entry(
        self,
        fence: SubmissionFenceV1,
        q2: ExecutionQuoteSnapshotV1,
    ) -> SubmitReceipt: ...

    def ensure_protection(self, intent: TradeIntent, fill: AuthoritativeFill) -> ProtectionReceipt: ...

    def close_and_reconcile(self, intent: TradeIntent) -> FlatReceipt: ...


class NautilusExecutionPorts(Protocol):
    """Provider-neutral Nautilus operations owned by the single lifecycle coordinator."""

    def execution_query(
        self,
        *,
        binding: VenueBinding,
        client_id: ClientId,
        intent: TradeIntent,
    ) -> AuthoritativeExecutionState: ...

    def execution_probe_quote(
        self,
        *,
        binding: VenueBinding,
        instrument_id: str,
    ) -> BoundedQuoteProbe: ...

    def execution_submit_entry(
        self,
        *,
        binding: VenueBinding,
        client_id: ClientId,
        fence: SubmissionFenceV1,
        q2: ExecutionQuoteSnapshotV1,
    ) -> SubmitReceipt: ...

    def execution_ensure_protection(
        self,
        *,
        binding: VenueBinding,
        client_id: ClientId,
        intent: TradeIntent,
        fill: AuthoritativeFill,
    ) -> ProtectionReceipt: ...

    def execution_close_and_reconcile(
        self,
        *,
        binding: VenueBinding,
        client_id: ClientId,
        intent: TradeIntent,
    ) -> FlatReceipt: ...


AccountReportLoader = Callable[[Any], Awaitable[tuple[list[Any], list[Any]]]]


class _ClosedNautilusExecutionAdapter:
    def __init__(
        self,
        *,
        binding: VenueBinding,
        client_id: ClientId,
        venue: Any,
        ports: NautilusExecutionPorts | None = None,
        account_client: Any | None = None,
        account_report_loader: AccountReportLoader = load_complete_account_reports,
    ) -> None:
        self.binding = binding
        self.client_id = client_id
        self.venue = venue
        self._ports = ports
        self._account_client = account_client
        self._account_report_loader = account_report_loader

    def _require_intent(self, intent: TradeIntent) -> NautilusExecutionPorts:
        if intent.binding != self.binding:
            raise ValueError("execution_adapter_intent_binding_mismatch")
        if self._ports is None:
            raise RuntimeError("execution_adapter_lifecycle_ports_missing")
        return self._ports

    def query(self, intent: TradeIntent) -> AuthoritativeExecutionState:
        ports = self._require_intent(intent)
        return ports.execution_query(binding=self.binding, client_id=self.client_id, intent=intent)

    async def reconcile_account(self, binding: VenueBinding) -> AccountReconciliation:
        if binding != self.binding:
            raise ValueError("execution_adapter_account_binding_mismatch")
        client = self._account_client
        if client is None:
            raise RuntimeError("execution_adapter_account_client_missing")
        position_reports, order_reports = await self._account_report_loader(client)
        return AccountReconciliation(
            binding=self.binding,
            position_reports=tuple(position_reports),
            order_reports=tuple(order_reports),
        )

    def probe_quote(self, binding: VenueBinding, instrument: str) -> BoundedQuoteProbe:
        if binding != self.binding:
            raise ValueError("execution_adapter_quote_binding_mismatch")
        if self._ports is None:
            raise RuntimeError("execution_adapter_lifecycle_ports_missing")
        return self._ports.execution_probe_quote(binding=binding, instrument_id=instrument)

    def submit_entry(
        self,
        fence: SubmissionFenceV1,
        q2: ExecutionQuoteSnapshotV1,
    ) -> SubmitReceipt:
        if self._ports is None:
            raise RuntimeError("execution_adapter_lifecycle_ports_missing")
        return self._ports.execution_submit_entry(
            binding=self.binding,
            client_id=self.client_id,
            fence=fence,
            q2=q2,
        )

    def ensure_protection(self, intent: TradeIntent, fill: AuthoritativeFill) -> ProtectionReceipt:
        ports = self._require_intent(intent)
        return ports.execution_ensure_protection(
            binding=self.binding,
            client_id=self.client_id,
            intent=intent,
            fill=fill,
        )

    def close_and_reconcile(self, intent: TradeIntent) -> FlatReceipt:
        ports = self._require_intent(intent)
        return ports.execution_close_and_reconcile(
            binding=self.binding,
            client_id=self.client_id,
            intent=intent,
        )


class BinanceExecutionAdapter(_ClosedNautilusExecutionAdapter):
    def __init__(
        self,
        *,
        ports: NautilusExecutionPorts | None = None,
        account_client: Any | None = None,
        account_report_loader: AccountReportLoader = load_complete_account_reports,
    ) -> None:
        super().__init__(
            binding="BINANCE_USDM",
            client_id=ClientId(BINANCE),
            venue=BINANCE_VENUE,
            ports=ports,
            account_client=account_client,
            account_report_loader=account_report_loader,
        )


class HyperliquidExecutionAdapter(_ClosedNautilusExecutionAdapter):
    def __init__(
        self,
        *,
        ports: NautilusExecutionPorts | None = None,
        account_client: Any | None = None,
        account_report_loader: AccountReportLoader = load_complete_account_reports,
    ) -> None:
        super().__init__(
            binding="HYPERLIQUID_PERP",
            client_id=ClientId(HYPERLIQUID),
            venue=HYPERLIQUID_VENUE,
            ports=ports,
            account_client=account_client,
            account_report_loader=account_report_loader,
        )


def strategy_execution_adapters(ports: NautilusExecutionPorts) -> dict[VenueBinding, ExecutionAdapter]:
    """Construct the exact two-member union; configuration only selects a member, never a plugin."""

    return {
        "BINANCE_USDM": BinanceExecutionAdapter(ports=ports),
        "HYPERLIQUID_PERP": HyperliquidExecutionAdapter(ports=ports),
    }


def account_execution_adapter(
    binding: VenueBinding,
    client: Any,
    *,
    account_report_loader: AccountReportLoader = load_complete_account_reports,
) -> ExecutionAdapter:
    """Bind one provider client to its one compile-time adapter implementation."""

    if binding == "BINANCE_USDM":
        return BinanceExecutionAdapter(
            account_client=client,
            account_report_loader=account_report_loader,
        )
    if binding == "HYPERLIQUID_PERP":
        return HyperliquidExecutionAdapter(
            account_client=client,
            account_report_loader=account_report_loader,
        )
    raise AssertionError("execution_adapter_binding_unreachable")


__all__ = [
    "AccountReconciliation",
    "AuthoritativeExecutionState",
    "AuthoritativeFill",
    "BinanceExecutionAdapter",
    "BoundedQuoteProbe",
    "ExecutionAdapter",
    "FlatReceipt",
    "HyperliquidExecutionAdapter",
    "NautilusExecutionPorts",
    "ProtectionReceipt",
    "SubmitReceipt",
    "account_execution_adapter",
    "strategy_execution_adapters",
]
