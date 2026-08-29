"""Post-delivery verification of one claimed News progression relationship."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator

from .models import ExactNewsModel, ProgressionReviewState

PROGRESSION_REVIEW_TIMEOUT_SECONDS = 12.0
PROGRESSION_REVIEW_REASON_MAX_CHARS = 60
_PROGRESSION_REASON_TERMINATORS = ("。", "！", "？", "…", ".", "!", "?")


def compact_progression_reason(value: object) -> str:
    """Normalize model prose into one bounded sentence suitable for nested delivery metadata."""

    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if len(text) > PROGRESSION_REVIEW_REASON_MAX_CHARS:
        return f"{text[: PROGRESSION_REVIEW_REASON_MAX_CHARS - 1].rstrip()}…"
    if text.endswith(_PROGRESSION_REASON_TERMINATORS):
        return text
    if len(text) == PROGRESSION_REVIEW_REASON_MAX_CHARS:
        return f"{text[:-1].rstrip()}…"
    return f"{text}。"


class ProgressionReview(ExactNewsModel):
    """One bounded, auditable answer about whether a prior card is the real parent story."""

    state: Literal["confirmed", "rejected", "unavailable"]
    candidate_i: int | None = Field(default=None, ge=0)
    candidate_headline_zh: str | None = Field(default=None, max_length=120)
    reason_zh: str = Field(min_length=1, max_length=PROGRESSION_REVIEW_REASON_MAX_CHARS)
    verifier_id: str = Field(min_length=1, max_length=120)

    @field_validator("reason_zh", mode="before")
    @classmethod
    def _compact_reason(cls, value: object) -> str:
        return compact_progression_reason(value)

    @model_validator(mode="after")
    def _confirmed_review_names_exactly_one_candidate(self) -> ProgressionReview:
        has_candidate = self.candidate_i is not None and bool(self.candidate_headline_zh)
        if (self.state == "confirmed") != has_candidate:
            raise ValueError("news_progression_review_candidate_contract_invalid")
        return self


@runtime_checkable
class ProgressionVerifier(Protocol):
    """Optional model-backed capability; delivery never waits for it before the initial send."""

    async def review(
        self,
        *,
        event: Mapping[str, Any],
        verdict: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any] | ProgressionReview: ...


__all__ = [
    "PROGRESSION_REVIEW_REASON_MAX_CHARS",
    "PROGRESSION_REVIEW_TIMEOUT_SECONDS",
    "ProgressionReview",
    "ProgressionReviewState",
    "ProgressionVerifier",
    "compact_progression_reason",
]
