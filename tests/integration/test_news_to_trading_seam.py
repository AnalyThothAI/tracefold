"""A real News OI frame becomes one atomic engine-neutral Case/Signal pair."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

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
from tracefold.trading.storage.execution_stream import ExecutionRuntimeState

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
    conn.execute(
        "TRUNCATE news_items, news_event_evidence_snapshots, trading_cases, trading_trade_signals, "
        "trading_candidate_gate_decisions, trading_execution_runtime_state RESTART IDENTITY CASCADE"
    )
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
    assert [row["symbol"] for row in rows] == [OI_SYMBOL]
    evidence_rows = repos.news.trade_evidence_oi_rows(
        metric_version=OI_METRIC_VERSION,
        start_observed_at_ms=rows[0]["observed_at_ms"],
        end_observed_at_ms=rows[0]["observed_at_ms"] + 1,
        known_at_or_before_ms=now_ms() + 60_000,
        available_at_or_before_ms=now_ms() + 60_000,
    )
    # The live read and the evidence read answer the same ledger through the same sixteen keys (#510).
    assert [dict(row) for row in evidence_rows] == [dict(rows[0])]
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
    # `20260903_0355` dropped the six dead columns; a Case row now carries only what the lane writes.
    assert set(cases[0]).isdisjoint(
        {"regime", "program_version", "program_sha256", "program_output", "capital_disposition", "capital_reason"}
    )
    assert signals[0]["market_key"] == f"crypto:perp:{OI_SYMBOL}:USDT"
    # The lane emitting no Intent and no order used to be two `count(*) = 0` reads. `20260901_0347`
    # dropped both tables, so the claim is now made by the schema rather than measured here — see
    # `test_trading_signal_hard_cut.py::test_0347_drops_every_retired_execution_table_and_only_its_own_functions`.


def _numeric_oi_fact(conn: Any, *, event_id: str, item_id: str, observed_at_ms: int, symbol: str = OI_SYMBOL) -> None:
    """One News OI ledger row and the Item it was parsed from, and nothing else.

    No verdict, no editorial pipeline output, no learning epoch and no active-arm row: the
    deterministic numbers and their source Item are the whole of what Trading reads.
    """

    repos = repositories_for_connection(conn)
    news = repos.news
    with repos.transaction():
        news.upsert_item(
            item_id=item_id,
            source_id="opennews",
            source_item_key=item_id,
            title=OI_TITLE,
            raw_first_line="",
            description="",
            canonical_url=None,
            reporting_origin="OpenNews",
            published_at_ms=observed_at_ms,
            observed_at_ms=observed_at_ms,
            provider_metadata_json='{"source": "binance"}',
            strategy_ids_json='["1019"]',
            ingest_mode="live",
            trace_id="trace",
            now_ms=observed_at_ms,
        )
        news.insert_event(
            event_id=event_id,
            leader_item_id=item_id,
            dedupe_family="market",
            event_kind="oi",
            comparison_fingerprint=event_id,
            comparison_title=OI_TITLE,
            leader_title=OI_TITLE,
            focus_fact_id=f"fact:{event_id}",
            focus_fact_text=OI_TITLE,
            focus_fact_context="",
            focus_fact_method="whole_item",
            focus_span_start=0,
            focus_span_end=len(OI_TITLE),
            opened_at_ms=observed_at_ms,
            expires_at_ms=observed_at_ms + 3_600_000,
            admission="candidate",
            queue_priority="normal",
            provider_score=90,
            engine_type="market",
            asset_class="crypto",
            grounded_assets=(),
            grounded_assets_json="[]",
            watchlist_hits=(),
            watchlist_hits_json="[]",
            macro_lexicon=False,
            storyline_key=f"story:{event_id}",
            context_line="",
            ingest_mode="live",
            trace_id="trace",
            band_keys=(event_id,),
            now_ms=observed_at_ms,
        )
        news.insert_oi_signal(
            event_id=event_id,
            metric_version=OI_METRIC_VERSION,
            symbol=symbol,
            direction="rise",
            oi_change_bps=720,
            oi_value_usd=32_170_000,
            whale_long_profit_bps=8_021,
            whale_oi_ratio_bps=10_071,
            observed_at_ms=observed_at_ms,
            now_ms=observed_at_ms,
            source_strategy_id="1019",
            source_contract_version="opennews_oi_source_v1",
            measurement_window_ms=300_000,
            source_item_id=item_id,
            source_venue="binance",
        )


def test_numeric_oi_ledger_alone_freezes_a_case_and_emits_a_signal(clean: Any) -> None:
    """#510 PR-4. The deterministic ledger row is the whole seam.

    No triage verdict, no learning epoch and no active arm exist for this frame, so before the cut the
    projection returned nothing at all and the Case was never frozen. A News policy or Program bump
    changes exactly this much of Trading: nothing.
    """

    conn = clean
    observed = now_ms() - 30_000
    _numeric_oi_fact(conn, event_id="numeric-evt", item_id="numeric-item", observed_at_ms=observed)
    assert conn.execute("SELECT count(*) AS n FROM news_verdicts").fetchone()["n"] == 0

    repos = repositories_for_connection(conn)
    rows = repos.news.trade_candidate_oi_rows(
        metric_version=OI_METRIC_VERSION,
        after_created_at_ms=0,
        until_created_at_ms=now_ms() + 60_000,
    )
    assert [row["event_id"] for row in rows] == ["numeric-evt"]
    assert rows[0]["symbol"] == OI_SYMBOL
    assert rows[0]["venue"] == "binance"
    assert rows[0]["ingest_mode"] == "live"
    assert rows[0]["available_at_ms"] == observed

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
        release_revision="test-release",
        clock=now_ms,
    )
    turn = asyncio.run(lane.advance())

    cases = [dict(row) for row in conn.execute("SELECT * FROM trading_cases").fetchall()]
    signals = [dict(row) for row in conn.execute("SELECT * FROM trading_trade_signals").fetchall()]
    assert (turn.sources, turn.cases_created, turn.signals_emitted) == (1, 1, 1)
    assert len(cases) == len(signals) == 1
    assert CaseState(cases[0]["state"]) is CaseState.SIGNAL_EMITTED
    assert cases[0]["manifest"]["manifest_version"] == "trading_manifest_v11"
    assert cases[0]["manifest"]["primary_trigger"]["persisted_at_ms"] == observed
    assert signals[0]["market_key"] == f"crypto:perp:{OI_SYMBOL}:USDT"


def _publish_runtime_catalogue(conn: Any, *market_keys: str) -> None:
    """One started Runtime that has published exactly the markets it can reach."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.trading.put_execution_runtime_state(
            ExecutionRuntimeState(
                account_slot="binance_usdm_primary",
                mode="paper",
                runtime_release="nautilus-1.231.0+oi-v1",
                config_sha256="a" * 64,
                runtime_id=UUID("33333333-3333-4333-8333-333333333333"),
                runtime_revision="b" * 40,
                image_digest="sha256:" + "c" * 64,
                credential_fingerprint="d" * 64,
                lifecycle_state="running",
                alive=True,
                execution_safe=True,
                entries_armed=True,
                control_plane_ready=True,
                singleton_ready=True,
                startup_reconciled=True,
                portfolio_ready=True,
                audit_ready=True,
                day_start_ready=True,
                unexpected_exposure=False,
                account_flat=True,
                positions_count=0,
                open_orders_count=0,
                protection_status="not_applicable",
                reconciliation_observed_at_ns=2_000,
                heartbeat_at_ns=2_100,
                entry_block_reason=None,
                started_at_ns=1_900,
                updated_at_ns=2_100,
                routes=tuple(sorted(market_keys)),
            )
        )
    conn.commit()


