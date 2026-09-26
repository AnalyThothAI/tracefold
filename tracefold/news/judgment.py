"""News judgment values and narrow backend port, without model-framework imports."""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import Field

from .event_update import (
    ActionPhase,
    ClaimMode,
    ClaimPolarity,
    ClaimRelationKind,
    CoverageKind,
    Digest,
    Exact,
    ImpactChannel,
    ReadAction,
    Ref,
)

TaskKind = Literal["claim_state", "claim_relation", "evidence_support", "coverage", "next_read", "impact_channel"]
TASK_KINDS: tuple[TaskKind, ...] = (
    "claim_state",
    "claim_relation",
    "evidence_support",
    "coverage",
    "next_read",
    "impact_channel",
)
TASK_VERSIONS: dict[TaskKind, str] = {kind: f"news_{kind}_v1" for kind in TASK_KINDS}
BATCH_ITEMS = 8
NATIVE_PHASE_SECONDS = 2.0


class ClaimStateAnswer(Exact):
    mode: ClaimMode
    phase: ActionPhase
    polarity: ClaimPolarity


class ClaimRelationAnswer(Exact):
    relation: ClaimRelationKind
    cause: Literal["world_action", "source_correction", "evidence_change", "unknown"]
    scope_changed: bool


class EvidenceSupportAnswer(Exact):
    support: Literal["supports_statement", "supports_proposition", "refutes", "reports", "not_addressed", "unresolved"]


class CoverageAnswer(Exact):
    coverage: CoverageKind


class NextReadAnswer(Exact):
    action: ReadAction


class ImpactAnswer(Exact):
    channel: ImpactChannel


class JudgmentItem(Exact):
    item_id: Ref
    payload: dict[str, Any]
    evidence_refs: tuple[Ref, ...] = ()

    def frozen_input(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        if any(ref not in evidence for ref in self.evidence_refs):
            raise ValueError("news_judgment_evidence_reference_invalid")
        return {
            "item_id": self.item_id,
            "payload": self.payload,
            "evidence": {ref: evidence[ref] for ref in sorted(set(self.evidence_refs))},
        }


class JudgmentItemResult(Exact):
    item_id: Ref
    dependency_sha256: Digest
    task_kind: TaskKind
    task_version: str
    status: Literal["resolved", "unresolved", "unavailable"]
    value: dict[str, Any] | None
    backend: Literal["native", "generative"]
    probabilities: dict[str, dict[str, float]] = Field(default_factory=dict)
    # Noul has true-probability, not an independent provider confidence.
    true_probabilities: dict[str, float] = Field(default_factory=dict)
    confidences: dict[str, float] = Field(default_factory=dict)
    error_code: str | None = None


class JudgmentCall(Exact):
    backend: Literal["native", "generative"]
    task_kind: TaskKind
    task_version: str
    question_sha256: Digest
    input_sha256: Digest
    item_ids: tuple[Ref, ...]
    requested_model: str | None = None
    served_model: str | None = None
    provider_request_id: str | None = None
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_microusd: int | None = None
    latency_ms: int = Field(ge=0)
    error_code: str | None = None


class JudgmentBatchResult(Exact):
    results: tuple[JudgmentItemResult, ...]
    calls: tuple[JudgmentCall, ...] = ()


@dataclass(frozen=True, slots=True)
class JudgmentDeadline:
    total_at: float
    native_at: float

    @classmethod
    def start(cls, *, total_at: float, native_seconds: float = NATIVE_PHASE_SECONDS) -> JudgmentDeadline:
        if not math.isfinite(total_at) or not math.isfinite(native_seconds) or native_seconds <= 0:
            raise ValueError("news_judgment_deadline_invalid")
        return cls(total_at=total_at, native_at=min(total_at, time.monotonic() + native_seconds))

    def require_remaining(self) -> float:
        remaining = self.total_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("news_judgment_total_deadline")
        return remaining


class NewsJudgmentBackend(Protocol):
    async def judge_batch(
        self,
        *,
        task_kind: TaskKind,
        task_version: str,
        frozen_evidence: Mapping[str, Any],
        ordered_items: Sequence[JudgmentItem],
        deadline: JudgmentDeadline,
        cached: Mapping[str, JudgmentItemResult] | None = None,
        checkpoint: Callable[[JudgmentBatchResult], Awaitable[None]] | None = None,
    ) -> JudgmentBatchResult: ...


class JudgmentConfigurationError(ValueError):
    """An operator configuration/authentication problem, never a hidden fallback."""


class JudgmentOutputError(ValueError):
    """A provider answer that does not satisfy this batch's typed answer contract."""
