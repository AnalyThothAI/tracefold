"""Semantic and notification stores over real PostgreSQL: every port guarantee, races and replays (#706)."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_0424_sql import (
    ANALYSES_SQL,
    ANALYSIS_HEADS_SQL,
    CLAIM_LINKS_SQL,
    NOTIFICATION_DECISIONS_SQL,
    NOTIFY_JOBS_SQL,
    SEMANTIC_JOBS_SQL,
    SEMANTIC_RESULTS_SQL,
    UPDATE_PENDING_SQL,
    UPDATE_RECEIPTS_SQL,
)
from tests.support.news_current_delivery import seed_delivery
from tests.support.news_event_updates import persist_analysis_document
from tests.support.news_reader import PushAll
from tests.support.news_recall_window import (
    PROBE_RECEIPT,
    PROBE_STATEMENT,
    PROBE_STATEMENT_ZH,
    gold_fixture,
    gold_window_filler,
    probe_window,
)
from tests.support.news_update_pg import (
    EVENT,
    STAMP,
    TEXT,
    Clock,
    Composer,
    Sender,
    StubAnalyzer,
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
    set_semantic_job,
    sql,
    store,
    trade_rows,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.entities import asset_retrieval_symbols, commodity_name_patterns
from tracefold.news.market_review.instruments import COMMODITY_SYMBOLS
from tracefold.news.notifications.contracts import ClaimDecision, FrozenCard, NotificationPlan
from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt, ReaderNovelty, reader_novelty
from tracefold.news.notifications.planner import NotificationPlanner
from tracefold.news.notifications.ports import SendOutcome
from tracefold.news.notifications.recall import (
    RecallCandidate,
    RouteEvidence,
    lexical_evidence,
    query_for_claim,
    select_for_claim,
)
from tracefold.news.notifications.service import Notifications
from tracefold.news.storage.errors import EventUpdateConflict, IntentLeaseLost
from tracefold.news.storage.judgment_store import PgJudgmentCache
from tracefold.news.storage.semantic_input import frozen_input
from tracefold.news.updates.contracts import (
    Asset,
    Citation,
    Claim,
    DraftClaim,
    EventUpdate,
    Extraction,
    FrozenInput,
    PriorClaim,
    ReadTarget,
    RelationDraft,
    SupportDraft,
)
from tracefold.news.updates.identity import digest, identity
from tracefold.news.updates.judgment import Answer
from tracefold.news.updates.ports import SemanticObservation
from tracefold.news.updates.projection import reading_views
from tracefold.news.updates.public import public_updates

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def head_claim(claim_ref: str, *, statement: str | None = None, **fields: Any) -> dict[str, Any]:
    """The adopted head's claim document under another ref, with `statement` and `fields` replaced."""

    document = sql(f"SELECT document FROM ({ANALYSES_SQL}) WHERE event_id = %s", (EVENT,))[0]["document"]
    claim = copy.deepcopy(document["claims"][0])
    claim["ref"] = claim_ref
    claim["fields"].update(fields)
    if statement is not None:
        claim["statement"] = statement
    return claim


def seed_update_version(
    event_id: str, *, content_revision: str, claims: list[dict[str, Any]], head: bool = False
) -> None:
    """An adopted version of another Event carrying exactly `claims`; `head` also makes it that Event's head."""

    source = sql(f"SELECT document, observation_result_id FROM ({ANALYSES_SQL}) WHERE event_id = %s", (EVENT,))[0]
    document = {
        **source["document"],
        "event_id": event_id,
        "content_revision": content_revision,
        "previous_content_revision": None,
        "claims": claims,
    }

    def persist(repos):
        return persist_analysis_document(repos.conn, document, adopted_at_ms=STAMP - 10_000, head=head)

    ThreadedDb()._run("seed-analysis", persist)


def seed_sent_claim_projection(event_id: str, *, content_revision: str, claim_ref: str, related: bool = True) -> None:
    """Give a synthetic receipt the frozen claim version it actually says it carried."""

    unrelated = {"subject": "Miner", "assets": [{"symbol": "CL", "market_type": "commodity", "role": "primary"}]}
    claim = (
        head_claim(claim_ref)
        if related
        else head_claim(claim_ref, statement="Miner halts a Chilean copper pit", **unrelated)
    )
    seed_update_version(event_id, content_revision=content_revision, claims=[claim])


def freeze_receipt(intent_id: str) -> None:
    sql(
        f"""UPDATE news_notifications d SET sent_claims=(
             SELECT COALESCE(jsonb_agg(claim), '[]'::jsonb)
             FROM jsonb_array_elements(u.document->'claims') claim
             WHERE d.claim_refs ? (claim->>'ref'))
           FROM ({ANALYSES_SQL}) u WHERE d.intent_id=%s
             AND u.event_id=d.event_id AND u.content_revision=d.content_revision""",
        (intent_id,),
    )


def seed_sent_receipt(
    event_id: str, *, intent_id: str, content_revision: str, claim_refs: list[str], body: str, settled_at_ms: int
) -> None:
    """A delivered `update` receipt that carried `claim_refs` of that exact Event version."""

    sql(
        """WITH source(intent_id,event_id,kind,state,card,receipt,attempted_at_ms,settled_at_ms,created_at_ms,
        history_context,content_revision,claim_refs,body,payload_sha256,plan_key) AS (VALUES (%s, %s, 'update', 'sent',
        '{}'::jsonb, '{}'::jsonb, %s, %s, %s, '{}'::jsonb, %s, %s::jsonb,
                %s, %s, false))
INSERT INTO news_notifications(intent_id,event_id,kind,state,card,receipt,attempted_at_ms,settled_at_ms,
        created_at_ms,history_context,content_revision,claim_refs,plan_key,notification_id,origin,updated_at_ms)
SELECT s.intent_id::text,s.event_id::text,s.kind::text,s.state::text,s.card::jsonb||jsonb_build_object('body',
        s.body::text,'payload_sha256',s.payload_sha256::text),s.receipt::jsonb,s.attempted_at_ms::bigint,
        s.settled_at_ms::bigint,s.created_at_ms::bigint,s.history_context::jsonb,s.content_revision::text,
        s.claim_refs::jsonb,s.plan_key::boolean,s.intent_id::text,'legacy_delivery',s.created_at_ms::bigint FROM source
        s
ON CONFLICT(notification_id) DO UPDATE SET intent_id=EXCLUDED.intent_id,state=EXCLUDED.state,card=EXCLUDED.card,
        receipt=EXCLUDED.receipt,attempted_at_ms=EXCLUDED.attempted_at_ms,settled_at_ms=EXCLUDED.settled_at_ms,
        created_at_ms=EXCLUDED.created_at_ms,history_context=EXCLUDED.history_context,
        content_revision=EXCLUDED.content_revision,claim_refs=EXCLUDED.claim_refs,plan_key=EXCLUDED.plan_key,
        updated_at_ms=EXCLUDED.updated_at_ms""",
        (
            intent_id,
            event_id,
            settled_at_ms,
            settled_at_ms,
            settled_at_ms,
            content_revision,
            json.dumps(claim_refs),
            body,
            digest(body),
        ),
    )

    freeze_receipt(intent_id)


def seed_window_receipts(rows: list[tuple[str, str, int]], *, event_id: str = "window-filler") -> None:
    """Sent receipts `(intent_id, body, settled_at_ms)` projecting no claim: the rest of a window."""

    seed_event(event_id, title=event_id, fingerprint=event_id, at_ms=STAMP - 7_200_000)
    seed_update_version(event_id, content_revision=digest(event_id), claims=[])
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction(), conn.cursor() as cursor:
            cursor.executemany(
                """WITH source(intent_id,event_id,kind,state,card,receipt,attempted_at_ms,settled_at_ms,created_at_ms,
        history_context,content_revision,claim_refs,body,payload_sha256,plan_key,sent_claims) AS (VALUES (%s, %s,
        'update', 'sent', '{}'::jsonb, '{}'::jsonb, %s, %s, %s, '{}'::jsonb, %s,
                        '["cl:window-filler"]'::jsonb, %s, %s, false, '[]'::jsonb))
INSERT INTO news_notifications(intent_id,event_id,kind,state,card,receipt,attempted_at_ms,settled_at_ms,
        created_at_ms,history_context,content_revision,claim_refs,plan_key,sent_claims,notification_id,origin,
        updated_at_ms)
SELECT s.intent_id::text,s.event_id::text,s.kind::text,s.state::text,s.card::jsonb||jsonb_build_object('body',
        s.body::text,'payload_sha256',s.payload_sha256::text),s.receipt::jsonb,s.attempted_at_ms::bigint,
        s.settled_at_ms::bigint,s.created_at_ms::bigint,s.history_context::jsonb,s.content_revision::text,
        s.claim_refs::jsonb,s.plan_key::boolean,s.sent_claims::jsonb,s.intent_id::text,'legacy_delivery',
        s.created_at_ms::bigint FROM source s
ON CONFLICT(notification_id) DO UPDATE SET intent_id=EXCLUDED.intent_id,state=EXCLUDED.state,card=EXCLUDED.card,
        receipt=EXCLUDED.receipt,attempted_at_ms=EXCLUDED.attempted_at_ms,settled_at_ms=EXCLUDED.settled_at_ms,
        created_at_ms=EXCLUDED.created_at_ms,history_context=EXCLUDED.history_context,
        content_revision=EXCLUDED.content_revision,claim_refs=EXCLUDED.claim_refs,plan_key=EXCLUDED.plan_key,
        sent_claims=EXCLUDED.sent_claims,updated_at_ms=EXCLUDED.updated_at_ms""",
                [
                    (intent_id, event_id, at_ms, at_ms, at_ms, digest(event_id), body, digest(body))
                    for intent_id, body, at_ms in rows
                ],
            )
    finally:
        conn.close()


# ------------------------------------------------------------------ identities


def test_text_digest_matches_the_python_identity() -> None:
    for body in ('标题\n\n第一行 "引号" \\ tab\t', "é combining", "emoji 🚀"):
        assert sql("SELECT news_text_digest(%s) AS sha", (body,))[0]["sha"] == digest(body)


# ------------------------------------------------------------------ semantic turn


def test_agent_turn_adopts_once_with_public_row_and_pending_notification() -> None:
    pg, db, clock = store()
    seed_event()
    analyzer = StubAnalyzer()
    source = asyncio.run(pg.semantic.input_for(EVENT))
    assert source.revision == 1 and source.lineage_id == f"lineage-{EVENT}"
    assert [item.text for item in source.evidence] == [TEXT]
    assert source.evidence[0].source.source_authority == "reputable_secondary"
    assert source.evidence[0].source.first_available_at_ms == STAMP

    assert asyncio.run(run_agent(agent(pg.semantic, clock, analyzer), EVENT)) == "adopted"
    head = asyncio.run(pg.semantic.head(EVENT))
    assert head is not None and head.input_revision == 1
    work = sql(f"SELECT wanted_revision, done_revision, lease_token, last_outcome FROM ({SEMANTIC_JOBS_SQL})")[0]
    assert work == {"wanted_revision": 1, "done_revision": 1, "lease_token": None, "last_outcome": "adopted"}
    rows = trade_rows()
    assert [(row["kind"], row["source_fact_key"], row["source_revision"]) for row in rows] == [
        ("catalyst", EVENT, head.content_revision)
    ]
    assert rows[0]["payload"]["schema_version"] == "news_public_update_v1"
    notification = sql(f"SELECT channel, state, content_revision, decision_ref FROM ({NOTIFY_JOBS_SQL})")[0]
    assert notification == {
        "channel": "news",
        "state": "pending",
        "content_revision": head.content_revision,
        "decision_ref": None,
    }

    # A replay of the same work reuses its checkpoints and adopts nothing new.
    assert asyncio.run(run_agent(agent(pg.semantic, clock, analyzer), EVENT)) == "unchanged"
    assert analyzer.extract_calls == 1
    assert sql(f"SELECT count(*) AS n FROM ({ANALYSES_SQL})")[0]["n"] == 1
    assert len(trade_rows()) == 1
    assert "news_update_adopt" in db.names


def test_checkpoints_and_observations_are_insert_only() -> None:
    pg, _db, _clock = store()
    seed_event()
    source = asyncio.run(pg.semantic.input_for(EVENT))
    first = extraction_for(source)
    other = Extraction(claims=(draft(source.evidence[0], action="denies tariff"),))
    assert asyncio.run(pg.semantic.save_extraction("work-1", first)) == first
    assert asyncio.run(pg.semantic.save_extraction("work-1", other)) == first
    checkpoint = asyncio.run(pg.semantic.checkpoint("work-1"))
    assert checkpoint is not None and checkpoint.extraction == first

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
    assert asyncio.run(pg.semantic.save_observation(observation)) == observation
    replay = observation.model_copy(update={"completed_at_ms": STAMP + 999})
    assert asyncio.run(pg.semantic.save_observation(replay)).completed_at_ms == STAMP + 10
    with pytest.raises(EventUpdateConflict, match="news_semantic_observation_conflict"):
        asyncio.run(pg.semantic.save_observation(observation.model_copy(update={"program_identity": "other"})))


