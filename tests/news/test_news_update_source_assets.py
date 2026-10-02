"""Source-wide provider tags are candidates; only selected cited-source assets become claim fields."""

from __future__ import annotations

import pytest

from tests.support.news_update_semantic import draft, material, update_one
from tracefold.news.models import MARKET_TYPES, market_type_of
from tracefold.news.updates.contracts import Asset, Citation, EventUpdate, Extraction, FrozenInput, SourceAssetCandidate
from tracefold.news.updates.extraction import ground_extraction


@pytest.mark.parametrize(
    ("listed", "model_market", "source_market", "expected"),
    [
        (("crypto",), "unknown", "unknown", "crypto"),
        (("commodity",), "unknown", "unknown", "commodity"),
        (("crypto", "equity"), "unknown", "unknown", "unknown"),
        ((), "unknown", "unknown", "unknown"),
        (("index",), "crypto", "unknown", "crypto"),
        (("crypto", "equity"), "equity", "unknown", "equity"),
        (("crypto",), "crypto", "equity", "equity"),
    ],
)
def test_listing_fills_only_abandoned_unambiguous_markets(listed, model_market, source_market, expected) -> None:
    evidence = material("The named instrument changes price.")
    source = FrozenInput(
        event_id="event",
        revision=1,
        lineage_id="lineage",
        evidence=(evidence,),
        asset_candidates={
            evidence.ref: (SourceAssetCandidate(symbol="ASSET", market_type=source_market, listed_markets=listed),)
        },
    )
    claim = draft(evidence)
    claim = claim.model_copy(
        update={
            "fields": claim.fields.model_copy(
                update={"assets": (Asset(symbol="ASSET", market_type=model_market, role="primary"),)}
            )
        }
    )
    assert ground_extraction(source, Extraction(claims=(claim,))).claims[0].fields.assets[0].market_type == expected


def test_listing_disagreement_between_cited_sources_is_not_collapsed() -> None:
    first, second = material("ACME reports earnings."), material("ACME announces a token.", revision=2)
    source = FrozenInput(
        event_id="event",
        revision=1,
        lineage_id="lineage",
        evidence=(first, second),
        asset_candidates={
            first.ref: (SourceAssetCandidate(symbol="ACME", listed_markets=("equity",)),),
            second.ref: (SourceAssetCandidate(symbol="ACME", listed_markets=("crypto",)),),
        },
    )
    claim = draft(first)
    fields = claim.fields.model_copy(update={"assets": (Asset(symbol="ACME", market_type="unknown", role="primary"),)})
    own = claim.model_copy(update={"fields": fields})
    assert ground_extraction(source, Extraction(claims=(own,))).claims[0].fields.assets[0].market_type == "equity"
    both = own.model_copy(
        update={
            "citations": (
                Citation(evidence_ref=first.ref, quote=first.text),
                Citation(evidence_ref=second.ref, quote=second.text),
            )
        }
    )
    assert ground_extraction(source, Extraction(claims=(both,))).claims[0].fields.assets[0].market_type == "unknown"


def test_selected_source_spelling_and_known_market_are_preserved_without_copying_other_tags() -> None:
    evidence = material("Oracle reports quarterly earnings and mentions Microsoft.")
    source = FrozenInput(
        event_id="event",
        revision=1,
        lineage_id="lineage",
        evidence=(evidence,),
        asset_candidates={
            evidence.ref: (
                SourceAssetCandidate(symbol="xyz-ORCLUSDT", market_type="equity", grade="1"),
                SourceAssetCandidate(symbol="MSFT", market_type="equity", grade="3"),
                SourceAssetCandidate(symbol="BTC", market_type="crypto", grade="3"),
            )
        },
    )
    claim = draft(evidence)
    assets = (
        Asset(symbol="XYZ-ORCLUSDT", market_type="unknown", role="primary"),
        Asset(symbol="msft", market_type="crypto", role="mentioned"),
    )
    claim = claim.model_copy(update={"fields": claim.fields.model_copy(update={"assets": assets})})
    actual = ground_extraction(source, Extraction(claims=(claim,))).claims[0].fields.assets
    assert [(row.symbol, row.market_type, row.role) for row in actual] == [
        ("xyz-ORCLUSDT", "equity", "primary"),
        ("MSFT", "equity", "mentioned"),
    ]
    assert len(source.asset_candidates[evidence.ref]) == 3


