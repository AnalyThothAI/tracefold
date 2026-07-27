from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from tracefold.macro import DocumentFact
from tracefold.macro.fed_analysis import (
    FedAnalysisEvidence,
    FedDocumentAnalysisDraft,
    canonicalize_document_analysis,
)
from tracefold.macro.fed_roles import derive_fomc_role_facts, match_effective_role


def _minutes_document() -> DocumentFact:
    return DocumentFact(
        document_id="macrodoc_minutes",
        dataset_id="federal_reserve.fomc.documents",
        document_type="minutes",
        title="Minutes",
        effective_date=date(2026, 6, 17),
        published_at_ms=1_000,
        received_at_ms=2_000,
        source_url="https://www.federalreserve.gov/monetarypolicy/fomcminutes20260617.htm",
        content_text="Official minutes body.",
        metadata={
            "content_hash": "sha256:minutes",
            "fomc_role_records": [
                {
                    "official_name": "Example Official",
                    "role_title": "FOMC member",
                    "organization": "Federal Open Market Committee",
                    "fomc_voter": True,
                }
            ],
        },
    )


def test_minutes_derive_append_only_role_facts_and_match_event_date() -> None:
    facts = derive_fomc_role_facts(_minutes_document())
    assert len(facts) == 1
    fact = facts[0]
    assert fact.fomc_participant is True
    assert fact.fomc_voter is True
    assert fact.effective_start == date(2026, 6, 17)

    matched = match_effective_role(
        "Example Official",
        effective_date=date(2026, 7, 1),
        role_rows=[
            {
                "role_fact_id": fact.role_fact_id,
                "official_id": fact.official_id,
                "official_name": fact.official_name,
                "role_title": fact.role_title,
                "organization": fact.organization,
                "effective_start": fact.effective_start,
                "effective_end": fact.effective_end,
                "fomc_participant": fact.fomc_participant,
                "fomc_voter": fact.fomc_voter,
                "received_at_ms": fact.received_at_ms,
            }
        ],
    )
    assert matched is not None
    assert matched["official_id"] == fact.official_id


def test_minutes_role_facts_remove_html_footnote_markers() -> None:
    document = replace(
        _minutes_document(),
        metadata={
            "content_hash": "sha256:minutes",
            "fomc_role_records": [
                {
                    "official_name": "2 Loretta J. Mester",
                    "role_title": "FOMC member",
                    "organization": "Federal Open Market Committee",
                    "fomc_voter": True,
                },
                {
                    "official_name": "3",
                    "role_title": "FOMC member",
                    "organization": "Federal Open Market Committee",
                    "fomc_voter": True,
                },
            ],
        },
    )

    facts = derive_fomc_role_facts(document)

    assert [fact.official_name for fact in facts] == ["Loretta J. Mester"]


def test_document_analysis_requires_exact_source_evidence() -> None:
    document = {
        "document_id": "macrodoc_speech",
        "document_hash": "sha256:speech",
        "content_text": "Inflation remains too high and policy must stay restrictive.",
    }
    draft = FedDocumentAnalysisDraft(
        policy_relevance="policy_signal",
        stance="hawkish",
        confidence=0.8,
        change_from_prior="no_prior",
        rationale="通胀仍高且政策保持限制性。",
        evidence=[
            FedAnalysisEvidence(
                excerpt="Inflation remains too high",
                claim="通胀判断偏鹰",
            )
        ],
    )

    analysis = canonicalize_document_analysis(
        draft,
        document=document,
        roster_context=None,
        prior_analysis=None,
    )
    assert analysis["source_body_hash"] == "sha256:speech"
    assert analysis["evidence"][0]["excerpt"] == "Inflation remains too high"

    invalid = draft.model_copy(update={"evidence": [FedAnalysisEvidence(excerpt="invented quote", claim="不可验证")]})
    with pytest.raises(ValueError, match="evidence_not_exact"):
        canonicalize_document_analysis(
            invalid,
            document=document,
            roster_context=None,
            prior_analysis=None,
        )
