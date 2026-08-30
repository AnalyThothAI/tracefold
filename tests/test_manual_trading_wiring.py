from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tracefold.app.workers.wiring.manual_trading import (
    ManualAccountMarketSnapshotReader,
    ManualTradingRunner,
    manual_trade_sources_from_news_projection,
)
from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.news import TelegramManualTradeProjectionV1
from tracefold.news.manual_trade_projection import displayed_assets_from_telegram_card
from tracefold.news.market_review.pricing import ProviderQuote
from tracefold.news.storage.decisions import DecisionStorage
from tracefold.trading import ManualTradeSource, TradeSide

TARGET = "a" * 64


def test_telegram_trade_assets_come_from_the_rendered_target_line_not_news_text() -> None:
    card = {
        "header": {"title": {"content": "ZEC、BTC 与 SOL 资金轮转"}},
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    "该地址抛售 ZEC，资金轮转至 BTC 和 SOL。\n"
                    "利空 · 影响明显 · HYPE · mlm onchain · 16:55\n"
                    "行情 HYPE $81.111 24h -2.69%"
                ),
            }
        ],
    }

    assert displayed_assets_from_telegram_card(card) == ("HYPE",)


def test_telegram_trade_assets_preserve_multiple_rendered_targets_in_display_order() -> None:
    card = {
        "elements": [
            {
                "tag": "markdown",
                "content": "正文里的 XRP 不算。\n利多 · 影响重大 · HYPE ETH · provider · 09:01",
            }
        ],
    }

    assert displayed_assets_from_telegram_card(card) == ("HYPE", "ETH")


def test_telegram_target_projection_preserves_an_exact_contract_address() -> None:
    contract = "0x5317c0d077d2eeb639448939b930d49c4984b63b"
    card = {
        "elements": [
            {
                "tag": "markdown",
                "content": f"正文不参与标的判断。\n利多 · 影响重大 · {contract} · provider · 09:01",
            }
        ],
    }

    assert displayed_assets_from_telegram_card(card) == (contract,)


def test_storage_projection_reads_only_targets_persisted_on_the_sent_telegram_card() -> None:
    class Result:
        def fetchall(self) -> list[dict[str, object]]:
            return [
                {
                    "event_id": "event-42",
                    "card": {
                        "header": {"title": {"content": "正文提到 BTC、SOL 和 XRP"}},
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": (
                                    "正文里的 BTC、SOL 和 XRP 不算。\n利多 · 影响重大 · HYPE ETH · provider · 09:01"
                                ),
                            }
                        ],
                    },
                }
            ]

    class Connection:
        def execute(self, _query: str, params: tuple[str, str]) -> Result:
            assert params == ("42", TARGET)
            return Result()

    class Storage(DecisionStorage):
        conn = Connection()

        def event_detail(self, event_id: str) -> dict[str, object]:
            assert event_id == "event-42"
            return {
                "event": {
                    "event_id": event_id,
                    "opened_at_ms": 1_900_000_000_000,
                    "grounded_assets": ["BTC", "SOL", "XRP"],
                },
                "triage": {
                    "final_decision": "push",
                    "degraded": False,
                    "direction": "bullish",
                    "headline_zh": "正文提到 BTC、SOL 和 XRP",
                    "assets": [{"symbol": "BTC", "role": "primary"}],
                },
            }

    projection = Storage().telegram_manual_trade_projection(message_id=42, target_sha256=TARGET)

    assert projection is not None
    assert projection.displayed_assets == ("HYPE", "ETH")


@pytest.mark.parametrize(
    ("edit_state", "delete_state"),
    (
        ("editing", None),
        ("ambiguous", None),
        (None, "deleting"),
        (None, "ambiguous"),
    ),
)
def test_storage_projection_rejects_uncertain_telegram_card_state(
    edit_state: str | None,
    delete_state: str | None,
) -> None:
    class Result:
        def fetchall(self) -> list[dict[str, object]]:
            return [
                {
                    "event_id": "event-42",
                    "card": {"elements": [{"tag": "markdown", "content": "利多 · HYPE · provider · 09:01"}]},
                    "edit_state": edit_state,
                    "delete_state": delete_state,
                }
            ]

    class Connection:
        def execute(self, query: str, _params: tuple[str, str]) -> Result:
            assert "(edit_state IS NULL OR edit_state = 'edited')" in query
            assert "delete_state IS NULL" in query
            return Result()

    class Storage(DecisionStorage):
        conn = Connection()

        def event_detail(self, _event_id: str) -> dict[str, object]:
            raise AssertionError("uncertain Telegram state must fail before reading Event detail")

    assert Storage().telegram_manual_trade_projection(message_id=42, target_sha256=TARGET) is None