def test_only_claim_cited_sources_restore_tags_and_conflicting_markets_are_not_guessed() -> None:
    first, second = material("ACME reports earnings."), material("ACME announces a token.", revision=2)
    source = FrozenInput(
        event_id="event",
        revision=1,
        lineage_id="lineage",
        evidence=(first, second),
        asset_candidates={
            first.ref: (SourceAssetCandidate(symbol="ACME", market_type="equity"),),
            second.ref: (SourceAssetCandidate(symbol="ACME", market_type="crypto"),),
        },
    )
    claim = draft(first)
    claim = claim.model_copy(
        update={
            "fields": claim.fields.model_copy(
                update={"assets": (Asset(symbol="acme", market_type="unknown", role="primary"),)}
            )
        }
    )
    assert ground_extraction(source, Extraction(claims=(claim,))).claims[0].fields.assets[0].market_type == "equity"
    both = claim.model_copy(
        update={
            "citations": (
                Citation(evidence_ref=first.ref, quote=first.text),
                Citation(evidence_ref=second.ref, quote=second.text),
            )
        }
    )
    actual = ground_extraction(source, Extraction(claims=(both,))).claims[0].fields.assets[0]
    assert actual.market_type == "unknown"
    assert source.asset_candidates[first.ref][0].market_type == "equity"
    assert source.asset_candidates[second.ref][0].market_type == "crypto"


def test_address_case_and_explicit_text_supplements_do_not_become_source_aliases() -> None:
    evidence = material("Acme names TOKEN and its Solana contract.")
    address = "Abcdefghijkmnopqrstuvwxyz123456789"
    source = FrozenInput(
        event_id="event",
        revision=1,
        lineage_id="lineage",
        evidence=(evidence,),
        asset_candidates={evidence.ref: (SourceAssetCandidate(symbol=address, market_type="crypto"),)},
    )
    claim = draft(evidence)
    assets = (
        Asset(symbol=address.lower(), market_type="unknown", role="mentioned"),
        Asset(symbol="TOKEN", market_type="crypto", role="primary"),
    )
    claim = claim.model_copy(update={"fields": claim.fields.model_copy(update={"assets": assets})})
    assert ground_extraction(source, Extraction(claims=(claim,))).claims[0].fields.assets == assets
    no_assets = claim.model_copy(update={"fields": claim.fields.model_copy(update={"assets": ()})})
    assert ground_extraction(source, Extraction(claims=(no_assets,))).claims[0].fields.assets == ()


@pytest.mark.parametrize(("legacy", "current"), [("forex", "fx"), ("fund", "unknown")])
def test_legacy_market_read_preserves_adopted_document_and_claim_references(legacy: str, current: str) -> None:
    from tracefold.app.http.routes.events import _attach_asset_refs

    _source, _extraction, head = update_one()
    document = head.model_dump(mode="json")
    document["claims"][0]["fields"]["assets"][0]["market_type"] = legacy
    parsed = EventUpdate.model_validate(document)
    assert parsed.claims[0].fields.assets[0].market_type == current
    assert parsed.claims[0].ref == document["claims"][0]["ref"]
    assert parsed.ref == head.ref and parsed.content_sha == head.content_sha
    assert parsed.content_revision == head.content_revision
    raw_asset = document["claims"][0]["fields"]["assets"][0]
    assert market_type_of(raw_asset["market_type"]) == current
    candidate = SourceAssetCandidate.model_validate({"symbol": raw_asset["symbol"], "market_type": legacy})
    assert candidate.market_type == current

    class Instruments:
        def asset_refs(self, requests):
            return {request: {"symbol": request.symbol, "market_type": request.market_type} for request in requests}

    # Feed SQL supplies raw JSONB; Detail supplies the validated legacy Asset. The same route must
    # resolve both into the same typed question without changing either adopted historical ref.
    events = [
        {"event_id": "feed", "assets": [raw_asset]},
        {"event_id": "detail", "assets": [parsed.claims[0].fields.assets[0].model_dump(mode="json")]},
    ]
    _attach_asset_refs(events, news=None, instruments=Instruments())
    assert events[0]["assets"] == events[1]["assets"] == [{"symbol": raw_asset["symbol"], "market_type": current}]


@pytest.mark.parametrize("contract", [Asset, SourceAssetCandidate])
def test_legacy_read_compatibility_does_not_accept_arbitrary_market_values(contract) -> None:
    data = {"symbol": "ASSET", "market_type": "bond"}
    if contract is Asset:
        data["role"] = "primary"
    with pytest.raises(ValueError):
        contract.model_validate(data)


@pytest.mark.parametrize("market", MARKET_TYPES)
def test_claim_asset_uses_the_shared_market_vocabulary(market) -> None:
    assert Asset(symbol="ASSET", market_type=market, role="primary").market_type == market
