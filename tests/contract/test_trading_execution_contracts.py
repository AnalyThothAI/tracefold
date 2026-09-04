from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from tracefold.trading.execution_contracts import (
    ExecutionObservationV1,
    OperatorIntentV1,
    TradeSignalV1,
)


def _signal(**updates: object) -> TradeSignalV1:
    payload: dict[str, object] = {
        "signal_version": "trade_signal_v1",
        "seq": 1,
        "signal_id": "a" * 64,
        "case_id": "case-btc-long",
        "market_key": "crypto:perp:BTC:USDT",
        "direction": "long",
        "observed_at_ns": 1_000,
        "expires_at_ns": 2_000,
    }
    payload.update(updates)
    return TradeSignalV1.model_validate(payload)


def _intent(**updates: object) -> OperatorIntentV1:
    payload: dict[str, object] = {
        "intent_version": "operator_intent_v1",
        "seq": 1,
        "command_id": "d" * 64,
        "account_slot": "binance-usdm-demo-v1",
        "action": "flatten",
        "scope": "account",
        "reason": "operator drill",
        "operator_identity": "operator:7",
        "authentication_identity": "telegram:user:7",
        "requested_at_ns": 1_000,
        "expires_at_ns": 2_000,
        "market_key": None,
        "direction": None,
    }
    payload.update(updates)
    return OperatorIntentV1.model_validate(payload)


def _observation(**updates: object) -> ExecutionObservationV1:
    payload: dict[str, object] = {
        "observation_version": "execution_observation_v1",
        "event_id": "f" * 64,
        "account_slot": "binance-usdm-demo-v1",
        "execution_strategy": "oi_nautilus_v1",
        "signal_id": "a" * 64,
        "command_id": None,
        "normalized_kind": "signal_disposition",
        "occurred_at_ns": 1_500,
        "observed_at_ns": 1_600,
        "native_identity_references": ("client_order_id:entry-1",),
        "summary": {"disposition": "accepted"},
    }
    payload.update(updates)
    return ExecutionObservationV1.model_validate(payload)


def test_trade_signal_is_engine_neutral_and_bounded() -> None:
    signal = _signal()

    assert signal.direction == "long"
    # `alpha_metadata` is forbidden like every other extra key now. It only ever carried the policy
    # rule, which the Case that emitted the Signal records as `policy_reason` (#537 PR-3).
    for forbidden in ("quantity", "notional", "leverage", "account", "exchange", "order_type", "alpha_metadata"):
        with pytest.raises(ValidationError):
            _signal(**{forbidden: "forbidden"})

    with pytest.raises(ValidationError, match="trade_signal_clock_invalid"):
        _signal(expires_at_ns=1_000)


def test_json_bounds_use_postgres_jsonb_text_bytes_at_exact_edges() -> None:
    metadata = {f"k{index}": "x" * 246 for index in range(8)}
    references = tuple(f"{index:02d}" + "x" * 250 for index in range(16))

    assert _observation(summary=metadata).summary == metadata
    assert _observation(native_identity_references=references).native_identity_references == references
    # The same 16 references offered out of order and with a repeat: the contract normalizes rather
    # than refusing, because no CHECK restates the ordering rule any more (#520 PR-C).
    shuffled = (references[3], references[0], references[3], *references[1:3], *references[4:])
    assert _observation(native_identity_references=shuffled).native_identity_references == references

    oversized_metadata = metadata | {"k0": "x" * 247}
    oversized_references = (references[0] + "x", *references[1:])
    with pytest.raises(ValidationError, match="execution_metadata_invalid"):
        _observation(summary=oversized_metadata)
    with pytest.raises(ValidationError, match="execution_observation_native_identity_invalid"):
        _observation(native_identity_references=oversized_references)


@pytest.mark.parametrize(
    ("factory", "updates", "message"),
    [
        (_signal, {"case_id": "case\x00id"}, "trade_signal_case_invalid"),
        (_observation, {"summary": {"note": "bad\x00value"}}, "execution_metadata_invalid"),
        (_intent, {"reason": "bad\x00reason"}, "operator_intent_text_invalid"),
        (
            _observation,
            {"native_identity_references": ("bad\x00reference",)},
            "execution_observation_native_identity_invalid",
        ),
    ],
)
def test_contracts_reject_postgres_unrepresentable_text_before_storage(
    factory: Callable[..., object], updates: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        factory(**updates)


@pytest.mark.parametrize("action", ["resume_entries", "emergency_halt", "flatten"])
def test_a_high_risk_action_is_a_plain_authenticated_command(action: str) -> None:
    """#520: authentication plus a reason is the whole authority.

    `confirmation_identity` was a stored second one until PR-C dropped the column and its CHECK;
    PR-B then deleted the field and the `CONFIRM` token the ingress derived it from. Halt and
    flatten reduce risk, so a typing ritual in front of them is a cost, not a control.
    """

    intent = _intent(action=action)

    assert intent.action == action
    with pytest.raises(ValidationError):
        _intent(action=action, confirmation_identity="e" * 64)


def test_manual_entry_has_no_size_or_leverage_and_requires_market_direction() -> None:
    manual = _intent(
        action="manual_entry",
        market_key="crypto:perp:ETH:USDT",
        direction="short",
    )
    assert manual.direction == "short"

    with pytest.raises(ValidationError, match="operator_manual_entry_market_required"):
        _intent(action="manual_entry", market_key=None, direction=None)
    with pytest.raises(ValidationError):
        _intent(quantity="1")


def test_execution_observation_is_a_bounded_audit_fact_not_an_oms_state() -> None:
    observation = _observation()
    assert observation.normalized_kind == "signal_disposition"
    assert ExecutionObservationV1.model_validate(observation.model_dump(mode="json")) == observation

    with pytest.raises(ValidationError, match="execution_observation_signal_identity_required"):
        _observation(signal_id=None)
    with pytest.raises(ValidationError, match="execution_observation_correlation_ambiguous"):
        _observation(command_id="3" * 64)
    with pytest.raises(ValidationError):
        _observation(order_state="FILLED")
