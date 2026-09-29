"""EventUpdate read side over PostgreSQL: Event detail and feed.

The EventUpdate rows are written exactly as the store writes them (an observation, an insert-only revision,
the CAS head, semantic work, the notification plan and the intent ledger), from documents the real core
produced. The feed's SQL tab partition is checked against the Python outcome of every row it serves, so the
two statements of one precedence cannot drift apart.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_event_updates import (
    TARIFF_TOPIC,
    first_update,
    material,
    notify_plan,
    persist_plan,
    persist_semantic_work,
    persist_update,
    queue_intent,
    raised_update,
    settle_intent,
    silent_plan,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.artifact_identity import canonical_json
from tracefold.news.updates.contracts import Extraction, FrozenInput, PriorClaim, RelationDraft, SupportDraft
from tracefold.news.updates.semantics import assemble_update

pytestmark = pytest.mark.integration

NOW = 1_900_000_000_000
SENT_HEADLINE = "钢铁进口关税上调至 50%"


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


def _event(news: Any, event_id: str, *, opened_at_ms: int) -> None:
    item_id = f"item:{event_id}"
    news.upsert_item(
        item_id=item_id,
        source_id="opennews",
        source_item_key=item_id,
        title=f"Wire title for {event_id}",
        raw_first_line="",
        description="",
        canonical_url=f"https://example.test/{event_id}",
        reporting_origin="OpenNews",
        published_at_ms=opened_at_ms,
        observed_at_ms=opened_at_ms,
        provider_metadata_json=canonical_json({"strategies": [{"id": "1018", "name": "news"}]}),
        strategy_ids_json=canonical_json(["1018"]),
        ingest_mode="live",
        trace_id="trace",
        now_ms=opened_at_ms,
        source_artifact_id=f"artifact:{event_id}",
        market_kind=None,
        market_source_strategy_id=None,
        market_parse_status=None,
        market_parse_error=None,
    )
    news.insert_event(
        event_id=event_id,
        leader_item_id=item_id,
        dedupe_family="general",
        event_kind="news",
        comparison_fingerprint=f"fingerprint:{event_id}",
        comparison_title=f"Wire title for {event_id}",
        leader_title=f"Wire title for {event_id}",
        focus_fact_id=f"fact:{event_id}",
        focus_fact_text=f"Wire title for {event_id}",
        focus_fact_context="",
        focus_fact_method="whole_item",
        focus_span_start=0,
        focus_span_end=10,
        opened_at_ms=opened_at_ms,
        expires_at_ms=opened_at_ms + 3_600_000,
        admission="candidate",
        queue_priority="normal",
        provider_score=90,
        engine_type="news",
        asset_class="none",
        grounded_assets=(),
        grounded_assets_json="[]",
        watchlist_hits=(),
        watchlist_hits_json="[]",
        macro_lexicon=False,
        storyline_key=f"story:{event_id}",
        context_line="",
        ingest_mode="live",
        trace_id="trace",
        band_keys=(f"band:{event_id}",),
        now_ms=opened_at_ms,
    )
    news.append_evidence_snapshot(event_id=event_id, now_ms=opened_at_ms)
    news.mark_event_published(event_id=event_id, now_ms=opened_at_ms)


def _seed(conn: Any) -> dict[str, Any]:
    """Four Events: sent update, silent update, semantic work pending, and one source-only Event."""

    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        for index, event_id in enumerate(("agent-sent", "agent-silent", "agent-pending", "source-only")):
            _event(news, event_id, opened_at_ms=NOW - (index + 1) * 60_000)

        head = first_update("agent-sent", adopted_at_ms=NOW - 50_000)
        persist_update(conn, head)
        raised = raised_update(head, adopted_at_ms=NOW - 40_000)
        persist_update(conn, raised)
        persist_semantic_work(conn, "agent-sent", wanted=2, done=2, now_ms=NOW - 40_000, last_outcome="adopted")
        plan = notify_plan(raised, key=True)
        persist_plan(conn, raised, plan, state="done", now_ms=NOW - 35_000)
        queue_intent(conn, raised, plan, now_ms=NOW - 35_000)
        settle_intent(
            conn,
            raised,
            plan,
            state="sent",
            headline_zh=SENT_HEADLINE,
            body=f"【重点】{SENT_HEADLINE}",
            now_ms=NOW - 30_000,
        )

        silent = first_update("agent-silent", adopted_at_ms=NOW - 100_000)
        persist_update(conn, silent)
        persist_semantic_work(conn, "agent-silent", wanted=1, done=1, now_ms=NOW - 100_000, last_outcome="adopted")
        persist_plan(conn, silent, silent_plan(silent), state="done", now_ms=NOW - 95_000)

        persist_semantic_work(conn, "agent-pending", wanted=1, done=None, now_ms=NOW - 170_000)

    return {"head": head, "raised": raised, "plan": plan, "silent": silent}


def _feed(news: Any, **over: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "source_authority": None,
        "subject_code": None,
        "admission": None,
        "event_kind": None,
        "search": None,
        "limit": 20,
        "cursor": None,
        "now_ms": NOW,
    }
    params.update(over)
    return news.list_feed(**params)


def test_a_news_agent_event_detail_reads_its_update_processing_and_timeline(conn) -> None:
    seeded = _seed(conn)
    head, raised, plan = seeded["head"], seeded["raised"], seeded["plan"]
    news = repositories_for_connection(conn).news

    detail = news.event_detail("agent-sent")

    assert detail is not None
    assert "event_update" in detail
    update = detail["event_update"]
    assert update["content_revision"] == raised.content_revision
    assert update["previous_content_revision"] == head.content_revision
    assert (update["headline"], update["headline_source"]) == (SENT_HEADLINE, "sent_card")
    assert update["topics"] == [{"code": TARIFF_TOPIC, "label_zh": "关税"}]
    # The earlier claim is found in the Event's own adopted history, never guessed.
    (change,) = update["changes"]
    assert change["kind"] == "parameter_change"
    assert change["previous_statement"] == head.claims[0].statement
    assert change["previous_event_id"] == "agent-sent"
    assert update["disputed_claim_refs"] == [head.claims[0].ref]
    assert {
        (source["source"]["publisher_id"], row["relation"])
        for source in update["sources"]
        for row in source["relations"]
    } >= {
        ("wire", "supports"),
        ("rival", "refutes"),
    }
    processing = detail["processing"]
    assert processing["semantic"]["state"] == "done" and processing["semantic"]["wanted_revision"] == 2
    assert [row["adopted_content_revision"] for row in processing["observations"]] == [
        raised.content_revision,
        head.content_revision,
    ]
    assert processing["notification"]["plan"]["action"] == "notify"
    assert processing["notification"]["plan"]["key"] is True
    (intent,) = processing["intents"]
    assert intent["intent_id"] == plan.intent_id
    assert (intent["state"], intent["body"]) == ("sent", f"【重点】{SENT_HEADLINE}")
    assert intent["receipt"] == {"channel": "telegram", "message_id": 42}
    assert [row["intent_id"] for row in detail["deliveries"]] == [plan.intent_id]
    assert detail["outcome"]["kind"] == "delivered" and detail["outcome"]["text_zh"] == "已推送（重点）"
    assert detail["reader_receipt"]["state"] == "received"
    stages = [step["stage"] for step in detail["timeline"]]
    assert stages[:2] == ["received", "gate"]
    assert {"evidence", "semantic", "notify", "delivery"} <= set(stages)
    assert {"triage", "decide"}.isdisjoint(stages)
    adoptions = [step for step in detail["timeline"] if step["title_zh"] == "采用更新"]
    assert [step["facts"]["change_kinds"] for step in adoptions] == [["new_fact"], ["parameter_change"]]


def test_a_source_only_event_reads_source_without_update(conn) -> None:
    _seed(conn)
    news = repositories_for_connection(conn).news
    detail = news.event_detail("source-only")
    assert detail is not None
    assert detail["event_update"] is None and detail["processing"] is None
    assert detail["feedback"] == {"feedback_n": 0, "latest": None}
    assert "event_update" in detail
    assert [step["stage"] for step in detail["timeline"]] == ["received", "gate"]
    assert detail["outcome"]["kind"] == "no_update"


def test_a_mixed_feed_page_partitions_into_the_same_tabs_its_rows_report(conn) -> None:
    seeded = _seed(conn)
    news = repositories_for_connection(conn).news

    page = _feed(news)

    rows = {row["event_id"]: row for row in page["events"]}
    assert list(rows) == ["agent-sent", "agent-silent", "agent-pending", "source-only"]
    assert {event_id: row["outcome"]["kind"] for event_id, row in rows.items()} == {
        "agent-sent": "delivered",
        "agent-silent": "not_notified",
        "agent-pending": "queued_semantic",
        "source-only": "no_update",
    }
    assert rows["agent-sent"]["update"]["headline"] == SENT_HEADLINE
    assert rows["agent-sent"]["update"]["headline_source"] == "sent_card"
    assert rows["agent-sent"]["update"]["claim_n"] == 2
    assert rows["agent-silent"]["update"]["headline"] == seeded["silent"].claims[0].statement
    assert rows["agent-silent"]["update"]["headline_source"] == "claim"
    assert rows["agent-silent"]["outcome"]["reason_zh"] == "仅进入信息流"
    assert rows["agent-pending"]["update"] is None
    assert rows["source-only"]["update"] is None
    assert rows["source-only"]["update"] is None

    assert page["counts"] == {"total": 4, "pushed": 1, "held": 2, "pending": 1}
    for group in ("pushed", "held", "pending"):
        served = {row["event_id"] for row in _feed(news, outcome=group)["events"]}
        assert served == {event_id for event_id, row in rows.items() if row["outcome"]["group"] == group}, group


def test_feed_counts_keep_sent_precedence_with_multiple_intents(conn) -> None:
    seeded = _seed(conn)
    repos = repositories_for_connection(conn)
    with repos.transaction():
        old = seeded["head"]
        plan = notify_plan(old, key=True)
        # A later failed attempt must neither hide the earlier sent card nor count the Event twice.
        settle_intent(
            conn,
            old,
            plan,
            state="terminal",
            headline_zh="Earlier revision",
            body="Earlier revision",
            error_code="channel_rejected",
            now_ms=NOW - 1_000,
        )

    page = _feed(repos.news)
    assert len(page["events"]) == 4
    assert page["counts"] == {"total": 4, "pushed": 1, "held": 2, "pending": 1}
    for group in ("pushed", "held", "pending"):
        rows = _feed(repos.news, outcome=group)["events"]
        assert len(rows) == page["counts"][group]
        assert all(row["outcome"]["group"] == group for row in rows)
    assert _feed(repos.news, outcome="pushed")["events"][0]["event_id"] == "agent-sent"


def test_an_owed_intent_and_a_new_revision_move_the_row_back_to_pending(conn) -> None:
    seeded = _seed(conn)
    repos = repositories_for_connection(conn)
    with repos.transaction():
        silent = seeded["silent"]
        persist_plan(conn, silent, notify_plan(silent), state="done", now_ms=NOW - 10_000)
        queue_intent(conn, silent, notify_plan(silent), now_ms=NOW - 10_000)
        persist_semantic_work(conn, "source-only", wanted=2, done=1, now_ms=NOW - 5_000)

    rows = {row["event_id"]: row for row in _feed(repos.news)["events"]}

    assert rows["agent-silent"]["outcome"]["kind"] == "pending_delivery"
    # A historical Event re-opened by new evidence enters the current semantic work path.
    assert rows["source-only"]["outcome"]["kind"] == "queued_semantic"
    counts = _feed(repos.news)["counts"]
    assert counts == {"total": 4, "pushed": 1, "held": 0, "pending": 3}


def test_topic_and_cited_source_filters_read_the_adopted_head(conn) -> None:
    _seed(conn)
    news = repositories_for_connection(conn).news

    topics = _feed(news, subject_code=(TARIFF_TOPIC,))
    issuers = _feed(news, source_authority=("issuer_first_party",))
    secondary = _feed(news, source_authority=("reputable_secondary",))

    assert [row["event_id"] for row in topics["events"]] == ["agent-sent", "agent-silent"]
    assert topics["filters"]["subject_code"] == TARIFF_TOPIC
    assert [row["event_id"] for row in issuers["events"]] == ["agent-sent", "agent-silent"]
    # An Event without a current adopted head has no current cited-source authority.
    assert secondary["events"] == []
    assert issuers["counts"]["total"] == 2


def test_a_change_against_a_related_events_head_names_that_event(conn) -> None:
    seeded = _seed(conn)
    related_head = seeded["silent"]
    repos = repositories_for_connection(conn)
    evidence = material("Ministry confirms the steel tariff will apply to allied producers.", publisher="ministry")
    source = FrozenInput(
        event_id="agent-pending",
        revision=1,
        lineage_id="agent-pending:line",
        evidence=(evidence,),
        prior=tuple(
            PriorClaim(event_id=related_head.event_id, content_revision=related_head.content_revision, claim=claim)
            for claim in related_head.claims
        ),
    )
    draft = related_head.claims[0]
    extraction = Extraction.model_validate(
        {
            "claims": [
                {
                    "slot": "a",
                    "statement": evidence.text,
                    "fields": draft.fields.model_dump(mode="json") | {"object": "allied steel imports"},
                    "citations": [{"evidence_ref": evidence.ref, "quote": evidence.text}],
                }
            ],
            "relations": [
                RelationDraft(
                    slot="a", previous_ref=draft.ref, relation="adds_information", change_kind="scope_change"
                ).model_dump(mode="json")
            ],
            "supports": [SupportDraft(slot="a", evidence_ref=evidence.ref, relation="supports").model_dump()],
        }
    )
    related = assemble_update(source, extraction, None, adopted_at_ms=NOW - 1_000)
    assert related is not None
    with repos.transaction():
        persist_update(conn, related)
        persist_semantic_work(conn, "agent-pending", wanted=1, done=1, now_ms=NOW - 1_000)

    detail = repos.news.event_detail("agent-pending")

    assert detail is not None
    (change,) = detail["event_update"]["changes"]
    assert change["kind"] == "scope_change"
    assert change["previous_event_id"] == "agent-silent"
    assert change["previous_statement"] == draft.statement
    assert detail["outcome"]["kind"] == "not_notified"


def test_an_unsent_later_revision_is_titled_by_the_claim_it_changed(conn) -> None:
    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        _event(news, "agent-raised", opened_at_ms=NOW - 60_000)
        head = first_update("agent-raised", adopted_at_ms=NOW - 50_000)
        persist_update(conn, head)
        raised = raised_update(head, adopted_at_ms=NOW - 40_000)
        persist_update(conn, raised)
        persist_semantic_work(conn, "agent-raised", wanted=2, done=2, now_ms=NOW - 40_000, last_outcome="adopted")
        persist_plan(conn, raised, silent_plan(raised), state="done", now_ms=NOW - 35_000)

    (row,) = _feed(news)["events"]
    (change,) = [change for change in raised.changes if change.kind == "parameter_change"]
    changed = next(claim for claim in raised.claims if claim.ref == change.current_ref)
    # The SQL twin of `headline_claim_statement`: the 50% claim, not the superseded 25% lead claim.
    assert changed.statement != head.claims[0].statement
    assert (row["update"]["headline"], row["update"]["headline_source"]) == (changed.statement, "claim")


def test_item_related_events_pages_all_memberships_without_duplicate_events(conn) -> None:
    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        for index in range(13):
            _event(news, f"related-{index:02d}", opened_at_ms=NOW + index)
        for index in range(1, 13):
            news.add_member(
                event_id=f"related-{index:02d}",
                item_id="item:related-00",
                joined_at_ms=NOW + index,
                match_kind="near",
                jaccard_estimate=0.8,
                provider_score=None,
                fact_id=f"shared-{index}",
                fact_text=f"shared scope {index}",
                now_ms=NOW + index,
            )
        # One Item can contribute multiple facts to one Event; pagination counts the Event once.
        news.add_member(
            event_id="related-12",
            item_id="item:related-00",
            joined_at_ms=NOW + 20,
            match_kind="near",
            jaccard_estimate=0.8,
            provider_score=None,
            fact_id="shared-12-extra",
            fact_text="extra scope",
            now_ms=NOW + 20,
        )
    first = news.item_related_events(item_id="item:related-00", after_event_id=None, limit=5)
    second = news.item_related_events(item_id="item:related-00", after_event_id=first["next_cursor"], limit=5)
    third = news.item_related_events(item_id="item:related-00", after_event_id=second["next_cursor"], limit=5)
    pages = [first, second, third]
    assert [page["total_events"] for page in pages] == [13, 13, 13]
    assert [len(page["events"]) for page in pages] == [5, 5, 3]
    assert third["next_cursor"] is None
    events = {row["event_id"]: row for page in pages for row in page["events"]}
    assert len(events) == 13
    assert events["related-00"]["match_kinds"] == ["leader"]
    assert events["related-12"]["member_scopes"] == ["extra scope", "shared scope 12"]
    assert events["related-12"]["leader_item_id"] == "item:related-12"
    assert all(row["sent_count"] == 0 for row in events.values())


def test_failed_notification_agrees_in_feed_detail_and_tab_counts(conn) -> None:
    seeded = _seed(conn)
    news = repositories_for_connection(conn).news
    with conn.transaction():
        conn.execute(
            "UPDATE news_notification_work SET state='failed', attempts=3,"
            " last_error_code='news_notification_plan:KeyError' WHERE event_id='agent-silent'"
        )
    # The work's terminal state is its own column, whatever its last decision said.
    detail = news.event_detail("agent-silent")
    assert detail["processing"]["notification"]["state"] == "failed"
    assert detail["processing"]["notification"]["last_error_code"] == "news_notification_plan:KeyError"
    assert detail["outcome"]["kind"] == "notification_failed"
    rows = {row["event_id"]: row for row in _feed(news)["events"]}
    assert rows["agent-silent"]["outcome"]["kind"] == "notification_failed"
    assert "news_notification_plan:KeyError" in rows["agent-silent"]["outcome"]["reason_zh"]
    assert rows["agent-silent"]["outcome"]["group"] == "held"
    for group in ("held", "pending", "pushed"):
        served = {row["event_id"] for row in _feed(news, outcome=group)["events"]}
        assert served == {event for event, row in rows.items() if row["outcome"]["group"] == group}
    assert detail["event_update"]["content_revision"] == seeded["silent"].content_revision
