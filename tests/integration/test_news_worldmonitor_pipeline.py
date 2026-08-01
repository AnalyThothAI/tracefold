from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.news import (
    NewsAcquisition,
    NewsFeedEntry,
    NewsFeedFetch,
    NewsInterface,
    NewsRepository,
    NewsSourceDefinition,
    OpenNewsExpectedError,
    opennews_source,
    parse_opennews_message,
)
from tracefold.news.projection import NewsProjectionSnapshot, compute_news_story_projection

NOW_MS = 1_785_560_400_000


class _SingleConnectionDB:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def worker_session(self, *_args: Any, **_kwargs: Any):
        return repository_session_for_connection(self.conn)

    async def run_business(
        self,
        _operation_name: str,
        function: Any,
        /,
        *args: Any,
        operation_timeout_seconds: float,
        **kwargs: Any,
    ) -> Any:
        del operation_timeout_seconds
        return function(*args, **kwargs)


class _SlowLivePublishDB(_SingleConnectionDB):
    def __init__(self, conn: Any) -> None:
        super().__init__(conn)
        self.release_live_publish = asyncio.Event()

    async def run_business(
        self,
        operation_name: str,
        function: Any,
        /,
        *args: Any,
        operation_timeout_seconds: float,
        **kwargs: Any,
    ) -> Any:
        if operation_name == "opennews_live_publish":
            await self.release_live_publish.wait()
        return await super().run_business(
            operation_name,
            function,
            *args,
            operation_timeout_seconds=operation_timeout_seconds,
            **kwargs,
        )


class _InlineCapability:
    async def run(self, _operation_name: str, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("timeout_seconds", None)
        kwargs.pop("service_timeout_seconds", None)
        kwargs.pop("operation_timeout_seconds", None)
        kwargs.pop("allow_shutdown", None)
        on_submitted = kwargs.pop("on_submitted", None)
        if on_submitted is not None:
            on_submitted()
        return function(*args, **kwargs)


class _NoopReader:
    def fetch_wire(self, **_kwargs: Any) -> NewsFeedFetch:
        return NewsFeedFetch(status_code=200, fetch_path="direct", not_modified=True)

    def close(self) -> None:
        return None


class _FakeOpenNewsRest:
    def __init__(self, events: tuple[Any, ...]) -> None:
        self.events = events
        self.calls = 0

    def fetch_latest(self) -> tuple[Any, ...]:
        self.calls += 1
        return self.events

    def close(self) -> None:
        return None


class _FakeOpenNewsWebSocket:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.connected = 0
        self.delivered = False
        self.closed = 0
        self._block = asyncio.Event()

    async def connect(self) -> None:
        self.connected += 1

    async def receive(self) -> dict[str, Any]:
        if not self.delivered:
            self.delivered = True
            return self.message
        await self._block.wait()
        return self.message

    async def close(self) -> None:
        self.closed += 1


class _ReconnectOpenNewsWebSocket:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.connected = 0
        self.closed = 0
        self.delivered = False
        self._block = asyncio.Event()

    async def connect(self) -> None:
        self.connected += 1

    async def receive(self) -> dict[str, Any]:
        if self.connected == 1:
            raise OpenNewsExpectedError("opennews_ws_disconnected")
        if not self.delivered:
            self.delivered = True
            return self.message
        await self._block.wait()
        return self.message

    async def close(self) -> None:
        self.closed += 1


class _FloodOpenNewsWebSocket:
    def __init__(self, *, count: int) -> None:
        self.count = count
        self.connected = 0
        self.closed = 0
        self.delivered = 0
        self.release = asyncio.Event()
        self._block = asyncio.Event()

    async def connect(self) -> None:
        self.connected += 1

    async def receive(self) -> dict[str, Any]:
        await self.release.wait()
        if self.delivered < self.count:
            sequence = self.delivered
            self.delivered += 1
            return {
                "method": "news.update",
                "params": {
                    "id": f"flood-{sequence}",
                    "text": f"Bounded buffer report {sequence}",
                    "newsType": "Reuters",
                    "engineType": "news",
                    "ts": NOW_MS + sequence,
                },
            }
        await self._block.wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed += 1


def _rss_source() -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id="rss-reuters",
        name="Reuters",
        feed_url="https://example.com/rss",
        tier=1,
        memberships=("finance",),
    )