def test_two_adopters_of_one_head_adopt_exactly_once() -> None:
    pg, _db, _clock = store()
    seed_event()
    base = asyncio.run(pg.semantic.input_for(EVENT))

    async def race() -> list[bool]:
        results = await asyncio.gather(
            *(
                adopt_next(
                    pg.semantic,
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
    assert sql(f"SELECT count(*) AS n FROM ({ANALYSES_SQL})")[0]["n"] == 1
    head = asyncio.run(pg.semantic.head(EVENT))
    assert head is not None
    assert [row["source_revision"] for row in trade_rows()] == [head.content_revision]
    assert sql(f"SELECT content_revision FROM ({NOTIFY_JOBS_SQL})")[0]["content_revision"] == head.content_revision


def test_adoption_never_downgrades_the_input_revision() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    stale = FrozenInput(
        event_id=EVENT,
        revision=1,
        lineage_id="lineage",
        evidence=(evidence("Agency adds aluminium to the tariff."),),
        prior=tuple(PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=c) for c in head.claims),
    )
    newer = stale.model_copy(update={"revision": 2})
    extracted = extraction_for(newer)
    adopted, update = asyncio.run(adopt_next(pg.semantic, head, newer, extracted))
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
        asyncio.run(adopt_next(pg.semantic, update, older, extraction_for(older), work_id="work-old"))
    assert sql(f"SELECT input_revision FROM ({ANALYSIS_HEADS_SQL})")[0]["input_revision"] == 2


def test_possible_new_is_adopted_and_marked_for_notification_without_a_public_row() -> None:
    pg, _db, _clock = store()
    seed_event()
    seed_event("ev-other", text="Agency orders a 25% tariff on steel.", fingerprint="fp-other")
    other_head_claim = asyncio.run(adopt_other_event(pg.semantic))
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
    adopted, update = asyncio.run(adopt_next(pg.semantic, None, source, extracted))
    assert adopted
    assert [change.kind for change in update.changes] == ["possible_new"]
    assert [row for row in trade_rows() if row["source_fact_key"] == EVENT] == []
    work = sql(f"SELECT state, content_revision FROM ({NOTIFY_JOBS_SQL}) WHERE event_id = %s", (EVENT,))[0]
    assert work == {"state": "pending", "content_revision": update.content_revision}


def test_claim_links_outlive_the_revision_that_asserted_them_and_are_read_from_both_ends() -> None:
    """#742: a later revision that no longer repeats a comparison does not lose it; both Events see the link."""

    pg, _db, _clock = store()
    seed_event()
    seed_event("ev-other", text="Agency orders a 25% tariff on steel.", fingerprint="fp-other")
    other = asyncio.run(adopt_other_event(pg.semantic))
    first_source = FrozenInput(
        event_id=EVENT, revision=1, lineage_id="lineage", evidence=(evidence(TEXT),), prior=(other,)
    )
    linked = Extraction(
        claims=(draft(first_source.evidence[0]),),
        relations=(
            RelationDraft(slot="a", previous_ref=other.claim.ref, relation="adds_information", change_kind="new_fact"),
        ),
    )
    adopted, first = asyncio.run(adopt_next(pg.semantic, None, first_source, linked))
    assert adopted
    rows = sql(
        "SELECT update_ref,current_ref,previous_ref,relation,current_event_id,previous_event_id "
        f"FROM ({CLAIM_LINKS_SQL})"
    )
    assert rows == [
        {
            "update_ref": first.ref,
            "current_ref": first.claims[0].ref,
            "previous_ref": other.claim.ref,
            "relation": "adds_information",
            "current_event_id": EVENT,
            "previous_event_id": "ev-other",
        }
    ]
    correction = evidence("Correction: Agency orders a 50% tariff, not 25%.")
    second_source = FrozenInput(
        event_id=EVENT,
        revision=2,
        lineage_id="lineage",
        evidence=(correction,),
        prior=tuple(PriorClaim(event_id=EVENT, content_revision=first.content_revision, claim=c) for c in first.claims),
    )
    corrected = Extraction(
        claims=(draft(correction, action="orders 50% tariff"),),
        relations=(
            RelationDraft(slot="a", previous_ref=first.claims[0].ref, relation="corrects", change_kind="correction"),
        ),
    )
    adopted, second = asyncio.run(adopt_next(pg.semantic, first, second_source, corrected))
    assert adopted and all(change.previous_ref != other.claim.ref for change in second.changes)
    assert len(sql(f"SELECT 1 FROM ({CLAIM_LINKS_SQL})")) == 2
    own = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    theirs = asyncio.run(pg.notifications.notification_snapshot("ev-other", "news"))
    assert own is not None and theirs is not None
    pair = (first.claims[0].ref, other.claim.ref, "adds_information")
    assert pair in {(link.current_ref, link.previous_ref, link.relation) for link in own.reader.links}
    assert pair in {(link.current_ref, link.previous_ref, link.relation) for link in theirs.reader.links}


def test_a_correction_is_a_source_update_outbox_row_in_the_app_relay_mapping() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
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
    adopted, update = asyncio.run(adopt_next(pg.semantic, head, source, extracted))
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
            return repositories_for_connection(conn).news.semantic_work.semantic_status(now_ms=now_ms)
        finally:
            conn.close()

    assert (status()["semantic_pending"], status()["semantic_deferred"], status()["semantic_failed_exhausted"]) == (
        1,
        0,
        0,
    )
    set_semantic_job(None, next_attempt_at_ms=now_ms + 60_000)
    set_semantic_job(None, attempts=3, last_outcome="failed", last_error_code="output_truncated")
    assert (status()["semantic_pending"], status()["semantic_deferred"], status()["semantic_failed_exhausted"]) == (
        0,
        0,
        1,
    )


def test_equivalent_with_external_conflict_keeps_one_claim_through_adoption_plan_and_receipt() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    sender = Sender("sent")
    composer = Composer()
    assert asyncio.run(notifications(pg.notifications, clock, sender, composer).process(EVENT, "news")) == "sent"
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
    adopted, update = asyncio.run(adopt_next(pg.semantic, head, source, extracted))
    assert adopted and [claim.ref for claim in update.claims] == [head.claims[0].ref]
    assert {row["kind"] for row in trade_rows()} == {"catalyst", "source_update"}
    assert len(update.evidence_relations) == 2
    assert any(change.relation == "conflicts" for change in update.changes)

    clock.now_ms += 1_000  # A receipt settled at the prior turn's clock is now visible to history.
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None and len(snapshot.reader.receipts) == 1
    result = asyncio.run(notifications(pg.notifications, clock, sender, composer).process(EVENT, "news"))
    assert result == "no_notification"
    assert len(sender.cards) == 1 and composer.calls == 1
    assert sql(f"SELECT count(*) AS n FROM ({UPDATE_RECEIPTS_SQL}) WHERE state='sent'")[0]["n"] == 1


def test_empty_extraction_records_the_task_read_and_does_not_loop_on_the_same_source() -> None:
    pg, _db, clock = store()
    seed_event()
    analyzer = StubAnalyzer(lambda _source: Extraction(claims=()))
    # #742 W1: a first read without claims is not an Event version; it settles its read all the same.
    assert asyncio.run(run_agent(agent(pg.semantic, clock, analyzer), EVENT)) == "unchanged"
    assert asyncio.run(pg.semantic.head(EVENT)) is None
    assert sql(f"SELECT count(*) AS n FROM ({NOTIFY_JOBS_SQL}) WHERE event_id=%s", (EVENT,))[0]["n"] == 0
    first = sql(f"SELECT processed_read_refs,done_revision FROM ({SEMANTIC_JOBS_SQL}) WHERE event_id=%s", (EVENT,))[0]
    assert len(first["processed_read_refs"]) == 1 and first["done_revision"] == 1
    observed = sql(f"SELECT read_refs FROM ({SEMANTIC_RESULTS_SQL}) WHERE event_id=%s", (EVENT,))[0]
    assert observed["read_refs"] == first["processed_read_refs"]

    set_semantic_job(EVENT, wanted_revision=2)
    assert asyncio.run(run_agent(agent(pg.semantic, clock, analyzer), EVENT)) == "unchanged"
    assert analyzer.extract_calls == 1
    second = sql(f"SELECT processed_read_refs,done_revision FROM ({SEMANTIC_JOBS_SQL}) WHERE event_id=%s", (EVENT,))[0]
    assert second == {"processed_read_refs": first["processed_read_refs"], "done_revision": 2}


def test_semantic_work_is_leased_bounded_and_reopened_by_a_new_revision() -> None:
    pg, _db, clock = store()
    seed_event()
    lease = asyncio.run(pg.semantic.claim_semantic_work(EVENT, lease_ms=30_000))
    assert lease is not None and lease.wanted_revision == 1 and lease.attempts == 1
    assert asyncio.run(pg.semantic.claim_semantic_work(EVENT, lease_ms=30_000)) is None
    for attempt in range(1, 4):
        if attempt > 1:
            clock.now_ms += 10 * 60_000
            lease = asyncio.run(pg.semantic.claim_semantic_work(EVENT, lease_ms=30_000))
            assert lease is not None and lease.attempts == attempt
        assert lease is not None
        asyncio.run(pg.semantic.defer_semantic_event(lease, reason="provider_unavailable"))
    clock.now_ms += 10 * 60_000
    assert asyncio.run(pg.semantic.claim_semantic_work(EVENT, lease_ms=30_000)) is None
    assert asyncio.run(pg.semantic.pending_semantic_events(10)) == ()
    row = sql(f"SELECT attempts, last_outcome, last_error_code FROM ({SEMANTIC_JOBS_SQL})")[0]
    assert row == {"attempts": 3, "last_outcome": "failed", "last_error_code": "provider_unavailable"}

    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            revision = repositories_for_connection(conn).news.semantic_work.request_semantic_revision(
                event_id=EVENT, lineage_id="lineage-2", now_ms=clock.now_ms
            )
    finally:
        conn.close()
    assert revision == 2
    # A new revision is new work: the failed outcome, its code and the spent attempts belong to the old one.
    row = sql(f"SELECT attempts, last_outcome, last_error_code FROM ({SEMANTIC_JOBS_SQL})")[0]
    assert row == {"attempts": 0, "last_outcome": None, "last_error_code": None}
    assert asyncio.run(pg.semantic.pending_semantic_events(10)) == (EVENT,)
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            news = repositories_for_connection(conn).news
            # A wake recorded for an older revision does not hide the newer one from repair.
            assert (
                news.semantic_work.mark_semantic_work_published(event_id=EVENT, revision=1, now_ms=clock.now_ms)
                is False
            )
            assert (
                news.semantic_work.mark_semantic_work_published(event_id=EVENT, revision=2, now_ms=clock.now_ms) is True
            )
    finally:
        conn.close()
    assert asyncio.run(pg.semantic.pending_semantic_events(10)) == ()
    clock.now_ms += 20_000
    assert asyncio.run(pg.semantic.pending_semantic_events(10)) == (EVENT,)


def test_one_optional_read_per_lineage_and_its_attached_input() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    source = asyncio.run(pg.semantic.input_for(EVENT))
    assert asyncio.run(pg.semantic.reserve_extra_read(source.lineage_id, "target-1"))
    assert not asyncio.run(pg.semantic.reserve_extra_read(source.lineage_id, "target-2"))
    read = evidence("The ministry's order lists steel and aluminium.", publisher="ministry")
    target = ReadTarget(ref="target-1", action="read_current_artifact", description="ministry order")
    asyncio.run(pg.semantic.attach_extra_evidence(source, target, (read,), (head.claims[0].ref,)))
    asyncio.run(pg.semantic.record_read_outcome(source.lineage_id, outcome="attached"))
    attached = asyncio.run(pg.semantic.input_for(EVENT))
    assert attached.revision == 2 and attached.lineage_id == source.lineage_id
    assert attached.evidence == (read,) and attached.focus_claim_refs == (head.claims[0].ref,)
    assert [row.claim.ref for row in attached.prior] == [head.claims[0].ref]
    assert sql(f"SELECT extra_read_state FROM ({SEMANTIC_JOBS_SQL})")[0]["extra_read_state"] == "attached"
    assert not asyncio.run(pg.semantic.reserve_extra_read(source.lineage_id, "target-3"))


# ------------------------------------------------------------------ notifications


def test_a_notification_turn_sends_once_and_keeps_the_exact_receipt() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    sender = Sender("sent")
    assert asyncio.run(notifications(pg.notifications, clock, sender).process(EVENT, "news")) == "sent"
    card = sender.cards[0]
    ledger = sql(f"SELECT * FROM ({UPDATE_RECEIPTS_SQL})")[0]
    assert ledger["kind"] == "update" and ledger["state"] == "sent"
    assert ledger["intent_id"] == card.intent_id and ledger["body"] == card.body
    assert ledger["payload_sha256"] == card.payload_sha256 == digest(card.body)
    assert ledger["decision_ref"] == sql(f"SELECT decision_ref FROM ({NOTIFY_JOBS_SQL})")[0]["decision_ref"]
    assert ledger["claim_refs"] == [head.claims[0].ref] and ledger["content_revision"] == head.content_revision
    assert ledger["receipt"]["provider_message_id"] == "41"
    # The provider's own receipt is kept beside it: what the Telegram enrichment edit is fenced by.
    assert (ledger["receipt"]["message_id"], ledger["receipt"]["target_sha256"]) == (41, "a" * 64)
    # The ledger's `card` is the frozen card itself, headline at the top level for the read side.
    assert FrozenCard.model_validate(ledger["card"]) == card and ledger["card"]["headline_zh"] == card.headline_zh
    assert ledger["history_context"]["headline_zh"] == card.headline_zh
    assert sql(f"SELECT count(*) AS n FROM ({UPDATE_PENDING_SQL})")[0]["n"] == 0
    work = sql(f"""SELECT w.state,d.plan AS plan FROM ({NOTIFY_JOBS_SQL}) w
                      LEFT JOIN ({NOTIFICATION_DECISIONS_SQL}) d ON d.decision_ref=w.decision_ref""")[0]
    assert work["state"] == "done"
    assert [row["reason"] for row in work["plan"]["claim_decisions"]] == ["reader_push"]
    assert sql("SELECT to_regclass('news_notification_review_tasks_v1') AS relation") == [{"relation": None}]
    assert sql("SELECT notification_id,state,card->>'body' AS body FROM news_notifications") == [
        {"notification_id": ledger["decision_ref"], "state": "sent", "body": card.body}
    ]
    # Done for this head: a second turn has no work and never resends.
    assert asyncio.run(notifications(pg.notifications, clock, sender).process(EVENT, "news")) == "no_work"
    assert len(sender.cards) == 1


def test_two_planners_reserve_one_intent_without_resetting_it() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)

    async def race() -> list[Any]:
        return list(
            await asyncio.gather(pg.notifications.atomic_record_plan(plan), pg.notifications.atomic_record_plan(plan))
        )

    leases = [result.lease for result in asyncio.run(race())]
    assert sum(lease is not None for lease in leases) == 1
    winner = next(lease for lease in leases if lease is not None)
    queued = sql(
        f"SELECT intent_id, lease_token, attempts, content_revision, claim_refs, plan_key FROM ({UPDATE_PENDING_SQL})"
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
    work = sql(f"SELECT state, next_attempt_at_ms FROM ({NOTIFY_JOBS_SQL})")[0]
    assert work == {"state": "pending", "next_attempt_at_ms": clock.now_ms + 120_000}
    assert asyncio.run(pg.notifications.pending_notification_events("news", 10)) == ()


def test_a_turn_that_dies_after_reserving_is_reclaimed_after_its_lease() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    orphan = asyncio.run(pg.notifications.atomic_record_plan(notify_plan(head, snapshot.reader.revision))).lease
    assert orphan is not None
    clock.now_ms += 120_001
    assert asyncio.run(pg.notifications.pending_notification_events("news", 10)) == (EVENT,)
    sender = Sender("sent")
    assert asyncio.run(notifications(pg.notifications, clock, sender).process(EVENT, "news")) == "sent"
    assert sender.cards[0].intent_id == orphan.intent_id
    assert sql(f"SELECT attempts FROM ({UPDATE_PENDING_SQL})") == []
    assert sql(f"SELECT state FROM ({NOTIFY_JOBS_SQL})")[0]["state"] == "done"


def test_the_janitor_holds_an_unsettled_update_send_ambiguous_and_releases_its_reservation() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)
    lease = asyncio.run(pg.notifications.atomic_record_plan(plan)).lease
    assert lease is not None
    body = "关税\n\n机构加征关税"
    card = FrozenCard(
        intent_id=lease.intent_id,
        claim_refs=plan.selected_claim_refs,
        headline_zh="关税",
        body=body,
        payload_sha256=digest(body),
    )
    asyncio.run(save_card(pg.notifications, lease, card))
    assert asyncio.run(pg.notifications.atomic_begin_send(lease, card)) == "begun"
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            settled = repositories_for_connection(conn).news.notification_delivery.terminalize_interrupted_deliveries(
                now_ms=clock.now_ms + 120_000
            )
    finally:
        conn.close()
    assert settled == 1
    assert sql(f"SELECT state, error_code FROM ({UPDATE_RECEIPTS_SQL})") == [
        {"state": "ambiguous", "error_code": "ambiguous_after_crash"}
    ]
    assert sql(f"SELECT count(*) AS n FROM ({UPDATE_PENDING_SQL})")[0]["n"] == 0


@pytest.mark.parametrize("related", [False, True])
def test_only_a_related_receipt_settled_after_the_snapshot_races_the_plan(related: bool) -> None:
    """A newly sent receipt for a matching delivered claim changes both snapshot and CAS context."""

    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    seed_event("ev-other", fingerprint="fp-other", title="Chile pit halted", text="Miner halts copper pit.")
    seed_sent_claim_projection(
        "ev-other",
        content_revision=hashlib.sha256(b"ev-other").hexdigest(),
        claim_ref="cl:fixture",
        related=related,
    )
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
    committed = asyncio.run(pg.notifications.atomic_record_plan(notify_plan(head, snapshot.reader.revision)))
    if not related:
        assert committed.status == "committed" and committed.lease is not None
        return
    assert committed.status == "reader_changed"
    work = sql(f"SELECT state, decision_ref FROM ({NOTIFY_JOBS_SQL}) WHERE event_id = %s", (EVENT,))[0]
    assert work == {"state": "pending", "decision_ref": None}
    assert sql(f"SELECT count(*) AS n FROM ({UPDATE_PENDING_SQL})")[0]["n"] == 0
    # The losing plan's decision is kept, so the next turn reuses its judgment rather than asking again.
    assert sql(f"SELECT count(*) AS n FROM ({NOTIFICATION_DECISIONS_SQL}) WHERE event_id = %s", (EVENT,))[0]["n"] == 1


@pytest.mark.parametrize("related", [False, True])
def test_begin_send_uses_the_same_claim_context_as_the_snapshot(related: bool) -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)
    lease = asyncio.run(pg.notifications.atomic_record_plan(plan)).lease
    assert lease is not None
    body = "关税\n\n机构加征关税"
    card = FrozenCard(
        intent_id=lease.intent_id,
        claim_refs=plan.selected_claim_refs,
        headline_zh="关税",
        body=body,
        payload_sha256=digest(body),
    )
    asyncio.run(save_card(pg.notifications, lease, card))
    seed_event("ev-other", fingerprint="fp-other", title="Chile pit halted", text="Miner halts copper pit.")
    seed_sent_claim_projection(
        "ev-other",
        content_revision=hashlib.sha256(b"ev-other").hexdigest(),
        claim_ref="cl:fixture",
        related=related,
    )
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            seed_delivery(
                conn,
                event_id="ev-other",
                at_ms=clock.now_ms - 5_000,
                history_context={"comparison_title": "Chile pit halted"},
            )
    finally:
        conn.close()
    assert asyncio.run(pg.notifications.atomic_begin_send(lease, card)) == ("reader_changed" if related else "begun")
    own_sent = sql(f"SELECT count(*) AS n FROM ({UPDATE_RECEIPTS_SQL}) WHERE event_id = %s", (EVENT,))[0]["n"]
    assert own_sent == (0 if related else 1)


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
    assert asyncio.run(run_agent(agent(pg.semantic, clock, StubAnalyzer(two_claims)), EVENT)) == "adopted"
    head = asyncio.run(pg.semantic.head(EVENT))
    assert head is not None and len(head.claims) == 2
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision, deferred=(head.claims[1].ref,))
    lease = asyncio.run(pg.notifications.atomic_record_plan(plan)).lease
    assert lease is not None and lease.card is None
    work = sql(
        f"SELECT w.state,w.attempts,d.plan AS plan FROM ({NOTIFY_JOBS_SQL}) w "
        f"LEFT JOIN ({NOTIFICATION_DECISIONS_SQL}) d ON d.decision_ref=w.decision_ref"
    )[0]
    assert work["state"] == "pending" and work["attempts"] == 0
    assert {row["decision"] for row in work["plan"]["claim_decisions"]} == {"notify", "deferred"}


