"""Bounded task contracts. Content uncertainty is not a provider failure."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Coroutine, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from ..generation_capacity import GENERATION_CAPACITY_WAIT, generation_capacity_wait_timed_out
from .contracts import Exact
from .identity import identity

Task = Literal[
    "mode",
    "phase",
    "content_kind",
    "relation",
    "support",
    "next_read",
]
QUESTION_VERSION: Final = "news_questions_v3"
# The per-claim readings the native backend owns when it is configured.
CLAIM_READING_TASKS: Final[tuple[Task, ...]] = ("mode", "phase", "content_kind")

# The direct question each task asks about one item. Both backends receive it; the native backend puts it
# in each slot's question, the generated backend in its criteria.
TASK_QUESTIONS: Final[dict[Task, str]] = {
    "mode": "Which speech act does the cited material establish for this claim?",
    "phase": "Which realization phase does the cited material establish for this action?",
    "content_kind": "Which kind of new content does this claim state?",
    "relation": "How does the current claim relate to the previous claim?",
    "support": "How does this material relate to this exact claim?",
    "next_read": "Would reading this supplied target resolve the stated gap?",
}

OPTIONS: Final[dict[Task, tuple[tuple[str, str], ...]]] = {
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
    "phase": (
        ("proposed", "An action is under consideration."),
        ("announced", "The action has been announced."),
        ("ordered", "An operative instruction or order was issued."),
        ("effective", "Evidence states the measure is legally in effect."),
        ("executing", "Evidence states actual execution is ongoing."),
        ("completed", "Evidence states execution completed."),
        ("cancelled", "Evidence states a planned measure was cancelled."),
        ("not_applicable", "Not an action with a realization phase."),
        ("unknown", "The phase is not established. A date passing is not execution evidence."),
    ),
    # Adapted from the retired fact_kind seed. Statements, recaps and promotion are not content kinds: they
    # are the claim's mode or its relation to earlier claims.
    "content_kind": (
        (
            "state_change",
            "Something in the world is now in a different state: a product, feature, network, market or service "
            "went live, became available, was cancelled, delayed, taken down, recalled or repriced; a launch "
            "date, price, fee, commercial term or capacity commitment named by the party that owns it; a venue "
            "listing, delisting, suspending or admitting an instrument; an attack, strike, closure, outage or "
            "breach that happened; a deal signed, closed or terminated.",
        ),
        (
            "official_measure",
            "An authority took or ordered a measure: a rate decision, a sanction, a ban, an export restriction, "
            "a licence granted or revoked, a regulator or ministry directing something to be done or stopped. "
            "It is the authority's action, not its opinion.",
        ),
        (
            "new_quantity",
            "A figure about the subject's own activity reported for the first time at this value: earnings, "
            "guidance, an exact count of users, traders, volume, fees or capacity, an official statistic's "
            "release. A record in the subject's own activity is also new_quantity.",
        ),
        (
            "level_crossed",
            "A market price, rate or level moved. Choose this when the text names a threshold it crossed "
            "(above, below, reclaimed, broke, lost), and also for a bare price or percentage move that names "
            "no threshold, record or quantified flow.",
        ),
        (
            "period_record",
            "A market figure is the highest, lowest, largest or first of a named period, or of all time.",
        ),
        (
            "quantified_flow",
            "An amount moved, with a number: liquidated, deposited, withdrawn, transferred, net inflow or "
            "outflow, holdings raised or cut, a position opened or closed.",
        ),
        (
            "schedule",
            "A calendar item that has not happened yet: a meeting date, a data release date, a scheduled call.",
        ),
        (
            "other",
            "A concrete proposition none of the above describes, such as a lawsuit filed, a ruling or a settlement.",
        ),
    ),
    "relation": (
        (
            "equivalent",
            "Same assertion, subject, polarity, period, quantities, conditions and realization; no additional fact.",
        ),
        ("adds_information", "Adds a condition, parameter or other fact without correcting the earlier report."),
        (
            "real_world_change",
            "Reports an actual new action or state change, including reversal; not a media correction.",
        ),
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
    "next_read": (
        ("read", "Reading this supplied existing target may resolve the stated important gap."),
        ("no_useful_read", "No useful additional read is supported by the supplied gap/target."),
        ("unresolved", "The usefulness of this read is uncertain."),
    ),
}

# An operation timeout for one native request. It is an engineering budget, not a service SLA; each batch
# gets its own, and none of them extends the shared stage deadline.
NATIVE_OPERATION_SECONDS: Final = 2.0
# Packing budget for the tasks that are split into batches.
DEFAULT_BATCH_SIZE: Final = 8
MAX_BATCH_SIZE: Final = 32
MAX_QUESTIONS_PER_REQUEST: Final = 64
# Independent batches of one question set run at most this many at a time. The generated backend shares one
# local endpoint with every other semantic and notification turn, so the bound stays small.
PARALLEL_BATCHES: Final = 3


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


_ERROR_CODE = re.compile(r"^[a-z0-9_:.]{1,160}$")


def error_code(exc: BaseException, *, default: str) -> str:
    """Keep a bounded code; never persist arbitrary provider/library exception text."""
    if generation_capacity_wait_timed_out(exc):
        return GENERATION_CAPACITY_WAIT
    text = str(exc)
    return text if _ERROR_CODE.fullmatch(text) else f"{default}:{type(exc).__name__}"


@dataclass(frozen=True, slots=True)
class Budget:
    """One stage's shared wall-clock deadline.

    Operations take a bounded share of it; no batch, fallback or rebase resets it.
    """

    deadline: float

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

    def operation(self, limit: float) -> float:
        """A single operation's timeout: its own limit, never beyond the stage deadline."""

        return min(limit, self.remaining())