def _seam_lane(conn: Any) -> SignalLane:
    async def bars(_candidate: Any, start: int, end: int) -> list[Bar]:
        aligned = (start // BAR_INTERVAL_MS) * BAR_INTERVAL_MS
        return [
            Bar(open_at_ms=opened, close_at_ms=opened + BAR_INTERVAL_MS, close=Decimal("150.00"))
            for opened in range(aligned, end + BAR_INTERVAL_MS, BAR_INTERVAL_MS)
        ]

    return SignalLane(
        db=TradingDatabase(conn),
        config=SignalLaneConfig(),
        bars=bars,
        oi_projection=_news_projection(NewsDatabase(conn)),
        release_revision="test-release",
        clock=now_ms,
    )


def test_a_market_the_runtime_cannot_execute_never_spends_the_turns_one_case_freeze(clean: Any) -> None:
    """#510 PR-2 F2P. The lane freezes one Case per turn; an unlistable market used to win it.

    On 2026-09-02 three of six Signals were emitted for markets Binance USD-M does not list. Each
    froze a Case, emitted a Signal, and came back `instrument_unmapped` from the Runtime, while a
    listed market behind it was deferred as `lane_capacity_exhausted`. Here the unlisted frame is the
    newer one, so before the cut it took the freeze and BTC waited a turn.
    """

    conn = clean
    _publish_runtime_catalogue(conn, "crypto:perp:BTC:USDT")
    _numeric_oi_fact(conn, event_id="listed-evt", item_id="listed-item", observed_at_ms=now_ms() - 60_000, symbol="BTC")
    _numeric_oi_fact(
        conn, event_id="absent-evt", item_id="absent-item", observed_at_ms=now_ms() - 30_000, symbol="DELL"
    )
    conn.commit()

    turn = asyncio.run(_seam_lane(conn).advance())

    decisions = {
        str(row["source_key"]): dict(row)
        for row in conn.execute("SELECT * FROM trading_candidate_gate_decisions").fetchall()
    }
    cases = [dict(row) for row in conn.execute("SELECT * FROM trading_cases").fetchall()]
    unlisted = decisions["oi:absent-evt:oi_signal_v1"]

    assert turn.sources == 2
    assert (unlisted["status"], unlisted["stage"], unlisted["reason"]) == (
        "REJECTED",
        "eligibility",
        "instrument_unmapped",
    )
    assert unlisted["retryable"] is False
    assert unlisted["evidence"]["market_key"] == "crypto:perp:DELL:USDT"
    assert unlisted["gate_version"] == "trading_admission_v8"
    assert unlisted["case_id"] is None
    # The one freeze went to the market a Runtime can actually reach.
    assert [row["underlying_key"] for row in cases] == ["crypto:BTC"]
    assert turn.cases_created == 1
    assert [
        str(row["market_key"]) for row in conn.execute("SELECT market_key FROM trading_trade_signals").fetchall()
    ] == ["crypto:perp:BTC:USDT"]


def test_no_published_catalogue_admits_every_market_exactly_as_before(clean: Any) -> None:
    """Execution disabled, or no Runtime started yet: the Signal is a notification card, not an order."""

    conn = clean
    _numeric_oi_fact(
        conn, event_id="absent-evt", item_id="absent-item", observed_at_ms=now_ms() - 30_000, symbol="DELL"
    )
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM trading_execution_runtime_state").fetchone()["n"] == 0

    turn = asyncio.run(_seam_lane(conn).advance())

    reasons = {
        str(row["reason"]) for row in conn.execute("SELECT reason FROM trading_candidate_gate_decisions").fetchall()
    }
    assert turn.cases_created == 1
    assert reasons == {"case_created"}
