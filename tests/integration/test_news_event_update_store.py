"""The EventUpdate `NewsStore` over real PostgreSQL: every port guarantee, races and replays (#706)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_attention import NotifyAll
from tests.support.news_current_delivery import seed_delivery
from tests.support.news_update_pg import (
    EVENT,
    STAMP,
    TEXT,
    Clock,
    Composer,
    Sender,
    StubAnalyzer,
    TaskBackend,
    ThreadedDb,
    adopt_next,
    adopt_other_event,
    adopted_head,
    agent,
    draft,
    evidence,
    extraction_for,
    notifications,
    notify_plan,
    run_agent,
    save_card,
    seed_event,
    sql,
    store,
    trade_rows,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.storage.event_update_store import PgJudgmentCache, PgNewsStore
from tracefold.news.storage.event_updates import EventUpdateConflict, IntentLeaseLost, frozen_input
from tracefold.news.updates.contracts import (
    Citation,
    DraftClaim,
    Extraction,
    FrozenInput,
    PriorClaim,
    ReadTarget,
    RelationDraft,
    SupportDraft,
)
from tracefold.news.updates.identity import digest, identity
from tracefold.news.updates.judgment import Answer, NewsJudgments
from tracefold.news.updates.notification import (
    ClaimDecision,
    FrozenCard,
    NotificationPlan,
    NotificationPlanner,
)
from tracefold.news.updates.ports import SemanticObservation, SendOutcome
from tracefold.news.updates.projection import reading_views
from tracefold.news.updates.public import public_updates
from tracefold.news.updates.service import Notifications

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


# ------------------------------------------------------------------ identities


def test_text_digest_matches_the_python_identity() -> None:
    for body in ('标题\n\n第一行 "引号" \\ tab\t', "é combining", "emoji 🚀  "):
        assert sql("SELECT news_text_digest(%s) AS sha", (body,))[0]["sha"] == digest(body)


# ------------------------------------------------------------------ semantic turn


def test_agent_turn_adopts_once_with_public_row_and_pending_notification() -> None:
    pg, db, clock = store()
    seed_event()
    analyzer = StubAnalyzer()
    source = asyncio.run(pg.input_for(EVENT))
    assert source.revision == 1 and source.lineage_id == f"lineage-{EVENT}"
    assert [item.text for item in source.evidence] == [TEXT]
    assert source.evidence[0].source.source_authority == "reputable_secondary"
    assert source.evidence[0].source.first_available_at_ms == STAMP

    assert asyncio.run(run_agent(agent(pg, clock, analyzer), EVENT)) == "adopted"
    head = asyncio.run(pg.head(EVENT))
    assert head is not None and head.input_revision == 1
    work = sql("SELECT wanted_revision, done_revision, lease_token, last_outcome FROM news_semantic_work")[0]
    assert work == {"wanted_revision": 1, "done_revision": 1, "lease_token": None, "last_outcome": "adopted"}
    rows = trade_rows()
    assert [(row["kind"], row["source_fact_key"], row["source_revision"]) for row in rows] == [
        ("catalyst", EVENT, head.content_revision)
    ]
    assert rows[0]["payload"]["schema_version"] == "news_public_update_v1"
    notification = sql("SELECT channel, state, content_revision, decision_ref FROM news_notification_work")[0]
    assert notification == {
        "channel": "news",
        "state": "pending",
        "content_revision": head.content_revision,
        "decision_ref": None,
    }

    # A replay of the same work reuses its checkpoints and adopts nothing new.
    assert asyncio.run(run_agent(agent(pg, clock, analyzer), EVENT)) == "unchanged"
    assert analyzer.extract_calls == 1
    assert sql("SELECT count(*) AS n FROM news_event_updates")[0]["n"] == 1
    assert len(trade_rows()) == 1
    assert "news_update_adopt" in db.names


def test_checkpoints_and_observations_are_insert_only() -> None:
    pg, _db, _clock = store()
    seed_event()
    source = asyncio.run(pg.input_for(EVENT))
    first = extraction_for(source)
    other = Extraction(claims=(draft(source.evidence[0], action="denies tariff"),))
    assert asyncio.run(pg.save_extraction("work-1", first)) == first
    assert asyncio.run(pg.save_extraction("work-1", other)) == first
    checkpoint = asyncio.run(pg.checkpoint("work-1"))
    assert checkpoint is not None and checkpoint.extraction == first and checkpoint.understanding is None

    observation = SemanticObservation(
        result_id="result-1",
        work_id="work-1",
        event_id=EVENT,
        input_revision=1,
        input_sha256=source.input_sha,
        program_identity="program-test",
        completed_at_ms=STAMP + 10,
        understanding=first,
        read_refs=tuple(view.read_ref for view in reading_views(source)),
    )
    assert asyncio.run(pg.save_observation(observation)) == observation
    replay = observation.model_copy(update={"completed_at_ms": STAMP + 999})
    assert asyncio.run(pg.save_observation(replay)).completed_at_ms == STAMP + 10
    with pytest.raises(EventUpdateConflict, match="news_semantic_observation_conflict"):
        asyncio.run(pg.save_observation(observation.model_copy(update={"program_identity": "other"})))


def test_two_adopters_of_one_head_adopt_exactly_once() -> None:
    pg, _db, _clock = store()
    seed_event()
    base = asyncio.run(pg.input_for(EVENT))

    async def race() -> list[bool]:
        results = await asyncio.gather(
            *(
                adopt_next(
                    pg,
                    None,
                    base.model_copy(update={"evidence": (evidence(f"Agency orders a {rate}% tariff."),)}),
                    extraction_for(
                        base.model_copy(update={"evidence": (evidence(f"Agency orders a {rate}% tariff."),)})
                    ),
                    work_id=f"work-{rate}",
                )
                for rate in (25, 30)
            )
        )
        return [adopted for adopted, _update in results]

    assert sorted(asyncio.run(race())) == [False, True]
    assert sql("SELECT count(*) AS n FROM news_event_updates")[0]["n"] == 1
    head = asyncio.run(pg.head(EVENT))
    assert head is not None
    assert [row["source_revision"] for row in trade_rows()] == [head.content_revision]
    assert sql("SELECT content_revision FROM news_notification_work")[0]["content_revision"] == head.content_revision


def test_adoption_never_downgrades_the_input_revision() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    stale = FrozenInput(
        event_id=EVENT,
        revision=1,
        lineage_id="lineage",
        evidence=(evidence("Agency adds aluminium to the tariff."),),
        prior=tuple(PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=c) for c in head.claims),
    )
    newer = stale.model_copy(update={"revision": 2})
    extracted = extraction_for(newer)
    adopted, update = asyncio.run(adopt_next(pg, head, newer, extracted))
    assert adopted
    older = FrozenInput(
        event_id=EVENT,
        revision=1,
        lineage_id="lineage",
        evidence=(evidence("Agency adds copper to the tariff.", revision="3"),),
        prior=tuple(
            PriorClaim(event_id=EVENT, content_revision=update.content_revision, claim=c) for c in update.claims
        ),
    )
    with pytest.raises(EventUpdateConflict, match="news_update_input_revision_downgrade"):
        asyncio.run(adopt_next(pg, update, older, extraction_for(older), work_id="work-old"))
    assert sql("SELECT input_revision FROM news_event_update_heads")[0]["input_revision"] == 2


def test_possible_new_is_adopted_and_marked_for_notification_without_a_public_row() -> None:
    pg, _db, _clock = store()
    seed_event()
    seed_event("ev-other", text="Agency orders a 25% tariff on steel.", fingerprint="fp-other")
    other_head_claim = asyncio.run(adopt_other_event(pg))
    assert not trade_rows() or all(row["source_fact_key"] == "ev-other" for row in trade_rows())
    source = FrozenInput(
        event_id=EVENT,
        revision=1,
        lineage_id="lineage",
        evidence=(evidence(TEXT),),
        prior=(other_head_claim,),
    )
    extracted = Extraction(
        claims=(draft(source.evidence[0]),),
        relations=(RelationDraft(slot="a", previous_ref=other_head_claim.claim.ref, relation="unresolved"),),
    )
    adopted, update = asyncio.run(adopt_next(pg, None, source, extracted))
    assert adopted
    assert [change.kind for change in update.changes] == ["possible_new"]
    assert [row for row in trade_rows() if row["source_fact_key"] == EVENT] == []
    work = sql("SELECT state, content_revision FROM news_notification_work WHERE event_id = %s", (EVENT,))[0]
    assert work == {"state": "pending", "content_revision": update.content_revision}


def test_a_correction_is_a_source_update_outbox_row_in_the_app_relay_mapping() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    correction = evidence("Correction: Agency orders a 50% tariff, not 25%.")
    source = FrozenInput(
        event_id=EVENT,
        revision=2,
        lineage_id="lineage",
        evidence=(correction,),
        prior=tuple(PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=c) for c in head.claims),
    )
    extracted = Extraction(
        claims=(draft(correction, action="orders 50% tariff"),),
        relations=(
            RelationDraft(slot="a", previous_ref=head.claims[0].ref, relation="corrects", change_kind="correction"),
        ),
    )
    adopted, update = asyncio.run(adopt_next(pg, head, source, extracted))
    assert adopted
    kinds = {(row["kind"], row["source_revision"]) for row in trade_rows()}
    assert ("source_update", update.content_revision) in kinds

    # Exactly the rows the App relay consumes (tests/trading/news_public_updates.py::outbox_row): the
    # Event, its content revision, the PublicUpdate JSON and the semantic completion clock -- never the
    # adoption clock. Nothing in News reads or acknowledges them: the App relay is the only relay.
    rows = sql(
        """
        SELECT kind, source_fact_key, source_revision, payload, source_recorded_at_ms, acknowledged_at_ms
          FROM news_trade_events WHERE source_revision = %s ORDER BY kind
        """,
        (update.content_revision,),
    )
    expected = sorted(
        (
            {
                "kind": "catalyst" if row.kind == "catalyst_delta" else "source_update",
                "source_fact_key": row.event_id,
                "source_revision": row.content_revision,
                "payload": row.model_dump(mode="json"),
                "source_recorded_at_ms": row.semantic_completed_at_ms,
                "acknowledged_at_ms": None,
            }
            for row in public_updates(update, semantic_completed_at_ms=STAMP + 100)
        ),
        key=lambda row: str(row["kind"]),
    )
    assert rows == expected
    assert [row["kind"] for row in rows] == ["source_update"]
    corrected = next(row for row in rows if row["kind"] == "source_update")
    assert corrected["payload"]["retired_claim_refs"] == [head.claims[0].ref]
    assert all(row["source_recorded_at_ms"] == STAMP + 100 != update.adopted_at_ms for row in rows)


# ------------------------------------------------------------------ semantic work bookkeeping


def test_semantic_status_separates_runnable_deferred_and_exhausted() -> None:
    seed_event()
    now_ms = STAMP + 60_000

    def status() -> dict[str, Any]:
        conn = connect_postgres_test(read_only=True)
        try:
            return repositories_for_connection(conn).news.semantic_status(now_ms=now_ms)
        finally:
            conn.close()

    assert (status()["semantic_pending"], status()["semantic_deferred"], status()["semantic_failed_exhausted"]) == (
        1,
        0,
        0,
    )
    sql("UPDATE news_semantic_work SET next_attempt_at_ms=%s", (now_ms + 60_000,))
    assert (status()["semantic_pending"], status()["semantic_deferred"]) == (0, 1)
    sql("UPDATE news_semantic_work SET attempts=3,last_outcome='failed',last_error_code='output_truncated'")
    assert (status()["semantic_pending"], status()["semantic_deferred"], status()["semantic_failed_exhausted"]) == (
        0,
        0,
        1,
    )


def test_equivalent_with_external_conflict_keeps_one_claim_through_adoption_plan_and_receipt() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    sender = Sender("sent")
    composer = Composer()
    assert asyncio.run(notifications(pg, clock, sender, composer).process(EVENT, "news")) == "sent"
    assert len(sender.cards) == 1

    repeated = evidence("Another wire repeats the agency's 25% steel tariff.", publisher="other-wire")
    external = head.claims[0].model_copy(update={"ref": "external-tariff-report"})
    source = FrozenInput(
        event_id=EVENT,
        revision=2,
        lineage_id="repeat-with-external-conflict",
        evidence=(repeated,),
        prior=(
            PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=head.claims[0]),
            PriorClaim(event_id="related-event", content_revision="related-revision", claim=external),
        ),
    )
    extracted = Extraction(
        claims=(
            DraftClaim(
                slot="a",
                statement=head.claims[0].statement,
                fields=head.claims[0].fields,
                citations=(Citation(evidence_ref=repeated.ref, quote=repeated.text),),
            ),
        ),
        relations=(
            RelationDraft(slot="a", previous_ref=head.claims[0].ref, relation="equivalent"),
            RelationDraft(slot="a", previous_ref=external.ref, relation="conflicts", change_kind="conflict"),
        ),
        supports=(SupportDraft(slot="a", evidence_ref=repeated.ref, relation="reports"),),
    )
    adopted, update = asyncio.run(adopt_next(pg, head, source, extracted))
    assert adopted and [claim.ref for claim in update.claims] == [head.claims[0].ref]
    assert {row["kind"] for row in trade_rows()} == {"catalyst", "source_update"}
    assert len(update.evidence_relations) == 2
    assert any(change.relation == "conflicts" for change in update.changes)

    clock.now_ms += 1_000  # A receipt settled at the prior turn's clock is now visible to history.
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None and len(snapshot.reader.receipts) == 1
    result = asyncio.run(notifications(pg, clock, sender, composer).process(EVENT, "news"))
    assert result == "no_notification"
    assert len(sender.cards) == 1 and composer.calls == 1
    assert sql("SELECT count(*) AS n FROM news_deliveries WHERE state='sent'")[0]["n"] == 1


def test_empty_extraction_records_the_task_read_and_does_not_loop_on_the_same_source() -> None:
    pg, _db, clock = store()
    seed_event()
    analyzer = StubAnalyzer(lambda _source: Extraction(claims=()))
    assert asyncio.run(run_agent(agent(pg, clock, analyzer), EVENT)) == "adopted"
    first = sql("SELECT processed_read_refs,done_revision FROM news_semantic_work WHERE event_id=%s", (EVENT,))[0]
    assert len(first["processed_read_refs"]) == 1 and first["done_revision"] == 1
    observed = sql("SELECT read_refs FROM news_semantic_observations WHERE event_id=%s", (EVENT,))[0]
    assert observed["read_refs"] == first["processed_read_refs"]

    sql("UPDATE news_semantic_work SET wanted_revision=2 WHERE event_id=%s", (EVENT,))
    assert asyncio.run(run_agent(agent(pg, clock, analyzer), EVENT)) == "unchanged"
    assert analyzer.extract_calls == 1
    second = sql("SELECT processed_read_refs,done_revision FROM news_semantic_work WHERE event_id=%s", (EVENT,))[0]
    assert second == {"processed_read_refs": first["processed_read_refs"], "done_revision": 2}


def test_semantic_work_is_leased_bounded_and_reopened_by_a_new_revision() -> None:
    pg, _db, clock = store()
    seed_event()
    lease = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=30_000))
    assert lease is not None and lease.wanted_revision == 1 and lease.attempts == 1
    assert asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=30_000)) is None
    for attempt in range(1, 4):
        if attempt > 1:
            clock.now_ms += 10 * 60_000
            lease = asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=30_000))
            assert lease is not None and lease.attempts == attempt
        assert lease is not None
        asyncio.run(pg.defer_semantic_event(lease, reason="provider_unavailable"))
    clock.now_ms += 10 * 60_000
    assert asyncio.run(pg.claim_semantic_work(EVENT, lease_ms=30_000)) is None
    assert asyncio.run(pg.pending_semantic_events(10)) == ()
    row = sql("SELECT attempts, last_outcome, last_error_code FROM news_semantic_work")[0]
    assert row == {"attempts": 3, "last_outcome": "failed", "last_error_code": "provider_unavailable"}

    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            revision = repositories_for_connection(conn).news.request_semantic_revision(
                event_id=EVENT, lineage_id="lineage-2", now_ms=clock.now_ms
            )
    finally:
        conn.close()
    assert revision == 2
    # A new revision is new work: the failed outcome, its code and the spent attempts belong to the old one.
    row = sql("SELECT attempts, last_outcome, last_error_code FROM news_semantic_work")[0]
    assert row == {"attempts": 0, "last_outcome": None, "last_error_code": None}
    assert asyncio.run(pg.pending_semantic_events(10)) == (EVENT,)
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            news = repositories_for_connection(conn).news
            # A wake recorded for an older revision does not hide the newer one from repair.
            assert news.mark_semantic_work_published(event_id=EVENT, revision=1, now_ms=clock.now_ms) is False
            assert news.mark_semantic_work_published(event_id=EVENT, revision=2, now_ms=clock.now_ms) is True
    finally:
        conn.close()
    assert asyncio.run(pg.pending_semantic_events(10)) == ()
    clock.now_ms += 20_000
    assert asyncio.run(pg.pending_semantic_events(10)) == (EVENT,)


def test_one_optional_read_per_lineage_and_its_attached_input() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    source = asyncio.run(pg.input_for(EVENT))
    assert asyncio.run(pg.reserve_extra_read(source.lineage_id, "target-1"))
    assert not asyncio.run(pg.reserve_extra_read(source.lineage_id, "target-2"))
    read = evidence("The ministry's order lists steel and aluminium.", publisher="ministry")
    target = ReadTarget(ref="target-1", action="read_current_artifact", description="ministry order")
    asyncio.run(pg.attach_extra_evidence(source, target, (read,), (head.claims[0].ref,)))
    asyncio.run(pg.record_read_outcome(source.lineage_id, outcome="attached"))
    attached = asyncio.run(pg.input_for(EVENT))
    assert attached.revision == 2 and attached.lineage_id == source.lineage_id
    assert attached.evidence == (read,) and attached.focus_claim_refs == (head.claims[0].ref,)
    assert [row.claim.ref for row in attached.prior] == [head.claims[0].ref]
    assert sql("SELECT extra_read_state FROM news_semantic_work")[0]["extra_read_state"] == "attached"
    assert not asyncio.run(pg.reserve_extra_read(source.lineage_id, "target-3"))


# ------------------------------------------------------------------ notifications


def test_a_notification_turn_sends_once_and_keeps_the_exact_receipt() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    sender = Sender("sent")
    assert asyncio.run(notifications(pg, clock, sender).process(EVENT, "news")) == "sent"
    card = sender.cards[0]
    ledger = sql("SELECT * FROM news_deliveries")[0]
    assert ledger["kind"] == "update" and ledger["state"] == "sent"
    assert ledger["intent_id"] == card.intent_id and ledger["body"] == card.body
    assert ledger["payload_sha256"] == card.payload_sha256 == digest(card.body)
    assert ledger["decision_ref"] == sql("SELECT decision_ref FROM news_notification_work")[0]["decision_ref"]
    assert ledger["claim_refs"] == [head.claims[0].ref] and ledger["content_revision"] == head.content_revision
    assert ledger["receipt"]["provider_message_id"] == "41"
    # The provider's own receipt is kept beside it: what the Telegram enrichment edit is fenced by.
    assert (ledger["receipt"]["message_id"], ledger["receipt"]["target_sha256"]) == (41, "a" * 64)
    # The ledger's `card` is the frozen card itself, headline at the top level for the read side.
    assert FrozenCard.model_validate(ledger["card"]) == card and ledger["card"]["headline_zh"] == card.headline_zh
    assert ledger["history_context"]["headline_zh"] == card.headline_zh
    assert sql("SELECT count(*) AS n FROM news_delivery_queue")[0]["n"] == 0
    work = sql("""SELECT w.state,d.plan AS plan FROM news_notification_work w
                      LEFT JOIN news_notification_decisions d ON d.decision_ref=w.decision_ref""")[0]
    assert work["state"] == "done"
    assert [row["reason"] for row in work["plan"]["claim_decisions"]] == ["editor_notify"]
    reviewed = sql("SELECT delivery_state,delivery_body FROM news_notification_review_tasks_v1")[0]
    assert reviewed == {"delivery_state": "sent", "delivery_body": card.body}
    # Done for this head: a second turn has no work and never resends.
    assert asyncio.run(notifications(pg, clock, sender).process(EVENT, "news")) == "no_work"
    assert len(sender.cards) == 1


def test_two_planners_reserve_one_intent_without_resetting_it() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)

    async def race() -> list[Any]:
        return list(await asyncio.gather(pg.atomic_record_plan(plan), pg.atomic_record_plan(plan)))

    leases = [result.lease for result in asyncio.run(race())]
    assert sum(lease is not None for lease in leases) == 1
    winner = next(lease for lease in leases if lease is not None)
    queued = sql(
        "SELECT intent_id, lease_token, attempts, content_revision, claim_refs, plan_key FROM news_delivery_queue"
    )
    assert queued == [
        {
            "intent_id": plan.intent_id,
            "lease_token": winner.lease_token,
            "attempts": 0,
            "content_revision": head.content_revision,
            "claim_refs": list(plan.selected_claim_refs),
            "plan_key": False,
        }
    ]
    # The marker stays pending while the reserved intent is in flight, due only after its lease.
    work = sql("SELECT state, next_attempt_at_ms FROM news_notification_work")[0]
    assert work == {"state": "pending", "next_attempt_at_ms": clock.now_ms + 120_000}
    assert asyncio.run(pg.pending_notification_events("news", 10)) == ()


def test_a_turn_that_dies_after_reserving_is_reclaimed_after_its_lease() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    orphan = asyncio.run(pg.atomic_record_plan(notify_plan(head, snapshot.reader.revision))).lease
    assert orphan is not None
    clock.now_ms += 120_001
    assert asyncio.run(pg.pending_notification_events("news", 10)) == (EVENT,)
    sender = Sender("sent")
    assert asyncio.run(notifications(pg, clock, sender).process(EVENT, "news")) == "sent"
    assert sender.cards[0].intent_id == orphan.intent_id
    assert sql("SELECT attempts FROM news_delivery_queue") == []
    assert sql("SELECT state FROM news_notification_work")[0]["state"] == "done"


def test_the_janitor_holds_an_unsettled_update_send_ambiguous_and_releases_its_reservation() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)
    lease = asyncio.run(pg.atomic_record_plan(plan)).lease
    assert lease is not None
    body = "关税\n\n机构加征关税"
    card = FrozenCard(
        intent_id=lease.intent_id,
        claim_refs=plan.selected_claim_refs,
        headline_zh="关税",
        body=body,
        payload_sha256=digest(body),
    )
    asyncio.run(save_card(pg, lease, card))
    assert asyncio.run(pg.atomic_begin_send(lease, card)) == "begun"
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            settled = repositories_for_connection(conn).news.terminalize_interrupted_deliveries(
                now_ms=clock.now_ms + 120_000
            )
    finally:
        conn.close()
    assert settled == 1
    assert sql("SELECT state, error_code FROM news_deliveries") == [
        {"state": "ambiguous", "error_code": "ambiguous_after_crash"}
    ]
    assert sql("SELECT count(*) AS n FROM news_delivery_queue")[0]["n"] == 0


@pytest.mark.parametrize("related", [False, True])
def test_only_a_related_receipt_settled_after_the_snapshot_races_the_plan(related: bool) -> None:
    """#742 W5: the reader revision is this Event's related receipts, recomputed inside the CAS. A card
    sent elsewhere about something else is not a race; one whose text is close to this Event's claims is."""

    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    seed_event("ev-other", fingerprint="fp-other", title="Chile pit halted", text="Miner halts copper pit.")
    title = "Agency orders steel tariff" if related else "Chile pit halted"
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            seed_delivery(
                conn,
                event_id="ev-other",
                at_ms=clock.now_ms - 5_000,
                history_context={"comparison_title": title},
                card={"header": {"title": {"content": "智利铜矿停产"}}},
            )
    finally:
        conn.close()
    committed = asyncio.run(pg.atomic_record_plan(notify_plan(head, snapshot.reader.revision)))
    if not related:
        assert committed.status == "committed" and committed.lease is not None
        return
    assert committed.status == "reader_changed"
    work = sql("SELECT state, decision_ref FROM news_notification_work WHERE event_id = %s", (EVENT,))[0]
    assert work == {"state": "pending", "decision_ref": None}
    assert sql("SELECT count(*) AS n FROM news_delivery_queue")[0]["n"] == 0
    # The losing plan's decision is kept, so the next turn reuses its judgment rather than asking again.
    assert sql("SELECT count(*) AS n FROM news_notification_decisions WHERE event_id = %s", (EVENT,))[0]["n"] == 1


