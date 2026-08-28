from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tracefold.app.workers.wiring.manual_trading import (
    ManualAccountMarketSnapshotReader,
    ManualTradingRunner,
    manual_trade_source_from_news_projection,
)
from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.news import TelegramManualTradeProjectionV1
from tracefold.news.market_review.pricing import ProviderQuote
from tracefold.trading import ManualTradeSource, TradeSide

TARGET = "a" * 64


def _projection(**changes: object) -> TelegramManualTradeProjectionV1:
    values: dict[str, object] = {
        "projection_version": "telegram_manual_trade_projection_v1",
        "event_id": "event-42",
        "opened_at_ms": 1_900_000_000_000,
        "final_decision": "push",
        "degraded": False,
        "direction": "bullish",
        "title_zh": "BTC ETF 净流入创纪录",
        "primary_assets": ("BTC",),
        "grounded_assets": ("BTC",),
    }
    values.update(changes)
    return TelegramManualTradeProjectionV1(**values)  # type: ignore[arg-type]


def test_news_projection_accepts_one_grounded_primary_direction() -> None:
    source = manual_trade_source_from_news_projection(
        _projection(),
        message_id=42,
        target_sha256=TARGET,
    )

    assert source is not None
    assert source.news_event_id == "event-42"
    assert source.base_symbol == "BTC"
    assert source.side is TradeSide.LONG
    assert source.delivery_message_id == 42
    assert source.delivery_target_sha256 == TARGET


def test_news_projection_maps_bearish_to_short() -> None:
    source = manual_trade_source_from_news_projection(
        _projection(direction="bearish"),
        message_id=42,
        target_sha256=TARGET,
    )

    assert source is not None
    assert source.side is TradeSide.SHORT
    assert source.headline_zh == "BTC ETF 净流入创纪录"


def test_news_projection_fails_closed_for_non_tradeable_news() -> None:
    rejected = (
        _projection(final_decision="drop"),
        _projection(degraded=True),
        _projection(direction="neutral"),
        _projection(primary_assets=()),
        _projection(primary_assets=("BTC", "ETH")),
        _projection(primary_assets=("ETH",)),
    )

    for projection in rejected:
        assert manual_trade_source_from_news_projection(projection, message_id=42, target_sha256=TARGET) is None


class _Database:
    def __init__(self) -> None:
        self.trading = SimpleNamespace(
            manual_account_snapshot=lambda _account_ref: {
                "account_ref": "binance-manual-demo-1",
                "venue": "binance_usdm_demo",
                "equity_usd": Decimal("1234.56"),
                "observed_at_ms": 1_900_000_000_000,
            }
        )

    async def read(self, _name: str, fn: object, *, timeout_seconds: float):
        assert timeout_seconds == 3.0
        return fn(SimpleNamespace(trading=self.trading))  # type: ignore[operator]

    async def tx(self, _name: str, fn: object, *, timeout_seconds: float):
        assert timeout_seconds == 3.0
        return fn(SimpleNamespace(trading=self.trading))  # type: ignore[operator]


def _source() -> ManualTradeSource:
    return ManualTradeSource(
        news_event_id="event-42",
        delivery_target_sha256=TARGET,
        delivery_message_id=42,
        headline_zh="BTC ETF 净流入创纪录",
        base_symbol="BTC",
        side=TradeSide.LONG,
        source_observed_at_ms=1_899_999_999_000,
    )


def test_snapshot_reader_combines_fresh_executor_equity_and_public_market_price() -> None:
    async def quotes(symbols: tuple[str, ...]):
        assert symbols == ("BTCUSDT",)
        return (
            ProviderQuote(
                venue_symbol="BTCUSDT",
                price=Decimal("64250.10"),
                source_at_ms=1_900_000_000_005,
            ),
        )

    snapshot = asyncio.run(
        ManualAccountMarketSnapshotReader(
            _Database(),
            account_ref="binance-manual-demo-1",
            clock_ms=lambda: 1_900_000_000_010,
            quote_fetcher=quotes,
        ).snapshot_for(_source())
    )

    assert snapshot.account_equity_usd == Decimal("1234.56")
    assert snapshot.reference_entry == Decimal("64250.10")
    assert snapshot.instrument_id == "BTCUSDT"


def test_snapshot_reader_rejects_stale_executor_equity_before_fetching_market() -> None:
    database = _Database()
    database.trading.manual_account_snapshot = lambda _account_ref: {
        "venue": "binance_usdm_demo",
        "equity_usd": Decimal("1234.56"),
        "observed_at_ms": 1_899_999_900_000,
    }

    with pytest.raises(RuntimeError, match="manual_trading_account_snapshot_stale"):
        asyncio.run(
            ManualAccountMarketSnapshotReader(
                database,
                account_ref="binance-manual-demo-1",
                clock_ms=lambda: 1_900_000_000_010,
            ).snapshot_for(_source())
        )


