"""Telegram News delivery: one configured channel and one outbound attempt.

Every card here is built by the renderer production builds it with (`news_reader_card`,
`market_reader_card`) and handed to the sender as the `ReaderCard` value object. The adapter used to
receive Feishu's JSON and read the card back out of the rendered markdown, so these tests used to
assert on a string round trip through a second channel's serializer (#562 PR-C).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from tracefold.integrations.telegram import (
    _TELEGRAM_TEXT_MAX,
    TelegramDeliveryError,
    TelegramNewsPushSender,
    _fit_telegram_message,
    _plain_html_text,
)
from tracefold.news import ReaderDeliveryPresentation, ReaderMarketMovement, ReaderTradeTarget
from tracefold.news.delivery import news_reader_card
from tracefold.news.feishu_card import feishu_card
from tracefold.news.market_notifications import MarketObservation, MarketTrack, market_reader_card
from tracefold.news.reader_card import ReaderCard, ReaderCardLink

CHANNEL_ID = -1001234567890
BOT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345"
BOT_TOKEN_ROTATED = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12346"
BOT_ID = 123456


def _preflight_response(request: httpx.Request) -> httpx.Response | None:
    method = request.url.path.rsplit("/", maxsplit=1)[-1]
    payload = json.loads(request.content)
    if method == "getChat":
        assert payload == {"chat_id": CHANNEL_ID}
        return httpx.Response(200, json={"ok": True, "result": {"id": CHANNEL_ID, "type": "channel"}})
    if method == "getMe":
        assert payload == {}
        return httpx.Response(200, json={"ok": True, "result": {"id": BOT_ID, "is_bot": True}})
    if method == "getChatMember":
        assert payload == {"chat_id": CHANNEL_ID, "user_id": BOT_ID}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "status": "administrator",
                    "user": {"id": BOT_ID, "is_bot": True},
                    "can_post_messages": True,
                },
            },
        )
    return None


# 14:32 on the reader's clock, so a card that states no time of its own still states this one.
NEWS_AT_MS = 1_787_898_725_000


def _card(
    *,
    source_url: str = "https://www.coindesk.com/news/1",
    title: str = "BTC ETF 净流入",
    lead: str = "连续第三日净流入",
    direction: str = "bullish",
    magnitude: int = 2,
    novelty: str = "progression",
    assets: Sequence[str] = ("BTC",),
    origin: str = "CoinDesk",
    member_count: int = 2,
    decision: str = "push",
    degraded: bool = False,
    description: str = "",
    quotes: Sequence[Mapping[str, Any]] = (),
) -> ReaderCard:
    """One News first card, from the builder the Deliverer uses."""

    return news_reader_card(
        event={
            "event_id": "abc12345" + "f" * 56,
            "leader_title": title,
            "leader_description": description,
            "leader_url": source_url,
            "reporting_origin": origin,
            "member_count": member_count,
            "leader_published_at_ms": NEWS_AT_MS,
        },
        verdict={
            "direction": direction,
            "magnitude": magnitude,
            "novelty": novelty,
            "headline_zh": title,
            "why_zh": lead,
        },
        decision=decision,
        grounded_assets=list(assets),
        assets=list(assets),
        degraded=degraded,
        quotes=list(quotes),
    )


def _market_card(
    *,
    family: str = "oi",
    reason: str = "first",
    track: Mapping[str, Any] | None = None,
    observations: Sequence[Mapping[str, Any]] = (),
    detail_url: str | None = "https://console.example.test/news/market/i1",
    action_changes: int = 0,
) -> ReaderCard:
    """One market card, from the builder the market notification loop uses."""

    return market_reader_card(
        track=MarketTrack(**{"family": family, **(track or {})}),
        reason=reason,
        observations=[MarketObservation(**row) for row in observations],
        detail_url=detail_url,
        action_changes=action_changes,
    )


def _send(sender: TelegramNewsPushSender, card: ReaderCard, **kwargs: Any) -> dict[str, Any]:
    """One send through the adapter, carrying the channel payload the ledger freezes beside the card.

    Both travel together everywhere a card is sent (#562 PR-C): Feishu posts the payload, this channel
    renders the model. Serializing it here rather than passing a stub keeps the pair the real one.
    """

    return sender.send_card(card, channel_payload=feishu_card(card), **kwargs)


def _edit(
    sender: TelegramNewsPushSender,
    receipt: Mapping[str, Any],
    card: ReaderCard,
    **kwargs: Any,
) -> dict[str, Any]:
    return sender.edit_card(receipt, card, channel_payload=feishu_card(card), **kwargs)


def _without_timing(value: object) -> str:
    """Keep layout assertions deterministic; timing has its own fixed-clock contract tests below."""

    return "\n".join(
        line for line in str(value).splitlines() if not line.startswith(("新闻时间  ", "事件时间  ", "推送时间  "))
    ).rstrip()


def test_sender_posts_scannable_sections_and_links_the_normalized_source_text() -> None:
    observed: dict[str, object] = {}
    methods: list[str] = []
    timeout_totals: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        timeout_totals.append(sum(float(value) for value in request.extensions["timeout"].values()))
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}},
            },
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    assert methods == []

    sender.prepare()
    receipt = _send(sender, _card())

    assert observed["chat_id"] == CHANNEL_ID
    assert _without_timing(observed["text"]) == (
        "🟢 <b>BTC ETF 净流入</b>\n\n"
        "🔄 <b>新进展</b>\n\n"
        "连续第三日净流入\n\n"
        "🎯 <b>标的</b>  BTC\n"
        "新闻后 暂无\n"
        "1h 暂无，\n"
        "24h 暂无\n\n"
        "🧭 <b>方向</b>  利多 · 影响明显\n\n"
        '🔗 <b>来源</b>  <a href="https://www.coindesk.com/news/1">CoinDesk</a> · 2 条报道'
    )
    assert observed["parse_mode"] == "HTML"
    assert "abc12345" not in str(observed["text"])
    assert observed["link_preview_options"] == {"is_disabled": True}
    assert "reply_markup" not in observed
    assert methods == ["getChat", "getMe", "getChatMember", "sendMessage"]
    assert all(total <= 5.0 for total in timeout_totals)
    assert receipt["provider"] == "telegram"
    assert receipt["message_id"] == 42
    assert len(str(receipt["target_sha256"])) == 64
    assert str(CHANNEL_ID) not in json.dumps(receipt)


def test_sender_renders_the_compact_single_asset_layout() -> None:
    observed: dict[str, object] = {}
    card = _card(
        source_url="https://x.com/jukan05/status/1234567890123456789",
        title="美光台湾工厂初步投票支持罢工比例达 80%，工会要求改为利润分红制",
        lead=(
            "美光约 60% 全球产能集中在台湾，是 HBM 先进制程的主力基地，工会参照三星 10.5%、"
            "SK 海力士 10% 的利润分红水平施压，9 月中旬前进入强制调解，若调解破裂将进入罢工投票，"
            "压低美光产能利用率与现金流。"
        ),
        direction="bearish",
        assets=("MU",),
        origin="jukan05",
        member_count=1,
    )

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
        wall_clock_ms=lambda: 1_787_885_313_000,
    )
    sender.prepare()
    _send(
        sender,
        card,
        presentation=ReaderDeliveryPresentation(
            trade_targets=(
                ReaderTradeTarget(
                    ticker="MU",
                    venue="binance.perp",
                    venue_symbol="MUUSDT",
                    base_symbol="MU",
                    quote_asset="USDT",
                ),
            ),
            market_movements=(
                ReaderMarketMovement(
                    ticker="MU",
                    after_news_bps=0,
                    return_1h_bps=-25,
                    change_24h_bps=-511,
                    one_hour_state="available",
                ),
            ),
            news_at_ms=1_787_885_301_000,
            observed_at_ms=1_787_885_301_000,
            novelty="progression",
            progression_from_headline="美光工会此前启动劳资协商",
            progression_review_state="confirmed",
            progression_review_reason="同一工会行动进入罢工投票阶段，新增了明确比例和下一步程序。",
            progression_review_parent_age_minutes=61,
            progression_review_parent_message_id=41,
        ),
    )

    ticker = '<a href="https://www.binance.com/en/futures/MUUSDT">MU</a>'
    assert observed["text"] == (
        "🔴 <b>美光台湾工厂初步投票支持罢工比例达 80%，工会要求改为利润分红制</b>\n\n"
        "🔄 <b>新进展</b>\n"
        "<blockquote>✅ <b>已确认关联</b>\n"
        '↳ <a href="https://t.me/c/1234567890/41">此前：美光工会此前启动劳资协商</a> · 1h 1mins 前</blockquote>\n\n'
        "美光约 60% 全球产能集中在台湾，是 HBM 先进制程的主力基地，工会参照三星 10.5%、"
        "SK 海力士 10% 的利润分红水平施压，9 月中旬前进入强制调解，若调解破裂将进入罢工投票，"
        "压低美光产能利用率与现金流。\n\n"
        f"🎯 <b>标的</b>  {ticker}\n"
        "新闻后 0.00%\n"
        "1h -0.25%，\n"
        "24h -5.11%\n\n"
        "🧭 <b>方向</b>  利空 · 影响明显\n"
        "\n"
        "新闻时间  10:48\n"
        "推送时间  10:48\n"
        '🔗 <b>来源</b>  <a href="https://x.com/jukan05/status/1234567890123456789">jukan05 的推特</a>'
    )


def test_sender_does_not_render_unclear_direction_or_magnitude_as_trade_targets() -> None:
    observed: dict[str, object] = {}
    card = _card(
        source_url="https://x.com/FirstSquawk/status/1234567890123456789",
        title="中国存储芯片厂商长鑫存储起诉五角大楼，挑战涉军企业清单指定",
        lead="长鑫存储在美国法院起诉，要求撤销五角大楼将其列入涉军企业清单的决定。",
        direction="unclear",
        novelty="new_fact",
        assets=("CXMT",),
        origin="FirstSquawk",
        member_count=1,
    )

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(
        sender,
        card,
        presentation=ReaderDeliveryPresentation(
            trade_targets=(
                ReaderTradeTarget(
                    ticker="CXMT",
                    venue="binance.perp",
                    venue_symbol="CXMTUSDT",
                    base_symbol="CXMT",
                    quote_asset="USDT",
                ),
            ),
            market_movements=(
                ReaderMarketMovement(
                    ticker="CXMT",
                    after_news_bps=0,
                    return_1h_bps=8,
                    change_24h_bps=182,
                    one_hour_state="available",
                ),
            ),
            novelty="new_fact",
        ),
    )

    text = str(observed["text"])
    assert text.count("🎯 <b>标的</b>") == 1
    assert "🎯 <b>标的</b>  方向待定" not in text
    assert "🎯 <b>标的</b>  影响明显" not in text
    assert '<a href="https://www.binance.com/en/futures/CXMTUSDT">CXMT</a>' in text
    assert "🧭 <b>方向</b>  方向待定 · 影响明显" in text


def test_sender_puts_new_fact_below_title_and_explains_macro_events_without_a_ticker() -> None:
    observed: dict[str, object] = {}
    card = _card(
        source_url="",
        title="美国 2026 年初步基准非农就业下修 7.9 万人",
        lead="官方就业基线整体下移，利率市场将重新定价劳动力转弱路径。",
        direction="bearish",
        magnitude=3,
        novelty="new_fact",
        assets=(),
        origin="jin10",
        member_count=1,
        decision="escalate",
    )

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
        wall_clock_ms=lambda: 1_787_925_781_000,
    )
    sender.prepare()
    _send(
        sender,
        card,
        presentation=ReaderDeliveryPresentation(
            news_at_ms=1_787_925_762_000,
            market_scope="macro",
            novelty="new_fact",
        ),
    )

    assert observed["text"] == (
        "⚡ <b>美国 2026 年初步基准非农就业下修 7.9 万人</b>\n\n"
        "🆕 <b>新事实</b>\n\n"
        "官方就业基线整体下移，利率市场将重新定价劳动力转弱路径。\n\n"
        "🌐 <b>影响范围</b>  宏观市场 · 暂无直接标的\n\n"
        "🧭 <b>方向</b>  利空 · 影响重大\n\n"
        "新闻时间  22:02\n"
        "推送时间  22:03\n"
        "🔗 <b>来源</b>  金十"
    )


def test_sender_sends_pending_market_data_then_edits_the_same_message() -> None:
    observed: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        payload = json.loads(request.content)
        observed.append((method, payload))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    clock = iter((1_787_885_313_000, 1_787_885_315_000))
    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
        wall_clock_ms=lambda: next(clock),
    )
    sender.prepare()
    initial = _send(
        sender,
        _card(),
        presentation=ReaderDeliveryPresentation(
            news_at_ms=1_787_885_301_000,
            market_data_state="pending",
            novelty="progression",
            progression_review_state="pending",
        ),
    )
    updated = _edit(
        sender,
        initial,
        _card(),
        presentation=ReaderDeliveryPresentation(
            market_movements=(ReaderMarketMovement("BTC", 0, -25, -511, "available"),),
            news_at_ms=1_787_885_301_000,
            novelty="progression",
            progression_review_state="rejected",
            progression_review_reason="候选报道的主体和事件链不同。",
        ),
    )

    assert [method for method, _payload in observed] == ["sendMessage", "editMessageText"]
    assert "新闻后 计算中\n1h 计算中，\n24h 计算中" in str(observed[0][1]["text"])
    assert "🔄 <b>新进展</b>\n<blockquote>⏳ <b>关联确认中</b></blockquote>" in str(observed[0][1]["text"])
    assert observed[1][1]["chat_id"] == CHANNEL_ID
    assert observed[1][1]["message_id"] == 42
    assert "新闻后 0.00%\n1h -0.25%，\n24h -5.11%" in str(observed[1][1]["text"])
    assert "🆕 <b>新事实</b>" in str(observed[1][1]["text"])
    assert "🔄 <b>新进展</b>" not in str(observed[1][1]["text"])
    assert "未确认关联" not in str(observed[1][1]["text"])
    assert "候选报道的主体和事件链不同" not in str(observed[1][1]["text"])
    assert "接续「" not in str(observed[1][1]["text"])
    assert "推送时间  10:48" in str(observed[1][1]["text"])
    assert initial["pushed_at_ms"] == 1_787_885_313_000
    assert updated["pushed_at_ms"] == initial["pushed_at_ms"]
    assert updated["edited_at_ms"] == 1_787_885_315_000


def test_sender_hides_unavailable_progression_evidence_after_downgrading_to_new_fact() -> None:
    observed: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(
        sender,
        _card(),
        presentation=ReaderDeliveryPresentation(
            novelty="progression",
            progression_review_state="unavailable",
            progression_review_reason="上游复核服务暂时不可用，\n请稍后确认。",
        ),
    )

    assert "🆕 <b>新事实</b>" in str(observed["text"])
    assert "🔄 <b>新进展</b>" not in str(observed["text"])
    assert "关联待确认" not in str(observed["text"])
    assert "上游复核服务暂时不可用" not in str(observed["text"])


def test_sender_links_a_fresh_bitget_target_even_when_prices_are_unavailable() -> None:
    observed: dict[str, object] = {}
    card = _card(lead="业绩改善", novelty="new_fact", assets=("2605",), origin="rtpr.io", member_count=1)

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(
        sender,
        card,
        presentation=ReaderDeliveryPresentation(
            trade_targets=(
                ReaderTradeTarget(
                    ticker="2605",
                    venue="bitget.perp",
                    venue_symbol="METALIGHTUSDT",
                    base_symbol="METALIGHT",
                    quote_asset="USDT",
                ),
            )
        ),
    )

    assert '<a href="https://www.bitget.com/futures/usdt/metalightusdt">2605</a>' in str(observed["text"])


def test_edit_failure_does_not_invalidate_the_preflight_for_the_next_initial_send() -> None:
    methods: list[str] = []
    message_id = 40

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal message_id
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        methods.append(method)
        if method == "editMessageText":
            return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request"})
        message_id += 1
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": message_id, "chat": {"id": CHANNEL_ID}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    initial = _send(sender, _card(), presentation=ReaderDeliveryPresentation(market_data_state="pending"))
    with pytest.raises(TelegramDeliveryError, match="news_delivery_telegram_edit_http_rejected"):
        _edit(sender, initial, _card())

    following = _send(sender, _card(), presentation=ReaderDeliveryPresentation(market_data_state="pending"))

    assert methods == ["sendMessage", "editMessageText", "sendMessage"]
    assert following["message_id"] == 42


def test_edit_rejects_noncanonical_receipt_fields_before_calling_telegram() -> None:
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        methods.append(method)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    initial = _send(sender, _card(), presentation=ReaderDeliveryPresentation(market_data_state="pending"))

    with pytest.raises(TelegramDeliveryError, match="news_delivery_telegram_edit_receipt_invalid"):
        _edit(sender, {**initial, "provider_response": "untrusted"}, _card())

    assert methods == ["sendMessage"]


def test_sender_renders_exact_binance_tickers_as_html_links() -> None:
    observed: dict[str, object] = {}
    card = _card(assets=("BTC-USDT", "BTC"))

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(
        sender,
        card,
        presentation=ReaderDeliveryPresentation(
            trade_targets=(
                ReaderTradeTarget(
                    ticker="BTC",
                    venue="binance.perp",
                    venue_symbol="BTCUSDT",
                    base_symbol="BTC",
                    quote_asset="USDT",
                ),
            ),
        ),
    )

    ticker = '<a href="https://www.binance.com/en/futures/BTCUSDT">BTC</a>'
    assert _without_timing(observed["text"]) == (
        "🟢 <b>BTC ETF 净流入</b>\n\n"
        "🔄 <b>新进展</b>\n\n"
        "连续第三日净流入\n\n"
        "🎯 <b>标的</b>  BTC-USDT\n"
        "新闻后 暂无\n"
        "1h 暂无，\n"
        "24h 暂无\n\n"
        f"🎯 <b>标的</b>  {ticker}\n"
        "新闻后 暂无\n"
        "1h 暂无，\n"
        "24h 暂无\n\n"
        "🧭 <b>方向</b>  利多 · 影响明显\n\n"
        '🔗 <b>来源</b>  <a href="https://www.coindesk.com/news/1">CoinDesk</a> · 2 条报道'
    )


def test_sender_renders_each_asset_in_its_own_complete_market_block() -> None:
    observed: dict[str, object] = {}
    card = _card(
        source_url="https://x.com/serenity/status/1234567890123456789",
        lead="资金从 BTC 轮动至 ETH",
        direction="bearish",
        magnitude=3,
        novelty="",
        assets=("BTC", "ETH"),
        origin="serenity",
        member_count=1,
    )

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(
        sender,
        card,
        presentation=ReaderDeliveryPresentation(
            trade_targets=(
                ReaderTradeTarget(
                    ticker="BTC",
                    venue="binance.perp",
                    venue_symbol="BTCUSDT",
                    base_symbol="BTC",
                    quote_asset="USDT",
                ),
                ReaderTradeTarget(
                    ticker="ETH",
                    venue="binance.spot",
                    venue_symbol="ETHUSDT",
                    base_symbol="ETH",
                    quote_asset="USDT",
                ),
            ),
            market_movements=(
                ReaderMarketMovement(
                    ticker="BTC",
                    after_news_bps=110,
                    return_1h_bps=80,
                    change_24h_bps=320,
                    one_hour_state="available",
                ),
                ReaderMarketMovement(
                    ticker="ETH",
                    after_news_bps=-40,
                    return_1h_bps=None,
                    change_24h_bps=170,
                    one_hour_state="unavailable",
                ),
            ),
        ),
    )

    btc = '<a href="https://www.binance.com/en/futures/BTCUSDT">BTC</a>'
    eth = '<a href="https://www.binance.com/en/trade/ETH_USDT">ETH</a>'
    assert _without_timing(observed["text"]) == (
        "🔴 <b>BTC ETF 净流入</b>\n\n"
        "资金从 BTC 轮动至 ETH\n\n"
        f"🎯 <b>标的</b>  {btc}\n"
        "新闻后 +1.10%\n"
        "1h +0.80%，\n"
        "24h +3.20%\n\n"
        f"🎯 <b>标的</b>  {eth}\n"
        "新闻后 -0.40%\n"
        "1h 暂无，\n"
        "24h +1.70%\n\n"
        "🧭 <b>方向</b>  利空 · 影响重大\n\n"
        '🔗 <b>来源</b>  <a href="https://x.com/serenity/status/1234567890123456789">serenity 的推特</a>'
    )
    assert "reply_markup" not in observed


def test_sender_shows_the_news_and_push_times_to_the_minute() -> None:
    """`HH:MM`, from `card_format.clock`: a card is not a log line, and one clock serves both cards."""

    observed: dict[str, object] = {}
    card = _card(
        source_url="https://www.bloomberg.com/news/articles/2026-08-28/bitcoin",
        lead="现货 ETF 资金继续流入",
        novelty="new_fact",
        origin="Bloomberg",
        member_count=1,
    )

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
        wall_clock_ms=lambda: 1_787_898_733_400,
    )
    sender.prepare()
    _send(
        sender,
        card,
        presentation=ReaderDeliveryPresentation(
            news_at_ms=1_787_898_725_000,
            observed_at_ms=1_787_898_725_000,
        ),
    )

    assert str(observed["text"]).endswith(
        "新闻时间  14:32\n"
        "推送时间  14:32\n"
        '🔗 <b>来源</b>  <a href="https://www.bloomberg.com/news/articles/2026-08-28/bitcoin">彭博社</a>'
    )
    assert "处理时长" not in str(observed["text"])


def test_sender_keeps_known_push_time_when_news_time_is_missing() -> None:
    observed: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
        wall_clock_ms=lambda: 1_787_898_733_400,
    )
    sender.prepare()
    card = _card()
    card = replace(card, times=replace(card.times, event_at_ms=None))
    _send(sender, card, presentation=ReaderDeliveryPresentation())

    assert str(observed["text"]).endswith(
        "新闻时间  暂无\n"
        "推送时间  14:32\n"
        '🔗 <b>来源</b>  <a href="https://www.coindesk.com/news/1">CoinDesk</a> · 2 条报道'
    )


def test_sender_uses_source_url_host_when_card_origin_is_missing() -> None:
    observed: dict[str, object] = {}
    card = _card(source_url="https://www.reuters.com/world/example", lead="", origin="", member_count=1)

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(sender, card)

    assert '<a href="https://www.reuters.com/world/example">路透社</a>' in str(observed["text"])


@pytest.mark.parametrize(
    ("origin", "source_url", "label"),
    [
        ("serenity", "https://x.com/serenity/status/1234567890123456789", "serenity 的推特"),
        ("jin10", "https://xnews.jin10.com/details/123", "金十"),
        ("Bloomberg", "https://www.bloomberg.com/news/articles/123", "彭博社"),
        ("wire", "https://jin10.com.evil.test/story", "jin10.com.evil.test"),
        ("jin10", "https://jin10.com.evil.test/story", "jin10.com.evil.test"),
        ("Bloomberg", "https://bloomberg.com.evil.test/story", "bloomberg.com.evil.test"),
        ("Reuters", "https://reuters.com.evil.test/story", "reuters.com.evil.test"),
        (
            "news-history.newsliquid.com",
            "https://news-history.newsliquid.com/b/nL1N44P00N",
            "路透社",
        ),
        ("Reuters", "https://news-history.newsliquid.com/b/nL1N44P00N", "路透社"),
        (
            "news-history.newsliquid.com",
            "https://news-history.newsliquid.com/b/opaque-provider-id",
            "原始媒体未识别（NewsLiquid 中转）",
        ),
        (
            "Reuters",
            "https://news-history.newsliquid.com.evil.test/b/nL1N44P00N",
            "news-history.newsliquid.com.evil.test",
        ),
    ],
)
def test_sender_normalizes_only_proven_source_domains(origin: str, source_url: str, label: str) -> None:
    observed: dict[str, object] = {}
    card = _card(source_url=source_url, origin=origin, member_count=1)

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(sender, card)

    assert f'<a href="{source_url}">{label}</a>' in str(observed["text"])


def test_day_change_is_never_reparsed_out_of_the_rendered_market_line() -> None:
    """#562: the 24 h number is the computed movement's or it is 暂无.

    The adapter used to regex `24h +7.91%` back out of the card line it had just stripped, which made a
    rendered string a fourth source for a number the quote read model already owns. A card that arrives
    without a movement for an asset now says so, exactly as it already did for 新闻后 and 1h.
    """

    observed: dict[str, object] = {}
    card = _card(
        quotes=[
            {
                "requested_symbol": "BTC",
                "price": "74553.10",
                "change_pct": 7.91,
                "change_basis": "rolling_24h",
                "state": "fresh",
                "instrument_class": "crypto",
            }
        ]
    )

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(sender, card, presentation=ReaderDeliveryPresentation())

    assert "24h +7.91%" in "\n".join(card.body_lines())
    text = str(observed["text"])
    assert "24h 暂无" in text
    assert "7.91%" not in text


def test_a_trade_link_cannot_leave_the_venue_its_own_template_names() -> None:
    """#562: one host allowlist -- the venue template here -- and every interpolated segment encoded.

    The adapter no longer parses the URL it just built to re-check the host, port, query and path that
    `reader_trade_targets` and this template already decided. What keeps a hostile contract identity
    inside `www.binance.com` is structural: the host and prefix are literals and the rest is quoted.
    """

    observed: dict[str, object] = {}
    card = _card(lead="业绩改善", novelty="new_fact", assets=("ETH",), origin="rtpr.io", member_count=1)

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(
        sender,
        card,
        presentation=ReaderDeliveryPresentation(
            trade_targets=(
                ReaderTradeTarget(
                    ticker="ETH",
                    venue="binance.spot",
                    venue_symbol="ETHUSDT?next=https://evil.example/x",
                    base_symbol="ETH",
                    quote_asset="USDT?next=https://evil.example/x",
                ),
            ),
        ),
    )

    href = next(url for url in re.findall(r'<a href="([^"]+)"', str(observed["text"])) if "binance" in url)
    assert href.startswith("https://www.binance.com/en/trade/ETH_")
    assert urlsplit(href).hostname == "www.binance.com"
    assert not urlsplit(href).query and not urlsplit(href).fragment
    assert "/x" not in href.removeprefix("https://www.binance.com/en/trade/")


def test_sender_never_turns_untrusted_ticker_destinations_into_links() -> None:
    observed: dict[str, object] = {}
    card = _card(
        source_url="",
        lead="",
        novelty="new_fact",
        assets=("BTC", "ETH", "SOL"),
        origin="Reuters",
        member_count=1,
    )

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(
        sender,
        card,
        presentation=ReaderDeliveryPresentation(
            trade_targets=(
                ReaderTradeTarget(
                    ticker="BTC",
                    venue="binance.perp",
                    venue_symbol="ETHUSDT",
                    base_symbol="ETH",
                    quote_asset="USDT",
                ),
                ReaderTradeTarget(
                    ticker="ETH",
                    venue="binance.perp",
                    venue_symbol="ETH/USDT",
                    base_symbol="ETH",
                    quote_asset="USDT",
                ),
                ReaderTradeTarget(
                    ticker="SOL",
                    venue="binance.spot",
                    venue_symbol="SOLUSDT",
                    base_symbol="SOL",
                    quote_asset="USDT",
                ),
            ),
        ),
    )

    assert ">BTC</a>" not in str(observed["text"])
    assert ">ETH</a>" not in str(observed["text"])
    assert '<a href="https://www.binance.com/en/trade/SOL_USDT">SOL</a>' in str(observed["text"])


def test_sender_escapes_untrusted_card_text_before_enabling_html() -> None:
    observed: dict[str, object] = {}
    card = _card(
        source_url="https://www.reuters.com/world/example",
        title="A < B & <i>not markup</i>",
        lead="利润 < 预期 & 风险上升",
        direction="bearish",
        novelty="",
        assets=(),
        origin="Reuters",
        member_count=1,
    )

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(sender, card)

    assert observed["parse_mode"] == "HTML"
    # The card model's own sanitizer already dropped the markdown-ish `>` from the untrusted title;
    # everything that survives it reaches this channel escaped, so `<i` is text and never a tag.
    assert _without_timing(observed["text"]) == (
        "🔴 <b>A &lt; B &amp; &lt;inot markup&lt;/i</b>\n\n"
        "利润 &lt; 预期 &amp; 风险上升\n\n"
        "🧭 <b>方向</b>  利空 · 影响明显\n\n"
        '🔗 <b>来源</b>  <a href="https://www.reuters.com/world/example">路透社</a>'
    )


def test_degraded_card_uses_asset_label_instead_of_claiming_a_model_judgment() -> None:
    observed: dict[str, object] = {}
    card = _card(
        source_url="",
        title="交易所恢复提现",
        assets=("BTC", "ETH"),
        origin="opennews",
        member_count=1,
        degraded=True,
    )

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(sender, card)

    assert _without_timing(observed["text"]) == (
        "⚪ <b>交易所恢复提现</b>\n\n"
        "🎯 <b>标的</b>  BTC\n"
        "新闻后 暂无\n"
        "1h 暂无，\n"
        "24h 暂无\n\n"
        "🎯 <b>标的</b>  ETH\n"
        "新闻后 暂无\n"
        "1h 暂无，\n"
        "24h 暂无\n\n"
        "🔗 <b>来源</b>  opennews"
    )


def test_http_client_info_logs_never_include_the_bot_token(caplog: pytest.LogCaptureFixture) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    caplog.set_level(logging.INFO, logger="httpx")
    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )

    sender.prepare()
    _send(sender, _card())

    assert BOT_TOKEN not in caplog.text


def test_production_transport_injects_the_bot_token_only_in_the_wire_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire_paths: list[str] = []
    connections: list[Any] = []

    class FakeResponse:
        status = 200
        reason = "OK"

        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        @staticmethod
        def getheaders() -> list[tuple[str, str]]:
            return [("content-type", "application/json")]

        def read(self, _limit: int) -> bytes:
            return json.dumps({"ok": True, "result": self.payload}).encode()

    class FakeHTTPSConnection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.response = FakeResponse({})
            self.connected = False
            connections.append(self)

        def connect(self) -> None:
            # #553 PR-2 made connecting its own step: a failure here provably wrote no request bytes,
            # which is what lets a caller retry it without risking a second notification.
            self.connected = True

        def request(self, _verb: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
            del body, headers
            assert self.connected, "the transport must connect before it writes request bytes"
            wire_paths.append(path)
            method = path.rsplit("/", maxsplit=1)[-1]
            payloads: dict[str, dict[str, object]] = {
                "getChat": {"id": CHANNEL_ID, "type": "channel"},
                "getMe": {"id": BOT_ID, "is_bot": True},
                "getChatMember": {
                    "status": "administrator",
                    "user": {"id": BOT_ID, "is_bot": True},
                    "can_post_messages": True,
                },
                "sendMessage": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}},
            }
            self.response = FakeResponse(payloads[method])

        def getresponse(self) -> FakeResponse:
            return self.response

        def close(self) -> None:
            return None

    monkeypatch.setattr("tracefold.integrations.telegram.HTTPSConnection", FakeHTTPSConnection)
    sender = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHANNEL_ID)

    sender.prepare()
    _send(sender, _card())

    assert wire_paths == [
        f"/bot{BOT_TOKEN}/getChat",
        f"/bot{BOT_TOKEN}/getMe",
        f"/bot{BOT_TOKEN}/getChatMember",
        f"/bot{BOT_TOKEN}/sendMessage",
    ]
    assert len(connections) == len(wire_paths)


def test_target_receipt_is_keyed_and_changes_when_the_bot_token_rotates() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    before_sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    before_sender.prepare()
    before = _send(before_sender, _card())
    after_sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN_ROTATED,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    after_sender.prepare()
    after = _send(after_sender, _card())

    unkeyed = hashlib.sha256(f"telegram-private-channel-v1:{CHANNEL_ID}".encode()).hexdigest()
    assert before["target_sha256"] != unkeyed
    assert before["target_sha256"] != after["target_sha256"]


def test_sender_rejects_a_response_from_any_other_chat() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(_request)
        if preflight is not None:
            return preflight
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": -1009999999999}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()

    with pytest.raises(TelegramDeliveryError, match="telegram_response_chat_mismatch"):
        _send(sender, _card())


@pytest.mark.parametrize("chat_id", [0, 123456789, -123456789, -100, -1000000000, "@four", "channel", "", True])
def test_sender_accepts_only_a_channel_telegram_can_be_asked_for(chat_id: object) -> None:
    """A Bot API channel id or a public `@name`, and nothing else: this is the one place that decides."""

    with pytest.raises(ValueError, match="news_push_telegram_chat_id_invalid"):
        TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=chat_id)  # type: ignore[arg-type]


def test_a_public_channel_is_addressed_by_its_name_and_bound_to_the_id_it_answers_with() -> None:
    """#562 §5 rows 1 and 11: a public channel is a product decision, so it is a usable target.

    Everything the preflight proves is unchanged -- the channel type, the bot's identity and its right
    to post -- and one thing is added: the `@name` in the answer has to be the `@name` that was asked
    for, because a name binds to whichever channel currently carries it. Every later response is then
    checked against the numeric id Telegram itself answered with.
    """

    observed: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        payload = json.loads(request.content)
        observed.append({"method": method, **payload})
        if method == "getChat":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"id": CHANNEL_ID, "type": "channel", "username": "tracefold_feed"}},
            )
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": BOT_ID, "is_bot": True}})
        if method == "getChatMember":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "status": "administrator",
                        "user": {"id": BOT_ID, "is_bot": True},
                        "can_post_messages": True,
                    },
                },
            )
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id="@tracefold_feed",
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    receipt = _send(
        sender,
        _card(),
        presentation=ReaderDeliveryPresentation(
            novelty="progression",
            progression_from_headline="ETF 前一日净流入",
            progression_review_state="confirmed",
            progression_review_parent_age_minutes=61,
            progression_review_parent_message_id=41,
        ),
    )

    assert [entry["method"] for entry in observed] == ["getChat", "getMe", "getChatMember", "sendMessage"]
    assert observed[0]["chat_id"] == "@tracefold_feed"
    assert observed[-1]["chat_id"] == "@tracefold_feed"
    # The parent link is the public channel's own permalink, not the private `t.me/c/<id>` form.
    assert '<a href="https://t.me/tracefold_feed/41">此前：ETF 前一日净流入</a>' in str(observed[-1]["text"])
    assert receipt["message_id"] == 42


def test_a_public_channel_target_reads_the_same_however_the_operator_spelled_it() -> None:
    """One case rule for the target: a Telegram username is case-insensitive, and so is this digest.

    The target string is what the receipt digest is built from and what the preflight compares the
    answer against. Two spellings meaning two digests would orphan every receipt already stored the
    moment an operator re-cased their configuration -- the message would still be there and the
    enrichment edit would refuse it as another sender's.
    """

    lower = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id="@tracefold_feed")
    upper = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id="  @Tracefold_Feed  ")

    assert lower._target_sha256 == upper._target_sha256
    assert lower._chat_id == upper._chat_id == "@tracefold_feed"


def test_a_public_channel_that_answers_with_another_name_is_not_this_target() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.rsplit("/", maxsplit=1)[-1] != "getChat":
            raise AssertionError("the preflight must stop at the mismatched channel")
        return httpx.Response(
            200,
            json={"ok": True, "result": {"id": CHANNEL_ID, "type": "channel", "username": "someone_else"}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id="@tracefold_feed",
        transport=httpx.MockTransport(handle),
    )
    with pytest.raises(TelegramDeliveryError) as raised:
        sender.prepare()

    assert raised.value.code == "news_delivery_telegram_target_chat_mismatch"


def test_a_plain_http_source_keeps_its_button() -> None:
    """#562 §5 row 11. Refusing `http://` dropped the source link off legitimate publishers' cards.

    A transport opinion about somebody else's site is the reader's own client's to have; what this
    adapter owes the reader is the link the card was built with.
    """

    observed: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID}}})

    sender = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHANNEL_ID, transport=httpx.MockTransport(handle))
    sender.prepare()
    _send(sender, _card(source_url="http://www.example.test/story"))

    assert '<a href="http://www.example.test/story">' in str(observed["text"])


def test_a_channel_with_a_public_username_is_a_valid_target() -> None:
    """#562 §5 row 11. A public channel is a product decision, not a delivery failure.

    The preflight still binds delivery to the exact chat id and to a channel rather than a group or a
    personal chat; what it stopped doing is refusing the operator's own choice to publish.
    """

    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        if method == "getChat":
            return httpx.Response(
                200,
                json={"ok": True, "result": {"id": CHANNEL_ID, "type": "channel", "username": "public_feed"}},
            )
        return _preflight_response(request) or httpx.Response(
            200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID}}}
        )

    sender = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHANNEL_ID, transport=httpx.MockTransport(handle))
    sender.prepare()
    receipt = _send(sender, _card())

    assert methods == ["getChat", "getMe", "getChatMember", "sendMessage"]
    assert receipt["message_id"] == 42


def test_an_over_long_card_is_clipped_rather_than_lost() -> None:
    """#562 §5 row 7. Telegram's 4096-character limit used to settle the whole delivery `terminal`.

    Everything the reader came for -- the title, the facts line, the assets and the source link -- fits;
    only the body ran long, and the card was thrown away whole rather than trimmed.
    """

    observed: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID}}})

    sender = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHANNEL_ID, transport=httpx.MockTransport(handle))
    sender.prepare()
    receipt = _send(sender, _wide_card(400))

    text = str(observed["text"])
    assert receipt["message_id"] == 42
    assert len(_plain_html_text(text)) <= _TELEGRAM_TEXT_MAX
    assert text.startswith("🟢 <b>BTC ETF 净流入</b>")
    assert "连续第三日净流入" in text
    assert text.endswith('🔗 <b>来源</b>  <a href="https://www.coindesk.com/news/1">CoinDesk</a> · 2 条报道')
    # Bottom-up: the first asset blocks survive and the last ones are the ones given up.
    assert "🎯 <b>标的</b>  AAA0000" in text
    assert "AAA0399" not in text


@pytest.mark.parametrize(
    "source_url",
    [
        # The rules that are about this adapter, not about somebody else's transport: credentials in
        # the URL, a non-default port, and a length no card needs (#562 §5 row 11). Accepting `http`
        # widened the scheme, not those rules -- a userinfo or port is a redirect trick either way.
        "https://user:secret@example.test/story",
        "http://user:secret@example.test/story",
        "http://example.test:8080/story",
        "https://example.test:8443/story",
        "ftp://example.test/story",
        "https://example.test/" + "a" * 2_100,
    ],
)
def test_sender_keeps_an_unsafe_source_destination_as_plain_text(source_url: str) -> None:
    observed: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    _send(sender, _card(source_url=source_url))

    assert "reply_markup" not in observed
    assert "<a href=" not in str(observed["text"])
    assert str(observed["text"]).endswith("🔗 <b>来源</b>  CoinDesk · 2 条报道")
    assert "⏱ <b>时间</b>" not in str(observed["text"])


@pytest.mark.parametrize(
    ("chat", "error"),
    [
        ({"id": CHANNEL_ID, "type": "supergroup"}, "target_not_private_channel"),
        ({"id": -1009999999999, "type": "channel"}, "target_chat_mismatch"),
    ],
)
def test_sender_rejects_any_target_that_is_not_the_exact_private_channel(chat: dict[str, object], error: str) -> None:
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", maxsplit=1)[-1])
        return httpx.Response(200, json={"ok": True, "result": chat})

    sender = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHANNEL_ID, transport=httpx.MockTransport(handle))
    assert methods == []

    with pytest.raises(TelegramDeliveryError, match=error):
        sender.prepare()

    assert methods == ["getChat"]
    assert "sendMessage" not in methods


def test_sender_requires_the_bot_to_have_channel_post_permission() -> None:
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        if method in {"getChat", "getMe"}:
            response = _preflight_response(request)
            assert response is not None
            return response
        assert method == "getChatMember"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "status": "administrator",
                    "user": {"id": BOT_ID, "is_bot": True},
                    "can_post_messages": False,
                },
            },
        )

    sender = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHANNEL_ID, transport=httpx.MockTransport(handle))
    assert methods == []

    with pytest.raises(TelegramDeliveryError, match="target_post_permission_missing"):
        sender.prepare()

    assert methods == ["getChat", "getMe", "getChatMember"]
    assert "sendMessage" not in methods


def test_sender_retries_preflight_on_a_later_event_after_transient_provider_failure() -> None:
    methods: list[str] = []
    get_chat_attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal get_chat_attempts
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        if method == "getChat":
            get_chat_attempts += 1
            if get_chat_attempts == 1:
                return httpx.Response(503, json={"ok": False})
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 43, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(TelegramDeliveryError, match="preflight_http_failed"):
        sender.prepare()
    sender.prepare()
    receipt = _send(sender, _card())

    assert receipt["message_id"] == 43
    assert methods == ["getChat", "getChat", "getMe", "getChatMember", "sendMessage"]


def test_sender_never_starts_send_message_after_the_shared_call_budget_is_exhausted() -> None:
    clock = [100.0]
    methods: list[str] = []

    def monotonic() -> float:
        return clock[0]

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        assert method == "getChat"
        clock[0] += 7.1
        return httpx.Response(200, json={"ok": True, "result": {"id": CHANNEL_ID, "type": "channel"}})

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
        monotonic=monotonic,
    )

    with pytest.raises(TelegramDeliveryError, match="preflight_budget_exhausted"):
        sender.prepare()

    assert methods == ["getChat"]


def test_sender_refuses_to_send_before_target_preflight() -> None:
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", maxsplit=1)[-1])
        return httpx.Response(500)

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(TelegramDeliveryError, match="target_not_prepared"):
        _send(sender, _card())

    assert methods == []


# The same card branches PR-A pinned for the Feishu serializer, read here for the other channel.
BRANCH_CARDS: dict[str, dict[str, Any]] = {
    entry["id"]: entry
    for entry in json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "news" / "reader_card_branch_cards.json").read_text(
            encoding="utf-8"
        )
    )["entries"]
}


def _fixture_market_card(fixture_id: str) -> ReaderCard:
    """One market card from the shared branch corpus, through the loop's own builder."""

    inputs = BRANCH_CARDS[fixture_id]["inputs"]
    return market_reader_card(
        track=MarketTrack(**inputs["track"]),
        reason=inputs["reason"],
        observations=[MarketObservation(**row) for row in inputs["observations"]],
        detail_url=inputs["detail_url"],
        action_changes=inputs["action_changes"],
    )


def _sent_text(card: ReaderCard, *, presentation: ReaderDeliveryPresentation | None = None) -> str:
    """One card through the real adapter and a fake Telegram endpoint; the text that left."""

    observed: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
        wall_clock_ms=lambda: 1_788_600_420_000,  # 17:27 on the reader's clock
    )
    sender.prepare()
    sender.send_card(card, channel_payload=feishu_card(card), presentation=presentation)
    return str(observed["text"])