def test_not_sent_retries_the_same_identity_and_frozen_payload() -> None:
    pg, _db, clock = store()
    adopted_head(pg.semantic, clock)
    sender = Sender("not_sent", "sent")
    composer = Composer()
    turn = notifications(pg.notifications, clock, sender, composer)
    assert asyncio.run(turn.process(EVENT, "news")) == "not_sent"
    assert sql(f"SELECT count(*) AS n FROM ({UPDATE_RECEIPTS_SQL})")[0]["n"] == 0
    queued = sql(f"SELECT lease_token, attempts, frozen_card, error_code FROM ({UPDATE_PENDING_SQL})")[0]
    assert queued["lease_token"] is None and queued["attempts"] == 1 and queued["error_code"] == "rate_limited"
    assert sql(f"SELECT state FROM ({NOTIFY_JOBS_SQL})")[0]["state"] == "pending"

    clock.now_ms += 5 * 60_000
    assert asyncio.run(pg.notifications.pending_notification_events("news", 10)) == (EVENT,)
    assert asyncio.run(turn.process(EVENT, "news")) == "sent"
    assert composer.calls == 1
    assert sender.cards[0] == sender.cards[1]
    ledger = sql(f"SELECT state, payload_sha256, intent_id FROM ({UPDATE_RECEIPTS_SQL})")[0]
    assert ledger == {
        "state": "sent",
        "payload_sha256": sender.cards[0].payload_sha256,
        "intent_id": sender.cards[0].intent_id,
    }


def test_late_settlement_of_previous_unsent_lease_cannot_settle_next_send() -> None:
    pg, _db, clock = store()
    adopted_head(pg.semantic, clock)
    sender = Sender("not_sent")
    turn = notifications(pg.notifications, clock, sender)
    assert asyncio.run(turn.process(EVENT, "news")) == "not_sent"
    prior = sql(f"SELECT last_settlement FROM ({UPDATE_PENDING_SQL})")[0]["last_settlement"]
    clock.now_ms += 5 * 60_000
    prepared = asyncio.run(turn.service.prepare(EVENT, "news"))
    assert prepared.status == "ready" and prepared.lease is not None and prepared.card is not None
    assert asyncio.run(pg.notifications.atomic_begin_send(prepared.lease, prepared.card)) == "begun"
    old_lease = prepared.lease.model_copy(update={"lease_token": prior["lease_token"]})
    old_outcome = SendOutcome(
        state="not_sent",
        payload_sha256=prepared.card.payload_sha256,
        error_code="rate_limited",
        retryable=True,
    )
    assert (
        asyncio.run(pg.notifications.settle_send(old_lease, prepared.card, old_outcome, settled_at_ms=clock.now_ms))
        == "already_settled"
    )
    assert sql(f"SELECT state FROM ({UPDATE_RECEIPTS_SQL})")[0]["state"] == "sending"
    assert sql(f"SELECT attempts,lease_token FROM ({UPDATE_PENDING_SQL})")[0] == {
        "attempts": 1,
        "lease_token": prepared.lease.lease_token,
    }
    with pytest.raises(RuntimeError, match="news_send_settlement_conflict"):
        asyncio.run(
            pg.notifications.settle_send(
                old_lease,
                prepared.card,
                SendOutcome(state="sent", payload_sha256=prepared.card.payload_sha256, message_id="late"),
                settled_at_ms=clock.now_ms,
            )
        )
    assert sql(f"SELECT state FROM ({UPDATE_RECEIPTS_SQL})")[0]["state"] == "sending"


@pytest.mark.parametrize("state", ["sent", "ambiguous", "not_sent"])
def test_final_settlement_retry_matches_exact_lease_and_outcome(state: str) -> None:
    pg, _db, clock = store()
    adopted_head(pg.semantic, clock)
    turn = notifications(pg.notifications, clock, Sender(), Composer())
    prepared = asyncio.run(turn.service.prepare(EVENT, "news"))
    assert prepared.status == "ready" and prepared.lease is not None and prepared.card is not None
    lease, card = prepared.lease, prepared.card
    assert asyncio.run(pg.notifications.atomic_begin_send(lease, card)) == "begun"
    outcome = SendOutcome(
        state=state,
        payload_sha256=card.payload_sha256,
        message_id="provider-123" if state == "sent" else None,
        error_code="provider_failure" if state != "sent" else None,
        retryable=False,
    )
    settled = asyncio.run(pg.notifications.settle_send(lease, card, outcome, settled_at_ms=clock.now_ms))
    assert settled == {"sent": "sent", "ambiguous": "ambiguous", "not_sent": "terminal"}[state]
    if state == "sent":
        # A later receipt edit cannot erase the original settlement identity.
        sql("UPDATE news_notifications SET receipt = receipt || '{\"enriched\": true}'::jsonb")
    assert (
        asyncio.run(pg.notifications.settle_send(lease, card, outcome, settled_at_ms=clock.now_ms)) == "already_settled"
    )
    wrong_lease = lease.model_copy(update={"lease_token": "later-lease"})
    with pytest.raises(RuntimeError, match="news_send_settlement_conflict"):
        asyncio.run(pg.notifications.settle_send(wrong_lease, card, outcome, settled_at_ms=clock.now_ms))
    changed_outcome = outcome.model_copy(
        update={"message_id": "different"} if state == "sent" else {"error_code": "different"}
    )
    with pytest.raises(RuntimeError, match="news_send_settlement_conflict"):
        asyncio.run(pg.notifications.settle_send(lease, card, changed_outcome, settled_at_ms=clock.now_ms))


