"""A new EventUpdate with no legacy verdict can be reviewed whether selected or held."""

import asyncio

import pytest
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_attention import FeedOnly, NotifyAll
from tests.support.news_update_pg import EVENT, TaskBackend, adopted_head, store
from tracefold.news.review.desk import DecisionFeedbackSubmission, DeskQuery, Principal, ReviewDesk, TaskRef
from tracefold.news.storage.event_update_store import PgJudgmentCache
from tracefold.news.updates.judgment import Budget, NewsJudgments
from tracefold.news.updates.notification import NotificationPlanner
from tracefold.platform.postgres.client import transaction

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


@pytest.mark.parametrize("selected", [True, False])
def test_new_decision_is_reviewable_without_old_verdict(selected: bool) -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    planner = NotificationPlanner(
        NewsJudgments(generated=TaskBackend({"coverage": "full"}), cache=PgJudgmentCache(pg.db)),
        NotifyAll() if selected else FeedOnly(),
    )
    plan = asyncio.run(planner.plan(head, snapshot.reader, Budget.start(5), now_ms=clock.now_ms))
    committed = asyncio.run(pg.atomic_record_plan(plan))
    assert committed.recorded
    assert (committed.lease is not None) == selected

    conn = connect_postgres_test(read_only=False)
    try:
        desk = ReviewDesk(conn, now_ms=clock.now_ms + 1)
        principal = Principal(subject="reviewer")
        queue = desk.open(DeskQuery(status="pending", event=EVENT), principal=principal)
        assert queue["rubric_version"] == "news_attention_review_v1"
        assert len(queue["tasks"]) == 1
        task = queue["tasks"][0]
        assert task["reason"] == ("editor_notify" if selected else "editor_feed_only")
        ref = TaskRef(task_id=task["task_id"], task_version=task["task_version"])
        evidence = desk.evidence(ref, principal=principal)
        assert evidence["evidence"]["candidate"]["claims"][0]["ref"] == head.claims[0].ref
        assert evidence["evidence"]["candidate"]["claims"][0]["citations"][0]["quote"]
        source_only = desk.evidence(ref, principal=principal, source_only=True)
        assert "agent" not in source_only and source_only["task"]["task_id"] == task["task_id"]
        with transaction(conn):
            receipt = desk.submit(
                ref,
                DecisionFeedbackSubmission(should_push="should_push" if selected else "should_hold"),
                principal=principal,
                idempotency_key=f"review-{selected}",
            )
        assert receipt["receipt"]["task_id"] == task["task_id"]
        accepted = desk.open(DeskQuery(status="accepted", event=EVENT), principal=principal)
        assert accepted["tasks"][0]["review_status"] == "accepted"
        coverage = desk.open(DeskQuery(view="coverage"), principal=principal)["counts"]
        assert coverage["claims"] == 1
        assert coverage["selected"] == int(selected)
        assert coverage["feed_only"] == int(not selected)
        assert coverage["reviewed"] == 1
        with pytest.raises(CheckViolation, match="news_notification_record_append_only"), transaction(conn):
            conn.execute(
                "UPDATE news_notification_decisions SET plan=plan WHERE decision_ref=%s",
                (plan.record_ref,),
            )
        with pytest.raises(CheckViolation, match="news_notification_record_append_only"), transaction(conn):
            conn.execute(
                "UPDATE news_notification_feedback SET note=note WHERE review_id=%s",
                (receipt["receipt"]["review_id"],),
            )
    finally:
        conn.close()


def test_identical_decision_input_reuses_persisted_assessment() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None

    class CountingAssessor(NotifyAll):
        calls = 0

        async def assess(self, claims, *, sources, watch_symbols):
            self.calls += 1
            return await super().assess(claims, sources=sources, watch_symbols=watch_symbols)

    assessor = CountingAssessor()
    planner = NotificationPlanner(
        NewsJudgments(generated=TaskBackend({"coverage": "full"}), cache=PgJudgmentCache(pg.db)), assessor
    )
    kwargs = {
        "now_ms": clock.now_ms,
        "reuse": lambda fingerprint: pg.lookup_notification_decision(EVENT, "news", fingerprint),
    }
    first = asyncio.run(planner.plan(head, snapshot.reader, Budget.start(5), **kwargs))
    committed = asyncio.run(pg.atomic_record_plan(first))
    assert committed.recorded and assessor.calls == 1
    second = asyncio.run(planner.plan(head, snapshot.reader, Budget.start(5), **kwargs))
    assert assessor.calls == 1
    assert second.record_ref == committed.effective_plan.record_ref


