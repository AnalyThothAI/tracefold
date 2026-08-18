"""News V3 consumer unit tests: fake bus + fake repositories, no PostgreSQL and no broker."""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.news import consumers as consumers_module
from tracefold.news.bus import (
    RK_RAW_LIVE,
    RK_VERDICT_ESCALATE,
    RK_VERDICT_PUSH,
    BusMessage,
    PermanentError,
    TransientError,
)
from tracefold.news.consumers import ControlConsumer, DeduperConsumer, DelivererConsumer, TriageConsumer
from tracefold.news.models import TRIAGE_POLICY_VERSION, TRIAGE_PROMPT_VERSION
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
    def __init__(self, news: RecordingNews, *, admission_timeout_for: set[str] | None = None) -> None:
        self.news = news
        self.operations: list[str] = []
        self.admission_timeout_for = admission_timeout_for or set()

    @contextmanager
    def worker_session(self, name: str, *_args: Any, **_kwargs: Any):
        del name
        yield SimpleNamespace(news=self.news, transaction=nullcontext)

    async def run_business(self, name: str, fn: Any, *args: Any, operation_timeout_seconds: float, **kwargs: Any):
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


def test_deduper_admission_timeout_is_transient_and_publishes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
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
    with pytest.raises(TransientError, match="db_admission_timeout:news_deduper_admit"):
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
    news = RecordingNews(get_verdict=None, event_card=_card(), event_status={}, sent_count_since=0, insert_verdict=True)
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
    assert inserted["trace"]["attempt"] == 1 and inserted["trace"]["queue_lag_ms"] >= 0
    assert "latency_ms" not in inserted["trace"]
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
            event_id="ev-weak", priority="normal", provider_score_max=85.0, grounded_assets=["AMD"], watchlist_hits=[]
        ),
        _card(event_id="ev-macro", priority="high", provider_score_max=75.0, grounded_assets=[], watchlist_hits=[]),
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


# ---------------------------------------------------------------- Deliverer
def _deliverer(news: RecordingNews, bus: FakeBus, *, hourly_cap: int = 20) -> DelivererConsumer:
    deliverer = DelivererConsumer(
        bus=bus,
        db=FakeWorkerDatabase(news),
        sender=None,
        finite_operations=InlineFinite(),
        min_interval_seconds=0.0,
        hourly_cap=hourly_cap,
    )
    deliverer._stop_event = asyncio.Event()
    return deliverer


def _delivery_news(**overrides: Any) -> RecordingNews:
    states = iter(overrides.pop("begin_states", ["new"]))
    responses: dict[str, Any] = {
        "event_card": _card(),
        "latest_verdict": lambda *, event_id, stage: (
            {"final_decision": "push", "verdict": {"direction": "bullish", "magnitude": 2, "headline_zh": "英伟达"}}
            if stage == "triage"
            else None
        ),
        "get_presentation": {"display_title": "英伟达投资", "outcome": "translated"},
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
    assert "sender" not in "".join(news.names())


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


# ---------------------------------------------------------------- Control
def test_control_consumer_applies_commands_to_control_state() -> None:
    news = RecordingNews()
    control = ControlConsumer(bus=FakeBus(), db=FakeWorkerDatabase(news))

    async def scenario() -> None:
        await control.handle(_message("control", {"action": "pause_delivery"}))
        await control.handle(_message("control", {"action": "mute_symbol", "key": "btc", "ttl_ms": 3_600_000}))
        await control.handle(_message("control", {"action": "mute_theme", "key": "rates"}))
        await control.handle(_message("control", {"action": "unmute", "key": "BTC"}))
        await control.handle(_message("control", {"action": "resume_delivery"}))
        with pytest.raises(PermanentError, match="news_control_action_invalid"):
            await control.handle(_message("control", {"action": "explode"}))
        with pytest.raises(PermanentError, match="news_control_key_required"):
            await control.handle(_message("control", {"action": "mute_theme"}))

    asyncio.run(scenario())

    writes = [kwargs for name, kwargs in news.calls if name == "write_control"]
    assert [w["paused"] for w in writes] == [True, True, True, True, False]
    assert [(m["kind"], m["key"]) for m in writes[1]["mutes"]] == [("symbol", "BTC")]
    assert {(m["kind"], m["key"]) for m in writes[2]["mutes"]} == {("symbol", "BTC"), ("theme", "rates")}
    assert [(m["kind"], m["key"]) for m in writes[3]["mutes"]] == [("theme", "rates")]
    assert news.control_state == {"paused": False, "mutes": writes[4]["mutes"]}
    assert all(m["until_ms"] > time.time() * 1000 for m in news.control_state["mutes"])
    assert news.names().count("write_control") == 5