def test_an_open_interest_card_keeps_its_family_mark_its_event_time_and_its_own_body() -> None:
    """#562 §1: the card that reached readers as a white circle, three 暂无 prices and the send time.

    Every part of this text is the market card's own: 🔵 is the OI family (Feishu's `blue`), the body
    is the model's OI lines, `事件时间 01:00` is the observation's stamp rather than the moment this
    process happened to send, and the console link is the card's own button rather than a source.
    """

    text = _sent_text(_fixture_market_card("market-oi-followup-billions"))

    assert text == (
        "🔵 <b>持仓异动 · 跟进 · BTC</b>\n\n"
        "sideways 0% · 01:00\n"
        "OI $12.40B · binance · oi_signal_v1|opennews_oi_source_v1|300000\n\n"
        "事件时间  01:00\n"
        "推送时间  17:27\n"
        "🔗 <b>来源</b>  opennews oi · 1 条报道\n"
        '🔗 <a href="https://console.example.com/news">打开明细</a>'
    )
    assert "标的" not in text
    assert "暂无" not in text


def test_a_liquidation_card_carries_the_familys_mark_and_the_reported_figure() -> None:
    text = _sent_text(_fixture_market_card("market-liquidation-three-reports"))

    assert text == (
        "🔴 <b>强平 · ETH</b>\n\n"
        "binance · 空单被强平 3 笔 · 16:40–16:51\n"
        "最大单笔来源报告金额 $1,000,000.00\n"
        "各来源报告金额不相加：没有可信底层成交标识时只列报告数与最大单笔。\n\n"
        "事件时间  16:51\n"
        "推送时间  17:27\n"
        "🔗 <b>来源</b>  opennews liquidation · 3 条报道\n"
        '🔗 <a href="https://console.example.com">打开明细</a>'
    )
    assert "暂无" not in text