def _claim(repository: NewsRepository, source_id: str, now_ms: int) -> str:
    token = str(uuid4())
    row = repository.conn.execute(
        """
        UPDATE news_sources
           SET claim_token=%s::uuid, claim_lease_expires_at_ms=%s,
               last_fetch_started_at_ms=%s, updated_at_ms=%s
         WHERE source_id=%s RETURNING source_id
        """,
        (token, now_ms + 45_000, now_ms, now_ms, source_id),
    ).fetchone()
    assert row is not None
    return token


async def _wait_for_item(conn: Any) -> None:
    for _ in range(100):
        if conn.execute("SELECT count(*) AS n FROM news_items").fetchone()["n"]:
            conn.commit()
            return
        conn.commit()
        await asyncio.sleep(0.01)
    raise AssertionError("OpenNews item was not published")


async def _wait_until(predicate: Any, *, message: str) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(message)


def test_fake_wss_rest_and_rss_converge_through_real_postgres_and_public_reads() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        message = {
            "method": "news.update",
            "params": {
                "id": "wire-1",
                "text": "Fed holds rates steady after policy meeting",
                "newsType": "Reuters",
                "engineType": "news",
                "link": None,
                "ts": NOW_MS - 1_000,
                "aiRating": {"score": 99, "signal": "long"},
            },
        }
        event = parse_opennews_message(message)
        assert event is not None
        rest = _FakeOpenNewsRest((event,))
        websocket = _FakeOpenNewsWebSocket(message)
        acquisition = NewsAcquisition(
            db=_SingleConnectionDB(conn),
            finite_operations=_InlineCapability(),
            cpu=_InlineCapability(),
            sources=(_rss_source(), opennews_source()),
            feed_reader=_NoopReader(),
            feed_parser=lambda value: value,
            opennews_rest_client=rest,
            opennews_ws_client=websocket,
        )

        async def exercise() -> None:
            await acquisition.reconcile()
            stop = asyncio.Event()
            task = asyncio.create_task(acquisition.run_opennews(stop_event=stop))
            await _wait_for_item(conn)
            await asyncio.sleep(0.05)
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)
            await acquisition.close()

        asyncio.run(exercise())
        repository = NewsRepository(conn)
        rss = _rss_source()
        with conn.transaction():
            repository.record_fetch_success(
                source=rss,
                entries=(
                    NewsFeedEntry(
                        guid="rss-1",
                        link="https://reuters.example/article",
                        title="Fed holds rates steady after policy meeting",
                        published_at_ms=NOW_MS - 2_000,
                        reporting_origin="Reuters",
                        raw={"guid": "rss-1"},
                    ),
                ),
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                status_code=200,
                fetch_path="direct",
                direct_error_code=None,
                etag=None,
                last_modified=None,
                not_modified=False,
                claim_token=_claim(repository, rss.source_id, NOW_MS),
            )
            result = repository.rebuild_stories(now_ms=NOW_MS)
        assert result["stories"] == 1
        assert conn.execute("SELECT count(*) AS n FROM news_feed_observations").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM news_items").fetchone()["n"] == 2
        story = NewsInterface(repository).get_feed()["stories"][0]
        assert story["item_count"] == 2
        assert story["source_count"] == 1
        assert story["source_name"] == "reuters"
        detail = NewsInterface(repository).get_story(story_id=story["story_id"])
        assert detail is not None
        assert len(detail["members"]) == 2
        assert any(member["url"] is None for member in detail["members"])
        assert rest.calls >= 1
        opennews_status = conn.execute(
            """
            SELECT live_connected, last_recovery_at_ms, gap_unclosed
              FROM news_sources WHERE source_id='news-opennews'
            """
        ).fetchone()
        assert opennews_status["live_connected"] is False
        assert opennews_status["gap_unclosed"] is False
        assert opennews_status["last_recovery_at_ms"] is not None
        assert (
            conn.execute(
                """
                SELECT count(*) AS n FROM news_source_fetches
                 WHERE source_id='news-opennews' AND fetch_path='opennews_rest'
                """
            ).fetchone()["n"]
            >= 1
        )
    finally:
        conn.close()


