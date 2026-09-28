"""PostgreSQL feedback and explicit retired task boundary."""

from __future__ import annotations

import uuid

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.review.desk import (
    DecisionFeedbackSubmission,
    DeskQuery,
    ExternalMissSubmission,
    Principal,
    ReviewDesk,
    TaskRef,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]
PRINCIPAL = Principal(subject="operator")


@pytest.fixture()
def conn():
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


def test_external_miss_creates_snapshot_and_short_feedback(conn) -> None:
    db_now = int(
        conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms").fetchone()[
            "now_ms"
        ]
    )
    desk = ReviewDesk(conn, now_ms=db_now + 1)
    submission = ExternalMissSubmission(
        source_url="https://example.test/missed",
        title="A material source item the receiver never ingested",
        body="Primary source body",
        occurred_at_ms=db_now - 10_000,
        feedback=DecisionFeedbackSubmission(should_push="should_push", note="Reader should see this"),
    )
    key = str(uuid.uuid4())
    with repositories_for_connection(conn).transaction():
        receipt = desk.submit(None, submission, principal=PRINCIPAL, idempotency_key=key)
    with repositories_for_connection(conn).transaction():
        again = desk.submit(None, submission, principal=PRINCIPAL, idempotency_key=key)
    assert again["idempotent"] is True and again["receipt"]["review_id"] == receipt["receipt"]["review_id"]
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM news_external_miss_snapshots) AS snapshots, "
        "(SELECT count(*) FROM news_notification_external_feedback) AS feedback, "
        "(SELECT count(*) FROM news_reviews) AS legacy_reviews"
    ).fetchone()
    assert counts == {"snapshots": 1, "feedback": 1, "legacy_reviews": 0}
    assert (
        conn.execute("SELECT provenance FROM news_external_miss_snapshots").fetchone()["provenance"]
        == "operator_reported"
    )
    with pytest.raises(ValueError, match="news_review_idempotency_conflict"):
        desk.submit(None, submission.model_copy(update={"title": "Changed"}), principal=PRINCIPAL, idempotency_key=key)


def test_external_miss_rejects_future_source_time(conn) -> None:
    db_now = int(
        conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms").fetchone()[
            "now_ms"
        ]
    )
    submission = ExternalMissSubmission(
        source_url="https://example.test/future",
        title="Future source",
        occurred_at_ms=db_now + 60_000,
        feedback=DecisionFeedbackSubmission(should_push="uncertain"),
    )
    with (
        repositories_for_connection(conn).transaction(),
        pytest.raises(ValueError, match="news_review_external_miss_future"),
    ):
        ReviewDesk(conn).submit(None, submission, principal=PRINCIPAL, idempotency_key=str(uuid.uuid4()))


def test_retired_event_task_has_one_rejection_boundary(conn) -> None:
    desk = ReviewDesk(conn)
    ref = TaskRef(task_id="evt.old.1.pin", task_version="1" * 64)
    with pytest.raises(ValueError, match="news_review_legacy_task_retired"):
        desk.open(DeskQuery(task=ref.task_id), principal=PRINCIPAL)
    with pytest.raises(ValueError, match="news_review_task_kind_unsupported"):
        desk.evidence(ref, principal=PRINCIPAL)
    with pytest.raises(ValueError, match="news_review_legacy_task_retired"):
        desk.submit(
            ref, DecisionFeedbackSubmission(should_push="uncertain"), principal=PRINCIPAL, idempotency_key="old"
        )
