"""Shared App transaction for durable authenticated Trading commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tracefold.trading import PreparedOperatorIntent


@dataclass(frozen=True, slots=True)
class OperatorIntentReceipt:
    command_id: str
    seq: int
    disposition: Literal["awaiting_runtime"]
    reason: str | None = None


def persist_operator_intent(repo: Any, prepared: PreparedOperatorIntent) -> OperatorIntentReceipt:
    """Authenticate upstream, then append intent without interpreting Runtime state."""

    row = repo.append_operator_intent(prepared)
    value = prepared.value
    return OperatorIntentReceipt(command_id=value.command_id, seq=int(row[0]), disposition="awaiting_runtime")


__all__ = ["OperatorIntentReceipt", "persist_operator_intent"]
