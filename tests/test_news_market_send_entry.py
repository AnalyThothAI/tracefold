"""The one initial-send entry ordinary News and market notifications share (#553 PR-2 §5.2).

Two claims are proved here and they are different. The first is *fairness*: both owners queue at one
place, so the operator's one pacing interval means one thing and a market burst cannot hold the entry
against a News card. The second is *what a failed send proved*: the adapters are driven at their real
boundary -- the whole `send_card` path including signing, size limits, status classification and
response parsing, with only the socket replaced -- because the difference between "provably not sent"
and "unknown" is decided inside that path and nowhere else.

Ordinary News is unchanged by both: it still reads `code` and only `code`.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from typing import Any

import httpx
import pytest

from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.integrations.feishu import (
    FeishuNewsPushSender,
    NewsPushExternalError,
    generate_feishu_signature,
)
from tracefold.integrations.telegram import TelegramDeliveryError, TelegramNewsPushSender
from tracefold.news.market_notifications import (
    COMMIT_PHASE_NOT_SENT,
    COMMIT_PHASE_UNKNOWN,
    SEND_ATTEMPTS_MAX,
    classify_send_failure,
)
from tracefold.news.pipeline.delivery import InitialSendEntry

FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/0123456789abcdef"
SIGNING_SECRET = "replay-signing-secret"
BOT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345"
CHAT_ID = -1001234567890
BOT_ID = 123456


class _SlowSender:
    """A sender whose one external call takes a measurable, fixed time."""

    def __init__(self, *, seconds: float) -> None:
        self.seconds = seconds
        self.calls: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0

    def prepare(self) -> None:
        return None

    def send_card(self, card: Any, *, presentation: Any = None) -> dict[str, Any]:
        del presentation
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            time.sleep(self.seconds)
            self.calls.append(str(card.get("owner")))
            return {"provider": "test", "owner": card.get("owner")}
        finally:
            self.concurrent -= 1

    def close(self) -> None:
        return None


async def _entry(sender: Any, *, min_interval_seconds: float = 0.0) -> tuple[InitialSendEntry, FiniteOperations]:
    finite = FiniteOperations()
    return (
        InitialSendEntry(sender=sender, finite_operations=finite, min_interval_seconds=min_interval_seconds),
        finite,
    )


def test_one_entry_serialises_both_owners_so_the_provider_sees_one_send_at_a_time() -> None:
    """The finite-operation pool has three slots; the entry is what stops two owners using two."""

    asyncio.run(_serialises())


async def _serialises() -> None:
    sender = _SlowSender(seconds=0.02)
    entry, finite = await _entry(sender)
    await asyncio.gather(
        *(entry.send_prepared_card({"owner": f"market-{index}"}) for index in range(4)),
        entry.send_prepared_card({"owner": "news"}),
    )
    finite.close()
    assert sender.max_concurrent == 1
    assert len(sender.calls) == 5


def test_the_operators_one_pacing_interval_paces_both_owners_together() -> None:
    asyncio.run(_paces_both())


async def _paces_both() -> None:
    sender = _SlowSender(seconds=0.0)
    entry, finite = await _entry(sender, min_interval_seconds=0.05)
    started = time.monotonic()
    for index in range(4):
        await entry.send_prepared_card({"owner": f"card-{index}"})
    elapsed = time.monotonic() - started
    finite.close()
    # Three gaps between four sends. Two independent pacers would have halved this.
    assert elapsed >= 3 * 0.05


def test_a_market_burst_never_starves_the_news_card_already_waiting() -> None:
    """`asyncio.Lock` admits in arrival order, which is the whole of "fair queueing"."""

    asyncio.run(_no_starvation())


async def _no_starvation() -> None:
    sender = _SlowSender(seconds=0.005)
    entry, finite = await _entry(sender)
    # One market card is in flight; a News card queues behind it, then twenty more market cards.
    first = asyncio.create_task(entry.send_prepared_card({"owner": "market-0"}))
    await asyncio.sleep(0)
    news = asyncio.create_task(entry.send_prepared_card({"owner": "news"}))
    await asyncio.sleep(0)
    burst = [asyncio.create_task(entry.send_prepared_card({"owner": f"market-{index}"})) for index in range(1, 21)]
    await asyncio.gather(first, news, *burst)
    finite.close()
    assert sender.calls.index("news") == 1


def test_mixed_stream_ordinary_news_latency_with_and_without_market_load(capsys) -> None:
    """The acceptance measurement, at the interval an operator actually configures.

    `news.push.min_interval_seconds` defaults to 0.6 s, and at that setting the interval -- not the
    provider -- is what a card waits for. Measuring at zero would have measured the stub instead.

    Three arms. `unloaded` is ordinary News alone. `replayed_rate` adds market traffic at *more* than
    the rate the offline replay measured -- that replay produced 246 cards in 72 h, about one every
    seventeen minutes, so a market card inside a five-second window is already an overstatement --
    and is the arm that describes production. `recovery_burst` is the honest worst case: after an
    outage every group's merged content becomes due at once, so a News card queues behind the whole
    drain, and its wait is the queue depth times the interval. That is a law rather than a constant,
    which is why it is asserted as one: it is what says a 60-card drain delays a News card by about
    36 s at this setting.
    """

    unloaded = asyncio.run(_measure(news_cards=4, market_cards=0))
    replayed_rate = asyncio.run(_measure(news_cards=4, market_cards=1))
    burst = asyncio.run(_measure(news_cards=1, market_cards=_BURST_CARDS, burst=True))
    interval_ms = _PRODUCTION_INTERVAL * 1000
    report = {
        "min_interval_seconds": _PRODUCTION_INTERVAL,
        "unloaded": {"p50_ms": _p(unloaded, 50), "p95_ms": _p(unloaded, 95), "cards": len(unloaded)},
        "replayed_rate": {
            "p50_ms": _p(replayed_rate, 50),
            "p95_ms": _p(replayed_rate, 95),
            "cards": len(replayed_rate),
        },
        "recovery_burst": {
            "cards_ahead": _BURST_CARDS,
            "measured_wait_ms": _p(burst, 50),
            "per_card_ms": round(_p(burst, 50) / _BURST_CARDS, 1),
            "extrapolated_60_card_drain_ms": round(60 * _p(burst, 50) / _BURST_CARDS),
        },
    }
    with capsys.disabled():
        print(f"\nmixed-stream ordinary News initial-send latency: {report}")

    # Every News card is still sent under every load: a market card never displaces one.
    assert len(unloaded) == len(replayed_rate) == 4
    # At the replayed rate the added wait is bounded by the one send it can queue behind.
    assert _p(replayed_rate, 95) <= _p(unloaded, 95) + 2 * interval_ms
    # And the burst arm is the interval times the depth, which is the law the 36 s figure comes from.
    queued = _p(burst, 50)
    assert (_BURST_CARDS - 1) * interval_ms <= queued <= (_BURST_CARDS + 2) * interval_ms


# The operator's configured default (`news.push.min_interval_seconds`). Measuring anywhere else would
# be measuring the stub's own latency rather than the pacing that actually decides a reader's wait.
_PRODUCTION_INTERVAL = 0.6
# Enough depth to measure the law without spending a minute of wall clock proving arithmetic.
_BURST_CARDS = 8
_SEND_SECONDS = 0.002


async def _measure(*, news_cards: int, market_cards: int, burst: bool = False) -> list[float]:
    """Ordinary-News initial-send latency under one load, through the real shared entry."""

    sender = _SlowSender(seconds=_SEND_SECONDS)
    entry, finite = await _entry(sender, min_interval_seconds=_PRODUCTION_INTERVAL)
    latencies: list[float] = []

    async def news_card() -> None:
        started = time.monotonic()
        await entry.send_prepared_card({"owner": "news"})
        latencies.append((time.monotonic() - started) * 1000)

    async def market_card(index: int) -> None:
        await asyncio.sleep(0.0 if burst else index * _PRODUCTION_INTERVAL)
        await entry.send_prepared_card({"owner": "market"})

    # The market cards are queued first, so the News card that follows them waits behind the depth
    # they created -- which is exactly the shape of a recovery drain.
    market = [asyncio.create_task(market_card(index)) for index in range(market_cards)]
    await asyncio.sleep(0)
    await asyncio.gather(*(news_card() for _ in range(news_cards)), *market)
    finite.close()
    return latencies


def _p(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, (percentile * len(ordered)) // 100)], 3)


# --- the adapter boundary: what one failed send proved --------------------------------------------


def _feishu(handler: Any) -> FeishuNewsPushSender:
    """The real adapter with a real signing secret: the whole `send_card` path bar the socket."""

    return FeishuNewsPushSender(
        webhook_url=FEISHU_URL, signing_secret=SIGNING_SECRET, transport=httpx.MockTransport(handler)
    )


def _telegram_preflight(request: httpx.Request) -> httpx.Response | None:
    """The adapter's real target preflight, answered as Telegram answers it."""

    method = request.url.path.rsplit("/", maxsplit=1)[-1]
    if method == "getChat":
        return httpx.Response(200, json={"ok": True, "result": {"id": CHAT_ID, "type": "channel"}})
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
    return None


