"""Telegram News delivery: one configured channel and one outbound attempt."""

from __future__ import annotations

import hashlib
import json
import logging

import httpx
import pytest

from tracefold.integrations.telegram import TelegramDeliveryError, TelegramNewsPushSender
from tracefold.news import ReaderDeliveryPresentation, ReaderMarketMovement, ReaderTradeTarget

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


def _card(*, source_url: str = "https://www.coindesk.com/news/1") -> dict[str, object]:
    return {
        "header": {"title": {"content": "BTC ETF 净流入"}, "template": "green"},
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    "连续第三日净流入\n"
                    "利多 · 新进展 · 影响明显 · BTC · CoinDesk（2 条报道） · 14:32\n"
                    "行情 BTC $74,553.10 24h +7.91%"
                ),
            },
            {
                "tag": "action",
                "actions": [{"tag": "button", "text": {"content": "打开来源"}, "url": source_url}],
            },
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "Tracefold · abc12345"}]},
        ],
    }


def _without_timing(value: object) -> str:
    """Keep layout assertions deterministic; timing has its own fixed-clock contract tests below."""

    return "\n".join(
        line
        for line in str(value).splitlines()
        if not line.startswith("新闻时间  ") and not line.startswith("推送时间  ")
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
    receipt = sender.send_card(_card())

    assert observed["chat_id"] == CHANNEL_ID
    assert _without_timing(observed["text"]) == (
        "🟢 <b>BTC ETF 净流入</b>\n\n"
        "🔄 <b>新进展</b>\n\n"
        "连续第三日净流入\n\n"
        "🎯 <b>标的</b>  BTC\n"
        "新闻后 暂无\n"
        "1h 暂无，\n"
        "24h +7.91%\n\n"
        "🧭 <b>方向</b>  明显利多\n\n"
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
    card = _card(source_url="https://x.com/jukan05/status/1234567890123456789")
    card["header"] = {
        "title": {"content": "美光台湾工厂初步投票支持罢工比例达 80%，工会要求改为利润分红制"},
        "template": "red",
    }
    card["elements"][0]["content"] = (
        "美光约 60% 全球产能集中在台湾，是 HBM 先进制程的主力基地，工会参照三星 10.5%、"
        "SK 海力士 10% 的利润分红水平施压，9 月中旬前进入强制调解，若调解破裂将进入罢工投票，"
        "压低美光产能利用率与现金流。\n"
        "利空 · 新进展 · 影响明显 · MU · jukan05 · 10:48\n"
        "行情 MU $127.00 24h -5.11%"
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
    sender.send_card(
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
        '↳ <a href="https://t.me/c/1234567890/41">此前：美光工会此前启动劳资协商</a> · 1h 1mins 前\n'
        "现进展：同一工会行动进入罢工投票阶段，新增了明确比例和下一步程序。</blockquote>\n\n"
        "美光约 60% 全球产能集中在台湾，是 HBM 先进制程的主力基地，工会参照三星 10.5%、"
        "SK 海力士 10% 的利润分红水平施压，9 月中旬前进入强制调解，若调解破裂将进入罢工投票，"
        "压低美光产能利用率与现金流。\n\n"
        f"🎯 <b>标的</b>  {ticker}\n"
        "新闻后 0.00%\n"
        "1h -0.25%，\n"
        "24h -5.11%\n\n"
        "🧭 <b>方向</b>  明显利空\n"
        "\n"
        "新闻时间  10:48:21\n"
        "推送时间  10:48:33\n"
        '🔗 <b>来源</b>  <a href="https://x.com/jukan05/status/1234567890123456789">jukan05 的推特</a>'
    )


def test_sender_puts_new_fact_below_title_and_explains_macro_events_without_a_ticker() -> None:
    observed: dict[str, object] = {}
    card = {
        "header": {"title": {"content": "⚡ 美国 2026 年初步基准非农就业下修 7.9 万人"}, "template": "red"},
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    "官方就业基线整体下移，利率市场将重新定价劳动力转弱路径。\n利空 · 影响重大 · jin10 · 22:02"
                ),
            }
        ],
    }

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
    sender.send_card(
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
        "🧭 <b>方向</b>  重大利空\n\n"
        "新闻时间  22:02:42\n"
        "推送时间  22:03:01\n"
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
    initial = sender.send_card(
        _card(),
        presentation=ReaderDeliveryPresentation(
            news_at_ms=1_787_885_301_000,
            market_data_state="pending",
            novelty="progression",
            progression_review_state="pending",
        ),
    )
    updated = sender.edit_card(
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
    assert "🆕 <b>新事实</b>\n<blockquote>↩️ <b>未确认关联:</b> 候选报道的主体和事件链不同。</blockquote>" in str(
        observed[1][1]["text"]
    )
    assert "🔄 <b>新进展</b>" not in str(observed[1][1]["text"])
    assert "接续「" not in str(observed[1][1]["text"])
    assert "推送时间  10:48:33" in str(observed[1][1]["text"])
    assert initial["pushed_at_ms"] == 1_787_885_313_000
    assert updated["pushed_at_ms"] == initial["pushed_at_ms"]
    assert updated["edited_at_ms"] == 1_787_885_315_000


def test_sender_keeps_an_unavailable_progression_reason_to_one_nested_line() -> None:
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
    sender.send_card(
        _card(),
        presentation=ReaderDeliveryPresentation(
            novelty="progression",
            progression_review_state="unavailable",
            progression_review_reason="上游复核服务暂时不可用，\n请稍后确认。",
        ),
    )

    assert (
        "🆕 <b>新事实</b>\n<blockquote>⚠️ <b>关联待确认:</b> 上游复核服务暂时不可用， 请稍后确认。</blockquote>"
    ) in str(observed["text"])
    assert "🔄 <b>新进展</b>" not in str(observed["text"])


def test_sender_deletes_only_the_exact_receipted_message() -> None:
    observed: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        payload = json.loads(request.content)
        observed.append((method, payload))
        result: object = (
            True
            if method == "deleteMessage"
            else {
                "message_id": 42,
                "chat": {"id": CHANNEL_ID, "type": "channel"},
            }
        )
        return httpx.Response(200, json={"ok": True, "result": result})

    clock = iter((1_787_885_313_000, 1_787_885_315_000))
    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
        wall_clock_ms=lambda: next(clock),
    )
    sender.prepare()
    initial = sender.send_card(_card())
    deleted = sender.delete_card(initial)

    assert [method for method, _payload in observed] == ["sendMessage", "deleteMessage"]
    assert observed[1][1] == {"chat_id": CHANNEL_ID, "message_id": 42}
    assert deleted["message_id"] == 42
    assert deleted["deleted_at_ms"] == 1_787_885_315_000


def test_sender_links_a_fresh_bitget_target_even_when_prices_are_unavailable() -> None:
    observed: dict[str, object] = {}
    card = _card()
    card["elements"][0]["content"] = "业绩改善\n利多 · 影响明显 · 2605 · rtpr.io · 14:32"

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
    sender.send_card(
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
    initial = sender.send_card(_card(), presentation=ReaderDeliveryPresentation(market_data_state="pending"))
    with pytest.raises(TelegramDeliveryError, match="news_delivery_telegram_edit_http_rejected"):
        sender.edit_card(initial, _card())

    following = sender.send_card(_card(), presentation=ReaderDeliveryPresentation(market_data_state="pending"))

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
    initial = sender.send_card(_card(), presentation=ReaderDeliveryPresentation(market_data_state="pending"))

    with pytest.raises(TelegramDeliveryError, match="news_delivery_telegram_edit_receipt_invalid"):
        sender.edit_card({**initial, "provider_response": "untrusted"}, _card())

    assert methods == ["sendMessage"]


def test_sender_renders_exact_binance_tickers_as_html_links() -> None:
    observed: dict[str, object] = {}
    card = _card()
    card["elements"][0]["content"] = (
        "连续第三日净流入\n"
        "利多 · 新进展 · 影响明显 · BTC-USDT BTC · CoinDesk（2 条报道） · 14:32\n"
        "行情 BTC $74,553.10 24h +7.91%"
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
    sender.send_card(
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
        "24h +7.91%\n\n"
        "🧭 <b>方向</b>  明显利多\n\n"
        '🔗 <b>来源</b>  <a href="https://www.coindesk.com/news/1">CoinDesk</a> · 2 条报道'
    )


def test_sender_renders_each_asset_in_its_own_complete_market_block() -> None:
    observed: dict[str, object] = {}
    card = _card(source_url="https://x.com/serenity/status/1234567890123456789")
    card["header"]["template"] = "red"
    card["elements"][0]["content"] = (
        "资金从 BTC 轮动至 ETH\n"
        "利空 · 影响重大 · BTC ETH · serenity · 14:32\n"
        "行情 BTC $74,553.10 24h +3.20% · ETH $2,300.00 24h +1.70%"
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
    sender.send_card(
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
        "🧭 <b>方向</b>  重大利空\n\n"
        '🔗 <b>来源</b>  <a href="https://x.com/serenity/status/1234567890123456789">serenity 的推特</a>'
    )
    assert "reply_markup" not in observed


def test_sender_shows_second_level_news_and_push_times() -> None:
    observed: dict[str, object] = {}
    card = _card(source_url="https://www.bloomberg.com/news/articles/2026-08-28/bitcoin")
    card["elements"][0]["content"] = (
        "现货 ETF 资金继续流入\n利多 · 影响明显 · BTC · Bloomberg · 14:32\n行情 BTC $74,553.10 24h +7.91%"
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
    sender.send_card(
        card,
        presentation=ReaderDeliveryPresentation(
            news_at_ms=1_787_898_725_000,
            observed_at_ms=1_787_898_725_000,
        ),
    )

    assert str(observed["text"]).endswith(
        "新闻时间  14:32:05\n"
        "推送时间  14:32:13\n"
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
    sender.send_card(_card(), presentation=ReaderDeliveryPresentation())

    assert str(observed["text"]).endswith(
        "新闻时间  暂无\n"
        "推送时间  14:32:13\n"
        '🔗 <b>来源</b>  <a href="https://www.coindesk.com/news/1">CoinDesk</a> · 2 条报道'
    )


def test_sender_uses_source_url_host_when_card_origin_is_missing() -> None:
    observed: dict[str, object] = {}
    card = _card(source_url="https://www.reuters.com/world/example")
    card["elements"][0]["content"] = "利多 · 影响明显 · BTC · - · 14:32"

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
    sender.send_card(card)

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
    card = _card(source_url=source_url)
    card["elements"][0]["content"] = f"利多 · 影响明显 · BTC · {origin} · 14:32"

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
    sender.send_card(card)

    assert f'<a href="{source_url}">{label}</a>' in str(observed["text"])


def test_sender_never_turns_untrusted_ticker_destinations_into_links() -> None:
    observed: dict[str, object] = {}
    card = _card()
    card["elements"] = [
        {
            "tag": "markdown",
            "content": (
                "利多 · 影响明显 · BTC ETH SOL · Reuters · 14:32\n"
                "行情 BTC $74,553.10 24h +7.91% · ETH $2,300.00 24h +1.00% · SOL $200 24h +1.00%"
            ),
        }
    ]

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
    sender.send_card(
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
    card = _card()
    card["header"] = {"title": {"content": "A < B & <i>not markup</i>"}, "template": "red"}
    card["elements"] = [
        {
            "tag": "markdown",
            "content": "利润 < 预期 & 风险上升\n利空 · 影响明显 · A&B · Reuters · 14:32",
        }
    ]

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
    sender.send_card(card)

    assert observed["parse_mode"] == "HTML"
    assert _without_timing(observed["text"]) == (
        "🔴 <b>A &lt; B &amp; &lt;i&gt;not markup&lt;/i&gt;</b>\n\n"
        "利润 &lt; 预期 &amp; 风险上升\n\n"
        "🎯 <b>标的</b>  A&amp;B\n"
        "新闻后 暂无\n"
        "1h 暂无，\n"
        "24h 暂无\n\n"
        "🧭 <b>方向</b>  明显利空\n\n"
        "🔗 <b>来源</b>  路透社"
    )


def test_degraded_card_uses_asset_label_instead_of_claiming_a_model_judgment() -> None:
    observed: dict[str, object] = {}
    card = _card()
    card["header"] = {"title": {"content": "交易所恢复提现"}, "template": "grey"}
    card["elements"] = [
        {"tag": "markdown", "content": "BTC ETH · opennews · 14:32"},
    ]

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
    sender.send_card(card)

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
    sender.send_card(_card())

    assert BOT_TOKEN not in caplog.text


def test_production_transport_injects_the_bot_token_only_in_the_wire_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire_paths: list[str] = []

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

        def request(self, _verb: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
            del body, headers
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
    sender.send_card(_card())

    assert wire_paths == [
        f"/bot{BOT_TOKEN}/getChat",
        f"/bot{BOT_TOKEN}/getMe",
        f"/bot{BOT_TOKEN}/getChatMember",
        f"/bot{BOT_TOKEN}/sendMessage",
    ]


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
    before = before_sender.send_card(_card())
    after_sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN_ROTATED,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    after_sender.prepare()
    after = after_sender.send_card(_card())

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
        sender.send_card(_card())


@pytest.mark.parametrize("chat_id", [0, 123456789, -123456789, -100, -1000000000, "@public_channel"])
def test_sender_accepts_only_a_private_channel_bot_api_id(chat_id: object) -> None:
    with pytest.raises(ValueError, match="news_push_telegram_chat_id_invalid"):
        TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=chat_id)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "source_url",
    [
        "http://example.test/not-allowed",
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
    sender.send_card(_card(source_url=source_url))

    assert "reply_markup" not in observed
    assert "<a href=" not in str(observed["text"])
    assert str(observed["text"]).endswith("🔗 <b>来源</b>  CoinDesk · 2 条报道")
    assert "⏱ <b>时间</b>" not in str(observed["text"])


@pytest.mark.parametrize(
    ("chat", "error"),
    [
        ({"id": CHANNEL_ID, "type": "supergroup"}, "target_not_private_channel"),
        ({"id": CHANNEL_ID, "type": "channel", "username": "public_feed"}, "target_not_private_channel"),
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
    receipt = sender.send_card(_card())

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
        sender.send_card(_card())

    assert methods == []
