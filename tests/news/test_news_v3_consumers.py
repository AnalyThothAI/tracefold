"""News V3 consumer unit tests: fake bus + fake repositories, no PostgreSQL and no broker."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from tests.support.news_judgment import trade_relevance
from tracefold.app.workers.wiring.database import WorkerNewsColdDatabase, WorkerNewsDatabase
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.bus import (
    RK_RAW_LIVE,
    RK_VERDICT_PUSH,
    BusMessage,
    DeferError,
    PermanentError,
    TransientError,
)
from tracefold.news.market_review.pricing import Candle
from tracefold.news.models import (
    OUTBOX_MAX_AGE_MS,
    TRIAGE_POLICY_VERSION,
    ReaderDeliveryPresentation,
    ReaderMarketMovement,
    ReaderTradeTarget,
)
from tracefold.news.oi_signals import DEFAULT_OI_POLICY, program_sha256
from tracefold.news.pipeline import admission as admission_module
from tracefold.news.pipeline import triage_audit as triage_audit_module
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.delivery import DelivererConsumer
from tracefold.news.pipeline.maintenance import JanitorLoop
from tracefold.news.pipeline.recovery import RecoveryRunner
from tracefold.news.pipeline.triage import TriageConsumer
from tracefold.news.program.contracts import (
    EditorialEnvelope,
    ProgramCallTrace,
    ProgramTrace,
    ProgramUsage,
    SemanticJudgeError,
    SemanticJudgment,
    TriageContext,
)
from tracefold.news.reader_history import ReaderHistorySnapshot, assemble_reader_history
from tracefold.news.release.canary import CanaryRuntimeArm
from tracefold.news.triage_rules import DEFAULT_POLICY
from tracefold.platform.resource import ResourceAdmissionTimeout

NOW_MS = 1_800_000_000_000
WATCHLIST = frozenset({"BTC", "NVDA"})
PROGRAM_VERSION = "news_semantic_program_test_v1"
PROGRAM_SHA256 = "9" * 64


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

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, {**{f"arg{i}": a for i, a in enumerate(args)}, **kwargs}))
            if name == "reader_history" and name not in self.responses:
                return ReaderHistorySnapshot()  # nothing pushed yet
            if name == "latest_evidence_snapshot" and name not in self.responses:
                card = self.responses.get("event_card") or {}
                return {
                    "evidence_version": int(card.get("evidence_version") or 1),
                    "evidence_sha256": str(card.get("evidence_sha256") or "e" * 64),
                    "focus_fact_id": str(card.get("focus_fact_id") or "fact-1"),
                }
            if name == "event_admission" and name not in self.responses:
                response = self.responses.get("event_card") or {}
                card = response if isinstance(response, dict) else {}
                return {
                    "admission": str(card.get("admission") or "candidate"),
                    "event_kind": str(card.get("event_kind") or "news"),
                    "storyline_key": str(card.get("storyline_key") or ""),
                }
            if name == "outbox_scan" and name not in self.responses:
                # #76: the Janitor turn is one read — rows to rescue plus how many the ceiling gave up on.
                return (self.responses.get("unpublished_candidates") or [], self.responses.get("expired_count", 0))
            value = self.responses.get(name)
            return value(*args, **kwargs) if callable(value) else value

        return _call

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def kwargs_of(self, name: str) -> dict[str, Any]:
        return next(kwargs for called, kwargs in self.calls if called == name)


class RecordingInstruments:
    """The #75 universe as the consumers see it: empty by default, so the Gate falls back to the `XYZ-` prefix and
    the alias table stays inert — every pre-existing expectation holds unchanged."""

    def __init__(self, *, classes: dict[str, str] | None = None, aliases: dict[str, str] | None = None) -> None:
        self.classes = classes or {}
        self.aliases = aliases or {}

    def instrument_classes(self) -> dict[str, str]:
        return dict(self.classes)

    def alias_map(self) -> dict[str, str]:
        return dict(self.aliases)


class RecordingPrice:
    """Quote-plane double; silent by default so non-price tests keep their contract."""

    def __init__(
        self,
        *,
        quotes: list[dict[str, Any]] | None = None,
        reactions: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.quotes = quotes or []
        self.reactions = reactions or []
        self.error = error
        self.requested: list[list[str]] = []
        self.requested_reaction_versions: list[str | None] = []

    def quotes_for_symbols(self, symbols: Any, *, now_ms: int) -> list[dict[str, Any]]:
        del now_ms
        self.requested.append(list(symbols))
        if self.error is not None:
            raise self.error
        return list(self.quotes)

    def event_reactions(self, event_id: str, *, metric_version: str | None = None) -> list[dict[str, Any]]:
        del event_id
        self.requested_reaction_versions.append(metric_version)
        if self.error is not None:
            raise self.error
        return list(self.reactions)


class FakeWorkerDatabase:
    """Stands in for `WorkerDatabase`; the ports it hands out are the production adapters over it.

    Reaching a consumer's own `read`/`tx` lands on the News lane and appends to `operations`; the
    Janitor's cold port lands on `run_business` and appends to `heavy_operations`. A consumer that took
    the wrong lane is therefore visible as a wrong list, not as a passing test.
    """

    def __init__(
        self,
        news: RecordingNews,
        *,
        admission_timeout_for: set[str] | None = None,
        instruments: RecordingInstruments | None = None,
        price: RecordingPrice | None = None,
    ) -> None:
        self.news = news
        self.instruments = instruments or RecordingInstruments()
        self.price = price or RecordingPrice()
        self.operations: list[str] = []
        self.heavy_operations: list[str] = []
        self.admission_timeout_for = admission_timeout_for or set()
        self._port = WorkerNewsDatabase(self)
        self.cold_port = WorkerNewsColdDatabase(self)

    async def read(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        return await self._port.read(name, fn, timeout_seconds=timeout_seconds)

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        return await self._port.tx(name, fn, timeout_seconds=timeout_seconds)

    @contextmanager
    def worker_session(self, name: str, *_args: Any, **_kwargs: Any):
        del name
        yield SimpleNamespace(
            news=self.news,
            instruments=self.instruments,
            price=self.price,
            transaction=nullcontext,
        )

    async def run_news(self, name: str, fn: Any, *args: Any, operation_timeout_seconds: float, **kwargs: Any):
        del operation_timeout_seconds
        self.operations.append(name)
        if name in self.admission_timeout_for:
            raise ResourceAdmissionTimeout(f"worker_database_admission_timeout:{name}")
        return fn(*args, **kwargs)

    def heavy_business(self) -> FakeWorkerDatabase:
        return self

    async def run_business(self, name: str, fn: Any, *args: Any, operation_timeout_seconds: float, **kwargs: Any):
        del operation_timeout_seconds
        self.heavy_operations.append(name)
        if name in self.admission_timeout_for:
            raise ResourceAdmissionTimeout(f"worker_database_admission_timeout:{name}")
        return fn(*args, **kwargs)


class InlineFinite:
    async def run(self, _name: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("timeout_seconds", None)
        kwargs.pop("allow_shutdown", None)
        return fn(*args, **kwargs)


def _card(**overrides: Any) -> dict[str, Any]:
    card = {
        "event_id": "ev-strong",
        "family": "general",
        "leader_title": "NVIDIA to invest $100bn in OpenAI data centre",
        "leader_url": "https://example.test/nvda",
        "leader_description": "",
        "reporting_origin": "FT",
        "admission": "candidate",
        "queue_priority": "high",
        "provider_score_max": 92.0,
        "asset_class": "equity_or_commodity",
        "grounded_assets": ["NVDA"],
        "watchlist_hits": ["NVDA"],
        "macro_lexicon": False,
        "storyline_key": "asset:NVDA",
        "comparison_fingerprint": "f" * 64,
        "trace_id": "trace-1",
        "evidence_version": 1,
        "evidence_sha256": "e" * 64,
        "focus_fact_id": "fact-1",
        "evidence_schema_version": "news_event_evidence_v2",
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
                gate=SimpleNamespace(queue_priority="high", amqp_priority=5),
            ),
            SimpleNamespace(event_created=False, admission="candidate", event_id="ev-1", family="macro", gate=None),
            SimpleNamespace(
                event_created=True,
                admission="suppressed_ungrounded",
                event_id="ev-2",
                family="general",
                gate=SimpleNamespace(queue_priority="normal", amqp_priority=0),
            ),
            # `listing_deterministic` is an admitted admission, not a suppression: exchange listing/delisting
            # frames must reach Triage like any candidate (#72 — they used to die silently right here).
            SimpleNamespace(
                event_created=True,
                admission="listing_deterministic",
                event_id="ev-3",
                family="listing",
                gate=SimpleNamespace(queue_priority="high", amqp_priority=5),
            ),
            # #126: a Strategy Tracefold has no local knowledge of. There is no allowlist to consult — the
            # provider account enabled it and the socket pushed it, so the Gate judges it like any other.
            SimpleNamespace(
                event_created=True,
                admission="candidate",
                event_id="ev-4",
                family="general",
                gate=SimpleNamespace(queue_priority="normal", amqp_priority=0),
            ),
        ]
    )
    seen: list[dict[str, Any]] = []

    def fake_admit(repos: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return SimpleNamespace(results=(next(admissions),))

    monkeypatch.setattr(admission_module, "admit_frame", fake_admit)
    news = RecordingNews()
    bus = FakeBus()
    deduper = DeduperConsumer(bus=bus, db=FakeWorkerDatabase(news), watchlist_symbols=frozenset({"BTC"}))
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
        await deduper.handle(raw)  # redelivery: admit_frame reports nothing new
        await deduper.handle(raw)  # a suppressed admission never reaches Triage
        await deduper.handle(raw)  # a listing admission does
        # An unknown Strategy is ordinary work now, not a frame to drop.
        foreign = _message(
            "raw",
            {"params": {**params, "strategy": {"id": 4242, "name": "other"}}, "strategy_id": "4242"},
            routing_key=RK_RAW_LIVE.format(strategy_id="4242"),
        )
        await deduper.handle(foreign)
        with pytest.raises(PermanentError, match="news_raw_params_missing"):
            await deduper.handle(_message("raw", {}))

    asyncio.run(scenario())

    assert len(seen) == 5
    assert seen[0]["ingest_mode"] == "live" and seen[0]["observed_at_ms"] == NOW_MS - 5
    assert seen[0]["trace_id"] == "trace-1" and seen[0]["watchlist_symbols"] == frozenset({"BTC"})
    assert seen[0]["event"].provider_record_id == "3568501"
    assert bus.routing_keys() == ["event.macro.high", "event.listing.high", "event.general.normal"]
    assert bus.published[0].payload == {"event_id": "ev-1"}
    assert bus.published[0].priority == 5 and bus.published[0].message_id == "event:ev-1"
    assert bus.published[1].payload == {"event_id": "ev-3"}
    assert news.names() == ["mark_event_published"] * 3
    assert news.kwargs_of("mark_event_published")["event_id"] == "ev-1"


def test_deduper_admission_timeout_defers_uncounted_and_publishes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admission_module, "admit_frame", lambda *_a, **_k: pytest.fail("db never admitted"))
    news = RecordingNews()
    bus = FakeBus()
    deduper = DeduperConsumer(
        bus=bus,
        db=FakeWorkerDatabase(news, admission_timeout_for={"news_deduper_admit"}),
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


@pytest.mark.parametrize(
    ("strategy_id", "strategy_name", "source_type"),
    [
        (2026, "聪明钱监控", "wallet"),
        (2083, "Large-scale liquidation", "market"),
    ],
)
def test_unsupported_market_contracts_are_auditable_without_triage_or_delivery(
    strategy_id: int,
    strategy_name: str,
    source_type: str,
) -> None:
    news = RecordingNews(
        upsert_item=True,
        fact_membership=None,
        find_exact_event=None,
        find_artifact_event=None,
        find_band_candidates=[],
    )
    bus = FakeBus()
    deduper = DeduperConsumer(bus=bus, db=FakeWorkerDatabase(news), watchlist_symbols=frozenset())
    raw = _message(
        "raw",
        {
            "params": {
                "id": strategy_id * 1_000,
                "engineType": "market",
                "text": "BTC market frame whose field semantics are not contracted",
                "source": "opennews",
                "ts": NOW_MS,
                "strategy": {
                    "id": strategy_id,
                    "name": strategy_name,
                    "sourceType": source_type,
                },
            },
            "strategy_id": str(strategy_id),
            "ingest_mode": "live",
            "observed_at_ms": NOW_MS,
        },
        routing_key=RK_RAW_LIVE.format(strategy_id=str(strategy_id)),
    )

    asyncio.run(deduper.handle(raw))

    inserted = news.kwargs_of("insert_event")
    assert inserted["event_kind"] == "unsupported_market"
    assert inserted["admission"] == "unsupported_market_contract"
    assert inserted["source_contract_reason"] == "unsupported_market_contract"
    assert news.kwargs_of("upsert_item")["provider_metadata"]["strategies"] == [
        {
            "id": str(strategy_id),
            "name": strategy_name,
            "source_type": source_type,
            "engine_type": "market",
        }
    ]
    assert bus.published == [], "unsupported means no Triage message, hence no model or Delivery path"
    assert "mark_event_published" not in news.names()


# ---------------------------------------------------------------- Triage
class _RecordingJudge:
    """Counts model calls so a test can assert one never happened."""

    def __init__(self) -> None:
        self.calls = 0

    async def judge(self, context: Any) -> Any:
        self.calls += 1
        raise AssertionError("the model must not be called for this Event")


def _triage(news: RecordingNews, bus: FakeBus, *, judge: Any = None) -> TriageConsumer:
    return TriageConsumer(
        bus=bus,
        db=FakeWorkerDatabase(news),
        judge=judge,
        program_version=PROGRAM_VERSION,
        program_sha256=PROGRAM_SHA256,
        watchlist_symbols=WATCHLIST,
        watchlist=sorted(WATCHLIST),
        concurrency=1,
        circuit_failures=3,
        circuit_open_seconds=60.0,
        runtime_manifest={"manifest_sha": "d" * 64},
    )


def test_triage_holds_a_queued_event_reclassified_by_the_current_source_contract() -> None:
    evidence_card = _card(admission="candidate")
    news = RecordingNews(
        event_card=evidence_card,
        event_admission={
            "admission": "unsupported_market_contract",
            "event_kind": "unsupported_market",
            "storyline_key": "asset:NVDA",
        },
        get_verdict=lambda **_kwargs: pytest.fail("held Event must not inspect or republish a verdict"),
    )
    judge = _RecordingJudge()
    bus = FakeBus()

    asyncio.run(_triage(news, bus, judge=judge).handle(_message("event", {"event_id": "ev-strong"})))

    assert evidence_card["admission"] == "candidate", "the immutable evidence snapshot is not rewritten"
    assert judge.calls == 0
    assert "get_verdict" not in news.names()
    assert "insert_verdict" not in news.names()
    assert bus.published == []


def test_triage_without_model_pushes_an_objective_watchlist_fact_and_persists_editorial_identity() -> None:
    """A degraded card is one typed judgment; only the grounded watchlist guard makes it reader-eligible."""

    news = RecordingNews(get_verdict=None, event_card=_card(), insert_verdict=True)
    bus = FakeBus()
    triage = _triage(news, bus)

    asyncio.run(
        triage.handle(_message("event", {"event_id": "ev-strong"}, routing_key="event.general.high", priority=5))
    )

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["stage"] == "triage" and inserted["policy_version"] == TRIAGE_POLICY_VERSION
    assert inserted["degraded"] is True and inserted["error_code"] == "news_semantic_program_unconfigured"
    assert inserted["model"] is None and inserted["model_decision"] is None
    assert "prompt_version" not in inserted
    assert inserted["program_version"] == PROGRAM_VERSION
    assert inserted["program_sha256"] == PROGRAM_SHA256
    assert inserted["editorial"]["editorial_origin"] == "degraded_unavailable"
    assert inserted["editorial"]["relevance"] is None
    assert len(inserted["scored_judgment_sha256"]) == 64
    assert inserted["runtime_manifest_sha"] == "d" * 64
    assert inserted["rule_baseline_decision"] == "push" and inserted["final_decision"] == "push"
    assert inserted["verdict"]["headline_zh"] == "NVIDIA to invest $100bn in OpenAI data centre"  # wire headline
    trace = inserted["trace"]
    assert trace["attempt"] == 1 and trace["queue_lag_ms"] >= 0
    assert trace["program_version"] == PROGRAM_VERSION and trace["program_sha256"] == PROGRAM_SHA256
    assert trace["verdict_sha256"] == canonical_sha(inserted["verdict"])
    assert trace["status"] == {
        "storyline_key": "asset:NVDA",
        "preliminary": True,
        "queue_lag_ms": trace["queue_lag_ms"],
    }  # replayable, quota-free input snapshot
    assert "latency_ms" not in trace and "input_sha256" not in trace  # no model call happened
    assert "NVIDIA to invest $100bn" in news.kwargs_of("set_context_line")["context_line"]
    # escalate is a high-importance push (⚡ + priority); there is no second lane to notify
    assert [(m.routing_key, m.payload) for m in bus.published] == [
        (RK_VERDICT_PUSH, {"event_id": "ev-strong", "kind": "first"}),
    ]
    assert all(m.priority == 5 and m.trace_id == "trace-1" for m in bus.published)
    assert news.names()[-1] == "mark_verdict_published"
    # #267 is scoped to the deterministic lanes. Here the Event's assets are the Gate's grounding
    # evidence, provenance-checked against the Item; promoting a non-deterministic verdict's own
    # primaries would let an unchecked reading seed a price measurement and a canonical asset.
    assert "record_event_assets" not in news.names()


@pytest.mark.parametrize(
    "card",
    [
        _card(
            event_id="ev-weak",
            queue_priority="normal",
            provider_score_max=75.0,
            grounded_assets=["AMD"],
            watchlist_hits=[],
        ),
        _card(
            event_id="ev-quiet",
            queue_priority="normal",
            provider_score_max=95.0,
            grounded_assets=[],
            watchlist_hits=[],
        ),
    ],
)
def test_triage_without_model_drops_when_rule_baseline_drops(card: dict[str, Any]) -> None:
    news = RecordingNews(get_verdict=None, event_card=card, insert_verdict=True)
    bus = FakeBus()

    asyncio.run(_triage(news, bus).handle(_message("event", {"event_id": card["event_id"]})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["degraded"] is True
    assert inserted["rule_baseline_decision"] == "drop" and inserted["final_decision"] == "drop"
    assert bus.published == []
    assert "mark_verdict_published" not in news.names()


def test_triage_without_model_fails_open_on_a_deterministic_listing() -> None:
    card = _card(
        event_id="ev-listing",
        queue_priority="normal",
        admission="listing_deterministic",
        provider_score_max=10.0,
        grounded_assets=[],
        watchlist_hits=[],
    )

    news = RecordingNews(get_verdict=None, event_card=card, insert_verdict=True)
    bus = FakeBus()

    asyncio.run(_triage(news, bus).handle(_message("event", {"event_id": card["event_id"]})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["degraded"] is True
    assert inserted["rule_baseline_decision"] == "push" and inserted["final_decision"] == "push"
    assert inserted["verdict"]["headline_zh"]  # the wire headline, which delivery renders verbatim
    assert bus.routing_keys() == [RK_VERDICT_PUSH]


def test_triage_without_model_does_not_treat_provider_score_or_queue_priority_as_editorial_truth() -> None:

    card = _card(
        event_id="ev-strong-80",
        queue_priority="high",
        provider_score_max=85.0,
        grounded_assets=["AMD"],
        watchlist_hits=[],
    )
    news = RecordingNews(get_verdict=None, event_card=card, insert_verdict=True)
    bus = FakeBus()

    asyncio.run(_triage(news, bus).handle(_message("event", {"event_id": card["event_id"]})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["degraded"] is True and inserted["rule_baseline_decision"] == "drop"
    assert inserted["final_decision"] == "drop"
    assert bus.routing_keys() == []


def _program_call(
    *,
    predictor: Literal["event_semantics", "reader_card"],
    marker: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    provider_cost_microusd: int | None,
) -> ProgramCallTrace:
    return ProgramCallTrace(
        predictor=predictor,
        route="primary",
        attempt=1,
        request_sha256=marker * 64,
        input_sha256=marker * 64,
        model_binding="primary",
        physical_provider_call=True,
        output_sha256="d" * 64,
        validated_output={"marker": marker},
        provider="test",
        model="fake",
        model_sha256="e" * 64,
        latency_ms=7,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        total_tokens=input_tokens + output_tokens,
        provider_cost_microusd=provider_cost_microusd,
        finish_reason="stop",
    )


def _program_trace(
    *,
    fallback_from: str | None = None,
    novelty_defaulted: bool = False,
    context_sha256: str = "1" * 64,
    verdict_sha256: str | None = "7" * 64,
    calls: tuple[ProgramCallTrace, ...] = (),
) -> ProgramTrace:
    return ProgramTrace(
        program_version=PROGRAM_VERSION,
        program_sha256=PROGRAM_SHA256,
        context_sha256=context_sha256,
        factory_id="news_semantic_program_v1",
        event_semantics_sha256="5" * 64,
        reader_card_sha256="6" * 64,
        verdict_sha256=verdict_sha256,
        answering_route="fallback" if fallback_from else "primary",
        fallback_from=fallback_from,
        novelty_defaulted=novelty_defaulted,
        calls=calls,
    )


def _synthetic_program_call() -> ProgramCallTrace:
    return ProgramCallTrace(
        predictor="event_semantics",
        route="primary",
        attempt=1,
        request_sha256="0" * 64,
        input_sha256="0" * 64,
        model_binding="primary",
        error_code="news_program_model_binding_unresolved",
    )


def _judgment(
    verdict: Any,
    *,
    model: str = "fake",
    fallback_from: str | None = None,
    trace: ProgramTrace | None = None,
    usage: ProgramUsage | None = None,
) -> SemanticJudgment:
    verdict_payload = verdict.model_dump(mode="json") if hasattr(verdict, "model_dump") else dict(verdict)
    editorial = EditorialEnvelope.issue(editorial_origin="model", relevance=trade_relevance())
    default_calls = (
        _program_call(
            predictor="event_semantics",
            marker="8",
            input_tokens=1,
            output_tokens=1,
            cached_tokens=0,
            provider_cost_microusd=None,
        ),
        _program_call(
            predictor="reader_card",
            marker="9",
            input_tokens=1,
            output_tokens=1,
            cached_tokens=0,
            provider_cost_microusd=None,
        ),
    )
    actual_trace = (
        trace.model_copy(update={"editorial_sha256": editorial.editorial_sha256})
        if trace is not None
        else _program_trace(
            fallback_from=fallback_from,
            verdict_sha256=canonical_sha(verdict_payload),
            calls=default_calls,
        ).model_copy(update={"editorial_sha256": editorial.editorial_sha256})
    )
    physical_calls = tuple(call for call in actual_trace.calls if call.physical_provider_call)
    complete_cost = bool(physical_calls) and all(call.provider_cost_microusd is not None for call in physical_calls)
    return SemanticJudgment(
        verdict=verdict,
        editorial=editorial,
        program_version=PROGRAM_VERSION,
        program_sha256=PROGRAM_SHA256,
        trace=actual_trace,
        usage=usage
        or ProgramUsage(
            wall_latency_ms=10,
            call_count=len(actual_trace.calls),
            physical_call_count=len(physical_calls),
            input_tokens=sum(call.input_tokens for call in actual_trace.calls),
            output_tokens=sum(call.output_tokens for call in actual_trace.calls),
            cached_tokens=sum(call.cached_tokens for call in actual_trace.calls),
            total_tokens=sum(call.total_tokens for call in actual_trace.calls),
            provider_cost_microusd=(
                sum(int(call.provider_cost_microusd) for call in physical_calls) if complete_cost else None
            ),
        ),
        answering_model=model,
        fallback_from=fallback_from,
    )


def _program_error(
    code: str,
    *,
    retryable: bool = False,
    output_failure: bool = False,
    partial_trace: ProgramTrace | None = None,
) -> SemanticJudgeError:
    return SemanticJudgeError(
        code,
        retryable=retryable,
        output_failure=output_failure,
        attempts=1,
        partial_trace=partial_trace,
    )


def test_failed_program_usage_ignores_synthetic_entry_before_costed_fallback_calls() -> None:
    synthetic = _synthetic_program_call()
    fallback_calls = (
        _program_call(
            predictor="event_semantics",
            marker="1",
            input_tokens=5,
            output_tokens=2,
            cached_tokens=1,
            provider_cost_microusd=11,
        ).model_copy(update={"route": "fallback"}),
        _program_call(
            predictor="reader_card",
            marker="2",
            input_tokens=7,
            output_tokens=3,
            cached_tokens=0,
            provider_cost_microusd=13,
        ).model_copy(update={"route": "fallback"}),
    )
    program_trace = _program_trace(
        fallback_from="news_program_model_binding_unresolved",
        verdict_sha256=None,
        calls=(synthetic, *fallback_calls),
    )

    usage = triage_audit_module._usage_from_partial_trace(program_trace, attempts=3)
    trace: dict[str, Any] = {}
    executions = [{"trace": program_trace.model_dump(mode="json"), "usage": usage}]
    triage_audit_module._sync_program_audit(trace, executions=executions, selected_execution_index=None)

    assert usage == {
        "wall_latency_ms": 14,
        "call_count": 3,
        "physical_call_count": 2,
        "input_tokens": 12,
        "output_tokens": 5,
        "cached_tokens": 1,
        "total_tokens": 17,
        "provider_cost_microusd": 24,
    }
    assert trace["model_attempts"] == 3
    assert trace["physical_model_attempts"] == 2
    assert trace["provider_cost_microusd"] == 24
    assert len(trace["program_executions"][0]["trace"]["calls"]) == 3


def test_failed_program_synthetic_only_trace_has_zero_physical_cost() -> None:
    synthetic = _synthetic_program_call()
    program_trace = _program_trace(verdict_sha256=None, calls=(synthetic,))

    usage = triage_audit_module._usage_from_partial_trace(program_trace, attempts=1)
    trace: dict[str, Any] = {}
    executions = [{"trace": program_trace.model_dump(mode="json"), "usage": usage}]
    triage_audit_module._sync_program_audit(trace, executions=executions, selected_execution_index=None)

    assert usage == {
        "wall_latency_ms": 0,
        "call_count": 1,
        "physical_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "provider_cost_microusd": 0,
    }
    assert trace["model_attempts"] == 1
    assert trace["physical_model_attempts"] == 0
    assert trace["provider_cost_microusd"] == 0
    assert trace["program_executions"][0]["trace"]["calls"][0]["physical_provider_call"] is False


class _ScriptedSemanticJudge:
    """SemanticJudge double that records typed contexts and returns or raises scripted outcomes."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.inputs: list[TriageContext] = []

    async def judge(self, context: TriageContext) -> SemanticJudgment:
        self.inputs.append(context)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, SemanticJudgment):
            return outcome
        return _judgment(outcome)


