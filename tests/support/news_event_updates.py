"""Real EventUpdate revisions, plans and intents for read-side tests (#706).

Every value is built through the exact core contracts (`assemble_update`, `NotificationPlan`), so a read
test exercises the documents the writer produces rather than a hand-written look-alike. The PostgreSQL
helper writes the rows exactly as the EventUpdate store does, one statement per table, and nothing else.
"""

from __future__ import annotations

from typing import Any

from tracefold.news.updates.contracts import (
    DraftClaim,
    EventUpdate,
    Evidence,
    Extraction,
    FrozenInput,
    ImplicationDraft,
    OpenQuestion,
    PriorClaim,
    RelationDraft,
    Source,
    SupportDraft,
)
from tracefold.news.updates.identity import canonical_json, digest
from tracefold.news.updates.notification import ClaimDecision, NotificationPlan
from tracefold.news.updates.semantics import assemble_update

STAMP = 1_790_405_000_000
TARIFF_TOPIC = "medtop:20000384"


def material(
    text: str,
    *,
    publisher: str = "wire",
    revision: int = 1,
    origin: str | None = "agency",
    authority: str = "unknown",
) -> Evidence:
    return Evidence.issue(
        text,
        Source(
            publisher_id=publisher,
            artifact_id=f"{publisher}-release",
            artifact_revision=str(revision),
            first_available_at_ms=STAMP + revision,
            origin_id=origin,
            attribution="Agency spokesperson",
            url=f"https://{publisher}.example.test/release",
            source_authority=authority,  # type: ignore[arg-type]
        ),
    )


def _draft(evidence: Evidence, *, rate: str, phase: str = "announced", slot: str = "a") -> DraftClaim:
    return DraftClaim.model_validate(
        {
            "slot": slot,
            "statement": evidence.text,
            "fields": {
                "subject": "Agency",
                "action": "set tariff",
                "object": "steel imports",
                "speaker": "Agency spokesperson",
                "mode": "decision",
                "phase": phase,
                "content_kind": "official_measure",
                "effective_at": "2026-10-01",
                "conditions": ["unless a deal is signed"],
                "quantities": [{"name": "rate", "value": rate, "unit": "%"}],
                "assets": [{"symbol": "CL", "market_type": "commodity", "role": "primary"}],
            },
            "citations": [{"evidence_ref": evidence.ref, "quote": evidence.text}],
        }
    )


def first_update(event_id: str, *, adopted_at_ms: int = STAMP + 5) -> EventUpdate:
    """One announced 25% tariff: one source supports it, another refutes it, with an inference and a gap."""

    wire = material("Agency announces 25% tariff on steel imports effective October 1.", authority="issuer_first_party")
    rival = material("Officials deny any tariff decision on steel imports.", publisher="rival", origin="ministry")
    source = FrozenInput(event_id=event_id, revision=1, lineage_id=f"{event_id}:line-1", evidence=(wire, rival))
    extraction = Extraction(
        claims=(_draft(wire, rate="25"),),
        topics=(TARIFF_TOPIC,),
        supports=(
            SupportDraft(slot="a", evidence_ref=wire.ref, relation="supports"),
            SupportDraft(slot="a", evidence_ref=rival.ref, relation="refutes"),
        ),
        implications=(
            ImplicationDraft(
                slots=("a",),
                channel="steel input costs",
                explanation="A higher tariff raises domestic steel input costs.",
                conditions=("if the tariff takes effect",),
                origin="system_hypothesis",
            ),
        ),
        open_questions=(OpenQuestion(question="Has the order been signed?", slots=("a",)),),
    )
    update = assemble_update(source, extraction, None, adopted_at_ms=adopted_at_ms)
    assert update is not None
    return update


