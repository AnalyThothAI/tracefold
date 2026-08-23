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
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.trading import (
    ACTIVE_ORDER_STATES,
    TRADING_MANIFEST_VERSION,
    Bar,
    Blacklist,
    CandidateRunner,
    EligibilityPolicy,
    Funnel,
    InstrumentRef,
    NewsTradeCandidate,
    OiTradeCandidate,
    OrderPolicy,
    PaperAdapter,
    PaperFaults,
    ReconcileRunner,
    RegimePolicy,
    TradePolicy,
    TradingCaseManifest,
    TradingConfig,
    assess,
    canonical_sha256,
    news_candidate,
    oi_candidate,
)

pytestmark = pytest.mark.integration

# Deliberately after the dynamically-created program_v6 epoch in migration 0301.
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
    assert trading.approve_order(order_id="o1", payload_sha256="digest", now_ms=NOW) is True
    conn.commit()
    # A second approval of an already-approved order changes nothing: the operator signed once.
    assert trading.approve_order(order_id="o1", payload_sha256="digest", now_ms=NOW) is False


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
        "UPDATE news_verdicts SET program_version = 'news_semantic_program_v4', verdict = %s::jsonb, "
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


def _runner(conn: Any, *, adapter: PaperAdapter, now: int, config: TradingConfig | None = None) -> CandidateRunner:
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
        program=None,
        clock=lambda: now,
    )


def test_oi_projection_exposes_only_post_epoch_v10_judgments(conn) -> None:
    epoch_start = int(
        conn.execute("SELECT starts_at_ms FROM news_learning_epochs WHERE epoch_id = 'program_v6'").fetchone()[
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
    assert rows[0]["learning_epoch"] == "program_v6"
    assert rows[0]["policy_version"] == "news_triage_policy_v10"
    assert rows[0]["editorial_origin"] == "telemetry_deterministic"
    assert len(rows[0]["scored_judgment_sha256"]) == 64


def test_model_projection_requires_v4_model_editorial_in_the_v6_epoch(conn) -> None:
    for event_id, symbol, program_version, origin in (
        ("current-model", "DOGE", "news_semantic_program_v4", "model"),
        ("old-model", "SOL", "program_v5", "model"),
        ("wrong-origin", "XRP", "news_semantic_program_v4", "telemetry_deterministic"),
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
    assert rows[0]["learning_epoch"] == "program_v6"
    assert rows[0]["program_version"] == "news_semantic_program_v4"
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
    } == {("program_v6", "news_oi_signal_v1", "news_triage_policy_v10", "telemetry_deterministic")}
    assert {
        (row["learning_epoch"], row["program_version"], row["policy_version"], row["editorial_origin"])
        for row in news_rows
    } == {("program_v6", "news_semantic_program_v4", "news_triage_policy_v10", "model")}

    blacklist = Blacklist.from_rows([])
    oi = oi_candidate(
        next(row for row in oi_rows if row["event_id"] == "oi-a"),
        now_ms=NOW,
        blacklist=blacklist,
    )
    projected_news = news_candidate(
        next(row for row in news_rows if row["event_id"] == "news-a"),
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
    assert manifest.digest() == "d41ce17a8f46af1aefeea08a5a750498765fb15960f9d4566a97f6985555ccba"


def test_a_qualifying_frame_becomes_one_paper_order_with_no_model_call(conn) -> None:
    now = NOW
    _seed_oi_event(conn, event_id="e1", symbol="DOGE", observed_at_ms=now - MINUTE)
    adapter = PaperAdapter()
    # `program=None`: an OI-only case must decide without a model, so an unconfigured program is not
    # an obstacle. If the lane ever routed one through DSPy this would settle as `program_unconfigured`.
    report = asyncio.run(_runner(conn, adapter=adapter, now=now).turn())

    assert report["created"] == 1
    trading = _repos(conn).trading
    case = trading.cases()[0]
    assert case["manifest"]["manifest_version"] == TRADING_MANIFEST_VERSION
    assert case["manifest"]["oi"]["learning_epoch"] == "program_v6"
    assert case["manifest"]["oi"]["policy_version"] == "news_triage_policy_v10"
    assert case["case_kind"] == "oi_only"
    assert case["state"] == "ORDER_PREPARED"
    assert case["policy_reason"] == "oi_only_paper_regime"
    order = trading.order_for_case(case_id=case["case_id"])
    assert order is not None
    assert order["state"] == "ACKNOWLEDGED"
    assert int(order["provider_attempt_count"]) == 1
    assert adapter.attempts == 1


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
    assert order["state"] == "OPEN"
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

    # The in-memory paper book is gone after a restart, so `observe` returns None.
    reconcile = ReconcileRunner(
        db=_DirectDb(conn), config=_config(), bars=lambda _v: None, adapter=PaperAdapter(), clock=lambda: NOW
    )
    asyncio.run(reconcile.turn())
    order = conn.execute("SELECT * FROM trading_orders").fetchone()
    assert order["state"] == "MANUAL_REVIEW_REQUIRED"
    assert order["state_reason"] == "exit_ambiguous_position_absent"


def test_an_observation_that_is_not_a_live_order_is_not_adopted_as_open(conn) -> None:
    _case(conn, case_id="c1", source_key="k1")
    _order(conn, order_id="o1", case_id="c1", underlying="crypto:DOGE", exchange_id="paper", state="AMBIGUOUS")
    order = _order_row(conn, "o1")

    class _RejectingAdapter(PaperAdapter):
        async def observe(self, _order: Any) -> Any:
            from tracefold.trading import ExecutionReceipt

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
    adapter = PaperAdapter()
    reconcile = ReconcileRunner(
        db=_DirectDb(conn), config=_config(), bars=lambda _v: None, adapter=adapter, clock=lambda: NOW
    )
    asyncio.run(reconcile.turn())
    row = _order_row(conn, "o1")
    assert row["state"] == "ACKNOWLEDGED"
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
    trading.update_order(order_id="o1", state="MANUAL_REVIEW_REQUIRED", now_ms=NOW)
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
    assert conn.execute("SELECT state FROM trading_orders").fetchone()["state"] == "ACKNOWLEDGED"

    async def feed(_symbol: str, _start: int, _end: int) -> Any:
        return (Bar(open_at_ms=now, close_at_ms=now + 300_000, close=Decimal("102")),)

    reconcile = ReconcileRunner(
        db=_DirectDb(conn), config=_config(), bars=lambda _v: feed, adapter=adapter, clock=lambda: now + 400_000
    )
    asyncio.run(reconcile.turn())
    assert conn.execute("SELECT state FROM trading_orders").fetchone()["state"] == "OPEN"
