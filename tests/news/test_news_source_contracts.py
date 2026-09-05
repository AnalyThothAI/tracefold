from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.source_contracts import (
    MARKET_CATEGORY_CONFLICT,
    SOURCE_CONTRACT_CLASSIFIER_VERSION,
    SourceIdentity,
    classify_source_contract,
    classify_source_contracts,
    market_route,
    source_contract_admission,
)


def _metadata(
    strategy_id: str,
    name: str,
    source_type: str,
    engine_type: str,
    *,
    score: float | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "strategies": [
            {
                "id": strategy_id,
                "name": name,
                "source_type": source_type,
                "engine_type": engine_type,
            }
        ]
    }
    if score is not None:
        metadata["score"] = score
    return metadata


@pytest.mark.parametrize(
    ("identity", "family", "event_kind", "market_kind"),
    [
        (SourceIdentity("1019", "OI Event Monitor", "market", "market"), "oi_v1", None, "oi"),
        (
            SourceIdentity("1353", "Listing and Delisting Announcements", "news", "listing"),
            "listing_v1",
            "listing",
            None,
        ),
        (SourceIdentity("2000", "实时清算", "market", "market"), "liquidation_v1", None, "liquidation"),
        (SourceIdentity("2026", "聪明钱监控", "wallet", "market"), "smart_money_v1", None, "smart_money"),
        (
            SourceIdentity("2083", "Large-scale liquidation", "market", "market"),
            "liquidation_v1",
            None,
            "liquidation",
        ),
    ],
)
def test_exact_provider_contracts_have_one_closed_classification(
    identity: SourceIdentity,
    family: str,
    event_kind: str | None,
    market_kind: str | None,
) -> None:
    result = classify_source_contract(
        _metadata(identity.strategy_id, identity.strategy_name, identity.source_type, identity.engine_type)
    )
    assert result.identity == identity
    assert result.source_contract_family == family
    assert (result.event_kind, result.market_kind) == (event_kind, market_kind)
    assert result.classifier_version == SOURCE_CONTRACT_CLASSIFIER_VERSION
    # Exactly one of the two vocabularies answers for a frame; a contract in both would let one frame
    # be an Event and an observation at once.
    assert (result.event_kind is None) != (result.market_kind is None)


@pytest.mark.parametrize("field", ["strategy_name", "source_type", "engine_type"])
@pytest.mark.parametrize(
    "identity",
    [
        SourceIdentity("1019", "OI Event Monitor", "market", "market"),
        SourceIdentity("2000", "实时清算", "market", "market"),
        SourceIdentity("2026", "聪明钱监控", "wallet", "market"),
        SourceIdentity("2083", "Large-scale liquidation", "market", "market"),
    ],
)
def test_a_market_strategy_keeps_its_contract_when_the_provider_renames_it(
    identity: SourceIdentity,
    field: str,
) -> None:
    """#553. The provider renamed `Large-scale liquidation` and every 2083 frame fell out of contract.

    The display name, the source type and the engine type describe the provider's console. The id is
    the provider's own primary key for the Strategy, and it is the only field this routing reads.
    """

    baseline = classify_source_contract(
        _metadata(identity.strategy_id, identity.strategy_name, identity.source_type, identity.engine_type)
    )
    values = identity._asdict()
    values[field] = f"wrong-{values[field]}"
    drifted = classify_source_contract(
        _metadata(values["strategy_id"], values["strategy_name"], values["source_type"], values["engine_type"])
    )
    assert drifted.source_contract_family == baseline.source_contract_family
    assert drifted.market_kind == baseline.market_kind


def test_a_different_id_never_inherits_a_bound_strategys_contract() -> None:
    """An unbound scoreless market Strategy is `unknown_market` -- stored and readable, never reinterpreted."""

    result = classify_source_contract(_metadata("1020", "OI Event Monitor", "market", "market"))
    assert result.source_contract_family == "unknown_market"
    assert result.market_kind == "unknown_market"