def test_deferred_claims_keep_notification_pending_beside_the_reserved_intent() -> None:
    pg, _db, clock = store()

    def two_claims(source: FrozenInput) -> Extraction:
        item = source.evidence[0]
        return Extraction(
            claims=(
                draft(item, "a", action="orders tariff", quote="Agency orders a 25% tariff"),
                draft(item, "b", action="sets effective date", quote="effective October 1"),
            )
        )

    seed_event()
    assert asyncio.run(run_agent(agent(pg, clock, StubAnalyzer(two_claims)), EVENT)) == "adopted"
    head = asyncio.run(pg.head(EVENT))
    assert head is not None and len(head.claims) == 2
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision, deferred=(head.claims[1].ref,))
    lease = asyncio.run(pg.atomic_record_plan(plan)).lease
    assert lease is not None and lease.card is None
    work = sql(
        "SELECT w.state,w.attempts,d.plan AS plan FROM news_notification_work w "
        "LEFT JOIN news_notification_decisions d ON d.decision_ref=w.decision_ref"
    )[0]
    assert work["state"] == "pending" and work["attempts"] == 0
    assert {row["decision"] for row in work["plan"]["claim_decisions"]} == {"notify", "deferred"}


def test_not_sent_retries_the_same_identity_and_frozen_payload() -> None:
    pg, _db, clock = store()
    adopted_head(pg, clock)
    sender = Sender("not_sent", "sent")
    composer = Composer()
    turn = notifications(pg, clock, sender, composer)
    assert asyncio.run(turn.process(EVENT, "news")) == "not_sent"
    assert sql("SELECT count(*) AS n FROM news_deliveries")[0]["n"] == 0
    queued = sql("SELECT lease_token, attempts, frozen_card, error_code FROM news_delivery_queue")[0]
    assert queued["lease_token"] is None and queued["attempts"] == 1 and queued["error_code"] == "rate_limited"
    assert sql("SELECT state FROM news_notification_work")[0]["state"] == "pending"

    clock.now_ms += 5 * 60_000
    assert asyncio.run(pg.pending_notification_events("news", 10)) == (EVENT,)
    assert asyncio.run(turn.process(EVENT, "news")) == "sent"
    assert composer.calls == 1
    assert sender.cards[0] == sender.cards[1]
    ledger = sql("SELECT state, payload_sha256, intent_id FROM news_deliveries")[0]
    assert ledger == {
        "state": "sent",
        "payload_sha256": sender.cards[0].payload_sha256,
        "intent_id": sender.cards[0].intent_id,
    }


