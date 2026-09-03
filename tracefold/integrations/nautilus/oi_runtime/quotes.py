"""Concrete owner of which instruments this Runtime pays the venue to stream."""

from __future__ import annotations

from typing import Any

from nautilus_trader.model.identifiers import InstrumentId

from .state import QUOTE_WARMUP_NS, RuntimeExecutionState


class QuoteStreamCoordinator:
    """One quote subscription per instrument an execution actually needs, and no others.

    `on_start` used to subscribe all ~500 routed USDT perpetuals at once. Binance answered by closing
    the market-data WebSocket with 1008 `Too many requests`, and every illiquid route that did connect
    kept feeding `market_stale` refusals to a Runtime that holds at most one position (#510 E). A
    stream is opened when an admitted entry needs a mark and closed when nothing needs one.
    """

    def __init__(self, *, engine: Any, state: RuntimeExecutionState) -> None:
        self._engine = engine
        self._state = state

    @property
    def subscribed(self) -> frozenset[InstrumentId]:
        return frozenset(self._state.quote_subscriptions)

    def ensure(self, instrument_id: InstrumentId, now_ns: int) -> int:
        """Subscribe on first need; return when this instrument's subscription started."""

        subscribed_at_ns = self._state.quote_subscriptions.get(instrument_id)
        if subscribed_at_ns is not None:
            return subscribed_at_ns
        self._engine.subscribe_quote_ticks(instrument_id)
        self._state.quote_subscriptions[instrument_id] = now_ns
        return now_ns

    def release(self, instrument_id: InstrumentId) -> None:
        """Close the stream as soon as no execution on this instrument still needs a mark."""

        if any(
            state.route.instrument_id == instrument_id and (state.active or state.position_quantity > 0)
            for state in self._state.executions.values()
        ):
            return
        if self._state.quote_subscriptions.pop(instrument_id, None) is None:
            return
        self._engine.unsubscribe_quote_ticks(instrument_id)

    def sweep(self, now_ns: int) -> None:
        """Close a stream an admission opened and then refused, once its warm-up window is spent."""

        for instrument_id, subscribed_at_ns in tuple(self._state.quote_subscriptions.items()):
            if now_ns - subscribed_at_ns > QUOTE_WARMUP_NS:
                self.release(instrument_id)

    def release_all(self) -> None:
        for instrument_id in tuple(self._state.quote_subscriptions):
            del self._state.quote_subscriptions[instrument_id]
            self._engine.unsubscribe_quote_ticks(instrument_id)


__all__ = ["QuoteStreamCoordinator"]
