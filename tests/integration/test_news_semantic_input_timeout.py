"""A claim whose input read times out spends the attempt, backs off and stays visible (#771).

Before #771 the cancelled recall statement rolled back the whole claim: no attempt, no backoff, no error, and the
wake repair claimed the same Event again at once, holding the News lane indefinitely.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_update_admission import work
from tests.support.news_update_pg import EVENT, STAMP, Clock, ThreadedDb, seed_event, sql
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.storage.evidence import EvidenceStorage
from tracefold.news.storage.semantic_rows import SEMANTIC_JOBS_SQL
from tracefold.news.storage.semantic_store import PgSemanticStore
from tracefold.news.storage.semantic_work import SEMANTIC_ATTEMPTS_MAX, SEMANTIC_RETRY_MS
from tracefold.news.updates.projection import reading_views

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


class StatementTimeoutDb(ThreadedDb):
    """The News port with the Workers' per-transaction statement timeout, shortened for the test."""

    def _run(self, name: str, fn: Any, repeatable_read: bool = False) -> Any:
        self.names.append(name)
        conn = connect_postgres_test(read_only=False)
        try:
            repos = repositories_for_connection(conn)
            with repos.transaction():
                conn.execute("SET LOCAL statement_timeout = '200ms'")
                return fn(repos)
        finally:
            conn.close()


def _slow_recall(self: EvidenceStorage, query: Any) -> list[dict[str, Any]]:
    self.conn.execute("SELECT pg_sleep(5)")
    return []


def test_input_timeout_spends_the_attempt_backs_off_and_fails_visibly_when_exhausted(monkeypatch):
    seed_event()
    clock = Clock(STAMP + 10)
    store = PgSemanticStore(StatementTimeoutDb(), clock=clock)
    monkeypatch.setattr(EvidenceStorage, "evidence_candidates", _slow_recall)

    for attempt in range(1, SEMANTIC_ATTEMPTS_MAX + 1):
        assert asyncio.run(store.claim_semantic_work(EVENT, lease_ms=180_000)) is None
        row = work(EVENT)
        assert row["attempts"] == attempt
        assert row["lease_token"] is None and row["leased_until_ms"] is None
        assert row["last_error_code"] == "news_semantic_input_timeout"
        assert row["attempt_read_refs"] == [] and row["failed_read_refs"] == []
        if attempt < SEMANTIC_ATTEMPTS_MAX:
            assert row["last_outcome"] == "news_semantic_input_timeout"
            assert row["next_attempt_at_ms"] == clock.now_ms + SEMANTIC_RETRY_MS[attempt - 1]
            # Backed off: neither a wake nor the repair scan claims it before it is due.
            clock.now_ms += 1
            assert asyncio.run(store.claim_semantic_work(EVENT, lease_ms=180_000)) is None
            assert work(EVENT)["attempts"] == attempt
            assert asyncio.run(store.pending_semantic_events(10)) == ()
            clock.now_ms = row["next_attempt_at_ms"]
            assert asyncio.run(store.pending_semantic_events(10)) == (EVENT,)
    # Spent: the existing visible failure, counted by code on the status page and retryable by an operator.
    row = work(EVENT)
    assert row["last_outcome"] == "failed" and row["attempts"] == SEMANTIC_ATTEMPTS_MAX
    clock.now_ms += 3_600_000
    assert asyncio.run(store.claim_semantic_work(EVENT, lease_ms=180_000)) is None
    assert asyncio.run(store.pending_semantic_events(10)) == ()
    status = sql(f"SELECT last_error_code, count(*) AS n FROM ({SEMANTIC_JOBS_SQL}) GROUP BY 1")
    assert status == [{"last_error_code": "news_semantic_input_timeout", "n": 1}]

    monkeypatch.undo()
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            assert repositories_for_connection(conn).news.semantic_work.retry_failed_revision(
                event_id=EVENT, revision=str(row["wanted_revision"]), now_ms=clock.now_ms
            )
    finally:
        conn.close()
    lease = asyncio.run(store.claim_semantic_work(EVENT, lease_ms=180_000))
    assert lease is not None and lease.attempts == 1
    assert work(EVENT)["attempt_read_refs"] == [view.read_ref for view in reading_views(lease.source)]
