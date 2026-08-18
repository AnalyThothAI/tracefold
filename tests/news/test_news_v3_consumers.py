"""News V3 consumer unit tests: fake bus + fake repositories, no PostgreSQL and no broker."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.news import consumers as consumers_module
from tracefold.news.agents.analyst import AnalystRunResult
from tracefold.news.analyst_evidence import EvidenceBundle
from tracefold.news.analyst_rules import VerifyResult
from tracefold.news.bus import (
    RK_RAW_LIVE,
    RK_VERDICT_DEEP,
    RK_VERDICT_ESCALATE,
    RK_VERDICT_PUSH,
    BusMessage,
    DeferError,
    PermanentError,
)
from tracefold.news.consumers import (
    AnalystConsumer,
    DeduperConsumer,
    DelivererConsumer,
    JanitorLoop,
    TriageConsumer,
)
from tracefold.news.models import (
    ANALYST_POLICY_VERSION,
    TRIAGE_POLICY_VERSION,
    TRIAGE_PROMPT_VERSION,
    AnalystVerdict,
)
from tracefold.platform.resource import ResourceAdmissionTimeout

NOW_MS = 1_800_000_000_000
WATCHLIST = frozenset({"BTC", "NVDA"})


class FakeBus:
    def __init__(self) -> None:
        self.published: list[BusMessage] = []

    async def publish(self, message: BusMessage) -> None:
        self.published.append(message)

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
        foreign = _message(
            "raw",
            {"params": {**params, "strategy": {"id": 4242, "name": "other"}}, "strategy_id": "4242"},
            routing_key=RK_RAW_LIVE.format(strategy_id="4242"),
        )
        await deduper.handle(foreign)
        with pytest.raises(PermanentError, match="news_raw_params_missing"):
            await deduper.handle(_message("raw", {}))

    asyncio.run(scenario())

    assert len(seen) == 3
    assert seen[0]["ingest_mode"] == "live" and seen[0]["observed_at_ms"] == NOW_MS - 5
    assert seen[0]["trace_id"] == "trace-1" and seen[0]["watchlist_symbols"] == frozenset({"BTC"})
    assert seen[0]["event"].provider_record_id == "3568501"
    assert bus.routing_keys() == ["event.macro.high"]
    assert bus.published[0].payload == {"event_id": "ev-1"}
    assert bus.published[0].priority == 5 and bus.published[0].message_id == "event:ev-1"
    assert news.names() == ["mark_event_published"]
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
    assert inserted["verdict"]["headline_zh"] == "模型不可用（规则兜底）"
    trace = inserted["trace"]
    assert trace["attempt"] == 1 and trace["queue_lag_ms"] >= 0
    assert len(trace["prompt_sha256"]) == 64 and trace["status"] == status_row  # replayable snapshot
    assert "latency_ms" not in trace and "input_sha256" not in trace  # no model call happened
    assert "模型不可用" in news.kwargs_of("set_context_line")["context_line"]
    assert [(m.routing_key, m.payload) for m in bus.published] == [
        (RK_VERDICT_PUSH, {"event_id": "ev-strong", "kind": "first"}),
        (RK_VERDICT_ESCALATE, {"event_id": "ev-strong"}),
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


# ---------------------------------------------------------------- Analyst
def _bundle(status_row: dict[str, Any] | None = None) -> EvidenceBundle:
    return EvidenceBundle(
        event_id="ev-strong",
        storyline_key="asset:NVDA",
        payload={
            "event": {"event_id": "ev-strong", "triage": {"direction": "bullish", "magnitude": 2}},
            "event_status": {},
        },
        evidence={"history:abc": {"event_id": "ev-old"}},
        status_row=dict(status_row or {}),
    )


class ScriptedAnalyst:
    model_name = "test-analyst"
    prompt_sha256 = "a" * 64

    def __init__(self, result: AnalystRunResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def analyze(self, *, bundle: EvidenceBundle, triage_direction: str) -> AnalystRunResult:
        self.calls.append({"bundle": bundle, "triage_direction": triage_direction})
        return self.result


def _analyst_verdict(**overrides: Any) -> AnalystVerdict:
    fields: dict[str, Any] = {
        "agrees_with_triage": True,
        "revised_direction": "bullish",
        "revised_magnitude": 2,
        "novelty_assessment": "new",
        "context_evidence": ["history:abc"],
        "thesis_zh": "英伟达投资 OpenAI 数据中心，利多算力链。",
        "follow_up_needed": True,
        "confidence": 0.7,
    }
    fields.update(overrides)
    return AnalystVerdict(**fields)


def test_analyst_uses_one_prefetched_bundle_and_publishes_followup_after_first_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle({"pushed_2h": 1, "last_push_ago_ms": 600_000})
    monkeypatch.setattr(consumers_module, "build_evidence_bundle", lambda repos, **kwargs: bundle)
    # the Analyst adds something: it revises the magnitude the first card carried
    result = AnalystRunResult(
        _analyst_verdict(revised_magnitude=3), VerifyResult(True), 1200, 1, None, 1, 900, 200, 500
    )
    analyst = ScriptedAnalyst(result)
    news = RecordingNews(
        get_verdict=None,
        event_status={"last_push_ago_ms": 600_000},  # last push predates this run -> not superseded
        insert_verdict=True,
        delivery={"state": "sent"},
    )
    bus = FakeBus()

    asyncio.run(
        AnalystConsumer(bus=bus, db=FakeWorkerDatabase(news), analyst=analyst, concurrency=2).handle(
            _message("verdict", {"event_id": "ev-strong"}, routing_key=RK_VERDICT_ESCALATE)
        )
    )

    assert analyst.calls[0]["bundle"] is bundle and analyst.calls[0]["triage_direction"] == "bullish"
    inserted = news.kwargs_of("insert_verdict")
    assert inserted["stage"] == "deep" and inserted["policy_version"] == ANALYST_POLICY_VERSION
    assert inserted["final_decision"] == "push" and inserted["degraded"] is False
    assert inserted["trace"]["attempts"] == 1 and inserted["trace"]["input_tokens"] == 900
    assert inserted["trace"]["prompt_sha256"] == "a" * 64 and inserted["trace"]["status"] == bundle.status_row
    assert bus.routing_keys() == [RK_VERDICT_DEEP]
    assert bus.published[0].payload == {"event_id": "ev-strong", "kind": "followup"}
    assert news.names()[-1] == "mark_verdict_published"


def test_analyst_followup_without_new_information_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agreeing with Triage, same direction and magnitude, novelty 'new': nothing to add -> no second card."""

    monkeypatch.setattr(consumers_module, "build_evidence_bundle", lambda repos, **kwargs: _bundle())
    analyst = ScriptedAnalyst(AnalystRunResult(_analyst_verdict(), VerifyResult(True), 900, 1, None, 1))
    news = RecordingNews(get_verdict=None, event_status={"last_push_ago_ms": 600_000}, insert_verdict=True)
    bus = FakeBus()

    asyncio.run(
        AnalystConsumer(bus=bus, db=FakeWorkerDatabase(news), analyst=analyst, concurrency=2).handle(
            _message("verdict", {"event_id": "ev-strong"})
        )
    )

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "drop" and inserted["degraded"] is False
    assert bus.published == []


