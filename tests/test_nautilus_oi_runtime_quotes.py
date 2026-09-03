"""On-demand quote subscriptions: what the Runtime pays the venue to stream, and when (#510 PR-5b).

Production evidence: `on_start` subscribed every routed USDT perpetual - 525 in the PR-0 capacity
receipt - and Binance closed the market-data WebSocket with 1008 `Too many requests`, while the
illiquid routes that did connect kept feeding `market_stale` refusals to a Runtime that holds at most
one position (#510 E).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import InstrumentId, PositionId
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs

from tests.nautilus_oi_runtime_fixtures import (
    ACCOUNT_ID,
    NOW_NS,
    SignalRows,
    oi_profile,
    registered_oi_strategy,
    trade_signal,
)
from tracefold.integrations.nautilus.oi_runtime.config import OiInstrumentRoute
from tracefold.integrations.nautilus.oi_runtime.observations import RETRYABLE_ENTRY_REASONS
from tracefold.integrations.nautilus.oi_runtime.state import QUOTE_WARMUP_NS

_CATALOGUE_ROUTES = 525


def _catalogue_profile() -> object:
    """The route catalogue at the size `_discover_routes` actually returns on Binance USD-M."""

    btc = TestInstrumentProvider.btcusdt_perp_binance()
    routes = [OiInstrumentRoute(market_key="crypto:perp:BTC:USDT", instrument_id=btc.id, stop_distance_bps=200)]
    routes.extend(
        OiInstrumentRoute(
            market_key=f"crypto:perp:SYN{index}:USDT",
            instrument_id=InstrumentId.from_str(f"SYN{index}USDT-PERP.BINANCE"),
            stop_distance_bps=200,
        )
        for index in range(_CATALOGUE_ROUTES - 1)
    )
    return replace(oi_profile(), routes=tuple(routes))


def test_start_subscribes_no_quotes_however_large_the_route_catalogue_is() -> None:
    profile = _catalogue_profile()
    context = registered_oi_strategy(profile=profile)

    context.strategy.on_start()

    assert len(profile.routes) == _CATALOGUE_ROUTES
    assert context.strategy.subscribed == []
    assert context.strategy.quote_subscriptions == frozenset()


def test_an_admitted_entry_subscribes_exactly_its_own_instrument() -> None:
    profile = _catalogue_profile()
    context = registered_oi_strategy(values=(trade_signal(),), profile=profile)
    context.strategy.on_start()

    context.strategy.on_timer(None)

    assert len(context.strategy.submitted) == 1
    assert context.strategy.subscribed == [context.instrument.id]
    assert context.strategy.quote_subscriptions == frozenset({context.instrument.id})


def test_no_order_is_sized_before_the_first_tick_and_the_signal_is_redelivered_not_refused() -> None:
    signal = trade_signal()
    context = registered_oi_strategy(values=(signal,), with_quote=False)
    context.strategy.on_start()

    context.strategy.on_timer(None)

    # Subscribed, nothing submitted, and no durable verdict: the Signal stays unresolved so the next
    # indexed poll offers it again inside its TTL.
    assert context.strategy.quote_subscriptions == frozenset({context.instrument.id})
    assert context.strategy.submitted == []
    assert context.audit.queued_count == 0
    assert context.signals.pending_ids == frozenset()
    assert "market_subscription_pending" in RETRYABLE_ENTRY_REASONS

    context.cache.add_quote_tick(
        TestDataStubs.quote_tick(
            instrument=context.instrument,
            bid_price=9_999,
            ask_price=10_000,
            ts_event=NOW_NS,
            ts_init=NOW_NS,
        )
    )
    context.signals.poll_once(SignalRows(signal))
    context.strategy.on_timer(None)

    assert len(context.strategy.submitted) == 1
    assert context.strategy.subscribed == [context.instrument.id]


def test_a_market_that_never_ticks_is_refused_terminally_once_the_warm_up_is_spent() -> None:
    signal = trade_signal()
    context = registered_oi_strategy(values=(signal,), with_quote=False)
    context.strategy.on_start()
    context.strategy.on_timer(None)
    assert context.audit.queued_count == 0

    context.clock.set_time(NOW_NS + QUOTE_WARMUP_NS + 1)
    context.signals.poll_once(SignalRows(signal))
    context.strategy.on_timer(None)

    disposition = context.audit.flush_once(lambda _values: None)[0]
    assert disposition.normalized_kind == "signal_disposition"
    assert disposition.summary == {"disposition": "instrument_or_market_missing"}
    assert context.strategy.submitted == []
    # The refused stream is closed by the same pump that refused it; nothing holds it open.
    assert context.strategy.quote_subscriptions == frozenset()
    assert context.strategy.unsubscribed == [context.instrument.id]


def test_a_closed_position_gives_the_quote_stream_back() -> None:
    context = registered_oi_strategy(values=(trade_signal(),))
    context.strategy.on_start()
    context.strategy.on_timer(None)
    entry = context.strategy.submitted[0][0]
    position_id = PositionId("BTCUSDT-PERP.BINANCE-OI-RUNTIME")
    context.strategy.on_position_opened(
        SimpleNamespace(
            instrument_id=context.instrument.id,
            account_id=ACCOUNT_ID,
            strategy_id=context.strategy.id,
            opening_order_id=entry.client_order_id,
            side=PositionSide.LONG,
            position_id=position_id,
            quantity=context.instrument.make_qty(Decimal("0.049")),
            avg_px_open=10_000.0,
            ts_opened=NOW_NS + 2,
        )
    )

    assert context.strategy.quote_subscriptions == frozenset({context.instrument.id})

    context.strategy.on_position_closed(
        SimpleNamespace(
            instrument_id=context.instrument.id,
            account_id=ACCOUNT_ID,
            strategy_id=context.strategy.id,
            position_id=position_id,
            quantity=context.instrument.make_qty(Decimal(0)),
            ts_closed=NOW_NS + 3,
        )
    )

    assert context.strategy.quote_subscriptions == frozenset()
    assert context.strategy.unsubscribed == [context.instrument.id]


def test_stopping_hands_back_every_stream_it_opened_and_no_others() -> None:
    profile = _catalogue_profile()
    context = registered_oi_strategy(values=(trade_signal(),), profile=profile)
    context.strategy.on_start()
    context.strategy.on_timer(None)

    context.strategy.on_stop()

    assert context.strategy.unsubscribed == [context.instrument.id]
    assert context.strategy.quote_subscriptions == frozenset()
