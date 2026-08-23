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
    Bar,
    CandidateRunner,
    EligibilityPolicy,
    OrderPolicy,
    PaperAdapter,
    PaperFaults,
    ReconcileRunner,
    RegimePolicy,
    TradePolicy,
    TradingConfig,
    canonical_sha256,
)

pytestmark = pytest.mark.integration

NOW = 1_787_000_000_000
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


@pytest.fixture()
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    yield connection
    connection.close()


def _repos(conn: Any) -> Any:
    return repositories_for_connection(conn)


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
    assert trading.mark_submitting(order_id="o1", now_ms=NOW) is True
    conn.commit()
    # A caller that somehow tried again finds zero rows updated and must not call the provider.
    assert trading.mark_submitting(order_id="o1", now_ms=NOW) is False
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
        "leader_title, opened_at_ms, last_member_at_ms, expires_at_ms, member_count, admission, priority, "
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
        "evidence_sha256, focus_fact_id, program_version, program_sha256) "
        "VALUES (%s, 'triage', 'news_triage_policy_v9', 'push', 'push', 'push', '{}'::jsonb, '{}'::jsonb, false, "
        "%s, %s, 1, 'sha', %s, 'news_oi_signal_v1', repeat('a', 64))",
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
    assert case["case_kind"] == "oi_only"
    assert case["state"] == "ORDER_PREPARED"
    assert case["policy_reason"] == "oi_only_paper_regime"
    order = trading.order_for_case(case_id=case["case_id"])
    assert order is not None
    assert order["state"] == "ACKNOWLEDGED"
    assert int(order["provider_attempt_count"]) == 1
    assert adapter.attempts == 1


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
    trading.mark_submitting(order_id="o1", now_ms=NOW)
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
    assert order["state_reason"] == "submitting_after_restart"
    assert adapter.attempts == 0


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
