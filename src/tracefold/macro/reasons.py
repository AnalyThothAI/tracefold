from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MacroReasonImpact = Literal["none", "limited", "blocked"]
MacroReasonRecovery = Literal["none", "automatic", "next_session", "operator_action"]


class MacroReason(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=2_000)
    impact: MacroReasonImpact
    affected_dataset_ids: tuple[str, ...] = ()
    affected_claim_ids: tuple[str, ...] = ()
    retryable: bool
    recovery: MacroReasonRecovery
    next_action: str | None = Field(default=None, max_length=2_000)
    next_check_at_ms: int | None = Field(default=None, ge=0)


def macro_reason(
    *,
    code: str,
    message: str,
    impact: MacroReasonImpact,
    affected_dataset_ids: tuple[str, ...] = (),
    affected_claim_ids: tuple[str, ...] = (),
    retryable: bool,
    recovery: MacroReasonRecovery,
    next_action: str | None = None,
    next_check_at_ms: int | None = None,
) -> dict[str, object]:
    return MacroReason(
        code=code,
        message=message,
        impact=impact,
        affected_dataset_ids=affected_dataset_ids,
        affected_claim_ids=affected_claim_ids,
        retryable=retryable,
        recovery=recovery,
        next_action=next_action,
        next_check_at_ms=next_check_at_ms,
    ).model_dump(mode="json")


__all__ = [
    "MacroReason",
    "MacroReasonImpact",
    "MacroReasonRecovery",
    "macro_reason",
]