def test_late_settlement_of_previous_unsent_lease_cannot_settle_next_send() -> None:
    pg, _db, clock = store()
    adopted_head(pg, clock)
    sender = Sender("not_sent")
    turn = notifications(pg, clock, sender)
    assert asyncio.run(turn.process(EVENT, "news")) == "not_sent"
    prior = sql("SELECT last_settlement FROM news_delivery_queue")[0]["last_settlement"]
    clock.now_ms += 5 * 60_000
    prepared = asyncio.run(turn.service.prepare(EVENT, "news"))
    assert prepared.status == "ready" and prepared.lease is not None and prepared.card is not None
    assert asyncio.run(pg.atomic_begin_send(prepared.lease, prepared.card)) == "begun"
    old_lease = prepared.lease.model_copy(update={"lease_token": prior["lease_token"]})
    old_outcome = SendOutcome(
        state="not_sent",
        payload_sha256=prepared.card.payload_sha256,
        error_code="rate_limited",
        retryable=True,
    )
    assert (
        asyncio.run(pg.settle_send(old_lease, prepared.card, old_outcome, settled_at_ms=clock.now_ms))
        == "already_settled"
    )
    assert sql("SELECT state FROM news_deliveries")[0]["state"] == "sending"
    assert sql("SELECT attempts,lease_token FROM news_delivery_queue")[0] == {
        "attempts": 1,
        "lease_token": prepared.lease.lease_token,
    }
    with pytest.raises(RuntimeError, match="news_send_settlement_conflict"):
        asyncio.run(
            pg.settle_send(
                old_lease,
                prepared.card,
                SendOutcome(state="sent", payload_sha256=prepared.card.payload_sha256, message_id="late"),
                settled_at_ms=clock.now_ms,
            )
        )
    assert sql("SELECT state FROM news_deliveries")[0]["state"] == "sending"


