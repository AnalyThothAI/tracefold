"""Nautilus ``QuoteTick`` conversion for Trading's provider-neutral Quote authority."""

from __future__ import annotations

from nautilus_trader.model.data import QuoteTick

from tracefold.trading.quote_authority import ExecutionQuote


def execution_quote_from_nautilus(quote: QuoteTick, *, stream_generation: int) -> ExecutionQuote:
    if not isinstance(quote, QuoteTick):
        raise TypeError("execution_quote_requires_nautilus_quote_tick")
    if stream_generation < 0:
        raise ValueError("execution_quote_generation_invalid")
    return ExecutionQuote(
        instrument_id=quote.instrument_id.value,
        bid=quote.bid_price.as_decimal(),
        ask=quote.ask_price.as_decimal(),
        ts_event_ns=int(quote.ts_event),
        ts_init_ns=int(quote.ts_init),
        stream_generation=stream_generation,
    )


__all__ = ["execution_quote_from_nautilus"]
