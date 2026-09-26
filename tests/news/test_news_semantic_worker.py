"""The semantic stage without PostgreSQL or a provider: lease, retry policy, faults and the outage incident.

Every business outcome is a store call the worker makes; no test asserts a private call sequence.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, ClassVar

import dspy
import pytest

from tracefold.news.bus import Q_TRIAGE, BusMessage, PermanentError, TransientError
from tracefold.news.pipeline.semantic import PROVIDER_OUTAGE_CAUSE, SemanticWorker, error_code
from tracefold.news.storage.event_updates import SEMANTIC_ATTEMPTS_MAX, EventUpdateConflict, SemanticLease
from tracefold.news.updates import dspy_backend
from tracefold.news.updates.contracts import (
    Citation,
    ClaimFields,
    DraftClaim,
    Evidence,
    Extraction,
    FrozenInput,
    PriorClaim,
    Source,
)
from tracefold.news.updates.judgment import (
    Answer,
    BatchResult,
    Budget,
    ConfigurationFault,
    ContractFault,
    NewsJudgments,
    ProviderUnavailable,
    Question,
    Task,
)
from tracefold.news.updates.semantics import SemanticAnalyzer, assemble_update

NOW = 1_790_405_000_000


class Clock:
    def __init__(self) -> None:
        self.now_ms = NOW

    def __call__(self) -> int:
        return self.now_ms


class FakeStore:
    """The work lease the worker claims, and every settlement it writes."""

    def __init__(self, *leases: SemanticLease | None) -> None:
        self.leases = list(leases)
        self.claims: list[str] = []
        self.deferred: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []

    async def claim_semantic_work(self, event_id: str, *, lease_ms: int) -> SemanticLease | None:
        self.claims.append(event_id)
        return self.leases.pop(0) if self.leases else None

    async def defer_semantic_event(self, lease: SemanticLease, *, reason: str, retry_after_ms: int = 0) -> None:
        self.deferred.append((lease.event_id, reason))

    async def fail_semantic_event(self, lease: SemanticLease, *, error_code: str) -> None:
        self.failed.append((lease.event_id, error_code))


class FakeAgent:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, bool]] = []

    async def process(self, event_id: str, *, final_attempt: bool = True) -> str:
        self.calls.append((event_id, final_attempt))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)


class RecordingNews:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def open_incident(self, **kwargs: Any) -> None:
        self.calls.append(("open_incident", kwargs))

    def close_open_incidents(self, **kwargs: Any) -> None:
        self.calls.append(("close_open_incidents", kwargs))


class FakeDb:
    def __init__(self) -> None:
        self.news = RecordingNews()
        self.names: list[str] = []

    async def read(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        self.names.append(name)
        return fn(SimpleNamespace(news=self.news))

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        self.names.append(name)
        return fn(SimpleNamespace(news=self.news))


class FakeBus:
    def __init__(self) -> None:
        self.consumed: list[tuple[str, int]] = []

    async def consume(self, queue: str, handler: Any, *, prefetch: int, stop_event: Any) -> None:
        self.consumed.append((queue, prefetch))


def lease(event_id: str = "ev-1", *, revision: int = 1, attempts: int = 1) -> SemanticLease:
    return SemanticLease(
        event_id=event_id,
        wanted_revision=revision,
        lineage_id=f"lineage-{event_id}-{revision}",
        lease_token="token",
        attempts=attempts,
    )


def wake(event_id: str = "ev-1", revision: int = 1) -> BusMessage:
    return BusMessage(
        kind="event",
        message_id=f"event:{event_id}:{revision}",
        routing_key="event.general.normal",
        payload={"event_id": event_id, "revision": revision},
        trace_id="trace",
        occurred_at_ms=NOW,
    )


def worker(store: FakeStore, agent: FakeAgent, *, db: FakeDb | None = None, clock: Clock | None = None) -> Any:
    return SemanticWorker(
        bus=FakeBus(),
        db=db or FakeDb(),  # type: ignore[arg-type]
        store=store,
        agent=agent,
        concurrency=2,
        circuit_failures=2,
        circuit_open_seconds=60.0,
        program_identity="program-test",
        clock=clock or Clock(),
    )


def test_a_wake_with_nothing_due_runs_no_turn_and_a_wake_without_an_event_is_permanent() -> None:
    store, agent = FakeStore(None), FakeAgent()
    subject = worker(store, agent)

    asyncio.run(subject.handle(wake()))
    with pytest.raises(PermanentError, match="news_event_id_missing"):
        asyncio.run(subject.handle(BusMessage("event", "x", "event.general.normal", {}, "t", NOW)))

    assert store.claims == ["ev-1"] and agent.calls == []


def test_a_worker_without_a_semantic_runtime_acknowledges_wakes_and_leaves_work_pending() -> None:
    store = FakeStore(lease())
    subject = SemanticWorker(
        bus=FakeBus(),
        db=FakeDb(),  # type: ignore[arg-type]
        store=store,
        agent=None,
        concurrency=1,
        circuit_failures=2,
        circuit_open_seconds=60.0,
        program_identity=None,
    )

    asyncio.run(subject.handle(wake()))

    assert store.claims == [] and store.deferred == [] and store.failed == []


def test_a_provider_failure_defers_and_only_the_last_attempt_is_final() -> None:
    store = FakeStore(lease(attempts=1), lease(attempts=SEMANTIC_ATTEMPTS_MAX))
    agent = FakeAgent(ProviderUnavailable("news_judgment_http_429"), "adopted")
    subject = worker(store, agent)

    asyncio.run(subject.handle(wake()))
    asyncio.run(subject.handle(wake()))

    # Before the last attempt, an unavailable comparison is retried; the last attempt adopts it as
    # unresolved (`possible_new`) inside the agent.
    assert agent.calls == [("ev-1", False), ("ev-1", True)]
    assert store.deferred == [("ev-1", "news_judgment_http_429")]
    assert store.failed == []


def test_a_stage_deadline_is_a_provider_deferral_with_a_bounded_code() -> None:
    store = FakeStore(lease())
    subject = worker(store, FakeAgent(TimeoutError()))

    asyncio.run(subject.handle(wake()))

    assert store.deferred == [("ev-1", "news_provider_unavailable:TimeoutError")]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ContractFault("news_citation_not_in_frozen_source"), "news_citation_not_in_frozen_source"),
        (ConfigurationFault("news_judgment_http_401"), "news_judgment_http_401"),
        (EventUpdateConflict("news_semantic_observation_conflict"), "news_semantic_observation_conflict"),
        (LookupError("news_event_input_missing"), "news_event_input_missing"),
        (ValueError("free text a provider wrote"), "news_semantic_contract_fault:ValueError"),
    ],
)
def test_a_contract_fault_fails_the_revision_with_its_code_and_never_decides_no_news(
    error: Exception, code: str
) -> None:
    store = FakeStore(lease())
    subject = worker(store, FakeAgent(error))

    asyncio.run(subject.handle(wake()))

    assert store.failed == [("ev-1", code)]
    assert store.deferred == []


def test_an_unclassified_error_is_deferred_and_a_database_failure_is_the_brokers_retry() -> None:
    store = FakeStore(lease(), lease())
    subject = worker(store, FakeAgent(RuntimeError("bug"), TransientError("news_db_timeout")))

    asyncio.run(subject.handle(wake()))
    with pytest.raises(TransientError):
        asyncio.run(subject.handle(wake()))

    assert store.deferred == [("ev-1", "news_semantic_unexpected:RuntimeError")]
    assert store.failed == []


def test_revisions_that_arrive_during_a_turn_are_processed_by_the_same_wake() -> None:
    store = FakeStore(lease(revision=1), lease(revision=2), None)
    agent = FakeAgent("adopted", "unchanged")
    subject = worker(store, agent)

    asyncio.run(subject.handle(wake()))

    assert agent.calls == [("ev-1", False), ("ev-1", False)]
    assert store.claims == ["ev-1", "ev-1", "ev-1"]


def test_a_sustained_outage_opens_one_incident_pauses_claims_and_an_answer_closes_it() -> None:
    clock = Clock()
    db = FakeDb()
    store = FakeStore(lease(), lease(), lease())
    agent = FakeAgent(ProviderUnavailable("news_generation_LMServerError"), TimeoutError(), "adopted")
    subject = worker(store, agent, db=db, clock=clock)

    asyncio.run(subject.handle(wake()))
    asyncio.run(subject.handle(wake()))
    assert [name for name, _ in db.news.calls] == ["open_incident"]
    assert db.news.calls[0][1]["cause_class"] == PROVIDER_OUTAGE_CAUSE

    # While open, pending work stays durable and unclaimed; the repair turn wakes it later.
    asyncio.run(subject.handle(wake()))
    assert store.claims == ["ev-1", "ev-1"]

    clock.now_ms += 61_000
    asyncio.run(subject.handle(wake()))
    # The answered turn claims once more for a revision that arrived meanwhile; none is due.
    assert store.claims == ["ev-1", "ev-1", "ev-1", "ev-1"]
    assert [name for name, _ in db.news.calls] == ["open_incident", "close_open_incidents"]
    assert db.news.calls[1][1]["cause_classes"] == [PROVIDER_OUTAGE_CAUSE]


def test_the_worker_reconciles_the_incident_before_it_consumes_the_existing_queue() -> None:
    db = FakeDb()
    bus = FakeBus()
    subject = SemanticWorker(
        bus=bus,
        db=db,  # type: ignore[arg-type]
        store=FakeStore(),
        agent=FakeAgent(),
        concurrency=3,
        circuit_failures=2,
        circuit_open_seconds=60.0,
        program_identity="program-test",
    )

    asyncio.run(subject.run(stop_event=asyncio.Event()))

    assert db.names == ["news_semantic_incident_reconcile"]
    assert bus.consumed == [(Q_TRIAGE, 3)]


def test_error_codes_are_bounded_and_never_free_text() -> None:
    assert error_code(ContractFault("news_x:y.z"), default="d") == "news_x:y.z"
    assert error_code(ContractFault("Traceback: secret"), default="d") == "d:ContractFault"


# ---------------------------------------------------------------- analyzer retry policy (core)


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, Answer] = {}

    async def get(self, key: str) -> Answer | None:
        return self.values.get(key)

    async def put(self, key: str, answer: Answer) -> None:
        self.values.setdefault(key, answer)


class ScriptedBackend:
    """Relation answers fail until `recover()`; every source answer is `supports`."""

    identity = "scripted-generated"

    def __init__(self) -> None:
        self.failing = True
        self.calls: list[tuple[Task, int]] = []

    def recover(self) -> None:
        self.failing = False

    async def judge(self, task: Task, items: tuple[Question, ...], *, context_json: str | None) -> BatchResult:
        self.calls.append((task, len(items)))
        if task == "relation" and self.failing:
            raise ProviderUnavailable("news_generation_LMRateLimitError")
        value = "adds_information" if task == "relation" else "supports"
        return BatchResult(
            answers=tuple(Answer(item_id=item.item_id, value=value, backend=self.identity) for item in items)
        )


class UnusedExtractor:
    identity = "unused-extractor"

    async def extract(self, source: FrozenInput, *, extract_only: bool) -> Extraction:
        raise AssertionError("the analyzer is given the extraction")


def _evidence(text: str, revision: str) -> Evidence:
    return Evidence.issue(
        text, Source(publisher_id="wire", artifact_id="a-1", artifact_revision=revision, first_available_at_ms=NOW)
    )


def _draft(item: Evidence, slot: str = "a", *, conditions: tuple[str, ...] = ()) -> DraftClaim:
    return DraftClaim(
        slot=slot,
        statement=item.text,
        fields=ClaimFields(
            subject="Agency", action="orders tariff", mode="decision", phase="ordered", conditions=conditions
        ),
        citations=(Citation(evidence_ref=item.ref, quote=item.text),),
    )


def _input_with_prior() -> tuple[FrozenInput, Extraction, Any]:
    first = _evidence("Agency orders a 25% tariff.", "1")
    source = FrozenInput(event_id="ev-1", revision=1, lineage_id="l-1", evidence=(first,))
    head = assemble_update(source, Extraction(claims=(_draft(first),)), None, adopted_at_ms=NOW)
    assert head is not None
    later = _evidence("Agency orders a 25% tariff with a pharma exemption.", "2")
    prior = tuple(PriorClaim(event_id="ev-1", content_revision=head.content_revision, claim=c) for c in head.claims)
    return (
        FrozenInput(event_id="ev-1", revision=2, lineage_id="l-2", evidence=(later,), prior=prior),
        Extraction(claims=(_draft(later, conditions=("pharma exemption",)),)),
        head,
    )


def test_an_unavailable_relation_is_retried_before_the_last_attempt_and_possible_new_on_it() -> None:
    backend = ScriptedBackend()
    cache = MemoryCache()
    analyzer = SemanticAnalyzer(UnusedExtractor(), NewsJudgments(generated=backend, cache=cache))
    source, extraction, head = _input_with_prior()

    with pytest.raises(ProviderUnavailable, match="news_relation_unavailable"):
        asyncio.run(analyzer.understand(source, extraction, Budget.start(5.0), final_attempt=False))

    understood = asyncio.run(analyzer.understand(source, extraction, Budget.start(5.0), final_attempt=True))
    update = assemble_update(source, understood, head, adopted_at_ms=NOW + 1)
    assert update is not None
    assert [change.kind for change in update.changes] == ["possible_new"]


def test_a_retry_re_asks_only_the_answers_the_provider_could_not_give() -> None:
    backend = ScriptedBackend()
    cache = MemoryCache()
    analyzer = SemanticAnalyzer(UnusedExtractor(), NewsJudgments(generated=backend, cache=cache))
    source, extraction, head = _input_with_prior()

    with pytest.raises(ProviderUnavailable):
        asyncio.run(analyzer.understand(source, extraction, Budget.start(5.0), final_attempt=False))
    backend.recover()
    understood = asyncio.run(analyzer.understand(source, extraction, Budget.start(5.0), final_attempt=False))

    update = assemble_update(source, understood, head, adopted_at_ms=NOW + 1)
    assert update is not None and [change.kind for change in update.changes] == ["new_fact"]
    # The recovered relation was asked again; the source answer cached on the first attempt was not.
    assert backend.calls == [("relation", 1), ("relation", 1), ("support", 1)]


# ---------------------------------------------------------------- generative route fallback (core)


class _ScriptedPredict:
    """Stands in for `dspy.Predict`: one scripted result per LM of the route."""

    script: ClassVar[dict[str, Any]] = {}
    asked: ClassVar[list[str]] = []

    def __init__(self, signature: Any) -> None:
        del signature

    async def acall(self, *, lm: Any, **inputs: Any) -> Any:
        type(self).asked.append(lm)
        outcome = type(self).script[lm]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@contextmanager
def _scripted(monkeypatch: pytest.MonkeyPatch, script: dict[str, Any]):
    _ScriptedPredict.script = script
    _ScriptedPredict.asked = []
    monkeypatch.setattr(dspy_backend.dspy, "Predict", _ScriptedPredict)
    yield _ScriptedPredict.asked


def test_a_transient_primary_failure_asks_the_declared_fallback_once(monkeypatch: pytest.MonkeyPatch) -> None:
    with _scripted(monkeypatch, {"primary": dspy.LMRateLimitError("rate"), "fallback": "answer"}) as asked:
        result = asyncio.run(dspy_backend._generate(object(), ("primary", "fallback")))
    assert result == "answer" and asked == ["primary", "fallback"]


def test_a_route_that_fails_on_every_endpoint_is_provider_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    failures = {"primary": dspy.LMServerError("5xx"), "fallback": dspy.LMTimeoutError("slow")}
    with _scripted(monkeypatch, failures) as asked, pytest.raises(ProviderUnavailable, match="LMTimeoutError"):
        asyncio.run(dspy_backend._generate(object(), ("primary", "fallback")))
    assert asked == ["primary", "fallback"]


def test_a_parse_failure_is_a_contract_fault_and_never_a_second_vote(monkeypatch: pytest.MonkeyPatch) -> None:
    parse_failure = dspy.AdapterParseError("adapter", dspy.Signature("question -> answer"), "bad")
    script = {"primary": parse_failure, "fallback": "answer"}
    with _scripted(monkeypatch, script) as asked, pytest.raises(ContractFault):
        asyncio.run(dspy_backend._generate(object(), ("primary", "fallback")))
    assert asked == ["primary"]


def test_a_single_lm_is_a_route_of_one(monkeypatch: pytest.MonkeyPatch) -> None:
    with _scripted(monkeypatch, {"only": dspy.LMTransportError("down")}) as asked, pytest.raises(ProviderUnavailable):
        asyncio.run(dspy_backend._generate(object(), "only"))
    assert asked == ["only"]
