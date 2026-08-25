"""The order ledger against a real PostgreSQL: the invariants that only a database can enforce.

Three of these exist because a runner's in-memory check is not an authority:

* one source fact produces one case, however often the bounded overlap window re-reads it;
* one underlying holds one active order **across both venues**, in every state that can carry or may
  yet turn out to carry exposure;
* one order records at most one provider attempt, because OpenTrade publishes no idempotency key and
  nothing downstream can deduplicate a resend.

The end-to-end runner tests drive the two real runners over this schema with a fault-scripted paper
adapter, so the ambiguous branch — the one that matters and that a always-succeeds fake never reaches
— is exercised rather than described.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.workers.wiring.news_to_trading import (
    news_trade_candidates,
    news_trade_instruments,
    to_news_candidate_row,
    to_oi_candidate_row,
)
from tracefold.trading.candidate.blacklist import Blacklist
from tracefold.trading.candidate.eligibility import EligibilityPolicy, Funnel, news_candidate, oi_candidate
from tracefold.trading.contracts import (
    ACTIVE_ORDER_STATES,
    TRADING_MANIFEST_VERSION,
    Bar,
    ExecutionObservation,
    ExecutionReceipt,
    InstrumentRef,
    LivePreflight,
    NativeProtection,
    NewsTradeCandidate,
    OiTradeCandidate,
    PreparedOrder,
    RemoteExposure,
    StartupReconciliation,
    TradeDecision,
    TradingCaseManifest,
    canonical_sha256,
)
from tracefold.trading.decision.policy import TradePolicy
from tracefold.trading.decision.program import DecisionResult
from tracefold.trading.decision.regime import RegimePolicy, assess
from tracefold.trading.execution.order import OrderPolicy
from tracefold.trading.execution.paper import PaperAdapter, PaperFaults
from tracefold.trading.pipeline.candidate import CandidateRunner
from tracefold.trading.pipeline.reconcile import ReconcileRunner
from tracefold.trading.pipeline.runtime import TradingConfig

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]

# Deliberately after the dynamically-created program_v7 epoch in migration 0302.
NOW = 1_900_000_000_000
MINUTE = 60_000


class _DirectDb:
    """The runners' database port, satisfied synchronously against one test connection.

    Production wires the one-slot cold admission; the runners only require `tx`/`read`, so the test
    exercises exactly the same call sites without a worker pool.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def tx(self, _name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        del timeout_seconds
        repos = repositories_for_connection(self._conn)
        try:
            result = fn(repos)
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()
        return result

    async def read(self, _name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        del timeout_seconds
        return fn(repositories_for_connection(self._conn))


@pytest.fixture(scope="module")
def _database():
    """Migrate once for the module, not once per test.

    A per-test `DROP SCHEMA public CASCADE` is far more churn than any other integration module
    creates, and it races other modules' lingering connections in a full-suite run — the tests then
    fail on `alembic_version already exists` rather than on anything they assert. One migration plus a
    cheap per-test truncation isolates just as well and is an order of magnitude faster.
    """

    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture()
def conn(_database):
    _reset(_database)
    return _database


def _reset(conn: Any) -> None:
    conn.execute("TRUNCATE trading_order_observations, trading_orders, trading_cases CASCADE")
    conn.execute("TRUNCATE news_oi_signals, news_verdicts, news_events, news_items CASCADE")
    conn.execute("DELETE FROM news_market_instruments")
    conn.execute("DELETE FROM trading_symbol_blacklist")
    # The deny-list seeds are migration state, so restoring them is part of restoring the schema.
    conn.execute(
        "INSERT INTO trading_symbol_blacklist (base_symbol, reason, expires_at_ms, created_at_ms, updated_at_ms) "
        "VALUES ('BTC', 'benchmark_large_cap', NULL, %s, %s), ('ETH', 'benchmark_large_cap', NULL, %s, %s), "
        "('CL', 'commodity_not_target', NULL, %s, %s)",
        (NOW, NOW, NOW, NOW, NOW, NOW),
    )
    conn.execute(
        "UPDATE trading_runtime_state SET control = 'RUNNING', day_key = '', orders_today = 0, "
        "dspy_calls_today = 0, funnel = '{}'::jsonb, updated_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    conn.commit()


def _repos(conn: Any) -> Any:
    return repositories_for_connection(conn)


def _day_key_for(now_ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _order_row(conn: Any, order_id: str) -> Any:
    return conn.execute("SELECT * FROM trading_orders WHERE order_id = %s", (order_id,)).fetchone()


def _case(conn: Any, *, case_id: str, source_key: str, underlying: str = "crypto:DOGE") -> bool:
    created = _repos(conn).trading.insert_case(
        case_id=case_id,
        underlying_key=underlying,
        case_kind="oi_only",
        mode="paper",
        primary_source_key=source_key,
        supplemental_source_keys=(),
        manifest={"case_kind": "oi_only"},
        manifest_sha256="sha",
        regime="buildup_up",
        observed_at_ms=NOW,
        now_ms=NOW,
    )
    conn.commit()
    return created


def _legacy_manifest(case_kind: str) -> dict[str, Any]:
    """The exact source-identity shape frozen by #161 before #160's hard cut."""

    oi = {
        "event_id": "legacy-oi",
        "observed_at_ms": NOW,
        "base_symbol": "DOGE",
        "venue": "hyperliquid",
        "oi_direction": "rise",
        "oi_change_bps": 320,
        "oi_value_usd": 73_010_000,
        "whale_long_profit_bps": 9_900,
        "whale_oi_ratio_bps": 21_097,
        "rank_in_window": 1,
        "metric_version": "oi_signal_v1",
        "program_version": "news_oi_signal_v1",
    }
    news = {
        "event_id": "legacy-news",
        "verdict_created_at_ms": NOW,
        "opened_at_ms": NOW,
        "base_symbol": "DOGE",
        "evidence_version": 1,
        "evidence_sha256": "sha",
        "focus_fact_id": "f",
        "comparison_fingerprint": "fp",
        "source_artifact_id": "x:1",
        "source_published_at_ms": NOW,
        "final_decision": "push",
        "event_type": "listing",
        "risk_direction": "bullish",
        "scope": "single_name",
        "magnitude": 2,
        "novelty": "new_fact",
        "headline_zh": "标题",
        "why_zh": "机制",
        "program_version": "program_v5",
        "policy_version": "news_triage_policy_v9",
    }
    return {
        "manifest_version": "trading_manifest_v1",
        "case_kind": case_kind,
        "underlying_key": "crypto:DOGE",
        "base_symbol": "DOGE",
        "cutoff_ms": NOW,
        "oi": oi if case_kind == "oi_only" else None,
        "news": news if case_kind == "news_only" else None,
        "regime": {"regime": "buildup_up", "reason": "quadrant", "pre_move_bps": 200, "oi_direction": "rise"},
        "instrument": {
            "exchange_id": "paper",
            "venue": "paper",
            "provider_symbol": "DOGEUSDT",
            "base_symbol": "DOGE",
            "instrument_class": "crypto",
            "quote_asset": "USDT",
            "observed_at_ms": NOW,
        },
        "mark_price": "102",
        "pre_move_bps": 200,
    }


def _order(conn: Any, *, order_id: str, case_id: str, underlying: str, exchange_id: str, state: str) -> None:
    _repos(conn).trading.insert_prepared_order(
        order_id=order_id,
        case_id=case_id,
        underlying_key=underlying,
        exchange_id=exchange_id,
        provider_symbol="DOGEUSDT",
        account_ref="default",
        mode="paper",
        side="buy",
        notional_usd="50",
        quantity="0.5",
        entry_reference="100",
        stop_price="98",
        take_profit_price=None,
        max_holding_ms=900_000,
        taker_fee_bps=5,
        payload={"symbol": "DOGEUSDT"},
        payload_sha256="digest",
        state=state,
        must_close_at_ms=NOW + 1_800_000,
        now_ms=NOW,
    )
    conn.commit()


# ---------------------------------------------------------------------------- identity invariants
def test_one_source_fact_produces_one_case_however_often_the_window_is_re_read(conn) -> None:
    assert _case(conn, case_id="c1", source_key="oi:e1:v1") is True
    # The scanner keeps no cursor; it re-reads a bounded overlap. This is what makes that safe.
    assert _case(conn, case_id="c2", source_key="oi:e1:v1") is False
    assert len(_repos(conn).trading.cases()) == 1


def test_one_case_authors_at_most_one_order(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="PREPARED")
    inserted = _repos(conn).trading.insert_prepared_order(
        order_id="o2",
        case_id="c1",
        underlying_key="crypto:DOGE",
        exchange_id="paper",
        provider_symbol="DOGEUSDT",
        account_ref="default",
        mode="paper",
        side="buy",
        notional_usd="50",
        quantity="0.5",
        entry_reference="100",
        stop_price="98",
        take_profit_price=None,
        max_holding_ms=900_000,
        taker_fee_bps=5,
        payload={},
        payload_sha256="d",
        state="PREPARED",
        must_close_at_ms=None,
        now_ms=NOW,
    )
    conn.commit()
    assert inserted is False


@pytest.mark.parametrize("state", sorted(ACTIVE_ORDER_STATES))
def test_every_active_state_blocks_a_second_order_for_the_same_underlying_across_venues(conn, state: str) -> None:
    """Including the four a literal reading of the spec leaves out.

    `RECONCILING` and `MANUAL_REVIEW_REQUIRED` mean "we do not know whether Binance filled it", and
    `UNPROTECTED` means "there is a position and no stop". Those are the states in which a second
    venue's entry is most dangerous, so they are the states the index most has to cover.
    """

    _case(conn, case_id="c1", source_key="k1")
    _case(conn, case_id="c2", source_key="k2")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="binance", state=state)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _order(
            conn,
            order_id="o2",
            case_id="c2",
            underlying="crypto:DOGE",
            exchange_id="hyperliquid",
            state="PREPARED",
        )
    conn.rollback()


def test_a_terminal_order_releases_the_underlying(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _case(conn, case_id="c2", source_key="k2")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="binance", state="OPEN")
    _repos(conn).trading.update_order(order_id="o1", state="CLOSED", closed_at_ms=NOW, now_ms=NOW)
    conn.commit()
    _order(conn, order_id="o2", case_id="c2", underlying="crypto:DOGE", exchange_id="hyperliquid", state="PREPARED")
    assert _repos(conn).trading.active_underlyings() == ["crypto:DOGE"]


def test_a_different_underlying_is_unaffected(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _case(conn, case_id="c2", source_key="k2", underlying="crypto:SOL")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="binance", state="OPEN")
    _order(conn, order_id="o2", case_id="c2", underlying="crypto:SOL", exchange_id="hyperliquid", state="OPEN")
    assert sorted(_repos(conn).trading.active_underlyings()) == ["crypto:DOGE", "crypto:SOL"]


# ---------------------------------------------------------------------------- one attempt
def test_the_ledger_can_record_exactly_one_provider_attempt(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="PREPARED")
    trading = _repos(conn).trading
    assert trading.claim_attempt(order_id="o1", kind="entry", now_ms=NOW) == "claimed"
    conn.commit()
    # A caller that somehow tried again finds zero rows updated and must not call the provider.
    assert trading.claim_attempt(order_id="o1", kind="entry", now_ms=NOW) == "already_spent"
    conn.commit()
    assert int(trading.order(order_id="o1")["provider_attempt_count"]) == 1


def test_the_attempt_ceiling_is_a_database_check_not_a_convention(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="PREPARED")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE trading_orders SET provider_attempt_count = 2 WHERE order_id = 'o1'")
    conn.rollback()


def test_an_unchanged_remote_observation_bumps_a_counter_rather_than_appending_rows(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="OPEN")
    trading = _repos(conn).trading
    content = {"state": "OPEN", "qty": "0.5"}
    for _ in range(3):
        trading.record_observation(
            order_id="o1",
            observation_kind="reconcile",
            content_sha256=canonical_sha256(content),
            content=content,
            now_ms=NOW,
        )
    conn.commit()
    rows = trading.observations(order_id="o1")
    assert len(rows) == 1
    assert int(rows[0]["seen_count"]) == 3


# ---------------------------------------------------------------------------- approval
def test_approval_is_bound_to_the_exact_payload_digest_and_is_idempotent(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _order(
        conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="binance", state="AWAITING_APPROVAL"
    )
    trading = _repos(conn).trading
    assert trading.approve_order(order_id="o1", payload_sha256="wrong", now_ms=NOW) is False
    approved_at = NOW + 30_000
    assert trading.approve_order(order_id="o1", payload_sha256="digest", now_ms=approved_at) is True
    conn.commit()
    approved = trading.order(order_id="o1")
    assert approved["state_reason"] == "operator_approved_c2"
    assert approved["next_reconcile_at_ms"] == approved_at
    assert approved["updated_at_ms"] == approved_at
    assert trading.reschedule_order(
        order_id="o1",
        expected_state="APPROVED",
        next_reconcile_at_ms=NOW + 61_000,
        now_ms=NOW + 31_000,
    )
    conn.commit()
    deferred = trading.order(order_id="o1")
    assert deferred["next_reconcile_at_ms"] == NOW + 61_000
    assert deferred["updated_at_ms"] == approved_at
    # A second approval of an already-approved order changes nothing: the operator signed once.
    assert trading.approve_order(order_id="o1", payload_sha256="digest", now_ms=NOW) is False


def test_approval_after_the_operator_window_is_rejected_at_the_database_boundary(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _order(
        conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="binance", state="AWAITING_APPROVAL"
    )
    trading = _repos(conn).trading
    assert trading.approve_order(order_id="o1", payload_sha256="digest", now_ms=NOW + 60_001) is False
    assert trading.order(order_id="o1")["state"] == "AWAITING_APPROVAL"


# ---------------------------------------------------------------------------- runner, end to end
def _seed_oi_event(conn: Any, *, event_id: str, symbol: str, observed_at_ms: int, venue: str = "hyperliquid") -> None:
    conn.execute(
        "INSERT INTO news_items (item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms, "
        "provider_metadata, provenance, first_ingest_mode, created_at_ms, updated_at_ms) "
        "VALUES (%s, 'opennews', %s, %s, %s, %s, %s::jsonb, '{}'::jsonb, 'live', %s, %s)",
        (
            f"i-{event_id}",
            f"k-{event_id}",
            f"{symbol} OI Rise 3.2%",
            observed_at_ms,
            observed_at_ms,
            f'{{"source": "{venue}"}}',
            observed_at_ms,
            observed_at_ms,
        ),
    )
    conn.execute(
        "INSERT INTO news_events (event_id, leader_item_id, family, comparison_fingerprint, comparison_title, "
        "leader_title, opened_at_ms, last_member_at_ms, expires_at_ms, member_count, admission, queue_priority, "
        "engine_type, asset_class, grounded_assets, watchlist_hits, macro_lexicon, storyline_key, ingest_mode, "
        "focus_fact_id, created_at_ms, updated_at_ms) VALUES (%s, %s, 'market', %s, %s, %s, %s, %s, %s, 1, "
        "'telemetry_deterministic', 'normal', 'market', 'crypto', %s::jsonb, '[]'::jsonb, false, %s, 'live', "
        "%s, %s, %s)",
        (
            event_id,
            f"i-{event_id}",
            f"fp-{event_id}",
            symbol,
            f"{symbol} OI Rise 3.2%",
            observed_at_ms,
            observed_at_ms,
            observed_at_ms + 3_600_000,
            f'["{symbol}"]',
            f"asset:{symbol}",
            f"f-{event_id}",
            observed_at_ms,
            observed_at_ms,
        ),
    )
    conn.execute(
        "INSERT INTO news_verdicts (event_id, stage, policy_version, model_decision, rule_baseline_decision, "
        "final_decision, verdict, trace, degraded, published_at_ms, created_at_ms, evidence_version, "
        "evidence_sha256, focus_fact_id, program_version, program_sha256, editorial, "
        "scored_judgment_sha256, runtime_manifest_sha) "
        "VALUES (%s, 'triage', 'news_triage_policy_v10', 'push', 'push', 'push', '{}'::jsonb, '{}'::jsonb, false, "
        "%s, %s, 1, 'sha', %s, 'news_oi_signal_v1', repeat('a', 64), "
        "jsonb_build_object('editorial_origin', 'telemetry_deterministic', 'editorial_sha256', repeat('b', 64), "
        "'relevance', NULL), repeat('c', 64), repeat('d', 64))",
        (event_id, observed_at_ms, observed_at_ms, f"f-{event_id}"),
    )
    conn.execute(
        "INSERT INTO news_oi_signals (event_id, metric_version, symbol, direction, oi_change_bps, oi_value_usd, "
        "whale_long_profit_bps, whale_oi_ratio_bps, observed_at_ms, rank_in_window, created_at_ms) "
        "VALUES (%s, 'oi_signal_v1', %s, 'rise', 320, 73010000, 9900, 21097, %s, 1, %s)",
        (event_id, symbol, observed_at_ms, observed_at_ms),
    )
    conn.execute(
        "INSERT INTO news_market_instruments (venue, venue_symbol, base_symbol, instrument_class, quote_asset, "
        "status, last_seen_ms) VALUES ('binance.perp', %s, %s, 'crypto', 'USDT', 'trading', %s) "
        "ON CONFLICT DO NOTHING",
        (f"{symbol}USDT", symbol, observed_at_ms),
    )
    conn.commit()


def _promote_to_model_projection(conn: Any, *, event_id: str, symbol: str) -> None:
    conn.execute("UPDATE news_events SET admission = 'candidate' WHERE event_id = %s", (event_id,))
    conn.execute(
        "UPDATE news_verdicts SET program_version = 'news_semantic_program_v5', verdict = %s::jsonb, "
        "editorial = jsonb_build_object('editorial_origin', 'model', "
        "'editorial_sha256', repeat('e', 64), 'relevance', '{}'::jsonb) WHERE event_id = %s",
        (
            '{"assets":[{"symbol":"' + symbol + '","role":"primary"}],"novelty":"new_fact","magnitude":2,'
            '"direction":"bullish","scope":"single_name","event_type":"listing",'
            '"headline_zh":"标题","why_zh":"机制"}',
            event_id,
        ),
    )
    conn.commit()


def _config(**kwargs: Any) -> TradingConfig:
    defaults: dict[str, Any] = {
        "mode": "paper",
        "eligibility": EligibilityPolicy(max_age_ms=3_600_000, symbol_cooldown_ms=1_800_000),
        "regime": RegimePolicy(lookback_ms=3_600_000, min_price_move_bps=100, max_price_move_bps=600),
        "trade": TradePolicy(),
        "order": OrderPolicy(max_holding_ms=900_000),
    }
    defaults.update(kwargs)
    return TradingConfig(**defaults)


def _regime_bars(now: int) -> tuple[Bar, ...]:
    """Flat at 100 for the first half of the lookback, then 102: a +200 bps pre-move, inside the band."""

    start = now - 5_400_000
    out: list[Bar] = []
    for index in range(18):
        open_at = start + index * 300_000
        close_at = open_at + 300_000
        out.append(Bar(open_at_ms=open_at, close_at_ms=close_at, close=Decimal("100" if index < 9 else "102")))
    return tuple(out)


class _LiveDecisionProgram:
    async def decide(self, manifest: TradingCaseManifest) -> DecisionResult:
        assert manifest.case_kind == "news_oi"
        return DecisionResult(
            decision=TradeDecision(
                decision="long",
                directness="direct",
                surprise=3,
                price_in=0,
                alignment="aligned",
                horizon="hours",
                reason_code="material_direct_catalyst",
                thesis_zh="直接催化剂与 OI 象限一致",
                invalidation_zh="催化失效",
            ),
            identity=None,
            trace={"calls": 0, "provider": "test"},
        )


class _ReadOnlyLiveAdapter:
    name = "opentrade"
    writes_enabled = False

    def __init__(
        self,
        *,
        startup_exposures: tuple[RemoteExposure, ...] = (),
        account_identity_proven: bool = True,
    ) -> None:
        self.startup_exposures = startup_exposures
        self.account_identity_proven = account_identity_proven
        self.preflight_calls = 0
        self.submit_calls = 0
        self.startup_instruments: list[InstrumentRef] = []

    def _truth(self, *, instrument: InstrumentRef, account_ref: str) -> LivePreflight:
        exact_instrument = instrument.model_copy(
            update={
                "provider_symbol": f"{instrument.base_symbol}/USDT:USDT",
                "observed_at_ms": NOW,
            }
        )
        return LivePreflight(
            provider=self.name,
            observed_at_ms=NOW,
            server_time_ms=NOW,
            venue_healthy=True,
            instrument=exact_instrument,
            mark_price=Decimal("10"),
            bid_price=Decimal("9.99"),
            ask_price=Decimal("10.01"),
            spread_bps=20,
            quantity_step=Decimal("0.1"),
            price_tick=Decimal("0.01"),
            min_quantity=Decimal("0.1"),
            min_notional=Decimal("5"),
            requested_account_ref=account_ref,
            observed_account_ref=account_ref if self.account_identity_proven else None,
            available_balance=Decimal("100"),
            available_currency="USDT",
            hedged=True,
            leverage=1,
            margin_mode="cross",
            positions=tuple(item for item in self.startup_exposures if item.kind == "position"),
            open_orders=tuple(item for item in self.startup_exposures if item.kind == "open_order"),
        )

    async def startup(self, *, instrument: InstrumentRef, account_ref: str) -> StartupReconciliation:
        self.startup_instruments.append(instrument)
        return StartupReconciliation(preflight=self._truth(instrument=instrument, account_ref=account_ref))

    async def preflight(self, *, instrument: InstrumentRef, account_ref: str) -> LivePreflight:
        self.preflight_calls += 1
        return self._truth(instrument=instrument, account_ref=account_ref)

    async def submit(self, order: PreparedOrder) -> ExecutionReceipt:
        del order
        self.submit_calls += 1
        raise AssertionError("C1 must not submit")

    async def observe(self, order: PreparedOrder) -> ExecutionObservation:
        return ExecutionObservation(
            state="UNKNOWN",
            observed_at_ms=NOW,
            remote_order_id=order.remote_order_id,
            evidence={"provider": self.name},
        )

    async def close(self, order: PreparedOrder, *, quantity: Decimal) -> ExecutionReceipt:
        del order, quantity
        raise AssertionError("C1 must not close")

    async def aclose(self) -> None:
        return None


class _LiveLifecycleAdapter(_ReadOnlyLiveAdapter):
    writes_enabled = True

    def __init__(
        self,
        *,
        observations: list[ExecutionObservation] | None = None,
        repreflight_update: dict[str, Any] | None = None,
        submit_fault: str | None = None,
        observe_fault: bool = False,
    ) -> None:
        super().__init__()
        self.observations = list(observations or [])
        self.repreflight_update = dict(repreflight_update or {})
        self.submit_fault = submit_fault
        self.observe_fault = observe_fault
        self.close_quantities: list[Decimal] = []
        self.observed_close_ids: list[str | None] = []
        self.close_faults: list[str] = []

    async def preflight(self, *, instrument: InstrumentRef, account_ref: str) -> LivePreflight:
        self.preflight_calls += 1
        truth = self._truth(instrument=instrument, account_ref=account_ref)
        return truth.model_copy(update=self.repreflight_update if self.preflight_calls > 1 else {})

    async def submit(self, order: PreparedOrder) -> ExecutionReceipt:
        self.submit_calls += 1
        if self.submit_fault == "crash":
            raise SystemExit("process died after provider call")
        if self.submit_fault == "timeout":
            raise TimeoutError("lost provider answer")
        if self.submit_fault == "reject":
            return ExecutionReceipt(state="REJECTED", reason="provider_rejected")
        return ExecutionReceipt(state="ACKNOWLEDGED", remote_order_id=f"remote-{order.order_id}")

    async def observe(self, order: PreparedOrder) -> ExecutionObservation:
        self.observed_close_ids.append(order.remote_close_order_id)
        if self.observe_fault:
            raise TimeoutError("read unavailable")
        if self.observations:
            return self.observations.pop(0)
        return ExecutionObservation(
            state="UNKNOWN",
            observed_at_ms=NOW,
            remote_order_id=order.remote_order_id,
            evidence={"provider": self.name},
        )

    async def close(self, order: PreparedOrder, *, quantity: Decimal) -> ExecutionReceipt:
        del order
        self.close_quantities.append(quantity)
        if self.close_faults and self.close_faults.pop(0) == "timeout":
            raise TimeoutError("lost close answer")
        return ExecutionReceipt(state="ACKNOWLEDGED", remote_order_id=f"close-{len(self.close_quantities)}")


def _runner(
    conn: Any,
    *,
    adapter: Any,
    now: int,
    config: TradingConfig | None = None,
    program: Any = None,
) -> CandidateRunner:
    at_trigger = _regime_bars(now)

    def bars(_exchange_id: str) -> Any:
        async def fetch(_symbol: str, _start: int, _end: int) -> Any:
            return at_trigger

        return fetch

    return CandidateRunner(
        db=_DirectDb(conn),
        config=config or _config(),
        bars=bars,
        adapter=adapter,
        candidate_projection=news_trade_candidates,
        instrument_projection=news_trade_instruments,
        program=program,
        clock=lambda: now,
    )


def _live_config(*, order: OrderPolicy | None = None) -> TradingConfig:
    return _config(
        mode="live_reviewed",
        account_ref="canary",
        live_symbol="DOGE",
        venue_priority=("binance",),
        order=order
        or OrderPolicy(
            fixed_notional_usd=Decimal("10"),
            max_holding_ms=900_000,
            max_open_underlyings=1,
            max_orders_per_day=1,
        ),
    )


def _prepare_live_reviewed(
    conn: Any,
    *,
    adapter: _LiveLifecycleAdapter,
    config: TradingConfig | None = None,
) -> tuple[TradingConfig, Any]:
    _seed_oi_event(conn, event_id="live-c2-oi", symbol="DOGE", observed_at_ms=NOW - MINUTE)
    _seed_oi_event(conn, event_id="live-c2-news", symbol="DOGE", observed_at_ms=NOW - 2 * MINUTE)
    _promote_to_model_projection(conn, event_id="live-c2-news", symbol="DOGE")
    live_config = config or _live_config()
    report = asyncio.run(
        _runner(
            conn,
            adapter=adapter,
            now=NOW,
            config=live_config,
            program=_LiveDecisionProgram(),
        ).turn()
    )
    assert report["created"] == 1, report
    row = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert row["state"] == "AWAITING_APPROVAL"
    return live_config, row


def _approve_live(conn: Any, row: Any, *, now: int = NOW) -> None:
    assert _repos(conn).trading.approve_order(
        order_id=str(row["order_id"]),
        payload_sha256=str(row["payload_sha256"]),
        now_ms=now,
    )
    conn.commit()


def _live_position_observation(
    row: Any,
    *,
    quantity: Decimal = Decimal("1"),
    protected: bool = True,
    first_fill_at_ms: int = NOW,
) -> ExecutionObservation:
    remote_id = f"remote-{row['order_id']}"
    protection = (
        NativeProtection(
            remote_order_id=f"stop-{row['order_id']}",
            parent_remote_order_id=remote_id,
            account_ref="canary",
            exchange_id="binance",
            provider_symbol=str(row["provider_symbol"]),
            side="sell",
            quantity=quantity,
            trigger_price=Decimal(str(row["stop_price"])),
            reduce_only=True,
            status="open",
        )
        if protected
        else None
    )
    return ExecutionObservation(
        state="PARTIAL"
        if quantity < Decimal(str(row["quantity"]))
        else ("OPEN_PROTECTED" if protected else "OPEN_UNPROTECTED"),
        observed_at_ms=NOW,
        remote_order_id=remote_id,
        actual_position_quantity=quantity,
        filled_quantity=quantity,
        average_price=Decimal("10"),
        first_fill_at_ms=first_fill_at_ms,
        protection=protection,
        evidence={"provider": "opentrade", "snapshot": str(quantity)},
    )


def _live_absent_observation(row: Any, *, observed_at_ms: int) -> ExecutionObservation:
    return ExecutionObservation(
        state="ABSENT_CONFIRMED",
        observed_at_ms=observed_at_ms,
        remote_order_id=f"remote-{row['order_id']}",
        evidence={"provider": "opentrade", "snapshot": f"absent-{observed_at_ms}"},
    )


def test_oi_projection_exposes_only_post_epoch_v10_judgments(conn) -> None:
    epoch_start = int(
        conn.execute("SELECT starts_at_ms FROM news_learning_epochs WHERE epoch_id = 'program_v7'").fetchone()[
            "starts_at_ms"
        ]
    )
    _seed_oi_event(conn, event_id="pre-epoch", symbol="SOL", observed_at_ms=epoch_start - 1)
    _seed_oi_event(conn, event_id="old-policy", symbol="XRP", observed_at_ms=NOW - 2 * MINUTE)
    conn.execute("UPDATE news_verdicts SET policy_version = 'news_triage_policy_v9' WHERE event_id = 'old-policy'")
    _seed_oi_event(conn, event_id="current", symbol="DOGE", observed_at_ms=NOW - MINUTE)
    conn.commit()

    rows = _repos(conn).news.trade_candidate_oi_rows(
        metric_version="oi_signal_v1",
        after_created_at_ms=epoch_start - MINUTE,
        until_created_at_ms=NOW,
        max_rank_in_window=5,
        min_oi_value_usd=1,
    )

    assert [row["event_id"] for row in rows] == ["current"]
    assert rows[0]["learning_epoch"] == "program_v7"
    assert rows[0]["policy_version"] == "news_triage_policy_v10"
    assert rows[0]["editorial_origin"] == "telemetry_deterministic"
    assert len(rows[0]["scored_judgment_sha256"]) == 64


def test_model_projection_requires_v4_model_editorial_in_the_v6_epoch(conn) -> None:
    for event_id, symbol, program_version, origin in (
        ("current-model", "DOGE", "news_semantic_program_v5", "model"),
        ("old-model", "SOL", "program_v5", "model"),
        ("wrong-origin", "XRP", "news_semantic_program_v5", "telemetry_deterministic"),
    ):
        _seed_oi_event(conn, event_id=event_id, symbol=symbol, observed_at_ms=NOW - MINUTE)
        relevance = "{}" if origin == "model" else "null"
        conn.execute(
            "UPDATE news_events SET admission = 'candidate' WHERE event_id = %s",
            (event_id,),
        )
        conn.execute(
            "UPDATE news_verdicts SET program_version = %s, verdict = %s::jsonb, "
            "editorial = jsonb_build_object('editorial_origin', %s::text, 'editorial_sha256', repeat('e', 64), "
            "'relevance', %s::jsonb) WHERE event_id = %s",
            (
                program_version,
                '{"assets":[{"symbol":"' + symbol + '","role":"primary"}],"novelty":"new_fact",'
                '"magnitude":2,"direction":"bullish","scope":"single_name","event_type":"listing",'
                '"headline_zh":"标题","why_zh":"机制"}',
                origin,
                relevance,
                event_id,
            ),
        )
    conn.commit()

    rows = _repos(conn).news.trade_candidate_news_rows(
        after_created_at_ms=NOW - 2 * MINUTE,
        until_created_at_ms=NOW,
    )

    assert [row["event_id"] for row in rows] == ["current-model"]
    assert rows[0]["learning_epoch"] == "program_v7"
    assert rows[0]["program_version"] == "news_semantic_program_v5"
    assert rows[0]["editorial_origin"] == "model"
    assert len(rows[0]["runtime_manifest_sha"]) == 64


def test_news_to_trading_projection_freezes_fields_boundaries_order_and_content_identity(conn) -> None:
    after = NOW - 2 * MINUTE
    middle = NOW - MINUTE
    for event_id, symbol, created_at_ms in (
        ("oi-after-boundary", "ADA", after),
        ("oi-b", "SOL", middle),
        ("oi-a", "DOGE", middle),
        ("oi-until-boundary", "XRP", NOW),
    ):
        _seed_oi_event(conn, event_id=event_id, symbol=symbol, observed_at_ms=created_at_ms)

    for event_id, symbol, created_at_ms in (
        ("news-after-boundary", "ADA", after),
        ("news-b", "SOL", middle),
        ("news-a", "DOGE", middle),
        ("news-until-boundary", "XRP", NOW),
    ):
        _seed_oi_event(conn, event_id=event_id, symbol=symbol, observed_at_ms=created_at_ms)
        _promote_to_model_projection(conn, event_id=event_id, symbol=symbol)

    news_repository = _repos(conn).news
    oi_rows = news_repository.trade_candidate_oi_rows(
        metric_version="oi_signal_v1",
        after_created_at_ms=after,
        until_created_at_ms=NOW,
        max_rank_in_window=5,
        min_oi_value_usd=1,
    )
    news_rows = news_repository.trade_candidate_news_rows(
        after_created_at_ms=after,
        until_created_at_ms=NOW,
    )

    assert [row["event_id"] for row in oi_rows] == ["oi-a", "oi-b", "oi-until-boundary"]
    assert [row["event_id"] for row in news_rows] == ["news-a", "news-b", "news-until-boundary"]
    assert set(oi_rows[0]) == {
        "direction",
        "editorial_origin",
        "editorial_sha256",
        "event_id",
        "final_decision",
        "ingest_mode",
        "learning_epoch",
        "metric_version",
        "observed_at_ms",
        "oi_change_bps",
        "oi_value_usd",
        "policy_version",
        "program_sha256",
        "program_version",
        "rank_in_window",
        "runtime_manifest_sha",
        "scored_judgment_sha256",
        "symbol",
        "venue",
        "verdict_created_at_ms",
        "whale_long_profit_bps",
        "whale_oi_ratio_bps",
    }
    assert set(news_rows[0]) == {
        "asset_class",
        "comparison_fingerprint",
        "editorial_origin",
        "editorial_sha256",
        "event_id",
        "evidence_sha256",
        "evidence_version",
        "final_decision",
        "focus_fact_id",
        "grounded_assets",
        "ingest_mode",
        "learning_epoch",
        "opened_at_ms",
        "policy_version",
        "program_sha256",
        "program_version",
        "runtime_manifest_sha",
        "scored_judgment_sha256",
        "source_artifact_id",
        "source_published_at_ms",
        "verdict",
        "verdict_created_at_ms",
    }
    assert {
        (row["learning_epoch"], row["program_version"], row["policy_version"], row["editorial_origin"])
        for row in oi_rows
    } == {("program_v7", "news_oi_signal_v1", "news_triage_policy_v10", "telemetry_deterministic")}
    assert {
        (row["learning_epoch"], row["program_version"], row["policy_version"], row["editorial_origin"])
        for row in news_rows
    } == {("program_v7", "news_semantic_program_v5", "news_triage_policy_v10", "model")}

    blacklist = Blacklist.from_rows([])
    # Through the App mapper, because that is the only path a projection row takes to the trading lane.
    oi = oi_candidate(
        to_oi_candidate_row(next(row for row in oi_rows if row["event_id"] == "oi-a")),
        now_ms=NOW,
        blacklist=blacklist,
    )
    projected_news = news_candidate(
        to_news_candidate_row(next(row for row in news_rows if row["event_id"] == "news-a")),
        now_ms=NOW,
        blacklist=blacklist,
    )
    assert isinstance(oi, OiTradeCandidate)
    assert isinstance(projected_news, NewsTradeCandidate)
    manifest = TradingCaseManifest(
        case_kind="news_oi",
        underlying_key="crypto:DOGE",
        base_symbol="DOGE",
        cutoff_ms=NOW,
        oi=oi,
        news=projected_news,
        regime=assess(oi_direction=oi.oi_direction, move=200),
        instrument=InstrumentRef(
            exchange_id="binance",
            venue="binance.perp",
            provider_symbol="DOGEUSDT",
            base_symbol="DOGE",
            instrument_class="crypto",
            quote_asset="USDT",
            observed_at_ms=NOW,
        ),
        mark_price=Decimal("102"),
        pre_move_bps=200,
    )
    # #162 PR8-B: the manifest binds the exact News generation, so bumping epoch/program moves its
    # content hash. That is the contract working — a case frozen under v6 must not read as a v7 case.
    assert manifest.digest() == "0d659c107dbab3d1640da51bfb93f01c3a1154209265a1e2c656cf2e90259540"


def test_a_qualifying_frame_becomes_one_paper_order_with_no_model_call(conn) -> None:
    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    adapter = PaperAdapter()
    # `program=None`: an OI-only case must decide without a model, so an unconfigured program is not
    # an obstacle. If the lane ever routed one through DSPy this would settle as `program_unconfigured`.
    report = asyncio.run(_runner(conn, adapter=adapter, now=now).turn())

    assert report["created"] == 1, report
    trading = _repos(conn).trading
    case = trading.cases()[0]
    assert case["manifest"]["manifest_version"] == TRADING_MANIFEST_VERSION
    assert case["manifest"]["oi"]["learning_epoch"] == "program_v7"
    assert case["manifest"]["oi"]["policy_version"] == "news_triage_policy_v10"
    assert case["case_kind"] == "oi_only"
    assert case["state"] == "ORDER_PREPARED"
    assert case["policy_reason"] == "oi_only_paper_regime"
    order = trading.order_for_case(case_id=case["case_id"])
    assert order is not None
    assert order["state"] == "ACKNOWLEDGED"
    assert order["filled_quantity"] is None
    assert order["average_price"] is None
    assert order["position_opened_at_ms"] is None
    assert order["must_close_at_ms"] is None
    assert int(order["provider_attempt_count"]) == 1
    assert adapter.attempts == 1


def test_an_acknowledged_paper_order_is_reconstructed_after_restart(conn) -> None:
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=NOW - MINUTE)
    asyncio.run(_runner(conn, adapter=PaperAdapter(), now=NOW).turn())

    restarted_adapter = PaperAdapter()
    reconcile = ReconcileRunner(
        db=_DirectDb(conn),
        config=_config(),
        bars=lambda _venue: None,
        adapter=restarted_adapter,
        clock=lambda: NOW + MINUTE,
    )
    asyncio.run(reconcile.turn())

    order = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert order["state"] == "OPEN"
    assert order["remote_order_id"] == f"paper-{order['order_id']}"
    assert order["filled_quantity"] == order["quantity"]
    assert order["average_price"] == order["entry_reference"]
    assert restarted_adapter.attempts == 0


def test_live_prepare_uses_fresh_provider_truth_and_the_existing_observation_ledger(conn) -> None:
    _seed_oi_event(conn, event_id="live-c1-oi", symbol="DOGE", observed_at_ms=NOW - MINUTE)
    _seed_oi_event(conn, event_id="live-c1-news", symbol="DOGE", observed_at_ms=NOW - 2 * MINUTE)
    _promote_to_model_projection(conn, event_id="live-c1-news", symbol="DOGE")
    adapter = _ReadOnlyLiveAdapter()
    report = asyncio.run(
        _runner(
            conn,
            adapter=adapter,
            now=NOW,
            config=_config(
                mode="live_reviewed",
                account_ref="canary",
                live_symbol="DOGE",
                venue_priority=("binance",),
                order=OrderPolicy(
                    fixed_notional_usd=Decimal("10"),
                    max_holding_ms=900_000,
                    max_open_underlyings=1,
                    max_orders_per_day=1,
                ),
            ),
            program=_LiveDecisionProgram(),
        ).turn()
    )
    assert report["created"] == 1, report
    assert report["decided"] == 1, report

    order = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert order is not None
    assert order["state"] == "AWAITING_APPROVAL"
    assert order["provider_symbol"] == "DOGE/USDT:USDT"
    assert order["entry_reference"] == Decimal("10")
    assert order["quantity"] == Decimal("1.0")
    assert order["payload"]["hedged"] is True
    observations = _repos(conn).trading.observations(order_id=str(order["order_id"]))
    assert [row["observation_kind"] for row in observations] == ["live_preflight"]
    assert observations[0]["content"]["account_identity_proven"] is True
    assert observations[0]["content"]["available_balance_proven"] is True
    assert "available_balance" not in observations[0]["content"]
    assert adapter.preflight_calls == 1
    assert adapter.submit_calls == 0


def test_external_startup_inventory_blocks_a_live_turn_before_case_creation(conn) -> None:
    _seed_oi_event(conn, event_id="live-exposure", symbol="DOGE", observed_at_ms=NOW - MINUTE)
    adapter = _ReadOnlyLiveAdapter(
        startup_exposures=(
            RemoteExposure(
                kind="position",
                exchange_id="binance",
                provider_symbol="DOGE/USDT:USDT",
                side="long",
                quantity=Decimal("1"),
            ),
        )
    )
    report = asyncio.run(
        _runner(
            conn,
            adapter=adapter,
            now=NOW,
            config=_config(
                mode="live_reviewed",
                account_ref="canary",
                live_symbol="DOGE",
                venue_priority=("binance",),
            ),
        ).turn()
    )

    assert report["skipped"] == "live_startup_not_ready"
    assert report["funnel"]["startup_reject:external_exposure"] == 1
    assert conn.execute("SELECT count(*) AS n FROM trading_cases").fetchone()["n"] == 0
    assert adapter.startup_instruments[0].quote_asset is None
    assert adapter.startup_instruments[0].venue == "binance.perp"


def test_read_only_c1_never_claims_or_submits_an_approved_live_order(conn) -> None:
    _case(conn, case_id="live-approved", source_key="live:approved")
    trading = _repos(conn).trading
    assert trading.insert_prepared_order(
        order_id="live-approved-order",
        case_id="live-approved",
        underlying_key="crypto:DOGE",
        exchange_id="binance",
        provider_symbol="DOGE/USDT:USDT",
        account_ref="canary",
        mode="live_reviewed",
        side="buy",
        notional_usd="10",
        quantity="1",
        entry_reference="10",
        stop_price="9.8",
        take_profit_price=None,
        max_holding_ms=900_000,
        taker_fee_bps=5,
        payload={"symbol": "DOGE/USDT:USDT"},
        payload_sha256="digest",
        state="AWAITING_APPROVAL",
        must_close_at_ms=NOW + 900_000,
        now_ms=NOW,
    )
    assert trading.approve_order(order_id="live-approved-order", payload_sha256="digest", now_ms=NOW)
    conn.commit()
    adapter = _ReadOnlyLiveAdapter()
    report = asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=_config(mode="live_reviewed", account_ref="canary", live_symbol="DOGE"),
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW,
        ).turn()
    )

    order = _order_row(conn, "live-approved-order")
    assert report["due"] == 1
    assert order["state"] == "APPROVED"
    assert int(order["provider_attempt_count"]) == 0
    assert adapter.submit_calls == 0


def test_read_only_c1_records_a_live_ack_observation_without_calling_it_a_fill(conn) -> None:
    _case(conn, case_id="live-ack", source_key="live:ack")
    trading = _repos(conn).trading
    assert trading.insert_prepared_order(
        order_id="live-ack-order",
        case_id="live-ack",
        underlying_key="crypto:DOGE",
        exchange_id="binance",
        provider_symbol="DOGE/USDT:USDT",
        account_ref="canary",
        mode="live_reviewed",
        side="buy",
        notional_usd="10",
        quantity="1",
        entry_reference="10",
        stop_price="9.8",
        take_profit_price=None,
        max_holding_ms=900_000,
        taker_fee_bps=5,
        payload={"symbol": "DOGE/USDT:USDT"},
        payload_sha256="digest",
        state="ACKNOWLEDGED",
        must_close_at_ms=None,
        now_ms=NOW,
    )
    conn.commit()
    adapter = _ReadOnlyLiveAdapter()
    report = asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=_config(mode="live_reviewed", account_ref="canary", live_symbol="DOGE"),
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW,
        ).turn()
    )

    order = _order_row(conn, "live-ack-order")
    assert report["resolved"] == 1
    assert order["state"] == "MANUAL_REVIEW_REQUIRED"
    assert order["filled_quantity"] is None
    assert order["position_opened_at_ms"] is None
    assert [row["observation_kind"] for row in trading.observations(order_id="live-ack-order")] == ["reconcile"]
    assert adapter.submit_calls == 0