def test_a_smart_money_card_keeps_its_turquoise_mark_and_its_action_timeline() -> None:
    text = _sent_text(_fixture_market_card("market-smart-money-action-change-six-reports"))

    assert text == (
        "💠 <b>聪明钱 · 平仓</b>\n\n"
        "js-2（来源标签，非已核实地址） · hyperliquid · 10:00–10:42\n"
        "动作变化 3 次 · 首 平空 → 末 开空\n"
        "开多 $160,180.00 · 开多 · 平多 $7,500.25 · 开空 $1,000,000.00\n"
        "Close 只表示来源报告的平仓/减仓动作，不代表账户已全部清仓。\n\n"
        "事件时间  10:42\n"
        "推送时间  17:27\n"
        "🔗 <b>来源</b>  opennews smart_money · 6 条报道"
    )
    assert "暂无" not in text


def test_a_smart_money_card_that_reported_no_close_carries_no_close_caveat() -> None:
    """The caveat is the card model's, so this channel drops it on the same cards Feishu does (#562).

    Telegram serializes `market_lines()` rather than a layout of its own, which is exactly what this
    states: an account that has only opened gets no sentence about what a Close would have meant.
    """

    text = _sent_text(_fixture_market_card("market-smart-money-unlabelled-account"))

    assert "开多 $500" in text
    assert "Close" not in text and "清仓" not in text