def _projection(**changes: object) -> TelegramManualTradeProjectionV1:
    values: dict[str, object] = {
        "projection_version": "telegram_manual_trade_projection_v1",
        "event_id": "event-42",
        "opened_at_ms": 1_900_000_000_000,
        "final_decision": "push",
        "degraded": False,
        "direction": "bullish",
        "title_zh": "BTC ETF 净流入创纪录",
        "displayed_assets": ("BTC",),
    }
    values.update(changes)
    return TelegramManualTradeProjectionV1(**values)  # type: ignore[arg-type]


def test_news_projection_uses_only_the_assets_displayed_on_the_telegram_card() -> None:
    sources = manual_trade_sources_from_news_projection(
        _projection(),
        message_id=42,
        target_sha256=TARGET,
    )

    assert len(sources) == 1
    source = sources[0]
    assert source.news_event_id == "event-42"
    assert source.base_symbol == "BTC"
    assert source.side is TradeSide.LONG
    assert source.delivery_message_id == 42
    assert source.delivery_target_sha256 == TARGET


def test_news_projection_maps_bearish_to_short() -> None:
    sources = manual_trade_sources_from_news_projection(
        _projection(direction="bearish"),
        message_id=42,
        target_sha256=TARGET,
    )

    assert len(sources) == 1
    source = sources[0]
    assert source.side is TradeSide.SHORT
    assert source.headline_zh == "BTC ETF 净流入创纪录"


def test_news_projection_preserves_every_displayed_asset_for_operator_selection() -> None:
    sources = manual_trade_sources_from_news_projection(
        _projection(
            title_zh="正文同时提到 ZEC、BTC 和 SOL",
            displayed_assets=("HYPE", "ETH"),
        ),
        message_id=42,
        target_sha256=TARGET,
    )

    assert tuple(source.base_symbol for source in sources) == ("HYPE", "ETH")


def test_news_projection_fails_closed_for_non_tradeable_news() -> None:
    rejected = (
        _projection(final_decision="drop"),
        _projection(degraded=True),
        _projection(direction="neutral"),
        _projection(displayed_assets=()),
    )

    for projection in rejected:
        assert manual_trade_sources_from_news_projection(projection, message_id=42, target_sha256=TARGET) == ()


class _Database:
    def __init__(self) -> None:
        self.trading = SimpleNamespace(
            manual_account_snapshot=lambda _account_ref: {
                "account_ref": "binance-manual-live-1",
                "venue": "binance_usdm_live",
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
            account_ref="binance-manual-live-1",
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
        "venue": "binance_usdm_live",
        "equity_usd": Decimal("1234.56"),
        "observed_at_ms": 1_899_999_900_000,
    }

    with pytest.raises(RuntimeError, match="manual_trading_account_snapshot_stale"):
        asyncio.run(
            ManualAccountMarketSnapshotReader(
                database,
                account_ref="binance-manual-live-1",
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


def test_runner_syncs_each_private_profiles_command_menu_before_polling() -> None:
    observed: list[tuple[str, object]] = []
    trading = SimpleNamespace(
        manual_next_telegram_update_id=lambda: 0,
        terminalize_stale_manual_notifications=lambda **_kwargs: 0,
        begin_manual_notification=lambda **_kwargs: None,
    )
    database = _Database()
    database.trading = trading

    class Client:
        def set_commands(self, *, chat_id: int, commands: tuple[tuple[str, str], ...]) -> None:
            observed.append(("commands", (chat_id, commands)))

        def poll_updates(self, *, next_update_id: int):
            observed.append(("poll", next_update_id))
            return ()

    class Finite:
        async def run(self, name: str, function: object, /, *args: object, timeout_seconds: float, **kwargs: object):
            assert timeout_seconds == 8.0
            observed.append(("finite", name))
            return function(*args, **kwargs)  # type: ignore[operator]

    runner = ManualTradingRunner(
        database=database,
        client=Client(),  # type: ignore[arg-type]
        controller=object(),  # type: ignore[arg-type]
        finite=Finite(),
        poll_seconds=1.0,
        command_menus={
            111: (("start", "查看可用指令"), ("test_futures", "发送合约测试新闻")),
            222: (("start", "查看可用指令"), ("test_onchain", "发送链上测试新闻")),
        },
    )

    assert asyncio.run(runner.turn()) == 0
    assert observed[:6] == [
        ("finite", "manual_telegram_command_menu"),
        (
            "commands",
            (111, (("start", "查看可用指令"), ("test_futures", "发送合约测试新闻"))),
        ),
        ("finite", "manual_telegram_command_menu"),
        (
            "commands",
            (222, (("start", "查看可用指令"), ("test_onchain", "发送链上测试新闻"))),
        ),
        ("finite", "manual_telegram_poll"),
        ("poll", 0),
    ]


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
