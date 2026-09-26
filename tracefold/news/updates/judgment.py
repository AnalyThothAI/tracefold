"""Bounded task contracts. Content uncertainty is not a provider failure."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from pydantic import Field

from .contracts import Exact
from .identity import identity

Task = Literal["mode", "phase", "relation", "support", "coverage", "topic", "next_read", "impact_channel"]
QUESTION_VERSION = "news_questions_v1"
OPTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "mode": (
        ("observation", "The cited material reports an observation, measurement, or completed act."),
        ("decision", "A named actor made a specific decision, even if not implemented yet."),
        ("commitment", "A named actor commits to a specific future action; not evidence of execution."),
        ("conditional_threat", "A named actor threatens a specific action if stated conditions hold."),
        ("guidance", "A named institution or actor provides specific forward guidance."),
        ("forecast", "A forecast or expectation, not the observed outcome."),
        ("commentary", "Opinion or rhetoric without a specific action, measure, or new observation."),
        ("promotion", "Solicitation or promotional copy rather than a concrete event."),
        ("unknown", "The material does not establish the speech act."),
    ),
    "phase": tuple((value, desc) for value, desc in (
        ("proposed", "An action is under consideration."), ("announced", "The action has been announced."),
        ("ordered", "An operative instruction or order was issued."), ("effective", "Evidence states the measure is legally in effect."),
        ("executing", "Evidence states actual execution is ongoing."), ("completed", "Evidence states execution completed."),
        ("cancelled", "Evidence states a planned measure was cancelled."), ("not_applicable", "Not an action with a realization phase."),
        ("unknown", "The phase is not established. A date passing is not execution evidence."),
    )),
    "relation": (
        ("equivalent", "Same assertion, subject, polarity, period, quantities, conditions and realization; no additional fact."),
        ("adds_information", "Adds a condition, parameter or other fact without correcting the earlier report."),
        ("real_world_change", "Reports an actual new action or state change, including reversal; not a media correction."),
        ("corrects", "Explicitly corrects or retracts an earlier reported assertion."),
        ("conflicts", "Sources make incompatible claims; the material does not establish which is true."),
        ("unrelated", "Related topic or wording, but a different proposition."),
        ("unresolved", "Not enough evidence to determine the relationship."),
    ),
    "support": (
        ("supports", "This material supports this exact proposition within its stated attribution and conditions."),
        ("refutes", "This material directly disputes this proposition."),
        ("reports", "This material merely attributes or repeats the proposition; not independent confirmation."),
        ("not_addressed", "This material does not address this proposition."),
        ("unresolved", "The support relationship is not established."),
    ),
    "coverage": (
        ("full", "The actual delivered text contains the whole selected proposition, including its quantities, period, negation and conditions."),
        ("partial", "The actual delivered text covers only part of the selected proposition."),
        ("none", "The actual delivered text does not cover this proposition. Topic/story similarity is not coverage."),
        ("unresolved", "Coverage cannot be established from this text."),
    ),
    "next_read": (
        ("read", "Reading this supplied existing target may resolve the stated important gap."),
        ("no_useful_read", "No useful additional read is supported by the supplied gap/target."),
        ("unresolved", "The usefulness of this read is uncertain."),
    ),
    "impact_channel": (
        ("applicable", "The supplied mechanism is supported as a conditional implication by the cited claims."),
        ("not_applicable", "The proposed mechanism is not supported by these claims."),
        ("unresolved", "Applicability is not established."),
    ),
}


class Question(Exact):
    item_id: str
    # JSON-native, frozen when constructing the task. This is business input,
    # never raw provider HTTP fields or a model-generated tool/URL.
    payload_json: str


class Answer(Exact):
    item_id: str
    value: str | bool | None
    status: Literal["available", "unavailable"] = "available"
    backend: str
    probabilities: dict[str, float] | None = None
    error_code: str | None = None


class BatchResult(Exact):
    answers: tuple[Answer, ...]


class ProviderUnavailable(RuntimeError):
    """A specifically classified retryable provider error."""


class ConfigurationFault(RuntimeError):
    """A non-retryable provider/configuration problem; never silently change key."""


class ContractFault(ValueError):
    """The response cannot satisfy the declared task; not a no-news decision."""


@dataclass(slots=True)
class Budget:
    deadline: float
    native_deadline: float | None = None

    @classmethod
    def start(cls, seconds: float) -> Budget:
        if seconds <= 0:
            raise ValueError("news_deadline_invalid")
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("news_stage_deadline")
        return remaining

    def begin_native(self, seconds: float = 2.0) -> None:
        # Exactly once, after extraction; batches and rebases share this window.
        if self.native_deadline is None:
            self.native_deadline = min(self.deadline, time.monotonic() + seconds)


class JudgmentBackend(Protocol):
    identity: str

    async def judge(self, task: Task, items: tuple[Question, ...], *, timeout: float) -> BatchResult: ...


class JudgmentCache(Protocol):
    async def get(self, key: str) -> Answer | None: ...
    async def put(self, key: str, answer: Answer) -> None: ...


class NewsJudgments:
    """One backend selection, bounded independent batches, and one local fallback.

    A persistent cache is supplied by the existing News store. Successful batches
    survive worker retry and are never sent to a second model for voting.
    """
    def __init__(self, *, generated: JudgmentBackend, cache: JudgmentCache,
                 native: JudgmentBackend | None = None, batch_size: int = 8) -> None:
        if not 1 <= batch_size <= 32:
            raise ValueError("news_judgment_batch_size_invalid")
        self.generated, self.native, self.cache, self.batch_size = generated, native, cache, batch_size
        self.identity = identity("backend", QUESTION_VERSION, generated.identity, None if native is None else native.identity, batch_size)

    def _key(self, task: Task, item: Question) -> str:
        return identity("relation", self.identity, QUESTION_VERSION, task, item.item_id, item.payload_json)

    async def judge(self, task: Task, items: tuple[Question, ...], budget: Budget) -> tuple[Answer, ...]:
        if len({q.item_id for q in items}) != len(items):
            raise ContractFault("news_question_duplicate_identity")
        cached: dict[str, Answer] = {}
        missing: list[Question] = []
        for item in items:
            budget.remaining()
            value = await self.cache.get(self._key(task, item))
            if value is not None:
                cached[item.item_id] = value
            else:
                missing.append(item)
        for start in range(0, len(missing), self.batch_size):
            batch = tuple(missing[start:start + self.batch_size])
            result: BatchResult
            if self.native is None:
                result = await self._generated(task, batch, budget)
            else:
                budget.begin_native()
                native_remaining = min(budget.remaining(), (budget.native_deadline or 0) - time.monotonic())
                try:
                    if native_remaining <= 0:
                        raise ProviderUnavailable("news_native_budget_exhausted")
                    async with asyncio.timeout(native_remaining):
                        result = await self.native.judge(task, batch, timeout=native_remaining)
                except (ProviderUnavailable, TimeoutError):
                    # Total cancellation/deadline is checked before a local fallback.
                    # Authentication/configuration faults and programming defects propagate.
                    budget.remaining()
                    result = await self._generated(task, batch, budget)
            self._validate(task, batch, result)
            for answer in result.answers:
                cached[answer.item_id] = answer
                if answer.status == "available":
                    question = next(q for q in batch if q.item_id == answer.item_id)
                    await self.cache.put(self._key(task, question), answer)
        return tuple(cached[item.item_id] for item in items)

    async def _generated(self, task: Task, batch: tuple[Question, ...], budget: Budget) -> BatchResult:
        remaining = budget.remaining()
        try:
            async with asyncio.timeout(remaining):
                return await self.generated.judge(task, batch, timeout=remaining)
        except ProviderUnavailable as exc:
            return BatchResult(answers=tuple(Answer(item_id=q.item_id, value=None, status="unavailable", backend=self.generated.identity, error_code=str(exc)) for q in batch))

    @staticmethod
    def _validate(task: Task, items: tuple[Question, ...], result: BatchResult) -> None:
        if {a.item_id for a in result.answers} != {q.item_id for q in items} or len(result.answers) != len(items):
            raise ContractFault("news_judgment_missing_or_duplicate_answer")
        choices = {key for key, _ in OPTIONS.get(task, ())}
        for answer in result.answers:
            if answer.status == "unavailable":
                if answer.value is not None:
                    raise ContractFault("news_unavailable_answer_has_value")
            elif task == "topic":
                if type(answer.value) is not bool:
                    raise ContractFault("news_topic_boolean_required")
            elif answer.value not in choices:
                raise ContractFault("news_judgment_option_invalid")
