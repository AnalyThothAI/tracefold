from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.source_contracts import (
    SOURCE_CONTRACT_CLASSIFIER_VERSION,
    SourceIdentity,
    classify_source_contract,
    classify_source_contracts,
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
    ("identity", "family", "event_kind", "reason"),
    [
        (
            SourceIdentity("1019", "OI Event Monitor", "market", "market"),
            "oi_v1",
            "oi",
            None,
        ),
        (
            SourceIdentity("1353", "Listing and Delisting Announcements", "news", "listing"),
            "listing_v1",
            "listing",
            None,
        ),
        (
            SourceIdentity("2000", "实时清算", "market", "market"),
            "liquidation_v1",
            "liquidation",
            None,
        ),
        (
            SourceIdentity("2026", "聪明钱监控", "wallet", "market"),
            "unsupported_market",
            "unsupported_market",
            "unsupported_market_contract",
        ),
        (
            SourceIdentity("2083", "Large-scale liquidation", "market", "market"),
            "unsupported_market",
            "unsupported_market",
            "unsupported_market_contract",
        ),
    ],
)
def test_exact_provider_contracts_have_one_closed_classification(
    identity: SourceIdentity,
    family: str,
    event_kind: str,
    reason: str | None,
) -> None:
    result = classify_source_contract(
        _metadata(identity.strategy_id, identity.strategy_name, identity.source_type, identity.engine_type)
    )
    assert result.identity == identity
    assert result.source_contract_family == family
    assert result.event_kind == event_kind
    assert result.reason == reason
    assert result.classifier_version == SOURCE_CONTRACT_CLASSIFIER_VERSION


@pytest.mark.parametrize("field", ["strategy_id", "strategy_name", "source_type", "engine_type"])
@pytest.mark.parametrize(
    "identity",
    [
        SourceIdentity("1019", "OI Event Monitor", "market", "market"),
        SourceIdentity("1353", "Listing and Delisting Announcements", "news", "listing"),
        SourceIdentity("2000", "实时清算", "market", "market"),
    ],
)
def test_a_known_id_with_any_rebound_identity_field_fails_closed(
    identity: SourceIdentity,
    field: str,
) -> None:
    values = identity._asdict()
    values[field] = f"wrong-{values[field]}"
    if field == "strategy_id":
        # A different provider-enabled listing remains a listing Program route; an unbound scoreless
        # market Strategy is unsupported. Neither inherits the old id's exact contract.
        if identity.engine_type == "listing":
            result = classify_source_contract(
                _metadata(
                    values["strategy_id"],
                    values["strategy_name"],
                    values["source_type"],
                    values["engine_type"],
                )
            )
            assert (result.source_contract_family, result.event_kind, result.reason) == ("listing_v1", "listing", None)
            return
        expected_reason = "unsupported_market_contract"
    else:
        values["strategy_id"] = identity.strategy_id
        expected_reason = "source_contract_drift"
    result = classify_source_contract(
        _metadata(
            values["strategy_id"],
            values["strategy_name"],
            values["source_type"],
            values["engine_type"],
        )
    )
    assert result.source_contract_family == "unsupported_market"
    assert result.event_kind == "unsupported_market"
    assert result.reason == expected_reason


def test_provider_enablement_still_allows_ordinary_news_listing_and_scored_market() -> None:
    ordinary = classify_source_contract(_metadata("9999", "Any enabled news", "news", "news"))
    listing = classify_source_contract(_metadata("9998", "Any enabled listing", "news", "listing"))
    scored_market = classify_source_contract(_metadata("9997", "Rated market signal", "market", "market", score=85))
    assert (ordinary.source_contract_family, ordinary.event_kind, ordinary.reason) == ("news_v1", "news", None)
    assert (listing.source_contract_family, listing.event_kind, listing.reason) == ("listing_v1", "listing", None)
    assert (scored_market.source_contract_family, scored_market.event_kind, scored_market.reason) == (
        "news_v1",
        "news",
        None,
    )


def test_every_strategy_tuple_on_a_material_item_is_reconstructable_in_first_seen_order() -> None:
    metadata = _metadata("1018", "News Score > 70", "news", "news", score=90)
    metadata["strategies"].extend(
        [
            _metadata("1019", "OI Event Monitor", "market", "market")["strategies"][0],
            _metadata("1019", "wrong OI monitor", "market", "market")["strategies"][0],
        ]
    )

    contracts = classify_source_contracts(metadata)

    assert [(contract.event_kind, contract.reason) for contract in contracts] == [
        ("news", None),
        ("oi", None),
        ("unsupported_market", "source_contract_drift"),
    ]


@pytest.mark.parametrize("source_type", ["market", "wallet"])
def test_unbound_scoreless_market_or_wallet_frames_have_a_named_safe_terminal(source_type: str) -> None:
    result = classify_source_contract(_metadata("9999", "Unknown source", source_type, "market"))
    assert result.source_contract_family == "unsupported_market"
    assert result.event_kind == "unsupported_market"
    assert result.reason == "unsupported_market_contract"


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (_metadata("1019", "OI Event Monitor", "market", "market"), "telemetry_deterministic"),
        (_metadata("2000", "实时清算", "market", "market"), "liquidation_deterministic"),
        (_metadata("2026", "聪明钱监控", "wallet", "market"), "unsupported_market_contract"),
        (_metadata("9998", "Any enabled listing", "news", "listing"), "listing_deterministic"),
        (_metadata("9999", "Any enabled news", "news", "news"), "candidate"),
    ],
)
def test_one_pure_composition_selects_the_existing_route(metadata: dict[str, Any], expected: str) -> None:
    contract = classify_source_contract(metadata)
    assert source_contract_admission(contract, generic_admission="candidate", ingest_mode="live") == expected
    recovery = "unsupported_market_contract" if contract.source_contract_family == "unsupported_market" else "recovery"
    assert source_contract_admission(contract, generic_admission="candidate", ingest_mode="recovery") == recovery
