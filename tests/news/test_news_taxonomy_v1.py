from __future__ import annotations

import pytest
from pydantic import ValidationError

from tracefold.news.models import TriageVerdict
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
            assets=(),
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
        (("@coinbase",), "issuer_first_party"),
        (("https://www.reuters.com/world",), "reputable_secondary"),
        (("Reuters fan account",), "unknown"),
        (("fan:reuters",), "unknown"),
        (("fake|sec",), "unknown"),
        (("notreuters.com",), "unknown"),
        (("https://reuters.com.evil.example/world",), "unknown"),
        (("https://reuters.com@evil.example/world",), "unknown"),
        (("https://wire.reuters.com/world",), "unknown"),
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