def test_opennews_disconnect_reconnects_and_rest_closes_the_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        monkeypatch.setattr("tracefold.news.runtime._OPENNEWS_RECONNECT_SECONDS", 0.01)
        message = {
            "method": "news.update",
            "params": {
                "id": "after-reconnect",
                "text": "OpenNews resumes after a disconnected socket",
                "newsType": "Reuters",
                "engineType": "news",
                "ts": NOW_MS,
            },
        }
        rest = _FakeOpenNewsRest(())
        websocket = _ReconnectOpenNewsWebSocket(message)
        acquisition = NewsAcquisition(
            db=_SingleConnectionDB(conn),
            finite_operations=_InlineCapability(),
            cpu=_InlineCapability(),
            sources=(_rss_source(), opennews_source()),
            feed_reader=_NoopReader(),
            feed_parser=lambda value: value,
            opennews_rest_client=rest,
            opennews_ws_client=websocket,
        )

        async def exercise() -> None:
            await acquisition.reconcile()
            stop = asyncio.Event()
            task = asyncio.create_task(acquisition.run_opennews(stop_event=stop))
            await _wait_until(
                lambda: (
                    websocket.connected >= 2
                    and conn.execute("SELECT count(*) AS n FROM news_items").fetchone()["n"] == 1
                ),
                message="OpenNews did not reconnect and publish",
            )
            await _wait_until(
                lambda: rest.calls >= 2,
                message="OpenNews reconnect did not request REST recovery",
            )
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)
            await acquisition.close()

        asyncio.run(exercise())
        status = conn.execute(
            """
            SELECT live_connected, gap_unclosed, last_recovery_at_ms
              FROM news_sources WHERE source_id='news-opennews'
            """
        ).fetchone()
        assert websocket.closed >= 2
        assert status["live_connected"] is False
        assert status["gap_unclosed"] is False
        assert status["last_recovery_at_ms"] is not None
    finally:
        conn.close()


def test_opennews_buffer_is_bounded_and_overflow_requests_rest_recovery() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        rest = _FakeOpenNewsRest(())
        websocket = _FloodOpenNewsWebSocket(count=300)
        db = _SlowLivePublishDB(conn)
        acquisition = NewsAcquisition(
            db=db,
            finite_operations=_InlineCapability(),
            cpu=_InlineCapability(),
            sources=(opennews_source(),),
            feed_reader=_NoopReader(),
            feed_parser=lambda value: value,
            opennews_rest_client=rest,
            opennews_ws_client=websocket,
        )

        async def exercise() -> None:
            await acquisition.reconcile()
            stop = asyncio.Event()
            task = asyncio.create_task(acquisition.run_opennews(stop_event=stop))
            await _wait_until(lambda: rest.calls >= 1, message="startup recovery did not run")
            websocket.release.set()
            await _wait_until(
                lambda: websocket.delivered == 300 and rest.calls >= 2,
                message="buffer overflow did not request recovery",
            )
            assert acquisition._opennews_queue.qsize() == 256
            db.release_live_publish.set()
            stop.set()
            await asyncio.wait_for(task, timeout=3.0)
            await acquisition.close()

        asyncio.run(exercise())
        observations = conn.execute(
            """
            SELECT count(*) AS n FROM news_feed_observations
             WHERE source_id='news-opennews' AND observation_kind='report'
            """
        ).fetchone()["n"]
        assert 0 < observations <= 257
        assert rest.calls >= 2
        assert (
            conn.execute(
                """
                SELECT count(*) AS n FROM news_source_fetches
                 WHERE source_id='news-opennews' AND fetch_path='opennews_rest'
                """
            ).fetchone()["n"]
            >= 2
        )
    finally:
        conn.close()