@pytest.mark.parametrize(
    ("detail_url", "linked"),
    [
        # The operator's own console origin, from the `api.public_url` they configured. A deployment
        # whose console answers on a port is ordinary, and the no-non-default-port rule exists for
        # links a *provider* supplied, so applying it here dropped the button off those deployments.
        ("https://console.example.test:8443/news/market/i1", True),
        ("http://10.4.0.9:8080/news/market/i1", True),
        ("https://console.example.test/news/market/i1", True),
        # The rules that hold for any link this adapter hands to Telegram still hold for this one.
        ("https://user:secret@console.example.test/news/market/i1", False),
        ("ftp://console.example.test/news/market/i1", False),
    ],
)
def test_a_market_cards_console_button_keeps_the_origin_the_operator_configured(detail_url: str, linked: bool) -> None:
    card = replace(
        _fixture_market_card("market-oi-followup-billions"),
        link=ReaderCardLink(url=detail_url, label="打开明细"),
    )

    text = _sent_text(card)

    assert (f'🔗 <a href="{detail_url}">打开明细</a>' in text) is linked
    assert "打开明细" in text or not linked


def test_a_market_card_shows_the_market_number_its_model_carries() -> None:
    """#562 PR-B lands the quote on market cards; this channel renders whatever the card carries.

    The quote is one of the family's own body lines rather than a block this channel places, so it
    sits where the OI family puts it -- under the measurement -- and a card whose quote is not fresh
    reads exactly as it did before the quote existed. There is no second copy of that placement here
    and none of the fresh-only rule either.
    """

    from tracefold.news.reader_card import ReaderCardQuote

    fixture = _fixture_market_card("market-oi-followup-billions")
    card = replace(
        fixture,
        quotes=(
            ReaderCardQuote(
                symbol="BTC", price="74553.10", change_pct=7.91, change_basis="rolling_24h", freshness="fresh"
            ),
            ReaderCardQuote(symbol="ETH", price="2300", freshness="stale"),
        ),
        market=replace(fixture.market, whale_long_profit_bps=8_840, whale_oi_ratio_bps=14_390),
    )

    text = _sent_text(card)

    assert text == (
        "🔵 <b>持仓异动 · 跟进 · BTC</b>\n\n"
        "sideways 0% · 01:00\n"
        "OI $12.40B · binance · oi_signal_v1|opennews_oi_source_v1|300000\n"
        "行情 BTC $74,553.10 24h +7.91%\n"
        "鲸鱼多头盈利 88.4% · 鲸鱼持仓/OI 143.9%\n\n"
        "事件时间  01:00\n"
        "推送时间  17:27\n"
        "🔗 <b>来源</b>  opennews oi · 1 条报道\n"
        '🔗 <a href="https://console.example.com/news">打开明细</a>'
    )
    # The stale second quote costs its own entry and nothing else, on this channel as on Feishu.
    assert "ETH" not in text