def raised_update(head: EventUpdate, *, adopted_at_ms: int = STAMP + 100) -> EventUpdate:
    """The same measure raised to 50%: a parameter change against the head's claim."""

    raised = material("Agency raises the steel import tariff to 50% effective October 1.", revision=2)
    prior = tuple(
        PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=claim) for claim in head.claims
    )
    source = FrozenInput(
        event_id=head.event_id, revision=2, lineage_id=f"{head.event_id}:line-2", evidence=(raised,), prior=prior
    )
    extraction = Extraction(
        claims=(_draft(raised, rate="50"),),
        topics=(TARIFF_TOPIC,),
        relations=(
            RelationDraft(
                slot="a",
                previous_ref=head.claims[0].ref,
                relation="real_world_change",
                change_kind="parameter_change",
            ),
        ),
        supports=(SupportDraft(slot="a", evidence_ref=raised.ref, relation="supports"),),
        implications=(
            ImplicationDraft(
                slots=("a",),
                channel="domestic mills",
                explanation="The agency says the higher rate protects domestic mills.",
                origin="reported_causality",
            ),
        ),
        open_questions=(OpenQuestion(question="Has the order been signed?", slots=("a",)),),
    )
    update = assemble_update(source, extraction, head, adopted_at_ms=adopted_at_ms)
    assert update is not None
    return update


def notify_plan(update: EventUpdate, *, key: bool = False, reader_revision: str = "reader-1") -> NotificationPlan:
    retired = set(update.retired_claim_refs)
    return NotificationPlan(
        action="notify",
        reason="uncovered_claims",
        update_ref=update.ref,
        claim_decisions=tuple(
            ClaimDecision(claim_ref=claim.ref, decision="not_notified", reason="retired")
            if claim.ref in retired
            else ClaimDecision(claim_ref=claim.ref, decision="notify", reason="actionable_content")
            for claim in update.claims
        ),
        key=key,
        channel="news",
        reader_revision=reader_revision,
    )


def silent_plan(update: EventUpdate, *, reader_revision: str = "reader-1") -> NotificationPlan:
    return NotificationPlan(
        action="no_notification",
        reason="no_uncovered_actionable_claims",
        update_ref=update.ref,
        claim_decisions=tuple(
            ClaimDecision(claim_ref=claim.ref, decision="not_notified", reason="mode_commentary")
            for claim in update.claims
        ),
        channel="news",
        reader_revision=reader_revision,
    )


def persist_update(conn: Any, update: EventUpdate, *, completed_at_ms: int | None = None) -> str:
    """Write one observation and its adopted revision, and move the head to it. Returns the result id."""

    result_id = f"result:{update.event_id}:{update.content_revision[:12]}"
    completed = int(completed_at_ms if completed_at_ms is not None else update.adopted_at_ms)
    conn.execute(
        """
        INSERT INTO news_semantic_observations (
          result_id, work_id, event_id, input_revision, input_sha256, program_identity, completed_at_ms,
          understanding
        ) VALUES (%s, %s, %s, %s, %s, 'news_updates:test-program', %s, '{}'::jsonb)
        """,
        (result_id, f"work:{update.event_id}", update.event_id, update.input_revision, "a" * 64, completed),
    )
    conn.execute(
        """
        INSERT INTO news_event_updates (
          event_id, content_revision, input_revision, previous_content_revision, adopted_at_ms,
          observation_result_id, document
        ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            update.event_id,
            update.content_revision,
            update.input_revision,
            update.previous_content_revision,
            update.adopted_at_ms,
            result_id,
            canonical_json(update),
        ),
    )
    conn.execute(
        """
        INSERT INTO news_event_update_heads (event_id, content_revision, input_revision, update_ref, adopted_at_ms)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO UPDATE
          SET content_revision = EXCLUDED.content_revision, input_revision = EXCLUDED.input_revision,
              update_ref = EXCLUDED.update_ref, adopted_at_ms = EXCLUDED.adopted_at_ms
        """,
        (update.event_id, update.content_revision, update.input_revision, update.ref, update.adopted_at_ms),
    )
    return result_id


def persist_semantic_work(
    conn: Any,
    event_id: str,
    *,
    wanted: int,
    done: int | None,
    now_ms: int,
    attempts: int = 0,
    last_outcome: str | None = None,
    last_error_code: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO news_semantic_work (
          event_id, wanted_revision, done_revision, lineage_id, attempts, next_attempt_at_ms,
          last_outcome, last_error_code, updated_at_ms
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO UPDATE
          SET wanted_revision = EXCLUDED.wanted_revision, done_revision = EXCLUDED.done_revision,
              attempts = EXCLUDED.attempts, last_outcome = EXCLUDED.last_outcome,
              last_error_code = EXCLUDED.last_error_code, updated_at_ms = EXCLUDED.updated_at_ms
        """,
        (
            event_id,
            wanted,
            done,
            f"{event_id}:lineage",
            attempts,
            now_ms,
            last_outcome,
            last_error_code,
            now_ms,
        ),
    )