def test_decision_queue_filters_before_limit_and_uses_one_read_for_a_sparse_page() -> None:
    pg, _db, clock = store()
    head = adopted_head(pg, clock)
    snapshot = asyncio.run(pg.notification_snapshot(EVENT, "news"))
    assert snapshot is not None
    planner = NotificationPlanner(
        NewsJudgments(generated=TaskBackend({"coverage": "full"}), cache=PgJudgmentCache(pg.db)), NotifyAll()
    )
    plan = asyncio.run(planner.plan(head, snapshot.reader, Budget.start(5), now_ms=clock.now_ms))
    assert asyncio.run(pg.atomic_record_plan(plan)).recorded
    conn = connect_postgres_test(read_only=False)
    try:
        original = conn.execute(
            "SELECT * FROM news_notification_decisions WHERE decision_ref=%s", (plan.record_ref,)
        ).fetchone()
        claim_ref = head.claims[0].ref
        with transaction(conn):
            for index in range(1, 41):
                digest = f"pagination-{index}"
                copied = dict(original["plan"])
                copied["assessment_input_digest"] = digest
                if index == 40:
                    copied["claim_decisions"][0]["reason"] = "editor_feed_only"
                decision_ref = f"notification_decision:pagination-{index}"
                conn.execute(
                    """INSERT INTO news_notification_decisions
                       (decision_ref,event_id,update_ref,channel,input_digest,input_snapshot,plan,origin,created_at_ms)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'editorial_v1',%s)""",
                    (
                        decision_ref,
                        EVENT,
                        original["update_ref"],
                        "news",
                        digest,
                        Jsonb(original["input_snapshot"]),
                        Jsonb(copied),
                        clock.now_ms - index,
                    ),
                )
            for index, decision_ref in [
                (0, plan.record_ref),
                *[(number, f"notification_decision:pagination-{number}") for number in range(1, 40)],
            ]:
                conn.execute(
                    """INSERT INTO news_notification_feedback
                       (review_id,decision_ref,claim_ref,task_version,reviewer,idempotency_key,
                        request_sha,should_push,note,created_at_ms)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'should_push','',%s)""",
                    (
                        f"pagination-review-{index}",
                        decision_ref,
                        claim_ref,
                        "a" * 64,
                        "reviewer",
                        f"pagination-{index}",
                        "b" * 64,
                        clock.now_ms + index,
                    ),
                )

        class CountingConnection:
            calls = 0

            def execute(self, sql, params=()):
                self.calls += 1
                return conn.execute(sql, params)

        counted = CountingConnection()
        desk = ReviewDesk(counted, now_ms=clock.now_ms + 1)
        pending = desk.open(DeskQuery(status="pending", event=EVENT, limit=1), principal=Principal(subject="reader"))
        assert counted.calls == 1
        assert [task["decision_ref"] for task in pending["tasks"]] == ["notification_decision:pagination-40"]
        assert pending["next_cursor"] is None
        sparse = desk.open(
            DeskQuery(status="pending", stratum="feed_only", limit=1), principal=Principal(subject="reader")
        )
        assert len(sparse["tasks"]) == 1 and sparse["counts"] == {"feed_only": 1}
        accepted = desk.open(DeskQuery(status="accepted", limit=10), principal=Principal(subject="reader"))
        assert len(accepted["tasks"]) == 10 and accepted["next_cursor"]
        second = desk.open(
            DeskQuery(status="accepted", limit=10, cursor=accepted["next_cursor"]),
            principal=Principal(subject="reader"),
        )
        assert len(second["tasks"]) == 10
        assert set(task["task_id"] for task in accepted["tasks"]).isdisjoint(
            task["task_id"] for task in second["tasks"]
        )
        with pytest.raises(ValueError, match="news_review_cursor_invalid"):
            desk.open(DeskQuery(cursor="bm90LWEtY3Vyc29y"), principal=Principal(subject="reader"))
    finally:
        conn.close()