@pytest.mark.parametrize("state", ["sent", "ambiguous", "not_sent"])
def test_final_settlement_retry_matches_exact_lease_and_outcome(state: str) -> None:
    pg, _db, clock = store()
    adopted_head(pg, clock)
    turn = notifications(pg, clock, Sender(), Composer())
    prepared = asyncio.run(turn.service.prepare(EVENT, "news"))
    assert prepared.status == "ready" and prepared.lease is not None and prepared.card is not None
    lease, card = prepared.lease, prepared.card
    assert asyncio.run(pg.atomic_begin_send(lease, card)) == "begun"
    outcome = SendOutcome(
        state=state,
        payload_sha256=card.payload_sha256,
        message_id="provider-123" if state == "sent" else None,
        error_code="provider_failure" if state != "sent" else None,
        retryable=False,
    )
    settled = asyncio.run(pg.settle_send(lease, card, outcome, settled_at_ms=clock.now_ms))
    assert settled == {"sent": "sent", "ambiguous": "ambiguous", "not_sent": "terminal"}[state]
    if state == "sent":
        # A later receipt edit cannot erase the original settlement identity.
        sql("UPDATE news_deliveries SET receipt = receipt || '{\"enriched\": true}'::jsonb")
    assert asyncio.run(pg.settle_send(lease, card, outcome, settled_at_ms=clock.now_ms)) == "already_settled"
    wrong_lease = lease.model_copy(update={"lease_token": "later-lease"})
    with pytest.raises(RuntimeError, match="news_send_settlement_conflict"):
        asyncio.run(pg.settle_send(wrong_lease, card, outcome, settled_at_ms=clock.now_ms))
    changed_outcome = outcome.model_copy(
        update={"message_id": "different"} if state == "sent" else {"error_code": "different"}
    )
    with pytest.raises(RuntimeError, match="news_send_settlement_conflict"):
        asyncio.run(pg.settle_send(lease, card, changed_outcome, settled_at_ms=clock.now_ms))