def test_an_ambiguous_send_is_possibly_sent_never_resent_and_never_holds_the_work() -> None:
    """#742 N5 (review D7). Before, an ambiguous claim deferred every later plan and spent its attempts
    until the work was exhausted. It may already be on the reader's screen: it is not sent again, and
    nothing waits for an outcome that will never come."""

    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    with pytest.raises(RuntimeError, match="provider connection dropped"):
        asyncio.run(notifications(pg.notifications, clock, Sender("raise")).process(EVENT, "news"))
    ledger = sql(f"SELECT state, error_code, claim_refs FROM ({UPDATE_RECEIPTS_SQL})")[0]
    assert ledger == {"state": "ambiguous", "error_code": "RuntimeError", "claim_refs": [head.claims[0].ref]}
    assert sql(f"SELECT count(*) AS n FROM ({UPDATE_PENDING_SQL})")[0]["n"] == 0
    assert sql(f"SELECT state FROM ({NOTIFY_JOBS_SQL})")[0]["state"] == "done"

    sql("UPDATE news_jobs SET state = 'pending'")
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None and snapshot.reader.ambiguous_claim_refs == (head.claims[0].ref,)
    assert snapshot.reader.blocked_claim_refs == () and snapshot.reader.receipts == ()
    sender = Sender("sent")
    assert asyncio.run(notifications(pg.notifications, clock, sender).process(EVENT, "news")) == "no_notification"
    assert sender.cards == []
    work = sql(
        f"SELECT w.state,w.attempts,d.plan AS plan FROM ({NOTIFY_JOBS_SQL}) w "
        f"LEFT JOIN ({NOTIFICATION_DECISIONS_SQL}) d ON d.decision_ref=w.decision_ref"
    )[0]
    assert (work["state"], work["attempts"]) == ("done", 0)
    assert work["plan"]["claim_decisions"][0]["reason"] == "send_outcome_ambiguous"
    assert sql(f"SELECT state FROM ({UPDATE_RECEIPTS_SQL})")[0]["state"] == "ambiguous"


def test_begin_send_rechecks_the_head_and_keeps_the_frozen_card_for_the_same_identity() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)
    lease = asyncio.run(pg.notifications.atomic_record_plan(plan)).lease
    assert lease is not None
    card = FrozenCard(
        intent_id=lease.intent_id,
        claim_refs=plan.selected_claim_refs,
        headline_zh="关税",
        body="关税\n\n机构加征关税",
        payload_sha256=digest("关税\n\n机构加征关税"),
    )
    assert asyncio.run(save_card(pg.notifications, lease, card)) == card
    other = card.model_copy(update={"body": "另一版本", "payload_sha256": digest("另一版本")})
    with pytest.raises(ValueError):
        asyncio.run(save_card(pg.notifications, lease, other))

    source = FrozenInput(
        event_id=EVENT,
        revision=2,
        lineage_id="lineage",
        evidence=(evidence("Agency adds aluminium to the tariff."),),
        prior=tuple(PriorClaim(event_id=EVENT, content_revision=head.content_revision, claim=c) for c in head.claims),
    )
    adopted, update = asyncio.run(adopt_next(pg.semantic, head, source, extraction_for(source)))
    assert adopted
    assert asyncio.run(pg.notifications.atomic_begin_send(lease, card)) == "head_changed"
    queued = sql(f"SELECT lease_token, frozen_card FROM ({UPDATE_PENDING_SQL})")[0]
    assert queued["lease_token"] is None and FrozenCard.model_validate(queued["frozen_card"]) == card
    assert sql(f"SELECT count(*) AS n FROM ({UPDATE_RECEIPTS_SQL})")[0]["n"] == 0
    work = sql(f"SELECT state, content_revision FROM ({NOTIFY_JOBS_SQL})")[0]
    assert work == {"state": "pending", "content_revision": update.content_revision}
    # The released lease no longer fences a card write.
    with pytest.raises(IntentLeaseLost):
        asyncio.run(save_card(pg.notifications, lease, card))


def test_snapshot_reads_current_sent_ledger() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    seed_event("ev-other", title="Agency orders steel tariff", fingerprint="fp-tariff", at_ms=STAMP - 7_200_000)
    seed_sent_claim_projection(
        "ev-other", content_revision=hashlib.sha256(b"ev-other").hexdigest(), claim_ref="cl:fixture"
    )
    card = {"header": {"title": {"content": "机构加征钢铁关税"}}}
    context = {"why_zh": "影响钢铁进口", "dedupe_family": "general", "comparison_fingerprint": "fp-tariff"}
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            seed_delivery(conn, event_id="ev-other", at_ms=STAMP - 3_600_000, history_context=context, card=card)
    finally:
        conn.close()
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None and snapshot.update == head
    assert len(snapshot.reader.receipts) == 1
    assert snapshot.reader.receipts[0].intent_id.startswith("intent:")
    receipts = sql(f"SELECT kind,state,card,body,payload_sha256 FROM ({UPDATE_RECEIPTS_SQL}) WHERE event_id='ev-other'")
    assert receipts == [
        {
            "kind": "update",
            "state": "sent",
            "card": {**card, "body": "机构加征钢铁关税", "payload_sha256": digest("机构加征钢铁关税")},
            "body": "机构加征钢铁关税",
            "payload_sha256": digest("机构加征钢铁关税"),
        }
    ]


def test_card_failure_releases_the_lease_and_the_third_fails_the_work() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    for attempt in range(1, 4):
        snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
        assert snapshot is not None
        lease = asyncio.run(pg.notifications.atomic_record_plan(notify_plan(head, snapshot.reader.revision))).lease
        assert lease is not None
        asyncio.run(pg.notifications.record_unsent_failure(lease, error_code="TimeoutError", retryable=True))
        queued = sql(f"SELECT state, attempts, lease_token FROM ({UPDATE_PENDING_SQL})")[0]
        assert queued["attempts"] == attempt and queued["lease_token"] is None
        clock.now_ms += 15 * 60_000
    assert queued["state"] == "dead"
    # The unsent intent ended, and the work says why, instead of closing as if it had been delivered.
    assert sql(f"SELECT state, last_error_code FROM ({NOTIFY_JOBS_SQL})")[0] == {
        "state": "failed",
        "last_error_code": "TimeoutError",
    }
    assert asyncio.run(pg.notifications.notification_snapshot(EVENT, "news")) is None


def test_judgment_cache_keeps_the_first_answer_and_retention_purges_old_rows() -> None:
    db = ThreadedDb()
    clock = Clock()
    cache = PgJudgmentCache(db, clock=clock)
    first = Answer(item_id="q1", value="full", backend="generated")
    second = Answer(item_id="q2", value="none", backend="generated")
    asyncio.run(cache.put_many({"key-1": first, "key-2": second}))
    asyncio.run(cache.put_many({"key-1": first.model_copy(update={"value": "none"})}))
    # One statement reads a whole question set; a missing key is simply absent.
    assert asyncio.run(cache.get_many(("key-1", "key-2", "missing"))) == {"key-1": first, "key-2": second}
    assert db.names.count("news_judgment_cache_get") == 1
    clock.now_ms += 15 * 24 * 3_600_000
    assert (
        asyncio.run(
            db.tx(
                "cache_retention",
                lambda repos: repos.news.judgment_cache.purge_semantic_caches(now_ms=clock(), limit=100),
            )
        )
        == 2
    )
    assert asyncio.run(cache.get_many(("key-1",))) == {}


def test_frozen_input_requires_material() -> None:
    with pytest.raises(LookupError, match="news_event_input_missing"):
        frozen_input(EVENT, {"work": None, "items": [], "item_ids": [], "head": None})


def test_a_no_notification_plan_is_final_for_the_head_and_retires_an_unsent_reservation() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    lease = asyncio.run(pg.notifications.atomic_record_plan(notify_plan(head, snapshot.reader.revision))).lease
    assert lease is not None
    asyncio.run(pg.notifications.record_unsent_failure(lease, error_code="TimeoutError", retryable=True))
    clock.now_ms += 5 * 60_000
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    silent = NotificationPlan(
        action="no_notification",
        reason="no_uncovered_actionable_claims",
        update_ref=head.ref,
        claim_decisions=tuple(
            ClaimDecision(claim_ref=claim.ref, decision="not_notified", reason="reader_feed") for claim in head.claims
        ),
        channel="news",
        reader_revision=snapshot.reader.revision,
        reader_identity="fixture_reader",
        input_digest=digest({"update": head.model_dump(mode="json"), "fixture": "silent"}),
    )
    assert asyncio.run(pg.notifications.atomic_record_plan(silent)).status == "committed"
    assert sql(f"SELECT count(*) AS n FROM ({UPDATE_PENDING_SQL})")[0]["n"] == 0
    work = sql(
        f"SELECT w.state,w.reader_revision,d.plan AS plan FROM ({NOTIFY_JOBS_SQL}) w "
        f"LEFT JOIN ({NOTIFICATION_DECISIONS_SQL}) d ON d.decision_ref=w.decision_ref"
    )[0]
    assert work["state"] == "done" and work["reader_revision"] == snapshot.reader.revision
    assert work["plan"]["claim_decisions"][0]["reason"] == "reader_feed"
    assert asyncio.run(pg.notifications.notification_snapshot(EVENT, "news")) is None


