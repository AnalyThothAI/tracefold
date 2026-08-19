"""News V3 consumer unit tests: fake bus + fake repositories, no PostgreSQL and no broker."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.news import consumers as consumers_module
from tracefold.news.bus import (
    RK_RAW_LIVE,
    RK_VERDICT_PUSH,
    BusMessage,
    DeferError,
    PermanentError,
)
from tracefold.news.consumers import (
    DeduperConsumer,
    DelivererConsumer,
    JanitorLoop,
    TriageConsumer,
)
from tracefold.news.models import TRIAGE_POLICY_VERSION, TRIAGE_PROMPT_VERSION
from tracefold.platform.resource import ResourceAdmissionTimeout

NOW_MS = 1_800_000_000_000
WATCHLIST = frozenset({"BTC", "NVDA"})


class FakeBus:
    def __init__(self) -> None:
        self.published: list[BusMessage] = []
        self.consumed: list[str] = []

    async def publish(self, message: BusMessage) -> None:
        self.published.append(message)

    async def consume(self, queue: str, handler: Any, *, prefetch: int, stop_event: Any) -> None:
        self.consumed.append(queue)

    def routing_keys(self) -> list[str]:
        return [message.routing_key for message in self.published]


class RecordingNews:
    """Minimal NewsRepository double: records every call and answers from a scripted table."""

    def __init__(self, **responses: Any) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.control_state: dict[str, Any] = {"paused": False, "mutes": []}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, {**{f"arg{i}": a for i, a in enumerate(args)}, **kwargs}))
            if name == "read_control":
                return dict(self.control_state)
            if name == "write_control":
                self.control_state = {"paused": bool(kwargs["paused"]), "mutes": list(kwargs["mutes"])}
                return None
            if name == "told_ledger" and name not in self.responses:
                return []  # nothing pushed yet: an empty told ledger
            value = self.responses.get(name)
            return value(*args, **kwargs) if callable(value) else value

        return _call

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def kwargs_of(self, name: str) -> dict[str, Any]:
        return next(kwargs for called, kwargs in self.calls if called == name)


class FakeWorkerDatabase:
    """Only the News lane exists on the fake; a consumer that reaches for run_business is a wiring bug."""

    def __init__(self, news: RecordingNews, *, admission_timeout_for: set[str] | None = None) -> None:
        self.news = news
        self.operations: list[str] = []
        self.admission_timeout_for = admission_timeout_for or set()

    @contextmanager
    def worker_session(self, name: str, *_args: Any, **_kwargs: Any):
        del name
        yield SimpleNamespace(news=self.news, transaction=nullcontext)

    async def run_news(self, name: str, fn: Any, *args: Any, operation_timeout_seconds: float, **kwargs: Any):
        del operation_timeout_seconds
        self.operations.append(name)
        if name in self.admission_timeout_for:
            raise ResourceAdmissionTimeout(f"worker_database_admission_timeout:{name}")
        return fn(*args, **kwargs)


class InlineFinite:
    async def run(self, _name: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("timeout_seconds", None)
        kwargs.pop("allow_shutdown", None)
        return await fn(*args, **kwargs)


def _card(**overrides: Any) -> dict[str, Any]:
    card = {
        "event_id": "ev-strong",
        "family": "general",
        "leader_title": "NVIDIA to invest $100bn in OpenAI data centre",
        "leader_url": "https://example.test/nvda",
        "leader_description": "",
        "reporting_origin": "FT",
        "admission": "candidate",
        "priority": "high",
        "provider_score_max": 92.0,
        "asset_class": "equity_or_commodity",
        "grounded_assets": ["NVDA"],
        "watchlist_hits": ["NVDA"],
        "macro_lexicon": False,
        "storyline_key": "asset:NVDA",
        "comparison_fingerprint": "f" * 64,
        "trace_id": "trace-1",
    }
    card.update(overrides)
    return card


def _message(kind: str, payload: dict[str, Any], *, routing_key: str = "", priority: int = 0) -> BusMessage:
    return BusMessage(
        kind=kind,  # type: ignore[arg-type]
        message_id=f"{kind}:{payload.get('event_id', 'x')}",
        routing_key=routing_key,
        payload=payload,
        trace_id="trace-1",
        occurred_at_ms=NOW_MS,
        priority=priority,
    )


# ---------------------------------------------------------------- Deduper
def test_deduper_publishes_new_candidate_events_once(monkeypatch: pytest.MonkeyPatch) -> None:
    admissions = iter(
        [
            SimpleNamespace(
                event_created=True,
                admission="candidate",
                event_id="ev-1",
                family="macro",
                gate=SimpleNamespace(priority="high", amqp_priority=5),
            ),
            SimpleNamespace(event_created=False, admission="candidate", event_id="ev-1", family="macro", gate=None),
            SimpleNamespace(
                event_created=True,
                admission="suppressed_ungrounded",
                event_id="ev-2",
                family="general",
                gate=SimpleNamespace(priority="normal", amqp_priority=0),
            ),
            # `listing_deterministic` is an admitted admission, not a suppression: exchange listing/delisting
            # frames must reach Triage like any candidate (#72 — they used to die silently right here).
            SimpleNamespace(
                event_created=True,
                admission="listing_deterministic",
                event_id="ev-3",
                family="listing",
                gate=SimpleNamespace(priority="high", amqp_priority=5),
            ),
        ]
    )
    seen: list[dict[str, Any]] = []

    def fake_admit(repos: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return next(admissions)

    monkeypatch.setattr(consumers_module, "admit_item", fake_admit)
    news = RecordingNews()
    bus = FakeBus()
    deduper = DeduperConsumer(
        bus=bus, db=FakeWorkerDatabase(news), strategy_ids=("1018",), watchlist_symbols=frozenset({"BTC"})
    )
    params = {
        "id": 3_568_501,
        "engineType": "news",
        "text": "U.S. 30-Year Treasury Yield Climbs to 5.32%, Highest Since 2007",
        "ts": NOW_MS,
        "strategy": {"id": 1018, "name": "News Score > 70"},
        "aiRating": {"score": 88},
    }
    raw = _message(
        "raw",
        {"params": params, "strategy_id": "1018", "ingest_mode": "live", "observed_at_ms": NOW_MS - 5},
        routing_key=RK_RAW_LIVE.format(strategy_id="1018"),
    )

    async def scenario() -> None:
        await deduper.handle(raw)
        await deduper.handle(raw)  # redelivery: admit_item reports nothing new
        await deduper.handle(raw)  # a suppressed admission never reaches Triage
        await deduper.handle(raw)  # a listing admission does
        foreign = _message(
            "raw",
            {"params": {**params, "strategy": {"id": 4242, "name": "other"}}, "strategy_id": "4242"},
            routing_key=RK_RAW_LIVE.format(strategy_id="4242"),
        )
        await deduper.handle(foreign)
        with pytest.raises(PermanentError, match="news_raw_params_missing"):
            await deduper.handle(_message("raw", {}))

    asyncio.run(scenario())

    assert len(seen) == 4
    assert seen[0]["ingest_mode"] == "live" and seen[0]["observed_at_ms"] == NOW_MS - 5
    assert seen[0]["trace_id"] == "trace-1" and seen[0]["watchlist_symbols"] == frozenset({"BTC"})
    assert seen[0]["event"].provider_record_id == "3568501"
    assert bus.routing_keys() == ["event.macro.high", "event.listing.high"]
    assert bus.published[0].payload == {"event_id": "ev-1"}
    assert bus.published[0].priority == 5 and bus.published[0].message_id == "event:ev-1"
    assert bus.published[1].payload == {"event_id": "ev-3"}
    assert news.names() == ["mark_event_published", "mark_event_published"]
    assert news.kwargs_of("mark_event_published")["event_id"] == "ev-1"


def test_deduper_admission_timeout_defers_uncounted_and_publishes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(consumers_module, "admit_item", lambda *_a, **_k: pytest.fail("db never admitted"))
    news = RecordingNews()
    bus = FakeBus()
    deduper = DeduperConsumer(
        bus=bus,
        db=FakeWorkerDatabase(news, admission_timeout_for={"news_deduper_admit"}),
        strategy_ids=("1018",),
        watchlist_symbols=frozenset(),
    )
    raw = _message(
        "raw",
        {
            "params": {"id": 1, "engineType": "news", "text": "x", "ts": NOW_MS, "strategy": {"id": 1018, "name": "n"}},
            "strategy_id": "1018",
        },
    )
    with pytest.raises(DeferError, match="db_admission_timeout:news_deduper_admit"):
        asyncio.run(deduper.handle(raw))
    assert bus.published == [] and news.calls == []


# ---------------------------------------------------------------- Triage
def _triage(news: RecordingNews, bus: FakeBus, *, hourly_cap: int = 20) -> TriageConsumer:
    return TriageConsumer(
        bus=bus,
        db=FakeWorkerDatabase(news),
        model=None,
        watchlist_symbols=WATCHLIST,
        watchlist=sorted(WATCHLIST),
        hourly_cap=hourly_cap,
        concurrency=1,
        circuit_failures=3,
        circuit_open_seconds=60.0,
    )


def test_triage_without_model_escalates_watchlist_or_high_score_and_persists_degraded_verdict() -> None:
    status_row = {"pushed_2h": 0, "pushed_4h": 0, "max_magnitude_2h": 0, "max_magnitude_4h": 0}
    news = RecordingNews(
        get_verdict=None, event_card=_card(), event_status=status_row, sent_count_since=0, insert_verdict=True
    )
    bus = FakeBus()
    triage = _triage(news, bus)

    asyncio.run(
        triage.handle(_message("event", {"event_id": "ev-strong"}, routing_key="event.general.high", priority=5))
    )

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["stage"] == "triage" and inserted["policy_version"] == TRIAGE_POLICY_VERSION
    assert inserted["degraded"] is True and inserted["error_code"] == "news_triage_model_unconfigured"
    assert inserted["model"] is None and inserted["model_decision"] is None
    assert inserted["prompt_version"] == TRIAGE_PROMPT_VERSION
    assert inserted["rule_baseline_decision"] == "push" and inserted["final_decision"] == "escalate"
    assert inserted["verdict"]["headline_zh"] == "NVIDIA to invest $100bn in OpenAI data centre"  # wire headline
    trace = inserted["trace"]
    assert trace["attempt"] == 1 and trace["queue_lag_ms"] >= 0
    assert len(trace["prompt_sha256"]) == 64 and trace["status"] == status_row  # replayable snapshot
    assert "latency_ms" not in trace and "input_sha256" not in trace  # no model call happened
    assert "NVIDIA to invest $100bn" in news.kwargs_of("set_context_line")["context_line"]
    # escalate is a high-importance push (⚡ + priority); there is no second lane to notify
    assert [(m.routing_key, m.payload) for m in bus.published] == [
        (RK_VERDICT_PUSH, {"event_id": "ev-strong", "kind": "first"}),
    ]
    assert all(m.priority == 5 and m.trace_id == "trace-1" for m in bus.published)
    assert news.names()[-1] == "mark_verdict_published"


@pytest.mark.parametrize(
    "card",
    [
        _card(
            event_id="ev-weak", priority="normal", provider_score_max=75.0, grounded_assets=["AMD"], watchlist_hits=[]
        ),
        _card(event_id="ev-macro", priority="high", provider_score_max=85.0, grounded_assets=[], watchlist_hits=[]),
    ],
)
def test_triage_without_model_drops_when_rule_baseline_drops(card: dict[str, Any]) -> None:
    news = RecordingNews(get_verdict=None, event_card=card, event_status={}, sent_count_since=0, insert_verdict=True)
    bus = FakeBus()

    asyncio.run(_triage(news, bus).handle(_message("event", {"event_id": card["event_id"]})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["degraded"] is True
    assert inserted["rule_baseline_decision"] == "drop" and inserted["final_decision"] == "drop"
    assert bus.published == []
    assert "mark_verdict_published" not in news.names()


def test_triage_without_model_pushes_a_grounded_score_80_event() -> None:
    """Degraded mode is not silent: a provider score >= 80 on a grounded asset still pushes (rule baseline)."""

    card = _card(
        event_id="ev-strong-80", priority="normal", provider_score_max=85.0, grounded_assets=["AMD"], watchlist_hits=[]
    )
    news = RecordingNews(get_verdict=None, event_card=card, event_status={}, sent_count_since=0, insert_verdict=True)
    bus = FakeBus()

    asyncio.run(_triage(news, bus).handle(_message("event", {"event_id": card["event_id"]})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["degraded"] is True and inserted["rule_baseline_decision"] == "push"
    assert inserted["final_decision"] == "push"
    assert bus.routing_keys() == [RK_VERDICT_PUSH]


def test_triage_without_model_never_pushes_while_muted_or_paused() -> None:
    news = RecordingNews(get_verdict=None, event_card=_card(), event_status={}, sent_count_since=0, insert_verdict=True)
    news.control_state = {"paused": False, "mutes": [{"kind": "symbol", "key": "NVDA", "until_ms": NOW_MS * 2}]}
    bus = FakeBus()

    asyncio.run(_triage(news, bus).handle(_message("event", {"event_id": "ev-strong"})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "drop" and inserted["override_rule"] == "muted"
    assert inserted["rule_baseline_decision"] == "push"
    assert bus.published == []


class _FailingTriageModel:
    """TriageModel double: raises the scripted TriageModelError per call, or returns a fixed verdict."""

    model_name = "fake"

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)

    async def triage(self, _human: str) -> Any:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _triage_with_model(news: RecordingNews, bus: FakeBus, model: Any) -> TriageConsumer:
    triage = _triage(news, bus)
    triage.model = model
    return triage


def test_triage_output_failure_is_traced_and_never_opens_the_circuit() -> None:
    """max_tokens truncation / schema mismatch: degraded verdict with finish_reason + parsing_error in the trace, the
    transport circuit stays closed (no incident), and the next Event still reaches the model."""

    from tracefold.news.agents.triage_model import TriageModelError

    card = _card()
    news = RecordingNews(get_verdict=None, event_card=card, event_status={}, sent_count_since=0, insert_verdict=True)
    bus = FakeBus()
    truncated = [
        TriageModelError(
            "news_triage_output_truncated",
            output_failure=True,
            finish_reason="length",
            output_tokens=300,
            detail="ValidationError: 8 validation errors",
        )
        for _ in range(4)
    ]
    triage = _triage_with_model(news, bus, _FailingTriageModel(truncated))

    for index in range(4):
        asyncio.run(triage.handle(_message("event", {"event_id": f"ev-trunc-{index}"})))

    inserted = [kwargs for name, kwargs in news.calls if name == "insert_verdict"]
    assert len(inserted) == 4
    assert all(row["degraded"] is True and row["error_code"] == "news_triage_output_truncated" for row in inserted)
    assert inserted[0]["trace"]["finish_reason"] == "length" and inserted[0]["trace"]["output_tokens"] == 300
    assert inserted[0]["trace"]["parsing_error"] == "ValidationError: 8 validation errors"
    assert "open_incident" not in news.names()
    assert not triage.circuit.is_open(NOW_MS * 2)
    assert triage.circuit.failures == 0


def test_triage_consumer_start_closes_incidents_left_open_by_a_previous_process() -> None:
    news = RecordingNews(close_open_incidents=1)
    bus = FakeBus()
    triage = _triage(news, bus)
    stop = asyncio.Event()
    stop.set()

    asyncio.run(triage.run(stop_event=stop))

    assert news.kwargs_of("close_open_incidents")["cause_classes"] == ["triage_circuit_open"]
    assert bus.consumed == ["news.triage"]


def test_triage_transport_failures_open_the_circuit_and_a_success_closes_the_incident() -> None:
    from tracefold.news.agents.triage_model import TriageCallResult, TriageModelError
    from tracefold.news.models import TriageVerdict

    card = _card()
    news = RecordingNews(
        get_verdict=None,
        event_card=card,
        event_status={},
        sent_count_since=0,
        insert_verdict=True,
        open_incident=1,
        close_open_incidents=1,
    )
    bus = FakeBus()
    ok = TriageCallResult(
        verdict=TriageVerdict(
            novelty="new_fact",
            event_type="partnership",
            assets=[],
            direction="bullish",
            scope="single_name",
            magnitude=1,
            actionable=True,
            confidence=0.6,
            decision="push",
            headline_zh="ok",
        ),
        latency_ms=10,
        input_tokens=1,
        output_tokens=1,
        cached_tokens=None,
        model="fake",
    )
    outcomes: list[Any] = [TriageModelError("news_triage_timeout", retryable=True) for _ in range(3)] + [ok]
    triage = _triage_with_model(news, bus, _FailingTriageModel(outcomes))
    triage.circuit.open_seconds = 0.0  # let the fourth call reach the model in the same test clock

    for index in range(4):
        asyncio.run(triage.handle(_message("event", {"event_id": f"ev-net-{index}"})))

    assert news.names().count("open_incident") == 1
    assert news.kwargs_of("open_incident")["cause_class"] == "triage_circuit_open"
    assert news.kwargs_of("close_open_incidents")["cause_classes"] == ["triage_circuit_open"]
    inserted = [kwargs for name, kwargs in news.calls if name == "insert_verdict"]
    assert [row["degraded"] for row in inserted] == [True, True, True, False]


def test_triage_records_the_answering_model_and_the_fallback_reason() -> None:
    from tracefold.news.agents.triage_model import TriageCallResult
    from tracefold.news.models import TriageVerdict

    news = RecordingNews(get_verdict=None, event_card=_card(), event_status={}, sent_count_since=0, insert_verdict=True)
    bus = FakeBus()
    answered_by_fallback = TriageCallResult(
        verdict=TriageVerdict(
            novelty="new_fact",
            event_type="partnership",
            assets=[],
            direction="bullish",
            scope="single_name",
            magnitude=1,
            actionable=True,
            confidence=0.6,
            decision="push",
            headline_zh="ok",
        ),
        latency_ms=10,
        input_tokens=1,
        output_tokens=1,
        cached_tokens=None,
        model="deepseek-chat",
        attempts=2,
        fallback_from="news_triage_timeout",
    )
    triage = _triage_with_model(news, bus, _FailingTriageModel([answered_by_fallback]))

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-fallback"})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["model"] == "deepseek-chat" and inserted["degraded"] is False
    assert inserted["trace"]["model_fallback_from"] == "news_triage_timeout"
    assert inserted["trace"]["model_attempts"] == 2


def test_triage_replays_an_existing_unpublished_decision_without_reinserting() -> None:
    news = RecordingNews(
        get_verdict={"final_decision": "push", "published_at_ms": None},
        event_card=lambda *_a, **_k: pytest.fail("existing verdict must short-circuit"),
    )
    bus = FakeBus()

    asyncio.run(_triage(news, bus).handle(_message("event", {"event_id": "ev-strong"})))
    assert bus.routing_keys() == [RK_VERDICT_PUSH]
    assert news.names() == ["get_verdict", "mark_verdict_published"]

    settled = RecordingNews(get_verdict={"final_decision": "drop", "published_at_ms": None})
    quiet = FakeBus()
    asyncio.run(_triage(settled, quiet).handle(_message("event", {"event_id": "ev-strong"})))
    assert quiet.published == [] and settled.names() == ["get_verdict"]


def test_triage_rejects_missing_event_id_and_missing_event() -> None:
    news = RecordingNews(get_verdict=None, event_card=None)
    triage = _triage(news, FakeBus())
    with pytest.raises(PermanentError, match="news_event_id_missing"):
        asyncio.run(triage.handle(_message("event", {})))
    with pytest.raises(PermanentError, match="news_event_missing"):
        asyncio.run(triage.handle(_message("event", {"event_id": "ghost"})))


# ---------------------------------------------------------------- Deliverer
def _deliverer(news: RecordingNews, bus: FakeBus, *, hourly_cap: int = 20) -> DelivererConsumer:
    return DelivererConsumer(
        bus=bus,
        db=FakeWorkerDatabase(news),
        sender=None,
        finite_operations=InlineFinite(),
        min_interval_seconds=0.0,
        hourly_cap=hourly_cap,
    )


def _delivery_news(**overrides: Any) -> RecordingNews:
    states = iter(overrides.pop("begin_states", ["new"]))
    responses: dict[str, Any] = {
        "event_card": _card(),
        "latest_verdict": lambda *, event_id, stage: (
            {
                "final_decision": "push",
                "verdict": {"direction": "bullish", "magnitude": 2, "headline_zh": "英伟达", "title_zh": "英伟达投资"},
            }
            if stage == "triage"
            else None
        ),
        "sent_count_since": 0,
        "begin_delivery": lambda **_k: next(states),
        "settle_delivery": True,
    }
    responses.update(overrides)
    return RecordingNews(**responses)


def test_deliverer_without_sender_settles_terminal_delivery_unavailable() -> None:
    news = _delivery_news()
    bus = FakeBus()

    asyncio.run(_deliverer(news, bus).handle(_message("verdict", {"event_id": "ev-strong", "kind": "first"})))

    begin = news.kwargs_of("begin_delivery")
    assert begin["event_id"] == "ev-strong" and begin["kind"] == "first" and begin["card"] == {}
    settle = news.kwargs_of("settle_delivery")
    assert settle["state"] == "terminal" and settle["error_code"] == "delivery_unavailable"
    assert settle["receipt"] is None
    assert bus.published == []
    assert "get_presentation" not in news.names()


def test_deliverer_paused_lane_drops_instead_of_holding_the_message() -> None:
    news = _delivery_news()
    news.control_state = {"paused": True, "mutes": []}

    asyncio.run(_deliverer(news, FakeBus()).handle(_message("verdict", {"event_id": "ev-strong", "kind": "first"})))

    settle = news.kwargs_of("settle_delivery")
    assert settle["state"] == "terminal" and settle["error_code"] == "delivery_paused"


def test_deliverer_without_sender_leaves_existing_delivery_untouched() -> None:
    news = _delivery_news(begin_states=["terminal"])

    asyncio.run(_deliverer(news, FakeBus()).handle(_message("verdict", {"event_id": "ev-strong", "kind": "first"})))

    assert "begin_delivery" in news.names() and "settle_delivery" not in news.names()


def test_deliverer_hourly_cap_settles_first_card_terminal_before_any_send() -> None:
    news = _delivery_news(sent_count_since=20)

    asyncio.run(_deliverer(news, FakeBus(), hourly_cap=20).handle(_message("verdict", {"event_id": "ev-strong"})))

    settle = news.kwargs_of("settle_delivery")
    assert settle["state"] == "terminal" and settle["error_code"] == "hourly_cap_reached"


def test_deliverer_skips_dropped_first_cards() -> None:
    news = _delivery_news(latest_verdict=lambda *, event_id, stage: {"final_decision": "drop", "verdict": {}})
    asyncio.run(_deliverer(news, FakeBus()).handle(_message("verdict", {"event_id": "ev-strong"})))
    assert "begin_delivery" not in news.names()

    with pytest.raises(PermanentError, match="news_event_id_missing"):
        asyncio.run(_deliverer(_delivery_news(), FakeBus()).handle(_message("verdict", {})))
    with pytest.raises(PermanentError, match="news_delivery_inputs_missing"):
        asyncio.run(
            _deliverer(_delivery_news(event_card=None), FakeBus()).handle(_message("verdict", {"event_id": "ghost"}))
        )


# ---------------------------------------------------------------- Janitor
def test_janitor_republishes_candidates_that_never_left_the_process() -> None:
    news = RecordingNews(
        unpublished_candidates=[{"event_id": "ev-lost"}, {"event_id": "ev-gone"}],
        event_card=lambda event_id: (
            _card(event_id="ev-lost", family="general", priority="normal") if event_id == "ev-lost" else None
        ),
    )
    bus = FakeBus()

    republished = asyncio.run(JanitorLoop(db=FakeWorkerDatabase(news), bus=bus).republish_unpublished())

    assert republished == 1
    assert bus.routing_keys() == ["event.general.normal"]
    assert bus.published[0].payload == {"event_id": "ev-lost"} and bus.published[0].trace_id == "trace-1"
    assert news.kwargs_of("mark_event_published")["event_id"] == "ev-lost"


class _ScriptedTriageModel:
    """TriageModel double that records every human input and answers with scripted verdicts."""

    model_name = "fake"

    def __init__(self, verdicts: list[Any]) -> None:
        self.verdicts = list(verdicts)
        self.inputs: list[str] = []

    async def triage(self, human: str) -> Any:
        from tracefold.news.agents.triage_model import TriageCallResult

        self.inputs.append(human)
        verdict = self.verdicts.pop(0)
        return TriageCallResult(
            verdict=verdict, latency_ms=10, input_tokens=1, output_tokens=1, cached_tokens=None, model="fake"
        )


def _model_verdict(**overrides: Any) -> Any:
    from tracefold.news.models import TriageVerdict

    base: dict[str, Any] = {
        "novelty": "new_fact",
        "restates": -1,
        "event_type": "partnership",
        "assets": [{"symbol": "NVDA", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 2,
        "actionable": True,
        "confidence": 0.8,
        "decision": "push",
        "headline_zh": "英伟达投资 OpenAI",
    }
    base.update(overrides)
    return TriageVerdict(**base)


def _ledger_row(
    event_id: str, at_ms: int, *, key: str = "asset:NVDA", headline: str = "英伟达投资 OpenAI"
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "at_ms": at_ms,
        "storyline_key": key,
        "magnitude": 2,
        "direction": "bullish",
        "headline_zh": headline,
    }


def test_triage_told_ledger_reaches_the_model_and_the_trace_and_grounds_a_restatement() -> None:
    """The told ledger (what the reader already received) is in the status bar and the trace; a restatement the
    model grounds in it drops with the restated card's event id recorded; the persist step locks the final key."""

    ledger = [_ledger_row("ev-earlier", NOW_MS - 300_000), _ledger_row("ev-other", NOW_MS - 900_000, key="asset:BTC")]
    news = RecordingNews(
        get_verdict=None,
        event_card=_card(),
        event_status={"pushed_2h": 1, "pushed_4h": 1, "max_magnitude_2h": 2, "max_magnitude_4h": 2},
        sent_count_since=0,
        insert_verdict=True,
        told_ledger=ledger,
    )
    bus = FakeBus()
    model = _ScriptedTriageModel([_model_verdict(novelty="restatement", restates=0, decision="drop")])
    triage = _triage_with_model(news, bus, model)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    assert len(model.inputs) == 1
    human = model.inputs[0]
    assert '"told": [{"ago_min": ' in human and "英伟达投资 OpenAI" in human and "ev-earlier" not in human
    assert human.count('"headline_zh": "英伟达投资 OpenAI"') == 2
    assert human.index("<event_status>") < human.index('"told"')
    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "drop" and inserted["override_rule"] == "restatement"
    assert inserted["verdict"]["novelty"] == "restatement" and inserted["verdict"]["restates"] == 0
    trace = inserted["trace"]
    assert trace["storyline_key_preliminary"] == "asset:NVDA" and trace["storyline_key"] == "asset:NVDA"
    assert trace["told_count"] == 2 and [t["event_id"] for t in trace["told"]] == ["ev-earlier", "ev-other"]
    assert trace["restates_event_id"] == "ev-earlier"
    assert "status_final" in trace and "input_sha256" in trace and "reasked_after_told_change" not in trace
    assert news.kwargs_of("lock_storyline")["arg0"] == "asset:NVDA"
    # decide -> insert happen after the lock, inside the same persist call.
    names = news.names()
    assert names.index("lock_storyline") < names.index("insert_verdict")
    assert bus.published == []


