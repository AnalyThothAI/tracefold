"""Frozen export integrity, honest probability sampling and gzip tool seams."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts.export_news_reader_cases import (
    DECISIONS_SQL,
    RECEIPTS_SQL,
    SOURCE_TEXT_CHARS_MAX,
    UPDATES_SQL,
    bind_input_provenance,
    decision_frame,
    export_cases,
    receipt_as_of,
    restore_case,
    sample_frame,
)
from scripts.label_news_reader import blind_case
from scripts.news_reader_io import dataset_sha256, read_jsonl, write_jsonl
from tests.support.news_event_updates import STAMP, first_update, raised_update
from tracefold.news.notifications.reader import ReaderInput
from tracefold.news.updates.identity import digest


def facts() -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    update = first_update("export-original")
    messages = ["second selected receipt", "first selected receipt"]
    frozen = ReaderInput.of(update.claims[0], update, messages)
    decision = {
        "notification_id": "original-decision",
        "event_id": update.event_id,
        "update_ref": update.ref,
        "decided_at_ms": STAMP + 100,
        "input_snapshot": {},
        "plan": {
            "update_ref": update.ref,
            "reader_revision": "frozen-revision",
            "reader_identity": "historical-v3",
            "compared_receipts": [
                {"intent_id": intent, "payload_sha256": digest(body)}
                for intent, body in zip(["receipt-2", "receipt-1"], messages, strict=True)
            ],
            "claim_decisions": [
                {
                    "claim_ref": update.claims[0].ref,
                    "decision": "not_notified",
                    "reason": "reader_feed",
                    "reader": {
                        "novelty": "unlinked",
                        "render": "full",
                        "message_intents": ["receipt-2", "receipt-1"],
                        "input_digest": frozen.digest,
                        "judgment": {"importance": {"value": 2.2, "probabilities": [0, 0, 0.8, 0.2, 0]}},
                    },
                }
            ],
        },
    }
    receipts = [
        {
            "notification_id": f"prior-{index}",
            "intent_id": intent,
            "state": "sent",
            "claim_refs": [f"prior-{index}"],
            "card": {"body": body, "payload_sha256": digest(body)},
            "created_at_ms": STAMP - 10,
            "attempted_at_ms": STAMP - 9,
            "settled_at_ms": STAMP - 8,
        }
        for index, (intent, body) in enumerate(zip(["receipt-2", "receipt-1"], messages, strict=True))
    ]
    return (
        [decision],
        {
            update.ref: {
                "update_ref": update.ref,
                "event_id": update.event_id,
                "document": update.model_dump(mode="json"),
            }
        },
        receipts,
    )


def selected(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    rows, _ = sample_frame(
        decision_frame(decisions),
        from_ms=STAMP,
        to_ms=STAMP + 1_000,
        per_stratum=1,
        key_per_stratum=2,
        seed=805,
    )
    return rows[0]


def test_export_restores_original_version_and_receipt_order_and_blinds_metadata(tmp_path: Path) -> None:
    decisions, updates, receipts = facts()
    newer = raised_update(first_update("export-original"))
    updates[newer.ref] = {"event_id": newer.event_id, "document": newer.model_dump(mode="json")}
    restored = restore_case(selected(decisions), updates, list(reversed(receipts)))
    assert bind_input_provenance([restored]) == {"historical-v3": {"verified_original": 1}}
    assert restored["reader_input"]["claim"]["statement"] == first_update("export-original").claims[0].statement
    # The cited evidence text travels outside the model input for labelers; the uncited rival does not.
    assert restored["source_texts"] == ["Agency announces 25% tariff on steel imports effective October 1."]
    assert restored["reader_input"]["messages"] == ["second selected receipt", "first selected receipt"]
    assert restored["reader_input_sha256"] == restored["recorded_input_sha256"]
    assert restored["reader_input_provenance"] == "verified_original"
    assert restored["original_reason"] == "reader_feed" and restored["reader_applicable"] is True
    assert restored["deterministic_decision"] is None
    assert restored["report_kind_stratum"] == "unknown"
    assert "story_id" not in restored
    blinded, _ = blind_case(restored)
    assert blinded["source_text"] == restored["source_texts"][0]
    assert not ({"original_reason", "decision_ref", "sampling_frame", "reader_input_sha256"} & blinded.keys())
    assert "fields" not in blinded
    output = tmp_path / "cases.jsonl.gz"
    checksum = write_jsonl(output, [restored])
    compressed = output.read_bytes()
    assert read_jsonl(output) == [restored]
    assert checksum == dataset_sha256([restored])
    assert write_jsonl(output, [restored]) == checksum and output.read_bytes() == compressed


@pytest.mark.parametrize("broken", ["payload", "compared", "future", "missing"])
def test_selected_input_or_receipt_drift_fails_instead_of_dropping_and_resampling(broken: str) -> None:
    decisions, updates, receipts = facts()
    if broken == "payload":
        receipts[0]["card"]["body"] += " changed"
    elif broken == "compared":
        decisions[0]["plan"]["compared_receipts"][0]["payload_sha256"] = "wrong"
    elif broken == "future":
        receipts[0]["settled_at_ms"] = STAMP + 101
    else:
        receipts.pop(0)
    with pytest.raises(ValueError, match=r"original_(input|payload|message)"):
        restore_case(selected(decisions), updates, receipts)


def test_input_rendering_is_settled_per_recorded_judge_program() -> None:
    decisions, updates, receipts = facts()
    verified = restore_case(selected(decisions), updates, receipts)
    drifted = dict(verified, case_id="drifted", recorded_input_sha256="recorded-by-the-same-program")
    # A program that reproduces one input renders as this code does: any other mismatch is drift.
    with pytest.raises(ValueError, match="original_input_mismatch"):
        bind_input_provenance([dict(verified), dict(drifted)])
    # A program none of whose inputs reproduce answered an earlier rendering; rebuild, never digest-verify.
    older = dict(drifted, reader_identity="pre-v3-judge")
    assert bind_input_provenance([dict(verified), older]) == {
        "historical-v3": {"verified_original": 1},
        "pre-v3-judge": {"rebuilt": 1},
    }
    assert older["reader_input_provenance"] == "rebuilt"


def test_plans_before_compared_receipts_still_bind_each_body_to_its_sent_payload() -> None:
    decisions, updates, receipts = facts()
    del decisions[0]["plan"]["compared_receipts"]
    restored = restore_case(selected(decisions), updates, receipts)
    assert restored["reader_input"]["messages"] == ["second selected receipt", "first selected receipt"]
    receipts[0]["card"]["body"] += " changed"
    with pytest.raises(ValueError, match="original_payload_mismatch"):
        restore_case(selected(decisions), updates, receipts)


def test_cited_source_text_is_capped_per_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    decisions, updates, receipts = facts()
    assert SOURCE_TEXT_CHARS_MAX == 2_500
    monkeypatch.setattr("scripts.export_news_reader_cases.SOURCE_TEXT_CHARS_MAX", 6)
    assert restore_case(selected(decisions), updates, receipts)["source_texts"] == ["Agency"]


def test_fixed_rules_are_marked_unasked_and_future_settlement_preserves_inflight() -> None:
    decisions, updates, receipts = facts()
    row = decisions[0]["plan"]["claim_decisions"][0]
    row.update(reason="protected_listing", decision="notify")
    row.pop("reader")
    restored = restore_case(selected(decisions), updates, receipts)
    bind_input_provenance([restored])
    assert restored["reader_applicable"] is False
    assert restored["pre_reader_reason"] == "protected_listing"
    assert restored["deterministic_decision"] == "notify"
    assert restored["reader_input_provenance"] == "derived_unasked"
    assert restored["recorded_input_sha256"] is None
    assert restored["reader_input"]["messages"] == []
    later_failed = dict(receipts[0], state="terminal", settled_at_ms=STAMP + 110)
    assert receipt_as_of(later_failed, STAMP + 100).state == "sending"
    assert receipt_as_of(later_failed, STAMP + 120) is None
    later_failed["attempted_at_ms"] = STAMP + 105
    with pytest.raises(ValueError, match="receipt_history_unverifiable"):
        receipt_as_of(later_failed, STAMP + 100)
    assert receipt_as_of(dict(later_failed, created_at_ms=STAMP + 105), STAMP + 100) is None


@pytest.mark.parametrize("state", ["pending", "dead"])
def test_retained_not_sent_attempt_is_not_historical_inflight(state: str) -> None:
    decisions, updates, receipts = facts()
    claim_ref = decisions[0]["plan"]["claim_decisions"][0]["claim_ref"]
    retry = dict(
        receipts[0],
        intent_id="retained-unsent-attempt",
        claim_refs=[claim_ref],
        state=state,
        settled_at_ms=None,
        settlement={"state": "not_sent", "retryable": True},
    )
    with pytest.raises(ValueError, match="receipt_history_unverifiable"):
        restore_case(selected(decisions), updates, [*receipts, retry])


def test_current_sending_and_final_settlement_bound_provable_latest_attempt() -> None:
    _, _, receipts = facts()
    receipt = dict(receipts[0], state="sending", attempted_at_ms=STAMP + 90, settled_at_ms=None)
    assert receipt_as_of(receipt, STAMP + 100).state == "sending"
    final = dict(receipt, state="sent", settled_at_ms=STAMP + 110)
    assert receipt_as_of(final, STAMP + 100).state == "sending"
    assert receipt_as_of(final, STAMP + 110).state == "sent"
    overwritten = dict(final, attempted_at_ms=STAMP + 105)
    with pytest.raises(ValueError, match="receipt_history_unverifiable"):
        receipt_as_of(overwritten, STAMP + 100)
    never_attempted = dict(receipt, state="pending", attempted_at_ms=None)
    assert receipt_as_of(never_attempted, STAMP + 100) is None


def test_historical_held_is_not_inferred_from_novelty_or_rendering() -> None:
    decisions, _, _ = facts()
    record = decisions[0]["plan"]["claim_decisions"][0]["reader"]
    record.update(novelty="increment", render="increment")
    assert decision_frame(decisions)[0]["decision_band"] == "feed"
    record["scores"] = {"held": True}
    assert decision_frame(decisions)[0]["decision_band"] == "held"


def test_full_frame_strata_probabilities_priority_and_seed_are_frozen() -> None:
    base, _, _ = facts()
    decisions = []
    for i in range(9):
        row = deepcopy(base[0])
        row["notification_id"] = f"decision-{i}"
        if i >= 5:
            row["plan"]["claim_decisions"][0].update(reason="reader_key", decision="notify")
        decisions.append(row)
    frame = decision_frame(decisions)
    rows, manifest = sample_frame(
        frame,
        from_ms=STAMP,
        to_ms=STAMP + 1_000,
        per_stratum=2,
        key_per_stratum=3,
        seed=805,
    )
    again, _ = sample_frame(
        frame,
        from_ms=STAMP,
        to_ms=STAMP + 1_000,
        per_stratum=2,
        key_per_stratum=3,
        seed=805,
    )
    assert rows == again and len(rows) == 5
    assert manifest["sampling_frame"]["stratum_sizes"] == {"feed/unknown": 5, "key/unknown": 4}
    assert manifest["sample_counts"] == {"feed/unknown": 2, "key/unknown": 3}
    assert manifest["sampling_frame"]["unit"] == "claim_decision"
    assert manifest["certification_ready"] is False
    assert {row["inclusion_probability"] for row in rows if row["decision_band"] == "feed"} == {0.4}
    assert {row["inclusion_probability"] for row in rows if row["decision_band"] == "key"} == {0.75}


def test_export_reader_only_uses_bound_original_fact_queries() -> None:
    decisions, updates, receipts = facts()

    class Connection:
        rows: list[dict[str, Any]]

        def execute(self, sql: str, parameters: tuple[Any, ...]) -> Connection:
            assert "news_events" not in sql and "FOR UPDATE" not in sql
            if sql == DECISIONS_SQL:
                assert parameters == (STAMP, STAMP + 1_000)
                self.rows = decisions
            elif sql == UPDATES_SQL:
                assert parameters == ([decisions[0]["update_ref"]],)
                self.rows = list(updates.values())
            else:
                assert sql == RECEIPTS_SQL and parameters[0] == STAMP + 1_000
                self.rows = receipts
            return self

        def fetchall(self) -> list[dict[str, Any]]:
            return self.rows

    rows, manifest = export_cases(Connection(), from_ms=STAMP, to_ms=STAMP + 1_000, census=True)
    assert manifest["selected_units"] == 1 and manifest["reader_applicable_units"] == 1
    assert manifest["census"] is True and rows[0]["inclusion_probability"] == 1
    assert manifest["input_provenance_by_judge_program"] == {"historical-v3": {"verified_original": 1}}
    assert manifest["dataset_sha256"] == dataset_sha256(rows)