def _triage_with_judge(news: RecordingNews, bus: FakeBus, judge: Any) -> TriageConsumer:
    triage = _triage(news, bus)
    triage.judge = judge
    return triage


def test_triage_output_failure_is_traced_and_never_opens_the_circuit() -> None:
    """max_tokens truncation / schema mismatch: degraded verdict with finish_reason + parsing_error in the trace, the
    transport circuit stays closed (no incident), and the next Event still reaches the model."""

    card = _card()
    news = RecordingNews(get_verdict=None, event_card=card, insert_verdict=True)
    bus = FakeBus()
    truncated = [_program_error("news_program_output_truncated", output_failure=True) for _ in range(4)]
    triage = _triage_with_judge(news, bus, _ScriptedSemanticJudge(truncated))

    for index in range(4):
        asyncio.run(triage.handle(_message("event", {"event_id": f"ev-trunc-{index}"})))

    inserted = [kwargs for name, kwargs in news.calls if name == "insert_verdict"]
    assert len(inserted) == 4
    assert all(row["degraded"] is True and row["error_code"] == "news_program_output_truncated" for row in inserted)
    assert inserted[0]["trace"]["model_failure_retryable"] is False
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


def test_triage_runtime_manifest_registration_is_a_required_startup_operation() -> None:
    news = RecordingNews()
    triage = TriageConsumer(
        bus=FakeBus(),
        db=FakeWorkerDatabase(news, admission_timeout_for={"news_agent_runtime_manifest"}),
        judge=None,
        program_version=PROGRAM_VERSION,
        program_sha256=PROGRAM_SHA256,
        watchlist_symbols=WATCHLIST,
        watchlist=sorted(WATCHLIST),
        concurrency=1,
        circuit_failures=3,
        circuit_open_seconds=60.0,
        runtime_manifest={
            "manifest_sha": "1" * 64,
            "stable_bundle_sha": "2" * 64,
            "candidate_shas": [],
            "image_digest": "sha256:" + "3" * 64,
            "runtime_revision": "git:test",
            "now_ms": NOW_MS,
        },
    )

    with pytest.raises(DeferError, match="db_admission_timeout:news_agent_runtime_manifest"):
        asyncio.run(triage.register_runtime_manifest())


