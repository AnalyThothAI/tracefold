"""Blind proxy labels stay separate from owner truth, scores and the model rubric."""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts import label_news_reader as labels
from scripts.label_news_reader import (
    GUIDE_VERSION,
    agreement_report,
    blind_case,
    import_owner,
    normalize_label,
    prepare_owner,
    review_queue,
)
from scripts.news_reader_io import read_jsonl, write_jsonl
from scripts.news_reader_labeling import guide_version, owner_guide
from tracefold.news.notifications.policy import PUSHABLE_KINDS
from tracefold.news.updates.identity import digest


def record(case: str, *, owner: bool, kind: str = "new_action", push: str = "push", key: bool = True) -> dict[str, Any]:
    return {
        "case_id": case,
        "labeler": "owner" if owner else "claude:test",
        "guide_version": GUIDE_VERSION,
        "reader_input_sha256": f"test-case-{case}",
        "label": {"kind": kind, "push": push, "key": key, "anchor": "none", "note": "test only"},
    }


def test_eligibility_change_updates_annotation_rules_and_rejects_old_truth() -> None:
    changed = {**PUSHABLE_KINDS, "self_reported_metric": False}
    assert "self_reported_metric: true" in owner_guide()
    assert "self_reported_metric: false" in owner_guide(changed)
    assert guide_version(changed) != GUIDE_VERSION
    owner = record("a", owner=True)
    proxy = record("a", owner=False)
    proxy["guide_version"] = guide_version(changed)
    with pytest.raises(ValueError, match="guide_mismatch"):
        agreement_report([owner], [proxy])


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


def test_compressed_inputs_keep_sampling_metadata_outside_provider_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = {
        "case_id": "compressed",
        "reader_input": {
            "schema_version": "news_reader_input_v3",
            "as_of": "2026-10-02",
            "claim": {"statement": "A launch", "fields": {"subject": "project", "action": "launch"}},
            "sources": [{"publisher": "fixture", "quote": "A launch"}],
            "messages": [],
        },
        "sampling_design": "stratified",
        "sampling_unit": "claim_decision",
        "stratum": "feed/unknown",
        "inclusion_probability": 0.25,
        "sampling_frame": {"frame_id": "fixture", "unit": "claim_decision", "stratum_sizes": {"feed/unknown": 4}},
        "reader_applicable": True,
        "pre_reader_reason": "reader_unavailable",
        "deterministic_decision": None,
        "original_reason": "reader_feed",
    }
    case_path, output = tmp_path / "cases.jsonl.gz", tmp_path / "proxy.jsonl"
    write_jsonl(case_path, [source])

    class Process:
        returncode = 0

        async def communicate(self, prompt: bytes) -> tuple[bytes, bytes]:
            assert b"original_reason" not in prompt and b"sampling_frame" not in prompt
            response = {
                "case_id": "compressed",
                "story_id": "proxy-story",
                "label": {"kind": "new_action", "push": "push", "key": False, "anchor": "none", "note": "test"},
            }
            return json.dumps({"result": json.dumps([response]), "modelUsage": {"stub": {}}}).encode(), b""

    async def launch(*args: Any, **kwargs: Any) -> Process:
        assert args[0] == "claude" and kwargs["stdin"] == asyncio.subprocess.PIPE
        return Process()

    monkeypatch.setattr(labels.asyncio, "create_subprocess_exec", launch)
    asyncio.run(
        labels.label(argparse.Namespace(input=case_path, output=output, concurrency=1, timeout=5, batch_size=1))
    )
    record = labels.read_labels(output)[0]
    for field in ("sampling_frame", "sampling_unit", "inclusion_probability", "reader_applicable", "pre_reader_reason"):
        assert record[field] == source[field]
    compressed_labels = tmp_path / "proxy.jsonl.gz"
    write_jsonl(compressed_labels, [record])
    assert labels.read_labels(compressed_labels) == [record]


def owner_selection() -> list[dict[str, Any]]:
    frame = {
        "frame_id": "fixture-owner-frame",
        "unit": "independent_story_representative",
        "scope": "holdout",
        "selection_frozen_before_labels": True,
        "selected_case_ids": ["a", "b"],
        "stratum_sizes": {"key/unknown": 4},
        "units": 4,
    }
    return [
        {
            "case_id": case,
            "reader_input": {
                "schema_version": "news_reader_input_v3",
                "as_of": "2026-10-02",
                "claim": {
                    "statement": f"{case} new deadline",
                    "fields": {"subject": "project", "action": "deadline", "mode": "observation"},
                },
                "sources": [{"publisher": "fixture", "quote": f"{case} new deadline"}],
                "messages": ["first original receipt", "second original receipt"],
            },
            "sampling_design": "stratified",
            "sampling_unit": frame["unit"],
            "sampling_frame": frame,
            "stratum": "key/unknown",
            "inclusion_probability": 0.5,
        }
        for case in ("a", "b")
    ]