def test_snapshot_recalls_sent_claim_despite_many_unrelated_receipts() -> None:
    pg, _db, clock = store()
    adopted_head(pg.semantic, clock)
    leader = "Cryptocurrency prices remain stable across global markets"
    sql("UPDATE news_events SET comparison_title = %s WHERE event_id = %s", (leader, EVENT))

    def sent_update(event_id: str, title: str, body: str) -> None:
        seed_event(event_id, title=title, fingerprint=event_id, at_ms=STAMP - 21_600_000)
        seed_sent_claim_projection(
            event_id,
            content_revision="a" * 64,
            claim_ref="historical-claim",
            related=event_id == "actual-tariff",
        )
        context = {
            "comparison_title": title,
            "headline_zh": body,
            "why_zh": body,
            "dedupe_family": "general",
            "comparison_fingerprint": event_id,
        }
        sql(
            """WITH source(intent_id,event_id,kind,state,card,receipt,attempted_at_ms,settled_at_ms,created_at_ms,
        history_context,content_revision,claim_refs,body,payload_sha256,plan_key) AS (VALUES (%s, %s, 'update', 'sent',
        %s::jsonb, '{}'::jsonb, %s, %s, %s, %s::jsonb,
                    %s, '["historical-claim"]'::jsonb, %s, %s, false))
INSERT INTO news_notifications(intent_id,event_id,kind,state,card,receipt,attempted_at_ms,settled_at_ms,
        created_at_ms,history_context,content_revision,claim_refs,plan_key,notification_id,origin,updated_at_ms)
SELECT s.intent_id::text,s.event_id::text,s.kind::text,s.state::text,s.card::jsonb||jsonb_build_object('body',
        s.body::text,'payload_sha256',s.payload_sha256::text),s.receipt::jsonb,s.attempted_at_ms::bigint,
        s.settled_at_ms::bigint,s.created_at_ms::bigint,s.history_context::jsonb,s.content_revision::text,
        s.claim_refs::jsonb,s.plan_key::boolean,s.intent_id::text,'legacy_delivery',s.created_at_ms::bigint FROM source
        s
ON CONFLICT(notification_id) DO UPDATE SET intent_id=EXCLUDED.intent_id,state=EXCLUDED.state,card=EXCLUDED.card,
        receipt=EXCLUDED.receipt,attempted_at_ms=EXCLUDED.attempted_at_ms,settled_at_ms=EXCLUDED.settled_at_ms,
        created_at_ms=EXCLUDED.created_at_ms,history_context=EXCLUDED.history_context,
        content_revision=EXCLUDED.content_revision,claim_refs=EXCLUDED.claim_refs,plan_key=EXCLUDED.plan_key,
        updated_at_ms=EXCLUDED.updated_at_ms""",
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

        freeze_receipt(identity("intent", event_id))

    for index in range(35):
        sent_update(f"leader-noise-{index}", leader, "市场价格保持稳定")
    sent_update("actual-tariff", TEXT, "机构已宣布百分之二十五钢铁进口关税")
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    assert len(snapshot.reader.receipts) == 1
    assert snapshot.reader.receipts[0].intent_id == identity("intent", "actual-tariff")


def test_snapshot_keeps_earlier_incremental_receipt_and_excludes_future() -> None:
    pg, _db, clock = store()
    adopted_head(pg.semantic, clock)
    seed_event("incremental", title=TEXT, fingerprint="incremental")

    def sent_update(key: str, body: str, at_ms: int) -> str:
        intent = identity("intent", key)
        seed_sent_claim_projection("incremental", content_revision=digest(key), claim_ref="historical-claim")
        sql(
            """WITH source(intent_id,event_id,kind,state,card,receipt,attempted_at_ms,settled_at_ms,created_at_ms,
        history_context,content_revision,claim_refs,body,payload_sha256,plan_key) AS (VALUES (%s, 'incremental',
        'update', 'sent', '{}'::jsonb, '{}'::jsonb,
                    %s, %s, %s, %s::jsonb, %s, '["historical-claim"]'::jsonb, %s, %s, false))
INSERT INTO news_notifications(intent_id,event_id,kind,state,card,receipt,attempted_at_ms,settled_at_ms,
        created_at_ms,history_context,content_revision,claim_refs,plan_key,notification_id,origin,updated_at_ms)
SELECT s.intent_id::text,s.event_id::text,s.kind::text,s.state::text,s.card::jsonb||jsonb_build_object('body',
        s.body::text,'payload_sha256',s.payload_sha256::text),s.receipt::jsonb,s.attempted_at_ms::bigint,
        s.settled_at_ms::bigint,s.created_at_ms::bigint,s.history_context::jsonb,s.content_revision::text,
        s.claim_refs::jsonb,s.plan_key::boolean,s.intent_id::text,'legacy_delivery',s.created_at_ms::bigint FROM source
        s
ON CONFLICT(notification_id) DO UPDATE SET intent_id=EXCLUDED.intent_id,state=EXCLUDED.state,card=EXCLUDED.card,
        receipt=EXCLUDED.receipt,attempted_at_ms=EXCLUDED.attempted_at_ms,settled_at_ms=EXCLUDED.settled_at_ms,
        created_at_ms=EXCLUDED.created_at_ms,history_context=EXCLUDED.history_context,
        content_revision=EXCLUDED.content_revision,claim_refs=EXCLUDED.claim_refs,plan_key=EXCLUDED.plan_key,
        updated_at_ms=EXCLUDED.updated_at_ms""",
            (
                intent,
                at_ms,
                at_ms,
                at_ms,
                json.dumps({"comparison_title": TEXT}),
                digest(key),
                body,
                digest(body),
            ),
        )
        freeze_receipt(intent)
        return intent

    old = sent_update("A", "机构宣布关税", STAMP + 1)
    latest = sent_update("B", "生效日期为下月", STAMP + 2)
    future = sent_update("future", "还没发送的消息", clock() + 1)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    receipts = {row.intent_id: row.body for row in snapshot.reader.receipts}
    assert receipts[old] == "机构宣布关税"
    assert receipts[latest] == "生效日期为下月"
    assert future not in receipts


def reader_material(db: ThreadedDb, clock: Clock) -> dict[str, Any]:
    return asyncio.run(
        db.read(
            "test_reader_material",
            lambda repos: repos.news.notification_context.notification_snapshot_material(
                event_id=EVENT,
                channel="news",
                now_ms=clock(),
            ),
            repeatable_read=True,
        )
    )


def test_a_later_head_of_the_historical_event_does_not_change_its_receipt() -> None:
    """A receipt is read as the version it sent: its own `(event_id, content_revision)` and `claim_refs`.

    The historical Events later adopt heads with claims that were never pushed and that match this Event's
    subject and asset. Neither the projected claims, the route features, the selected bodies nor the reader
    revision may move.
    """

    pg, db, clock = store()
    adopted_head(pg.semantic, clock)
    related = {"subject": "Agency", "assets": [{"symbol": "X", "market_type": "equity", "role": "primary"}]}
    unrelated = {"subject": "Miner", "assets": [{"symbol": "CL", "market_type": "commodity", "role": "primary"}]}
    sent = head_claim("cl:hist-sent", statement="Agency sets the start date of the steel tariff", **related)
    quiet = head_claim("cl:quiet-sent", statement="Miner halts a Chilean copper pit", **unrelated)
    # Unpushed and structurally related: a sibling in the sent version, and a claim of a later head.
    sibling = head_claim("cl:quiet-sibling", statement="Agency widens the tariff", **related)
    for event_id, claim, extra, body in (
        ("ev-hist", sent, [], "机构公布钢铁关税生效日期"),
        ("ev-quiet", quiet, [sibling], "矿商暂停智利铜矿"),
    ):
        seed_event(event_id, title=body, fingerprint=event_id, at_ms=STAMP - 7_200_000)
        seed_update_version(event_id, content_revision=digest(f"{event_id}:1"), claims=[claim, *extra], head=True)
        seed_sent_receipt(
            event_id,
            intent_id=identity("intent", event_id),
            content_revision=digest(f"{event_id}:1"),
            claim_refs=[claim["ref"]],
            body=body,
            settled_at_ms=STAMP - 3_600_000,
        )

    def seen() -> tuple[str, list[tuple[Any, ...]]]:
        material = reader_material(db, clock)
        return material["revision"], [
            (
                row["intent_id"],
                row["body"],
                [claim["ref"] for claim in row["historical_claims"]],
                row["structure_rank"],
                row["lexical_rank"],
            )
            for row in material["receipt_rows"]
        ]

    before = seen()
    assert [row[:3] for row in before[1]] == [
        (identity("intent", "ev-hist"), "机构公布钢铁关税生效日期", ["cl:hist-sent"])
    ]
    later = {
        "ev-hist": [sent, head_claim("cl:hist-later", statement="Agency adds aluminium", **related)],
        "ev-quiet": [quiet, sibling, head_claim("cl:quiet-later", statement="Agency orders a steel tariff", **related)],
    }
    for event_id, claims in later.items():
        seed_update_version(event_id, content_revision=digest(f"{event_id}:2"), claims=claims, head=True)
    assert seen() == before


def test_hot_actor_cannot_exhaust_the_sql_route_before_concrete_matches() -> None:
    pg, db, clock = store()
    adopted_head(pg.semantic, clock)
    current = Claim.model_validate(
        head_claim(
            "cl:ct-current",
            statement="Aster lists CTUSDT perpetual",
            subject="Aster",
            action="listed",
            object="CTUSDT perpetual",
            assets=[{"symbol": "CTUSDT", "market_type": "crypto", "role": "primary"}],
        )
    )
    historical = [
        ("body", "Other spelling", "", [], "CTUSDT perpetual announcement", clock() - 6000),
        ("object", "Other spelling", "CTUSDT perpetual", [], "此前宣布具体合约", clock() - 5000),
        (
            "primary",
            "另一个写法",
            "",
            [{"symbol": "CTUSDT", "market_type": "crypto", "role": "primary"}],
            "同一标的较早推送",
            clock() - 4000,
        ),
        (
            "mentioned",
            "Aster",
            "unrelated product",
            [{"symbol": "CTUSDT", "market_type": "crypto", "role": "mentioned"}],
            "仅作为背景提及",
            clock() - 1,
        ),
        *(
            (
                f"actor-{index:02}",
                "Aster",
                f"other product {index}",
                [{"symbol": "SIUSDT", "market_type": "crypto", "role": "primary"}],
                f"不同动作{index}",
                clock() - 100 - index,
            )
            for index in range(40)
        ),
    ]
    candidates = []
    for key, subject, object_, assets, body, stamp in historical:
        event_id = f"hot-actor:{key}"
        historical_claim = head_claim(
            f"cl:{key}", statement=f"Earlier account {key}", subject=subject, object=object_, assets=assets
        )
        seed_event(event_id, title=body, fingerprint=event_id, at_ms=stamp - 100)
        seed_update_version(event_id, content_revision=digest(event_id), claims=[historical_claim])
        intent = identity("intent", event_id)
        seed_sent_receipt(
            event_id,
            intent_id=intent,
            content_revision=digest(event_id),
            claim_refs=[historical_claim["ref"]],
            body=body,
            settled_at_ms=stamp,
        )
        candidates.append(RecallCandidate(intent, digest(body), body, stamp, (Claim.model_validate(historical_claim),)))
    query = query_for_claim(current)
    routed = asyncio.run(
        db.read(
            "test_hot_actor_route",
            lambda repos: repos.news.notification_context._recall_receipt_rows((query,), now_ms=clock()),
        )
    )
    ranks = {row["intent_id"]: row["structure_rank"] for row in routed}
    assert ranks[identity("intent", "hot-actor:object")] == 1
    assert ranks[identity("intent", "hot-actor:primary")] == 2
    routes = {
        row["intent_id"]: RouteEvidence(
            structure_rank=row["structure_rank"],
            lexical_rank=row["lexical_rank"],
            lexical_terms=tuple(row["lexical_terms"] or ()),
        )
        for row in routed
    }
    sql_selected = select_for_claim(
        query, ReaderNovelty(novelty="unlinked"), tuple(candidates), as_of_ms=clock(), routes=routes
    )
    pure_selected = select_for_claim(query, ReaderNovelty(novelty="unlinked"), tuple(candidates), as_of_ms=clock())
    assert sql_selected.intent_ids == pure_selected.intent_ids
    assert sql_selected.intent_ids[:3] == (
        identity("intent", "hot-actor:object"),
        identity("intent", "hot-actor:body"),
        identity("intent", "hot-actor:primary"),
    )
    assert len(sql_selected.intent_ids) == 16


def test_sql_asset_route_reads_legacy_markets_like_claim_validation() -> None:
    pg, db, clock = store()
    adopted_head(pg.semantic, clock)
    currents = {
        "fx": Claim.model_validate(
            head_claim(
                "cl:fx-current",
                statement="Euro strengthens after the central bank decision",
                subject="European Central Bank",
                object="currency decision",
                assets=[{"symbol": "EURUSD", "market_type": "fx", "role": "primary"}],
            )
        ),
        "fund": Claim.model_validate(
            head_claim(
                "cl:fund-current",
                statement="Portfolio rebalances after a notice",
                subject="Portfolio",
                object="rebalancing",
                assets=[{"symbol": "ABC", "market_type": "unknown", "role": "primary"}],
            )
        ),
    }
    historical = [
        ("forex", "forex", "EURUSD", "欧洲央行", "外汇利率", "欧元此前走弱", clock() - 5000),
        ("actor", "equity", "OTHER", "Portfolio", "different action", "同一主体另一动作", clock() - 2000),
        ("fund", "fund", "ABC", "基金经理", "组合变更", "基金调仓消息", clock() - 1000),
    ]
    candidates = []
    for key, market, symbol, subject, object_, body, stamp in historical:
        event_id = f"legacy-market:{key}"
        historical_claim = head_claim(
            f"cl:legacy-{key}",
            statement=body,
            subject=subject,
            object=object_,
            assets=[{"symbol": symbol, "market_type": market, "role": "primary"}],
        )
        seed_event(event_id, title=body, fingerprint=event_id, at_ms=stamp - 100)
        seed_update_version(event_id, content_revision=digest(event_id), claims=[historical_claim])
        intent = identity("intent", event_id)
        seed_sent_receipt(
            event_id,
            intent_id=intent,
            content_revision=digest(event_id),
            claim_refs=[historical_claim["ref"]],
            body=body,
            settled_at_ms=stamp,
        )
        candidates.append(RecallCandidate(intent, digest(body), body, stamp, (Claim.model_validate(historical_claim),)))
    queries = {key: query_for_claim(claim) for key, claim in currents.items()}
    routed = asyncio.run(
        db.read(
            "test_legacy_market_route",
            lambda repos: repos.news.notification_context._recall_receipt_rows(tuple(queries.values()), now_ms=clock()),
        )
    )
    for key, query in queries.items():
        routes = {
            row["intent_id"]: RouteEvidence(
                structure_rank=row["structure_rank"],
                lexical_rank=row["lexical_rank"],
                lexical_terms=tuple(row["lexical_terms"] or ()),
            )
            for row in routed
            if row["current_ref"] == query.ref
        }
        assert all(route.lexical_rank is None for route in routes.values())
        sql_selected = select_for_claim(
            query, ReaderNovelty(novelty="unlinked"), tuple(candidates), as_of_ms=clock(), routes=routes
        )
        pure_selected = select_for_claim(query, ReaderNovelty(novelty="unlinked"), tuple(candidates), as_of_ms=clock())
        assert sql_selected == pure_selected
        if key == "fx":
            assert sql_selected.intent_ids == (identity("intent", "legacy-market:forex"),)
        else:
            # A legacy fund is unknown, so it shares the weak tier with the actor-only candidate.
            assert routes[identity("intent", "legacy-market:fund")].structure_rank == 1
            assert routes[identity("intent", "legacy-market:actor")].structure_rank == 2
            assert sql_selected.intent_ids == (
                identity("intent", "legacy-market:fund"),
                identity("intent", "legacy-market:actor"),
            )
    # The canonical read is a projection; adopted legacy JSON remains untouched.
    assert sql(
        "SELECT document->'claims'->0->'fields'->'assets'->0->>'market_type' AS market "
        f"FROM ({ANALYSES_SQL}) WHERE event_id='legacy-market:forex'"
    ) == [{"market": "forex"}]


def test_sql_generic_object_with_other_primary_assets_cannot_exhaust_the_route() -> None:
    pg, db, clock = store()
    adopted_head(pg.semantic, clock)
    current = Claim.model_validate(
        head_claim(
            "cl:acme-current",
            statement="ACME reports earnings",
            subject="ACME",
            object="earnings",
            assets=[{"symbol": "ACME", "market_type": "equity", "role": "primary"}],
        )
    )
    historical = [
        ("actual", "艾克米", "财报", "ACME", "艾克米此前发布财报", clock() - 5000),
        *(
            (f"noise-{index:02}", f"Other issuer {index}", "earnings", "OTHER", "其他公司财报", clock() - index - 1)
            for index in range(40)
        ),
    ]
    candidates = []
    for key, subject, object_, symbol, body, stamp in historical:
        event_id = f"generic-object:{key}"
        historical_claim = head_claim(
            f"cl:{event_id}",
            statement=body,
            subject=subject,
            object=object_,
            assets=[{"symbol": symbol, "market_type": "equity", "role": "primary"}],
        )
        seed_event(event_id, title=body, fingerprint=event_id, at_ms=stamp - 100)
        seed_update_version(event_id, content_revision=digest(event_id), claims=[historical_claim])
        intent = identity("intent", event_id)
        seed_sent_receipt(
            event_id,
            intent_id=intent,
            content_revision=digest(event_id),
            claim_refs=[historical_claim["ref"]],
            body=body,
            settled_at_ms=stamp,
        )
        candidates.append(RecallCandidate(intent, digest(body), body, stamp, (Claim.model_validate(historical_claim),)))
    query = query_for_claim(current)
    routed = asyncio.run(
        db.read(
            "test_generic_object_route",
            lambda repos: repos.news.notification_context._recall_receipt_rows((query,), now_ms=clock()),
        )
    )
    routes = {
        row["intent_id"]: RouteEvidence(
            structure_rank=row["structure_rank"],
            lexical_rank=row["lexical_rank"],
            lexical_terms=tuple(row["lexical_terms"] or ()),
        )
        for row in routed
    }
    assert routes[identity("intent", "generic-object:actual")].structure_rank == 1
    sql_selected = select_for_claim(
        query, ReaderNovelty(novelty="unlinked"), tuple(candidates), as_of_ms=clock(), routes=routes
    )
    pure_selected = select_for_claim(query, ReaderNovelty(novelty="unlinked"), tuple(candidates), as_of_ms=clock())
    assert sql_selected == pure_selected
    assert sql_selected.intent_ids[0] == identity("intent", "generic-object:actual")
    assert len(sql_selected.intent_ids) == 16


@pytest.mark.parametrize(
    ("previous_symbol", "relation", "missing_projection", "expected"),
    [
        ("SIUSDT", "equivalent", False, "unlinked"),
        ("SIUSDT", "adds_information", False, "unlinked"),
        ("SIUSDT", "real_world_change", False, "unlinked"),
        ("SIUSDT", "equivalent", True, "known"),
        ("SIUSDT", "adds_information", True, "increment"),
        ("SIUSDT", "corrects", False, "development"),
        ("CTUSDT", "real_world_change", False, "development"),
    ],
)
def test_reader_filters_old_listing_links_using_the_receipts_sent_claim_version(
    previous_symbol: str,
    relation: str,
    missing_projection: bool,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg, db, clock = store()

    def adopt_listing(event_id: str, symbol: str, phase: str) -> EventUpdate:
        text = f"Aster lists {symbol} perpetual ({phase})."
        seed_event(event_id, text=text, title=text, fingerprint=event_id)

        def build(source: FrozenInput) -> Extraction:
            original = draft(source.evidence[0], action="listed")
            original = original.model_copy(
                update={
                    "fields": original.fields.model_copy(
                        update={
                            "subject": "Aster",
                            "object": f"{symbol} perpetual",
                            "mode": "observation",
                            "phase": phase,
                            "content_kind": "state_change",
                            "assets": (Asset(symbol=symbol, market_type="crypto", role="primary"),),
                        }
                    )
                }
            )
            return Extraction(
                claims=(original,),
                supports=(SupportDraft(slot=original.slot, evidence_ref=source.evidence[0].ref, relation="supports"),),
                relations=tuple(
                    RelationDraft(slot=original.slot, previous_ref=prior.claim.ref, relation="unrelated")
                    for prior in source.prior
                ),
            )

        assert asyncio.run(run_agent(agent(pg.semantic, clock, StubAnalyzer(build)), event_id)) == "adopted"
        head = asyncio.run(pg.semantic.head(event_id))
        assert head is not None
        return head

    previous = adopt_listing("old-listing", previous_symbol, "announced")
    clock.now_ms += 1000
    old_intent = identity("intent", "old-listing")
    seed_sent_receipt(
        "old-listing",
        intent_id=old_intent,
        content_revision=digest("missing-version") if missing_projection else previous.content_revision,
        claim_refs=[previous.claims[0].ref],
        body="此前已推送上币公告",
        settled_at_ms=clock() - 500,
    )
    head = adopt_listing(EVENT, "CTUSDT", "effective")
    # A later unpushed CT claim in the historical Event is not the SI claim its receipt carried.
    seed_update_version(
        "old-listing",
        content_revision=digest("old-listing:later"),
        claims=[head.claims[0].model_dump(mode="json")],
        head=True,
    )
    current_ref, previous_ref = head.claims[0].ref, previous.claims[0].ref
    for update_ref, asserted_relation, asserted_at in (
        (identity("update", "older-assertion"), "corrects", head.adopted_at_ms - 1),
        (head.ref, relation, head.adopted_at_ms),
    ):
        seed_claim_link(update_ref, current_ref, previous_ref, asserted_relation, EVENT, asserted_at)
    clock.now_ms += 1000
    ledger_before = sql(f"SELECT * FROM ({CLAIM_LINKS_SQL}) ORDER BY asserted_at_ms")
    with monkeypatch.context() as legacy:
        legacy.setattr("tracefold.news.storage.notification_context.different_listing_assets", lambda *_: False)
        old_material = reader_material(db, clock)
    material = reader_material(db, clock)
    links = tuple(
        ClaimLink.model_validate({key: row[key] for key in ClaimLink.model_fields}) for row in material["links"]
    )
    receipts = tuple(
        LinkedReceipt(
            intent_id=row["intent_id"],
            state=row["state"],
            claim_refs=tuple(row["claim_refs"]),
            settled_at_ms=row["settled_at_ms"],
        )
        for row in material["link_receipts"]
    )
    assert reader_novelty(current_ref, links, receipts).novelty == expected
    if expected == "unlinked":
        assert material["links"] == []  # dropping the latest assertion does not revive the older correction
        assert material["revision"] != old_material["revision"]
    else:
        assert len(material["links"]) == 1 and material["links"][0]["relation"] == relation
        assert material["revision"] == old_material["revision"]
    assert material["revision"] == reader_material(db, clock)["revision"]
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    assert snapshot.reader.links == links
    assert snapshot.reader.revision == material["revision"]
    linked_row = next(row for row in material["link_receipts"] if row["intent_id"] == old_intent)
    assert [Claim.model_validate(claim).ref for claim in linked_row["historical_claims"]] == (
        [] if missing_projection else [previous_ref]
    )
    assert sql(f"SELECT * FROM ({CLAIM_LINKS_SQL}) ORDER BY asserted_at_ms") == ledger_before


def test_gold_claim_recalls_its_history_through_the_real_sql_routes() -> None:
    """#750 gold case over PostgreSQL: the frozen production receipts and links inside a production-shaped
    48 h window, projected, routed and selected by the same reader state the snapshot and both CAS sites build.

    "prices", "week", "gold" and "low" are as common in the window as in production, so none is rare enough to
    be lexical evidence: the copper and bitcoin receipts sharing them stay out and every direct antecedent
    comes back. The pure selector over the same window chooses the same receipts.
    """

    pg, db, clock = store()
    adopted_head(pg.semantic, clock)
    fixture = gold_fixture()
    carried = {row["intent_id"]: row["claim_refs"] for row in fixture["link_receipts"]}
    for row in fixture["candidates"]:
        event_id = f"gold-{row['intent_id'][7:19]}"
        revision = digest(row["intent_id"])
        seed_event(event_id, title=row["body"][:40], fingerprint=event_id, at_ms=row["settled_at_ms"] - 60_000)
        seed_update_version(event_id, content_revision=revision, claims=row["claims"])
        seed_sent_receipt(
            event_id,
            intent_id=row["intent_id"],
            content_revision=revision,
            claim_refs=carried.get(row["intent_id"], [claim["ref"] for claim in row["claims"]]),
            body=row["body"],
            settled_at_ms=row["settled_at_ms"],
        )
    filler = gold_window_filler(fixture)
    seed_window_receipts(filler)
    for number, link in enumerate(fixture["links"]):
        seed_claim_link(
            f"update:fixture-{number}",
            link["current_ref"],
            link["previous_ref"],
            link["relation"],
            "gold-links",
            link["asserted_at_ms"],
        )
    head = EventUpdate.model_validate(fixture["update"])
    state = asyncio.run(
        db.read(
            "test_gold_reader_state",
            lambda repos: repos.news.notification_context.reader_state(
                event_id=fixture["event_id"],
                head=head,
                now_ms=fixture["as_of_ms"],
            ),
            repeatable_read=True,
        )
    )
    claims = [Claim.model_validate(item) for item in fixture["claims"]]
    gold, data = (claim.ref for claim in claims)
    selected = {intent[7:13] for intent in state["receipt_intents_by_claim"][gold]}
    labels = fixture["labels"]
    assert len(state["receipt_intents_by_claim"][gold]) <= 16
    assert {key for key, label in labels.items() if label == 2} <= selected
    assert not selected & {key for key, label in labels.items() if label == 0}
    assert not selected & {"0b4d11", "177218", "641052", "d903d3"}
    assert state["receipt_intents_by_claim"][data] == ()
    window = tuple(
        RecallCandidate(
            intent_id=row["intent_id"],
            payload_sha256=row["payload_sha256"],
            body=row["body"],
            settled_at_ms=row["settled_at_ms"],
            claims=tuple(Claim.model_validate(item) for item in row["claims"]),
        )
        for row in fixture["candidates"]
    ) + tuple(RecallCandidate(intent, digest(body), body, at) for intent, body, at in filler)
    links = tuple(ClaimLink.model_validate(item) for item in fixture["links"])
    receipts = tuple(LinkedReceipt.model_validate(item) for item in fixture["link_receipts"])
    for claim in claims:
        pure = select_for_claim(
            query_for_claim(claim), reader_novelty(claim.ref, links, receipts), window, as_of_ms=fixture["as_of_ms"]
        )
        assert pure.intent_ids == state["receipt_intents_by_claim"][claim.ref]


def test_lexical_route_in_sql_counts_only_terms_rare_in_the_window_like_the_python_selector() -> None:
    """A House probe claim shares "market" and "trading" (市场, 交易) with unrelated receipts; only the receipt
    sharing rare terms is lexical evidence, in SQL and in the pure selector, term for term."""

    pg, db, clock = store()
    adopted_head(pg.semantic, clock)
    window = probe_window(clock())
    seed_window_receipts([(intent, text, at) for _, intent, text, at in window])
    keys = {intent: key for key, intent, _, _ in window}
    claims = {
        language: Claim.model_validate(head_claim(f"cl:probe-{language}", statement=statement, assets=[]))
        for language, statement in (("en", PROBE_STATEMENT), ("zh", PROBE_STATEMENT_ZH))
    }
    queries = {language: query_for_claim(claim) for language, claim in claims.items()}
    rows = asyncio.run(
        db.read(
            "test_probe_route",
            lambda repos: repos.news.notification_context._recall_receipt_rows(tuple(queries.values()), now_ms=clock()),
        )
    )
    candidates = tuple(RecallCandidate(intent, digest(text), text, at) for _, intent, text, at in window)
    routed = {
        language: {
            keys[str(row["intent_id"])]: tuple(sorted(row["lexical_terms"]))
            for row in rows
            if row["current_ref"] == query.ref and row["lexical_rank"] is not None
        }
        for language, query in queries.items()
    }
    pure = {
        language: {keys[intent]: terms for intent, (_, terms) in lexical_evidence(query, candidates).items()}
        for language, query in queries.items()
    }
    assert routed == pure
    assert set(routed["en"]) == set(routed["zh"]) == {PROBE_RECEIPT}
    english, chinese = set(routed["en"][PROBE_RECEIPT]), set(routed["zh"][PROBE_RECEIPT])
    assert {"hyperliquid", "oversight", "probe"} <= english and not {"market", "trading", "the"} & english
    assert {"监督", "调查"} <= chinese and not {"市场", "交易"} & chinese


def test_asset_route_in_sql_canonicalizes_symbols_like_the_python_selector() -> None:
    pg, db, clock = store()
    adopted_head(pg.semantic, clock)
    history = {
        "xag": ("XAG", "commodity"),
        "baiyin": ("白银", "commodity"),
        "spot-silver": ("现货白银", "commodity"),
        "silver-pair": ("XAG/USD", "commodity"),
        "silver-miner": ("Silver", "equity"),
        "gold": ("国际现货黄金", "commodity"),
        "cashtag": ("$OKLO", "crypto"),
        "exchange": ("HTX", "unknown"),
        "wti": ("WTI", "commodity"),
        "prefixed": (" xyz:CL ", "commodity"),
        "si-pair": ("SIUSDT", "crypto"),
        "si-quoted": ("SIUSDC", "crypto"),
        "si-equity": ("SI", "equity"),
        "fdusd-pair": ("ABCFDUSD", "crypto"),
        "busd-pair": ("BTCBUSD", "crypto"),
        "xaut": ("XAUT", "crypto"),
        "gold-token": ("GOLD", "crypto"),
        "skhx": ("SKHX", "equity"),
        "skhy": ("SKHY", "equity"),
        "solana": ("solana:AbCdEFGh123456789", "crypto"),
        "other-solana": ("solana:abcdefgh123456789", "crypto"),
    }
    for key, (symbol, market) in history.items():
        event_id = f"asset-{key}"
        claim = head_claim(
            f"cl:{key}",
            statement=f"Record {key}",
            subject=f"Holder {key}",
            assets=[{"symbol": symbol, "market_type": market, "role": "primary"}],
        )
        seed_event(event_id, title=key, fingerprint=event_id, at_ms=STAMP - 7_200_000)
        seed_update_version(event_id, content_revision=digest(event_id), claims=[claim])
        seed_sent_receipt(
            event_id,
            intent_id=identity("intent", event_id),
            content_revision=digest(event_id),
            claim_refs=[claim["ref"]],
            body=key,
            settled_at_ms=STAMP - 3_600_000,
        )
    currents = {
        "silver": ("Silver", "commodity"),
        "oklo": ("OKLO", "crypto"),
        "oil": ("oil", "commodity"),
        "si": ("$SI", "crypto"),
        "fdusd-base": ("ABC", "crypto"),
        "fdusd-partial": ("ABCFD", "crypto"),
        "busd-base": ("BTC", "crypto"),
        "busd-partial": ("BTCB", "crypto"),
        "token": ("XAUT", "crypto"),
        "issuer": ("SKHY", "equity"),
        "address": ("solana:AbCdEFGh123456789", "crypto"),
    }
    queries = tuple(
        query_for_claim(
            Claim.model_validate(
                head_claim(
                    f"cl:current-{key}",
                    statement="Quoted",
                    subject=f"Reader {key}",
                    assets=[{"symbol": symbol, "market_type": market, "role": "primary"}],
                )
            )
        )
        for key, (symbol, market) in currents.items()
    )
    rows = asyncio.run(
        db.read(
            "test_asset_route",
            lambda repos: repos.news.notification_context._recall_receipt_rows(queries, now_ms=clock()),
        )
    )
    routed = {
        key: {
            str(row["event_id"])[len("asset-") :]
            for row in rows
            if row["current_ref"] == f"cl:current-{key}" and row["structure_rank"] is not None
        }
        for key in currents
    }
    python = {
        key: {
            name
            for name, (symbol, market) in history.items()
            if market == currents[key][1]
            and asset_retrieval_symbols(symbol, market) & asset_retrieval_symbols(*currents[key])
        }
        for key in currents
    }
    assert routed == python
    assert routed == {
        "silver": {"xag", "baiyin", "spot-silver", "silver-pair"},
        "oklo": {"cashtag"},
        "oil": {"wti", "prefixed"},
        "si": {"si-pair", "si-quoted"},
        "fdusd-base": {"fdusd-pair"},
        "fdusd-partial": set(),
        "busd-base": {"busd-pair"},
        "busd-partial": set(),
        "token": {"xaut", "gold-token"},
        "issuer": {"skhx", "skhy"},
        "address": {"solana"},
    }
    # Every commodity-name pattern the Python side sends is a valid PostgreSQL regular expression.
    patterns = sorted({pattern for symbol in COMMODITY_SYMBOLS for pattern in commodity_name_patterns(symbol)})
    assert len(patterns) > 10
    assert sql("SELECT count(*) AS n FROM unnest(%s::text[]) p WHERE 'x' ~* p", (patterns,)) == [{"n": 0}]


@pytest.mark.parametrize("phase", ["planner", "card"])
def test_old_notification_failure_cannot_change_new_head_work(phase: str) -> None:
    pg, _db, clock = store()
    old = adopted_head(pg.semantic, clock)

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

        planner = NotificationPlanner(PushAll(), PgJudgmentCache(pg.semantic.db))
        service = Notifications(
            pg.notifications,
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
            adopted, new = await adopt_next(pg.semantic, old, source, extraction_for(source))
            assert adopted
            before = sql(f"SELECT * FROM ({NOTIFY_JOBS_SQL})")[0]
            assert before["content_revision"] == new.content_revision
        finally:
            release.set()
        turn = await task
        assert (turn.status, turn.error_code) == (
            "plan_failed" if phase == "planner" else "card_failed",
            f"news_{'notification_plan' if phase == 'planner' else 'card'}:RuntimeError",
        )
        assert sql(f"SELECT * FROM ({NOTIFY_JOBS_SQL})")[0] == before
        if phase == "card":
            intent = sql(f"SELECT content_revision, error_code, attempts, state FROM ({UPDATE_PENDING_SQL})")[0]
            assert intent == {
                "content_revision": old.content_revision,
                "error_code": "news_card:RuntimeError",
                "attempts": 1,
                "state": "pending",
            }

    asyncio.run(exercise())


def test_repeated_planner_failure_settlement_for_one_snapshot_spends_one_attempt() -> None:
    pg, _db, _clock = store()
    head = adopted_head(pg.semantic, _clock)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None and snapshot.work_updated_at_ms is not None
    for _ in range(2):
        asyncio.run(
            pg.notifications.defer_notification(
                EVENT, "news", head.content_revision, snapshot.work_updated_at_ms, error_code="news_editor_down"
            )
        )
    assert sql(f"SELECT attempts, last_error_code FROM ({NOTIFY_JOBS_SQL})")[0] == {
        "attempts": 1,
        "last_error_code": "news_editor_down",
    }


def test_the_third_planner_failure_fails_the_work_and_only_an_exact_retry_reopens_it() -> None:
    """#742 N5: exhausted work is `failed` with its error code, not pending forever with none."""

    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    for expected in range(1, 3):
        asyncio.run(
            pg.notifications.defer_notification(
                EVENT, "news", head.content_revision, error_code=f"plan_error_{expected}"
            )
        )
        row = sql(f"SELECT state, attempts, next_attempt_at_ms FROM ({NOTIFY_JOBS_SQL})")[0]
        assert (row["state"], row["attempts"]) == ("pending", expected) and row["next_attempt_at_ms"] > clock()
    asyncio.run(pg.notifications.defer_notification(EVENT, "news", head.content_revision, error_code="plan_error_3"))
    failed = sql(f"SELECT * FROM ({NOTIFY_JOBS_SQL})")[0]
    assert (failed["state"], failed["attempts"], failed["last_error_code"]) == ("failed", 3, "plan_error_3")
    asyncio.run(pg.notifications.defer_notification(EVENT, "news", head.content_revision, error_code="plan_error_4"))
    assert sql(f"SELECT * FROM ({NOTIFY_JOBS_SQL})")[0] == failed
    assert asyncio.run(pg.notifications.pending_notification_events("news", 10)) == ()
    # Explicit recovery affects this planning version only.
    assert not asyncio.run(
        db.tx(
            "retry",
            lambda r: r.news.notification_work.retry_failed_revision(
                event_id=EVENT, revision="obsolete", now_ms=clock()
            ),
        )
    )
    assert asyncio.run(
        db.tx(
            "retry",
            lambda r: r.news.notification_work.retry_failed_revision(
                event_id=EVENT, revision=head.content_revision, now_ms=clock()
            ),
        )
    )
    assert sql(f"SELECT state, attempts FROM ({NOTIFY_JOBS_SQL})")[0] == {"state": "pending", "attempts": 0}
    assert asyncio.run(pg.notifications.pending_notification_events("news", 10)) == (EVENT,)


def test_expired_intent_lease_cannot_cross_the_send_boundary() -> None:
    pg, _db, clock = store()
    adopted_head(pg.semantic, clock)
    prepared = asyncio.run(notifications(pg.notifications, clock, Sender()).service.prepare(EVENT, "news"))
    assert prepared.lease is not None and prepared.card is not None
    before = sql("SELECT * FROM news_notifications")[0]
    clock.now_ms = before["lease_until_ms"]
    assert asyncio.run(pg.notifications.atomic_begin_send(prepared.lease, prepared.card)) == "lease_lost"
    assert sql("SELECT * FROM news_notifications") == [before]


def test_preflight_failure_cannot_reopen_a_sending_notification() -> None:
    pg, db, clock = store()
    adopted_head(pg.semantic, clock)
    prepared = asyncio.run(notifications(pg.notifications, clock, Sender()).service.prepare(EVENT, "news"))
    assert prepared.lease is not None and prepared.card is not None
    lease = prepared.lease
    assert asyncio.run(pg.notifications.atomic_begin_send(lease, prepared.card)) == "begun"
    before = sql("SELECT * FROM news_notifications")[0]
    assert not asyncio.run(
        db.tx(
            "preflight_failure",
            lambda r: r.news.notification_work.record_unsent_intent_failure(
                intent_id=lease.intent_id,
                lease_token=lease.lease_token,
                error_code="late_preflight",
                retryable=True,
                retry_after_ms=None,
                now_ms=clock(),
            ),
        )
    )
    assert sql("SELECT * FROM news_notifications") == [before]


def test_waiting_on_a_send_in_flight_spends_no_attempt_and_records_no_new_decision() -> None:
    """#742 N2/N5 (review D6). A claim held by this Event's own `sending` row waits: no attempt is spent,
    and a turn that decides exactly what the last one decided is the same decision row, not a new one."""

    pg, _db, clock = store()
    head = adopted_head(pg.semantic, clock)
    prepared = asyncio.run(notifications(pg.notifications, clock, Sender()).service.prepare(EVENT, "news"))
    assert prepared.status == "ready" and prepared.lease is not None and prepared.card is not None
    assert asyncio.run(pg.notifications.atomic_begin_send(prepared.lease, prepared.card)) == "begun"
    sql("UPDATE news_jobs SET state='pending', next_attempt_at_ms=%s", (clock(),))
    for _ in range(3):
        assert asyncio.run(notifications(pg.notifications, clock, Sender()).process(EVENT, "news")) == "unresolved"
        clock.now_ms += 30_000
    work = sql(f"SELECT state, attempts, next_attempt_at_ms FROM ({NOTIFY_JOBS_SQL})")[0]
    assert (work["state"], work["attempts"]) == ("pending", 0) and work["next_attempt_at_ms"] > clock() - 30_000
    unresolved = sql(f"SELECT count(*) AS n FROM ({NOTIFICATION_DECISIONS_SQL}) WHERE plan->>'action'='unresolved'")
    assert unresolved[0]["n"] == 1
    assert head.claims[0].ref in {
        row["claim_ref"]
        for row in sql(
            "SELECT jsonb_array_elements(plan->'claim_decisions')->>'claim_ref' AS claim_ref"
            f" FROM ({NOTIFICATION_DECISIONS_SQL}) WHERE plan->>'action'='unresolved'"
        )
    }


def test_an_orphaned_sending_row_is_held_ambiguous_and_its_plan_completes() -> None:
    """#742 N8: a `sending` row whose owner is gone is reconciled -- never one this process still owns --
    and the work it held is completed rather than left waiting until the claim goes stale."""

    pg, db, clock = store()
    adopted_head(pg.semantic, clock)
    prepared = asyncio.run(notifications(pg.notifications, clock, Sender()).service.prepare(EVENT, "news"))
    assert prepared.lease is not None and prepared.card is not None
    assert asyncio.run(pg.notifications.atomic_begin_send(prepared.lease, prepared.card)) == "begun"
    intent = prepared.lease.intent_id

    def reconcile(r: Any, *, now_ms: int, owned: tuple[str, ...] = ()) -> int:
        return r.news.notification_delivery.terminalize_interrupted_deliveries(now_ms=now_ms, exclude_intent_ids=owned)

    assert asyncio.run(db.tx("reconcile", lambda r: reconcile(r, now_ms=clock() + 30_000))) == 0
    owned_late = clock() + 120_000
    assert asyncio.run(db.tx("reconcile", lambda r: reconcile(r, now_ms=owned_late, owned=(intent,)))) == 0
    assert asyncio.run(db.tx("reconcile", lambda r: reconcile(r, now_ms=owned_late))) == 1
    assert sql(f"SELECT state, error_code FROM ({UPDATE_RECEIPTS_SQL})") == [
        {"state": "ambiguous", "error_code": "ambiguous_after_crash"}
    ]
    assert sql(f"SELECT count(*) AS n FROM ({UPDATE_PENDING_SQL})")[0]["n"] == 0
    assert sql(f"SELECT state FROM ({NOTIFY_JOBS_SQL})")[0]["state"] == "done"


def test_final_semantic_crash_is_settled_only_after_lease_expiry_and_retries_exact_version() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    facts = sql(f"SELECT document FROM ({ANALYSES_SQL})")
    outbox = trade_rows()
    checkpoints = sql("SELECT * FROM news_judgment_cache WHERE cache_key LIKE 'semantic_checkpoint:%'")
    set_semantic_job(
        None, wanted_revision=2, attempts=3, lease_token="last", lease_until_ms=clock() + 1000, last_outcome=None
    )

    def settle(r):
        return r.news.semantic_work.terminalize_exhausted_semantic_work(now_ms=clock(), limit=10)

    assert asyncio.run(db.tx("janitor", settle)) == 0
    clock.now_ms += 1001
    assert asyncio.run(db.tx("janitor", settle)) == 1
    failed = sql(f"SELECT last_outcome,last_error_code,lease_token FROM ({SEMANTIC_JOBS_SQL})")[0]
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
                    lambda r, revision=revision: r.news.semantic_work.retry_failed_revision(
                        event_id=EVENT, revision=revision, now_ms=clock()
                    ),
                )
            )
            is expected
        )
    work = sql(f"SELECT attempts,done_revision,last_outcome,last_error_code FROM ({SEMANTIC_JOBS_SQL})")[0]
    assert work["attempts"] == 0 and work["done_revision"] == 1 and work["last_outcome"] is None
    assert work["last_error_code"] == failed["last_error_code"]
    assert sql(f"SELECT document FROM ({ANALYSES_SQL})") == facts
    assert (
        trade_rows() == outbox
        and sql("SELECT * FROM news_judgment_cache WHERE cache_key LIKE 'semantic_checkpoint:%'") == checkpoints
    )
    assert asyncio.run(pg.semantic.head(EVENT)) == head