def test_an_ambiguous_send_is_possibly_sent_never_resent_and_never_holds_the_work() -> None:
    """#742 N5 (review D7). Before, an ambiguous claim deferred every later plan and spent its attempts
    until the work was exhausted. It may already be on the reader's screen: it is not sent again, and
    nothing waits for an outcome that will never come."""

    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    with pytest.raises(RuntimeError, match="provider connection dropped"):
        asyncio.run(notifications(pg, clock, Sender("raise")).process(EVENT, "news"))
    ledger = sql("SELECT state, error_code, claim_refs FROM news_deliveries")[0]
    assert ledger == {"state": "ambiguous", "error_code": "RuntimeError", "claim_refs": [head.claims[0].ref]}
    assert sql("SELECT count(*) AS n FROM news_delivery_queue")[0]["n"] == 0
    assert sql("SELECT state FROM news_notification_work")[0]["state"] == "done"

    sql("UPDATE news_notification_work SET state = 'pending'")
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None and snapshot.reader.ambiguous_claim_refs == (head.claims[0].ref,)
    assert snapshot.reader.blocked_claim_refs == () and snapshot.reader.receipts == ()
    sender = Sender("sent")
    assert asyncio.run(notifications(pg, clock, sender).process(EVENT, "news")) == "no_notification"
    assert sender.cards == []
    work = sql(
        "SELECT w.state,w.attempts,d.plan AS plan FROM news_notification_work w "
        "LEFT JOIN news_notification_decisions d ON d.decision_ref=w.decision_ref"
    )[0]
    assert (work["state"], work["attempts"]) == ("done", 0)
    assert work["plan"]["claim_decisions"][0]["reason"] == "send_outcome_ambiguous"
    assert sql("SELECT state FROM news_deliveries")[0]["state"] == "ambiguous"


def test_begin_send_rechecks_the_head_and_keeps_the_frozen_card_for_the_same_identity() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)
    lease = asyncio.run(pg.atomic_record_plan(plan)).lease
    assert lease is not None
    card = FrozenCard(
        intent_id=lease.intent_id,
        claim_refs=plan.selected_claim_refs,
        headline_zh="关税",
        body="关税\n\n机构加征关税",
        payload_sha256=digest("关税\n\n机构加征关税"),
    )
    assert asyncio.run(save_card(pg, lease, card)) == card
    other = card.model_copy(update={"body": "另一版本", "payload_sha256": digest("另一版本")})
    with pytest.raises(ValueError):
        asyncio.run(save_card(pg, lease, other))

    source = FrozenInput(
        event_id=EVENT,
        revision=2,
        lineage_id="lineage",
        evidence=(evidence("Agency adds aluminium to the tariff."),),
        prior=tuple(PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=c) for c in head.claims),
    )
    adopted, update = asyncio.run(adopt_next(pg, head, source, extraction_for(source)))
    assert adopted
    assert asyncio.run(pg.atomic_begin_send(lease, card)) == "head_changed"
    queued = sql("SELECT lease_token, frozen_card FROM news_delivery_queue")[0]
    assert queued["lease_token"] is None and FrozenCard.model_validate(queued["frozen_card"]) == card
    assert sql("SELECT count(*) AS n FROM news_deliveries")[0]["n"] == 0
    work = sql("SELECT state, content_revision FROM news_notification_work")[0]
    assert work == {"state": "pending", "content_revision": update.content_revision}
    # The released lease no longer fences a card write.
    with pytest.raises(IntentLeaseLost):
        asyncio.run(save_card(pg, lease, card))


def test_snapshot_reads_current_sent_ledger() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    seed_event("ev-other", title="Agency orders steel tariff", fingerprint="fp-tariff", at_ms=STAMP - 7_200_000)
    card = {"header": {"title": {"content": "机构加征钢铁关税"}}}
    context = {"why_zh": "影响钢铁进口", "dedupe_family": "general", "comparison_fingerprint": "fp-tariff"}
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            seed_delivery(conn, event_id="ev-other", at_ms=STAMP - 3_600_000, history_context=context, card=card)
    finally:
        conn.close()
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None and snapshot.update == head
    assert len(snapshot.reader.receipts) == 1
    assert snapshot.reader.receipts[0].intent_id.startswith("intent:")
    receipts = sql("SELECT kind,state,card,body,payload_sha256 FROM news_deliveries WHERE event_id='ev-other'")
    assert receipts == [
        {
            "kind": "update",
            "state": "sent",
            "card": card,
            "body": "机构加征钢铁关税",
            "payload_sha256": digest("机构加征钢铁关税"),
        }
    ]


def test_card_failure_releases_the_lease_and_the_third_fails_the_work() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    for attempt in range(1, 4):
        snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
        assert snapshot is not None
        lease = asyncio.run(pg.atomic_record_plan(notify_plan(head, snapshot.reader.revision))).lease
        assert lease is not None
        asyncio.run(pg.record_unsent_failure(lease, error_code="TimeoutError", retryable=True))
        queued = sql("SELECT state, attempts, lease_token FROM news_delivery_queue")[0]
        assert queued["attempts"] == attempt and queued["lease_token"] is None
        clock.now_ms += 15 * 60_000
    assert queued["state"] == "dead"
    # The unsent intent ended, and the work says why, instead of closing as if it had been delivered.
    assert sql("SELECT state, last_error_code FROM news_notification_work")[0] == {
        "state": "failed",
        "last_error_code": "TimeoutError",
    }
    assert asyncio.run(pg.notification_snapshot(EVENT, "news")) is None


def test_judgment_cache_keeps_the_first_answer_and_retention_purges_old_rows() -> None:
    db = ThreadedDb()
    clock = Clock()
    cache = PgJudgmentCache(db, clock=clock)
    first = Answer(item_id="q1", value="full", backend="generated")
    asyncio.run(cache.put("key-1", first))
    asyncio.run(cache.put("key-1", first.model_copy(update={"value": "none"})))
    assert asyncio.run(cache.get("key-1")) == first
    assert asyncio.run(cache.get("missing")) is None
    pg = PgNewsStore(db, clock=clock)
    clock.now_ms += 15 * 24 * 3_600_000
    assert asyncio.run(pg.purge_semantic_caches(limit=100)) == 1
    assert asyncio.run(cache.get("key-1")) is None


def test_frozen_input_requires_material() -> None:
    with pytest.raises(LookupError, match="news_event_input_missing"):
        frozen_input(EVENT, {"work": None, "items": [], "item_ids": [], "head": None})


def test_a_no_notification_plan_is_final_for_the_head_and_retires_an_unsent_reservation() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    lease = asyncio.run(pg.atomic_record_plan(notify_plan(head, snapshot.reader.revision))).lease
    assert lease is not None
    asyncio.run(pg.record_unsent_failure(lease, error_code="TimeoutError", retryable=True))
    clock.now_ms += 5 * 60_000
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    silent = NotificationPlan(
        action="no_notification",
        reason="no_uncovered_actionable_claims",
        update_ref=head.ref,
        claim_decisions=tuple(
            ClaimDecision(claim_ref=claim.ref, decision="not_notified", reason="editor_feed_only")
            for claim in head.claims
        ),
        channel="news",
        reader_revision=snapshot.reader.revision,
        assessment_input_digest=digest({"update": head.model_dump(mode="json"), "fixture": "silent"}),
        assessment_input={"update": head.model_dump(mode="json"), "fixture": "silent"},
    )
    assert asyncio.run(pg.atomic_record_plan(silent)).status == "committed"
    assert sql("SELECT count(*) AS n FROM news_delivery_queue")[0]["n"] == 0
    work = sql(
        "SELECT w.state,w.reader_revision,d.plan AS plan FROM news_notification_work w "
        "LEFT JOIN news_notification_decisions d ON d.decision_ref=w.decision_ref"
    )[0]
    assert work["state"] == "done" and work["reader_revision"] == snapshot.reader.revision
    assert work["plan"]["claim_decisions"][0]["reason"] == "editor_feed_only"
    assert asyncio.run(pg.notification_snapshot(EVENT, "news")) is None