def test_triage_transport_failures_open_the_circuit_and_a_success_closes_the_incident() -> None:
    from tracefold.news.models import TriageVerdict

    card = _card()
    news = RecordingNews(
        get_verdict=None,
        event_card=card,
        insert_verdict=True,
        open_incident=1,
        close_open_incidents=1,
    )
    bus = FakeBus()
    ok = _judgment(
        TriageVerdict(
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
        )
    )
    outcomes: list[Any] = [_program_error("news_program_timeout", retryable=True) for _ in range(3)] + [ok]
    triage = _triage_with_judge(news, bus, _ScriptedSemanticJudge(outcomes))
    triage.circuit.open_seconds = 0.0  # let the fourth call reach the model in the same test clock

    for index in range(4):
        asyncio.run(triage.handle(_message("event", {"event_id": f"ev-net-{index}"})))

    assert news.names().count("open_incident") == 1
    assert news.kwargs_of("open_incident")["cause_class"] == "triage_circuit_open"
    assert news.kwargs_of("close_open_incidents")["cause_classes"] == ["triage_circuit_open"]
    inserted = [kwargs for name, kwargs in news.calls if name == "insert_verdict"]
    assert [row["degraded"] for row in inserted] == [True, True, True, False]


def test_triage_records_the_answering_model_and_the_fallback_reason() -> None:
    from tracefold.news.models import TriageVerdict

    news = RecordingNews(get_verdict=None, event_card=_card(), insert_verdict=True)
    bus = FakeBus()
    answered_by_fallback = _judgment(
        TriageVerdict(
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
        model="deepseek-chat",
        fallback_from="news_program_timeout",
    )
    triage = _triage_with_judge(news, bus, _ScriptedSemanticJudge([answered_by_fallback]))

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-fallback"})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["model"] == "deepseek-chat" and inserted["degraded"] is False
    assert inserted["trace"]["verdict_sha256"] == canonical_sha(inserted["verdict"])
    assert inserted["trace"]["model_fallback_from"] == "news_program_timeout"
    assert inserted["trace"]["model_attempts"] == 2


def test_triage_replays_an_existing_unpublished_decision_without_reinserting() -> None:
    news = RecordingNews(
        get_verdict={"final_decision": "push", "published_at_ms": None},
        event_card=_card(),
    )
    bus = FakeBus()

    asyncio.run(_triage(news, bus).handle(_message("event", {"event_id": "ev-strong"})))
    assert bus.routing_keys() == [RK_VERDICT_PUSH]
    assert "event_card" in news.names()
    assert "insert_verdict" not in news.names()
    assert news.names()[-2:] == ["get_verdict", "mark_verdict_published"]

    settled = RecordingNews(get_verdict={"final_decision": "drop", "published_at_ms": None}, event_card=_card())
    quiet = FakeBus()
    asyncio.run(_triage(settled, quiet).handle(_message("event", {"event_id": "ev-strong"})))
    assert quiet.published == [] and settled.names()[-1] == "get_verdict"


def test_triage_rejects_missing_event_id_and_missing_event() -> None:
    news = RecordingNews(get_verdict=None, event_card=None)
    triage = _triage(news, FakeBus())
    with pytest.raises(PermanentError, match="news_event_id_missing"):
        asyncio.run(triage.handle(_message("event", {})))
    with pytest.raises(PermanentError, match="news_event_missing"):
        asyncio.run(triage.handle(_message("event", {"event_id": "ghost"})))


# ---------------------------------------------------------------- Deliverer
class RecordingSender:
    def __init__(self, order: list[str] | None = None) -> None:
        self.cards: list[dict[str, Any]] = []
        self.presentations: list[ReaderDeliveryPresentation] = []
        self.order = order

    def prepare(self) -> None:
        if self.order is not None:
            self.order.append("prepare")

    def send_card(
        self,
        card: Mapping[str, Any],
        *,
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        if self.order is not None:
            self.order.append("send")
        self.cards.append(dict(card))
        self.presentations.append(presentation or ReaderDeliveryPresentation())
        return {"status_code": 200, "code": 0}

    def close(self) -> None:
        return None


def _deliverer(
    news: RecordingNews,
    bus: FakeBus,
    *,
    price: RecordingPrice | None = None,
    sender: RecordingSender | None = None,
    candle_fetcher_for: Any | None = None,
) -> DelivererConsumer:
    return DelivererConsumer(
        bus=bus,
        db=FakeWorkerDatabase(news, price=price),
        sender=sender,
        finite_operations=InlineFinite(),
        min_interval_seconds=0.0,
        candle_fetcher_for=candle_fetcher_for,
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
        "begin_delivery": lambda **_k: next(states),
        "settle_delivery": True,
    }
    responses.update(overrides)
    return RecordingNews(**responses)


def test_deliverer_holds_a_queued_push_reclassified_by_the_current_source_contract() -> None:
    sender = RecordingSender()
    news = _delivery_news(
        event_admission={
            "admission": "unsupported_market_contract",
            "event_kind": "unsupported_market",
            "storyline_key": "asset:NVDA",
        },
        latest_verdict=lambda **_kwargs: pytest.fail("held Event must not read the historical push verdict"),
    )

    asyncio.run(
        _deliverer(news, FakeBus(), sender=sender).handle(
            _message("verdict", {"event_id": "ev-strong", "kind": "first"})
        )
    )

    assert sender.cards == []
    assert "latest_verdict" not in news.names()
    assert "begin_delivery" not in news.names()
    assert "settle_delivery" not in news.names()


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


def test_deliverer_prepares_the_provider_before_creating_the_sending_row() -> None:
    order: list[str] = []
    news = _delivery_news(begin_delivery=lambda **_kwargs: order.append("begin") or "new")
    sender = RecordingSender(order)

    asyncio.run(
        _deliverer(news, FakeBus(), sender=sender).handle(
            _message("verdict", {"event_id": "ev-strong", "kind": "first"})
        )
    )

    assert order == ["prepare", "begin", "send"]


def test_deliverer_settles_a_preflight_failure_without_calling_send() -> None:
    class PreflightError(RuntimeError):
        code = "news_delivery_telegram_preflight_transport_failed"

    class FailingPrepareSender(RecordingSender):
        def prepare(self) -> None:
            raise PreflightError

    news = _delivery_news()
    sender = FailingPrepareSender()

    asyncio.run(
        _deliverer(news, FakeBus(), sender=sender).handle(
            _message("verdict", {"event_id": "ev-strong", "kind": "first"})
        )
    )

    begin = news.kwargs_of("begin_delivery")
    settle = news.kwargs_of("settle_delivery")
    assert begin["card"] == {}
    assert settle["state"] == "terminal"
    assert settle["error_code"] == "news_delivery_telegram_preflight_transport_failed"
    assert sender.cards == []


def test_deliverer_without_sender_leaves_existing_delivery_untouched() -> None:
    news = _delivery_news(begin_states=["terminal"])

    asyncio.run(_deliverer(news, FakeBus()).handle(_message("verdict", {"event_id": "ev-strong", "kind": "first"})))

    assert "begin_delivery" in news.names() and "settle_delivery" not in news.names()


def test_deliverer_has_no_reader_count_input() -> None:
    news = _delivery_news()

    asyncio.run(_deliverer(news, FakeBus()).handle(_message("verdict", {"event_id": "ev-strong"})))

    assert "sent_count_since" not in news.names()
    settle = news.kwargs_of("settle_delivery")
    assert settle["state"] == "terminal" and settle["error_code"] == "delivery_unavailable"


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


def test_deliverer_prices_exactly_the_assets_the_card_names() -> None:
    price = RecordingPrice(
        quotes=[
            {
                "requested_symbol": "NVDA",
                "symbol": "NVDA",
                "base_symbol": "NVDA",
                "venue": "binance.perp",
                "venue_symbol": "NVDAUSDT",
                "quote_asset": "USDT",
                "price": "217.32",
                "change_pct": 1.5,
                "change_basis": "rolling_24h",
                "instrument_class": "equity",
                "state": "fresh",
            }
        ]
    )
    news = _delivery_news(
        latest_verdict=lambda *, event_id, stage: {
            "final_decision": "push",
            "verdict": {
                "direction": "bullish",
                "magnitude": 2,
                "headline_zh": "英伟达",
                "assets": [{"symbol": "NVDA", "role": "primary"}, {"symbol": "OPENAI", "role": "mentioned"}],
            },
        }
    )
    sender = RecordingSender()

    asyncio.run(
        _deliverer(news, FakeBus(), price=price, sender=sender).handle(
            _message("verdict", {"event_id": "ev-strong", "kind": "first"})
        )
    )

    assert price.requested == [["NVDA"]]
    assert sender.cards[0]["elements"][0]["content"].splitlines()[-1] == "行情 NVDA $217.32 24h +1.50%（永续）"
    assert sender.presentations[0].trade_targets == (
        ReaderTradeTarget(
            ticker="NVDA",
            venue="binance.perp",
            venue_symbol="NVDAUSDT",
            base_symbol="NVDA",
            quote_asset="USDT",
        ),
    )


def test_deliverer_passes_multi_asset_returns_and_timing_as_ephemeral_presentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tracefold.news.pipeline.delivery.now_ms", lambda: NOW_MS)
    price = RecordingPrice(
        quotes=[
            {
                "requested_symbol": "BTC",
                "symbol": "BTC",
                "base_symbol": "BTC",
                "venue": "binance.perp",
                "venue_symbol": "BTCUSDT",
                "quote_asset": "USDT",
                "price": "101.10",
                "change_pct": 3.2,
                "change_basis": "rolling_24h",
                "instrument_class": "crypto",
                "state": "fresh",
            },
            {
                "requested_symbol": "ETH",
                "symbol": "ETH",
                "base_symbol": "ETH",
                "venue": "binance.spot",
                "venue_symbol": "ETHUSDT",
                "quote_asset": "USDT",
                "price": "201.00",
                "change_pct": 1.7,
                "change_basis": "rolling_24h",
                "instrument_class": "crypto",
                "state": "fresh",
            },
        ],
    )
    news = _delivery_news(
        event_card=_card(
            leader_published_at_ms=NOW_MS - 20_000,
            leader_url="https://www.bloomberg.com/news/articles/example",
            grounded_assets=["BTC", "ETH"],
        ),
        event_delivery_timing={
            "news_at_ms": NOW_MS - 20_000,
            "reaction_anchor_at_ms": NOW_MS - 20_000,
            "observed_at_ms": NOW_MS - 8_000,
        },
        latest_verdict=lambda *, event_id, stage: {
            "final_decision": "push",
            "verdict": {
                "direction": "bullish",
                "magnitude": 2,
                "headline_zh": "比特币与以太坊走强",
                "assets": [
                    {"symbol": "BTC", "role": "primary"},
                    {"symbol": "ETH", "role": "primary"},
                ],
            },
        },
    )
    sender = RecordingSender()
    candle_calls: list[tuple[str, str, int, int]] = []

    def candle_fetcher_for(venue: str) -> Any:
        async def fetch(venue_symbol: str, start_ms: int, end_ms: int) -> tuple[Candle, ...]:
            candle_calls.append((venue, venue_symbol, start_ms, end_ms))
            hour_price, news_price = {
                "BTCUSDT": ("99.00", "100.00"),
                "ETHUSDT": ("200.00", "199.00"),
            }[venue_symbol]
            hour_at = NOW_MS - 3_600_000
            news_at = NOW_MS - 20_000
            return (
                Candle(hour_at - 60_000, hour_at, Decimal(hour_price)),
                Candle(news_at - 60_000, news_at, Decimal(news_price)),
            )

        return fetch

    asyncio.run(
        _deliverer(
            news,
            FakeBus(),
            price=price,
            sender=sender,
            candle_fetcher_for=candle_fetcher_for,
        ).handle(_message("verdict", {"event_id": "ev-strong", "kind": "first"}))
    )

    assert sender.presentations == [
        ReaderDeliveryPresentation(
            trade_targets=(
                ReaderTradeTarget("BTC", "binance.perp", "BTCUSDT", "BTC", "USDT"),
                ReaderTradeTarget("ETH", "binance.spot", "ETHUSDT", "ETH", "USDT"),
            ),
            market_movements=(
                ReaderMarketMovement("BTC", 110, 212, 320, "available"),
                ReaderMarketMovement("ETH", 101, 50, 170, "available"),
            ),
            news_at_ms=NOW_MS - 20_000,
            observed_at_ms=NOW_MS - 8_000,
        )
    ]
    assert price.requested_reaction_versions == []
    assert candle_calls == [
        ("binance.perp", "BTCUSDT", NOW_MS - 3_690_000, NOW_MS),
        ("binance.spot", "ETHUSDT", NOW_MS - 3_690_000, NOW_MS),
    ]


def _oi_delivery_news() -> RecordingNews:
    return _delivery_news(
        # Migration can recover the durable kind while retaining the historical
        # admission. Delivery therefore follows event_kind, not stale routing.
        event_card=_card(admission="candidate", event_kind="oi", grounded_assets=[]),
        latest_verdict=lambda *, event_id, stage: {
            "final_decision": "push",
            "program_version": "news_oi_signal_v1",
            "program_sha256": program_sha256(DEFAULT_OI_POLICY),
            "verdict": {
                "direction": "bullish",
                "magnitude": 2,
                "headline_zh": "▲ DOGE 持仓异动8.64%｜持仓7301万｜鲸鱼占比211.0%｜鲸鱼多头盈利80.6%｜4h内第1次",
                "assets": [{"symbol": "DOGE", "role": "primary", "market_type": "perp"}],
            },
        },
        oi_signal={"symbol": "DOGE", "metric_version": "oi_signal_v1"},
    )


def test_deliverer_prices_the_verified_asset_on_an_oi_card() -> None:
    price = RecordingPrice(
        quotes=[
            {
                "symbol": "DOGE",
                "price": "0.2143",
                "change_pct": 8.64,
                "change_basis": "rolling_24h",
                "instrument_class": "crypto",
                "state": "fresh",
            }
        ]
    )
    news = _oi_delivery_news()
    sender = RecordingSender()

    asyncio.run(
        _deliverer(news, FakeBus(), price=price, sender=sender).handle(
            _message("verdict", {"event_id": "ev-strong", "kind": "first"})
        )
    )

    assert price.requested == [["DOGE"]]
    lines = sender.cards[0]["elements"][0]["content"].splitlines()
    assert "DOGE" in lines[0]
    assert lines[-1] == "行情 DOGE $0.2143 24h +8.64%"


def test_deliverer_omits_a_stale_oi_quote_after_requesting_the_verified_symbol() -> None:
    price = RecordingPrice(quotes=[{"symbol": "DOGE", "price": "0.2143", "state": "stale"}])
    sender = RecordingSender()

    asyncio.run(
        _deliverer(_oi_delivery_news(), FakeBus(), price=price, sender=sender).handle(
            _message("verdict", {"event_id": "ev-strong", "kind": "first"})
        )
    )

    assert price.requested == [["DOGE"]]
    assert "行情" not in json.dumps(sender.cards[0], ensure_ascii=False)


def test_deliverer_does_not_upgrade_a_queued_pre_v2_oi_verdict() -> None:
    news = _oi_delivery_news()
    current = news.responses["latest_verdict"]
    pre_v2_program_sha256 = "a0c21e0745d4a7536431db744de3d4df241b223ca8345cc2e389426c245ad626"
    assert pre_v2_program_sha256 != program_sha256(DEFAULT_OI_POLICY)
    news.responses["latest_verdict"] = lambda *, event_id, stage: {
        **current(event_id=event_id, stage=stage),
        "program_sha256": pre_v2_program_sha256,
    }
    price = RecordingPrice(quotes=[{"symbol": "DOGE", "price": "0.2143", "state": "fresh"}])
    sender = RecordingSender()

    asyncio.run(
        _deliverer(news, FakeBus(), price=price, sender=sender).handle(
            _message("verdict", {"event_id": "ev-strong", "kind": "first"})
        )
    )

    assert "oi_signal" not in news.names()
    assert price.requested == []
    assert "行情" not in json.dumps(sender.cards[0], ensure_ascii=False)


def test_deliverer_does_not_price_an_ordinary_ungrounded_model_asset() -> None:
    price = RecordingPrice(quotes=[{"symbol": "DOGE", "price": "0.2143", "state": "fresh"}])
    news = _delivery_news(
        event_card=_card(admission="candidate", grounded_assets=[]),
        latest_verdict=lambda *, event_id, stage: {
            "final_decision": "push",
            "program_version": "program-v4",
            "verdict": {
                "direction": "bullish",
                "magnitude": 2,
                "headline_zh": "模型提到了 DOGE",
                "assets": [{"symbol": "DOGE", "role": "primary"}],
            },
        },
    )
    sender = RecordingSender()

    asyncio.run(
        _deliverer(news, FakeBus(), price=price, sender=sender).handle(
            _message("verdict", {"event_id": "ev-strong", "kind": "first"})
        )
    )

    assert price.requested == []
    assert "行情" not in json.dumps(sender.cards[0], ensure_ascii=False)


def test_deliverer_delivers_when_the_price_plane_fails() -> None:
    news = _delivery_news()
    sender = RecordingSender()

    asyncio.run(
        _deliverer(
            news, FakeBus(), price=RecordingPrice(error=RuntimeError("quote lane on fire")), sender=sender
        ).handle(_message("verdict", {"event_id": "ev-strong", "kind": "first"}))
    )

    assert news.kwargs_of("settle_delivery")["state"] == "sent"
    assert "行情" not in json.dumps(sender.cards[0], ensure_ascii=False)


def test_deliverer_delivers_when_quote_read_cannot_be_admitted() -> None:
    news = _delivery_news()
    sender = RecordingSender()
    db = FakeWorkerDatabase(news, admission_timeout_for={"news_delivery_quotes"}, price=RecordingPrice())
    deliverer = DelivererConsumer(
        bus=FakeBus(), db=db, sender=sender, finite_operations=InlineFinite(), min_interval_seconds=0.0
    )

    asyncio.run(deliverer.handle(_message("verdict", {"event_id": "ev-strong", "kind": "first"})))

    assert "news_delivery_quotes" in db.operations
    assert news.kwargs_of("settle_delivery")["state"] == "sent"
    assert "行情" not in json.dumps(sender.cards[0], ensure_ascii=False)


def test_deliverer_does_not_read_quotes_for_a_card_it_will_not_send() -> None:
    dropped = _delivery_news(latest_verdict=lambda *, event_id, stage: {"final_decision": "drop", "verdict": {}})
    dropped_price = RecordingPrice()
    asyncio.run(
        _deliverer(dropped, FakeBus(), price=dropped_price, sender=RecordingSender()).handle(
            _message("verdict", {"event_id": "ev-strong"})
        )
    )
    assert dropped_price.requested == []

    unavailable_price = RecordingPrice()
    asyncio.run(
        _deliverer(_delivery_news(), FakeBus(), price=unavailable_price).handle(
            _message("verdict", {"event_id": "ev-strong"})
        )
    )
    assert unavailable_price.requested == []


# ---------------------------------------------------------------- Janitor
def test_janitor_republishes_candidates_that_never_left_the_process() -> None:
    news = RecordingNews(
        unpublished_candidates=[{"event_id": "ev-lost"}, {"event_id": "ev-gone"}],
        event_card=lambda event_id: (
            _card(event_id="ev-lost", family="general", queue_priority="normal") if event_id == "ev-lost" else None
        ),
    )
    bus = FakeBus()

    db = FakeWorkerDatabase(news)
    republished = asyncio.run(JanitorLoop(db=db, cold_db=db.cold_port, bus=bus).republish_unpublished())

    assert republished == 1
    assert bus.routing_keys() == ["event.general.normal"]
    assert bus.published[0].payload == {"event_id": "ev-lost"} and bus.published[0].trace_id == "trace-1"
    assert news.kwargs_of("mark_event_published")["event_id"] == "ev-lost"
    # #76: the catch-up scan is bounded on both sides — a floor so it skips Events still mid-publish, and a
    # ceiling so it never delivers something the reader can no longer use.
    scan = news.kwargs_of("outbox_scan")
    assert scan["older_than_ms"] > scan["newer_than_ms"]
    assert scan["older_than_ms"] - scan["newer_than_ms"] == OUTBOX_MAX_AGE_MS - 15_000


def test_janitor_never_gives_up_on_a_stranded_event_silently(caplog, monkeypatch) -> None:
    """The ceiling drops work on the floor; that has to be visible or it is just a quieter version of #72."""

    news = RecordingNews(unpublished_candidates=[], expired_count=3)
    # Some earlier real-runtime tests reconfigure logging. This unit owns its logger state and restores it.
    monkeypatch.setattr(logging.getLogger("tracefold.news"), "disabled", False)
    with caplog.at_level("WARNING", logger="tracefold.news"):
        stranded = FakeWorkerDatabase(news)
        asyncio.run(JanitorLoop(db=stranded, cold_db=stranded.cold_port, bus=FakeBus()).republish_unpublished())
    assert any("gave up on 3 stranded event" in r.getMessage() for r in caplog.records)

    quiet = RecordingNews(unpublished_candidates=[])
    with caplog.at_level("WARNING", logger="tracefold.news"):
        caplog.clear()
        quiet_db = FakeWorkerDatabase(quiet)
        asyncio.run(JanitorLoop(db=quiet_db, cold_db=quiet_db.cold_port, bus=FakeBus()).republish_unpublished())
    assert not [r for r in caplog.records if "gave up" in r.getMessage()]


def test_janitor_runs_learning_retention_on_the_one_slot_cold_lane() -> None:
    news = RecordingNews(
        purge_learning_retention={
            "deleted_recordings": 2,
            "deleted_cases": 1,
            "deleted_artifacts": 0,
        }
    )
    db = FakeWorkerDatabase(news)

    asyncio.run(JanitorLoop(db=db, cold_db=db.cold_port).turn())

    assert db.heavy_operations == ["news_janitor"]
    assert db.operations == []
    assert news.kwargs_of("purge_learning_retention") == {"batch_size": 500}
    assert "expire_bands" in news.names() and "purge_before" in news.names()


def test_janitor_records_retention_failure_without_stopping_the_loop() -> None:
    def _fail(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("broken retention function")

    news = RecordingNews(purge_learning_retention=_fail)
    db = FakeWorkerDatabase(news)

    asyncio.run(JanitorLoop(db=db, cold_db=db.cold_port).turn())

    assert db.heavy_operations == ["news_janitor", "news_learning_retention_error"]
    error = news.kwargs_of("record_learning_retention_error")
    assert error["error_code"] == "learning_retention_failed:RuntimeError"


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


def test_triage_runs_exactly_the_persisted_canary_arm_and_traces_the_assignment() -> None:
    stable_judge = _ScriptedSemanticJudge([_model_verdict(headline_zh="稳定版不应被调用")])
    candidate_judge = _ScriptedSemanticJudge([_model_verdict(headline_zh="候选版真实输出")])
    stable_bundle = "a" * 64
    candidate_bundle = "b" * 64
    activation_id = "c" * 32
    news = RecordingNews(
        get_verdict=None,
        event_card=_card(queue_priority="normal"),
        insert_verdict=True,
        assign_agent_arm={
            "activation_id": activation_id,
            "arm": "candidate",
            "bundle_sha": candidate_bundle,
            "selector_version": "news_canary_selector_v2",
            "eligibility_reason": "eligible_bucket",
        },
        evaluate_canary_rolling_slo={"evaluated": True, "tripped": False},
    )
    triage = TriageConsumer(
        bus=FakeBus(),
        db=FakeWorkerDatabase(news),
        judge=stable_judge,
        program_version=PROGRAM_VERSION,
        program_sha256=PROGRAM_SHA256,
        watchlist_symbols=WATCHLIST,
        watchlist=sorted(WATCHLIST),
        concurrency=1,
        circuit_failures=3,
        circuit_open_seconds=60.0,
        stable_bundle_sha=stable_bundle,
        runtime_manifest={"manifest_sha": "e" * 64},
        canary_arms={
            candidate_bundle: CanaryRuntimeArm(
                bundle_sha=candidate_bundle,
                program=candidate_judge,
                policy=DEFAULT_POLICY,
                program_version=PROGRAM_VERSION,
                program_sha256=PROGRAM_SHA256,
            )
        },
    )

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    assert stable_judge.inputs == []
    assert len(candidate_judge.inputs) == 1
    inserted = news.kwargs_of("insert_verdict")
    assert inserted["program_version"] == PROGRAM_VERSION
    assert inserted["program_sha256"] == PROGRAM_SHA256
    assert inserted["verdict"]["headline_zh"] == "候选版真实输出"
    assert inserted["trace"]["agent_assignment"] == {
        "activation_id": activation_id,
        "arm": "candidate",
        "bundle_sha": candidate_bundle,
        "selector_version": "news_canary_selector_v2",
        "eligibility_reason": "eligible_bucket",
    }
    assert news.names().index("assign_agent_arm") < news.names().index("insert_verdict")
    assert "evaluate_canary_rolling_slo" in news.names()


def test_triage_fails_closed_when_a_persisted_stable_assignment_names_a_retired_bundle() -> None:
    current_bundle = "a" * 64
    retired_bundle = "b" * 64
    judge = _ScriptedSemanticJudge([_model_verdict(headline_zh="绝不能由新 Program 回答旧 assignment")])
    news = RecordingNews(
        get_verdict=None,
        event_card=_card(),
        insert_verdict=True,
        assign_agent_arm={
            "activation_id": None,
            "arm": "stable",
            "bundle_sha": retired_bundle,
            "selector_version": "stable_only_v2",
            "eligibility_reason": "no_active_canary",
        },
    )
    triage = TriageConsumer(
        bus=FakeBus(),
        db=FakeWorkerDatabase(news),
        judge=judge,
        program_version=PROGRAM_VERSION,
        program_sha256=PROGRAM_SHA256,
        watchlist_symbols=WATCHLIST,
        watchlist=sorted(WATCHLIST),
        concurrency=1,
        circuit_failures=3,
        circuit_open_seconds=60.0,
        stable_bundle_sha=current_bundle,
        runtime_manifest={"manifest_sha": "e" * 64},
    )

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    inserted = news.kwargs_of("insert_verdict")
    assert judge.inputs == []
    assert inserted["degraded"] is True
    assert inserted["error_code"] == "news_semantic_program_identity_mismatch"
    assert inserted["trace"]["agent_assignment"]["bundle_sha"] == retired_bundle


def _ledger_row(
    event_id: str,
    at_ms: int,
    *,
    key: str = "asset:NVDA",
    headline: str = "英伟达投资 OpenAI",
    grounded_assets: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "at_ms": at_ms,
        "storyline_key": key,
        "magnitude": 2,
        "direction": "bullish",
        "headline_zh": headline,
        "grounded_assets": grounded_assets if grounded_assets is not None else [],
        "assets": [],
    }


def _recent_history(*rows: dict[str, Any], now_ms: int = NOW_MS) -> ReaderHistorySnapshot:
    return assemble_reader_history(recent_rows=rows, now_ms=now_ms)


def test_triage_told_rows_carry_the_instruments_so_the_listing_exemption_can_fire() -> None:
    """A listing frame whose closest told entry is about a *different* instrument still reaches the reader.

    ``decide()`` compares symbol sets, so the told rows it reads have to carry symbols.  The rows the
    handler builds come from ``ToldLedgerEntry``, which is deliberately free of anything the Program may
    not read — so the symbols have to be re-attached from the source ledger row.  Without that, every
    told row looks like "no instruments", ``_names_another_instrument`` reads that as "not evidence of a
    different instrument", and the policy-v8 listing exemption is inert in production while its own unit
    test passes because it supplies the symbols by hand.
    """

    ledger = [
        _ledger_row(
            "ev-doge", NOW_MS - 300_000, key="asset:DOGE", headline="Coinbase 将上线狗狗币", grounded_assets=["DOGE"]
        )
    ]
    news = RecordingNews(
        get_verdict=None,
        event_card=_card(
            admission="listing_deterministic",
            grounded_assets=["BICO"],
            watchlist_hits=[],
            storyline_key="asset:BICO",
            leader_title="Upbit will list BICO",
        ),
        insert_verdict=True,
        reader_history=_recent_history(*ledger),
    )
    bus = FakeBus()
    model = _ScriptedSemanticJudge(
        [
            _model_verdict(
                novelty="restatement",
                restates=0,
                event_type="listing",
                decision="push",
                magnitude=1,
                assets=[{"symbol": "BICO", "role": "primary"}],
                headline_zh="Upbit 将上线 BICO",
            )
        ]
    )
    triage = _triage_with_judge(news, bus, model)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "push"
    assert inserted["override_rule"] != "restatement"


def test_triage_reader_history_reaches_the_model_and_the_trace_and_grounds_a_restatement() -> None:
    """The told ledger (what the reader already received) is in the status bar and the trace; a restatement the
    model grounds in it drops with the restated card's event id recorded; the persist step locks the final key."""

    ledger = [_ledger_row("ev-earlier", NOW_MS - 300_000), _ledger_row("ev-other", NOW_MS - 900_000, key="asset:BTC")]
    news = RecordingNews(
        get_verdict=None,
        event_card=_card(),
        insert_verdict=True,
        reader_history=_recent_history(*ledger),
    )
    bus = FakeBus()
    model = _ScriptedSemanticJudge([_model_verdict(novelty="restatement", restates=0, decision="drop")])
    triage = _triage_with_judge(news, bus, model)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    assert len(model.inputs) == 1
    context = model.inputs[0]
    assert [entry.event_id for entry in context.told.entries] == ["ev-earlier", "ev-other"]
    assert context.told.entries[0].headline_zh == "英伟达投资 OpenAI"
    assert context.evidence.title == "NVIDIA to invest $100bn in OpenAI data centre"
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


def test_triage_shows_targeted_history_to_the_model_but_never_to_recent_seen_policy() -> None:
    old_exact = {
        **_ledger_row("ev-old-exact", NOW_MS - 24 * 3_600_000),
        "family": "general",
        "comparison_fingerprint": "f" * 64,
        "canonical_assets": ["NVDA"],
    }
    history = assemble_reader_history(recent_rows=(), exact_rows=(old_exact,), now_ms=NOW_MS)
    news = RecordingNews(
        get_verdict=None,
        event_card=_card(),
        insert_verdict=True,
        reader_history=history,
    )
    model = _ScriptedSemanticJudge([_model_verdict(novelty="restatement", restates=0, decision="drop")])
    triage = _triage_with_judge(news, FakeBus(), model)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    entry = model.inputs[0].told.entries[0]
    assert (entry.event_id, entry.history_scope, entry.retrieval_reason, entry.tier) == (
        "ev-old-exact",
        "targeted",
        "exact_fingerprint",
        "exact_fact",
    )
    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "drop" and inserted["override_rule"] == "restatement"
    assert inserted["trace"]["seen_count"] == 0
    assert inserted["trace"]["reader_history"] == {
        "recent_count": 0,
        "targeted_count": 1,
        "source_count": 1,
        "selected_count": 1,
        "selected_reasons": ["exact_fingerprint"],
    }
    history_calls = [kwargs for name, kwargs in news.calls if name == "reader_history"]
    assert history_calls and all(call["include_targeted"] is True for call in history_calls)


def test_triage_does_not_reask_when_an_unrelated_card_lands_but_the_selection_is_unchanged() -> None:
    """The old rule re-asked whenever any new event id appeared in the recent ledger. In the fixed production
    cohort that fired on 16% of judgments and each one paid for a second full two-Predictor execution.

    Staleness is a property of the evidence the model was shown about *this* candidate. A card that shares no
    storyline, no instrument and no fact lands in the recency filler and cannot turn this Event into a
    restatement of anything, so the first judgment stands. `decide()` still measures the card against the
    refreshed wide ledger, so the duplicate defence loses nothing.
    """

    ledger_calls = {"n": 0}
    unrelated = _ledger_row("ev-unrelated", NOW_MS - 1_000, key="theme:trade", headline="完全无关的新卡片")

    def reader_history(*, now_ms: int, **_: Any) -> ReaderHistorySnapshot:
        ledger_calls["n"] += 1
        related = [_ledger_row(f"ev-{i}", NOW_MS - (10 + i) * 1_000) for i in range(4)]
        filler = [_ledger_row(f"bg-{i}", NOW_MS - (30 + i) * 1_000, key="theme:trade") for i in range(6)]
        rows = [unrelated, *related, *filler] if ledger_calls["n"] > 1 else [*related, *filler]
        return _recent_history(*rows, now_ms=now_ms)

    news = RecordingNews(get_verdict=None, event_card=_card(), insert_verdict=True, reader_history=reader_history)
    model = _ScriptedSemanticJudge([_model_verdict(novelty="new_fact")])
    triage = _triage_with_judge(news, FakeBus(), model)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    assert len(model.inputs) == 1, "an unrelated card must not buy a second paid execution"
    trace = news.kwargs_of("insert_verdict")["trace"]
    assert "reasked_after_told_change" not in trace
    # The wide ledger decide() measured against was re-read and does contain the new card.
    assert trace["seen_count"] == 11
    assert len(trace["selected_context_sha256"]) == 64 and len(trace["novelty_context_sha256"]) == 64


def test_triage_does_not_reask_when_only_the_source_row_order_changed() -> None:
    ledger_calls = {"n": 0}
    rows = [_ledger_row(f"ev-{i}", NOW_MS - (10 + i) * 1_000) for i in range(3)]

    def reader_history(*, now_ms: int, **_: Any) -> ReaderHistorySnapshot:
        ledger_calls["n"] += 1
        source = rows if ledger_calls["n"] == 1 else list(reversed(rows))
        return _recent_history(*source, now_ms=now_ms)

    news = RecordingNews(get_verdict=None, event_card=_card(), insert_verdict=True, reader_history=reader_history)
    model = _ScriptedSemanticJudge([_model_verdict(novelty="new_fact")])
    triage = _triage_with_judge(news, FakeBus(), model)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    assert len(model.inputs) == 1
    assert "reasked_after_told_change" not in news.kwargs_of("insert_verdict")["trace"]


def test_triage_reasks_once_when_a_card_landed_while_the_model_was_thinking() -> None:
    """A push committed between the ledger snapshot and the persist step means the model judged novelty against a
    stale ledger: the consumer asks once more with the fresh ledger and persists that verdict."""

    fresh_push = _ledger_row("ev-just-pushed", NOW_MS - 1_000)
    ledger_calls = {"n": 0}

    def reader_history(*, now_ms: int, **_: Any) -> ReaderHistorySnapshot:
        ledger_calls["n"] += 1
        # First read (load): nothing yet. Every later read (in-lock check, reload) sees the new card.
        if ledger_calls["n"] == 1:
            return ReaderHistorySnapshot()
        return _recent_history(fresh_push, now_ms=now_ms)

    news = RecordingNews(
        get_verdict=None,
        event_card=_card(),
        insert_verdict=True,
        reader_history=reader_history,
    )
    bus = FakeBus()
    first_verdict = _model_verdict(novelty="new_fact")
    reask_verdict = _model_verdict(novelty="restatement", restates=0, decision="drop")
    first_trace = _program_trace(
        context_sha256="a" * 64,
        verdict_sha256=canonical_sha(first_verdict.model_dump(mode="json")),
        calls=(
            _program_call(
                predictor="event_semantics",
                marker="1",
                input_tokens=5,
                output_tokens=2,
                cached_tokens=1,
                provider_cost_microusd=11,
            ),
            _program_call(
                predictor="reader_card",
                marker="2",
                input_tokens=7,
                output_tokens=3,
                cached_tokens=0,
                provider_cost_microusd=13,
            ),
        ),
    )
    reask_trace = _program_trace(
        context_sha256="b" * 64,
        verdict_sha256=canonical_sha(reask_verdict.model_dump(mode="json")),
        calls=(
            _program_call(
                predictor="event_semantics",
                marker="3",
                input_tokens=11,
                output_tokens=4,
                cached_tokens=2,
                provider_cost_microusd=17,
            ),
            _program_call(
                predictor="reader_card",
                marker="4",
                input_tokens=13,
                output_tokens=5,
                cached_tokens=1,
                provider_cost_microusd=19,
            ),
        ),
    )
    model = _ScriptedSemanticJudge(
        [
            _judgment(
                first_verdict,
                trace=first_trace,
                usage=ProgramUsage(
                    wall_latency_ms=20,
                    call_count=2,
                    physical_call_count=2,
                    input_tokens=12,
                    output_tokens=5,
                    cached_tokens=1,
                    total_tokens=17,
                    provider_cost_microusd=24,
                ),
            ),  # judged against the empty ledger
            _judgment(
                reask_verdict,
                trace=reask_trace,
                usage=ProgramUsage(
                    wall_latency_ms=30,
                    call_count=2,
                    physical_call_count=2,
                    input_tokens=24,
                    output_tokens=9,
                    cached_tokens=3,
                    total_tokens=33,
                    provider_cost_microusd=36,
                ),
            ),  # sees ev-just-pushed
        ]
    )
    triage = _triage_with_judge(news, bus, model)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    assert len(model.inputs) == 2 and model.inputs[0].told.entries == ()
    assert model.inputs[1].told.entries[0].event_id == "ev-just-pushed"
    assert model.inputs[1].told.entries[0].headline_zh == "英伟达投资 OpenAI"
    inserted = [kwargs for name, kwargs in news.calls if name == "insert_verdict"]
    assert len(inserted) == 1  # the stale round wrote nothing
    row = inserted[0]
    assert row["final_decision"] == "drop" and row["override_rule"] == "restatement"
    trace = row["trace"]
    assert trace["reasked_after_told_change"] is True
    assert trace["first_judgment"]["verdict"]["novelty"] == "new_fact"
    assert trace["first_judgment"]["verdict"]["decision"] == "push"
    assert trace["first_judgment"]["editorial"]["relevance"]["reader_value"] == "realtime"
    assert trace["first_input_sha256"] == first_trace.context_sha256
    assert trace["told_count"] == 1 and trace["restates_event_id"] == "ev-just-pushed"
    # The verdict-owning trace is the re-ask trace; the stale trace remains a
    # separate execution instead of having its calls spliced under this context.
    assert row["verdict"] == reask_verdict.model_dump(mode="json")
    assert trace["program_execution_index"] == 1
    assert trace["program_trace"]["context_sha256"] == reask_trace.context_sha256
    assert trace["program_trace"]["verdict_sha256"] == canonical_sha(row["verdict"])
    executions = trace["program_executions"]
    assert [execution["status"] for execution in executions] == ["superseded_stale_ledger", "accepted"]
    assert [execution["context_sha256"] for execution in executions] == [
        first_trace.context_sha256,
        reask_trace.context_sha256,
    ]
    assert [execution["recording_call_indices"] for execution in executions] == [[0, 1], [2, 3]]
    assert sum(len(execution["trace"]["calls"]) for execution in executions) == 4
    assert {
        field: trace[field]
        for field in (
            "latency_ms",
            "model_attempts",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "total_tokens",
            "provider_cost_microusd",
        )
    } == {
        "latency_ms": 50,
        "model_attempts": 4,
        "input_tokens": 36,
        "output_tokens": 14,
        "cached_tokens": 4,
        "total_tokens": 50,
        "provider_cost_microusd": 60,
    }
    assert news.names().count("lock_storyline") == 2
    # The re-ask reloads everything the model and decide() look at: card, sent ledger, and control state.
    assert news.names().count("event_card") == 2
    assert bus.published == []


def test_triage_rebuilds_gate_facts_when_evidence_changes_before_the_reask() -> None:
    initial = _card()
    refreshed = _card(
        evidence_version=2,
        evidence_sha256="f" * 64,
        focus_fact_id="fact-2",
        grounded_assets=[],
        provider_score_max=10.0,
        queue_priority="normal",
        storyline_key="macro:general",
    )
    cards = iter((initial, refreshed))
    verdict = _model_verdict(decision="drop", magnitude=1, actionable=False)
    judge = _ScriptedSemanticJudge([verdict, verdict])
    news = RecordingNews(
        get_verdict=None,
        event_card=lambda _event_id: next(cards),
        latest_evidence_snapshot={
            "evidence_version": 2,
            "evidence_sha256": "f" * 64,
            "focus_fact_id": "fact-2",
        },
        insert_verdict=True,
    )
    triage = _triage_with_judge(news, FakeBus(), judge)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    inserted = news.kwargs_of("insert_verdict")
    assert len(judge.inputs) == 2
    assert judge.inputs[0].gate.grounded_assets == ("NVDA",)
    assert judge.inputs[1].gate.grounded_assets == ()
    assert inserted["rule_baseline_decision"] == "drop"
    assert inserted["final_decision"] == "drop"
    assert inserted["trace"]["storyline_key_preliminary"] == "macro:general"
    assert inserted["trace"]["first_storyline_key_preliminary"] == "asset:NVDA"
    assert inserted["trace"]["reask_reason"] == "evidence"
    assert inserted["trace"]["reasked_after_evidence_change"] is True


def test_triage_evidence_reask_failure_degrades_against_the_refreshed_evidence() -> None:
    """A v1 judgment must never be persisted under the identity or Gate facts of v2 evidence."""

    initial = _card()
    refreshed = _card(
        leader_title="Second evidence no longer names a grounded asset",
        evidence_version=2,
        evidence_sha256="f" * 64,
        focus_fact_id="fact-2",
        grounded_assets=[],
        watchlist_hits=[],
        provider_score_max=10.0,
        queue_priority="normal",
        storyline_key="macro:general",
    )
    cards = iter((initial, refreshed))
    first_verdict = _model_verdict(magnitude=3, direction="bearish", scope="macro")

    class _EvidenceReaskFails:
        def __init__(self) -> None:
            self.inputs: list[TriageContext] = []

        async def judge(self, context: TriageContext) -> SemanticJudgment:
            self.inputs.append(context)
            context_sha = canonical_sha(context.model_dump(mode="json"))
            if len(self.inputs) == 1:
                return _judgment(
                    first_verdict,
                    trace=_program_trace(
                        context_sha256=context_sha,
                        verdict_sha256=canonical_sha(first_verdict.model_dump(mode="json")),
                        calls=(
                            _program_call(
                                predictor="event_semantics",
                                marker="1",
                                input_tokens=5,
                                output_tokens=2,
                                cached_tokens=0,
                                provider_cost_microusd=11,
                            ),
                            _program_call(
                                predictor="reader_card",
                                marker="2",
                                input_tokens=7,
                                output_tokens=3,
                                cached_tokens=0,
                                provider_cost_microusd=13,
                            ),
                        ),
                    ),
                )
            failed_trace = _program_trace(
                context_sha256=context_sha,
                verdict_sha256=None,
                calls=(
                    _program_call(
                        predictor="event_semantics",
                        marker="3",
                        input_tokens=9,
                        output_tokens=4,
                        cached_tokens=0,
                        provider_cost_microusd=17,
                    ),
                ),
            )
            raise _program_error("news_program_timeout", retryable=True, partial_trace=failed_trace)

    judge = _EvidenceReaskFails()
    news = RecordingNews(
        get_verdict=None,
        event_card=lambda _event_id: next(cards),
        latest_evidence_snapshot={
            "evidence_version": 2,
            "evidence_sha256": "f" * 64,
            "focus_fact_id": "fact-2",
        },
        insert_verdict=True,
    )
    triage = _triage_with_judge(news, FakeBus(), judge)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    inserted = news.kwargs_of("insert_verdict")
    assert len(judge.inputs) == 2
    assert inserted["evidence_version"] == 2
    assert inserted["evidence_sha256"] == "f" * 64
    assert inserted["focus_fact_id"] == "fact-2"
    assert inserted["degraded"] is True
    assert inserted["error_code"] == "news_program_timeout"
    assert inserted["model"] is None
    assert inserted["rule_baseline_decision"] == "drop"
    assert inserted["final_decision"] == "drop"
    assert inserted["verdict"]["headline_zh"] == "Second evidence no longer names a grounded asset"
    trace = inserted["trace"]
    assert trace["reask_reason"] == "evidence"
    assert trace["reasked_after_evidence_change"] is True
    assert trace["reask_failed"] == "news_program_timeout"
    assert "program_execution_index" not in trace
    assert "program_trace" not in trace
    assert "input_sha256" not in trace
    executions = trace["program_executions"]
    assert [execution["status"] for execution in executions] == ["superseded_evidence_change", "failed"]
    assert [execution["context"]["evidence"]["evidence_version"] for execution in executions] == [1, 2]
    assert [execution["recording_call_indices"] for execution in executions] == [[0, 1], [2]]
    assert trace["model_attempts"] == 3
    assert trace["physical_model_attempts"] == 3


def test_triage_reask_failure_keeps_the_first_verdict_instead_of_the_rule_baseline() -> None:
    """If the re-ask itself fails, the model's first (valid) judgment is persisted, not a degraded fallback."""

    fresh_push = _ledger_row("ev-just-pushed", NOW_MS - 1_000)
    ledger_calls = {"n": 0}

    def reader_history(*, now_ms: int, **_: Any) -> ReaderHistorySnapshot:
        ledger_calls["n"] += 1
        return ReaderHistorySnapshot() if ledger_calls["n"] == 1 else _recent_history(fresh_push, now_ms=now_ms)

    news = RecordingNews(
        get_verdict=None,
        event_card=_card(queue_priority="normal", provider_score_max=70.0),
        insert_verdict=True,
        reader_history=reader_history,
    )
    bus = FakeBus()

    first_verdict = _model_verdict(novelty="new_fact", magnitude=3, direction="bearish", scope="macro")
    first_trace = _program_trace(
        context_sha256="a" * 64,
        verdict_sha256=canonical_sha(first_verdict.model_dump(mode="json")),
        calls=(
            _program_call(
                predictor="event_semantics",
                marker="1",
                input_tokens=5,
                output_tokens=2,
                cached_tokens=1,
                provider_cost_microusd=11,
            ),
            _program_call(
                predictor="reader_card",
                marker="2",
                input_tokens=7,
                output_tokens=3,
                cached_tokens=0,
                provider_cost_microusd=13,
            ),
        ),
    )
    failed_reask_trace = _program_trace(
        context_sha256="c" * 64,
        verdict_sha256=None,
        calls=(
            _program_call(
                predictor="event_semantics",
                marker="3",
                input_tokens=17,
                output_tokens=6,
                cached_tokens=2,
                provider_cost_microusd=23,
            ),
        ),
    )

    class _FirstOkThenTimeout(_ScriptedSemanticJudge):
        async def judge(self, context: TriageContext) -> SemanticJudgment:
            if self.inputs:
                self.inputs.append(context)
                raise _program_error(
                    "news_program_timeout",
                    retryable=True,
                    partial_trace=failed_reask_trace,
                )
            return await super().judge(context)

    model = _FirstOkThenTimeout(
        [
            _judgment(
                first_verdict,
                trace=first_trace,
                usage=ProgramUsage(
                    wall_latency_ms=20,
                    call_count=2,
                    physical_call_count=2,
                    input_tokens=12,
                    output_tokens=5,
                    cached_tokens=1,
                    total_tokens=17,
                    provider_cost_microusd=24,
                ),
            )
        ]
    )
    triage = _triage_with_judge(news, bus, model)

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    assert len(model.inputs) == 2
    inserted = news.kwargs_of("insert_verdict")
    assert inserted["degraded"] is False and inserted["error_code"] is None
    assert inserted["final_decision"] == "push" and inserted["verdict"]["magnitude"] == 3
    assert inserted["verdict"] == first_verdict.model_dump(mode="json")
    trace = inserted["trace"]
    assert trace["reask_failed"] == "news_program_timeout"
    assert trace["reasked_after_told_change"] is True
    assert trace["program_execution_index"] == 0
    assert trace["program_trace"]["context_sha256"] == first_trace.context_sha256
    assert trace["program_trace"]["verdict_sha256"] == canonical_sha(inserted["verdict"])
    executions = trace["program_executions"]
    assert [execution["status"] for execution in executions] == ["accepted_after_reask_failure", "failed"]
    assert executions[1]["context_sha256"] == failed_reask_trace.context_sha256
    assert executions[1]["error"]["code"] == "news_program_timeout"
    assert executions[1]["trace"]["verdict_sha256"] is None
    assert [execution["recording_call_indices"] for execution in executions] == [[0, 1], [2]]
    assert sum(len(execution["trace"]["calls"]) for execution in executions) == 3
    assert {
        field: trace[field]
        for field in (
            "latency_ms",
            "model_attempts",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "total_tokens",
            "provider_cost_microusd",
        )
    } == {
        "latency_ms": 27,
        "model_attempts": 3,
        "input_tokens": 29,
        "output_tokens": 11,
        "cached_tokens": 3,
        "total_tokens": 40,
        "provider_cost_microusd": 47,
    }
    assert bus.routing_keys() == [RK_VERDICT_PUSH]


def test_triage_degraded_fallback_is_not_blocked_by_prior_reader_volume() -> None:
    """A rule-baseline fallback follows its safety rule; no reader quota vetoes it."""

    news = RecordingNews(
        get_verdict=None,
        event_card=_card(),  # watchlist NVDA -> rule baseline pushes
        insert_verdict=True,
    )
    bus = FakeBus()
    triage = _triage(news, bus)  # model=None -> degraded fallback

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["degraded"] is True and inserted["final_decision"] == "push"
    assert inserted["throttled_by"] is None and inserted["override_rule"] == "degraded_watchlist_objective"
    assert bus.routing_keys() == [RK_VERDICT_PUSH]


def _duplicate_news(*, ledger_headline: str) -> RecordingNews:
    """One actually-sent card in the duplicate-evidence ledger."""

    return RecordingNews(
        get_verdict=None,
        event_card=_card(queue_priority="normal", provider_score_max=75.0),
        insert_verdict=True,
        reader_history=_recent_history(_ledger_row("ev-earlier", NOW_MS - 300_000, headline=ledger_headline)),
    )


def test_triage_sends_a_distinct_card_and_traces_the_duplicate_measurement() -> None:
    """Prior counts never block the card; the sent ledger is measured only for duplicate evidence."""

    news = _duplicate_news(ledger_headline="美联储纪要显示官员对通胀存在分歧")
    bus = FakeBus()
    triage = _triage_with_judge(news, bus, _ScriptedSemanticJudge([_model_verdict()]))

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "push" and inserted["override_rule"] == "watchlist_objective_guard"
    assert inserted["throttled_by"] is None
    assert bus.routing_keys() == [RK_VERDICT_PUSH]
    assert inserted["trace"]["seen_similarity"] < 0.25 and inserted["trace"]["seen_count"] == 1
    assert "restates_event_id" not in inserted["trace"]


def test_triage_withholds_a_card_the_reader_already_received() -> None:
    """The model calling its own event new buys nothing: the measurement against the reader's window decides."""

    news = _duplicate_news(ledger_headline="英伟达投资 OpenAI 数据中心")
    bus = FakeBus()
    triage = _triage_with_judge(news, bus, _ScriptedSemanticJudge([_model_verdict(novelty="progression")]))

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "throttled"
    assert inserted["throttled_by"] == "storyline:asset:NVDA:seen"
    assert bus.published == []
    assert inserted["trace"]["seen_similarity"] >= 0.25
    seen_against = inserted["trace"]["seen_against"]
    assert seen_against["event_id"] == "ev-earlier"
    assert seen_against["headline_zh"] == "英伟达投资 OpenAI 数据中心"
    assert seen_against["at_ms"] == NOW_MS - 300_000  # `news why` can say how long ago the reader got it
    assert inserted["trace"]["seen_scope"] == "all"


def test_triage_withholds_a_batch_duplicate_on_a_storyline_nobody_has_pushed_on() -> None:
    """The OKX batch is caught by duplicate evidence even though every asset key is fresh."""

    news = _duplicate_news(ledger_headline="Ciena Corporation（$CIENx）出现在 OKX")
    bus = FakeBus()
    triage = _triage_with_judge(
        news,
        bus,
        _ScriptedSemanticJudge([_model_verdict(headline_zh="KLA Corporation（$KLACx）出现在 OKX")]),
    )

    asyncio.run(triage.handle(_message("event", {"event_id": "ev-strong"})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "throttled"
    assert inserted["throttled_by"] == "storyline:asset:NVDA:seen"
    assert inserted["trace"]["seen_scope"] == "all"
    assert bus.published == []


# ---------------------------------------------------------------- Recovery
class _FailingStrategyList:
    """A history client whose Strategy list is momentarily unavailable — a 429, a timeout, a bad gateway."""

    def __init__(self) -> None:
        self.hits_calls = 0

    async def get_strategy_list(self, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    async def get_strategy_hits(self, **_kwargs: Any) -> dict[str, Any]:
        self.hits_calls += 1
        return {"success": True, "data": [], "page": 1, "limit": 100, "total": 0}


def test_recovery_leaves_incidents_pending_when_the_strategy_list_is_unavailable() -> None:
    """#126 made recovery ask the provider which Strategies exist, which introduced a way to lose a backlog.

    `complete_recovery` is terminal and `pending_recovery_incidents` only selects `pending`, so settling an
    incident `unavailable` throws its outage window away for good. One failed Strategy-list read must not do
    that — it has to raise and let `run()` retry.
    """

    news = RecordingNews(
        pending_recovery_incidents=[
            {
                "incident_id": 1,
                "cause_class": "socket_closed",
                "opened_at_ms": 1_000,
                "closed_at_ms": 2_000,
                "recovery_from_at_ms": None,
                "recovery_to_at_ms": None,
            }
        ]
    )
    client = _FailingStrategyList()
    recovery = RecoveryRunner(bus=FakeBus(), db=FakeWorkerDatabase(news), history_client=client)

    with pytest.raises(TransientError, match="opennews_strategy_list_unavailable"):
        asyncio.run(recovery._recover_pending())

    assert "complete_recovery" not in news.names()
    assert client.hits_calls == 0


def test_recovery_without_a_history_client_still_settles_unavailable() -> None:
    """An absent client is permanent, not transient: there is nothing to wait for."""

    news = RecordingNews(
        pending_recovery_incidents=[
            {
                "incident_id": 7,
                "cause_class": "socket_closed",
                "opened_at_ms": 1_000,
                "closed_at_ms": 2_000,
                "recovery_from_at_ms": None,
                "recovery_to_at_ms": None,
            }
        ]
    )
    recovery = RecoveryRunner(bus=FakeBus(), db=FakeWorkerDatabase(news), history_client=None)

    asyncio.run(recovery._recover_pending())

    assert news.kwargs_of("complete_recovery")["status"] == "unavailable"


# ---------------------------------------------------------------------------- OI telemetry lane (#137)
_OI_TITLE = "TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"


def _oi_card(**overrides: Any) -> dict[str, Any]:
    card = _card(
        event_id="ev-oi",
        event_kind="oi",
        leader_title=_OI_TITLE,
        admission="telemetry_deterministic",
        engine_type="market",
        family="market_telemetry",
        grounded_assets=[],
        watchlist_hits=[],
        queue_priority="normal",
        provider_score_max=None,
        storyline_key="macro:market_telemetry",
        provider_metadata={"source": "binance", "coins": [{"symbol": "TRUMP", "market_type": "cex"}]},
        opened_at_ms=NOW_MS,
    )
    card.update(overrides)
    return card


def test_telemetry_is_judged_without_a_model_and_settles_on_the_ordinary_path() -> None:
    """The whole design in one test: no judge call, no arm assignment, and the verdict lands in
    `news_verdicts` through `_decide_and_persist` like any other Event."""

    news = RecordingNews(
        get_verdict=None,
        event_card=_oi_card(),
        insert_verdict=True,
        count_recent_eligible_oi_signals=0,
        latest_evidence_snapshot={"evidence_version": 1, "evidence_sha256": "e" * 64, "focus_fact_id": "fact-1"},
    )
    bus = FakeBus()
    judge = _RecordingJudge()

    asyncio.run(_triage(news, bus, judge=judge).handle(_message("event", {"event_id": "ev-oi"})))

    assert judge.calls == 0, "a telemetry frame must never reach the model"
    assert "assign_agent_arm" not in news.names(), "no model call means no arm to assign"
    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "push" and inserted["override_rule"] == "telemetry_deterministic"
    assert inserted["program_version"] == "news_oi_signal_v1" and inserted["degraded"] is False
    assert inserted["verdict"]["event_type"] == "oi_spike"
    assert inserted["verdict"]["headline_zh"] == (
        "▲ TRUMP 持仓异动4.55%｜持仓3217万｜鲸鱼占比100.7%｜鲸鱼多头盈利80.2%｜4h内第1次"
    )
    assert inserted["verdict"]["why_zh"] == ""
    assert inserted["trace"]["verdict_sha256"] == canonical_sha(inserted["verdict"])
    history_calls = [kwargs for name, kwargs in news.calls if name == "reader_history"]
    assert history_calls and all(call["include_targeted"] is False for call in history_calls)
    # The rank ledger is written, and the card goes out on the one delivery lane there has ever been.
    ledger = news.kwargs_of("insert_oi_signal")
    assert ledger["symbol"] == "TRUMP" and ledger["rank_in_window"] == 1
    resolved = news.kwargs_of("resolve_unverified_source_contract")
    assert resolved["event_id"] == "ev-oi" and resolved["reason"] is None
    assert isinstance(resolved["now_ms"], int)
    assert bus.routing_keys() == [RK_VERDICT_PUSH]


def test_a_judged_telemetry_frame_records_the_asset_its_gate_could_not_ground() -> None:
    """#267. The parser resolves the symbol the admission Gate could not read out of the wire text.

    Without this the Event has no `news_event_assets` row at all, and everything keyed on that table
    is blind to the entire lane: the Reaction planner never plants a row, so 价格/1H/4H are empty on
    every frame; `?symbol=` finds nothing, so the token page a frame links to lists neither it nor any
    other; and the instrument funnel counts the frame as naming nothing.
    """

    news = RecordingNews(
        get_verdict=None,
        event_card=_oi_card(),
        insert_verdict=True,
        count_recent_eligible_oi_signals=0,
        latest_evidence_snapshot={"evidence_version": 1, "evidence_sha256": "e" * 64, "focus_fact_id": "fact-1"},
    )

    asyncio.run(_triage(news, FakeBus()).handle(_message("event", {"event_id": "ev-oi"})))

    recorded = news.kwargs_of("record_event_assets")
    assert recorded["event_id"] == "ev-oi"
    # The judge's own primary, with the contract it named — not the Gate's empty `grounded_assets`.
    assert recorded["assets"] == [("TRUMP", "perp")]


def test_a_telemetry_frame_that_matched_no_template_records_no_asset() -> None:
    """A parse failure names no symbol, and inventing one would seed a price measurement on nothing."""

    news = RecordingNews(
        get_verdict=None,
        event_card=_oi_card(leader_title="Zeta posted something that is not the OI template"),
        insert_verdict=True,
        latest_evidence_snapshot={"evidence_version": 1, "evidence_sha256": "e" * 64, "focus_fact_id": "fact-1"},
    )

    asyncio.run(_triage(news, FakeBus()).handle(_message("event", {"event_id": "ev-oi"})))

    assert news.kwargs_of("insert_verdict")["error_code"] == "oi_parse_failed"
    assert news.kwargs_of("resolve_unverified_source_contract")["reason"] == "source_contract_drift"
    assert "record_event_assets" not in news.names()
    assert "insert_oi_signal" not in news.names()


def test_telemetry_beyond_the_window_rank_preserves_the_arithmetic_hold() -> None:
    news = RecordingNews(
        get_verdict=None,
        event_card=_oi_card(),
        insert_verdict=True,
        count_recent_eligible_oi_signals=2,
        latest_evidence_snapshot={"evidence_version": 1, "evidence_sha256": "e" * 64, "focus_fact_id": "fact-1"},
    )
    bus = FakeBus()

    asyncio.run(_triage(news, bus).handle(_message("event", {"event_id": "ev-oi"})))

    inserted = news.kwargs_of("insert_verdict")
    assert inserted["final_decision"] == "drop" and inserted["override_rule"] == "telemetry_deterministic"
    assert news.kwargs_of("insert_oi_signal")["rank_in_window"] == 3
    assert bus.published == []


def test_a_redelivered_telemetry_verdict_republishes_through_the_existing_guard() -> None:
    """The idempotency this lane relies on is the one Triage already had: the verdict row is the
    durable decision, and an unpublished push is re-published on redelivery."""

    news = RecordingNews(
        get_verdict={"final_decision": "push", "published_at_ms": None},
        event_card=_oi_card(),
    )
    bus = FakeBus()

    asyncio.run(_triage(news, bus).handle(_message("event", {"event_id": "ev-oi"})))

    assert "insert_verdict" not in news.names()
    assert bus.routing_keys() == [RK_VERDICT_PUSH]


# ---------------------------------------------------------------- liquidation telemetry lane (#213)
def _liquidation_card(**overrides: Any) -> dict[str, Any]:
    card = _card(
        event_id="ev-liquidation",
        event_kind="liquidation",
        leader_item_id="item-liquidation",
        leader_title="SPCX Large Short Liquidation 202.71K at $137.01",
        admission="liquidation_deterministic",
        engine_type="market",
        family="market_telemetry",
        grounded_assets=[],
        watchlist_hits=[],
        queue_priority="normal",
        provider_score_max=None,
        storyline_key="macro:market_telemetry",
        provider_metadata={"source": "binance", "coins": []},
        opened_at_ms=NOW_MS,
    )
    card.update(overrides)
    return card


def test_liquidation_is_judged_from_the_typed_fact_with_zero_model_calls() -> None:
    news = RecordingNews(
        # A historical generic-v10 row must not short-circuit the dedicated
        # liquidation policy selected from the durable admission.
        get_verdict=lambda **kwargs: (
            {"final_decision": "push", "published_at_ms": None}
            if kwargs["policy_version"] == TRIAGE_POLICY_VERSION
            else None
        ),
        event_card=_liquidation_card(),
        insert_verdict=True,
        market_liquidation={
            "source_key": "a" * 64,
            "item_id": "item-liquidation",
            "fact_id": "fact-1",
            "symbol": "SPCX",
            "venue": "binance",
            "liquidated_position_side": "short",
            "forced_order_side": "buy",
            "notional_usd": Decimal("202710"),
            "quantity": None,
            "price": Decimal("137.01"),
            "event_at_ms": NOW_MS - 1_000,
            "received_at_ms": NOW_MS,
            "parser_version": "liquidation_parser_v1",
            "provider_record_identity": "provider-1",
            "symbol_contract_identity": "unresolved:binance:SPCX",
            "position_side_semantics": "short=>forced_buy;long=>forced_sell",
            "quantity_semantics": "not_provided",
            "notional_semantics": "provider_reported_usd_notional",
            "price_semantics": "provider_reported_unspecified_price",
            "completeness_assumption": "selected_events_without_heartbeat",
            "throttle_assumption": "provider_throttle_unknown",
            "source_contract_version": "opennews_liquidation_source_v1",
            "source_contract_complete": False,
        },
        latest_evidence_snapshot={"evidence_version": 1, "evidence_sha256": "e" * 64, "focus_fact_id": "fact-1"},
    )
    bus = FakeBus()
    judge = _RecordingJudge()

    asyncio.run(_triage(news, bus, judge=judge).handle(_message("event", {"event_id": "ev-liquidation"})))

    assert judge.calls == 0
    assert "assign_agent_arm" not in news.names()
    assert news.kwargs_of("get_verdict")["policy_version"] == "news_liquidation_policy_v1"
    inserted = news.kwargs_of("insert_verdict")
    assert inserted["program_version"] == "news_liquidation_fact_v1"
    assert inserted["policy_version"] == "news_liquidation_policy_v1"
    assert inserted["degraded"] is False
    assert inserted["verdict"]["event_type"] == "liquidation"
    assert inserted["verdict"]["direction"] == "neutral"
    assert inserted["verdict"]["actionable"] is False
    assert inserted["trace"]["liquidation"]["forced_order_side"] == "buy"
    assert inserted["trace"]["gate_policy_version"] == "news_liquidation_admission_v1"
    assert inserted["trace"]["liquidation"]["source_contract"]["complete"] is False
    assert news.kwargs_of("resolve_unverified_source_contract")["reason"] is None
    assert bus.routing_keys() == [RK_VERDICT_PUSH]


def test_liquidation_missing_typed_fact_fails_closed_without_a_model_call() -> None:
    news = RecordingNews(
        get_verdict=None,
        event_card=_liquidation_card(),
        insert_verdict=True,
        market_liquidation=None,
        latest_evidence_snapshot={"evidence_version": 1, "evidence_sha256": "e" * 64, "focus_fact_id": "fact-1"},
    )
    judge = _RecordingJudge()

    asyncio.run(_triage(news, FakeBus(), judge=judge).handle(_message("event", {"event_id": "ev-liquidation"})))

    inserted = news.kwargs_of("insert_verdict")
    assert judge.calls == 0
    assert inserted["final_decision"] == "drop"
    assert inserted["override_rule"] == "liquidation_parse_failed"
    assert inserted["error_code"] == "liquidation_parse_failed"
    assert inserted["trace"]["liquidation"]["failure_stage"] == "source_contract_drift"
    assert news.kwargs_of("resolve_unverified_source_contract")["reason"] == "source_contract_drift"


def test_liquidation_redelivery_uses_its_independent_policy_identity() -> None:
    news = RecordingNews(
        get_verdict=lambda **kwargs: (
            {"final_decision": "push", "published_at_ms": None}
            if kwargs["policy_version"] == "news_liquidation_policy_v1"
            else None
        ),
        event_card=_liquidation_card(),
    )
    bus = FakeBus()

    asyncio.run(_triage(news, bus).handle(_message("event", {"event_id": "ev-liquidation"})))

    assert "insert_verdict" not in news.names()
    assert news.kwargs_of("mark_verdict_published")["policy_version"] == "news_liquidation_policy_v1"
    assert bus.routing_keys() == [RK_VERDICT_PUSH]
