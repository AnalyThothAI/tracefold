"""A real News OI frame becomes one atomic engine-neutral Case/Signal pair."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.workers.wiring.database import WorkerNewsDatabase
from tracefold.app.workers.wiring.news_to_trading import (
    MAPPED_NEWS_PROJECTION_VERSION,
    news_oi_sources,
    to_oi_candidate_row,
)
from tracefold.news import OI_METRIC_VERSION
from tracefold.news.bus import RK_RAW_LIVE, BusMessage, new_trace_id, now_ms
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.triage import TriageConsumer
from tracefold.news.storage.trade_projection import NEWS_TRADE_PROJECTION_VERSION
from tracefold.trading.contracts import Bar, CaseState, OiCandidateRow
from tracefold.trading.signal_lane import BAR_INTERVAL_MS, SignalLane, SignalLaneConfig

pytestmark = pytest.mark.integration

NOW = now_ms()
EPOCH_STARTED_AT_MS = NOW - 600_000
STABLE_BUNDLE_SHA = "b" * 64
OI_SYMBOL = "SOL"
OI_TITLE = f"{OI_SYMBOL}\tOI Rise 7.20%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"


class RecordingBus:
    def __init__(self) -> None:
        self.published: list[BusMessage] = []

    async def publish(self, message: BusMessage) -> None:
        self.published.append(message)

    def of_kind(self, kind: str) -> list[BusMessage]:
        return [message for message in self.published if message.kind == kind]


class NewsDatabase:
    """The production News database port over one isolated PostgreSQL connection."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self._port = WorkerNewsDatabase(self)

    async def read(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        return await self._port.read(name, fn, timeout_seconds=timeout_seconds)

    async def tx(self, name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        return await self._port.tx(name, fn, timeout_seconds=timeout_seconds)

    @contextmanager
    def worker_session(self, name: str, *_args: Any, **_kwargs: Any):
        del name
        yield repositories_for_connection(self.conn)

    async def run_news(self, name: str, fn: Any, *args: Any, operation_timeout_seconds: float, **kwargs: Any):
        del operation_timeout_seconds, name
        return fn(*args, **kwargs)


class TradingDatabase:
    """The current Trading callback port over the same real database."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def read(self, _name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        del timeout_seconds
        result = fn(repositories_for_connection(self.conn))
        return await result if inspect.isawaitable(result) else result

    async def tx(self, _name: str, fn: Any, *, timeout_seconds: float = 3.0) -> Any:
        del timeout_seconds
        repos = repositories_for_connection(self.conn)
        with repos.transaction():
            return fn(repos)


def _news_projection(database: NewsDatabase) -> Callable[[str, int, int], Awaitable[Sequence[OiCandidateRow]]]:
    async def read(metric_version: str, after_created_at_ms: int, until_created_at_ms: int) -> Sequence[OiCandidateRow]:
        return await database.read(
            "trading_oi_projection",
            lambda repos: news_oi_sources(repos, metric_version, after_created_at_ms, until_created_at_ms),
            timeout_seconds=10.0,
        )

    return read


@pytest.fixture(scope="module")
def conn(postgres_module_clone_dsn: str):
    del postgres_module_clone_dsn
    connection = connect_postgres_test(read_only=False)
    repositories_for_connection(connection).news.register_agent_runtime_manifest(
        manifest_sha="c" * 64,
        stable_bundle_sha=STABLE_BUNDLE_SHA,
        envelope_sha256="d" * 64,
        artifact_schema_version="news_program_artifact_v1",
        program_version="news_semantic_program_seam_v1",
        program_sha256="9" * 64,
        candidate_shas=(),
        image_digest="sha256:" + "f" * 64,
        runtime_revision="seam-revision",
        now_ms=EPOCH_STARTED_AT_MS,
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def clean(conn: Any):
    conn.execute("TRUNCATE news_items, news_event_evidence_snapshots RESTART IDENTITY CASCADE")
    conn.commit()
    return conn


def _raw_message() -> BusMessage:
    stamp = now_ms()
    frame = {
        "id": 2_850_777,
        "newsType": "strategy",
        "engineType": "market",
        "text": OI_TITLE,
        "source": "binance",
        "coins": [],
        "ts": datetime.now(UTC).isoformat(),
        "strategy": {"id": 1019, "name": "OI Event Monitor", "engineType": "market", "sourceType": "market"},
    }
    return BusMessage(
        kind="raw",
        message_id=f"raw:{frame['id']}",
        routing_key=RK_RAW_LIVE.format(strategy_id="1019"),
        payload={"params": frame, "strategy_id": "1019", "ingest_mode": "live", "observed_at_ms": stamp},
        trace_id=new_trace_id(),
        occurred_at_ms=stamp,
    )


def _judge_frame(conn: Any) -> str:
    bus = RecordingBus()
    database = NewsDatabase(conn)
    asyncio.run(DeduperConsumer(bus=bus, db=database, watchlist_symbols=frozenset({OI_SYMBOL})).handle(_raw_message()))
    conn.commit()
    triage = TriageConsumer(
        bus=bus,
        db=database,
        judge=None,
        program_version="news_semantic_program_seam_v1",
        program_sha256="9" * 64,
        watchlist_symbols=frozenset({OI_SYMBOL}),
        watchlist=[OI_SYMBOL],
        concurrency=1,
        circuit_failures=3,
        circuit_open_seconds=60.0,
        runtime_manifest={"manifest_sha": "e" * 64},
    )
    events = bus.of_kind("event")
    assert events
    for message in events:
        asyncio.run(triage.handle(message))
    conn.commit()
    return str(events[0].payload["event_id"])


def _running_generation(conn: Any) -> str:
    rows = repositories_for_connection(conn).news.trade_candidate_oi_rows(
        metric_version=OI_METRIC_VERSION,
        after_created_at_ms=0,
        until_created_at_ms=now_ms() + 60_000,
    )
    assert rows
    return str(rows[0]["learning_epoch"])


def test_news_frame_mapper_and_signal_lane_commit_one_current_pair(clean: Any) -> None:
    conn = clean
    event_id = _judge_frame(conn)
    repos = repositories_for_connection(conn)
    rows = repos.news.trade_candidate_oi_rows(
        metric_version=OI_METRIC_VERSION,
        after_created_at_ms=0,
        until_created_at_ms=now_ms() + 60_000,
    )
    assert [row["event_id"] for row in rows] == [event_id]
    assert [row["provider_symbol"] for row in rows] == [OI_SYMBOL]
    evidence_rows = repos.news.trade_evidence_oi_rows(
        metric_version=OI_METRIC_VERSION,
        start_observed_at_ms=rows[0]["observed_at_ms"],
        end_observed_at_ms=rows[0]["observed_at_ms"] + 1,
        known_at_or_before_ms=now_ms() + 60_000,
        available_at_or_before_ms=now_ms() + 60_000,
    )
    assert [row["provider_symbol"] for row in evidence_rows] == [OI_SYMBOL]
    assert dict(to_oi_candidate_row(rows[0])) == dict(rows[0])
    assert MAPPED_NEWS_PROJECTION_VERSION == NEWS_TRADE_PROJECTION_VERSION

    async def bars(_candidate: Any, start: int, end: int) -> list[Bar]:
        aligned = (start // BAR_INTERVAL_MS) * BAR_INTERVAL_MS
        return [
            Bar(open_at_ms=opened, close_at_ms=opened + BAR_INTERVAL_MS, close=Decimal("150.00"))
            for opened in range(aligned, end + BAR_INTERVAL_MS, BAR_INTERVAL_MS)
        ]

    lane = SignalLane(
        db=TradingDatabase(conn),
        config=SignalLaneConfig(),
        bars=bars,
        oi_projection=_news_projection(NewsDatabase(conn)),
        news_generation=_running_generation(conn),
        release_revision="test-release",
        clock=now_ms,
    )
    first = asyncio.run(lane.advance())
    second = asyncio.run(lane.advance())

    cases = [dict(row) for row in conn.execute("SELECT * FROM trading_cases ORDER BY created_at_ms").fetchall()]
    signals = [dict(row) for row in conn.execute("SELECT * FROM trading_trade_signals ORDER BY seq").fetchall()]
    assert (first.sources, first.cases_created, first.signals_emitted) == (1, 1, 1)
    assert second.cases_created == 0
    assert len(cases) == len(signals) == 1
    assert CaseState(cases[0]["state"]) is CaseState.SIGNAL_EMITTED
    assert cases[0]["case_id"] == signals[0]["case_id"]
    assert cases[0]["capital_disposition"] == "not_applicable"
    assert signals[0]["market_key"] == f"crypto:perp:{OI_SYMBOL}:USDT"
    assert int(conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"]) == 0
    assert int(conn.execute("SELECT count(*) AS n FROM trading_orders").fetchone()["n"]) == 0