def test_snapshot_recalls_exact_sent_text_beyond_leader_bands() -> None:
    pg, db, clock = store()
    adopted_head(pg, clock)
    leader = "Cryptocurrency prices remain stable across global markets"
    sql("UPDATE news_events SET comparison_title = %s WHERE event_id = %s", (leader, EVENT))

    def sent_update(event_id: str, title: str, body: str) -> None:
        seed_event(event_id, title=title, fingerprint=event_id, at_ms=STAMP - 21_600_000)
        context = {
            "comparison_title": title,
            "headline_zh": body,
            "why_zh": body,
            "dedupe_family": "general",
            "comparison_fingerprint": event_id,
        }
        sql(
            """
            INSERT INTO news_deliveries
              (intent_id, event_id, kind, state, card, receipt, attempted_at_ms,
               settled_at_ms, created_at_ms, history_context, content_revision,
               claim_refs, body, payload_sha256, plan_key)
            VALUES (%s, %s, 'update', 'sent', %s::jsonb, '{}'::jsonb, %s, %s, %s, %s::jsonb,
                    %s, '["historical-claim"]'::jsonb, %s, %s, false)
            """,
            (
                identity("intent", event_id),
                event_id,
                json.dumps({"header": {"title": {"content": body}}}),
                STAMP - 18_000_000,
                STAMP - 18_000_000,
                STAMP - 18_000_000,
                json.dumps(context),
                "a" * 64,
                body,
                digest(body),
            ),
        )

    for index in range(35):
        sent_update(f"leader-noise-{index}", leader, "市场价格保持稳定")
    sent_update("actual-tariff", TEXT, "机构已宣布百分之二十五钢铁进口关税")
    history = asyncio.run(
        db.read("test_history", lambda repos: repos.news.reader_history(event_id=EVENT, now_ms=clock()))
    )
    assert "actual-tariff" not in {row.event_id for row in history.told_source_rows}
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    assert len(snapshot.reader.receipts) == 16
    assert snapshot.reader.receipts[0].intent_id == identity("intent", "actual-tariff")


def test_snapshot_keeps_earlier_incremental_receipt_and_excludes_future_and_deleted() -> None:
    pg, _db, clock = store()
    adopted_head(pg, clock)
    seed_event("incremental", title=TEXT, fingerprint="incremental")

    def sent_update(key: str, body: str, at_ms: int, *, deleted: bool = False) -> str:
        intent = identity("intent", key)
        sql(
            """
            INSERT INTO news_deliveries
              (intent_id, event_id, kind, state, card, receipt, attempted_at_ms,
               settled_at_ms, created_at_ms, history_context, content_revision,
               claim_refs, body, payload_sha256, plan_key, delete_state,
               delete_evidence, delete_reason, delete_attempted_at_ms, delete_settled_at_ms)
            VALUES (%s, 'incremental', 'update', 'sent', '{}'::jsonb, '{}'::jsonb,
                    %s, %s, %s, %s::jsonb, %s, '["historical-claim"]'::jsonb, %s, %s, false, %s,
                    %s::jsonb, %s, %s, %s)
            """,
            (
                intent,
                at_ms,
                at_ms,
                at_ms,
                json.dumps({"comparison_title": TEXT}),
                digest(key),
                body,
                digest(body),
                "deleted" if deleted else None,
                "{}" if deleted else None,
                "test deletion" if deleted else None,
                at_ms if deleted else None,
                at_ms if deleted else None,
            ),
        )
        return intent

    old = sent_update("A", "机构宣布关税", STAMP + 1)
    latest = sent_update("B", "生效日期为下月", STAMP + 2)
    future = sent_update("future", "还没发送的消息", clock() + 1)
    removed = sent_update("removed", "已经删除的消息", STAMP + 3, deleted=True)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    receipts = {row.intent_id: row.body for row in snapshot.reader.receipts}
    assert receipts[old] == "机构宣布关税"
    assert receipts[latest] == "生效日期为下月"
    assert future not in receipts and removed not in receipts


@pytest.mark.parametrize("phase", ["planner", "card"])
def test_old_notification_failure_cannot_change_new_head_work(phase: str) -> None:
    pg, _db, clock = store()
    old = adopted_head(pg, clock)

    async def exercise() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        class FailingPlanner:
            async def plan(self, *args: Any, **kwargs: Any) -> Any:
                entered.set()
                await release.wait()
                raise RuntimeError("old planner failed")

        class FailingComposer:
            identity = "failing_card_composer"

            async def compose(self, *args: Any, **kwargs: Any) -> Any:
                entered.set()
                await release.wait()
                raise RuntimeError("old card failed")

        planner = NotificationPlanner(
            NewsJudgments(generated=TaskBackend({"coverage": "full"}), cache=PgJudgmentCache(pg.db)), NotifyAll()
        )
        service = Notifications(
            pg,
            FailingPlanner() if phase == "planner" else planner,
            FailingComposer() if phase == "card" else Composer(),
            clock=clock,
        )  # type: ignore[arg-type]
        task = asyncio.create_task(service.process(EVENT, "news", Sender()))
        await asyncio.wait_for(entered.wait(), 3)
        try:
            source = FrozenInput(
                event_id=EVENT,
                revision=2,
                lineage_id="next",
                evidence=(evidence("Agency adds aluminium to the tariff."),),
                prior=tuple(
                    PriorClaim(event_id=EVENT, content_revision=old.content_revision, claim=c) for c in old.claims
                ),
            )
            adopted, new = await adopt_next(pg, old, source, extraction_for(source))
            assert adopted
            before = sql("SELECT * FROM news_notification_work")[0]
            assert before["content_revision"] == new.content_revision
        finally:
            release.set()
        turn = await task
        assert (turn.status, turn.error_code) == (
            "plan_failed" if phase == "planner" else "card_failed",
            f"news_{'notification_plan' if phase == 'planner' else 'card'}:RuntimeError",
        )
        assert sql("SELECT * FROM news_notification_work")[0] == before
        if phase == "card":
            intent = sql("SELECT content_revision, error_code, attempts, state FROM news_delivery_queue")[0]
            assert intent == {
                "content_revision": old.content_revision,
                "error_code": "news_card:RuntimeError",
                "attempts": 1,
                "state": "pending",
            }

    asyncio.run(exercise())