def test_provider_enablement_still_allows_ordinary_news_listing_and_scored_market() -> None:
    ordinary = classify_source_contract(_metadata("9999", "Any enabled news", "news", "news"))
    listing = classify_source_contract(_metadata("9998", "Any enabled listing", "news", "listing"))
    scored_market = classify_source_contract(_metadata("9997", "Rated market signal", "market", "market", score=85))
    assert (ordinary.source_contract_family, ordinary.event_kind) == ("news_v1", "news")
    assert (listing.source_contract_family, listing.event_kind) == ("listing_v1", "listing")
    assert (scored_market.source_contract_family, scored_market.event_kind) == ("news_v1", "news")
    for contract in (ordinary, listing, scored_market):
        assert market_route((contract,)) is None


def test_a_renamed_listing_strategy_stays_on_the_listing_branch() -> None:
    """It is the ordinary News/listing branch, and #553 does not move it into the market plane."""

    renamed = classify_source_contract(_metadata("1353", "Listing and Delisting Announcements v2", "news", "listing"))
    assert (renamed.source_contract_family, renamed.event_kind) == ("listing_v1", "listing")


def test_every_strategy_tuple_on_a_material_item_is_reconstructable_in_first_seen_order() -> None:
    metadata = _metadata("1018", "News Score > 70", "news", "news", score=90)
    metadata["strategies"].extend(
        [
            _metadata("1019", "OI Event Monitor", "market", "market")["strategies"][0],
            _metadata("2026", "聪明钱监控", "wallet", "market")["strategies"][0],
        ]
    )

    contracts = classify_source_contracts(metadata)

    assert [(contract.event_kind, contract.market_kind) for contract in contracts] == [
        ("news", None),
        (None, "oi"),
        (None, "smart_money"),
    ]


def test_the_primary_strategy_decides_the_branch_and_accumulated_labels_never_move_a_news_item() -> None:
    """#553 §3.2. An Item picks up Strategy tuples across replays; that is not a reinterpretation.

    A frame whose own first tuple is ordinary news stays on the editorial branch even when a market
    Strategy has since been merged onto the Item, and a market frame is never handed to the model
    because a second tuple looks like news.
    """

    news_first = _metadata("1018", "News Score > 70", "news", "news", score=90)
    news_first["strategies"].append(_metadata("1019", "OI Event Monitor", "market", "market")["strategies"][0])
    assert market_route(classify_source_contracts(news_first)) is None

    market_first = _metadata("1019", "OI Event Monitor", "market", "market")
    market_first["strategies"].append(_metadata("1018", "News Score > 70", "news", "news")["strategies"][0])
    assert market_route(classify_source_contracts(market_first)) == ("unknown_market", MARKET_CATEGORY_CONFLICT)


def test_two_market_families_on_one_frame_are_recorded_as_a_conflict_not_reinterpreted() -> None:
    """Neither family's numeric semantics may be applied under the other's name."""

    conflicted = _metadata("1019", "OI Event Monitor", "market", "market")
    conflicted["strategies"].append(_metadata("2083", "Large-scale liquidation", "market", "market")["strategies"][0])
    assert market_route(classify_source_contracts(conflicted)) == ("unknown_market", MARKET_CATEGORY_CONFLICT)


@pytest.mark.parametrize("source_type", ["market", "wallet"])
def test_unbound_scoreless_market_or_wallet_frames_are_stored_as_unknown_market(source_type: str) -> None:
    result = classify_source_contract(_metadata("9999", "Unknown source", source_type, "market"))
    assert result.source_contract_family == "unknown_market"
    assert market_route((result,)) == ("unknown_market", None)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (_metadata("9998", "Any enabled listing", "news", "listing"), "listing_deterministic"),
        (_metadata("9999", "Any enabled news", "news", "news"), "candidate"),
    ],
)
def test_one_pure_composition_selects_the_existing_editorial_route(metadata: dict[str, Any], expected: str) -> None:
    contract = classify_source_contract(metadata)
    assert source_contract_admission(contract, generic_admission="candidate", ingest_mode="live") == expected
    assert source_contract_admission(contract, generic_admission="candidate", ingest_mode="recovery") == "recovery"