def _telegram(on_send: Any) -> TelegramNewsPushSender:
    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _telegram_preflight(request)
        return preflight if preflight is not None else on_send(request)

    sender = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHAT_ID, transport=httpx.MockTransport(handle))
    sender.prepare()
    return sender


def test_a_feishu_success_signs_the_request_and_returns_a_receipt() -> None:
    """The signature is part of the seam: an unsigned body is a request Feishu rejects."""

    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    receipt = _feishu(handle).send_card({"config": {}, "elements": []})
    assert receipt["provider"] == "feishu"
    assert receipt["code"] == 0
    body = seen[0]
    assert body["msg_type"] == "interactive"
    assert body["sign"] == generate_feishu_signature(
        timestamp_seconds=int(body["timestamp"]), signing_secret=SIGNING_SECRET
    )


@pytest.mark.parametrize(
    ("handler", "code", "commit_phase", "retryable"),
    [
        pytest.param(
            lambda request: httpx.Response(200, json={"code": 19001, "msg": "param invalid"}),
            "news_delivery_feishu_business_rejected",
            COMMIT_PHASE_NOT_SENT,
            False,
            id="explicit_vendor_rejection_is_provably_not_sent_and_not_retried",
        ),
        pytest.param(
            lambda request: httpx.Response(200, json={"code": 11232, "msg": "rate limited"}),
            "news_delivery_feishu_business_rate_limited",
            COMMIT_PHASE_NOT_SENT,
            True,
            id="an_explicit_rate_limit_is_provably_not_sent_and_retryable",
        ),
        pytest.param(
            lambda request: httpx.Response(429),
            "news_delivery_feishu_http_failed",
            COMMIT_PHASE_NOT_SENT,
            True,
            id="http_429_is_the_same_refusal",
        ),
        pytest.param(
            lambda request: httpx.Response(503),
            "news_delivery_feishu_http_failed",
            COMMIT_PHASE_UNKNOWN,
            False,
            id="a_5xx_is_not_evidence_that_nothing_was_delivered",
        ),
        pytest.param(
            lambda request: httpx.Response(400),
            "news_delivery_feishu_http_rejected",
            COMMIT_PHASE_NOT_SENT,
            False,
            id="a_4xx_refusal_is_provably_not_sent",
        ),
        pytest.param(
            lambda request: httpx.Response(200, content=b"not json"),
            "news_delivery_feishu_response_invalid",
            COMMIT_PHASE_UNKNOWN,
            False,
            id="an_unreadable_answer_proves_nothing_either_way",
        ),
    ],
)
def test_feishu_states_what_each_failure_proved_without_changing_its_code(
    handler: Any, code: str, commit_phase: str, retryable: bool
) -> None:
    with pytest.raises(NewsPushExternalError) as raised:
        _feishu(handler).send_card({"config": {}, "elements": []})
    # The code ordinary News records is untouched; the evidence beside it is new.
    assert raised.value.code == code
    assert raised.value.commit_phase == commit_phase
    assert raised.value.retryable is retryable