def test_missing_opennews_token_is_visible_and_does_not_block_rss() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        acquisition = NewsAcquisition(
            db=_SingleConnectionDB(conn),
            finite_operations=_InlineCapability(),
            cpu=_InlineCapability(),
            sources=(_rss_source(), opennews_source()),
            feed_reader=_NoopReader(),
            feed_parser=lambda value: value,
        )
        asyncio.run(acquisition.reconcile())
        status = conn.execute(
            """
            SELECT live_connected, gap_unclosed, last_error
              FROM news_sources WHERE source_id='news-opennews'
            """
        ).fetchone()
        assert status == {
            "live_connected": False,
            "gap_unclosed": True,
            "last_error": "opennews_token_missing",
        }
        repository = NewsRepository(conn)
        due_at_ms = conn.execute("SELECT next_fetch_at_ms FROM news_sources WHERE source_id='rss-reuters'").fetchone()[
            "next_fetch_at_ms"
        ]
        with conn.transaction():
            claimed = repository.claim_due_source(
                now_ms=int(due_at_ms),
                claim_token=str(uuid4()),
                lease_ms=45_000,
            )
        assert claimed is not None
        assert claimed["source_id"] == "rss-reuters"
    finally:
        conn.close()


def test_observation_kinds_do_not_create_extra_news_items() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        report = parse_opennews_message(
            {
                "method": "news.update",
                "params": {
                    "id": "wire-1",
                    "text": "Linkless market wire",
                    "newsType": "Reuters",
                    "engineType": "news",
                    "ts": NOW_MS,
                },
            }
        )
        translation = parse_opennews_message(
            {
                "method": "news.update",
                "params": {
                    "id": "wire-1-zh",
                    "text": "市场快讯",
                    "translationOf": "wire-1",
                    "newsType": "Reuters",
                    "engineType": "news",
                    "ts": NOW_MS,
                },
            }
        )
        annotation = parse_opennews_message(
            {"method": "news.ai_update", "params": {"id": "wire-1", "aiRating": {"score": 80}}}
        )
        assert report and translation and annotation
        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            first = repository.record_opennews_events(
                source=source,
                events=(report, translation, annotation),
                observed_at_ms=NOW_MS,
            )
            repeated = repository.record_opennews_events(
                source=source,
                events=(report, translation, annotation),
                observed_at_ms=NOW_MS + 1,
            )
        assert first == {
            "entries_seen": 3,
            "observations_inserted": 3,
            "items_inserted": 1,
            "items_updated": 0,
        }
        assert repeated["observations_inserted"] == 0
        assert conn.execute("SELECT count(*) AS n FROM news_items").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM news_feed_observations").fetchone()["n"] == 3
    finally:
        conn.close()


def test_full_projection_unchanged_input_writes_zero_and_expiry_changes_story_id() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = _rss_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            for guid, title, published in (
                ("early", "Fed holds rates steady", NOW_MS - 95 * 60 * 60 * 1_000),
                ("late", "Fed holds rates steady today", NOW_MS - 60_000),
            ):
                repository.record_fetch_success(
                    source=source,
                    entries=(
                        NewsFeedEntry(
                            guid=guid,
                            link=f"https://example.com/{guid}",
                            title=title,
                            published_at_ms=published,
                            reporting_origin="Reuters",
                            raw={"guid": guid},
                        ),
                    ),
                    started_at_ms=NOW_MS + (1 if guid == "late" else 0),
                    finished_at_ms=NOW_MS + (1 if guid == "late" else 0),
                    status_code=200,
                    fetch_path="direct",
                    direct_error_code=None,
                    etag=None,
                    last_modified=None,
                    not_modified=False,
                    claim_token=_claim(repository, source.source_id, NOW_MS + (1 if guid == "late" else 0)),
                )
            first = repository.rebuild_stories(now_ms=NOW_MS)
            old_id = conn.execute("SELECT story_id FROM news_stories").fetchone()["story_id"]
            second = repository.rebuild_stories(now_ms=NOW_MS)
        assert first["rows_written"] > 0
        assert second["projection_status"] == "unchanged_input"
        assert second["rows_written"] == 0
        with conn.transaction():
            third = repository.rebuild_stories(now_ms=NOW_MS + 2 * 60 * 60 * 1_000)
        new_id = conn.execute("SELECT story_id FROM news_stories").fetchone()["story_id"]
        assert third["projection_status"] == "rebuilt"
        assert old_id != new_id
        assert NewsInterface(repository).get_story(story_id=old_id) is None
    finally:
        conn.close()