@pytest.mark.parametrize("approved", [False, True])
def test_expired_live_approval_is_terminal_with_zero_provider_writes(conn, approved: bool) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    if approved:
        _approve_live(conn, row)

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 60_001,
        ).turn()
    )
    rejected = _order_row(conn, str(row["order_id"]))
    assert rejected["state"] == "REJECTED"
    assert rejected["state_reason"] == "approval_expired"
    assert int(rejected["provider_attempt_count"]) == 0
    assert adapter.submit_calls == 0


def test_an_unversioned_approved_row_from_before_c2_fails_closed(conn) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    conn.execute(
        "UPDATE trading_orders SET state = 'APPROVED', state_reason = NULL, "
        "next_reconcile_at_ms = %s, updated_at_ms = %s WHERE order_id = %s",
        (NOW + 80_000, NOW + 50_000, str(row["order_id"])),
    )
    conn.commit()

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 80_000,
        ).turn()
    )
    rejected = _order_row(conn, str(row["order_id"]))
    assert rejected["state"] == "REJECTED"
    assert rejected["state_reason"] == "approval_expired"
    assert int(rejected["provider_attempt_count"]) == 0
    assert adapter.submit_calls == 0


def test_stale_awaiting_scan_cannot_overwrite_a_concurrent_valid_approval(conn) -> None:
    adapter = _LiveLifecycleAdapter()
    config, stale_row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, stale_row, now=NOW + 59_999)

    class _StaleApprovalScanDb(_DirectDb):
        async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
            if name == "trading_reconcile_scan":
                return ([dict(stale_row)], "RUNNING")
            return await super().read(name, fn, timeout_seconds=timeout_seconds)

    report = asyncio.run(
        ReconcileRunner(
            db=_StaleApprovalScanDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 60_001,
        ).turn()
    )
    approved = _order_row(conn, str(stale_row["order_id"]))
    assert report["resolved"] == 0
    assert approved["state"] == "APPROVED"
    assert approved["state_reason"] == "operator_approved_c2"
    assert int(approved["provider_attempt_count"]) == 0
    assert adapter.submit_calls == 0