def test_runner_advances_cursor_only_after_controller_settlement() -> None:
    update = TelegramTradingUpdate(
        update_id=101,
        callback_query_id="callback-101",
        actor_user_id=123456789,
        chat_id=-1001234567890,
        message_id=42,
        data="tf:trade:v1",
        authorized=True,
    )
    state = {"cursor": 0, "claimed": False, "settled": False}

    def claim(value: TelegramTradingUpdate, *, now_ms: int) -> bool:
        assert value is update and now_ms > 0
        if state["claimed"]:
            return False
        state["claimed"] = True
        return True

    def settle(update_id: int, *, result_code: str, now_ms: int) -> bool:
        assert update_id == 101 and result_code == "session_created" and now_ms > 0
        state["settled"] = True
        state["cursor"] = 102
        return True

    trading = SimpleNamespace(
        manual_next_telegram_update_id=lambda: state["cursor"],
        claim_manual_telegram_update=claim,
        manual_telegram_update_state=lambda _update_id: "SETTLED" if state["settled"] else "RECEIVED",
        settle_manual_telegram_update=settle,
        terminalize_stale_manual_notifications=lambda **_kwargs: 0,
        begin_manual_notification=lambda **_kwargs: None,
    )
    database = _Database()
    database.trading = trading

    class Client:
        def poll_updates(self, *, next_update_id: int):
            assert next_update_id == 0
            return (update,)

    class Finite:
        async def run(self, _name: str, function: object, /, *args: object, timeout_seconds: float, **kwargs: object):
            assert timeout_seconds == 8.0
            return function(*args, **kwargs)  # type: ignore[operator]

    class Controller:
        async def handle(self, value: TelegramTradingUpdate) -> str:
            assert value is update
            assert state["cursor"] == 0
            return "session_created"

    runner = ManualTradingRunner(
        database=database,
        client=Client(),  # type: ignore[arg-type]
        controller=Controller(),  # type: ignore[arg-type]
        finite=Finite(),
        poll_seconds=1.0,
        clock_ms=lambda: 1_900_000_000_000,
    )

    assert asyncio.run(runner.turn()) == 1
    assert state == {"cursor": 102, "claimed": True, "settled": True}


def test_notification_reply_still_runs_when_interaction_edit_is_ambiguous() -> None:
    state: dict[str, object] = {
        "claimed": False,
        "interaction": "PENDING",
        "reply": "PENDING",
        "overall": "PENDING",
        "reply_called": False,
    }

    def begin_notification(*, now_ms: int):
        if state["claimed"]:
            return None
        state["claimed"] = True
        state["overall"] = "SENDING"
        return {
            "notification_id": "a" * 64,
            "source_message_id": 42,
            "interaction_message_id": 99,
            "notification_kind": "ORDER_REJECTED",
            "payload": {"leg": "entry"},
            "interaction_state": "PENDING",
            "reply_state": "PENDING",
            "attempted_at_ms": now_ms,
        }

    def begin_effect(_notification_id: str, *, effect: str, now_ms: int) -> bool:
        assert now_ms > 0 and state[effect] == "PENDING"
        state[effect] = "SENDING"
        return True

    def mark_interaction(_notification_id: str, *, error_code: str, now_ms: int) -> bool:
        assert error_code.startswith("interaction_") and now_ms > 0
        state["interaction"] = "AMBIGUOUS"
        return True

    def settle_reply(_notification_id: str, *, provider_message_id: int, now_ms: int) -> bool:
        assert provider_message_id == 888 and now_ms > 0
        state["reply"] = "SENT"
        state["overall"] = "SENT"
        return True

    trading = SimpleNamespace(
        manual_next_telegram_update_id=lambda: 0,
        terminalize_stale_manual_notifications=lambda **_kwargs: 0,
        begin_manual_notification=begin_notification,
        begin_manual_notification_effect=begin_effect,
        mark_manual_notification_interaction_ambiguous=mark_interaction,
        settle_manual_notification=settle_reply,
    )
    database = _Database()
    database.trading = trading

    class Client:
        def poll_updates(self, *, next_update_id: int):
            assert next_update_id == 0
            return ()

        def edit_interaction(self, **_kwargs: object) -> None:
            raise RuntimeError("edit failed after attempt")

        def send_interaction_reply(self, **_kwargs: object) -> int:
            state["reply_called"] = True
            return 888

    class Finite:
        async def run(self, _name: str, function: object, /, *args: object, timeout_seconds: float, **kwargs: object):
            assert timeout_seconds == 8.0
            return function(*args, **kwargs)  # type: ignore[operator]

    runner = ManualTradingRunner(
        database=database,
        client=Client(),  # type: ignore[arg-type]
        controller=object(),  # type: ignore[arg-type]
        finite=Finite(),
        poll_seconds=1.0,
        clock_ms=lambda: 1_900_000_000_000,
    )

    assert asyncio.run(runner.turn()) == 0
    assert state == {
        "claimed": True,
        "interaction": "AMBIGUOUS",
        "reply": "SENT",
        "overall": "SENT",
        "reply_called": True,
    }
