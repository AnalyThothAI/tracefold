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

from tracefold.news.oi_contracts import OI_METRIC_VERSION
from tracefold.news.oi_signals import (
    SOURCE_CONTRACT_VERSION,
    measurement_definition,
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


def test_merged_item_provenance_can_prove_oi_when_news_arrived_first() -> None:
    metadata = {
        **FIXTURE["provider_metadata"],
        "strategies": [
            {
                "id": "1018",
                "name": "News Score > 70",
                "source_type": "news",
                "engine_type": "news",
            },
            *FIXTURE["provider_metadata"]["strategies"],
        ],
    }

    contract = oi_source_contract(metadata)

    assert contract is not None
    assert (contract.strategy_id, contract.measurement_window_ms) == ("1019", 300_000)


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
        (
            {
                "strategies": [
                    {"id": "1020", "name": "OI Event Monitor", "source_type": "market", "engine_type": "market"}
                ]
            },
            "a different strategy wearing the same name",
        ),
        (
            {"strategies": [{"id": "2083", "name": "Large-scale liquidation", "source_type": "market"}]},
            "a market strategy that measures something else entirely",
        ),
        ({"strategies": "1019"}, "a scalar where a list belongs"),
    ],
)
def test_an_identity_that_does_not_match_exactly_is_unproven_rather_than_five_minutes(
    metadata: dict[str, Any], why: str
) -> None:
    assert oi_source_contract(metadata) is None, why


def test_a_renamed_but_identical_strategy_still_proves_its_own_window() -> None:
    """#553. The provider renames its Strategies; that is not evidence about the measurement.

    Only the id can repoint at a different monitor, and only the id is read. What stays refused is a
    *different* id -- including another market Strategy, which measures something else entirely.
    """

    renamed = oi_source_contract(
        {"strategies": [{"id": "1019", "name": "OI 15m Monitor", "source_type": "social", "engine_type": "market"}]}
    )
    assert renamed is not None
    assert (renamed.strategy_id, renamed.measurement_window_ms) == ("1019", 300_000)


def test_an_unproven_window_is_a_named_part_of_the_group_definition_not_a_blank() -> None:
    """A reader card is unaffected; what changes is that no consumer may read it as an interval claim.

    The definition string is what a notification group merges on, so `unproven` has to be spelled in
    it: a blank would let frames whose interval nobody established merge with frames whose interval is
    known, under the known one's name.
    """

    assert measurement_definition(None) == f"{OI_METRIC_VERSION}|unproven|unproven"
    proven = oi_source_contract(FIXTURE["provider_metadata"])
    assert proven is not None
    assert measurement_definition(proven) == f"{OI_METRIC_VERSION}|{SOURCE_CONTRACT_VERSION}|300000"
    assert measurement_definition(proven) != measurement_definition(None)


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