def test_in_window_approval_gets_one_runner_cadence_to_reach_submission(conn) -> None:
    approved_at = NOW + 31_000
    submitted_at = NOW + 60_001
    adapter = _LiveLifecycleAdapter(repreflight_update={"observed_at_ms": submitted_at, "server_time_ms": submitted_at})
    config, row = _prepare_live_reviewed(conn, adapter=adapter)

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 30_000,
        ).turn()
    )
    _approve_live(conn, row, now=approved_at)
    adapter.observations = [
        ExecutionObservation(
            state="WORKING",
            observed_at_ms=submitted_at,
            remote_order_id=f"remote-{row['order_id']}",
            evidence={"provider": "opentrade", "state": "working"},
        )
    ]

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: submitted_at,
        ).turn()
    )
    submitted = _order_row(conn, str(row["order_id"]))
    assert submitted["state"] == "ACKNOWLEDGED"
    assert int(submitted["provider_attempt_count"]) == 1
    assert adapter.submit_calls == 1


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"mark_price": Decimal("10.1")}, "live_repreflight_entry_drift"),
        ({"price_tick": Decimal("0.1")}, "live_repreflight_execution_contract_drift"),
        (
            {
                "positions": (
                    RemoteExposure(
                        kind="position",
                        exchange_id="binance",
                        provider_symbol="SOL/USDT:USDT",
                        side="long",
                        quantity=Decimal("1"),
                    ),
                )
            },
            "live_repreflight_remote_exposure",
        ),
    ],
)
def test_live_repreflight_drift_rejects_before_the_one_attempt(conn, update: dict[str, Any], reason: str) -> None:
    adapter = _LiveLifecycleAdapter(repreflight_update=update)
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 1_000,
        ).turn()
    )
    rejected = _order_row(conn, str(row["order_id"]))
    assert rejected["state"] == "REJECTED"
    assert rejected["state_reason"] == reason
    assert int(rejected["provider_attempt_count"]) == 0
    assert adapter.submit_calls == 0