def test_repeated_planner_failure_settlement_for_one_snapshot_spends_one_attempt() -> None:
    pg, _db, _clock = store()
    head = adopted_head(pg, _clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None and snapshot.work_updated_at_ms is not None
    for _ in range(2):
        asyncio.run(
            pg.defer_notification(
                EVENT, "news", head.content_revision, snapshot.work_updated_at_ms, error_code="news_editor_down"
            )
        )
    assert sql("SELECT attempts, last_error_code FROM news_notification_work")[0] == {
        "attempts": 1,
        "last_error_code": "news_editor_down",
    }


def test_the_third_planner_failure_fails_the_work_and_only_an_exact_retry_reopens_it() -> None:
    """#742 N5: exhausted work is `failed` with its error code, not pending forever with none."""

    pg, db, clock = store()
    head = adopted_head(pg, clock)
    for expected in range(1, 3):
        asyncio.run(pg.defer_notification(EVENT, "news", head.content_revision, error_code=f"plan_error_{expected}"))
        row = sql("SELECT state, attempts, next_attempt_at_ms FROM news_notification_work")[0]
        assert (row["state"], row["attempts"]) == ("pending", expected) and row["next_attempt_at_ms"] > clock()
    asyncio.run(pg.defer_notification(EVENT, "news", head.content_revision, error_code="plan_error_3"))
    failed = sql("SELECT * FROM news_notification_work")[0]
    assert (failed["state"], failed["attempts"], failed["last_error_code"]) == ("failed", 3, "plan_error_3")
    asyncio.run(pg.defer_notification(EVENT, "news", head.content_revision, error_code="plan_error_4"))
    assert sql("SELECT * FROM news_notification_work")[0] == failed
    assert asyncio.run(pg.pending_notification_events("news", 10)) == ()
    # Explicit recovery affects this planning version only.
    assert not asyncio.run(
        db.tx(
            "retry",
            lambda r: r.news.retry_failed_work(
                event_id=EVENT, kind="notification", revision="obsolete", now_ms=clock()
            ),
        )
    )
    assert asyncio.run(
        db.tx(
            "retry",
            lambda r: r.news.retry_failed_work(
                event_id=EVENT, kind="notification", revision=head.content_revision, now_ms=clock()
            ),
        )
    )
    assert sql("SELECT state, attempts FROM news_notification_work")[0] == {"state": "pending", "attempts": 0}
    assert asyncio.run(pg.pending_notification_events("news", 10)) == (EVENT,)


def test_waiting_on_a_send_in_flight_spends_no_attempt_and_records_no_new_decision() -> None:
    """#742 N2/N5 (review D6). A claim held by this Event's own `sending` row waits: no attempt is spent,
    and a turn that decides exactly what the last one decided is the same decision row, not a new one."""

    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    prepared = asyncio.run(notifications(pg, clock, Sender()).service.prepare(EVENT, "news"))
    assert prepared.status == "ready" and prepared.lease is not None and prepared.card is not None
    assert asyncio.run(pg.atomic_begin_send(prepared.lease, prepared.card)) == "begun"
    sql("UPDATE news_notification_work SET state='pending', next_attempt_at_ms=%s", (clock(),))
    for _ in range(3):
        assert asyncio.run(notifications(pg, clock, Sender()).process(EVENT, "news")) == "unresolved"
        clock.now_ms += 30_000
    work = sql("SELECT state, attempts, next_attempt_at_ms FROM news_notification_work")[0]
    assert (work["state"], work["attempts"]) == ("pending", 0) and work["next_attempt_at_ms"] > clock() - 30_000
    unresolved = sql("SELECT count(*) AS n FROM news_notification_decisions WHERE plan->>'action'='unresolved'")
    assert unresolved[0]["n"] == 1
    assert head.claims[0].ref in {
        row["claim_ref"]
        for row in sql(
            "SELECT jsonb_array_elements(plan->'claim_decisions')->>'claim_ref' AS claim_ref"
            " FROM news_notification_decisions WHERE plan->>'action'='unresolved'"
        )
    }


def test_an_orphaned_sending_row_is_held_ambiguous_and_its_plan_completes() -> None:
    """#742 N8: a `sending` row whose owner is gone is reconciled -- never one this process still owns --
    and the work it held is completed rather than left waiting until the claim goes stale."""

    pg, db, clock = store()
    adopted_head(pg, clock)
    prepared = asyncio.run(notifications(pg, clock, Sender()).service.prepare(EVENT, "news"))
    assert prepared.lease is not None and prepared.card is not None
    assert asyncio.run(pg.atomic_begin_send(prepared.lease, prepared.card)) == "begun"
    intent = prepared.lease.intent_id

    def reconcile(r: Any, *, now_ms: int, owned: tuple[str, ...] = ()) -> int:
        return r.news.terminalize_interrupted_deliveries(now_ms=now_ms, exclude_intent_ids=owned)

    assert asyncio.run(db.tx("reconcile", lambda r: reconcile(r, now_ms=clock() + 30_000))) == 0
    owned_late = clock() + 120_000
    assert asyncio.run(db.tx("reconcile", lambda r: reconcile(r, now_ms=owned_late, owned=(intent,)))) == 0
    assert asyncio.run(db.tx("reconcile", lambda r: reconcile(r, now_ms=owned_late))) == 1
    assert sql("SELECT state, error_code FROM news_deliveries") == [
        {"state": "ambiguous", "error_code": "ambiguous_after_crash"}
    ]
    assert sql("SELECT count(*) AS n FROM news_delivery_queue")[0]["n"] == 0
    assert sql("SELECT state FROM news_notification_work")[0]["state"] == "done"


def test_final_semantic_crash_is_settled_only_after_lease_expiry_and_retries_exact_version() -> None:
    pg, db, clock = store()
    head = adopted_head(pg, clock)
    facts = sql("SELECT document FROM news_event_updates")
    outbox = trade_rows()
    checkpoints = sql("SELECT * FROM news_semantic_checkpoints")
    sql(
        "UPDATE news_semantic_work SET wanted_revision=2, attempts=3, lease_token='last', "
        "leased_until_ms=%s, last_outcome=NULL",
        (clock() + 1000,),
    )

    def settle(r):
        return r.news.terminalize_exhausted_semantic_work(now_ms=clock(), limit=10)

    assert asyncio.run(db.tx("janitor", settle)) == 0
    clock.now_ms += 1001
    assert asyncio.run(db.tx("janitor", settle)) == 1
    failed = sql("SELECT last_outcome,last_error_code,lease_token FROM news_semantic_work")[0]
    assert failed == {
        "last_outcome": "failed",
        "last_error_code": "news_semantic_attempts_exhausted_after_lease",
        "lease_token": None,
    }
    assert asyncio.run(db.tx("janitor", settle)) == 0
    for revision, expected in [("1", False), ("2", True), ("2", False)]:
        assert (
            asyncio.run(
                db.tx(
                    "retry",
                    lambda r, revision=revision: r.news.retry_failed_work(
                        event_id=EVENT, kind="semantic", revision=revision, now_ms=clock()
                    ),
                )
            )
            is expected
        )
    work = sql("SELECT attempts,done_revision,last_outcome,last_error_code FROM news_semantic_work")[0]
    assert work["attempts"] == 0 and work["done_revision"] == 1 and work["last_outcome"] is None
    assert work["last_error_code"] == failed["last_error_code"]
    assert sql("SELECT document FROM news_event_updates") == facts
    assert trade_rows() == outbox and sql("SELECT * FROM news_semantic_checkpoints") == checkpoints
    assert asyncio.run(pg.head(EVENT)) == head


def test_notification_retry_revives_the_failed_unsent_intent_and_never_reopens_a_ledger() -> None:
    pg, db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)
    lease = asyncio.run(pg.atomic_record_plan(plan)).lease
    assert lease is not None
    sql("UPDATE news_delivery_queue SET attempts=2")
    asyncio.run(pg.record_unsent_failure(lease, error_code="bad_copy", retryable=True))
    assert sql("SELECT state FROM news_delivery_queue")[0]["state"] == "dead"
    assert sql("SELECT state, last_error_code FROM news_notification_work")[0] == {
        "state": "failed",
        "last_error_code": "bad_copy",
    }
    facts, outbox = sql("SELECT document FROM news_event_updates"), trade_rows()

    def retry(r):
        return r.news.retry_failed_work(
            event_id=EVENT, kind="notification", revision=head.content_revision, now_ms=clock()
        )

    assert asyncio.run(db.tx("retry", retry))
    assert sql("SELECT intent_id,attempts,state FROM news_delivery_queue") == [
        {"intent_id": lease.intent_id, "attempts": 0, "state": "pending"}
    ]
    assert not asyncio.run(db.tx("retry", retry))
    sender = Sender("sent")
    assert asyncio.run(notifications(pg, clock, sender).process(EVENT, "news")) == "sent"
    assert sender.cards[0].intent_id == lease.intent_id
    assert sql("SELECT state, last_error_code FROM news_notification_work")[0] == {
        "state": "done",
        "last_error_code": None,
    }
    sent = sql("SELECT * FROM news_deliveries")
    assert not asyncio.run(db.tx("retry", retry))
    assert sql("SELECT * FROM news_deliveries") == sent
    assert sql("SELECT document FROM news_event_updates") == facts and trade_rows() == outbox


