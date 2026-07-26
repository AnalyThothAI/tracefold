from __future__ import annotations

from tracefold.news import BriefEvidenceBundle, StoryAnalysisEvidence
from tracefold.news.validation import (
    validate_brief_publication,
    validate_story_publication,
)


def test_story_validation_grounds_each_fact_against_its_cited_revision() -> None:
    evidence = story_evidence(
        articles=(
            article(
                "revision-ecb",
                "European Central Bank cuts rates by 25 basis points on 2026-07-26",
            ),
            article(
                "revision-china",
                "China reports growth of 5%",
            ),
        )
    )
    payload = story_payload(
        fact_text="中国报告增长5%",
        evidence_references=("revision-ecb",),
    )

    draft, errors = validate_story_publication(payload, evidence)

    assert draft is None
    assert "unsupported_number:story-1:fact:0:5%" in errors
    assert "unsupported_entity:story-1:fact:0:中国" in errors


def test_story_validation_accepts_cross_language_aliases_and_normalized_units_dates() -> None:
    evidence = story_evidence(
        articles=(
            article(
                "revision-ecb",
                "European Central Bank cuts rates by 25 basis points on 2026-07-26",
            ),
        )
    )
    payload = story_payload(
        fact_text="欧洲央行于2026年7月26日降息25个基点",
        evidence_references=("revision-ecb",),
    )

    draft, errors = validate_story_publication(payload, evidence)

    assert errors == ()
    assert draft is not None


def test_story_validation_checks_interpretation_fields_for_unsupported_facts() -> None:
    evidence = story_evidence(articles=(article("revision-ecb", "European Central Bank cuts rates"),))
    payload = story_payload(
        fact_text="欧洲央行宣布降息",
        evidence_references=("revision-ecb",),
    )
    payload["economic_market_impact"] = "这可能令日本央行在2027年降息50个基点"

    draft, errors = validate_story_publication(payload, evidence)

    assert draft is None
    assert any(error.startswith("unsupported_number:story-1:interpretation:") for error in errors)
    assert any(error.startswith("unsupported_date:story-1:interpretation:") for error in errors)
    assert "unsupported_entity:story-1:interpretation:日本央行" in errors


def test_story_validation_requires_correction_evidence_and_uncertainty() -> None:
    evidence = story_evidence(
        evidence_posture="corrected",
        evidence_factors={},
        articles=(
            article("revision-original", "Agency reports output rose 10%"),
            article(
                "revision-correction",
                "Correction: Agency says output rose 1%",
                development_relation="correction",
            ),
        ),
    )
    payload = story_payload(
        fact_text="机构最初报告产出上升10%",
        evidence_references=("revision-original",),
    )

    draft, errors = validate_story_publication(payload, evidence)

    assert draft is None
    assert "evidence_posture_not_preserved:story-1" in errors
    assert "conflict_or_correction_refs_missing:story-1" in errors


def test_story_validation_can_ground_facts_in_bounded_content_snapshot() -> None:
    source = article("revision-source", "Central bank publishes decision")
    source["content_snapshot"] = {
        "content_snapshot_id": "snapshot-1",
        "status": "available",
        "content_hash": "content-hash",
        "fetched_at_ms": 100,
        "extracted_text": "The central bank cut rates by 75 basis points.",
    }
    evidence = story_evidence(articles=(source,))
    payload = story_payload(
        fact_text="央行降息75个基点",
        evidence_references=("revision-source",),
    )

    draft, errors = validate_story_publication(payload, evidence)

    assert errors == ()
    assert draft is not None


