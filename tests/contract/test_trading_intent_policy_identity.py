"""The execution policy identity is release evidence, and moving it is a signature (#348).

`INTENT_POLICY_SHA256` is stamped into `engine_identity` on every fenced entry and frozen onto every
Intent row. Nothing else pins it: the model validator deliberately lets history keep its own digest,
`create()` can only compare the constant to itself, and the schema asserts shape rather than value —
each for a reason recorded where it lives. This test is what is left, and it is the point: a digest
that moves silently is a policy change nobody signed for.

Re-pin deliberately, in the same commit that changes the payload, and say in the message which
execution number moved.
"""

from __future__ import annotations

import pytest

from tracefold.trading.intent import INTENT_POLICY_PAYLOAD, INTENT_POLICY_SHA256, INTENT_POLICY_VERSION

pytestmark = pytest.mark.contract

EXPECTED_VERSION = "trade_intent_policy_v5"
EXPECTED_SHA256 = "c797b92fadc3a14383a19effb89e40f11ad14cb541ee23a64d40f76cafc171d7"


def test_the_execution_policy_identity_is_the_one_this_release_signed_for() -> None:
    assert INTENT_POLICY_VERSION == EXPECTED_VERSION
    assert INTENT_POLICY_SHA256 == EXPECTED_SHA256


def test_the_payload_carries_demo_only_execution_ceilings() -> None:

    assert "max_entries_per_utc_day" not in INTENT_POLICY_PAYLOAD
    assert INTENT_POLICY_PAYLOAD["bindings"] == ["BINANCE_USDM"]
    assert set(INTENT_POLICY_PAYLOAD) == {
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