def test_notification_retry_revives_the_failed_unsent_intent_and_never_reopens_a_ledger() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    plan = notify_plan(head, snapshot.reader.revision)
    lease = asyncio.run(pg.notifications.atomic_record_plan(plan)).lease
    assert lease is not None
    sql("UPDATE news_notifications SET attempts=2 WHERE kind='update' AND state='pending'")
    asyncio.run(pg.notifications.record_unsent_failure(lease, error_code="bad_copy", retryable=True))
    assert sql(f"SELECT state FROM ({UPDATE_PENDING_SQL})")[0]["state"] == "dead"
    assert sql(f"SELECT state, last_error_code FROM ({NOTIFY_JOBS_SQL})")[0] == {
        "state": "failed",
        "last_error_code": "bad_copy",
    }
    facts, outbox = sql(f"SELECT document FROM ({ANALYSES_SQL})"), trade_rows()

    def retry(r):
        return r.news.notification_work.retry_failed_revision(
            event_id=EVENT, revision=head.content_revision, now_ms=clock()
        )

    assert asyncio.run(db.tx("retry", retry))
    assert sql(f"SELECT intent_id,attempts,state FROM ({UPDATE_PENDING_SQL})") == [
        {"intent_id": lease.intent_id, "attempts": 0, "state": "pending"}
    ]
    assert not asyncio.run(db.tx("retry", retry))
    sender = Sender("sent")
    assert asyncio.run(notifications(pg.notifications, clock, sender).process(EVENT, "news")) == "sent"
    assert sender.cards[0].intent_id == lease.intent_id
    assert sql(f"SELECT state, last_error_code FROM ({NOTIFY_JOBS_SQL})")[0] == {
        "state": "done",
        "last_error_code": None,
    }
    sent = sql(f"SELECT * FROM ({UPDATE_RECEIPTS_SQL})")
    assert not asyncio.run(db.tx("retry", retry))
    assert sql(f"SELECT * FROM ({UPDATE_RECEIPTS_SQL})") == sent
    assert sql(f"SELECT document FROM ({ANALYSES_SQL})") == facts and trade_rows() == outbox