@pytest.mark.parametrize(
    ("after_preflight", "reason"),
    [
        (NOW + 61_001, "approval_expired"),
        (NOW + 11_001, "live_repreflight_stale"),
    ],
)
def test_slow_repreflight_rechecks_expiry_and_freshness_before_write(conn, after_preflight: int, reason: str) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    clock_values = iter((NOW + 1_000, after_preflight))

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: next(clock_values),
        ).turn()
    )
    rejected = _order_row(conn, str(row["order_id"]))
    assert rejected["state"] == "REJECTED"
    assert rejected["state_reason"] == reason
    assert int(rejected["provider_attempt_count"]) == 0
    assert adapter.submit_calls == 0


@pytest.mark.parametrize(
    ("before_audit", "after_audit", "reason", "repreflight_update"),
    [
        (
            NOW + 59_000,
            NOW + 61_001,
            "approval_expired",
            {"observed_at_ms": NOW + 59_000, "server_time_ms": NOW + 59_000},
        ),
        (NOW + 9_000, NOW + 11_001, "live_repreflight_stale", {}),
    ],
)
def test_live_repreflight_rechecks_after_audit_before_claiming_the_attempt(
    conn,
    before_audit: int,
    after_audit: int,
    reason: str,
    repreflight_update: dict[str, Any],
) -> None:
    adapter = _LiveLifecycleAdapter(repreflight_update=repreflight_update)
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    clock_values = iter((NOW + 1_000, before_audit, after_audit))

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: next(clock_values),
        ).turn()
    )
    rejected = _order_row(conn, str(row["order_id"]))
    assert rejected["state"] == "REJECTED"
    assert rejected["state_reason"] == reason
    assert int(rejected["provider_attempt_count"]) == 0
    assert adapter.submit_calls == 0


@pytest.mark.parametrize(
    ("at_claim", "reason"),
    [
        (NOW + 61_001, "approval_expired"),
        (NOW + 11_001, "live_repreflight_stale"),
    ],
)
def test_live_repreflight_rechecks_inside_the_attempt_claim(conn, at_claim: int, reason: str) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    clock_values = iter((NOW + 1_000, NOW + 1_000, NOW + 1_000, at_claim))

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: next(clock_values),
        ).turn()
    )
    rejected = _order_row(conn, str(row["order_id"]))
    assert rejected["state"] == "REJECTED"
    assert rejected["state_reason"] == reason
    assert int(rejected["provider_attempt_count"]) == 0
    assert adapter.submit_calls == 0


@pytest.mark.parametrize(
    ("before_call", "reason"),
    [
        (NOW + 61_001, "approval_expired"),
        (NOW + 11_001, "live_repreflight_stale"),
    ],
)
def test_live_repreflight_rechecks_after_claim_commit_before_provider_call(
    conn,
    before_call: int,
    reason: str,
) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    clock_values = iter((NOW + 1_000, NOW + 1_000, NOW + 1_000, NOW + 1_000, before_call))

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: next(clock_values),
        ).turn()
    )
    rejected = _order_row(conn, str(row["order_id"]))
    assert rejected["state"] == "REJECTED"
    assert rejected["state_reason"] == reason
    assert int(rejected["provider_attempt_count"]) == 0
    assert _repos(conn).trading.orders_today(day_key=_day_key_for(NOW + 1_000)) == 0
    assert adapter.submit_calls == 0


def test_live_attempt_claim_crossing_utc_midnight_charges_the_new_day(conn) -> None:
    day_ms = 86_400_000
    next_midnight = (NOW // day_ms + 1) * day_ms
    before_midnight = next_midnight - 1
    adapter = _LiveLifecycleAdapter(
        repreflight_update={"observed_at_ms": before_midnight, "server_time_ms": before_midnight},
        submit_fault="crash",
    )
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    conn.execute(
        "UPDATE trading_orders SET created_at_ms = %s, updated_at_ms = %s WHERE order_id = %s",
        (next_midnight - 30_000, next_midnight - 30_000, str(row["order_id"])),
    )
    conn.commit()
    _approve_live(conn, row, now=before_midnight)
    clock_values = iter((before_midnight, before_midnight, before_midnight, next_midnight, next_midnight))

    with pytest.raises(SystemExit, match="process died after provider call"):
        asyncio.run(
            ReconcileRunner(
                db=_DirectDb(conn),
                config=config,
                bars=lambda _venue: None,
                adapter=adapter,
                clock=lambda: next(clock_values),
            ).turn()
        )

    claimed = _order_row(conn, str(row["order_id"]))
    assert claimed["state"] == "SUBMITTING"
    assert claimed["updated_at_ms"] == next_midnight
    assert _repos(conn).trading.orders_today(day_key=_day_key_for(before_midnight)) == 0
    assert _repos(conn).trading.orders_today(day_key=_day_key_for(next_midnight)) == 1
    assert adapter.submit_calls == 1


@pytest.mark.parametrize(
    ("quantity", "protected", "expected_state", "expected_closes"),
    [
        (Decimal("0.5"), True, "PARTIAL", []),
        (Decimal("1"), True, "OPEN", []),
        (Decimal("0.4"), False, "RECONCILING", [Decimal("0.4")]),
    ],
)
def test_live_fill_and_native_protection_drive_the_ledger_and_safety_close(
    conn,
    quantity: Decimal,
    protected: bool,
    expected_state: str,
    expected_closes: list[Decimal],
) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [_live_position_observation(row, quantity=quantity, protected=protected)]
    _approve_live(conn, row)

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 1_000,
        ).turn()
    )
    current = _order_row(conn, str(row["order_id"]))
    assert current["state"] == expected_state
    assert current["filled_quantity"] == quantity
    assert current["position_opened_at_ms"] == NOW
    assert int(current["provider_attempt_count"]) == 1
    assert adapter.submit_calls == 1
    assert adapter.close_quantities == expected_closes


@pytest.mark.parametrize(
    "evidence",
    [
        {"provider": "opentrade", "entry_remainder_working": True},
        {
            "provider": "opentrade",
            "entry_history_correlated_count": 1,
            "entry_history_count": 0,
        },
    ],
    ids=["working-partial", "mixed-entry-history"],
)
def test_unknown_live_truth_escalates_without_closing_any_position(conn, evidence: dict[str, object]) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [
        ExecutionObservation(
            state="UNKNOWN",
            observed_at_ms=NOW,
            remote_order_id=f"remote-{row['order_id']}",
            evidence=evidence,
        )
    ]
    _approve_live(conn, row)

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )

    current = _order_row(conn, str(row["order_id"]))
    assert current["state"] == "MANUAL_REVIEW_REQUIRED"
    assert current["filled_quantity"] is None
    assert adapter.submit_calls == 1
    assert adapter.close_quantities == []


def test_live_working_order_stays_acknowledged_without_a_fabricated_fill(conn) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [
        ExecutionObservation(
            state="WORKING",
            observed_at_ms=NOW,
            remote_order_id=f"remote-{row['order_id']}",
            evidence={"provider": "opentrade", "state": "working"},
        )
    ]
    _approve_live(conn, row)

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )
    working = _order_row(conn, str(row["order_id"]))
    assert working["state"] == "ACKNOWLEDGED"
    assert working["filled_quantity"] is None
    assert working["position_opened_at_ms"] is None
    assert adapter.submit_calls == 1


def test_live_acknowledgement_survives_one_empty_provider_read_before_the_fill_appears(conn) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [
        _live_absent_observation(row, observed_at_ms=NOW + 1_000),
        _live_position_observation(row),
    ]
    _approve_live(conn, row)

    first_turn = ReconcileRunner(
        db=_DirectDb(conn),
        config=config,
        bars=lambda _venue: None,
        adapter=adapter,
        clock=lambda: NOW + 1_000,
    )
    asyncio.run(first_turn.turn())
    pending = _order_row(conn, str(row["order_id"]))
    assert pending["state"] == "ACKNOWLEDGED"
    assert pending["state_reason"] == "provider_visibility_pending"
    assert str(row["underlying_key"]) in _repos(conn).trading.active_underlyings()

    second_turn = ReconcileRunner(
        db=_DirectDb(conn),
        config=config,
        bars=lambda _venue: None,
        adapter=adapter,
        clock=lambda: NOW + 31_001,
    )
    asyncio.run(second_turn.turn())
    opened = _order_row(conn, str(row["order_id"]))
    assert opened["state"] == "OPEN"
    assert int(opened["provider_attempt_count"]) == 1
    assert adapter.submit_calls == 1
    assert adapter.close_quantities == []


def test_two_empty_reads_after_a_live_ack_escalate_without_freeing_the_underlying(conn) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [
        _live_absent_observation(row, observed_at_ms=NOW + 1_000),
        _live_absent_observation(row, observed_at_ms=NOW + 31_001),
    ]
    _approve_live(conn, row)

    for observed_at in (NOW + 1_000, NOW + 31_001):
        asyncio.run(
            ReconcileRunner(
                db=_DirectDb(conn),
                config=config,
                bars=lambda _venue: None,
                adapter=adapter,
                clock=lambda observed_at=observed_at: observed_at,
            ).turn()
        )

    escalated = _order_row(conn, str(row["order_id"]))
    assert escalated["state"] == "MANUAL_REVIEW_REQUIRED"
    assert escalated["state_reason"] == "live_ack_visibility_unresolved"
    assert str(row["underlying_key"]) in _repos(conn).trading.active_underlyings()
    assert int(escalated["provider_attempt_count"]) == 1
    assert adapter.submit_calls == 1
    assert adapter.close_quantities == []


def test_live_submitting_after_restart_is_read_without_resending(conn) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    assert _repos(conn).trading.claim_attempt(order_id=str(row["order_id"]), kind="entry", now_ms=NOW + 1) == "claimed"
    conn.commit()
    adapter.observations = [_live_position_observation(row)]

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )
    orphaned = _order_row(conn, str(row["order_id"]))
    assert orphaned["state"] == "AMBIGUOUS"
    assert orphaned["state_reason"] == "entry_submitting_after_restart"
    assert adapter.submit_calls == 0

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_001
        ).turn()
    )
    recovered = _order_row(conn, str(row["order_id"]))
    assert recovered["state"] == "OPEN"
    assert int(recovered["provider_attempt_count"]) == 1
    assert adapter.submit_calls == 0


def test_unchanged_live_snapshot_bumps_seen_count_instead_of_growing_the_ledger(conn) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    snapshot = _live_position_observation(row)
    adapter.observations = [snapshot]
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )

    adapter.observations = [snapshot.model_copy(update={"observed_at_ms": NOW + 31_001})]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 31_001
        ).turn()
    )
    rows = [
        item
        for item in _repos(conn).trading.observations(order_id=str(row["order_id"]))
        if item["observation_kind"] == "reconcile"
    ]
    assert len(rows) == 1
    assert int(rows[0]["seen_count"]) == 2


def test_later_fill_time_evidence_never_extends_a_persisted_live_deadline(conn) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [
        _live_position_observation(row).model_copy(
            update={
                "observed_at_ms": NOW + 1_000,
                "first_fill_at_ms": None,
                "evidence": {"provider": "opentrade", "snapshot": "incomplete-time"},
            }
        )
    ]
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 1_000,
        ).turn()
    )
    first = _order_row(conn, str(row["order_id"]))
    conservative_opened = int(first["position_opened_at_ms"])
    conservative_deadline = int(first["must_close_at_ms"])
    assert conservative_opened == int(row["created_at_ms"])

    adapter.observations = [
        _live_position_observation(row, first_fill_at_ms=NOW + 500).model_copy(
            update={
                "observed_at_ms": NOW + 31_001,
                "evidence": {"provider": "opentrade", "snapshot": "later-complete-time"},
            }
        )
    ]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 31_001,
        ).turn()
    )
    current = _order_row(conn, str(row["order_id"]))
    assert int(current["position_opened_at_ms"]) == conservative_opened
    assert int(current["must_close_at_ms"]) == conservative_deadline


@pytest.mark.parametrize("regressed_state", ["WORKING", "REJECTED", "ABSENT_CONFIRMED", "UNKNOWN"])
def test_a_known_live_position_is_never_released_by_a_regressed_provider_snapshot(conn, regressed_state: str) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [_live_position_observation(row)]
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )

    adapter.observations = [
        ExecutionObservation(
            state=regressed_state,  # type: ignore[arg-type]
            observed_at_ms=NOW + 31_001,
            remote_order_id=f"remote-{row['order_id']}",
            evidence={"provider": "opentrade", "regressed": regressed_state},
        )
    ]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 31_001
        ).turn()
    )
    current = _order_row(conn, str(row["order_id"]))
    assert current["state"] == "MANUAL_REVIEW_REQUIRED"
    assert _repos(conn).trading.active_underlyings() == ["crypto:DOGE"]
    assert adapter.close_quantities == []


@pytest.mark.parametrize(
    "update",
    [
        {"remote_order_id": "another-entry"},
        {"actual_position_quantity": Decimal("2"), "filled_quantity": Decimal("2")},
        {"actual_position_quantity": Decimal("0.4"), "filled_quantity": Decimal("0.7")},
    ],
)
def test_inconsistent_live_position_evidence_escalates_without_a_close(conn, update: dict[str, Any]) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [_live_position_observation(row).model_copy(update=update)]
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )

    current = _order_row(conn, str(row["order_id"]))
    assert current["state"] == "MANUAL_REVIEW_REQUIRED"
    assert adapter.close_quantities == []


def test_future_live_fill_timestamp_escalates_without_extending_max_hold(conn) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [_live_position_observation(row, first_fill_at_ms=NOW + 1)]
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )

    current = _order_row(conn, str(row["order_id"]))
    assert current["state"] == "MANUAL_REVIEW_REQUIRED"
    assert current["must_close_at_ms"] is None
    assert adapter.close_quantities == []


def test_submit_timeout_spends_one_attempt_and_restart_resolves_only_by_read(conn) -> None:
    adapter = _LiveLifecycleAdapter(submit_fault="timeout", observe_fault=True)
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    first = ReconcileRunner(
        db=_DirectDb(conn),
        config=config,
        bars=lambda _venue: None,
        adapter=adapter,
        clock=lambda: NOW + 1_000,
    )
    asyncio.run(first.turn())
    ambiguous = _order_row(conn, str(row["order_id"]))
    assert ambiguous["state"] == "AMBIGUOUS"
    assert int(ambiguous["provider_attempt_count"]) == 1
    assert adapter.submit_calls == 1

    adapter.submit_fault = None
    adapter.observe_fault = False
    adapter.observations = [_live_position_observation(row)]
    restarted = ReconcileRunner(
        db=_DirectDb(conn),
        config=config,
        bars=lambda _venue: None,
        adapter=adapter,
        clock=lambda: NOW + 31_001,
    )
    asyncio.run(restarted.turn())
    opened = _order_row(conn, str(row["order_id"]))
    assert opened["state"] == "OPEN"
    assert int(opened["provider_attempt_count"]) == 1
    assert adapter.submit_calls == 1


def test_process_death_after_attempt_claim_keeps_the_daily_cap_charged(conn) -> None:
    adapter = _LiveLifecycleAdapter(submit_fault="crash")
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    runner = ReconcileRunner(
        db=_DirectDb(conn),
        config=config,
        bars=lambda _venue: None,
        adapter=adapter,
        clock=lambda: NOW + 1_000,
    )

    with pytest.raises(SystemExit, match="process died after provider call"):
        asyncio.run(runner.turn())
    crashed = _order_row(conn, str(row["order_id"]))
    assert crashed["state"] == "SUBMITTING"
    assert int(crashed["provider_attempt_count"]) == 1
    assert _repos(conn).trading.orders_today(day_key=_day_key_for(NOW + 1_000)) == 1

    adapter.submit_fault = None
    restarted = ReconcileRunner(
        db=_DirectDb(conn),
        config=config,
        bars=lambda _venue: None,
        adapter=adapter,
        clock=lambda: NOW + 31_001,
    )
    asyncio.run(restarted.turn())
    asyncio.run(restarted.turn())
    assert _order_row(conn, str(row["order_id"]))["state"] == "MANUAL_REVIEW_REQUIRED"
    assert _repos(conn).trading.resolve_manual_review(
        order_id=str(row["order_id"]), outcome="closed", reason="flat_at_venue", now_ms=NOW + 31_001
    )
    conn.commit()
    resolved = _order_row(conn, str(row["order_id"]))
    assert resolved["position_closed_at_ms"] is None
    assert _repos(conn).trading.last_close_at_ms(underlying_key=str(row["underlying_key"])) is None
    assert _repos(conn).trading.orders_today(day_key=_day_key_for(NOW + 31_001)) == 1
    assert adapter.submit_calls == 1