@pytest.mark.parametrize("state", ["sending", "ambiguous", "terminal"])
def test_notification_retry_never_reopens_an_intent_with_a_send_ledger(state: str) -> None:
    pg, db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    lease = asyncio.run(pg.atomic_record_plan(notify_plan(head, snapshot.reader.revision))).lease
    assert lease is not None
    from tracefold.news.updates.notification import freeze_card

    card = freeze_card(lease.plan, head, asyncio.run(Composer().compose(head.claims, sources={})))
    asyncio.run(save_card(pg, lease, card))
    assert asyncio.run(pg.atomic_begin_send(lease, card)) == "begun"
    sql("UPDATE news_deliveries SET state=%s", (state,))
    sql("UPDATE news_delivery_queue SET state='dead', attempts=3, lease_token=NULL, settled_at_ms=%s", (clock(),))
    sql("UPDATE news_notification_work SET state='failed', last_error_code='operator_test'")
    before = sql("SELECT * FROM news_deliveries")
    assert asyncio.run(
        db.tx(
            "retry",
            lambda r: r.news.retry_failed_work(
                event_id=EVENT, kind="notification", revision=head.content_revision, now_ms=clock()
            ),
        )
    )
    assert sql("SELECT * FROM news_deliveries") == before
    assert sql("SELECT state FROM news_delivery_queue")[0]["state"] == "dead"


def test_targeted_reanalysis_reuses_work_and_preserves_adopted_head() -> None:
    pg, db, clock = store()
    head = adopted_head(pg, clock)
    listing = asyncio.run(
        db.read("reanalysis_preview", lambda repos: repos.news.reanalysis_scope_list(event_id=EVENT, now_ms=clock()))
    )
    assert listing["wanted_revision"] == listing["done_revision"] == 1
    assert listing["head_revision"] == head.content_revision
    assert len(listing["scopes"]) == 1 and listing["scopes"][0]["completed"]
    read_ref = listing["scopes"][0]["read_ref"]
    next_revision = asyncio.run(
        db.tx(
            "reanalysis_request",
            lambda repos: repos.news.request_reanalysis(
                event_id=EVENT,
                expected_wanted_revision=1,
                expected_head_revision=head.content_revision,
                read_ref=read_ref,
                reason="confirmed omitted condition in previous task view",
                now_ms=clock(),
            ),
        )
    )
    assert next_revision == 2
    reopened = asyncio.run(pg.input_for(EVENT))
    assert reopened.revision == 2 and reopened.evidence
    assert reopened.reanalysis_reason == "confirmed omitted condition in previous task view"
    assert reopened.reanalysis_head_ref == head.ref
    result = asyncio.run(run_agent(agent(pg, clock, StubAnalyzer(lambda _source: Extraction(claims=()))), EVENT))
    assert result == "unchanged"
    assert asyncio.run(pg.head(EVENT)) == head
    observations = sql(
        "SELECT read_refs,reanalysis_reason,reanalysis_head_ref FROM news_semantic_observations ORDER BY input_revision"
    )
    assert observations[-1]["read_refs"] == [read_ref]
    assert observations[-1]["reanalysis_head_ref"] == head.ref
    assert sql("SELECT reanalysis_read_ref,done_revision FROM news_semantic_work")[0] == {
        "reanalysis_read_ref": None,
        "done_revision": 2,
    }
    with pytest.raises(EventUpdateConflict, match="news_reanalysis_wanted_revision_changed_or_incomplete"):
        asyncio.run(
            db.tx(
                "reanalysis_stale",
                lambda repos: repos.news.request_reanalysis(
                    event_id=EVENT,
                    expected_wanted_revision=1,
                    expected_head_revision=head.content_revision,
                    read_ref=read_ref,
                    reason="stale",
                    now_ms=clock(),
                ),
            )
        )


def test_snapshot_member_scopes_recover_each_fact_from_its_own_snapshot() -> None:
    from tracefold.news.events.facts import FactUnit

    pg, db, clock = store()
    seed_event(text="1. Agency suspends withdrawals.\n2. Beta releases earnings.\n3. Gamma opens a factory.")
    item = f"it-{EVENT}"
    first = FactUnit("withdrawals", 1, "Agency suspends withdrawals.", "Exchange bulletin", 3, 30, "explicit_numbered")
    second = FactUnit("earnings", 2, "Beta releases earnings.", "Company bulletin", 35, 55, "explicit_numbered")
    sql("DELETE FROM news_event_members WHERE event_id=%s", (EVENT,))
    for i, fact in enumerate((first, second)):
        sql(
            "INSERT INTO news_event_members(event_id,item_id,joined_at_ms,match_kind,fact_id,fact_text) "
            "VALUES(%s,%s,%s,'leader',%s,%s)",
            (EVENT, item, STAMP + i, fact.fact_id, fact.text),
        )
        asyncio.run(
            db.tx(
                "snapshot",
                lambda r, i=i, fact=fact: r.news.append_evidence_snapshot(
                    event_id=EVENT, now_ms=clock() + i, focus_item_id=item, focus_fact=fact
                ),
            )
        )
    source = asyncio.run(pg.input_for(EVENT))
    assert [(s.fact_id, s.fact_text, s.context) for s in source.extraction_scopes] == [
        (first.fact_id, first.text, first.context),
        (second.fact_id, second.text, second.context),
    ]
    assert len(source.evidence) == 1 and "Gamma opens" in source.evidence[0].text


def test_exact_digest_member_has_its_own_scope_without_focus_snapshot() -> None:
    from tracefold.news.events.facts import extract_fact_units

    pg, db, clock = store()
    body = (
        "1. Agency suspends withdrawals.\n"
        "2. Beta releases earnings.\n"
        "3. Gamma opens a factory.\n"
        "（以上内容仅供参考，不构成投资建议）"
    )
    seed_event(text=body, title="Daily digest")
    leader_item = f"it-{EVENT}"
    exact_item = f"{leader_item}-exact"
    leader_fact = extract_fact_units(item_id=leader_item, raw_text=body, fallback_title="Daily digest")[0]
    exact_fact = extract_fact_units(item_id=exact_item, raw_text=body, fallback_title="Daily digest")[0]
    sql(
        """
        INSERT INTO news_items (
          item_id, source_id, source_item_key, title, raw_first_line, description, canonical_url,
          reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
          first_ingest_mode, trace_id, created_at_ms, updated_at_ms, source_artifact_id,
          evidence_text, evidence_text_sha256
        )
        SELECT %s, source_id, %s, title, raw_first_line, description, canonical_url,
               reporting_origin, published_at_ms, observed_at_ms, provider_metadata, provenance,
               first_ingest_mode, trace_id, created_at_ms, updated_at_ms, %s,
               evidence_text, evidence_text_sha256
          FROM news_items WHERE item_id=%s
        """,
        (exact_item, exact_item, exact_item, leader_item),
    )
    sql("DELETE FROM news_event_members WHERE event_id=%s", (EVENT,))
    for item_id, fact, match_kind in (
        (leader_item, leader_fact, "leader"),
        (exact_item, exact_fact, "exact"),
    ):
        sql(
            "INSERT INTO news_event_members(event_id,item_id,joined_at_ms,match_kind,fact_id,fact_text) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (EVENT, item_id, STAMP, match_kind, fact.fact_id, fact.text),
        )
    asyncio.run(
        db.tx(
            "snapshot",
            lambda r: r.news.append_evidence_snapshot(
                event_id=EVENT, now_ms=clock(), focus_item_id=leader_item, focus_fact=leader_fact
            ),
        )
    )
    source = asyncio.run(pg.input_for(EVENT))
    assert len(source.evidence) == len(source.extraction_scopes) == 2
    assert {scope.fact_id for scope in source.extraction_scopes} == {leader_fact.fact_id, exact_fact.fact_id}
    for view in reading_views(source):
        assert view.mode == "scoped"
        shown = " ".join(span.text for span in view.spans)
        assert "Agency suspends withdrawals" in shown
        assert "不构成投资建议" in shown
        assert "Beta releases earnings" not in shown
