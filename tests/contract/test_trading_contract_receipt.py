"""The #376 contract receipt is a checked, executable dependency for #377."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracefold.trading.contract_receipt import (
    ExecutionPolicyContractReceiptV3,
    build_execution_policy_contract_receipt,
)

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
RECEIPT = ROOT / "docs/generated/execution-policy-contract-v3.json"


def test_the_committed_contract_receipt_is_exactly_derived_from_current_code() -> None:
    committed = ExecutionPolicyContractReceiptV3.model_validate(json.loads(RECEIPT.read_text(encoding="utf-8")))
    assert committed == build_execution_policy_contract_receipt()
    assert committed.terminal == "EXECUTION_POLICY_CONTRACT_V3_SEALED"


def test_the_receipt_closes_source_routing_and_freezes_every_execution_identity() -> None:
    receipt = build_execution_policy_contract_receipt()
    assert receipt.source_native_routing == {
        "BINANCE_USDM": "binance.usdm",
        "HYPERLIQUID_PERP": "hyperliquid.perp",
    }
    assert receipt.submission_fence_version == "submission_fence_v1"
    assert len(receipt.submission_fence_sha256) == 64
    assert set(receipt.exact_execution_values) == {
        "version",
        "bindings",
        "source_native",
        "side",
        "leverage_ceiling",
        "global_active_lifecycle_ceiling",
        "target_notional_ceiling",
        "ttl_ms",
        "stop_loss_bps",
        "max_holding_ms",
        "max_entry_drift_bps",
        "max_spread_bps",
        "quantity_rule",
    }