def human_labels(public: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "case_id": row["case_id"],
            "blind_input_sha256": row["blind_input_sha256"],
            "story_id": f"owner-story-{row['case_id']}",
            "label": {"kind": "new_action", "push": "push", "key": False, "anchor": "m1", "note": "new deadline"},
        }
        for row in public
    ]


def test_owner_preparation_blinds_real_input_and_import_restores_anchors_and_joint_metadata() -> None:
    source = owner_selection()
    original = deepcopy(source)
    public, manifest = prepare_owner(source, shuffle=lambda order: order.reverse())
    assert source == original
    assert set(public[0]) == {"case_id", "statement", "quotes", "sources", "as_of", "messages", "blind_input_sha256"}
    assert public[0]["messages"] == [
        {"id": "m1", "body": "second original receipt"},
        {"id": "m2", "body": "first original receipt"},
    ]
    assert not {"fields", "reader_input", "sampling_frame", "inclusion_probability", "stratum"} & public[0].keys()
    assert manifest["cases"][0]["reader_input_sha256"] == digest(source[0]["reader_input"])
    records = import_owner(source, list(reversed(human_labels(public))), manifest)
    assert [row["case_id"] for row in records] == ["a", "b"]
    assert records[0]["label"]["anchor"] == "m2"
    assert records[0]["labeler"] == "owner" and records[0]["proxy"] is False
    assert records[0]["guide_version"] == GUIDE_VERSION
    assert records[0]["reader_input_sha256"] == digest(source[0]["reader_input"])
    assert records[0]["sampling_frame"] == source[0]["sampling_frame"]
    assert records[0]["inclusion_probability"] == 0.5


@pytest.mark.parametrize("changed", ["source", "blind_sha", "manifest", "missing", "extra", "duplicate"])
def test_owner_import_rejects_changed_material_or_incomplete_frozen_selection(changed: str) -> None:
    source = owner_selection()
    public, manifest = prepare_owner(source)
    human = human_labels(public)
    if changed == "source":
        source[0]["reader_input"]["sources"][0]["quote"] += " changed"
    elif changed == "blind_sha":
        human[0]["blind_input_sha256"] = "wrong"
    elif changed == "manifest":
        manifest["cases"][0]["anchor_mapping"] = {"m1": "m2", "m2": "m2"}
    elif changed == "missing":
        human.pop()
    elif changed == "extra":
        human.append({**human[0], "case_id": "not-selected"})
    else:
        human.append(human[0])
    with pytest.raises(ValueError, match="news_owner_blind_"):
        import_owner(source, human, manifest)


def test_offline_owner_cli_roundtrip_compressed_files_without_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_path, blind_path = tmp_path / "selection.jsonl.gz", tmp_path / "blind.jsonl.gz"
    manifest_path, human_path, output = (
        tmp_path / "private.json",
        tmp_path / "human.jsonl.gz",
        tmp_path / "owner.jsonl.gz",
    )
    write_jsonl(source_path, owner_selection())

    def no_provider(*_: Any, **__: Any) -> None:
        raise AssertionError("offline owner commands must not construct a provider call")

    monkeypatch.setattr(labels.asyncio, "create_subprocess_exec", no_provider)
    monkeypatch.setattr(
        "sys.argv",
        [
            "label_news_reader",
            "prepare-owner",
            "--input",
            str(source_path),
            "--output",
            str(blind_path),
            "--manifest",
            str(manifest_path),
        ],
    )
    labels.main()
    write_jsonl(human_path, human_labels(read_jsonl(blind_path)))
    monkeypatch.setattr(
        "sys.argv",
        [
            "label_news_reader",
            "import-owner",
            "--input",
            str(source_path),
            "--manifest",
            str(manifest_path),
            "--labels",
            str(human_path),
            "--output",
            str(output),
        ],
    )
    labels.main()
    assert [row["labeler"] for row in labels.read_labels(output)] == ["owner", "owner"]


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
