"""Deterministic Binance Demo execution-policy contract receipt (#429)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .adapter_contracts import (
    BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
)
from .bindings import BINDING_VENUE, ExecutionBindingV1
from .capabilities import ExecutionCapabilitySnapshotV2
from .contracts import canonical_sha256
from .execution_policy import (
    EXECUTION_POLICY_SHA256,
    EXECUTION_POLICY_VERSION,
    PROTECTION_CONTRACT_SHA256,
    PROTECTION_CONTRACT_VERSION,
)
from .intent import INTENT_POLICY_PAYLOAD, INTENT_POLICY_SHA256, TradeIntent
from .quote_authority import (
    QUOTE_CONTRACT_SHA256,
    QUOTE_CONTRACT_VERSION,
    SUBMISSION_FENCE_SHA256,
    SUBMISSION_FENCE_VERSION,
)


class ExecutionPolicyContractReceiptV4(BaseModel):
    """Content-addressed proof that research can bind the exact executable contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_version: Literal["execution_policy_contract_receipt_v4"] = "execution_policy_contract_receipt_v4"
    terminal: Literal["EXECUTION_POLICY_CONTRACT_V4_SEALED"] = "EXECUTION_POLICY_CONTRACT_V4_SEALED"
    execution_capability_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_binding_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trade_intent_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_policy_version: str
    execution_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote_contract_version: str
    quote_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    submission_fence_version: str
    submission_fence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_contract_version: str
    protection_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_contract_sha256: dict[str, str]
    source_native_routing: dict[str, str]
    economic_lifecycle_identity: Literal["economic_lifecycle_v1"] = "economic_lifecycle_v1"
    economic_leg_identity: Literal["economic_leg_v1"] = "economic_leg_v1"
    exact_execution_values: dict[str, object]
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt_identity(self) -> Self:
        if self.receipt_sha256 != canonical_sha256(self.model_dump(mode="json", exclude={"receipt_sha256"})):
            raise ValueError("execution_policy_contract_receipt_identity_invalid")
        return self


def build_execution_policy_contract_receipt() -> ExecutionPolicyContractReceiptV4:
    """Build the only current sealed contract receipt from code-owned values."""

    payload = {
        "receipt_version": "execution_policy_contract_receipt_v4",
        "terminal": "EXECUTION_POLICY_CONTRACT_V4_SEALED",
        "execution_capability_schema_sha256": canonical_sha256(ExecutionCapabilitySnapshotV2.model_json_schema()),
        "execution_binding_schema_sha256": canonical_sha256(ExecutionBindingV1.model_json_schema()),
        "trade_intent_schema_sha256": canonical_sha256(TradeIntent.model_json_schema()),
        "intent_policy_sha256": INTENT_POLICY_SHA256,
        "execution_policy_version": EXECUTION_POLICY_VERSION,
        "execution_policy_sha256": EXECUTION_POLICY_SHA256,
        "quote_contract_version": QUOTE_CONTRACT_VERSION,
        "quote_contract_sha256": QUOTE_CONTRACT_SHA256,
        "submission_fence_version": SUBMISSION_FENCE_VERSION,
        "submission_fence_sha256": SUBMISSION_FENCE_SHA256,
        "protection_contract_version": PROTECTION_CONTRACT_VERSION,
        "protection_contract_sha256": PROTECTION_CONTRACT_SHA256,
        "adapter_contract_sha256": {
            "BINANCE_USDM": BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
        },
        "source_native_routing": {binding: venue for binding, venue in BINDING_VENUE.items()},
        "economic_lifecycle_identity": "economic_lifecycle_v1",
        "economic_leg_identity": "economic_leg_v1",
        "exact_execution_values": INTENT_POLICY_PAYLOAD,
    }
    return ExecutionPolicyContractReceiptV4(
        **payload,
        receipt_sha256=canonical_sha256(payload),
    )


__all__ = [
    "ExecutionPolicyContractReceiptV4",
    "build_execution_policy_contract_receipt",
]
