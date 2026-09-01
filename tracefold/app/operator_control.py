"""Shared App transaction for durable authenticated Trading commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tracefold.trading import (
    ExecutionObservationV1,
    PreparedOperatorIntent,
    canonical_sha256,
    prepare_execution_observations,
)

_INGRESS_RELEASE = "workers-control-v1"
_INGRESS_STRATEGY = "operator-control-v1"


@dataclass(frozen=True, slots=True)
class OperatorIntentReceipt:
    command_id: str
    seq: int
    disposition: Literal["awaiting_runtime", "not_applied"]
    reason: str | None = None


def persist_operator_intent(repo: Any, prepared: PreparedOperatorIntent) -> OperatorIntentReceipt:
    """Append intent and, when no profile is active, its terminal disposition atomically."""

    row = repo.append_operator_intent(prepared)
    value = prepared.value
    if repo.execution_profile_activation(value.target_profile_id) is not None:
        return OperatorIntentReceipt(command_id=value.command_id, seq=int(row[0]), disposition="awaiting_runtime")
    observation = _inactive_profile_observation(prepared)
    repo.append_execution_observations(prepare_execution_observations((observation,)))
    return OperatorIntentReceipt(
        command_id=value.command_id,
        seq=int(row[0]),
        disposition="not_applied",
        reason="execution_profile_inactive",
    )


def _inactive_profile_observation(prepared: PreparedOperatorIntent) -> ExecutionObservationV1:
    value = prepared.value
    payload = {
        "contract": "operator-control-disposition-v1",
        "command_id": value.command_id,
        "disposition": "not_applied",
        "reason": "execution_profile_inactive",
    }
    return ExecutionObservationV1(
        event_id=canonical_sha256(payload),
        runtime_profile_id=value.target_profile_id,
        runtime_release=_INGRESS_RELEASE,
        execution_strategy=_INGRESS_STRATEGY,
        command_id=value.command_id,
        normalized_kind="control_disposition",
        occurred_at_ns=value.requested_at_ns,
        observed_at_ns=value.requested_at_ns,
        summary={"disposition": "not_applied", "reason": "execution_profile_inactive"},
        payload_digest=canonical_sha256({"disposition": "not_applied", "reason": "execution_profile_inactive"}),
    )


__all__ = ["OperatorIntentReceipt", "persist_operator_intent"]
