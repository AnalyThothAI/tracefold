"""What the provider proves about *how* an OI frame was measured (#265).

The strategy #265 targets acts on "5 minute OI rise >= 10%". The frame's title carries no interval —
`TAC OI Rise 3.58%, OI Value 8.83M, …` says nothing about five minutes — and there is no interval field
anywhere in the provider payload. So the window is either proven from an exact strategy identity with a
real frame behind it, or it is not proven at all, and the second answer has to be as easy to act on as
the first.

Everything here is the refusal side of that. A default, an inference from arrival deltas, or a constant
inside a strategy would each turn an unverified interval into a frozen claim in an immutable Case.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tracefold.news.oi_signals import (
    DEFAULT_OI_POLICY,
    SOURCE_CONTRACT_VERSION,
    evaluate_oi,
    oi_judgment_trace,
    oi_source_contract,
    parse_oi_signal,
)

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "opennews_oi_source_1019.json").read_text())


def test_the_pinned_real_frame_proves_the_five_minute_window() -> None:
    """The evidence behind the mapping, kept as the frame this deployment actually stored."""

    contract = oi_source_contract(FIXTURE["provider_metadata"])
    assert contract is not None
    expected = FIXTURE["expected"]
    assert contract.strategy_id == expected["source_strategy_id"]
    assert contract.contract_version == expected["source_contract_version"] == SOURCE_CONTRACT_VERSION
    assert contract.measurement_window_ms == expected["measurement_window_ms"] == 300_000


def test_the_same_frame_still_parses_to_the_four_numbers_it_always_did() -> None:
    """The contract is published *beside* the measurements, never instead of them."""

    signal = parse_oi_signal(FIXTURE["title"])
    assert signal is not None
    expected = FIXTURE["expected"]
    assert signal.symbol == expected["symbol"]
    assert signal.direction == expected["direction"]
    assert signal.oi_change_bps == expected["oi_change_bps"]
    assert signal.oi_value_usd == expected["oi_value_usd"]
    assert signal.whale_long_profit_bps == expected["whale_long_profit_bps"]
    assert signal.whale_oi_ratio_bps == expected["whale_oi_ratio_bps"]


@pytest.mark.parametrize(
    ("metadata", "why"),
    [
        ({}, "no strategies member at all"),
        ({"strategies": []}, "an empty list"),
        ({"strategies": [{"id": "1019"}]}, "an id with no name, source or engine to key on"),
        (
            {
                "strategies": [
                    {"id": "1020", "name": "OI Event Monitor", "source_type": "market", "engine_type": "market"}
                ]
            },
            "a different strategy wearing the same name",
        ),
        (
            {
                "strategies": [
                    {"id": "1019", "name": "OI 15m Monitor", "source_type": "market", "engine_type": "market"}
                ]
            },
            "the id repointed at a different monitor",
        ),
        (
            {
                "strategies": [
                    {"id": "1019", "name": "OI Event Monitor", "source_type": "social", "engine_type": "market"}
                ]
            },
            "a drifted source type",
        ),
        ({"strategies": "1019"}, "a scalar where a list belongs"),
    ],
)
def test_an_identity_that_does_not_match_exactly_is_unproven_rather_than_five_minutes(
    metadata: dict[str, Any], why: str
) -> None:
    assert oi_source_contract(metadata) is None, why


def test_an_unproven_window_is_a_named_reason_in_the_trace_not_a_missing_key() -> None:
    """A reader card is unaffected; what changes is that no consumer may read it as an interval claim."""

    signal = parse_oi_signal(FIXTURE["title"])
    assert signal is not None
    judgment = evaluate_oi(signal, earlier_eligible_count=0, policy=DEFAULT_OI_POLICY)

    unproven = oi_judgment_trace(judgment, policy=DEFAULT_OI_POLICY, source=None)
    assert unproven["source_contract_rule"] == "source_window_unproven"
    assert unproven["measurement_window_ms"] is None
    assert unproven["source_strategy_id"] is None
    # Still a complete judgment: the four numbers and the rank rule are untouched.
    assert unproven["parsed"] is True
    assert unproven["oi_change_bps"] == FIXTURE["expected"]["oi_change_bps"]

    proven = oi_judgment_trace(
        judgment, policy=DEFAULT_OI_POLICY, source=oi_source_contract(FIXTURE["provider_metadata"])
    )
    assert proven["source_contract_rule"] == "proven"
    assert proven["measurement_window_ms"] == 300_000


def test_the_whale_profit_field_is_documented_as_the_providers_percentage_and_nothing_more() -> None:
    """#265 §3.3. The name invites a stronger reading than NewsLiquid publishes.

    The provider ships no `account_count`, `profitable_account_count`, `unrealized_pnl_usd` or
    `position_snapshot_at_ms`, so anything that renders one of those is inventing it. Pinning the
    docstring is cheap and is the only place a future reader will look before widening the meaning.
    """

    from tracefold.news.oi_signals import OiSourceContract

    doc = OiSourceContract.__doc__ or ""
    assert "not" in doc and "unrealised PnL" in doc
    assert "account_count" in doc and "profitable_account_count" in doc
