"""Independent reader evidence through one native request with bounded generation fallback."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from functools import lru_cache
from math import isfinite
from typing import Any, Final

import dspy  # type: ignore[import-untyped]
from dspy.adapters.types.decision import Choice, Noul, Score  # type: ignore[import-untyped]
from pydantic import ValidationError

from ..notifications.reader import (
    ANCHOR_QUESTION,
    INTERRUPT_QUESTION,
    MATERIALITY_LEVELS,
    MATERIALITY_QUESTION,
    READER_INSTRUCTIONS,
    READER_MESSAGES_MAX,
    READER_QUESTIONS_IDENTITY,
    REPORT_KIND_OPTIONS,
    REPORT_KIND_QUESTION,
    AnchorEvidence,
    InterruptEvidence,
    MaterialityEvidence,
    ReaderBackend,
    ReaderInput,
    ReaderJudgment,
    ReportKindEvidence,
    anchor_options,
)
from ..updates.identity import identity
from ..updates.judgment import Budget, ContractFault, ProviderUnavailable, error_code
from . import generation
from .generation import ADAPTER_VERSION, native_predict

log = logging.getLogger("tracefold.news")


# A native request shares state for all questions and falls back inside the same stage deadline.

READER_NATIVE_SECONDS: Final = 3.0


@lru_cache(maxsize=READER_MESSAGES_MAX + 1)
def reader_signature(messages: int) -> Any:
    """Independent reader questions over one state, with an anchor only when messages were supplied.

    The same signature serves System One natively and the generative route through DSPy's decision
    adapter, so both backends answer exactly the same questions.
    """

    fields: dict[str, Any] = {
        "as_of": (str, dspy.InputField(desc="UTC date when this claim first became visible; fixed for this claim.")),
        "claim": (
            dict[str, Any],
            dspy.InputField(desc="One adopted news claim: its structured fields, topics and cited sources."),
        ),
    }
    if messages:
        fields["messages"] = (
            list[dict[str, str]],
            dspy.InputField(desc="Messages already pushed to this reader, each with its id."),
        )
    fields["report_kind"] = (Choice[REPORT_KIND_OPTIONS], dspy.OutputField(desc=REPORT_KIND_QUESTION))
    if messages:
        fields["anchor_message"] = (
            Choice[anchor_options(messages)],
            dspy.OutputField(desc=ANCHOR_QUESTION),
        )
    fields["materiality"] = (Score[MATERIALITY_LEVELS], dspy.OutputField(desc=MATERIALITY_QUESTION))
    fields["interrupt_now"] = (Noul, dspy.OutputField(desc=INTERRUPT_QUESTION))
    return dspy.Signature(fields, instructions=READER_INSTRUCTIONS)


def _normalized(values: Mapping[Any, float]) -> dict[Any, float]:
    if any(not isfinite(value) or value < 0 for value in values.values()):
        raise ContractFault("news_reader_distribution_invalid")
    total = sum(values.values())
    if not isfinite(total) or total <= 0:
        raise ContractFault("news_reader_distribution_empty")
    return {key: value / total for key, value in values.items()}


def _reader_evidence(
    prediction: Any, messages: int
) -> tuple[ReportKindEvidence, MaterialityEvidence, InterruptEvidence, AnchorEvidence | None]:
    kind = prediction.report_kind
    if kind.probabilities is None:
        raise ContractFault("news_reader_report_kind_distribution_missing")
    report_kind = ReportKindEvidence(
        value=kind.value,
        probabilities=_normalized(kind.probabilities),
        confidence=kind.confidence,
    )
    score = prediction.materiality
    if score.probabilities is None:
        raise ContractFault("news_reader_materiality_distribution_missing")
    levels = _normalized(score.probabilities)
    if set(levels) != set(range(len(MATERIALITY_LEVELS))):
        raise ContractFault("news_reader_materiality_distribution_invalid")
    materiality = MaterialityEvidence(
        value=score.value,
        probabilities=tuple(levels[index] for index in range(len(MATERIALITY_LEVELS))),
        confidence=score.confidence,
    )
    urgency = prediction.interrupt_now
    if urgency.probability is None:
        raise ContractFault("news_reader_interrupt_distribution_missing")
    interrupt = InterruptEvidence(
        probabilities=(1.0 - urgency.probability, urgency.probability), confidence=urgency.confidence
    )
    if not messages:
        return report_kind, materiality, interrupt, None
    choice = prediction.anchor_message
    if choice.probabilities is None:
        raise ContractFault("news_reader_anchor_distribution_missing")
    anchor = AnchorEvidence(
        probabilities={str(key): value for key, value in _normalized(choice.probabilities).items()},
        confidence=choice.confidence,
    )
    return report_kind, materiality, interrupt, anchor


class DspyReaderJudge:
    """The reader judgment of one claim: System One when configured, else the generative News route.

    One request asks all questions. A native answer that is unavailable falls back once to the generative
    route with the same signature; authentication/configuration faults propagate. When neither backend
    answers, the result is `unavailable` with a bounded code, which the planner waits on and never reuses.
    """

    def __init__(
        self,
        generated_lm_factory: Callable[[], Any],
        *,
        generated_model_identity: str,
        native_lm_factory: Callable[[], Any] | None = None,
        native_model_identity: str | None = None,
        native_operation_seconds: float = READER_NATIVE_SECONDS,
    ) -> None:
        if (native_lm_factory is None) != (native_model_identity is None):
            raise ValueError("news_reader_native_route_incomplete")
        if native_operation_seconds <= 0:
            raise ValueError("news_reader_native_seconds_invalid")
        self.generated_lm_factory = generated_lm_factory
        self.native_lm_factory = native_lm_factory
        self.native_operation_seconds = native_operation_seconds
        self.native_identity = (
            None
            if native_model_identity is None
            else identity("news_reader_native", READER_QUESTIONS_IDENTITY, native_model_identity, "dspy-3.4-native")
        )
        self.generated_identity = identity(
            "news_reader_generated", ADAPTER_VERSION, READER_QUESTIONS_IDENTITY, generated_model_identity
        )
        self.identity = identity("news_reader_judge", self.native_identity, self.generated_identity)

    async def judge(self, reader: ReaderInput, budget: Budget) -> ReaderJudgment:
        signature = reader_signature(len(reader.messages))
        inputs = reader.model_inputs()
        if self.native_lm_factory is not None:
            timeout = budget.operation(self.native_operation_seconds)
            lm = self.native_lm_factory()
            try:
                async with asyncio.timeout(timeout):
                    prediction = await native_predict(signature, lm, code="news_reader", **inputs)
                return self._available("native", prediction, reader, served_model=_served_model(lm))
            except (ProviderUnavailable, ContractFault, TimeoutError) as exc:
                log.warning("news_reader_native_unavailable", extra={"error_code": _fault_code(exc)})
        remaining = budget.remaining()
        try:
            async with asyncio.timeout(remaining):
                prediction = await generation.generate(signature, self.generated_lm_factory(), **inputs)
            return self._available("generated", prediction, reader, served_model=None)
        except (ProviderUnavailable, ContractFault, TimeoutError) as exc:
            return ReaderJudgment(status="unavailable", error_code=_fault_code(exc))

    def _available(
        self, backend: ReaderBackend, prediction: Any, reader: ReaderInput, *, served_model: str | None
    ) -> ReaderJudgment:
        try:
            report_kind, materiality, interrupt, anchor = _reader_evidence(prediction, len(reader.messages))
        except (AttributeError, ValidationError) as exc:
            raise ContractFault("news_reader_answer_invalid") from exc
        return ReaderJudgment(
            status="available",
            backend=backend,
            identity=self.native_identity if backend == "native" else self.generated_identity,
            served_model=served_model,
            report_kind=report_kind,
            materiality=materiality,
            interrupt=interrupt,
            anchor=anchor,
        )


def _fault_code(exc: BaseException) -> str:
    # The named faults carry code-owned codes; a timeout carries none.
    if isinstance(exc, (ProviderUnavailable, ContractFault)) and str(exc):
        return str(exc)
    return error_code(exc, default="news_reader_timeout")


def _served_model(lm: Any) -> str | None:
    history = getattr(lm, "history", None) or ()
    model = getattr(history[-1], "served_model", None) if history else None
    return None if model is None else str(model)
