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
from scripts.news_reader_io import write_jsonl
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
                "statement": "The release will occur within 4 months.",
                "fields": {"subject": "project", "action": "launch", "mode": "observation", "actor_role": "unknown"},
            },
            "sources": [{"publisher": "fixture", "quote": "within 4 months"}],
            "messages": ["first", "second"],
        },
        "source_texts": ["G7 agrees a coordinated inventory release within 4 months", "Second cited report"],
        "story_id": "gold-story",
        "inclusion_probability": 0.1,
        "stratum": "production-key",
    }
    before = deepcopy(original)
    blinded, mapping = blind_case(original, lambda order: order.reverse())
    assert blinded == {
        "case_id": "a",
        "as_of": "2026-10-02",
        "statement": "The release will occur within 4 months.",
        "quotes": ["within 4 months"],
        "sources": [{"publisher": "fixture", "authority": "unknown"}],
        "source_text": "G7 agrees a coordinated inventory release within 4 months\n---\nSecond cited report",
        "messages": [{"id": "m1", "body": "second"}, {"id": "m2", "body": "first"}],
    }
    assert original == before
    response = {
        "case_id": "a",
        "story_id": "proxy-story",
        "repeat": False,
        "label": {"kind": "new_action", "push": "push", "key": False, "anchor": "m1", "note": "new deadline"},
    }
    assert normalize_label(response, mapping)["label"]["anchor"] == "m2"
    for change, error in (
        ({"label": {**response["label"], "anchor": "m3"}}, "blind_label_invalid"),
        ({"repeat": None}, "blind_label_invalid"),
        ({"repeat": True}, "repeat_requires_anchored_feed"),
    ):
        with pytest.raises(ValueError, match=error):
            normalize_label({**response, **change}, mapping)
    repeat = {**response, "repeat": True, "label": {**response["label"], "push": "feed"}}
    assert normalize_label(repeat, mapping)["repeat"] is True
    with pytest.raises(ValueError, match="scores_or_readings"):
        blind_case({**original, "answers": {"native": {"p_push": 0.9}}})
    with pytest.raises(ValueError, match="source_text_required"):
        blind_case({key: value for key, value in original.items() if key != "source_texts"})


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
        "source_texts": ["A project announced a launch"],
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
            assert b"A project announced a launch" in prompt
            response = {
                "case_id": "compressed",
                "story_id": "proxy-story",
                "repeat": False,
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
    assert record["repeat"] is False and record["guide_version"] == GUIDE_VERSION
    for field in ("sampling_frame", "sampling_unit", "inclusion_probability", "reader_applicable", "pre_reader_reason"):
        assert record[field] == source[field]
    compressed_labels = tmp_path / "proxy.jsonl.gz"
    write_jsonl(compressed_labels, [record])
    assert labels.read_labels(compressed_labels) == [record]


EARLIER_GUIDE = "news_reader_owner_guide_v5:" + "5" * 64


def owner_selection() -> list[dict[str, Any]]:
    frame = {
        "unit": "independent_story_representative",
        "selection_id": "fixture-selection",
        "selection_frozen_before_labels": True,
        "selected_case_ids": ["a", "b"],
    }
    return [
        {
            "case_id": case,
            "claim_ref": f"cl:{case}",
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
            "source_texts": [f"Project {case} sets a new deadline"],
            "story_id": f"story-{case}",
            "sampling_design": "stratified",
            "sampling_unit": frame["unit"],
            "sampling_frame": frame,
            "stratum": "push_region",
            "inclusion_probability": 0.5,
            # The candidate was fitted under an earlier guide; owner labels carry that guide.
            "guide_version": EARLIER_GUIDE,
        }
        for case in ("a", "b")
    ]


def proxy_labels(source: list[dict[str, Any]], *, repeat: str | None = None) -> list[dict[str, Any]]:
    return [
        {
            "case_id": row["case_id"],
            "story_id": f"proxy-{row['case_id']}",
            "repeat": row["case_id"] == repeat,
            "label": {
                "kind": "official_communication",
                "push": "feed" if row["case_id"] == repeat else "push",
                "key": False,
                "anchor": "m2" if row["case_id"] == repeat else "none",
                "note": "proxy note",
            },
            "labeler": "claude:test",
            "guide_version": row["guide_version"],
            "reader_input_sha256": digest(row["reader_input"]),
        }
        for row in source
    ]


def owner_answers(source: list[dict[str, Any]], *, dup: dict[str, str] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "case_id": row["case_id"],
            "push": "push",
            "key": True,
            "dup": (dup or {}).get(row["case_id"]),
            "note": "",
            "guide_version": row["guide_version"],
            "reviewed_at": "2026-10-03T08:17:30.319Z",
        }
        for row in source
    ]