def test_a_feishu_connect_failure_is_provably_not_sent_and_a_read_timeout_is_not() -> None:
    """The distinction the single collapsed transport code could not express."""

    def refuse_connection(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    def time_out_reading(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)

    with pytest.raises(NewsPushExternalError) as never_left:
        _feishu(refuse_connection).send_card({"config": {}, "elements": []})
    with pytest.raises(NewsPushExternalError) as no_answer:
        _feishu(time_out_reading).send_card({"config": {}, "elements": []})

    # One code, as before, and two different truths about the message.
    assert never_left.value.code == no_answer.value.code == "news_delivery_feishu_transport_failed"
    assert never_left.value.commit_phase == COMMIT_PHASE_NOT_SENT
    assert never_left.value.retryable is True
    assert no_answer.value.commit_phase == COMMIT_PHASE_UNKNOWN
    assert no_answer.value.retryable is False


def test_telegram_carries_the_same_commit_semantics() -> None:
    """Not a market-specific sender: the same three answers, from the other adapter's own path."""

    card = {"header": {"title": {"content": "x"}}, "elements": []}

    def business_rejection(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    def read_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)

    def connect_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(TelegramDeliveryError) as rejected:
        _telegram(business_rejection).send_card(card)
    assert rejected.value.code == "news_delivery_telegram_business_rejected"
    assert rejected.value.commit_phase == COMMIT_PHASE_NOT_SENT
    assert rejected.value.retryable is False

    with pytest.raises(TelegramDeliveryError) as unknown:
        _telegram(read_timeout).send_card(card)
    assert unknown.value.commit_phase == COMMIT_PHASE_UNKNOWN

    with pytest.raises(TelegramDeliveryError) as never_left:
        _telegram(connect_failure).send_card(card)
    assert never_left.value.commit_phase == COMMIT_PHASE_NOT_SENT
    assert never_left.value.retryable is True


@pytest.mark.parametrize(
    ("status", "body", "code", "commit_phase", "retryable"),
    [
        pytest.param(
            429,
            {"ok": False},
            "news_delivery_telegram_http_failed",
            COMMIT_PHASE_NOT_SENT,
            True,
            id="telegram_429_is_a_refusal",
        ),
        pytest.param(
            503,
            {"ok": False},
            "news_delivery_telegram_http_failed",
            COMMIT_PHASE_UNKNOWN,
            False,
            id="telegram_5xx_proves_nothing",
        ),
        pytest.param(
            403,
            {"ok": False},
            "news_delivery_telegram_http_rejected",
            COMMIT_PHASE_NOT_SENT,
            False,
            id="telegram_4xx_is_provably_not_sent",
        ),
        pytest.param(
            200,
            None,
            "news_delivery_telegram_response_invalid",
            COMMIT_PHASE_UNKNOWN,
            False,
            id="telegram_unparsable_body_proves_nothing",
        ),
    ],
)
def test_telegram_status_handling_matches_feishus_case_for_case(
    status: int, body: dict[str, Any] | None, code: str, commit_phase: str, retryable: bool
) -> None:
    """Same three answers from the same statuses: neither adapter is the market's special case."""

    def handle(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status, content=b"not json")
        return httpx.Response(status, json=body)

    with pytest.raises(TelegramDeliveryError) as raised:
        _telegram(handle).send_card({"header": {"title": {"content": "x"}}, "elements": []})
    assert raised.value.code == code
    assert raised.value.commit_phase == commit_phase
    assert raised.value.retryable is retryable


def test_the_market_classifier_reads_the_adapters_own_evidence_end_to_end() -> None:
    """The whole seam in one assertion: adapter -> exception -> delivery state."""

    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 11232, "msg": "rate limited"})

    def unreadable(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)

    try:
        _feishu(rate_limited).send_card({"config": {}, "elements": []})
    except NewsPushExternalError as exc:
        first = classify_send_failure(exc, attempts=1)
        spent = classify_send_failure(exc, attempts=SEND_ATTEMPTS_MAX)
    assert first.state == "pending"
    assert first.retry_in_ms == 5_000
    assert spent.state == "failed"

    try:
        _feishu(unreadable).send_card({"config": {}, "elements": []})
    except NewsPushExternalError as exc:
        unknown = classify_send_failure(exc, attempts=1)
    assert unknown.state == "unknown"
    assert unknown.retry_in_ms is None


def test_an_entry_with_no_sender_reports_it_rather_than_pretending_to_send() -> None:
    asyncio.run(_no_sender())


async def _no_sender() -> None:
    entry, finite = await _entry(None)
    assert entry.available is False
    with pytest.raises(RuntimeError, match="news_delivery_sender_unavailable"):
        await entry.send_prepared_card({"owner": "market"})
    finite.close()


def test_latency_percentile_helper_is_ordinary_arithmetic() -> None:
    """Guards the measurement, so a green mixed-stream run cannot be a broken percentile."""

    values = [float(value) for value in range(1, 101)]
    assert _p(values, 50) == statistics.quantiles(values, n=100)[49] + 0.5
    assert _p(values, 95) == 96.0
