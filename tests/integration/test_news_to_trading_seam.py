"""One OI frame crosses News, the App mapper, and the Trading capital lane — on real PostgreSQL.

News and Trading are siblings: neither imports the other and neither reads the other's tables, so
the only thing that connects a deterministic OI telemetry frame to a capital decision is
`app/workers/wiring/news_to_trading.py`. That module is field-by-field on purpose, and a
field-by-field mapper is exactly the kind of code that keeps compiling after it stops being true.

Both ends are real here. The News end runs the production Deduper and Triage over a real provider
frame, so the projection row the mapper receives is one the pipeline actually wrote rather than a
literal shaped like one. The Trading end runs the production `CapitalLane` against the real
authority rows. Nothing needs a live provider, and nothing needs a trading key — the absence of a
key is the point of the last assertion.

The expected outcome before #360 is a Policy `LONG` beside a capital refusal: a Case that reached a
decision, said the strategy would have taken it, and emitted no Intent because no authority exists
to grant one. #360 replaces that expectation atomically in its own PR; this module holds the
pre-#360 contract and no union of the two.
"""

from __future__ import annotations

import asyncio
import inspect
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
from tracefold.trading.capabilities import ExecutionCapabilitySnapshotV1, ExecutionInstrumentCapabilityV1
from tracefold.trading.capital_lane import BAR_INTERVAL_MS, CapitalLane, CapitalLaneConfig
from tracefold.trading.catalog import VenueInstrumentCatalogEntryV1, build_venue_catalog_snapshot
from tracefold.trading.contracts import Bar, CaseState

pytestmark = pytest.mark.integration

# The real clock, not a fixed literal. Evidence eligibility is a timestamp comparison: the trade
# projection admits a verdict only when the running epoch started before it, so an epoch opened at a
# far-future constant would leave the SQL correct and the result empty.
NOW = now_ms()
EPOCH_STARTED_AT_MS = NOW - 600_000
STABLE_BUNDLE_SHA = "b" * 64
OI_SYMBOL = "SOL"
# A frame the whole chain accepts, and every number in it is load-bearing: the OI rise clears the
# policy's `min_oi_change_bps` (5%), the OI value clears admission's `min_oi_value_usd` (20M), the
# whale ratio clears `min_whale_oi_ratio_bps` (50%), and `SOL` is not on the benchmark blacklist that
# `BTC` and `ETH` are. A frame that misses any of them stops earlier with a named reason, which is a
# real outcome but not the one this module is about.
OI_TITLE = f"{OI_SYMBOL} OI Rise 7.20%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"
CAPABILITY_SNAPSHOT = ExecutionCapabilitySnapshotV1(
    app_revision="seam-revision",
    app_image_digest="seam-image",
    nautilus_wheel_identity="seam-wheel",
    news_universe_digest="a" * 64,
    provider_universe_digest="b" * 64,
    included={
        f"{OI_SYMBOL}USDT-PERP.BINANCE": ExecutionInstrumentCapabilityV1(
            instrument_id=f"{OI_SYMBOL}USDT-PERP.BINANCE",
            native_symbol=f"{OI_SYMBOL}USDT",
            underlying_key=f"crypto:{OI_SYMBOL}",
            quote_currency="USDT",
            price_precision=2,
            size_precision=3,
            price_increment="0.01",
            size_increment="0.001",
            min_quantity="0.001",
            min_notional="5",
        )
    },
    excluded={},
)
CATALOG_SNAPSHOT = build_venue_catalog_snapshot(
    binding="BINANCE_USDM",
    captured_at_ms=NOW,
    stale_after_ms=86_400_000,
    instruments=(
        VenueInstrumentCatalogEntryV1(
            provider_instrument_id=f"{OI_SYMBOL}USDT",
            provider_symbol=f"{OI_SYMBOL}USDT",
            venue="binance.usdm",
            canonical_asset=OI_SYMBOL,
            canonical_namespace="crypto",
            product_kind="linear_perpetual",
            active=True,
            settlement_asset="USDT",
            margin_asset="USDT",
            price_increment="0.01",
            size_increment="0.001",
            min_quantity="0.001",
            raw_metadata_sha256="1" * 64,
        ),
    ),
)


class RecordingBus:
    def __init__(self) -> None:
        self.published: list[BusMessage] = []

    async def publish(self, message: BusMessage) -> None:
        self.published.append(message)

    def of_kind(self, kind: str) -> list[BusMessage]:
        return [message for message in self.published if message.kind == kind]


class NewsDatabase:
    """The production News database port over one test connection."""

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
    """One bounded read and one bounded transaction over the same real connection."""

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


