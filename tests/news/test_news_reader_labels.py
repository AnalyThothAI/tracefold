"""Blind proxy labels stay separate from owner truth, scores and the model rubric."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from scripts.label_news_reader import (
    GUIDE_VERSION,
    agreement_report,
    blind_case,
    normalize_label,
    review_queue,
)


def record(case: str, *, owner: bool, kind: str = "new_action", push: str = "push", key: bool = True) -> dict[str, Any]:
    return {
        "case_id": case,
        "labeler": "owner" if owner else "claude:test",
        "guide_version": GUIDE_VERSION,
        "reader_input_sha256": f"test-case-{case}",
        "label": {"kind": kind, "push": push, "key": key, "anchor": "none", "note": "test only"},
    }


def test_blind_input_has_source_text_but_no_model_readings_and_anchor_order_restores() -> None:
    original = {
        "case_id": "a",
        "reader_input": {
            "schema_version": "news_reader_input_v3",
            "as_of": "2026-10-02",
            "claim": {
                "statement": "A launch",
                "fields": {"subject": "project", "action": "launch", "mode": "observation", "actor_role": "unknown"},
            },
            "sources": [{"publisher": "fixture", "quote": "A launch"}],
            "messages": ["first", "second"],
        },
        "story_id": "gold-story",
        "inclusion_probability": 0.1,
        "stratum": "production-key",
    }
    before = deepcopy(original)
    blinded, mapping = blind_case(original, lambda order: order.reverse())
    assert blinded == {
        "case_id": "a",
        "as_of": "2026-10-02",
        "statement": "A launch",
        "quotes": ["A launch"],
        "sources": [{"publisher": "fixture", "authority": "unknown"}],
        "messages": [{"id": "m1", "body": "second"}, {"id": "m2", "body": "first"}],
    }
    assert original == before
    response = {
        "case_id": "a",
        "story_id": "proxy-story",
        "label": {"kind": "new_action", "push": "push", "key": False, "anchor": "m1", "note": "new deadline"},
    }
    assert normalize_label(response, mapping)["label"]["anchor"] == "m2"
    response["label"]["anchor"] = "m3"
    with pytest.raises(ValueError, match="blind_label_invalid"):
        normalize_label(response, mapping)
    original["answers"] = {"native": {"p_push": 0.9}}
    with pytest.raises(ValueError, match="scores_or_readings"):
        blind_case(original)


def test_agreement_reports_confusion_kappa_and_owner_only_key_certification() -> None:
    owner = [record("a", owner=True), record("b", owner=True, kind="promotion", push="feed", key=False)]
    proxy = [record("a", owner=False), record("b", owner=False, kind="promotion", push="borderline", key=True)]
    report = agreement_report(owner, proxy)
    assert report["proxy_is_truth"] is False
    assert report["fields"]["kind"]["kappa"] == 1
    assert report["fields"]["push"]["kappa"] == pytest.approx(2 / 3)
    assert report["fields"]["key"]["disagreement_rate"] == 0.5
    assert report["fields"]["key"]["confusion"] == [[0, 1], [0, 1]]
    assert report["key_disagreement_above_20_percent"] is True
    assert report["key_certification_labels"] == "owner only"
    proxy[0]["guide_version"] = "different-guide"
    with pytest.raises(ValueError, match="guide_mismatch"):
        agreement_report(owner, proxy)


def test_review_queue_uses_only_fit_oof_predictions_and_does_not_relabel() -> None:
    proxy = [record("a", owner=False), record("b", owner=False), record("holdout", owner=False)]
    before = deepcopy(proxy)
    fitted = {
        "split": {"fit": ["a", "b"], "certification": ["holdout"]},
        "oof_predictions": [
            {
                "case_id": "a",
                "input_sha256": "test-case-a",
                "guide_version": GUIDE_VERSION,
                "p_push": 0.1,
                "p_key": 0.3,
            },
            {
                "case_id": "b",
                "input_sha256": "test-case-b",
                "guide_version": GUIDE_VERSION,
                "p_push": 0.8,
                "p_key": 0.7,
            },
        ],
    }
    queue = review_queue(proxy, fitted)
    assert [row["case_id"] for row in queue] == ["a", "b"]
    assert proxy == before
    fitted["oof_predictions"].append(
        {
            "case_id": "holdout",
            "input_sha256": "test-case-holdout",
            "guide_version": GUIDE_VERSION,
            "p_push": 0.1,
            "p_key": 0.1,
        }
    )
    with pytest.raises(ValueError, match="not_out_of_fold_fit"):
        review_queue(proxy, fitted)
