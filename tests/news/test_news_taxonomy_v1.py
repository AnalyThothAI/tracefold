from __future__ import annotations

import pytest
from pydantic import ValidationError

from tracefold.news.models import TriageAsset, TriageVerdict
from tracefold.news.program.contracts import EditorialEnvelope, ScoredJudgment, TradeRelevanceV1
from tracefold.news.taxonomy import (
    IPTC_CODEBOOK_SHA256,
    ModelTaxonomyV1,
    NewsTaxonomyV1,
    source_authority,
    source_authority_from_evidence,
)
from tracefold.news.triage_rules import GateFacts, decide


def _taxonomy(**updates: object) -> NewsTaxonomyV1:
    values: dict[str, object] = {
        "subject_codes": ["medtop:20001279"],
        "event_family": "market_access",
        "change_state": "effective",
        "assertion_status": "confirmed",
        "source_authority": "reputable_secondary",
    }
    values.update(updates)
    return NewsTaxonomyV1.model_validate(values)


def _judgment(taxonomy: NewsTaxonomyV1) -> ScoredJudgment:
    relevance = TradeRelevanceV1(
        impact_breadth="single_instrument",
        tradability="direct",
        surprise="unscheduled",
        development_delta="state_change",
        channels=("exchange_access",),
        affected_markets=("single_asset",),
        reader_value="realtime",
    )
    return ScoredJudgment.issue(
        verdict=TriageVerdict(
            novelty="new_fact",
            restates=-1,
            # A named primary: policy v12 drops a single-name realtime verdict that names no instrument (#504),
            # which is a verdict fact and not a taxonomy one.
            assets=(TriageAsset(symbol="OKB", role="primary"),),
            direction="neutral",
            scope="single_name",
            magnitude=2,
            confidence=0.8,
            headline_zh="某交易所开放新市场",
            why_zh="新增市场改变可交易入口。",
            audience="crypto",
        ),
        editorial=EditorialEnvelope.issue(relevance=relevance, taxonomy=taxonomy),
    )


def test_taxonomy_schema_is_exact_bounded_and_content_pinned() -> None:
    taxonomy = _taxonomy(subject_codes=["medtop:20001279", "medtop:20000385", "medtop:20001279"])

    assert taxonomy.subject_codes == ("medtop:20000385", "medtop:20001279")
    assert taxonomy.codebook_sha256 == IPTC_CODEBOOK_SHA256
    with pytest.raises(ValidationError):
        ModelTaxonomyV1(
            subject_codes=("medtop:not-real",),
            event_family="market_access",
            change_state="effective",
            assertion_status="confirmed",
        )
    with pytest.raises(ValidationError):
        _taxonomy(unplanned_axis="must fail")
    with pytest.raises(ValidationError, match="news_taxonomy_parent_child_duplicate"):
        _taxonomy(subject_codes=["medtop:04000000", "medtop:20000205"])


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (("sec.gov",), "regulatory_filing"),
        (("edgar.sec.gov",), "regulatory_filing"),
        (("@coinbase",), "issuer_first_party"),
        (("https://www.reuters.com/world",), "reputable_secondary"),
        (("Reuters fan account",), "unknown"),
        (("fan:reuters",), "unknown"),
        (("fake|sec",), "unknown"),
        (("notreuters.com",), "unknown"),
        (("https://reuters.com.evil.example/world",), "unknown"),
        (("reuters.com.evil.example",), "unknown"),
        (("https://reuters.com@evil.example/world",), "unknown"),
        # #522 D1: a registered domain owns its subdomains. The three above still do not match, because
        # the boundary is a leading dot at the end of the host, not a substring.
        (("https://wire.reuters.com/world",), "reputable_secondary"),
        (("investor.uber.com",), "issuer_first_party"),
        (("www.barrons.com",), "reputable_secondary"),
        # A newswire distributes the issuer's own release verbatim, so it is first-party, not secondary.
        (("globenewswire.com",), "issuer_first_party"),
        (("prnewswire.com",), "issuer_first_party"),
        (("businesswire.com",), "issuer_first_party"),
        # The two highest-volume reporting origins of the #504 receipt, and one issuer product line.
        (("jin10",), "reputable_secondary"),
        (("first squawk",), "reputable_secondary"),
        (("@firstsquawk",), "reputable_secondary"),
        (("binance wallet",), "issuer_first_party"),
        # Deliberately out of the registry: an aggregator, a relay and a personal account carry no
        # institutional authority, and a belligerent's state media is a party to what it reports.
        (("opennews",), "unknown"),
        (("zerohedge",), "unknown"),
        (("alexbward",), "unknown"),
        (("tass",), "unknown"),
        (("irib",), "unknown"),
    ],
)
def test_source_authority_is_exact_code_owned_reporting_source(
    sources: tuple[str, ...],
    expected: str,
) -> None:
    assert source_authority(sources) == expected


def test_strategy_routing_ids_cannot_claim_source_authority() -> None:
    assert (
        source_authority_from_evidence({"source": "fan account", "strategies": ["reuters"], "provenance": ["sec"]})
        == "unknown"
    )


def test_taxonomy_has_no_delivery_authority() -> None:
    """The four model-owned axes never enter `decide()`. The code-owned `source_authority` does since #504 D3, and
    only as the corroboration fact for an eligible `escalate`; a realtime push is blind to it."""

    facts = GateFacts(grounded_assets=(), watchlist_symbols=frozenset(), admission="semantic")
    access = decide(_judgment(_taxonomy()), facts, None)
    rumor = decide(
        _judgment(
            _taxonomy(
                subject_codes=(),
                event_family="geopolitical_conflict",
                change_state="unknown",
                assertion_status="rumor",
                source_authority="unknown",
            )
        ),
        facts,
        None,
    )

    assert access == rumor
    assert access.final == "push"