def test_brief_validation_enforces_exact_story_order_and_cited_correction() -> None:
    corrected = {
        "story_id": "story-corrected",
        "title": "Agency corrects output estimate",
        "snippet": "",
        "event_core": {},
        "evidence_posture": "corrected",
        "evidence_factors": {},
        "evidence_articles": [
            article("revision-original", "Agency reports output rose 10%"),
            article(
                "revision-correction",
                "Correction: Agency says output rose 1%",
                development_relation="correction",
            ),
        ],
    }
    second = {
        "story_id": "story-second",
        "title": "Federal Reserve cuts rates",
        "snippet": "",
        "event_core": {"entities": ["fed"]},
        "evidence_posture": "single_origin_reported",
        "evidence_factors": {},
        "evidence_articles": [
            article("revision-fed", "Federal Reserve cuts rates"),
        ],
    }
    evidence = BriefEvidenceBundle(
        selection_snapshot_id="selection-1",
        selection_fingerprint="selection-fingerprint",
        evidence_bundle_hash="bundle-hash",
        cutoff_at_ms=100,
        stories=(corrected, second),
        narrative_groups=(),
        selection_policy_version="selection-v1",
    )
    payload = {
        "headline": "全球政策简报",
        "executive_summary": "机构修正数据，美联储降息",
        "items": [
            brief_item(
                "story-second",
                "美联储宣布降息",
                ("revision-fed",),
            ),
            brief_item(
                "story-corrected",
                "机构最初报告产出上升10%",
                ("revision-original",),
            ),
        ],
        "narratives": [],
        "global_watchpoints": [],
    }

    draft, errors = validate_brief_publication(payload, evidence)

    assert draft is None
    assert "selected_story_coverage_or_order_mismatch" in errors
    assert "evidence_posture_not_preserved:story-corrected" in errors
    assert "conflict_or_correction_refs_missing:story-corrected" in errors


def story_evidence(
    *,
    articles: tuple[dict[str, object], ...],
    evidence_posture: str = "single_origin_reported",
    evidence_factors: dict[str, object] | None = None,
) -> StoryAnalysisEvidence:
    return StoryAnalysisEvidence(
        story_id="story-1",
        material_evidence_hash="material-hash",
        title="Central bank decision",
        snippet="",
        event_core={},
        evidence_posture=evidence_posture,  # type: ignore[arg-type]
        evidence_factors=evidence_factors or {},
        impact_profile={},
        material_change="first_report",
        articles=articles,
    )


def article(
    evidence_ref: str,
    title: str,
    *,
    development_relation: str = "initial",
) -> dict[str, object]:
    return {
        "evidence_ref": evidence_ref,
        "article_id": f"article-{evidence_ref}",
        "revision_id": evidence_ref,
        "title": title,
        "snippet": "",
        "source_published_at_ms": 100,
        "observed_at_ms": 100,
        "language": "en",
        "canonical_url": f"https://example.com/{evidence_ref}",
        "source_id": "source",
        "source_name": "Source",
        "source_role": "original_publisher",
        "trust_tier": "trusted",
        "source_chain_id": "source",
        "publisher_organization_id": "source",
        "content_form": "report",
        "origin_relation": "originating",
        "development_relation": development_relation,
        "epistemic_use": "fact_evidence",
        "reporting_origin_id": "source",
        "origin_confidence": 0.9,
    }


def story_payload(
    *,
    fact_text: str,
    evidence_references: tuple[str, ...],
) -> dict[str, object]:
    return {
        "what_happened": [
            {
                "text": fact_text,
                "evidence_references": list(evidence_references),
            }
        ],
        "why_it_matters": "这改变了政策路径",
        "political_impact": "政治影响仍取决于后续执行",
        "economic_market_impact": "市场影响取决于传导条件",
        "disagreements_unknowns": [],
        "transmission_scenarios": [
            {
                "condition": "如果政策持续",
                "mechanism": "通过融资条件传导",
                "possible_effect": "市场波动可能上升",
                "confidence": "medium",
            }
        ],
        "next_checkpoint": "下一步观察官方文件",
    }


def brief_item(
    story_id: str,
    fact_text: str,
    evidence_references: tuple[str, ...],
) -> dict[str, object]:
    return {
        "story_id": story_id,
        "what_happened": [
            {
                "text": fact_text,
                "evidence_references": list(evidence_references),
            }
        ],
        "why_it_matters": "这会改变政策预期",
        "transmission_scenarios": [],
        "uncertainties": [],
        "watchpoints": [],
    }