def test_stale_full_projection_snapshot_writes_nothing() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = _rss_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            repository.record_fetch_success(
                source=source,
                entries=(
                    NewsFeedEntry(
                        guid="first",
                        link="https://example.com/first",
                        title="Fed holds rates steady",
                        published_at_ms=NOW_MS - 60_000,
                        reporting_origin="Reuters",
                        raw={"guid": "first"},
                    ),
                ),
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                status_code=200,
                fetch_path="direct",
                direct_error_code=None,
                etag=None,
                last_modified=None,
                not_modified=False,
                claim_token=_claim(repository, source.source_id, NOW_MS),
            )
            payload = repository.load_story_projection(now_ms=NOW_MS)
        snapshot = NewsProjectionSnapshot(
            input_fingerprint=str(payload["input_fingerprint"]),
            cutoff_ms=int(payload["cutoff_ms"]),
            scoring_epoch_ms=int(payload["scoring_epoch_ms"]),
            current_input_fingerprint=None,
            rows=tuple(dict(row) for row in payload["rows"]),
        )
        projection = compute_news_story_projection(snapshot)
        with conn.transaction():
            repository.record_fetch_success(
                source=source,
                entries=(
                    NewsFeedEntry(
                        guid="second",
                        link="https://example.com/second",
                        title="Treasury market opens after policy meeting",
                        published_at_ms=NOW_MS,
                        reporting_origin="AP",
                        raw={"guid": "second"},
                    ),
                ),
                started_at_ms=NOW_MS + 1,
                finished_at_ms=NOW_MS + 1,
                status_code=200,
                fetch_path="direct",
                direct_error_code=None,
                etag=None,
                last_modified=None,
                not_modified=False,
                claim_token=_claim(repository, source.source_id, NOW_MS + 1),
            )
            result = repository.publish_story_projection(
                snapshot=snapshot,
                projection=projection,
                now_ms=NOW_MS + 1,
            )
        assert result["projection_status"] == "stale_snapshot"
        assert result["rows_written"] == 0
        assert conn.execute("SELECT count(*) AS n FROM news_stories").fetchone()["n"] == 0
    finally:
        conn.close()


def test_story_invariant_failure_rolls_back_entire_publication() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = _rss_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            repository.record_fetch_success(
                source=source,
                entries=(
                    NewsFeedEntry(
                        guid="one",
                        link="https://example.com/one",
                        title="Fed holds rates steady",
                        published_at_ms=NOW_MS,
                        reporting_origin="Reuters",
                        raw={"guid": "one"},
                    ),
                ),
                started_at_ms=NOW_MS,
                finished_at_ms=NOW_MS,
                status_code=200,
                fetch_path="direct",
                direct_error_code=None,
                etag=None,
                last_modified=None,
                not_modified=False,
                claim_token=_claim(repository, source.source_id, NOW_MS),
            )
            payload = repository.load_story_projection(now_ms=NOW_MS)
        snapshot = NewsProjectionSnapshot(
            input_fingerprint=str(payload["input_fingerprint"]),
            cutoff_ms=int(payload["cutoff_ms"]),
            scoring_epoch_ms=int(payload["scoring_epoch_ms"]),
            current_input_fingerprint=None,
            rows=tuple(dict(row) for row in payload["rows"]),
        )
        projection = compute_news_story_projection(snapshot)
        projection["stories"][0]["item_count"] = 2
        with pytest.raises(RuntimeError, match="news_story_invariant_failed"), conn.transaction():
            repository.publish_story_projection(
                snapshot=snapshot,
                projection=projection,
                now_ms=NOW_MS,
            )
        assert conn.execute("SELECT count(*) AS n FROM news_stories").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM news_story_members").fetchone()["n"] == 0
        assert conn.execute("SELECT importance_score FROM news_items").fetchone()["importance_score"] == 0
    finally:
        conn.close()