def test_definitive_live_rejection_releases_the_conservative_daily_charge(conn) -> None:
    adapter = _LiveLifecycleAdapter(submit_fault="reject")
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )
    assert _order_row(conn, str(row["order_id"]))["state"] == "REJECTED"
    assert _repos(conn).trading.orders_today(day_key=_day_key_for(NOW + 1_000)) == 0
    assert adapter.submit_calls == 1


def test_live_max_holding_closes_the_current_position_quantity_and_waits_for_read_truth(conn) -> None:
    policy = OrderPolicy(
        fixed_notional_usd=Decimal("10"),
        max_holding_ms=60_000,
        max_open_underlyings=1,
        max_orders_per_day=1,
    )
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter, config=_live_config(order=policy))
    adapter.observations = [_live_position_observation(row)]
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )

    adapter.observations = [
        _live_position_observation(row, quantity=Decimal("0.4")).model_copy(update={"filled_quantity": Decimal("1")})
    ]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 61_000
        ).turn()
    )
    closing = _order_row(conn, str(row["order_id"]))
    assert closing["state"] == "RECONCILING"
    assert adapter.close_quantities == [Decimal("0.4")]

    adapter.observations = [
        ExecutionObservation(
            state="CLOSED",
            observed_at_ms=NOW + 91_000,
            remote_order_id=f"remote-{row['order_id']}",
            first_fill_at_ms=NOW,
            closed_at_ms=NOW + 62_000,
            average_price=Decimal("9.9"),
            exit_price=Decimal("10.1"),
            evidence={"provider": "opentrade", "closed": True},
        )
    ]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 91_001
        ).turn()
    )
    closed = _order_row(conn, str(row["order_id"]))
    assert closed["state"] == "CLOSED"
    assert closed["position_closed_at_ms"] == NOW + 62_000
    assert int(closed["realized_bps"]) == 192
    assert adapter.close_quantities == [Decimal("0.4")]


@pytest.mark.parametrize(
    ("terminal_without_fill", "expected_state", "expected_closes", "expected_attempts"),
    [
        (False, "MANUAL_REVIEW_REQUIRED", [Decimal("1")], (1, 1)),
        (True, "RECONCILING", [Decimal("1"), Decimal("1")], (1, 2)),
    ],
)
def test_live_exit_rearms_only_after_the_exact_close_is_terminal_without_fill(
    conn,
    terminal_without_fill: bool,
    expected_state: str,
    expected_closes: list[Decimal],
    expected_attempts: tuple[int, int],
) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [_live_position_observation(row, protected=False)]
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 1_000,
        ).turn()
    )
    assert adapter.close_quantities == [Decimal("1")]

    active = _live_position_observation(row, protected=False)
    adapter.observations = [
        active.model_copy(
            update={"evidence": {**active.evidence, "close_terminal_without_fill": terminal_without_fill}}
        )
    ]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 31_001,
        ).turn()
    )

    current = _order_row(conn, str(row["order_id"]))
    assert adapter.observed_close_ids[-1] == "close-1"
    assert current["state"] == expected_state
    assert adapter.close_quantities == expected_closes
    assert (int(current["exit_attempt_count"]), int(current["exit_attempt_total"])) == expected_attempts


def test_an_ambiguous_new_close_never_reuses_an_older_attempts_terminal_identity(conn) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [_live_position_observation(row, protected=False)]
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 1_000,
        ).turn()
    )

    terminal = _live_position_observation(row, protected=False)
    adapter.observations = [
        terminal.model_copy(update={"evidence": {**terminal.evidence, "close_terminal_without_fill": True}})
    ]
    adapter.close_faults = ["timeout"]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 31_001,
        ).turn()
    )
    assert adapter.close_quantities == [Decimal("1"), Decimal("1")]

    adapter.observations = [
        terminal.model_copy(update={"evidence": {**terminal.evidence, "close_terminal_without_fill": True}})
    ]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 61_002,
        ).turn()
    )

    current = _order_row(conn, str(row["order_id"]))
    assert adapter.observed_close_ids[-1] is None
    assert current["state"] == "MANUAL_REVIEW_REQUIRED"
    assert (int(current["exit_attempt_count"]), int(current["exit_attempt_total"])) == (1, 2)
    assert adapter.close_quantities == [Decimal("1"), Decimal("1")]


def test_manual_rearm_fences_off_old_close_identity_when_the_new_attempt_is_ambiguous(conn) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [_live_position_observation(row, protected=False)]
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 1_000,
        ).turn()
    )
    trading = _repos(conn).trading
    trading.update_order(
        order_id=str(row["order_id"]),
        state="MANUAL_REVIEW_REQUIRED",
        state_reason="operator_check_required",
        next_reconcile_at_ms=NOW + 2_000,
        now_ms=NOW + 2_000,
    )
    assert trading.resolve_manual_review(
        order_id=str(row["order_id"]),
        outcome="open",
        reason="position_still_open",
        now_ms=NOW + 3_000,
    )
    conn.commit()

    adapter.observations = [_live_position_observation(row, protected=False)]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 31_001,
        ).turn()
    )
    assert adapter.close_quantities == [Decimal("1"), Decimal("1")]

    trading.update_order(
        order_id=str(row["order_id"]),
        state="MANUAL_REVIEW_REQUIRED",
        state_reason="operator_check_required_again",
        next_reconcile_at_ms=NOW + 32_000,
        now_ms=NOW + 32_000,
    )
    assert trading.resolve_manual_review(
        order_id=str(row["order_id"]),
        outcome="open",
        reason="position_still_open",
        # The same timestamp and reason as the first resolution must still allocate a new generation.
        now_ms=NOW + 3_000,
    )
    conn.commit()
    resolutions = [
        item["content"]["generation"]
        for item in trading.observations(order_id=str(row["order_id"]))
        if item["observation_kind"] == "operator_resolution" and item["content"]["outcome"] == "open"
    ]
    assert sorted(resolutions) == [1, 2]

    adapter.close_faults = ["timeout"]
    adapter.observations = [_live_position_observation(row, protected=False)]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 61_002,
        ).turn()
    )
    assert adapter.close_quantities == [Decimal("1"), Decimal("1"), Decimal("1")]

    stale_terminal = _live_position_observation(row, protected=False)
    adapter.observations = [
        stale_terminal.model_copy(update={"evidence": {**stale_terminal.evidence, "close_terminal_without_fill": True}})
    ]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + 91_003,
        ).turn()
    )

    current = _order_row(conn, str(row["order_id"]))
    assert adapter.observed_close_ids[-1] is None
    assert current["state"] == "MANUAL_REVIEW_REQUIRED"
    assert adapter.close_quantities == [Decimal("1"), Decimal("1"), Decimal("1")]


def test_slow_live_observation_that_crosses_max_holding_closes_in_the_same_turn(conn) -> None:
    policy = OrderPolicy(
        fixed_notional_usd=Decimal("10"),
        max_holding_ms=60_000,
        max_open_underlyings=1,
        max_orders_per_day=1,
    )
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter, config=_live_config(order=policy))
    adapter.observations = [_live_position_observation(row)]
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )

    adapter.observations = [_live_position_observation(row)]
    clock_values = iter((NOW + 59_000, NOW + 61_000))
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: next(clock_values),
        ).turn()
    )
    assert adapter.close_quantities == [Decimal("1")]
    assert _order_row(conn, str(row["order_id"]))["state"] == "RECONCILING"


def test_ambiguous_entry_that_was_manually_closed_is_never_released_as_absent(conn) -> None:
    adapter = _LiveLifecycleAdapter(submit_fault="timeout", observe_fault=True)
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )
    adapter.observe_fault = False
    adapter.observations = [
        ExecutionObservation(
            state="CLOSED",
            observed_at_ms=NOW + 31_001,
            remote_order_id="provider-recovered-id",
            closed_at_ms=NOW + 20_000,
            average_price=Decimal("10"),
            exit_price=Decimal("10"),
            evidence={"provider": "opentrade", "manual_close": True},
        )
    ]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 31_001
        ).turn()
    )
    closed = _order_row(conn, str(row["order_id"]))
    assert closed["state"] == "CLOSED"
    assert closed["state_reason"] == "provider_closed"
    assert closed["position_opened_at_ms"] == NOW
    assert int(closed["provider_attempt_count"]) == 1


def test_manual_open_recovery_requires_and_persists_the_provider_entry_identity(conn) -> None:
    adapter = _LiveLifecycleAdapter(submit_fault="timeout")
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )
    manual = _order_row(conn, str(row["order_id"]))
    assert manual["state"] == "MANUAL_REVIEW_REQUIRED"
    assert manual["remote_order_id"] is None

    trading = _repos(conn).trading
    assert (
        trading.resolve_manual_review(
            order_id=str(row["order_id"]),
            outcome="open",
            reason="position_confirmed",
            now_ms=NOW + 2_000,
        )
        is False
    )
    remote_id = f"remote-{row['order_id']}"
    assert trading.resolve_manual_review(
        order_id=str(row["order_id"]),
        outcome="open",
        reason="position_confirmed",
        remote_order_id=remote_id,
        now_ms=NOW + 2_000,
    )
    conn.commit()
    adapter.observations = [_live_position_observation(row)]

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 2_001
        ).turn()
    )
    recovered = _order_row(conn, str(row["order_id"]))
    assert recovered["state"] == "OPEN"
    assert recovered["remote_order_id"] == remote_id
    assert int(recovered["provider_attempt_count"]) == 1
    assert adapter.submit_calls == 1


def test_manual_open_recovery_never_replaces_a_known_provider_entry_identity(conn) -> None:
    adapter = _LiveLifecycleAdapter()
    _, row = _prepare_live_reviewed(conn, adapter=adapter)
    trading = _repos(conn).trading
    order_id = str(row["order_id"])
    remote_id = f"remote-{order_id}"
    trading.update_order(
        order_id=order_id,
        state="MANUAL_REVIEW_REQUIRED",
        remote_order_id=remote_id,
        now_ms=NOW,
    )
    conn.commit()

    assert (
        trading.resolve_manual_review(
            order_id=order_id,
            outcome="open",
            reason="position_confirmed",
            remote_order_id="different-provider-order",
            now_ms=NOW + 1,
        )
        is False
    )
    unchanged = _order_row(conn, order_id)
    assert unchanged["state"] == "MANUAL_REVIEW_REQUIRED"
    assert unchanged["remote_order_id"] == remote_id

    assert trading.resolve_manual_review(
        order_id=order_id,
        outcome="open",
        reason="position_confirmed",
        remote_order_id=remote_id,
        now_ms=NOW + 2,
    )
    conn.commit()
    recovered = _order_row(conn, order_id)
    assert recovered["state"] == "OPEN"
    assert recovered["remote_order_id"] == remote_id


@pytest.mark.parametrize("missing_field", ["closed_at_ms", "average_price", "exit_price"])
def test_incomplete_live_close_evidence_never_fabricates_a_terminal_position(conn, missing_field: str) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    observation = ExecutionObservation(
        state="CLOSED",
        observed_at_ms=NOW + 1_000,
        remote_order_id=f"remote-{row['order_id']}",
        first_fill_at_ms=NOW,
        closed_at_ms=NOW + 500,
        average_price=Decimal("9.9"),
        exit_price=Decimal("10.1"),
        evidence={"provider": "opentrade", "closed": True},
    )
    adapter.observations = [observation.model_copy(update={missing_field: None})]
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )
    current = _order_row(conn, str(row["order_id"]))
    assert current["state"] == "MANUAL_REVIEW_REQUIRED"
    assert current["state_reason"] == "live_closed_evidence_incomplete"
    assert current["position_closed_at_ms"] is None
    assert current["realized_bps"] is None


@pytest.mark.parametrize("existing_opened_at", [False, True])
def test_live_close_before_the_conservative_open_lower_bound_is_invalid(conn, existing_opened_at: bool) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    if existing_opened_at:
        adapter.observations = [_live_position_observation(row)]
        asyncio.run(
            ReconcileRunner(
                db=_DirectDb(conn),
                config=config,
                bars=lambda _venue: None,
                adapter=adapter,
                clock=lambda: NOW + 1_000,
            ).turn()
        )

    observed_now = NOW + (31_001 if existing_opened_at else 1_000)
    adapter.observations = [
        ExecutionObservation(
            state="CLOSED",
            observed_at_ms=observed_now,
            remote_order_id=f"remote-{row['order_id']}",
            closed_at_ms=NOW - 1,
            average_price=Decimal("9.9"),
            exit_price=Decimal("10.1"),
            evidence={"provider": "opentrade", "closed": True},
        )
    ]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: observed_now
        ).turn()
    )
    current = _order_row(conn, str(row["order_id"]))
    assert current["state"] == "MANUAL_REVIEW_REQUIRED"
    assert current["state_reason"] == "live_closed_time_invalid"
    assert current["position_closed_at_ms"] is None
    assert current["realized_bps"] is None


@pytest.mark.parametrize("control", ["PAUSED", "CLOSE_ONLY"])
def test_control_blocks_entry_but_never_blocks_a_live_safety_close(conn, control: str) -> None:
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    adapter.observations = [_live_position_observation(row)]
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )
    _repos(conn).trading.set_control(control=control, now_ms=NOW + 2_000)
    conn.commit()
    adapter.observations = [_live_position_observation(row, protected=False)]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 31_001
        ).turn()
    )
    assert adapter.close_quantities == [Decimal("1")]


@pytest.mark.parametrize("control", ["PAUSED", "CLOSE_ONLY"])
def test_control_change_during_repreflight_blocks_the_atomic_entry_claim(conn, control: str) -> None:
    class _ControlChangingAdapter(_LiveLifecycleAdapter):
        async def preflight(self, *, instrument: InstrumentRef, account_ref: str) -> LivePreflight:
            truth = await super().preflight(instrument=instrument, account_ref=account_ref)
            if self.preflight_calls > 1:
                _repos(conn).trading.set_control(control=control, now_ms=NOW + 1_000)
                conn.commit()
            return truth

    adapter = _ControlChangingAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )

    blocked = _order_row(conn, str(row["order_id"]))
    assert blocked["state"] == "APPROVED"
    assert int(blocked["provider_attempt_count"]) == 0
    assert adapter.submit_calls == 0


def test_blacklist_added_during_repreflight_blocks_the_atomic_entry_claim(conn) -> None:
    class _BlacklistingAdapter(_LiveLifecycleAdapter):
        async def preflight(self, *, instrument: InstrumentRef, account_ref: str) -> LivePreflight:
            truth = await super().preflight(instrument=instrument, account_ref=account_ref)
            if self.preflight_calls > 1:
                _repos(conn).trading.blacklist_upsert(
                    base_symbol="DOGE",
                    reason="operator",
                    expires_at_ms=None,
                    now_ms=NOW + 1_000,
                )
                conn.commit()
            return truth

    adapter = _BlacklistingAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter)
    _approve_live(conn, row)
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=config, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )

    blocked = _order_row(conn, str(row["order_id"]))
    assert blocked["state"] == "APPROVED"
    assert int(blocked["provider_attempt_count"]) == 0
    assert adapter.submit_calls == 0