def test_owner_answers_push_and_key_while_kind_anchor_and_duplicates_come_from_the_proxy() -> None:
    source = owner_selection()
    original = deepcopy(source)
    public, manifest = prepare_owner(source, shuffle=lambda order: order.reverse())
    assert source == original
    assert set(public[0]) == {
        "case_id",
        "statement",
        "quotes",
        "sources",
        "source_text",
        "as_of",
        "messages",
        "blind_input_sha256",
    }
    assert public[0]["source_text"] == "Project a sets a new deadline"
    assert public[0]["messages"] == [
        {"id": "m1", "body": "second original receipt"},
        {"id": "m2", "body": "first original receipt"},
    ]
    assert not {"fields", "reader_input", "sampling_frame", "inclusion_probability", "stratum"} & public[0].keys()
    assert manifest["guide_version"] == EARLIER_GUIDE and manifest["selection_id"] == "fixture-selection"
    proxy = proxy_labels(source, repeat="b")
    records = import_owner(source, list(reversed(owner_answers(source, dup={"b": "agree"}))), manifest, proxy)
    first, second = records
    assert [row["case_id"] for row in records] == ["a", "b"]
    assert first["label"] == {
        "kind": "official_communication",
        "anchor": "none",
        "push": "push",
        "key": True,
        "note": "",
    }
    assert first["label_sources"] == {"kind": "proxy", "anchor": "proxy", "push": "owner", "key": "owner"}
    assert first["labeler"] == "owner" and first["proxy"] is False and first["story_id"] == "story-a"
    assert first["guide_version"] == EARLIER_GUIDE and first["reviewed_at"] == "2026-10-03T08:17:30.319Z"
    assert first["reader_input_sha256"] == digest(source[0]["reader_input"])
    assert first["sampling_frame"] == source[0]["sampling_frame"] and first["inclusion_probability"] == 0.5
    # A confirmed proxy duplicate is feed whatever the owner answered for new information.
    assert (second["label"]["push"], second["label"]["key"], second["label"]["anchor"]) == ("feed", False, "m2")
    assert second["owner_answer"]["dup"] == "agree" and second["proxy_source"]["repeat"] is True
    overruled = import_owner(source, owner_answers(source, dup={"b": "disagree"}), manifest, proxy)[1]
    assert (overruled["label"]["push"], overruled["label"]["key"]) == ("push", True)
    assert overruled["label_sources"]["push"] == "owner"


@pytest.mark.parametrize(
    "changed",
    [
        "source",
        "manifest",
        "missing",
        "extra",
        "duplicate",
        "unflagged_dup",
        "unanswered_dup",
        "guide",
        "time",
        "proxy",
    ],
)
def test_owner_import_rejects_changed_material_or_incomplete_frozen_selection(changed: str) -> None:
    source = owner_selection()
    _, manifest = prepare_owner(source)
    human = owner_answers(source)
    proxy = proxy_labels(source)
    if changed == "source":
        source[0]["source_texts"] = ["changed after preparation"]
    elif changed == "manifest":
        manifest["cases"][0]["anchor_mapping"] = {"m1": "m2", "m2": "m2"}
    elif changed == "missing":
        human.pop()
    elif changed == "extra":
        human.append({**human[0], "case_id": "not-selected"})
    elif changed == "duplicate":
        human.append(human[0])
    elif changed == "unflagged_dup":
        human[0]["dup"] = "agree"
    elif changed == "unanswered_dup":
        proxy = proxy_labels(source, repeat="a")
    elif changed == "guide":
        human[0]["guide_version"] = GUIDE_VERSION
    elif changed == "time":
        human[0]["reviewed_at"] = "2026-10-03T08:17:30"
    else:
        proxy[0]["guide_version"] = GUIDE_VERSION
    with pytest.raises(ValueError, match="news_owner_"):
        import_owner(source, human, manifest, proxy)


def test_offline_owner_cli_roundtrip_compressed_files_without_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_path, blind_path = tmp_path / "selection.jsonl.gz", tmp_path / "blind.jsonl.gz"
    manifest_path, human_path, proxy_path, output = (
        tmp_path / "private.json",
        tmp_path / "human.jsonl.gz",
        tmp_path / "proxy.jsonl.gz",
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
    write_jsonl(human_path, owner_answers(owner_selection()))
    write_jsonl(proxy_path, proxy_labels(owner_selection()))
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
            "--proxy",
            str(proxy_path),
            "--output",
            str(output),
        ],
    )
    labels.main()
    assert [row["labeler"] for row in labels.read_labels(output)] == ["owner", "owner"]


def test_agreement_reports_owner_fields_and_duplicate_confirmations() -> None:
    owner = [record("a", owner=True), record("b", owner=True, kind="promotion", push="feed", key=False)]
    owner[1]["owner_answer"] = {"dup": "agree"}
    proxy = [record("a", owner=False), record("b", owner=False, kind="promotion", push="borderline", key=True)]
    proxy[1]["repeat"] = True
    report = agreement_report(owner, proxy)
    assert report["proxy_is_truth"] is False
    assert set(report["fields"]) == {"push", "key"}
    assert report["fields"]["push"]["kappa"] == pytest.approx(2 / 3)
    assert report["fields"]["key"]["disagreement_rate"] == 0.5
    assert report["fields"]["key"]["confusion"] == [[0, 1], [0, 1]]
    assert report["proxy_duplicates"] == {"confirmed": 1, "overruled": 0}
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
