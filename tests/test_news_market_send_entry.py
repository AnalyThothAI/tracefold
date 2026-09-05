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
from collections.abc import Mapping
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

    The sample is four News cards per arm, so the statistics reported are a median and a **maximum**,
    named as such: a p95 over four samples is the maximum wearing a percentile's name, and this table
    is quoted in the PR body.

    Three arms. `unloaded` is ordinary News alone. `replayed_rate` adds market traffic at *more* than
    the rate the offline replay measured -- 246 cards in 72 h is about one every seventeen minutes, so
    a market card inside a five-second window is already an overstatement -- and is the arm that
    describes production. `recovery_burst` is the honest worst case: after an outage every group's
    merged content becomes due at once, so a News card queues behind the whole drain and its wait is
    the queue depth times the interval. That is a law rather than a constant, which is why it is
    asserted as one: it is what says a 60-card drain delays a News card by about 36 s here.
    """

    unloaded = asyncio.run(_measure(news_cards=_NEWS_CARDS, market_cards=0))
    replayed_rate = asyncio.run(_measure(news_cards=_NEWS_CARDS, market_cards=1))
    burst = asyncio.run(_measure(news_cards=_NEWS_CARDS, market_cards=_BURST_CARDS, burst=True))
    interval_ms = _PRODUCTION_INTERVAL * 1000
    # The first News card admitted is the one that queued behind exactly the burst: the ones after it
    # queue behind it too, which is arithmetic rather than a second measurement.
    first_after_burst = min(burst)
    report = {
        "min_interval_seconds": _PRODUCTION_INTERVAL,
        "samples_per_arm": _NEWS_CARDS,
        "unloaded": {"median_ms": _median(unloaded), "max_ms": _max(unloaded), "cards": len(unloaded)},
        "replayed_rate": {
            "median_ms": _median(replayed_rate),
            "max_ms": _max(replayed_rate),
            "cards": len(replayed_rate),
        },
        "recovery_burst": {
            "cards_ahead": _BURST_CARDS,
            "first_card_wait_ms": round(first_after_burst, 3),
            "per_card_ahead_ms": round(first_after_burst / _BURST_CARDS, 1),
            "extrapolated_60_card_drain_ms": round(60 * first_after_burst / _BURST_CARDS),
            "cards": len(burst),
        },
    }
    with capsys.disabled():
        print(f"\nmixed-stream ordinary News initial-send latency: {report}")

    # No News card is lost under any load, including the drain: a market card never displaces one.
    assert len(unloaded) == len(replayed_rate) == len(burst) == _NEWS_CARDS
    # At the replayed rate the added wait is bounded by the one send it can queue behind.
    assert _max(replayed_rate) <= _max(unloaded) + 2 * interval_ms
    # And the burst arm is the interval times the depth, which is the law the 36 s figure comes from.
    assert (_BURST_CARDS - 1) * interval_ms <= first_after_burst <= (_BURST_CARDS + 2) * interval_ms


# The operator's configured default (`news.push.min_interval_seconds`). Measuring anywhere else would
# be measuring the stub's own latency rather than the pacing that actually decides a reader's wait.
_PRODUCTION_INTERVAL = 0.6
# Four is what a 0.6 s interval affords in a hermetic lane; the statistics are named for that.
_NEWS_CARDS = 4
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

    # The market cards are queued first, so the News cards that follow them wait behind the depth
    # they created -- which is exactly the shape of a recovery drain.
    market = [asyncio.create_task(market_card(index)) for index in range(market_cards)]
    await asyncio.sleep(0)
    await asyncio.gather(*(news_card() for _ in range(news_cards)), *market)
    finite.close()
    return latencies


def _median(values: list[float]) -> float:
    return 0.0 if not values else round(sorted(values)[len(values) // 2], 3)


def _max(values: list[float]) -> float:
    return 0.0 if not values else round(max(values), 3)


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


def _telegram(on_send: Any, *, prepared: bool = True) -> TelegramNewsPushSender:
    """The real adapter. `prepared=False` leaves the target check to whoever sends."""

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _telegram_preflight(request)
        return preflight if preflight is not None else on_send(request)

    sender = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHAT_ID, transport=httpx.MockTransport(handle))
    if prepared:
        sender.prepare()
    return sender


def test_the_entry_prepares_an_unprepared_target_before_it_sends() -> None:
    """A market card has no earlier preflight of its own, so the entry has to be the one (#553 §5.2).

    Telegram refuses `send_card` outright until its channel, its bot identity and its posting right
    have been checked. Without this the first market card on a perfectly good channel would fail
    `news_delivery_telegram_target_not_prepared` -- and a test that prepared the sender by hand would
    never have noticed.
    """

    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", maxsplit=1)[-1])
        preflight = _telegram_preflight(request)
        if preflight is not None:
            return preflight
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 7, "chat": {"id": CHAT_ID, "type": "channel"}}}
        )

    sender = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHAT_ID, transport=httpx.MockTransport(handle))

    async def send() -> Mapping[str, Any]:
        entry, finite = await _entry(sender)
        try:
            return await entry.send_prepared_card({"header": {"title": {"content": "市场"}}, "elements": []})
        finally:
            finite.close()

    receipt = asyncio.run(send())
    assert receipt["provider"] == "telegram"
    # The preflight ran first, then the send: the card was never offered to an unvalidated target.
    assert methods == ["getChat", "getMe", "getChatMember", "sendMessage"]


class _RefusingPrepare:
    """A sender whose target check fails, which is a provider call like any other."""

    def __init__(self) -> None:
        self.prepared_at: list[float] = []

    def prepare(self) -> None:
        self.prepared_at.append(time.monotonic())
        raise RuntimeError("news_delivery_telegram_preflight_transport_failed")

    def send_card(self, card: Any, *, presentation: Any = None) -> dict[str, Any]:
        raise AssertionError("send must not be reached when the target check failed")

    def close(self) -> None:
        return None


def test_a_failing_target_check_is_still_paced() -> None:
    """The interval covers the whole held block, not only a successful send.

    A turn drains up to `SENDS_PER_TURN_MAX` cards. Against a broken target that is twenty preflights,
    and if the stamp were only written after a successful send every one of them would compute
    `wait <= 0` and go straight out -- the loop would hammer the provider precisely when it is least
    able to answer.
    """

    sender = _RefusingPrepare()
    interval = 0.05

    async def two_attempts() -> None:
        entry, finite = await _entry(sender, min_interval_seconds=interval)
        try:
            for _ in range(2):
                with pytest.raises(RuntimeError, match="preflight_transport_failed"):
                    await entry.send_prepared_card({"owner": "market"})
        finally:
            finite.close()

    asyncio.run(two_attempts())
    assert len(sender.prepared_at) == 2
    assert sender.prepared_at[1] - sender.prepared_at[0] >= interval


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


def test_the_reported_statistics_are_ordinary_arithmetic() -> None:
    """Guards the measurement, so a green mixed-stream run cannot be a broken summary."""

    values = [float(value) for value in range(1, 101)]
    assert _median(values) == statistics.median_high(values)
    assert _max(values) == 100.0
    assert (_median([]), _max([])) == (0.0, 0.0)