def persist_plan(conn: Any, update: EventUpdate, plan: NotificationPlan | None, *, state: str, now_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO news_notification_work (
          event_id, channel, content_revision, state, plan, reader_revision, attempts, next_attempt_at_ms,
          updated_at_ms
        ) VALUES (%s, 'news', %s, %s, %s::jsonb, %s, 0, %s, %s)
        ON CONFLICT (event_id, channel) DO UPDATE
          SET content_revision = EXCLUDED.content_revision, state = EXCLUDED.state, plan = EXCLUDED.plan,
              reader_revision = EXCLUDED.reader_revision, updated_at_ms = EXCLUDED.updated_at_ms
        """,
        (
            update.event_id,
            update.content_revision,
            state,
            canonical_json(plan) if plan is not None else None,
            plan.reader_revision if plan is not None else None,
            now_ms,
            now_ms,
        ),
    )


def queue_intent(conn: Any, update: EventUpdate, plan: NotificationPlan, *, now_ms: int) -> str:
    intent_id = plan.intent_id
    conn.execute(
        """
        INSERT INTO news_delivery_queue (
          intent_id, event_id, kind, state, attempts, enqueued_at_ms, next_attempt_at_ms, updated_at_ms,
          content_revision, claim_refs, plan_key
        ) VALUES (%s, %s, 'update', 'pending', 0, %s, %s, %s, %s, %s::jsonb, %s)
        """,
        (
            intent_id,
            update.event_id,
            now_ms,
            now_ms,
            now_ms,
            update.content_revision,
            canonical_json(list(plan.selected_claim_refs)),
            plan.key,
        ),
    )
    return intent_id


def settle_intent(
    conn: Any,
    update: EventUpdate,
    plan: NotificationPlan,
    *,
    state: str,
    headline_zh: str,
    body: str,
    now_ms: int,
    error_code: str | None = None,
) -> str:
    """The ledger row the deliverer settles with the exact frozen body; the queue row leaves."""

    intent_id = plan.intent_id
    conn.execute("DELETE FROM news_delivery_queue WHERE intent_id = %s", (intent_id,))
    conn.execute(
        """
        INSERT INTO news_deliveries (
          intent_id, event_id, kind, state, card, receipt, error_code, attempted_at_ms, settled_at_ms,
          created_at_ms, content_revision, claim_refs, body, payload_sha256, plan_key
        ) VALUES (%s, %s, 'update', %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
        """,
        (
            intent_id,
            update.event_id,
            state,
            canonical_json(
                {
                    "intent_id": intent_id,
                    "claim_refs": list(plan.selected_claim_refs),
                    "headline_zh": headline_zh,
                    "body": body,
                    "payload_sha256": digest(body),
                }
            ),
            canonical_json({"channel": "telegram", "message_id": 42}) if state == "sent" else None,
            error_code,
            now_ms,
            now_ms + 500 if state != "sending" else None,
            now_ms,
            update.content_revision,
            canonical_json(list(plan.selected_claim_refs)),
            body,
            digest(body),
            plan.key,
        ),
    )
    return intent_id


__all__ = [
    "STAMP",
    "TARIFF_TOPIC",
    "first_update",
    "material",
    "notify_plan",
    "persist_plan",
    "persist_semantic_work",
    "persist_update",
    "queue_intent",
    "raised_update",
    "settle_intent",
    "silent_plan",
]