def test_the_enrichment_edit_replaces_the_message_from_the_updated_card() -> None:
    """The first send is the card as it stood; the edit is the same message, from the resolved card.

    Nothing here re-reads the text of the first message: the quotes, the trade target, the movement
    returns and the confirmed progression all arrive as an updated `ReaderCard` plus its presentation.
    """

    observed: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.append((method, json.loads(request.content)))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    clock = iter((1_787_898_733_000, 1_787_898_741_000))
    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
        wall_clock_ms=lambda: next(clock),
    )
    sender.prepare()
    receipt = _send(
        sender,
        _card(),
        presentation=ReaderDeliveryPresentation(
            news_at_ms=NEWS_AT_MS,
            market_data_state="pending",
            novelty="progression",
            progression_review_state="pending",
        ),
    )
    edited = _edit(
        sender,
        receipt,
        _card(
            quotes=[
                {
                    "requested_symbol": "BTC",
                    "price": "74553.10",
                    "change_pct": 7.91,
                    "change_basis": "rolling_24h",
                    "state": "fresh",
                    "instrument_class": "crypto",
                }
            ]
        ),
        presentation=ReaderDeliveryPresentation(
            trade_targets=(
                ReaderTradeTarget(
                    ticker="BTC",
                    venue="binance.perp",
                    venue_symbol="BTCUSDT",
                    base_symbol="BTC",
                    quote_asset="USDT",
                ),
            ),
            market_movements=(ReaderMarketMovement("BTC", 110, 80, 791, "available"),),
            news_at_ms=NEWS_AT_MS,
            novelty="progression",
            progression_from_headline="ETF 前一日净流入",
            progression_review_state="confirmed",
            progression_review_parent_age_minutes=61,
            progression_review_parent_message_id=41,
        ),
    )

    assert [method for method, _payload in observed] == ["sendMessage", "editMessageText"]
    assert observed[0][1]["text"] == (
        "🟢 <b>BTC ETF 净流入</b>\n\n"
        "🔄 <b>新进展</b>\n"
        "<blockquote>⏳ <b>关联确认中</b></blockquote>\n\n"
        "连续第三日净流入\n\n"
        "🎯 <b>标的</b>  BTC\n"
        "新闻后 计算中\n"
        "1h 计算中，\n"
        "24h 计算中\n\n"
        "🧭 <b>方向</b>  利多 · 影响明显\n\n"
        "新闻时间  14:32\n"
        "推送时间  14:32\n"
        '🔗 <b>来源</b>  <a href="https://www.coindesk.com/news/1">CoinDesk</a> · 2 条报道'
    )
    assert observed[1][1]["message_id"] == 42
    assert observed[1][1]["text"] == (
        "🟢 <b>BTC ETF 净流入</b>\n\n"
        "🔄 <b>新进展</b>\n"
        "<blockquote>✅ <b>已确认关联</b>\n"
        '↳ <a href="https://t.me/c/1234567890/41">此前：ETF 前一日净流入</a> · 1h 1mins 前</blockquote>\n\n'
        "连续第三日净流入\n\n"
        '🎯 <b>标的</b>  <a href="https://www.binance.com/en/futures/BTCUSDT">BTC</a>\n'
        "新闻后 +1.10%\n"
        "1h +0.80%，\n"
        "24h +7.91%\n\n"
        "🧭 <b>方向</b>  利多 · 影响明显\n\n"
        "新闻时间  14:32\n"
        "推送时间  14:32\n"
        '🔗 <b>来源</b>  <a href="https://www.coindesk.com/news/1">CoinDesk</a> · 2 条报道'
    )
    assert edited["pushed_at_ms"] == receipt["pushed_at_ms"]
    assert edited["edited_at_ms"] == 1_787_898_741_000


