from __future__ import annotations

from tracefold.news import liquidations, oi_signals, smart_money
from tracefold.news.source_contracts import (
    SOURCE_CONTRACT_CLASSIFIER_VERSION,
    classify_source_contract,
    market_route,
)


def test_every_market_parser_stamps_its_own_generation_on_the_fact_it_writes() -> None:
    """The stored evidence of *how* an observation was read, one string per parser.

    These are what a replay compares against, so they are pinned here rather than left to whichever
    row happens to be in the database. `opennews_liquidation_source_v2` is the one that moved in
    #553: the venue allowlist is gone, the reporting Strategy is recorded and the native instrument
    token is kept, and all three change what a stored liquidation row means.
    """

    assert oi_signals.PARSER_VERSION == "oi_signal_parser_v1"
    assert oi_signals.SOURCE_CONTRACT_VERSION == "opennews_oi_source_v1"
    assert liquidations.PARSER_VERSION == "liquidation_parser_v1"
    assert liquidations.SOURCE_CONTRACT_VERSION == "opennews_liquidation_source_v2"
    assert smart_money.PARSER_VERSION == "smart_money_parser_v1"
    assert smart_money.SOURCE_CONTRACT_VERSION == "opennews_smart_money_source_v1"


def test_the_classifier_version_moved_with_the_routing_it_names() -> None:
    """v2 keys market families on the Strategy id and knows `smart_money`. v1 did neither."""

    assert SOURCE_CONTRACT_CLASSIFIER_VERSION == "opennews_source_classifier_v2"


def test_a_provider_rename_never_takes_a_market_frame_out_of_its_own_contract() -> None:
    """The measured failure: `Large-scale liquidation` was renamed and every 2083 frame fell out.

    A display name is a fact about the provider's console. The Strategy id is the provider's own
    primary key for what the frame measures, and it is the only thing this routing reads.
    """

    for strategy_id, expected in (
        ("1019", "oi"),
        ("2000", "liquidation"),
        ("2083", "liquidation"),
        ("2026", "smart_money"),
    ):
        renamed = classify_source_contract(
            {"strategies": [{"id": strategy_id, "name": "renamed", "source_type": "x", "engine_type": "y"}]}
        )
        assert market_route((renamed,)) == (expected, None), strategy_id


def test_each_parser_writes_the_reason_a_raw_card_carries_and_they_do_not_collide() -> None:
    reasons = {
        oi_signals.RAW_REASON_TEMPLATE_UNMATCHED,
        liquidations.RAW_REASON_TEMPLATE_UNMATCHED,
        smart_money.RAW_REASON_TEMPLATE_UNMATCHED,
    }
    assert len(reasons) == 3
    assert reasons == {"oi_template_unmatched", "liquidation_template_unmatched", "smart_money_template_unmatched"}
