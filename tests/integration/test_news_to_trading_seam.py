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
from tracefold.app.workers.wiring.news_to_trading import news_oi_sources, to_oi_candidate_row
from tracefold.news import OI_METRIC_VERSION
from tracefold.news.bus import RK_RAW_LIVE, BusMessage, new_trace_id, now_ms
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.trading.admission import ADMISSION_VERSION
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


def _admit_frame(conn: Any) -> str:
    """Run the admission consumer alone and return the ledger row's published source identity.

    Triage is deliberately absent (#553). An OI observation is stored with its typed fact in the
    admission transaction, so the whole News→Trading seam is answerable with no Event, no verdict,
    no reader history and no model configured anywhere in the process.
    """

    bus = RecordingBus()
    database = NewsDatabase(conn)
    asyncio.run(DeduperConsumer(bus=bus, db=database, watchlist_symbols=frozenset({OI_SYMBOL})).handle(_raw_message()))
    conn.commit()
    assert bus.of_kind("event") == [], "a market observation publishes nothing: there is no Event to triage"
    row = conn.execute(
        "SELECT event_id FROM news_oi_signals WHERE metric_version = %s", (OI_METRIC_VERSION,)
    ).fetchone()
    assert row is not None
    return str(row["event_id"])


def test_news_frame_mapper_and_signal_lane_commit_one_current_pair(clean: Any) -> None:
    conn = clean
    event_id = _admit_frame(conn)
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

    async def bars(_candidate: Any, start: int, end: int) -> list[Bar]:
        aligned = (start // BAR_INTERVAL_MS) * BAR_INTERVAL_MS
        return [
            Bar(open_at_ms=opened, close_at_ms=opened + BAR_INTERVAL_MS, close=Decimal("150.00"))
            for opened in range(aligned, end + BAR_INTERVAL_MS, BAR_INTERVAL_MS)
        ]

    lane = SignalLane(
        db=TradingDatabase(conn),
        config=SignalLaneConfig(oi_metric_version="oi_signal_v1"),
        bars=bars,
        oi_projection=_news_projection(NewsDatabase(conn)),
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

    No Event, no verdict, no editorial pipeline output and no active-arm row: the deterministic
    numbers and their source Item are the whole of what Trading reads. The Event this helper used to
    open went with the foreign key that demanded it (#553).
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
            market_kind="oi",
            market_source_strategy_id="1019",
            market_parse_status="parsed",
            market_parse_error=None,
        )
        news.insert_oi_signal(
            event_id=event_id,
            metric_version=OI_METRIC_VERSION,
            symbol=symbol,
            raw_instrument=symbol,
            direction="rise",
            oi_change_bps=720,
            oi_value_usd=32_170_000,
            whale_long_profit_bps=8_021,
            whale_oi_ratio_bps=10_071,
            observed_at_ms=observed_at_ms,
            received_at_ms=observed_at_ms,
            now_ms=observed_at_ms,
            provider="opennews",
            source_strategy_id="1019",
            source_contract_version="opennews_oi_source_v1",
            measurement_window_ms=300_000,
            measurement_definition="oi_signal_v1|opennews_oi_source_v1|300000",
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
        config=SignalLaneConfig(oi_metric_version="oi_signal_v1"),
        bars=bars,
        oi_projection=_news_projection(NewsDatabase(conn)),
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
                runtime_id=UUID("33333333-3333-4333-8333-333333333333"),
                alive=True,
                execution_safe=True,
                entries_armed=True,
                startup_reconciled=True,
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
                routes_count=len(set(market_keys)),
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
        config=SignalLaneConfig(oi_metric_version="oi_signal_v1"),
        bars=bars,
        oi_projection=_news_projection(NewsDatabase(conn)),
        clock=now_ms,
    )


def test_the_lane_admits_a_market_no_runtime_lists_and_the_runtime_is_the_one_catalogue(clean: Any) -> None:
    """#537 PR-3 F2P. Routability is answered once, by the process that can act on it.

    The lane used to read every Runtime's published catalogue and refuse an absent market as
    `eligibility:instrument_unmapped`, one scan behind the Runtime's own `instrument_unmapped`
    disposition and needing a "no catalogue published" special case that no other read had. Both frames
    now reach a Case; the Runtime refuses the one it cannot route, by name, on the entry path.

    The freeze budget went with it: two issuers in one window are two Cases in one turn.
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
    cases = {str(row["underlying_key"]) for row in conn.execute("SELECT underlying_key FROM trading_cases").fetchall()}

    assert turn.sources == 2
    assert turn.cases_created == 2
    assert cases == {"crypto:BTC", "crypto:DELL"}
    assert {value["reason"] for value in decisions.values()} == {"case_created"}
    assert decisions["oi:absent-evt:oi_signal_v1"]["case_id"] is not None
    # The rulebook that decided each row rides in `evidence` now, not in two key columns.
    assert decisions["oi:absent-evt:oi_signal_v1"]["evidence"]["gate_version"] == ADMISSION_VERSION
    assert set(decisions["oi:absent-evt:oi_signal_v1"]).isdisjoint({"gate_version", "gate_config_digest"})
    assert {
        str(row["market_key"]) for row in conn.execute("SELECT market_key FROM trading_trade_signals").fetchall()
    } == {"crypto:perp:BTC:USDT", "crypto:perp:DELL:USDT"}
    # What the Runtime publishes about its catalogue is its size, which is all `/status` renders.
    assert conn.execute("SELECT routes_count FROM trading_execution_runtime_state").fetchone()["routes_count"] == 1


def test_one_source_key_is_one_admission_row_whatever_configuration_saw_it(clean: Any) -> None:
    """#537 PR-3 F2P. The admission key is the source, so a re-decision advances the row it has.

    It used to be `(source_key, gate_version, gate_config_digest)`, on the promise that a threshold
    edit re-decides every source in a second row. The ledger never held two rows for one frame, and
    every reader paid for the possibility with a `DISTINCT ON` and a rule for which row was the answer.
    """

    conn = clean
    repos = repositories_for_connection(conn)
    row = {
        "source_key": "oi:key-collapse:oi_signal_v1",
        "trigger_kind": "oi",
        "underlying_key": "crypto:BTC",
        "source_observed_at_ms": now_ms(),
        "status": "DEFERRED",
        "stage": "market_context",
        "reason": "market_data_unavailable",
        "retryable": True,
        "case_id": None,
    }
    with repos.transaction():
        repos.trading.record_gate_decision(
            **row, evidence={"gate_config_digest": "a" * 64, "venue": "binance.usdm"}, now_ms=now_ms()
        )
    with repos.transaction():
        repos.trading.record_gate_decision(
            **{
                **row,
                "status": "REJECTED",
                "stage": "eligibility",
                "reason": "oi_value_below_floor",
                "retryable": False,
            },
            evidence={"gate_config_digest": "b" * 64, "floor": 20_000_000},
            now_ms=now_ms() + 1_000,
        )
    conn.commit()

    stored = [
        dict(value)
        for value in conn.execute("SELECT * FROM trading_candidate_gate_decisions ORDER BY source_key").fetchall()
    ]

    assert len(stored) == 1
    assert (stored[0]["status"], stored[0]["reason"], stored[0]["attempt_count"]) == (
        "REJECTED",
        "oi_value_below_floor",
        2,
    )
    # The terminal answer's own evidence, including the configuration that reached it.
    assert stored[0]["evidence"] == {"gate_config_digest": "b" * 64, "floor": 20_000_000}
    assert repos.trading.gate_decision_for_source_key(source_key=row["source_key"])["reason"] == (
        "oi_value_below_floor"
    )