def test_triage_reasks_once_when_a_card_landed_while_the_model_was_thinking() -> None:
    """A push committed between the ledger snapshot and the persist step means the model judged novelty against a
    stale ledger: the consumer asks once more with the fresh ledger and persists that verdict."""

    fresh_push = _ledger_row("ev-just-pushed", NOW_MS - 1_000)
    ledger_calls = {"n": 0}

    def told_ledger(
        *, now_ms: int, window_ms: int, limit: int, prefer_key: str | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        del now_ms, window_ms, prefer_key
        ledger_calls["n"] += 1
        # First read (load): nothing yet. Every later read (in-lock check, reload) sees the new card.
        if ledger_calls["n"] == 1:
            return []
        return [fresh_push][:limit]

    news = RecordingNews(
        get_verdict=None,
        event_card=_card(),
        event_status={},
        sent_count_since=0,
        insert_verdict=True,
        told_ledger=told_ledger,
    )
    bus = FakeBus()
    model = _ScriptedTriageModel(
        [
            _model_verdict(novelty="new_fact"),  # judged against the empty ledger
            _model_verdict(novelty="restatement", restates=0, decision="drop"),  # sees ev-just-pushed
        ]
    )
    triage = _triage_with_model(news, bus, model)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    assert len(model.inputs) == 2 and '"told": []' in model.inputs[0] and "ev-just-pushed" not in model.inputs[0]
    assert '"headline_zh": "英伟达投资 OpenAI"' in model.inputs[1]
    inserted = [kwargs for name, kwargs in news.calls if name == "insert_verdict"]
    assert len(inserted) == 1  # the stale round wrote nothing
    row = inserted[0]
    assert row["final_decision"] == "drop" and row["override_rule"] == "restatement"
    trace = row["trace"]
    assert trace["reasked_after_told_change"] is True
    assert trace["first_verdict"]["novelty"] == "new_fact" and trace["first_verdict"]["decision"] == "push"
    assert trace["told_count"] == 1 and trace["restates_event_id"] == "ev-just-pushed"
    assert news.names().count("lock_storyline") == 2
    # The re-ask reloads everything the model and decide() look at (card, window facts, control, hourly count).
    assert news.names().count("event_card") == 2 and news.names().count("read_control") == 2
    assert bus.published == []


def test_triage_reask_failure_keeps_the_first_verdict_instead_of_the_rule_baseline() -> None:
    """If the re-ask itself fails, the model's first (valid) judgment is persisted, not a degraded fallback."""

    from tracefold.news.agents.triage_model import TriageModelError

    fresh_push = _ledger_row("ev-just-pushed", NOW_MS - 1_000)
    ledger_calls = {"n": 0}

    def told_ledger(**kwargs: Any) -> list[dict[str, Any]]:
        ledger_calls["n"] += 1
        return [] if ledger_calls["n"] == 1 else [fresh_push][: int(kwargs.get("limit") or 1)]

    news = RecordingNews(
        get_verdict=None,
        event_card=_card(priority="normal", provider_score_max=70.0),
        event_status={},
        sent_count_since=0,
        insert_verdict=True,
        told_ledger=told_ledger,
    )
    bus = FakeBus()

    class _FirstOkThenTimeout(_ScriptedTriageModel):
        async def triage(self, human: str) -> Any:
            if self.inputs:
                self.inputs.append(human)
                raise TriageModelError("news_triage_timeout", retryable=True)
            return await super().triage(human)

    model = _FirstOkThenTimeout([_model_verdict(novelty="new_fact", magnitude=3, direction="bearish", scope="macro")])
    triage = _triage_with_model(news, bus, model)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    assert len(model.inputs) == 2
    inserted = news.kwargs_of("insert_verdict")
    assert inserted["degraded"] is False and inserted["error_code"] is None
    assert inserted["final_decision"] == "escalate" and inserted["verdict"]["magnitude"] == 3
    assert inserted["trace"]["reask_failed"] == "news_triage_timeout"
    assert inserted["trace"]["reasked_after_told_change"] is True
    assert bus.routing_keys() == [RK_VERDICT_PUSH]


def test_triage_degraded_fallback_never_earns_the_novelty_bypass() -> None:
    """A rule-baseline placeholder verdict (no model judgment) is soft-throttled like before policy v3."""

    news = RecordingNews(
        get_verdict=None,
        event_card=_card(),  # watchlist NVDA -> rule baseline pushes
        event_status={
            "pushed_2h": 1,
            "pushed_4h": 1,
            "max_magnitude_2h": 2,
            "max_magnitude_4h": 2,
            "directions_2h": ["neutral"],
            "directions_4h": ["neutral"],
        },
        sent_count_since=0,
        insert_verdict=True,
    )
    bus = FakeBus()
    triage = _triage(news, bus)  # model=None -> degraded fallback

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["degraded"] is True and inserted["final_decision"] == "throttled"
    assert inserted["throttled_by"] == "storyline:asset:NVDA" and inserted["override_rule"] != "novel_bypass"
    assert bus.published == []


def test_triage_novel_event_passes_the_soft_throttle_and_publishes() -> None:
    news = RecordingNews(
        get_verdict=None,
        event_card=_card(priority="normal", provider_score_max=75.0),
        event_status={
            "pushed_2h": 1,
            "pushed_4h": 1,
            "max_magnitude_2h": 2,
            "max_magnitude_4h": 2,
            "directions_2h": ["bullish"],
            "directions_4h": ["bullish"],
        },
        sent_count_since=0,
        insert_verdict=True,
        told_ledger=[_ledger_row("ev-earlier", NOW_MS - 300_000, headline="英伟达发布新芯片")],
    )
    bus = FakeBus()
    model = _ScriptedTriageModel([_model_verdict(novelty="progression")])
    triage = _triage_with_model(news, bus, model)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "push" and inserted["override_rule"] == "novel_bypass"
    assert inserted["throttled_by"] is None
    assert bus.routing_keys() == [RK_VERDICT_PUSH]
    assert "restates_event_id" not in inserted["trace"]
