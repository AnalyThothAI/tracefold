"""The EventUpdate read projection, outcome and timeline (#706), pure: no PostgreSQL, no provider."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.support.news_event_updates import first_update, notify_plan, raised_update, silent_plan
from tracefold.news.outcome import OUTCOME_GROUP, event_outcome
from tracefold.news.timeline import event_timeline, reader_delivery
from tracefold.news.update_view import (
    UPDATE_DECODE_ERROR,
    claim_reasons_zh,
    event_update_view,
    intent_views,
    notification_view,
    previous_content_refs,
    semantic_state,
)
from tracefold.news.updates.identity import canonical_json

NOW = 1_800_000_000_000


def _document(update: Any) -> dict[str, Any]:
    return json.loads(canonical_json(update))


def _outcome(**over: Any) -> Any:
    return event_outcome(
        admission=over.pop("admission", "candidate"),
        published_at_ms=over.pop("published_at_ms", NOW),
        triage=over.pop("triage", None),
        delivery=over.pop("delivery", None),
        delivery_queue=over.pop("delivery_queue", None),
        opened_at_ms=NOW,
        now_ms=NOW + 60_000,
        **over,
    )


_DONE = {"wanted_revision": 1, "done_revision": 1, "last_outcome": "adopted", "last_error_code": None}


@pytest.mark.parametrize(
    ("over", "kind", "group"),
    [
        ({"semantic": {"wanted_revision": 2, "done_revision": 1}}, "queued_semantic", "pending"),
        (
            {"semantic": {"wanted_revision": 1, "done_revision": None, "last_outcome": "failed"}},
            "semantic_failed",
            "held",
        ),
        ({"semantic": _DONE}, "no_update", "held"),
        ({"semantic": _DONE, "adopted": True}, "queued_notification", "pending"),
        (
            {"semantic": _DONE, "adopted": True, "notification": {"state": "pending", "action": "unresolved"}},
            "notification_deferred",
            "pending",
        ),
        (
            {"semantic": _DONE, "adopted": True, "notification": {"state": "done", "action": "notify"}},
            "pending_delivery",
            "pending",
        ),
        (
            {
                "semantic": _DONE,
                "adopted": True,
                "notification": {
                    "state": "done",
                    "action": "no_notification",
                    "claim_decisions": [
                        {"claim_ref": "a", "decision": "not_notified", "reason": "mode_commentary"},
                        {"claim_ref": "b", "decision": "not_notified", "reason": "mode_commentary"},
                        {"claim_ref": "c", "decision": "not_notified", "reason": "covered_by_sent_receipt"},
                    ],
                },
            },
            "not_notified",
            "held",
        ),
        (
            {"semantic": _DONE, "adopted": True, "delivery": {"state": "sent", "plan_key": True}},
            "delivered",
            "pushed",
        ),
        ({"semantic": _DONE, "adopted": True, "delivery": {"state": "ambiguous"}}, "delivery_ambiguous", "held"),
        (
            {
                "semantic": _DONE,
                "adopted": True,
                "delivery": {"state": "terminal", "error_code": "delivery_unavailable"},
                "delivery_queue": {"state": "pending"},
            },
            "pending_delivery",
            "pending",
        ),
        (
            {"semantic": _DONE, "adopted": True, "delivery_queue": {"state": "dead", "error_code": "x"}},
            "delivery_failed",
            "held",
        ),
    ],
)
def test_the_event_update_path_has_one_named_outcome_per_state(over: dict[str, Any], kind: str, group: str) -> None:
    outcome = _outcome(**over)

    assert (outcome.kind, outcome.group) == (kind, group)
    assert OUTCOME_GROUP[kind] == group
    assert outcome.text_zh


def test_outcome_texts_name_the_key_card_and_the_reasons_nothing_was_sent() -> None:
    assert _outcome(semantic=_DONE, adopted=True, delivery={"state": "sent", "plan_key": True}).text_zh == (
        "已推送（重点）"
    )
    silent = _outcome(
        semantic=_DONE,
        adopted=True,
        notification={
            "state": "done",
            "action": "no_notification",
            "claim_decisions": [
                {"decision": "not_notified", "reason": "mode_commentary"},
                {"decision": "not_notified", "reason": "mode_commentary"},
                {"decision": "not_notified", "reason": "covered_by_sent_receipt"},
            ],
        },
    )
    assert silent.reason_zh == "评论 ×2 · 已送达内容已覆盖"
    # A failed newer revision never hides the head the Event already has.
    failed_after_head = _outcome(
        semantic={"wanted_revision": 2, "done_revision": 1, "last_outcome": "failed"},
        adopted=True,
        notification={"state": "done", "action": "notify"},
    )
    assert failed_after_head.kind == "pending_delivery"


def test_a_legacy_verdict_outcome_is_untouched_by_the_update_path() -> None:
    legacy = {"final_decision": "escalate", "created_at_ms": NOW, "published_at_ms": NOW}
    assert _outcome(triage=legacy, delivery={"state": "sent"}).text_zh == "已推送（重点）"
    assert _outcome(triage=legacy).kind == "pending_delivery"
    assert _outcome(triage={**legacy, "final_decision": "drop"}).kind == "dropped"
    # Semantic work outranks the history: a legacy Event re-opened by new evidence is on the new path.
    assert _outcome(triage={**legacy, "final_decision": "drop"}, semantic={"wanted_revision": 2}).kind == (
        "queued_semantic"
    )


def test_semantic_state_is_the_workers_own_failure_or_an_unfinished_revision() -> None:
    assert semantic_state({"wanted_revision": 3, "done_revision": 2}) == "pending"
    assert semantic_state({"wanted_revision": 2, "done_revision": 2}) == "done"
    assert semantic_state({"wanted_revision": 2, "done_revision": 1, "last_outcome": "failed"}) == "failed"
    assert claim_reasons_zh([{"decision": "notify", "reason": "actionable_content"}]) == ""


def test_the_view_reports_an_undecodable_head_instead_of_guessing_it() -> None:
    document = _document(first_update("ev-1"))
    document["content_revision"] = "0" * 64

    assert event_update_view({"document": document}, previous_claims={}, sent_headline=None) is None
    assert previous_content_refs(document) == []
    assert UPDATE_DECODE_ERROR == "news_event_update_undecodable"


def test_a_change_whose_earlier_claim_was_not_found_keeps_its_refs_and_an_unknown_statement() -> None:
    head = first_update("ev-1")
    raised = raised_update(head)

    view = event_update_view({"document": _document(raised)}, previous_claims={}, sent_headline=None)

    assert view is not None
    (change,) = view["changes"]
    assert change["kind"] == "parameter_change" and change["previous_ref"] == head.claims[0].ref
    assert change["previous_content_ref"] == head.ref
    assert change["previous_statement"] is None and change["previous_event_id"] is None
    # No card was sent: the headline is the head's first unretired claim, and says so.
    assert (view["headline"], view["headline_source"]) == (head.claims[0].statement, "claim")


def test_intents_join_their_queue_and_ledger_rows_and_the_ledger_outranks() -> None:
    head = first_update("ev-1")
    plan = notify_plan(head)
    queued = {
        "intent_id": plan.intent_id,
        "kind": "update",
        "state": "pending",
        "attempts": 1,
        "enqueued_at_ms": NOW,
        "content_revision": head.content_revision,
        "claim_refs": list(plan.selected_claim_refs),
        "plan_key": False,
    }
    ledger = queued | {
        "state": "sending",
        "attempted_at_ms": NOW + 1,
        "settled_at_ms": None,
        "created_at_ms": NOW + 1,
        "card": {"headline_zh": "关税 25%"},
        "body": "关税 25%",
        "receipt": None,
    }
    legacy = {"intent_id": "legacy_intent:x", "kind": "first", "state": "sent"}

    (only_queued,) = intent_views([queued], [legacy])
    (both,) = intent_views([queued], [legacy, ledger])

    assert (only_queued["state"], only_queued["attempts"], only_queued["body"]) == ("queued", 1, None)
    assert (both["state"], both["body"], both["headline_zh"]) == ("sending", "关税 25%", "关税 25%")
    assert (
        intent_views([queued | {"state": "dead", "error_code": "news_delivery_attempts_exhausted"}], [])[0]["state"]
        == "dead"
    )


def test_notification_view_names_every_claim_decision_and_flags_an_undecodable_plan() -> None:
    head = first_update("ev-1")
    plan = silent_plan(head)
    statements = {claim.ref: claim.statement for claim in head.claims}
    work = {
        "state": "done",
        "content_revision": head.content_revision,
        "plan": _document(plan),
        "attempts": 0,
        "updated_at_ms": NOW,
    }

    view = notification_view(work, statements=statements)
    broken = notification_view(work | {"plan": {"action": "notify"}}, statements=statements)

    assert view is not None and view["plan"] is not None
    assert view["plan"]["action_zh"] == "不通知"
    assert [(row["decision_zh"], row["reason_zh"]) for row in view["plan"]["claim_decisions"]] == [("不通知", "评论")]
    assert view["plan"]["claim_decisions"][0]["statement"] == head.claims[0].statement
    assert broken is not None and broken["plan"] is None
    assert broken["plan_error_code"] == "news_notification_plan_undecodable"


def test_the_representative_reader_card_is_the_latest_sent_one() -> None:
    rows = [
        {"intent_id": "a", "kind": "update", "state": "sent", "created_at_ms": 1},
        {"intent_id": "b", "kind": "update", "state": "terminal", "created_at_ms": 3},
        {"intent_id": "c", "kind": "update", "state": "sent", "created_at_ms": 2},
        {"intent_id": "d", "kind": "followup", "state": "sent", "created_at_ms": 9},
    ]

    assert reader_delivery(rows)["intent_id"] == "c"  # type: ignore[index]
    assert reader_delivery(rows[1:2])["intent_id"] == "b"  # type: ignore[index]
    assert reader_delivery(rows[3:]) is None


def test_the_timeline_narrates_evidence_semantics_the_plan_and_the_intent_in_clock_order() -> None:
    head = first_update("ev-1", adopted_at_ms=NOW + 3_000)
    plan = notify_plan(head, key=True)
    event = {
        "event_id": "ev-1",
        "admission": "candidate",
        "opened_at_ms": NOW,
        "published_at_ms": NOW,
        "member_count": 1,
        "reporting_origin": "wire",
        "ingest_mode": "live",
    }
    notification = notification_view(
        {
            "state": "done",
            "content_revision": head.content_revision,
            "plan": _document(plan),
            "attempts": 0,
            "updated_at_ms": NOW + 4_000,
        },
        statements={claim.ref: claim.statement for claim in head.claims},
    )
    ledger = {
        "intent_id": plan.intent_id,
        "kind": "update",
        "state": "sent",
        "error_code": None,
        "attempted_at_ms": NOW + 5_000,
        "settled_at_ms": NOW + 5_500,
        "created_at_ms": NOW + 5_000,
        "content_revision": head.content_revision,
        "claim_refs": list(plan.selected_claim_refs),
        "plan_key": True,
        "card": {"headline_zh": "关税 25%"},
        "body": "关税 25%",
    }

    outcome, steps = event_timeline(
        event=event,
        members=[],
        verdicts=[],
        deliveries=[ledger],
        semantic={"wanted_revision": 1, "done_revision": 1, "updated_at_ms": NOW + 3_000},
        adopted=True,
        notification={"state": "done", "action": "notify"},
        evidence_snapshots=[{"evidence_version": 1, "created_at_ms": NOW + 1_000, "evidence_sha256": "e" * 64}],
        revisions=[
            {
                "content_revision": head.content_revision,
                "input_revision": 1,
                "previous_content_revision": None,
                "adopted_at_ms": NOW + 3_000,
                "observation_result_id": "result-1",
                "change_kinds": ["new_fact"],
                "claim_n": 1,
            }
        ],
        observations=[
            {"result_id": "result-1", "input_revision": 1, "program_identity": "p", "completed_at_ms": NOW + 2_900}
        ],
        notification_view=notification,
        intents=intent_views([], [ledger]),
        now_ms=NOW + 10_000,
    )

    assert (outcome.kind, outcome.text_zh) == ("delivered", "已推送（重点）")
    assert [step["stage"] for step in steps] == [
        "received",
        "gate",
        "evidence",
        "semantic",
        "semantic",
        "notify",
        "delivery",
    ]
    assert "triage" not in {step["stage"] for step in steps}
    assert steps[4]["summary_zh"] == "新事实 · 1 条命题"
    assert steps[5]["summary_zh"] == "通知 1 条命题 · 重点 · 有命题未被已送达内容覆盖"
    assert steps[6]["summary_zh"] == "已送达 · 1 条命题"