def test_analyst_followup_is_superseded_by_a_newer_push_in_the_same_storyline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(consumers_module, "build_evidence_bundle", lambda repos, **kwargs: _bundle())
    analyst = ScriptedAnalyst(
        AnalystRunResult(_analyst_verdict(revised_magnitude=3), VerifyResult(True), 900, 1, None, 1)
    )
    news = RecordingNews(get_verdict=None, event_status={"last_push_ago_ms": 0}, insert_verdict=True)
    bus = FakeBus()

    asyncio.run(
        AnalystConsumer(bus=bus, db=FakeWorkerDatabase(news), analyst=analyst, concurrency=2).handle(
            _message("verdict", {"event_id": "ev-strong"})
        )
    )

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "throttled"
    assert inserted["throttled_by"] == "storyline:asset:NVDA:superseded"
    assert bus.published == []


def test_analyst_verify_failure_persists_degraded_and_never_publishes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(consumers_module, "build_evidence_bundle", lambda repos, **kwargs: _bundle())
    failed = AnalystRunResult(
        _analyst_verdict(context_evidence=["history:fake"]),
        VerifyResult(False, "context_evidence_unknown"),
        1500,
        2,
        "news_analyst_verify:context_evidence_unknown",
        1,
    )
    news = RecordingNews(get_verdict=None, insert_verdict=True)
    bus = FakeBus()

    asyncio.run(
        AnalystConsumer(bus=bus, db=FakeWorkerDatabase(news), analyst=ScriptedAnalyst(failed), concurrency=1).handle(
            _message("verdict", {"event_id": "ev-strong"})
        )
    )

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "degraded" and inserted["override_rule"] == "verify_failed"
    assert inserted["degraded"] is True and inserted["trace"]["attempts"] == 2
    assert bus.published == [] and "event_status" not in news.names()


def test_analyst_without_model_or_bundle_settles_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    news = RecordingNews(get_verdict=None)
    asyncio.run(
        AnalystConsumer(bus=FakeBus(), db=FakeWorkerDatabase(news), analyst=None, concurrency=1).handle(
            _message("verdict", {"event_id": "ev-strong"})
        )
    )
    assert news.calls == []

    monkeypatch.setattr(consumers_module, "build_evidence_bundle", lambda repos, **kwargs: None)
    analyst = ScriptedAnalyst(AnalystRunResult(None, VerifyResult(False, "not_run"), 0, 0, None, 0))
    with pytest.raises(PermanentError, match="news_event_or_triage_missing"):
        asyncio.run(
            AnalystConsumer(bus=FakeBus(), db=FakeWorkerDatabase(news), analyst=analyst, concurrency=1).handle(
                _message("verdict", {"event_id": "ghost"})
            )
        )
    assert analyst.calls == []


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


def test_deliverer_skips_dropped_first_cards_and_followups_without_push_deep_verdict() -> None:
    news = _delivery_news(latest_verdict=lambda *, event_id, stage: {"final_decision": "drop", "verdict": {}})
    asyncio.run(_deliverer(news, FakeBus()).handle(_message("verdict", {"event_id": "ev-strong"})))
    assert "begin_delivery" not in news.names()

    followup = _delivery_news()
    asyncio.run(
        _deliverer(followup, FakeBus()).handle(_message("verdict", {"event_id": "ev-strong", "kind": "followup"}))
    )
    assert "begin_delivery" not in followup.names()

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