@pytest.mark.parametrize("state", ["sending", "ambiguous", "terminal"])
def test_notification_retry_never_reopens_an_intent_with_a_send_ledger(state: str) -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    lease = asyncio.run(pg.notifications.atomic_record_plan(notify_plan(head, snapshot.reader.revision))).lease
    assert lease is not None
    from tracefold.news.notifications.card import freeze_card

    card = freeze_card(lease.plan, head, asyncio.run(Composer().compose(head.claims, sources={})))
    asyncio.run(save_card(pg.notifications, lease, card))
    assert asyncio.run(pg.notifications.atomic_begin_send(lease, card)) == "begun"
    sql(
        "UPDATE news_notifications SET state=%s,settled_at_ms=CASE WHEN %s='sending' THEN NULL ELSE %s END",
        (state, state, clock()),
    )
    sql("UPDATE news_jobs SET state='failed', last_error_code='operator_test'")
    before = sql(f"SELECT * FROM ({UPDATE_RECEIPTS_SQL})")
    assert asyncio.run(
        db.tx(
            "retry",
            lambda r: r.news.notification_work.retry_failed_revision(
                event_id=EVENT, revision=head.content_revision, now_ms=clock()
            ),
        )
    )
    assert sql(f"SELECT * FROM ({UPDATE_RECEIPTS_SQL})") == before
    assert sql("SELECT state FROM news_notifications")[0]["state"] == state