@pytest.mark.parametrize("case_kind", ["oi_only", "news_only"])
@pytest.mark.parametrize("starting_state", ["PENDING", "RUNNING"])
def test_a_legacy_news_generation_case_is_blocked_before_model_or_order(
    conn, case_kind: str, starting_state: str
) -> None:
    manifest = _legacy_manifest(case_kind)
    trading = _repos(conn).trading
    assert trading.insert_case(
        case_id=f"legacy-{case_kind}-{starting_state}",
        underlying_key="crypto:DOGE",
        case_kind=case_kind,
        mode="paper",
        primary_source_key=f"legacy:{case_kind}:{starting_state}",
        supplemental_source_keys=(),
        manifest=manifest,
        manifest_sha256=canonical_sha256(manifest),
        regime="buildup_up",
        observed_at_ms=NOW,
        now_ms=NOW,
    )
    if starting_state == "RUNNING":
        conn.execute(
            "UPDATE trading_cases SET state = 'RUNNING', run_id = 'old-run', lease_expires_at_ms = %s",
            (NOW - 1,),
        )
    conn.commit()

    adapter = PaperAdapter()
    funnel = Funnel()
    result = asyncio.run(_runner(conn, adapter=adapter, now=NOW)._advance(funnel=funnel))

    case = trading.cases()[0]
    assert result == "news_generation_retired"
    assert case["state"] == "BLOCKED"
    assert case["policy_decision"] == "no_trade"
    assert case["policy_reason"] == "news_generation_retired"
    assert int(case["attempt_count"]) == 1
    assert funnel.as_dict() == {"advance_reject:news_generation_retired": 1}
    assert adapter.attempts == 0
    assert int(conn.execute("SELECT count(*) AS n FROM trading_orders").fetchone()["n"]) == 0


def test_two_frames_for_one_symbol_produce_at_most_one_active_order(conn) -> None:
    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - 2 * MINUTE)
    _seed_oi_event(conn, event_id="e2", symbol="DOGE", observed_at_ms=now - MINUTE)
    adapter = PaperAdapter()
    asyncio.run(_runner(conn, adapter=adapter, now=now).turn())
    asyncio.run(_runner(conn, adapter=adapter, now=now).turn())

    trading = _repos(conn).trading
    assert trading.active_underlyings() == ["crypto:DOGE"]
    orders = conn.execute("SELECT count(*) AS n FROM trading_orders").fetchone()
    assert int(orders["n"]) == 1


def test_a_blacklisted_underlying_never_reaches_an_order(conn) -> None:
    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="BTC", observed_at_ms=now - MINUTE)
    adapter = PaperAdapter()
    report = asyncio.run(_runner(conn, adapter=adapter, now=now).turn())
    assert report["created"] == 0
    assert adapter.attempts == 0
    assert conn.execute("SELECT count(*) AS n FROM trading_cases").fetchone()["n"] == 0
    # The rejection is named, and the day's funnel keeps it.
    funnel = _repos(conn).trading.runtime_state()["funnel"]
    assert any(key.startswith("oi_reject:blacklisted") for key in funnel)


def test_paused_stops_new_exposure_and_still_records_why(conn) -> None:
    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    _repos(conn).trading.set_control(control="PAUSED", now_ms=now)
    conn.commit()
    report = asyncio.run(_runner(conn, adapter=PaperAdapter(), now=now).turn())
    assert report["control"] == "PAUSED"
    assert conn.execute("SELECT count(*) AS n FROM trading_cases").fetchone()["n"] == 0


def test_an_ambiguous_attempt_is_resolved_by_reading_and_never_by_resending(conn) -> None:
    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    adapter = PaperAdapter(faults=PaperFaults(script=["ambiguous"]))
    asyncio.run(_runner(conn, adapter=adapter, now=now).turn())

    trading = _repos(conn).trading
    order = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert order["state"] == "AMBIGUOUS"
    assert int(order["provider_attempt_count"]) == 1
    # The underlying stays blocked while the answer is unknown.
    assert trading.active_underlyings() == ["crypto:DOGE"]

    reconcile = ReconcileRunner(
        db=_DirectDb(conn),
        config=_config(),
        bars=lambda _venue: None,
        adapter=adapter,
        clock=lambda: now + MINUTE,
    )
    asyncio.run(reconcile.turn())
    resolved = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert resolved["state"] == "OPEN"
    # Still one attempt: reconciliation reads, it never writes to the provider.
    assert int(resolved["provider_attempt_count"]) == 1
    assert adapter.attempts == 1


def test_an_attempt_that_never_landed_is_proven_absent_and_frees_the_underlying(conn) -> None:
    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    adapter = PaperAdapter(faults=PaperFaults(script=["ambiguous_lost"]))
    asyncio.run(_runner(conn, adapter=adapter, now=now).turn())

    reconcile = ReconcileRunner(
        db=_DirectDb(conn),
        config=_config(),
        bars=lambda _venue: None,
        adapter=adapter,
        clock=lambda: now + MINUTE,
    )
    asyncio.run(reconcile.turn())
    order = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert order["state"] == "REJECTED"
    assert order["state_reason"] == "proven_absent"
    assert _repos(conn).trading.active_underlyings() == []


def test_a_submitting_row_that_survived_a_restart_becomes_ambiguous_not_a_resend(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="PREPARED")
    trading = _repos(conn).trading
    trading.claim_attempt(order_id="o1", kind="entry", now_ms=NOW)
    conn.commit()

    adapter = PaperAdapter()
    reconcile = ReconcileRunner(
        db=_DirectDb(conn),
        config=_config(),
        bars=lambda _venue: None,
        adapter=adapter,
        clock=lambda: NOW + MINUTE,
    )
    asyncio.run(reconcile.turn())
    order = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert order["state"] == "AMBIGUOUS"
    assert order["state_reason"] == "entry_submitting_after_restart"
    assert adapter.attempts == 0


def test_the_frozen_row_records_the_venue_that_was_chosen_even_in_paper(conn) -> None:
    """`mode` says whether the write was real; `exchange_id` says where the intent was routed.

    Collapsing the two would erase the venue choice from the ledger, and the venue choice is the one
    thing the cross-venue invariant and the reconcile price read both need.
    """

    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    asyncio.run(_runner(conn, adapter=PaperAdapter(), now=now).turn())
    order = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert order["exchange_id"] == "binance"
    assert order["mode"] == "paper"
    assert order["payload"]["exchangeId"] == "binance"


def test_a_live_position_is_never_closed_from_a_simulated_exit(conn) -> None:
    """Fill, stop and close are provider facts. Candles are a local opinion about someone's account."""

    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="binance", state="OPEN")
    conn.execute(
        "UPDATE trading_orders SET mode = 'live_bounded', position_opened_at_ms = %s WHERE order_id = 'o1'",
        (NOW - 10_000_000,),
    )
    conn.commit()

    stopped_out = (Bar(open_at_ms=NOW - 600_000, close_at_ms=NOW - 300_000, close=Decimal("1")),)

    def bars(_venue: str) -> Any:
        async def fetch(_symbol: str, _start: int, _end: int) -> Any:
            return stopped_out

        return fetch

    reconcile = ReconcileRunner(
        db=_DirectDb(conn),
        config=_config(),
        bars=bars,
        adapter=PaperAdapter(),
        clock=lambda: NOW,
    )
    asyncio.run(reconcile.turn())
    order = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert order["state"] == "MANUAL_REVIEW_REQUIRED"
    assert order["state_reason"] == "exit_position_unknown"
    assert order["exit_reason"] is None


def test_the_clock_closes_an_open_position_without_news_or_a_model(conn) -> None:
    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    adapter = PaperAdapter()
    asyncio.run(_runner(conn, adapter=adapter, now=now).turn())

    later = now + 1_200_000
    flat = tuple(
        Bar(open_at_ms=now + i * 300_000, close_at_ms=now + (i + 1) * 300_000, close=Decimal("102")) for i in range(5)
    )

    def bars(_venue: str) -> Any:
        async def fetch(_symbol: str, _start: int, _end: int) -> Any:
            return flat

        return fetch

    reconcile = ReconcileRunner(
        db=_DirectDb(conn),
        config=_config(),
        bars=bars,
        adapter=adapter,
        clock=lambda: later,
    )
    asyncio.run(reconcile.turn())
    order = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert order["state"] == "CLOSED"
    assert order["exit_reason"] == "max_holding"
    # Both taker legs charged: entry and exit were both 102, so the receipt is the round-trip cost.
    assert int(order["realized_bps"]) == -10
    assert _repos(conn).trading.active_underlyings() == []


# ---------------------------------------------------------------------------- role privileges
def test_the_http_facing_role_can_only_read_the_trading_tables(conn) -> None:
    """`tracefold_serve` is reachable from the internet and the deny-list is a safety control.

    The one precedent for a serve write in this schema is append-only `INSERT` on `news_reviews`.
    Nothing here may give that role `UPDATE` or `DELETE`, least of all on the deny-list: a bug or an
    injection anywhere in the read path would then be able to erase the rule that keeps BTC/ETH/CL
    out of the order book.
    """

    rows = conn.execute(
        """
        SELECT table_name, privilege_type
          FROM information_schema.role_table_grants
         WHERE grantee = 'tracefold_serve' AND table_name LIKE 'trading\\_%'
        """
    ).fetchall()
    granted = {(str(row["table_name"]), str(row["privilege_type"])) for row in rows}
    assert granted, "serve must be able to read the trading tables"
    assert {privilege for _, privilege in granted} == {"SELECT"}


def test_the_workers_role_owns_every_trading_mutation(conn) -> None:
    rows = conn.execute(
        """
        SELECT table_name, privilege_type
          FROM information_schema.role_table_grants
         WHERE grantee = 'tracefold_workers' AND table_name LIKE 'trading\\_%'
        """
    ).fetchall()
    granted: dict[str, set[str]] = {}
    for row in rows:
        granted.setdefault(str(row["table_name"]), set()).add(str(row["privilege_type"]))
    assert granted["trading_symbol_blacklist"] >= {"SELECT", "INSERT", "UPDATE", "DELETE"}
    for table in ("trading_runtime_state", "trading_cases", "trading_orders", "trading_order_observations"):
        assert granted[table] >= {"SELECT", "INSERT", "UPDATE"}
        # Ledger rows are never deleted: an order that existed is audit, not garbage.
        assert "DELETE" not in granted[table]


def test_a_read_only_transaction_cannot_touch_the_deny_list(conn) -> None:
    """The failure the CLI would have hit in production, made explicit.

    `tracefold_serve` carries `default_transaction_read_only = on`, so routing an operator mutation
    through it does not merely over-grant — it does not work at all.
    """

    conn.execute("SET default_transaction_read_only = on")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("DELETE FROM trading_symbol_blacklist WHERE base_symbol = 'BTC'")
    conn.rollback()
    conn.execute("SET default_transaction_read_only = off")
    conn.commit()


# ---------------------------------------------------------------------------- count caps and the day roll
def test_a_single_turn_cannot_place_more_orders_than_the_daily_cap(conn) -> None:
    """`_plan` reads the cap once per turn; the cap has to be counted where it is spent.

    Four eligible symbols in one turn used to become four orders under a daily cap of one, because
    the only per-order authority was the partial unique index, and that is keyed on the underlying.
    """

    now = NOW
    for index, symbol in enumerate(("DOGE", "PENGU", "FET", "UNI")):
        _seed_oi_event(conn, event_id=f"e{index}", symbol=symbol, observed_at_ms=now - MINUTE)
    config = _config(order=OrderPolicy(max_holding_ms=900_000, max_orders_per_day=1, max_open_underlyings=4))
    asyncio.run(_runner(conn, adapter=PaperAdapter(), now=now, config=config).turn())
    assert int(conn.execute("SELECT count(*) AS n FROM trading_orders").fetchone()["n"]) == 1


def test_a_single_turn_cannot_exceed_the_open_underlying_cap(conn) -> None:
    now = NOW
    for index, symbol in enumerate(("DOGE", "PENGU", "FET", "UNI")):
        _seed_oi_event(conn, event_id=f"e{index}", symbol=symbol, observed_at_ms=now - MINUTE)
    config = _config(order=OrderPolicy(max_holding_ms=900_000, max_orders_per_day=10, max_open_underlyings=2))
    asyncio.run(_runner(conn, adapter=PaperAdapter(), now=now, config=config).turn())
    assert int(conn.execute("SELECT count(*) AS n FROM trading_orders").fetchone()["n"]) == 2


def test_the_day_roll_clears_every_per_day_counter_whichever_one_runs_first(conn) -> None:
    """Each counter used to roll only its own field, so the first writer of a new UTC day stamped the
    new key and left the others holding yesterday's numbers under it."""

    trading = _repos(conn).trading
    trading.bump_orders_today(day_key="2026-08-22", now_ms=NOW)
    trading.bump_dspy_calls(day_key="2026-08-22", now_ms=NOW)
    trading.merge_funnel(day_key="2026-08-22", counts={"oi_eligible": 5}, now_ms=NOW)
    conn.commit()
    assert trading.dspy_calls_today(day_key="2026-08-22") == 1

    # The new day's first writer is the order counter, not the model counter.
    trading.bump_orders_today(day_key="2026-08-23", now_ms=NOW)
    conn.commit()
    state = trading.runtime_state()
    assert state is not None
    assert state["day_key"] == "2026-08-23"
    assert int(state["orders_today"]) == 1
    assert int(state["dspy_calls_today"]) == 0
    assert state["funnel"] == {}
    assert trading.dspy_calls_today(day_key="2026-08-23") == 0


def test_the_frozen_mark_is_the_price_at_the_cutoff_not_the_freshest_bar(conn) -> None:
    """A manifest stamped `cutoff_ms` must not carry a price from after the cutoff.

    The bar feed is fetched a little past the trigger so the anchor itself resolves; taking the last
    close would put post-cutoff evidence into the document the model reads and the entry references.
    """

    now = NOW
    trigger = now - 10 * MINUTE
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=trigger)

    at_cutoff = Decimal("102")
    after_cutoff = Decimal("140")
    bars = [*_regime_bars(trigger)]
    bars.append(Bar(open_at_ms=trigger, close_at_ms=trigger + 300_000, close=after_cutoff))

    async def feed_result(_symbol: str, _start: int, _end: int) -> Any:
        return tuple(bars)

    runner = CandidateRunner(
        db=_DirectDb(conn),
        config=_config(),
        bars=lambda _venue: feed_result,
        adapter=PaperAdapter(),
        candidate_projection=news_trade_candidates,
        instrument_projection=news_trade_instruments,
        program=None,
        clock=lambda: now,
    )
    asyncio.run(runner.turn())
    order = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert order is not None
    assert Decimal(str(order["entry_reference"])) == at_cutoff


# ---------------------------------------------------------------------------- review regressions
def test_a_rejected_close_never_books_a_fabricated_result(conn) -> None:
    """The venue refused the close, so the position is still open.

    Writing CLOSED here booked a `realized_bps` computed off a candle nobody traded at, and freed the
    underlying for a second order stacked on top of a live position.
    """

    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    adapter = PaperAdapter()
    asyncio.run(_runner(conn, adapter=adapter, now=now).turn())

    adapter.faults = PaperFaults(script=["reject"])
    later = now + 1_200_000
    flat = tuple(
        Bar(open_at_ms=now + i * 300_000, close_at_ms=now + (i + 1) * 300_000, close=Decimal("102")) for i in range(5)
    )

    async def feed(_symbol: str, _start: int, _end: int) -> Any:
        return flat

    reconcile = ReconcileRunner(
        db=_DirectDb(conn), config=_config(), bars=lambda _v: feed, adapter=adapter, clock=lambda: later
    )
    asyncio.run(reconcile.turn())
    order = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert order["state"] == "MANUAL_REVIEW_REQUIRED"
    assert order["realized_bps"] is None
    assert order["position_closed_at_ms"] is None
    # Unknown exposure keeps the underlying blocked.
    assert _repos(conn).trading.active_underlyings() == ["crypto:DOGE"]


def test_the_exit_gets_its_own_one_attempt_guard(conn) -> None:
    """The entry has already spent `provider_attempt_count` by the time a position can close."""

    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="PREPARED")
    trading = _repos(conn).trading
    assert trading.claim_attempt(order_id="o1", kind="entry", now_ms=NOW) == "claimed"
    conn.commit()
    # The entry has now spent its counter; the position opens and later has to be closed.
    conn.execute("UPDATE trading_orders SET state = 'OPEN' WHERE order_id = 'o1'")
    conn.commit()

    assert trading.claim_attempt(order_id="o1", kind="exit", now_ms=NOW) == "claimed"
    conn.commit()
    assert trading.claim_attempt(order_id="o1", kind="exit", now_ms=NOW) in ("already_spent", "exhausted")
    conn.commit()
    row = trading.order(order_id="o1")
    assert (int(row["provider_attempt_count"]), int(row["exit_attempt_count"])) == (1, 1)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE trading_orders SET exit_attempt_count = 2 WHERE order_id = 'o1'")
    conn.rollback()