def test_a_card_the_catalogues_cannot_trade_says_so_under_its_title() -> None:
    """#562 §5 row 5: the edit that replaced deleting the card, in this channel's shape.

    The pipeline sets one field on the card; Feishu prints it as the first line of its markdown block
    and this channel prints it directly under the title. Neither adapter has a rule of its own about
    it, and the reader who already read this card is told rather than having it taken away.
    """

    text = _sent_text(replace(_card(), untradeable=True))

    assert text.startswith("🟢 <b>BTC ETF 净流入</b>\n\n<b>未找到可交易标的</b>\n\n")
    assert "连续第三日净流入" in text


def _wide_card(assets: int) -> ReaderCard:
    """A card whose asset list is what pushes its message towards the channel's own bound."""

    return replace(
        _card(),
        facts=replace(_card().facts, tickers=tuple(f"AAA{index:04d}" for index in range(assets))),
    )


def test_a_message_within_the_channel_bound_is_sent_whole() -> None:
    text = _sent_text(_wide_card(111))

    # The bound is on the text a reader receives, which is the message without its markup.
    assert 4_000 < len(_plain_html_text(text)) <= 4_096
    assert text.count("🎯 <b>标的</b>") == 111


def test_a_card_over_the_bound_gives_up_its_bottom_blocks_and_keeps_its_source() -> None:
    """Bottom-up: the judgment row sits lowest above the footer, then the last asset block."""

    whole = _sent_text(_wide_card(111))
    clipped = _sent_text(_wide_card(112))

    assert "🧭 <b>方向</b>  利多 · 影响明显" in whole
    assert len(_plain_html_text(clipped)) <= _TELEGRAM_TEXT_MAX
    assert "🧭 <b>方向</b>" not in clipped
    assert "AAA0111" not in clipped and "🎯 <b>标的</b>  AAA0110" in clipped
    assert clipped.startswith("🟢 <b>BTC ETF 净流入</b>")
    assert clipped.endswith('🔗 <b>来源</b>  <a href="https://www.coindesk.com/news/1">CoinDesk</a> · 2 条报道')


def test_clipping_gives_up_the_middle_before_the_title_or_the_footer() -> None:
    """#562 §5 row 7: the drop order is the card's priority, not the order of the list.

    The blocks between the title and the footer go from the bottom up -- the last asset block, then
    the ones above it, then the body, then the review band. The source line the card exists for
    outlives all of them, and only a card still over the bound with nothing but a title and a footer
    left gives up the footer too. Popping the tail instead would drop the reader's link while asset
    blocks above it survived.
    """

    block = "x" * 2_000

    # Bottom-up: one metadata block is enough to fit, and the one above it stays.
    assert _fit_telegram_message(["title", block, f"meta1 {block}", f"meta2 {block}", "footer"]) == (
        f"title\n\n{block}\n\nmeta1 {block}\n\nfooter"
    )
    # With nothing between them left to give, the body goes before the footer does.
    assert _fit_telegram_message(["title", block * 3, "footer"]) == "title\n\nfooter"
    # And the footer only when what is left is still over the bound -- a card always keeps one block.
    assert _fit_telegram_message([block * 3, "footer"]) == block * 3