def test_targeted_reanalysis_reuses_work_and_preserves_adopted_head() -> None:
    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    listing = asyncio.run(
        db.read(
            "reanalysis_preview",
            lambda repos: repos.news.semantic_work.reanalysis_scope_list(
                event_id=EVENT, now_ms=clock(), input=repos.news.semantic_input
            ),
        )
    )
    assert listing["wanted_revision"] == listing["done_revision"] == 1
    assert listing["head_revision"] == head.content_revision
    assert len(listing["scopes"]) == 1 and listing["scopes"][0]["completed"]
    read_ref = listing["scopes"][0]["read_ref"]
    next_revision = asyncio.run(
        db.tx(
            "reanalysis_request",
            lambda repos: repos.news.semantic_work.request_reanalysis(
                listing=listing,
                event_id=EVENT,
                expected_wanted_revision=1,
                expected_head_revision=head.content_revision,
                read_ref=read_ref,
                reason="confirmed omitted condition in previous task view",
                now_ms=clock(),
                input=repos.news.semantic_input,
            ),
        )
    )
    assert next_revision == 2
    reopened = asyncio.run(pg.semantic.input_for(EVENT))
    assert reopened.revision == 2 and reopened.evidence
    assert reopened.reanalysis_reason == "confirmed omitted condition in previous task view"
    assert reopened.reanalysis_head_ref == head.ref
    result = asyncio.run(
        run_agent(agent(pg.semantic, clock, StubAnalyzer(lambda _source: Extraction(claims=()))), EVENT)
    )
    assert result == "unchanged"
    assert asyncio.run(pg.semantic.head(EVENT)) == head
    observations = sql(
        f"SELECT read_refs,reanalysis_reason,reanalysis_head_ref FROM ({SEMANTIC_RESULTS_SQL}) ORDER BY input_revision"
    )
    assert observations[-1]["read_refs"] == [read_ref]
    assert observations[-1]["reanalysis_head_ref"] == head.ref
    assert sql(f"SELECT reanalysis_read_ref,done_revision FROM ({SEMANTIC_JOBS_SQL})")[0] == {
        "reanalysis_read_ref": None,
        "done_revision": 2,
    }
    with pytest.raises(EventUpdateConflict, match="news_reanalysis_wanted_revision_changed_or_incomplete"):
        asyncio.run(
            db.tx(
                "reanalysis_stale",
                lambda repos: repos.news.semantic_work.request_reanalysis(
                    listing=listing,
                    event_id=EVENT,
                    expected_wanted_revision=1,
                    expected_head_revision=head.content_revision,
                    read_ref=read_ref,
                    reason="stale",
                    now_ms=clock(),
                    input=repos.news.semantic_input,
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
    source = asyncio.run(pg.semantic.input_for(EVENT))
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
    # An exact member shares the fact, not necessarily every byte; a byte-identical copy is no new read (#770).
    reposted = body.replace("（以上内容", "（转载自交易所公告，以上内容")
    leader_fact = extract_fact_units(item_id=leader_item, raw_text=body, fallback_title="Daily digest")[0]
    exact_fact = extract_fact_units(item_id=exact_item, raw_text=reposted, fallback_title="Daily digest")[0]
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
               %s, evidence_text_sha256
          FROM news_items WHERE item_id=%s
        """,
        (exact_item, exact_item, exact_item, reposted, leader_item),
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
    source = asyncio.run(pg.semantic.input_for(EVENT))
    assert len(source.evidence) == len(source.extraction_scopes) == 2
    assert {scope.fact_id for scope in source.extraction_scopes} == {leader_fact.fact_id, exact_fact.fact_id}
    for view in reading_views(source):
        assert view.mode == "scoped"
        shown = " ".join(span.text for span in view.spans)
        assert "Agency suspends withdrawals" in shown
        assert "不构成投资建议" in shown
        assert "Beta releases earnings" not in shown


def seed_claim_link(
    update_ref: str, current_ref: str, previous_ref: str, relation: str, event_id: str, at_ms: int
) -> None:
    if not sql("SELECT 1 FROM news_events WHERE event_id=%s", (event_id,)):
        seed_event(event_id, fingerprint=event_id, at_ms=at_ms)
    document = {
        "event_id": event_id,
        "content_revision": digest(update_ref),
        "input_revision": 1,
        "claims": [],
        "changes": [{"current_ref": current_ref, "previous_ref": previous_ref, "relation": relation}],
    }

    def persist(repos):
        conn = repos.conn
        result_id = "link-fixture:" + digest(update_ref)
        conn.execute(
            """INSERT INTO news_analyses(analysis_id,event_id,origin,input_revision,completed_at_ms,work_id,
                 input_sha256,program_identity,understanding,content_revision,update_ref,adopted_at_ms,document)
               VALUES (%s,%s,'semantic',1,%s,%s,%s,'link-fixture','{}',%s,%s,%s,%s::jsonb)""",
            (
                result_id,
                event_id,
                at_ms,
                result_id,
                digest(document),
                document["content_revision"],
                update_ref,
                at_ms,
                json.dumps(document),
            ),
        )

    ThreadedDb()._run("seed-claim-link", persist)


def test_slow_reader_permission_releases_event_for_admission(monkeypatch):
    from threading import Event
    from time import monotonic

    from tracefold.news.storage.notification_context import NotificationContextStorage

    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    seed_event("ev-incoming", fingerprint="incoming")
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    plan = notify_plan(head, snapshot.reader.revision)
    started = Event()
    original = NotificationContextStorage.current_reader_revision

    def slow(self, event_id, *, now_ms):
        if not started.is_set():
            assert self.conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
            started.set()
            self.conn.execute("SELECT pg_sleep(1)")
        return original(self, event_id, now_ms=now_ms)

    monkeypatch.setattr(NotificationContextStorage, "current_reader_revision", slow)

    async def race():
        planning = asyncio.create_task(pg.notifications.atomic_record_plan(plan))
        assert await asyncio.to_thread(started.wait, 3)

        def admission(repos):
            assert repos.news.add_member(
                event_id=EVENT,
                item_id="it-ev-incoming",
                joined_at_ms=clock.now_ms,
                match_kind="near",
                jaccard_estimate=0.9,
                provider_score=90,
                fact_id="incoming",
                fact_text="New evidence",
                now_ms=clock.now_ms,
            )
            repos.news.semantic_work.request_semantic_revision(event_id=EVENT, lineage_id="same", now_ms=clock.now_ms)

        begin = monotonic()
        await asyncio.wait_for(db.tx("admission", admission), 0.25)
        assert monotonic() - begin < 0.25
        return await planning

    assert asyncio.run(race()).lease is not None


def test_sweep_cannot_settle_a_candidate_reowned_after_selection(monkeypatch):
    import tracefold.news.storage.notification_delivery as delivery

    pg, db, clock = store()
    adopted_head(pg.semantic, clock)
    prepared = asyncio.run(notifications(pg.notifications, clock, Sender()).service.prepare(EVENT, "news"))
    assert asyncio.run(pg.notifications.atomic_begin_send(prepared.lease, prepared.card)) == "begun"
    clock.now_ms += 120_000
    original = delivery.lock_event
    reowned = False

    def replace_owner(conn, event_id):
        nonlocal reowned
        if not reowned:
            reowned = True
            sql(
                "UPDATE news_notifications SET lease_token='new-owner',lease_until_ms=%s,attempted_at_ms=%s "
                "WHERE intent_id=%s",
                (clock.now_ms + 120_000, clock.now_ms, prepared.lease.intent_id),
            )
        original(conn, event_id)

    monkeypatch.setattr(delivery, "lock_event", replace_owner)
    assert (
        asyncio.run(
            db.tx(
                "sweep", lambda r: r.news.notification_delivery.terminalize_interrupted_deliveries(now_ms=clock.now_ms)
            )
        )
        == 0
    )
    row = sql("SELECT state,lease_token FROM news_notifications WHERE intent_id=%s", (prepared.lease.intent_id,))[0]
    assert row == {"state": "sending", "lease_token": "new-owner"}


def test_foreign_writes_do_not_invalidate_news_permission(monkeypatch):
    from threading import Event

    from tracefold.news.storage.notification_context import NotificationContextStorage

    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    plan = notify_plan(head, snapshot.reader.revision)
    started = Event()
    original = NotificationContextStorage.current_reader_revision

    def slow(self, event_id, *, now_ms):
        started.set()
        self.conn.execute("SELECT pg_sleep(.3)")
        return original(self, event_id, now_ms=now_ms)

    monkeypatch.setattr(NotificationContextStorage, "current_reader_revision", slow)

    async def race():
        planned = asyncio.create_task(pg.notifications.atomic_record_plan(plan))
        assert await asyncio.to_thread(started.wait, 3)
        await db.tx(
            "foreign_writer",
            lambda r: (r.trading.ensure_account("noise"), r.news.record_published_frame(now_ms=clock.now_ms)),
        )
        return await planned

    assert asyncio.run(race()).lease is not None
    assert db.names.count("news_update_plan_permission") == 1


def test_receipt_committing_inside_permission_read_invalidates_its_generation(monkeypatch):
    from threading import Event

    from tracefold.news.storage.notification_context import NotificationContextStorage

    pg, db, clock = store()
    head = adopted_head(pg.semantic, clock)
    snapshot = asyncio.run(pg.notifications.notification_snapshot(EVENT, "news"))
    plan = notify_plan(head, snapshot.reader.revision)
    seed_event("ev-other", fingerprint="fp-other", title="Agency orders steel tariff", text=TEXT)
    seed_sent_claim_projection(
        "ev-other", content_revision=hashlib.sha256(b"ev-other").hexdigest(), claim_ref="cl:fixture", related=True
    )
    started = Event()
    original = NotificationContextStorage.current_reader_revision

    def slow(self, event_id, *, now_ms):
        if not started.is_set():
            started.set()
            self.conn.execute("SELECT pg_sleep(.3)")
        return original(self, event_id, now_ms=now_ms)

    monkeypatch.setattr(NotificationContextStorage, "current_reader_revision", slow)

    async def race():
        planned = asyncio.create_task(pg.notifications.atomic_record_plan(plan))
        assert await asyncio.to_thread(started.wait, 3)
        await db.tx(
            "late_receipt",
            lambda r: seed_delivery(
                r.conn,
                event_id="ev-other",
                at_ms=clock.now_ms - 5_000,
                history_context={"comparison_title": "Agency orders steel tariff"},
                card={"header": {"title": {"content": "关税"}}},
            ),
        )
        return await planned

    assert asyncio.run(race()).status == "reader_changed"
    assert (
        sql("SELECT count(*) AS n FROM news_notifications WHERE event_id=%s AND state='pending'", (EVENT,))[0]["n"] == 0
    )