@pytest.fixture(scope="module")
def conn(postgres_module_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    repos = repositories_for_connection(connection)
    # Activating a capability snapshot requires a fresh zero-exposure proof (#286): the runtime has to
    # have seen a flat account since the snapshot it is replacing, or the activation refuses.
    connection.execute(
        "UPDATE trading_runtime_state SET nautilus_ready = false, nautilus_unexpected_exposure = false,"
        " nautilus_bootstrap_account_zero_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    assert repos.trading.append_and_activate_execution_capability_snapshot(CAPABILITY_SNAPSHOT, created_at_ms=NOW)
    repos.trading.store_venue_catalog_snapshot(snapshot=CATALOG_SNAPSHOT, now_ms=NOW)
    # The Workers startup barrier appoints the running Agent and opens its evidence epoch. The trade
    # projection joins that appointment rather than the newest epoch row, so without this the SQL is
    # correct and returns nothing, which is exactly the shape of failure worth not mistaking for a bug.
    repos.news.register_agent_runtime_manifest(
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
    opened = connection.execute(
        "SELECT starts_at_ms FROM news_learning_epochs WHERE bundle_sha = %s", (STABLE_BUNDLE_SHA,)
    ).fetchone()
    # `open_learning_epoch` starts an epoch strictly after every epoch already recorded, and the clone's
    # migrations wrote theirs when the baseline was built. Checking that the result is already in the past
    # is what keeps the "correct SQL, empty result" failure from reaching a test body as a mystery.
    assert opened is not None and int(opened["starts_at_ms"]) <= now_ms(), (
        "the epoch must start before the frames this module judges, or the projection is empty"
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def clean(conn: Any):
    """A no-key authority and an empty News plane: the exact pre-#360 production shape."""

    conn.execute("TRUNCATE trading_intents, trading_orders, trading_cases CASCADE")
    conn.execute("TRUNCATE news_items, news_event_evidence_snapshots RESTART IDENTITY CASCADE")
    conn.execute(
        """
        UPDATE trading_runtime_state
           SET control = 'RUNNING', nautilus_ready = false, nautilus_unexpected_exposure = false,
               active_capability_snapshot_sha256 = %s, active_capability_included_count = %s
         WHERE id = 1
        """,
        (CAPABILITY_SNAPSHOT.snapshot_sha256, len(CAPABILITY_SNAPSHOT.included)),
    )
    conn.execute(
        """
        UPDATE trading_binding_runtime
           SET credential_state = 'unconfigured', credential_fingerprint = NULL,
               runtime_state = 'stopped', account_state = 'unknown',
               heartbeat_at_ms = NULL, reason = 'credentials_unconfigured', updated_at_ms = %s
         WHERE binding = 'BINANCE_USDM'
        """,
        (NOW,),
    )
    conn.commit()
    return conn


def _oi_frame() -> dict[str, Any]:
    return {
        "id": 2_850_777,
        "newsType": "strategy",
        "engineType": "market",
        "text": OI_TITLE,
        "source": "binance",
        "coins": [],
        "ts": datetime.now(UTC).isoformat(),
        "strategy": {"id": 1019, "name": "OI Event Monitor", "engineType": "market", "sourceType": "market"},
    }


def _raw_message(frame: dict[str, Any]) -> BusMessage:
    stamp = now_ms()
    return BusMessage(
        kind="raw",
        message_id=f"raw:{frame['id']}",
        routing_key=RK_RAW_LIVE.format(strategy_id="1019"),
        payload={"params": frame, "strategy_id": "1019", "ingest_mode": "live", "observed_at_ms": stamp},
        trace_id=new_trace_id(),
        occurred_at_ms=stamp,
    )


def _judge_an_oi_frame(conn: Any) -> str:
    """Run the production Deduper and Triage over one OI frame; return its Event id."""

    bus = RecordingBus()
    db = NewsDatabase(conn)
    deduper = DeduperConsumer(bus=bus, db=db, watchlist_symbols=frozenset({OI_SYMBOL}))
    asyncio.run(deduper.handle(_raw_message(_oi_frame())))
    conn.commit()
    triage = TriageConsumer(
        bus=bus,
        db=db,
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
    assert events, "the OI frame must be admitted and published to Triage"
    for message in events:
        asyncio.run(triage.handle(message))
    conn.commit()
    return str(events[0].payload["event_id"])


def test_the_mapper_carries_every_projected_oi_field_across_the_sibling_boundary(clean) -> None:
    """Field-by-field is the contract: nothing computed, nothing defaulted, nothing dropped."""

    conn = clean
    event_id = _judge_an_oi_frame(conn)
    repos = repositories_for_connection(conn)
    signal = repos.news.oi_signal(event_id=event_id, metric_version=OI_METRIC_VERSION)
    assert signal is not None, "the deterministic OI route must have written a telemetry row"

    rows = repos.news.trade_candidate_oi_rows(
        metric_version=OI_METRIC_VERSION,
        after_created_at_ms=0,
        until_created_at_ms=now_ms() + 60_000,
    )
    assert [row["event_id"] for row in rows] == [event_id]

    mapped = to_oi_candidate_row(rows[0])

    # The whole contract in one comparison. Equal keys means nothing was dropped and nothing was
    # invented; equal values means nothing was computed or defaulted on the way across. A News
    # projection that gains, loses or renames a field fails here rather than months later inside a
    # capital decision that silently lost it.
    assert dict(mapped) == dict(rows[0])
    assert MAPPED_NEWS_PROJECTION_VERSION == NEWS_TRADE_PROJECTION_VERSION
    assert mapped["symbol"] == OI_SYMBOL
    assert mapped["venue"] == "binance", "the frame's own provider source survives the seam, not a default"
    assert mapped["ingest_mode"] == "live"


def test_a_news_oi_frame_becomes_one_blocked_case_with_a_policy_long_and_no_intent(clean) -> None:
    """The whole seam, once: a real frame reaches a real capital decision and emits nothing.

    Everything a reader would want to check is a durable row afterwards. The Case exists, it is
    terminal, its Policy said the strategy would have gone long, its capital refusal names exactly
    one reason, and the Intent ledger is empty because no key or ready runtime ever existed.
    """

    conn = clean
    _judge_an_oi_frame(conn)

    async def _bars(_symbol: str, start: int, end: int) -> list[Bar]:
        """A flat public-candle series over the window the lane asks for.

        The public REST candle catalogue is faked, and only that. It is not the risk boundary this
        module exists to cross — the seam is — and #373 forbids reaching a live provider here. What
        stays real is that the lane must find a bar closed at or before the frame's own cutoff, so
        the series is generated from the exact range the lane requested rather than from a literal.
        """

        aligned = (start // BAR_INTERVAL_MS) * BAR_INTERVAL_MS
        return [
            Bar(open_at_ms=opened, close_at_ms=opened + BAR_INTERVAL_MS, close=Decimal("150.00"))
            for opened in range(aligned, end + BAR_INTERVAL_MS, BAR_INTERVAL_MS)
        ]

    lane = CapitalLane(
        db=TradingDatabase(conn),
        config=CapitalLaneConfig(target_notional_usd=Decimal("7.5")),
        bars=_bars,
        oi_projection=news_oi_sources,
        news_generation=_running_generation(conn),
        clock=now_ms,
    )

    first = asyncio.run(lane.advance())
    second = asyncio.run(lane.advance())
    conn.commit()

    assert first.outcome == "ADVANCED"
    assert first.sources >= 1, "the lane read the News projection the mapper produced"
    admissions = [
        dict(row)
        for row in conn.execute(
            "SELECT source_key, status, stage, reason, evidence FROM trading_candidate_gate_decisions"
        ).fetchall()
    ]
    cases = [dict(row) for row in conn.execute("SELECT * FROM trading_cases ORDER BY created_at_ms").fetchall()]
    assert len(cases) == 1, f"one frame, one Case, and a second turn creates no more; admissions={admissions}"
    case = cases[0]
    assert case["trigger_kind"] == "oi"
    assert case["underlying_key"] == f"crypto:{OI_SYMBOL}"
    assert CaseState(case["state"]) is CaseState.BLOCKED
    assert case["policy_decision"] == "long"
    assert case["capital_disposition"] == "blocked"
    assert case["capital_reason"] == "credentials_unconfigured"
    assert case["decided_at_ms"] is not None
    assert second.cases_created == 0

    assert int(conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"]) == 0
    assert int(conn.execute("SELECT count(*) AS n FROM trading_orders").fetchone()["n"]) == 0


def _running_generation(conn: Any) -> str:
    """The epoch the projection itself joined, which is what the lane compares a frozen Case against.

    Reading it from the projection rather than from `news_learning_epochs` directly is deliberate:
    the projection joins the epoch of the *active agent*, not the newest row, so after a rollback
    those are different answers and only one of them is the generation the process is running.
    """

    rows = repositories_for_connection(conn).news.trade_candidate_oi_rows(
        metric_version=OI_METRIC_VERSION,
        after_created_at_ms=0,
        until_created_at_ms=now_ms() + 60_000,
    )
    assert rows, "the projection must expose the judged OI frame before the lane can scan it"
    return str(rows[0]["learning_epoch"])