class JudgmentBackend(Protocol):
    identity: str

    async def judge(self, task: Task, items: tuple[Question, ...], *, context_json: str | None) -> BatchResult:
        """Answer one batch of independent items over an optional shared frozen context.

        The caller bounds the call with its own asyncio.timeout.
        """
        ...


class JudgmentCache(Protocol):
    """Answers by cache key. Each call is one statement: one read for a question set, one write per batch."""

    async def get_many(self, keys: tuple[str, ...]) -> dict[str, Answer]: ...

    async def put_many(self, answers: Mapping[str, Answer]) -> None: ...


class NewsJudgments:
    """One backend selection, bounded independent batches, and one local fallback.

    A persistent cache is supplied by the existing News store. Successful batches
    survive worker retry and are never sent to a second model for voting. A batch
    whose response cannot be used leaves only its own items unavailable.
    """

    def __init__(
        self,
        *,
        generated: JudgmentBackend,
        cache: JudgmentCache,
        native: JudgmentBackend | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        native_operation_seconds: float = NATIVE_OPERATION_SECONDS,
        parallel_batches: int = PARALLEL_BATCHES,
    ) -> None:
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError("news_judgment_batch_size_invalid")
        if native_operation_seconds <= 0:
            raise ValueError("news_native_operation_seconds_invalid")
        if parallel_batches < 1:
            raise ValueError("news_judgment_parallel_batches_invalid")
        self.generated = generated
        self.native = native
        self.cache = cache
        self.batch_size = batch_size
        self.native_operation_seconds = native_operation_seconds
        self.parallel_batches = parallel_batches
        native_identity = None if native is None else native.identity
        self.identity = identity("backend", QUESTION_VERSION, generated.identity, native_identity, batch_size)

    def batches(self, task: Task, items: tuple[Question, ...]) -> tuple[tuple[Question, ...], ...]:
        if not items:
            return ()
        return tuple(items[start : start + self.batch_size] for start in range(0, len(items), self.batch_size))

    async def judge(
        self,
        task: Task,
        items: tuple[Question, ...],
        budget: Budget,
        *,
        context_json: str | None = None,
    ) -> tuple[Answer, ...]:
        """Answer every item: natively per batch when configured, with that batch's one generated fallback."""

        return await self._answer(task, items, budget, context_json, reask=False)

    async def reask(
        self,
        task: Task,
        items: tuple[Question, ...],
        budget: Budget,
        *,
        context_json: str | None = None,
    ) -> tuple[Answer, ...]:
        """One targeted generated question for content that stayed uncertain.

        It is never a second native vote. Answers are cached apart from first answers, so a retried stage
        reuses the re-ask rather than repeating it.
        """

        return await self._answer(task, items, budget, context_json, reask=True)

    def _key(self, task: Task, item: Question, context_json: str | None, *, reask: bool) -> str:
        # A re-ask is answered by the generated backend alone, so only its identity keys the answer.
        owner = identity("reask", self.generated.identity) if reask else self.identity
        return identity("judgment", owner, QUESTION_VERSION, task, item.item_id, item.payload_json, context_json)

    async def _answer(
        self,
        task: Task,
        items: tuple[Question, ...],
        budget: Budget,
        context_json: str | None,
        *,
        reask: bool,
    ) -> tuple[Answer, ...]:
        if len({item.item_id for item in items}) != len(items):
            raise ContractFault("news_question_duplicate_identity")
        if not items:
            return ()
        keys = {item.item_id: self._key(task, item, context_json, reask=reask) for item in items}
        budget.remaining()
        cached = await self.cache.get_many(tuple(keys.values()))
        answers = {item.item_id: cached[keys[item.item_id]] for item in items if keys[item.item_id] in cached}
        missing = tuple(item for item in items if item.item_id not in answers)
        slots = asyncio.Semaphore(self.parallel_batches)

        async def ask(batch: tuple[Question, ...]) -> BatchResult:
            async with slots:
                result = await self._batch(task, batch, budget, context_json, reask=reask)
            fresh = {keys[answer.item_id]: answer for answer in result.answers if answer.status == "available"}
            if fresh:
                await self.cache.put_many(fresh)
            return result

        for result in await _all(ask(batch) for batch in self.batches(task, missing)):
            answers.update((answer.item_id, answer) for answer in result.answers)
        return tuple(answers[item.item_id] for item in items)

    async def _batch(
        self,
        task: Task,
        batch: tuple[Question, ...],
        budget: Budget,
        context_json: str | None,
        *,
        reask: bool,
    ) -> BatchResult:
        """One batch's answers. A response that cannot answer its own items leaves them unavailable.

        The stage deadline, cancellation and configuration faults still end the whole question set.
        """

        try:
            if reask or self.native is None:
                result = await self._generated(task, batch, budget, context_json)
            else:
                result = await self._native(self.native, task, batch, budget, context_json)
            return self._validate(task, batch, result)
        except ContractFault as exc:
            return _unavailable(batch, backend=self.generated.identity, code=str(exc))

    async def _native(
        self,
        native: JudgmentBackend,
        task: Task,
        batch: tuple[Question, ...],
        budget: Budget,
        context_json: str | None,
    ) -> BatchResult:
        # Each native batch has its own operation timeout inside the shared stage deadline. A stage that
        # has already expired raises here, before any request or fallback.
        timeout = budget.operation(self.native_operation_seconds)
        try:
            async with asyncio.timeout(timeout):
                return await native.judge(task, batch, context_json=context_json)
        except (ProviderUnavailable, TimeoutError):
            # Only this batch falls back, after the stage deadline is checked. Authentication/configuration
            # faults, cancellation and programming defects propagate.
            budget.remaining()
            return await self._generated(task, batch, budget, context_json)

    async def _generated(
        self,
        task: Task,
        batch: tuple[Question, ...],
        budget: Budget,
        context_json: str | None,
    ) -> BatchResult:
        try:
            async with asyncio.timeout(budget.remaining()):
                return await self.generated.judge(task, batch, context_json=context_json)
        except ProviderUnavailable as exc:
            return _unavailable(batch, backend=self.generated.identity, code=str(exc))

    @staticmethod
    def _validate(task: Task, items: tuple[Question, ...], result: BatchResult) -> BatchResult:
        """Every item answered once. An option label is compared after case and separator normalization;
        a label that names no option leaves only that item unavailable, never the batch or the revision."""

        answered = [answer.item_id for answer in result.answers]
        if len(answered) != len(items) or set(answered) != {item.item_id for item in items}:
            raise ContractFault("news_judgment_missing_or_duplicate_answer")
        choices = {value for value, _ in OPTIONS.get(task, ())}
        answers = []
        for answer in result.answers:
            if answer.status == "unavailable":
                if answer.value is not None:
                    raise ContractFault("news_unavailable_answer_has_value")
                answers.append(answer)
                continue
            value = _option_label(answer.value)
            if value in choices:
                answers.append(answer.model_copy(update={"value": value}))
            else:
                answers.append(
                    answer.model_copy(
                        update={"value": None, "status": "unavailable", "error_code": "news_judgment_option_invalid"}
                    )
                )
        return BatchResult(answers=tuple(answers))


def _option_label(value: object) -> str:
    return "_".join(str(value).strip().casefold().replace("-", " ").split())


def _unavailable(batch: tuple[Question, ...], *, backend: str, code: str) -> BatchResult:
    return BatchResult(
        answers=tuple(
            Answer(item_id=item.item_id, value=None, status="unavailable", backend=backend, error_code=code)
            for item in batch
        )
    )


async def _all(calls: Iterable[Coroutine[Any, Any, BatchResult]]) -> list[BatchResult]:
    """Run the batches together; the first stage-level fault cancels the rest and is raised as itself."""

    tasks = [asyncio.ensure_future(call) for call in calls]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
