"""Owner business intent with synthetic split evidence; old real answers remain historical evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracefold.news.notifications.novelty import ReaderNovelty
from tracefold.news.notifications.policy import reader_decision
from tracefold.news.notifications.reader import (
    REPORT_KIND_OPTIONS,
    AnchorEvidence,
    InterruptEvidence,
    MaterialityEvidence,
    ReaderJudgment,
    ReportKind,
    ReportKindEvidence,
)
from tracefold.news.outcome import event_outcome
from tracefold.news.update_view import claim_reasons_zh, decode_plan, notification_view
from tracefold.news.updates.contracts import ClaimFields

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/news/reader_791_business_regressions.json").read_text())
SPLIT_FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures/news/reader_805_business_regressions.json").read_text()
)


def synthetic_judgment(
    kind: ReportKind, materiality: int, interrupt: float, backend: str, anchor: AnchorEvidence | None = None
) -> ReaderJudgment:
    return ReaderJudgment.model_validate(
        {
            "status": "available",
            "backend": backend,
            "identity": "synthetic_805_business_evidence",
            "report_kind": ReportKindEvidence(
                value=kind,
                probabilities={value: 1.0 if value == kind else 0.0 for value, _ in REPORT_KIND_OPTIONS},
                confidence=0.9,
            ),
            "materiality": MaterialityEvidence(
                value=materiality,
                probabilities=tuple(1.0 if index == materiality else 0.0 for index in range(4)),
                confidence=0.9,
            ),
            "interrupt": InterruptEvidence(probabilities=(1 - interrupt, interrupt), confidence=0.9),
            "anchor": anchor,
        }
    )


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["case_id"])
@pytest.mark.usefixtures("synthetic_reader_calibration")
def test_retained_owner_business_intent_with_synthetic_split_evidence(case: dict) -> None:
    # Existing anchor evidence is unchanged; no old importance score is converted into new model evidence.
    anchor = None if case["judgment"]["anchor"] is None else AnchorEvidence.model_validate(case["judgment"]["anchor"])
    judgment = synthetic_judgment(
        "background" if case["expected_outcome"] == "feed" else "official_communication", 2, 0.1, "native", anchor
    )
    result = reader_decision(
        ReaderNovelty.model_validate(case["novelty"]),
        judgment,
        first_available_at_ms=case["first_available_at_ms"],
        message_intents=case["message_intents"],
        claim_fields=ClaimFields.model_validate(case["fields"]),
    )
    assert judgment.status == "available" and judgment.backend == "native"
    assert result.outcome == ("ineligible" if case["expected_outcome"] == "feed" else case["expected_outcome"])
    assert result.anchor_intent_id == case["expected_anchor"]


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["case_id"])
def test_old_real_evidence_is_rejected_at_runtime_and_retained_read_only(case: dict) -> None:
    with pytest.raises(ValidationError):
        ReaderJudgment.model_validate(case["judgment"])
    raw = {
        "action": "no_notification",
        "reason": "no_uncovered_actionable_claims",
        "update_ref": "historical-update",
        "claim_decisions": [
            {
                "claim_ref": case["case_id"],
                "decision": "not_notified",
                "reason": "reader_feed",
                "reader": {"novelty": "unlinked", "judgment": case["judgment"]},
            }
        ],
        "channel": "news",
        "reader_revision": "historical-reader",
        "reader_identity": case["judgment"]["identity"],
        "input_digest": case["input_sha256"],
    }
    assert decode_plan(raw) is None
    view = notification_view(
        {"origin": "reader_v2", "plan": raw, "state": "done", "content_revision": "historical", "updated_at_ms": 1},
        statements={case["case_id"]: case["statement"]},
    )
    assert view is not None and view["plan_error_code"] is None
    row = view["plan"]["claim_decisions"][0]
    assert row["historical_judgment"] == case["judgment"]
    assert row["report_kind"] is None and row["p_push"] is None and "importance" not in row


def test_historical_reasons_and_known_receipts_remain_consistent_across_read_paths() -> None:
    from tracefold.news.storage.update_reads import EVENT_KNOWN_RECEIPTS_SQL, attach_earlier_receipts

    original = FIXTURE["cases"][0]
    raw = {
        "action": "no_notification",
        "reason": "no_uncovered_actionable_claims",
        "update_ref": "historical",
        "channel": "news",
        "reader_revision": "historical",
        "reader_identity": "historical",
        "input_digest": "historical",
        "claim_decisions": [
            {
                "claim_ref": "legacy",
                "decision": "not_notified",
                "reason": "reader_feed",
                "reader": {"novelty": "unlinked", "judgment": original["judgment"]},
            },
            {
                "claim_ref": "known",
                "decision": "not_notified",
                "reason": "known_to_reader",
                "reader": {
                    "novelty": "known",
                    "link_path": [
                        {"current_ref": "known", "previous_ref": "prior", "relation": "equivalent", "asserted_at_ms": 1}
                    ],
                },
            },
        ],
    }
    work = {"origin": "reader_v2", "state": "done", "plan": raw, "content_revision": "historical", "updated_at_ms": 10}
    view = notification_view(work, statements={})
    assert view is not None and view["plan_error_code"] is None
    expected = "旧版模型判断：只进信息流 · 另 1 件：读者已收到同一事实"
    assert view["plan"]["claim_decisions"][0]["reason_zh"] == "旧版模型判断：只进信息流"
    assert claim_reasons_zh(raw["claim_decisions"]) == expected
    assert claim_reasons_zh(view["plan"]["claim_decisions"]) == expected
    outcome = event_outcome(
        admission="candidate",
        delivery=None,
        adopted=True,
        notification={"state": "done", "action": raw["action"], "claim_decisions": raw["claim_decisions"]},
    )
    assert outcome.reason_zh == expected

    class Connection:
        def execute(self, sql: str, parameters: tuple) -> Connection:
            assert sql == EVENT_KNOWN_RECEIPTS_SQL and parameters == (["prior"], 10)
            return self

        def fetchall(self) -> list[dict]:
            return [
                {
                    "claim_ref": "prior",
                    "intent_id": "receipt",
                    "event_id": "prior-event",
                    "card": {"headline_zh": "旧标题", "body": "旧正文"},
                    "settled_at_ms": 2,
                }
            ]

    attach_earlier_receipts(Connection(), work, view)
    assert view["plan"]["claim_decisions"][1]["earlier"]["body"] == "旧正文"
    assert claim_reasons_zh(view["plan"]["claim_decisions"]) == expected
    assert view["plan"]["claim_decisions"][0]["historical_judgment"] == original["judgment"]
    assert claim_reasons_zh([{"decision": "not_notified", "reason": "reader_feed"}]) == ("推送概率未达要求，只进信息流")


@pytest.mark.usefixtures("synthetic_reader_calibration")
@pytest.mark.parametrize("backend", ["native", "generated"])
@pytest.mark.parametrize("case", SPLIT_FIXTURE["cases"], ids=lambda case: case["case_id"])
def test_split_business_policy(case: dict, backend: str) -> None:
    anchored = case.get("anchor") == "m1"
    anchor = AnchorEvidence(probabilities={"m1": 1.0, "none": 0.0}, confidence=0.9) if anchored else None
    judgment = synthetic_judgment(case["kind"], case["materiality"], case["interrupt"], backend, anchor)
    result = reader_decision(
        ReaderNovelty(novelty="unlinked"),
        judgment,
        first_available_at_ms=1,
        message_intents=("earlier",) if anchored else (),
    )
    assert result.outcome == (
        "ineligible" if case["kind"] in {"promotion", "commentary", "background"} else case["expected_outcome"]
    )
    assert result.anchor_intent_id == ("earlier" if anchored else None)
    assert result.scores is not None and result.scores.certification_status == "certified"