def test_an_ambiguous_exit_is_never_read_as_the_entry_never_landing(conn) -> None:
    """A close that filled with its answer lost is a completed round trip, not a phantom order."""

    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="AMBIGUOUS")
    conn.execute("UPDATE trading_orders SET state_reason = 'exit_close_ambiguous' WHERE order_id = 'o1'")
    conn.commit()

    # The process-local paper book is gone after restart, so absence cannot be proven.
    reconcile = ReconcileRunner(
        db=_DirectDb(conn), config=_config(), bars=lambda _v: None, adapter=PaperAdapter(), clock=lambda: NOW
    )
    asyncio.run(reconcile.turn())
    order = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert order["state"] == "MANUAL_REVIEW_REQUIRED"
    assert order["state_reason"] == "exit_ambiguous_position_unknown"


def test_an_observation_that_is_not_a_live_order_is_not_adopted_as_open(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="AMBIGUOUS")
    order = _order_row(conn, "o1")

    class _RejectingAdapter(PaperAdapter):
        async def observe(self, _order: Any) -> Any:
            from tracefold.trading.contracts import ExecutionReceipt

            return ExecutionReceipt(state="REJECTED", reason="venue_rejected")

    reconcile = ReconcileRunner(
        db=_DirectDb(conn), config=_config(), bars=lambda _v: None, adapter=_RejectingAdapter(), clock=lambda: NOW
    )
    asyncio.run(reconcile.turn())
    assert _order_row(conn, "o1")["state"] == "MANUAL_REVIEW_REQUIRED"
    del order


def test_a_prepared_intent_that_never_reached_the_network_stops_holding_the_slot(conn) -> None:
    """`provider_attempt_count = 0` proves no write left, so expiring it is provably harmless."""

    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="PREPARED")
    later = NOW + 3_600_000
    reconcile = ReconcileRunner(
        db=_DirectDb(conn), config=_config(), bars=lambda _v: None, adapter=PaperAdapter(), clock=lambda: later
    )
    asyncio.run(reconcile.turn())
    row = _order_row(conn, "o1")
    assert row["state"] == "REJECTED"
    assert row["state_reason"] == "prepared_expired_never_submitted"
    assert _repos(conn).trading.active_underlyings() == []


def test_close_only_and_paused_both_refuse_to_submit_an_approved_entry(conn) -> None:
    """The reconciler reading the control state at all is the fix; before this neither reached it."""

    for control in ("PAUSED", "CLOSE_ONLY"):
        _reset(conn)
        _repos(conn).trading.set_control(control=control, now_ms=NOW)
        conn.commit()
        _case(conn, case_id="c1", source_key="k1")
        _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="APPROVED")
        adapter = PaperAdapter()
        reconcile = ReconcileRunner(
            db=_DirectDb(conn), config=_config(), bars=lambda _v: None, adapter=adapter, clock=lambda: NOW
        )
        asyncio.run(reconcile.turn())
        assert adapter.attempts == 0, control
        assert _order_row(conn, "o1")["state"] == "APPROVED", control


def test_an_approved_order_is_submitted_once_when_the_lane_is_running(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="APPROVED")
    conn.execute("UPDATE trading_orders SET must_close_at_ms = NULL WHERE order_id = 'o1'")
    conn.commit()
    adapter = PaperAdapter()
    reconcile = ReconcileRunner(
        db=_DirectDb(conn), config=_config(), bars=lambda _v: None, adapter=adapter, clock=lambda: NOW
    )
    asyncio.run(reconcile.turn())
    row = _order_row(conn, "o1")
    assert row["state"] == "ACKNOWLEDGED"
    assert row["position_opened_at_ms"] is None
    assert row["must_close_at_ms"] is None
    assert int(row["provider_attempt_count"]) == 1
    assert adapter.attempts == 1


def test_only_a_real_exit_imposes_the_cooldown_and_enters_the_pnl_denominator(conn) -> None:
    """Four paths write `closed_at_ms`; only one of them is a position closing."""

    trading = _repos(conn).trading
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="PREPARED")
    trading.update_order(order_id="o1", state="REJECTED", closed_at_ms=NOW, now_ms=NOW)
    conn.commit()

    assert trading.last_close_at_ms(underlying_key="crypto:DOGE") is None
    counts = trading.status_counts(since_ms=NOW - 86_400_000)
    assert counts["closed_orders"] == 0

    _case(conn, case_id="c2", source_key="k2", underlying="crypto:SOL")
    _order(conn, order_id="o2", case_id="c2", underlying="crypto:SOL", exchange_id="paper", state="OPEN")
    trading.update_order(
        order_id="o2",
        state="CLOSED",
        realized_bps=150,
        position_closed_at_ms=NOW,
        closed_at_ms=NOW,
        now_ms=NOW,
    )
    conn.commit()
    assert trading.last_close_at_ms(underlying_key="crypto:SOL") == NOW
    counts = trading.status_counts(since_ms=NOW - 86_400_000)
    assert (counts["closed_orders"], counts["closed_realized_bps"]) == (1, 150)


def test_a_deferral_cannot_write_back_a_state_the_commit_path_has_advanced(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="PREPARED")
    trading = _repos(conn).trading
    trading.update_order(order_id="o1", state="ACKNOWLEDGED", now_ms=NOW)
    conn.commit()
    # The reconciler read PREPARED at the top of the batch; the row has since advanced.
    assert (
        trading.reschedule_order(order_id="o1", expected_state="PREPARED", next_reconcile_at_ms=NOW + 1, now_ms=NOW)
        is False
    )
    conn.commit()
    assert _order_row(conn, "o1")["state"] == "ACKNOWLEDGED"


def test_a_blacklist_entry_added_after_the_freeze_still_stops_the_order(conn) -> None:
    """The only per-symbol operator lever has to reach work that is already planned."""

    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    runner = _runner(conn, adapter=PaperAdapter(), now=now)
    # Freeze the case, then deny the symbol before the case is advanced.
    state = asyncio.run(runner._read_state(now))
    assert state is not None
    plans = runner._plan(state, funnel=Funnel(), now=now)
    assert asyncio.run(runner._freeze(plans[0], funnel=Funnel(), now=now)) is True
    _repos(conn).trading.blacklist_upsert(base_symbol="DOGE", reason="operator", expires_at_ms=None, now_ms=now)
    conn.commit()

    asyncio.run(runner._advance(funnel=Funnel()))
    assert int(conn.execute("SELECT count(*) AS n FROM trading_orders").fetchone()["n"]) == 0


def test_a_case_that_sat_past_its_freshness_window_is_blocked_not_traded(conn) -> None:
    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    runner = _runner(conn, adapter=PaperAdapter(), now=now)
    state = asyncio.run(runner._read_state(now))
    assert state is not None
    plans = runner._plan(state, funnel=Funnel(), now=now)
    assert asyncio.run(runner._freeze(plans[0], funnel=Funnel(), now=now)) is True

    # Resume hours later: the frozen mark is stale, so the case must not become an order.
    stale_runner = _runner(conn, adapter=PaperAdapter(), now=now + 10 * 3_600_000)
    asyncio.run(stale_runner._advance(funnel=Funnel()))
    case = _repos(conn).trading.cases()[0]
    assert case["state"] == "BLOCKED"
    assert case["policy_reason"] == "case_stale"
    assert int(conn.execute("SELECT count(*) AS n FROM trading_orders").fetchone()["n"]) == 0


def test_the_instrument_resolution_is_deterministic_across_identical_manifests(conn) -> None:
    """`binance.perp` is snapshotted with no quote filter, so several rows match one base symbol."""

    for symbol in ("DOGEUSDC", "DOGEUSDT", "DOGEUSDT_260327"):
        conn.execute(
            "INSERT INTO news_market_instruments (venue, venue_symbol, base_symbol, instrument_class, "
            "quote_asset, status, last_seen_ms) VALUES ('binance.perp', %s, 'DOGE', 'crypto', %s, 'trading', %s)",
            (symbol, "USDC" if symbol.endswith("USDC") else "USDT", NOW),
        )
    conn.commit()
    rows = [
        _repos(conn).news.trade_candidate_instrument(base_symbol="DOGE", venues=("binance.perp",)) for _ in range(5)
    ]
    assert all(page[0]["venue_symbol"] == rows[0][0]["venue_symbol"] for page in rows)
    assert rows[0][0]["venue_symbol"] == "DOGEUSDT"


# ---------------------------------------------------------------------------- second-pass regressions
def test_an_ambiguous_exit_can_be_retried_once_a_read_proves_the_position_is_still_open(conn) -> None:
    """The entry's contract applied to the exit made an ambiguous close unrecoverable.

    One attempt is right for an entry — a resend doubles the position. For an exit it meant the
    position could never be closed: the claim was spent, `_one_attempt` returned None, nothing was
    written, and the row hot-looped every turn holding its slot.
    """

    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="OPEN")
    trading = _repos(conn).trading
    assert trading.claim_attempt(order_id="o1", kind="exit", now_ms=NOW) == "claimed"
    conn.commit()
    assert trading.claim_attempt(order_id="o1", kind="exit", now_ms=NOW) in ("already_spent", "exhausted")

    # A read proves the close did not take effect, so re-issuing it cannot double-close. This is what
    # `_resolve_ambiguity._adopt` does: put the row back to OPEN and re-arm the exit together.
    trading.update_order(order_id="o1", state="OPEN", now_ms=NOW)
    assert trading.release_exit_attempt(order_id="o1", now_ms=NOW) is True
    conn.commit()
    assert trading.claim_attempt(order_id="o1", kind="exit", now_ms=NOW) == "claimed"
    conn.commit()
    assert int(trading.order(order_id="o1")["exit_attempt_total"]) == 2


def test_the_exit_retry_is_bounded(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="OPEN")
    trading = _repos(conn).trading
    for _ in range(3):
        assert trading.claim_attempt(order_id="o1", kind="exit", now_ms=NOW) == "claimed"
        conn.commit()
        conn.execute("UPDATE trading_orders SET state = 'OPEN' WHERE order_id = 'o1'")
        trading.release_exit_attempt(order_id="o1", now_ms=NOW)
        conn.commit()
    # Three total attempts is the ceiling; the release cannot lift it.
    assert trading.claim_attempt(order_id="o1", kind="exit", now_ms=NOW) in ("already_spent", "exhausted")
    conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE trading_orders SET exit_attempt_total = 4 WHERE order_id = 'o1'")
    conn.rollback()


def test_a_safety_closing_row_that_survived_a_restart_becomes_ambiguous(conn) -> None:
    """The exit's analogue of the `SUBMITTING` orphan; without it the catch-all deferred it forever."""

    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="OPEN")
    _repos(conn).trading.claim_attempt(order_id="o1", kind="exit", now_ms=NOW)
    conn.commit()
    assert _order_row(conn, "o1")["state"] == "SAFETY_CLOSING"

    adapter = PaperAdapter()
    reconcile = ReconcileRunner(
        db=_DirectDb(conn), config=_config(), bars=lambda _v: None, adapter=adapter, clock=lambda: NOW + MINUTE
    )
    asyncio.run(reconcile.turn())
    row = _order_row(conn, "o1")
    assert row["state"] == "AMBIGUOUS"
    assert row["state_reason"] == "exit_safety_closing_after_restart"
    assert adapter.attempts == 0


def test_a_deferral_pushes_the_backoff_out_for_an_acknowledged_row(conn) -> None:
    """`_defer` used to pass an aspirational state, so the guarded update matched nothing.

    The consequence was silent: the 30 s backoff never applied and every paper position was
    re-fetched and re-evaluated on every single turn between ack and close.
    """

    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    adapter = PaperAdapter()
    asyncio.run(_runner(conn, adapter=adapter, now=now).turn())
    assert _order_row(conn, conn.execute("SELECT order_id FROM trading_orders").fetchone()["order_id"])

    async def feed(_symbol: str, _start: int, _end: int) -> Any:
        return (Bar(open_at_ms=now, close_at_ms=now + 300_000, close=Decimal("102")),)

    reconcile = ReconcileRunner(
        db=_DirectDb(conn), config=_config(), bars=lambda _v: feed, adapter=adapter, clock=lambda: now + 60_000
    )
    asyncio.run(reconcile.turn())
    row = conn.execute("SELECT * FROM trading_orders").fetchone()
    # The acknowledged row is promoted to OPEN on its first managed turn, then backed off.
    assert row["state"] == "OPEN"
    assert int(row["next_reconcile_at_ms"]) > now + 60_000


def test_a_case_is_not_blocked_by_the_freshness_its_own_eligibility_already_spent(conn) -> None:
    """`case_stale` measured from the trigger discarded a signal for queueing behind another case."""

    now = NOW
    trigger = now - 280_000  # inside `max_age_ms`, with only 20 s of it left
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=trigger)
    runner = _runner(conn, adapter=PaperAdapter(), now=now)
    state = asyncio.run(runner._read_state(now))
    assert state is not None
    plans = runner._plan(state, funnel=Funnel(), now=now)
    assert asyncio.run(runner._freeze(plans[0], funnel=Funnel(), now=now)) is True

    # 30 s later — a plausible model deadline — the case must still be decidable.
    later_runner = _runner(conn, adapter=PaperAdapter(), now=now + 30_000)
    asyncio.run(later_runner._advance(funnel=Funnel()))
    case = _repos(conn).trading.cases()[0]
    assert case["state"] != "BLOCKED"
    assert case["policy_reason"] != "case_stale"


def test_a_venue_rejection_does_not_spend_the_daily_loss_envelope(conn) -> None:
    """The cap is documented as `notional x stop_bps x orders_per_day` — a loss envelope.

    A rejected order provably created no exposure, so four bad symbols must not exhaust the day.
    """

    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    asyncio.run(_runner(conn, adapter=PaperAdapter(faults=PaperFaults(script=["reject"])), now=now).turn())
    assert (
        _order_row(conn, conn.execute("SELECT order_id FROM trading_orders").fetchone()["order_id"])["state"]
        == "REJECTED"
    )
    assert _repos(conn).trading.orders_today(day_key=_day_key_for(now)) == 0


def test_the_operator_can_drain_a_manual_review_order(conn) -> None:
    """Five paths escalate there and it sits in the active index; without a drain the lane wedges."""

    trading = _repos(conn).trading
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="OPEN")
    trading.update_order(order_id="o1", state="MANUAL_REVIEW_REQUIRED", now_ms=NOW)
    conn.commit()
    assert trading.active_underlyings() == ["crypto:DOGE"]

    assert trading.resolve_manual_review(order_id="o1", outcome="closed", reason="flat_at_venue", now_ms=NOW) is True
    conn.commit()
    row = _order_row(conn, "o1")
    assert row["state"] == "CLOSED"
    assert row["state_reason"] == "operator_resolved:flat_at_venue"
    assert trading.active_underlyings() == []


def test_draining_a_manual_review_order_back_to_open_re_arms_its_exit(conn) -> None:
    trading = _repos(conn).trading
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="OPEN")
    trading.claim_attempt(order_id="o1", kind="exit", now_ms=NOW)
    trading.update_order(order_id="o1", state="MANUAL_REVIEW_REQUIRED", now_ms=NOW)
    conn.commit()

    assert trading.resolve_manual_review(order_id="o1", outcome="open", reason="still_open", now_ms=NOW) is True
    conn.commit()
    row = _order_row(conn, "o1")
    assert row["state"] == "OPEN"
    assert int(row["exit_attempt_count"]) == 0


