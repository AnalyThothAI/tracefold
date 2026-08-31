from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from tracefold.news.learning.taxonomy_metric import taxonomy_metric
from tracefold.news.taxonomy import (
    ASSERTION_STATUSES,
    CHANGE_STATES,
    EVENT_FAMILIES,
    IPTC_SUBJECT_CODES,
    SOURCE_AUTHORITIES,
)


def _taxonomy(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "taxonomy_version": "news_taxonomy_v1",
        "codebook_sha256": "6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac",
        "subject_codes": ["medtop:20001279"],
        "event_family": "market_access",
        "change_state": "effective",
        "assertion_status": "claimed",
        "source_authority": "reputable_secondary",
    }
    value.update(updates)
    return value


def _gold(**updates: Any) -> dict[str, Any]:
    taxonomy = _taxonomy(**updates)
    return {field: taxonomy[field] for field in ("subject_codes", "event_family", "change_state", "assertion_status")}


def _episode(
    case_id: str,
    cluster_id: str,
    *,
    gold: dict[str, Any],
    prediction: dict[str, Any],
    opened_at_ms: int,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "cluster_id": cluster_id,
        "context": {"now_ms": opened_at_ms},
        "accepted_review": {"taxonomy": gold},
        "production_judgment": {"editorial": {"taxonomy": prediction}},
    }


def test_taxonomy_metric_uses_the_legal_universe_and_actual_scored_population() -> None:
    family_gold = _gold(
        subject_codes=["medtop:20000178"],
        event_family="financial_results",
        change_state="reported",
        assertion_status="confirmed",
    )
    family_prediction = _taxonomy(
        subject_codes=["medtop:20000178", "medtop:20001279"],
        event_family="financial_results",
        change_state="reported",
        assertion_status="claimed",
        source_authority="unknown",
    )
    access_gold = _gold()
    access_prediction = _taxonomy(
        subject_codes=[],
        event_family="other",
        source_authority="issuer_first_party",
    )
    duplicate_gold = deepcopy(family_gold)
    duplicate_prediction = deepcopy(family_prediction)
    duplicate_prediction["source_authority"] = "issuer_first_party"
    episodes = [
        _episode("case-a", "cluster-a", gold=family_gold, prediction=family_prediction, opened_at_ms=1),
        _episode(
            "case-a-copy",
            "cluster-a",
            gold=duplicate_gold,
            prediction=duplicate_prediction,
            opened_at_ms=2,
        ),
        _episode("case-b", "cluster-b", gold=access_gold, prediction=access_prediction, opened_at_ms=3),
    ]

    result = taxonomy_metric(episodes)

    assert (result.case_n, result.independent_cluster_n, result.scored_case_n) == (3, 2, 2)
    assert result.outcome == "INSUFFICIENT_DATA"
    family = result.diagnostics["event_family"]
    assert result.primary == {"event_family_supported_label_macro_f1": 0.5}
    assert tuple(family["per_class"]) == EVENT_FAMILIES
    assert family["per_class"]["geopolitical_conflict"] == {
        "support": 0,
        "predicted": 0,
        "precision": None,
        "recall": None,
        "f1": None,
    }
    assert family["macro_f1"] == 0.5
    assert result.diagnostics["subject_codes"]["micro_f1"] == 0.5
    assert tuple(result.diagnostics["subject_codes"]["per_class"]) == IPTC_SUBJECT_CODES
    assert tuple(result.diagnostics["change_state"]["per_class"]) == CHANGE_STATES
    assert result.diagnostics["change_state"]["accuracy"] == 1.0
    assert tuple(result.diagnostics["assertion_status"]["per_class"]) == ASSERTION_STATUSES
    assert result.diagnostics["assertion_status"]["macro_f1"] == 0.333334
    assert result.diagnostics["four_axis_exact_match"]["accuracy"] == 0.0
    assert result.diagnostics["model_non_abstain"]["case_n"] == 1
    assert tuple(result.source_authority_registry_coverage["per_class"]) == SOURCE_AUTHORITIES
    assert result.source_authority_registry_coverage["coverage"] == 0.5

    changed_source = deepcopy(episodes)
    changed_source[0]["production_judgment"]["editorial"]["taxonomy"]["source_authority"] = "regulatory_filing"
    changed_source[1]["production_judgment"]["editorial"]["taxonomy"]["source_authority"] = "regulatory_filing"
    source_only_change = taxonomy_metric(changed_source)

    assert source_only_change.primary == result.primary
    assert source_only_change.diagnostics == result.diagnostics
    assert source_only_change.source_authority_registry_coverage["coverage"] == 1.0

    missed_subject = taxonomy_metric(
        [_episode("case-miss", "cluster-miss", gold=access_gold, prediction=access_prediction, opened_at_ms=4)]
    )
    assert missed_subject.diagnostics["subject_codes"]["micro_f1"] == 0.0


def test_taxonomy_metric_requires_gold_and_measures_at_sixty_independent_clusters() -> None:
    taxonomy = _gold()
    prediction = _taxonomy()
    episodes = [
        _episode(
            f"case-{index}",
            f"cluster-{index}",
            gold=taxonomy,
            prediction=prediction,
            opened_at_ms=index,
        )
        for index in range(60)
    ]

    assert taxonomy_metric(episodes[:-1]).outcome == "INSUFFICIENT_DATA"
    assert taxonomy_metric(episodes).outcome == "MEASURED"
    mixed = taxonomy_metric([*episodes, episodes[0] | {"case_id": "external-miss", "production_judgment": None}])
    assert (mixed.case_n, mixed.independent_cluster_n, mixed.scored_case_n) == (61, 60, 60)
    assert mixed.outcome == "MEASURED"
    with pytest.raises(ValueError, match="news_taxonomy_metric_gold_missing"):
        taxonomy_metric([episodes[0] | {"accepted_review": {}}])