def test_the_operator_drain_lifts_the_exit_ceiling_the_automated_release_cannot(conn) -> None:
    """The ceiling stops an unattended loop; a human who checked the venue is the actor it defers to.

    Without resetting `exit_attempt_total`, `resolve <id> open` after exhaustion put the row straight
    back into MANUAL_REVIEW_REQUIRED on the next turn with no explanation, and the only escape left
    was asserting `closed` about a position that is demonstrably still open.
    """

    trading = _repos(conn).trading
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="OPEN")
    for _ in range(3):
        assert trading.claim_attempt(order_id="o1", kind="exit", now_ms=NOW) == "claimed"
        trading.update_order(order_id="o1", state="OPEN", now_ms=NOW)
        trading.release_exit_attempt(order_id="o1", now_ms=NOW)
        conn.commit()
    assert trading.claim_attempt(order_id="o1", kind="exit", now_ms=NOW) == "exhausted"
    conn.commit()

    trading.update_order(order_id="o1", state="MANUAL_REVIEW_REQUIRED", now_ms=NOW)
    conn.commit()
    assert trading.resolve_manual_review(order_id="o1", outcome="open", reason="still_open", now_ms=NOW) is True
    conn.commit()
    row = _order_row(conn, "o1")
    assert (int(row["exit_attempt_count"]), int(row["exit_attempt_total"])) == (0, 0)
    assert trading.claim_attempt(order_id="o1", kind="exit", now_ms=NOW) == "claimed"


def test_an_operator_resolved_close_cools_the_symbol_but_is_not_a_measured_result(conn) -> None:
    trading = _repos(conn).trading
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="OPEN")
    trading.update_order(
        order_id="o1",
        state="MANUAL_REVIEW_REQUIRED",
        position_opened_at_ms=NOW - MINUTE,
        now_ms=NOW,
    )
    trading.resolve_manual_review(order_id="o1", outcome="closed", reason="flat", now_ms=NOW)
    conn.commit()

    # The cooldown applies: a position was open and is now confirmed flat.
    assert trading.last_close_at_ms(underlying_key="crypto:DOGE") == NOW
    # The PnL denominator does not: nobody computed a return for it.
    counts = trading.status_counts(since_ms=NOW - 86_400_000)
    assert (counts["closed_orders"], counts["closed_realized_bps"]) == (0, 0)


def test_an_approved_order_respects_the_daily_cap_the_insert_path_enforces(conn) -> None:
    """The reconciler is a second entry into `commit_order`; the caps live where the order is spent."""

    trading = _repos(conn).trading
    trading.bump_orders_today(day_key=_day_key_for(NOW), now_ms=NOW)
    conn.commit()
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="APPROVED")

    adapter = PaperAdapter()
    config = _config(order=OrderPolicy(max_holding_ms=900_000, max_orders_per_day=1, max_open_underlyings=4))
    reconcile = ReconcileRunner(
        db=_DirectDb(conn), config=config, bars=lambda _v: None, adapter=adapter, clock=lambda: NOW
    )
    asyncio.run(reconcile.turn())
    assert adapter.attempts == 0
    assert _order_row(conn, "o1")["state"] == "APPROVED"


def test_a_live_paper_position_is_reported_as_open_not_acknowledged(conn) -> None:
    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    adapter = PaperAdapter()
    asyncio.run(_runner(conn, adapter=adapter, now=now).turn())
    submitted = conn.execute("SELECT state, filled_quantity, average_price FROM trading_orders").fetchone()
    assert submitted["state"] == "ACKNOWLEDGED"
    assert submitted["filled_quantity"] is None
    assert submitted["average_price"] is None

    async def feed(_symbol: str, _start: int, _end: int) -> Any:
        return (Bar(open_at_ms=now, close_at_ms=now + 300_000, close=Decimal("102")),)

    reconcile = ReconcileRunner(
        db=_DirectDb(conn), config=_config(), bars=lambda _v: feed, adapter=adapter, clock=lambda: now + 400_000
    )
    asyncio.run(reconcile.turn())
    row = conn.execute(
        "SELECT state, filled_quantity, average_price, position_opened_at_ms, must_close_at_ms FROM trading_orders"
    ).fetchone()
    assert row["state"] == "OPEN"
    assert row["filled_quantity"] == Decimal("0.4901")
    assert row["average_price"] == Decimal("102")
    assert row["position_opened_at_ms"] == now
    assert row["must_close_at_ms"] == now + _config().order.max_holding_ms


# ------------------------------------------------------- #209 paper exit acceptance on real PostgreSQL
# Every test below is named `test_paper_exit_acceptance_*` because `make trading-smoke` selects them by
# that prefix. It is the acceptance contract for the three paper exits, not an incidental grouping:
# renaming one silently drops it out of the smoke lane.
class _CountedPaperAdapter(PaperAdapter):
    """Paper, with the two capital writes counted apart.

    `PaperAdapter.attempts` is one number for both legs, and the acceptance contract below is about
    each leg separately: a resent entry doubles a position and a resent close double-closes it. One
    shared counter cannot tell a passing run from either failure.
    """

    def __init__(self, *, faults: PaperFaults | None = None) -> None:
        super().__init__(faults=faults or PaperFaults())
        self.submits = 0
        self.closes = 0

    async def submit(self, order: PreparedOrder) -> ExecutionReceipt:
        self.submits += 1
        return await super().submit(order)

    async def close(self, order: PreparedOrder, *, quantity: Decimal) -> ExecutionReceipt:
        self.closes += 1
        return await super().close(order, quantity=quantity)


def _exit_bars(*, opened_at_ms: int, closes: Sequence[str]) -> tuple[Bar, ...]:
    """Consecutive closed five-minute bars from the instant the position opened."""

    return tuple(
        Bar(
            open_at_ms=opened_at_ms + index * 300_000,
            close_at_ms=opened_at_ms + (index + 1) * 300_000,
            close=Decimal(value),
        )
        for index, value in enumerate(closes)
    )


def _drive_paper_exit(
    conn: Any,
    *,
    order_policy: OrderPolicy,
    closes: Sequence[str],
    reconcile_at_ms: int,
) -> _CountedPaperAdapter:
    """One qualifying OI frame through the whole paper loop, on the real ledger.

    `BUY -> ACK -> OPEN -> exit -> close/SELL -> CLOSED`, driven by the two real runners over the two
    real tables. Promotion and the exit land in the same reconcile turn because that is what the
    runner does when an acknowledged order already has closed bars behind it; the ledger, not the
    turn count, is what the assertions read.
    """

    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=NOW - MINUTE)
    adapter = _CountedPaperAdapter()
    config = _config(order=order_policy)
    asyncio.run(_runner(conn, adapter=adapter, now=NOW, config=config).turn())
    assert _order_row_only(conn)["state"] == "ACKNOWLEDGED"

    bars = _exit_bars(opened_at_ms=NOW, closes=closes)

    async def feed(_symbol: str, _start: int, _end: int) -> Any:
        return bars

    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=config,
            bars=lambda _venue: feed,
            adapter=adapter,
            clock=lambda: reconcile_at_ms,
        ).turn()
    )
    return adapter


def _order_row_only(conn: Any) -> Any:
    return conn.execute("SELECT * FROM trading_orders").fetchone()


def _assert_closed_and_flat(
    conn: Any,
    adapter: _CountedPaperAdapter,
    *,
    exit_reason: str,
    exit_price: str,
    realized_bps: int,
    closed_at_ms: int,
) -> None:
    """The whole #209 happy-path contract, asserted the same way for all three exits."""

    row = _order_row_only(conn)
    assert (adapter.submits, adapter.closes) == (1, 1)
    assert row["state"] == "CLOSED"
    assert row["exit_reason"] == exit_reason
    assert row["exit_price"] == Decimal(exit_price)
    assert int(row["realized_bps"]) == realized_bps
    assert int(row["position_closed_at_ms"]) == closed_at_ms
    # The ledger's own record of how many times each leg was written, independent of the adapter.
    assert (int(row["provider_attempt_count"]), int(row["exit_attempt_total"])) == (1, 1)

    states = {str(item["state"]) for item in conn.execute("SELECT state FROM trading_orders").fetchall()}
    assert states == {"CLOSED"}
    # Flat at the *venue*, not merely terminal in the ledger. `active_underlyings` and `due_orders`
    # both filter on `ACTIVE_ORDER_STATES` and `CLOSED` is terminal, so asserting them here would only
    # restate the line above; the simulated book is the one thing that can disagree with it. A close
    # that acknowledged without taking effect leaves the position in `remote`.
    assert adapter.remote == {}


def test_paper_exit_acceptance_stop_loss_reaches_closed_and_flat(conn) -> None:
    """`fixed_stop_bps=200` on a 102 entry stops at 99.96; the first bar closes through it."""

    adapter = _drive_paper_exit(
        conn,
        order_policy=OrderPolicy(max_holding_ms=900_000),
        closes=["99"],
        reconcile_at_ms=NOW + 300_000,
    )
    # (99/102 - 1) * 10_000 = -294.1 bps gross, minus both taker legs at the frozen 5 bps.
    _assert_closed_and_flat(
        conn,
        adapter,
        exit_reason="stop_loss",
        exit_price="99",
        realized_bps=-304,
        closed_at_ms=NOW + 300_000,
    )


def test_paper_exit_acceptance_take_profit_reaches_closed_and_flat_when_enabled(conn) -> None:
    """The default `take_profit_bps=0` disables this exit, so the case has to enable it explicitly.

    A default paper run proves the stop and the clock. Reporting it as a take-profit proof would be
    describing a branch the deployed configuration never enters.
    """

    adapter = _drive_paper_exit(
        conn,
        order_policy=OrderPolicy(max_holding_ms=900_000, take_profit_bps=400),
        closes=["107"],
        reconcile_at_ms=NOW + 300_000,
    )
    # 102 * 1.04 = 106.08 take-profit; the bar closes above it. +490.2 bps gross, minus both legs.
    _assert_closed_and_flat(
        conn,
        adapter,
        exit_reason="take_profit",
        exit_price="107",
        realized_bps=480,
        closed_at_ms=NOW + 300_000,
    )


def test_paper_exit_acceptance_max_holding_reaches_closed_and_flat(conn) -> None:
    """Flat bars: no stop, no take-profit, and the frozen deadline is what ends the position."""

    adapter = _drive_paper_exit(
        conn,
        order_policy=OrderPolicy(max_holding_ms=600_000, take_profit_bps=400),
        closes=["102", "102"],
        reconcile_at_ms=NOW + 600_000,
    )
    _assert_closed_and_flat(
        conn,
        adapter,
        exit_reason="max_holding",
        exit_price="102",
        realized_bps=-10,
        closed_at_ms=NOW + 600_000,
    )


def test_paper_exit_acceptance_deadline_survives_a_restart_under_a_new_configuration(conn) -> None:
    """#209: the order row, not the running configuration, owns the exit semantics.

    `must_close_at_ms` is first written when reconciliation promotes an acknowledged order to OPEN.
    That promotion can be a restart and a redeploy after the intent was approved, so before the
    snapshot the deadline of an already-approved order was whatever `max_holding_seconds` said at
    promotion time. The realised return had the same problem through the taker fee.
    """

    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=NOW - MINUTE)
    approved_policy = OrderPolicy(max_holding_ms=900_000)
    asyncio.run(_runner(conn, adapter=PaperAdapter(), now=NOW, config=_config(order=approved_policy)).turn())
    prepared = _order_row_only(conn)
    assert prepared["state"] == "ACKNOWLEDGED"
    assert prepared["must_close_at_ms"] is None
    assert int(prepared["max_holding_ms"]) == 900_000
    assert int(prepared["taker_fee_bps"]) == 5

    # The process dies and comes back under a configuration that would both shorten the hold and
    # charge fifty times the fee.
    restarted = _config(order=OrderPolicy(max_holding_ms=60_000, taker_fee_bps=250))
    bars = _exit_bars(opened_at_ms=NOW, closes=["99"])

    async def feed(_symbol: str, _start: int, _end: int) -> Any:
        return bars

    adapter = _CountedPaperAdapter()
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=restarted,
            bars=lambda _venue: feed,
            adapter=adapter,
            clock=lambda: NOW + 300_000,
        ).turn()
    )

    row = _order_row_only(conn)
    assert int(row["must_close_at_ms"]) == NOW + 900_000
    assert row["exit_reason"] == "stop_loss"
    assert int(row["realized_bps"]) == -304
    assert (adapter.submits, adapter.closes) == (0, 1)


@pytest.mark.parametrize("state", ["ACKNOWLEDGED", "APPROVED", "OPEN"])
def test_paper_exit_acceptance_refuses_a_pre_snapshot_order_instead_of_re_governing_it(conn, state: str) -> None:
    """The one legacy shape the migration cannot answer for, and what happens to it.

    An active order that never opened a position has no provable holding budget — nothing records what
    `max_holding_seconds` was when it was approved. Reading today's configuration for it is exactly
    the drift #209 closes, so the reconciler refuses to manage it and uses the drain every other
    unprovable outcome uses. Nothing about the row is rewritten on the way there: an `APPROVED` row's
    `updated_at_ms` is its durable approval instant, and a snapshot write that moved it could carry a
    live entry past its own approval TTL.
    """

    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="binance", state=state)
    conn.execute(
        "UPDATE trading_orders SET max_holding_ms = NULL, taker_fee_bps = NULL, must_close_at_ms = NULL, "
        "remote_order_id = 'paper-o1', provider_attempt_count = 1 WHERE order_id = 'o1'"
    )
    conn.commit()

    adapter = _CountedPaperAdapter()
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn),
            config=_config(order=OrderPolicy(max_holding_ms=1_200_000, taker_fee_bps=7)),
            bars=lambda _venue: None,
            adapter=adapter,
            clock=lambda: NOW + MINUTE,
        ).turn()
    )

    row = _order_row_only(conn)
    assert row["state"] == "MANUAL_REVIEW_REQUIRED"
    assert row["state_reason"] == "legacy_execution_snapshot_missing"
    # Refused, not re-governed: no configuration reached the row and no exit was computed from one.
    assert (row["max_holding_ms"], row["taker_fee_bps"], row["must_close_at_ms"]) == (None, None, None)
    assert (adapter.submits, adapter.closes) == (0, 0)
    # It keeps the underlying blocked, and `tracefold trading resolve` is the operator's way out.
    assert _repos(conn).trading.active_underlyings() == ["crypto:DOGE"]


def test_a_live_order_keeps_its_own_deadline_and_fee_across_a_redeploy(conn) -> None:
    """#209 on the lane where real funds move, with the row and the configuration deliberately apart.

    Every other live test runs with `_order()`'s snapshot equal to `_config()`'s policy, so the two
    switched call sites on the live path — the promotion deadline and the realised return — would pass
    identically if they still read the running configuration.
    """

    approved = OrderPolicy(
        fixed_notional_usd=Decimal("10"),
        max_holding_ms=900_000,
        max_open_underlyings=1,
        max_orders_per_day=1,
    )
    adapter = _LiveLifecycleAdapter()
    config, row = _prepare_live_reviewed(conn, adapter=adapter, config=_live_config(order=approved))
    assert (int(row["max_holding_ms"]), int(row["taker_fee_bps"])) == (900_000, 5)
    adapter.observations = [_live_position_observation(row)]
    _approve_live(conn, row)

    # The process comes back under a configuration that would both shorten the hold to a minute and
    # charge fifty times the fee.
    redeployed = _live_config(
        order=OrderPolicy(
            fixed_notional_usd=Decimal("10"),
            max_holding_ms=60_000,
            taker_fee_bps=250,
            max_open_underlyings=1,
            max_orders_per_day=1,
        )
    )
    del config
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=redeployed, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 1_000
        ).turn()
    )
    opened = _order_row(conn, str(row["order_id"]))
    assert opened["state"] == "OPEN"
    assert int(opened["must_close_at_ms"]) == NOW + 900_000

    adapter.observations = [
        ExecutionObservation(
            state="CLOSED",
            observed_at_ms=NOW + 91_000,
            remote_order_id=f"remote-{row['order_id']}",
            first_fill_at_ms=NOW,
            closed_at_ms=NOW + 62_000,
            average_price=Decimal("9.9"),
            exit_price=Decimal("10.1"),
            evidence={"provider": "opentrade", "closed": True},
        )
    ]
    asyncio.run(
        ReconcileRunner(
            db=_DirectDb(conn), config=redeployed, bars=lambda _venue: None, adapter=adapter, clock=lambda: NOW + 91_001
        ).turn()
    )
    closed = _order_row(conn, str(row["order_id"]))
    assert closed["state"] == "CLOSED"
    # 202.0 bps gross, minus both legs at the frozen 5 bps. At the redeployed 250 it would be -298.
    assert int(closed["realized_bps"]) == 192
