"""TradeIntent persistence at the real PostgreSQL seam (#283)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from decimal import Decimal
from threading import Barrier, Event
from typing import Any

import pytest
from psycopg import OperationalError
from psycopg.errors import ForeignKeyViolation, InsufficientPrivilege, RaiseException, UniqueViolation

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.trading import (
    BlacklistSnapshotV1,
    ExecutionCapabilitySnapshotV1,
    ExecutionInstrumentCapabilityV1,
    IntentOutcome,
    ReplayReceiptV1,
    TradeIntent,
    deterministic_client_order_id,
)
from tracefold.trading.admission import ADMISSION_VERSION
from tracefold.trading.capital_lane import CapitalLane, CapitalLaneConfig
from tracefold.trading.catalog import VenueInstrumentCatalogEntryV1, build_venue_catalog_snapshot
from tracefold.trading.contracts import (
    CaseState,
    FrozenMarketContext,
    FrozenPolicyContext,
    InstrumentRef,
    OiMarketTrigger,
    OiTradeCandidate,
    TradingCaseManifest,
)
from tracefold.trading.policy import CAPITAL_POLICY

pytestmark = pytest.mark.integration

NOW = 1_900_000_000_000


# The bundle epoch the fixture Cases are frozen under, and the one the runner is composed with.
NEWS_GENERATION = "bundle_00000000"
AUTHORITY: dict[str, BlacklistSnapshotV1] = {}
CAPABILITY_SNAPSHOT = ExecutionCapabilitySnapshotV1(
    app_revision="test-revision",
    app_image_digest="test-image",
    nautilus_wheel_identity="test-wheel",
    news_universe_digest="a" * 64,
    provider_universe_digest="b" * 64,
    included={
        symbol: ExecutionInstrumentCapabilityV1(
            instrument_id=symbol,
            native_symbol=symbol.removesuffix("-PERP.BINANCE"),
            underlying_key=f"crypto:{symbol.removesuffix('USDT-PERP.BINANCE')}",
            quote_currency="USDT",
            price_precision=2,
            size_precision=3,
            price_increment="0.01",
            size_increment="0.001",
            min_quantity="0.001",
            min_notional="5",
        )
        for symbol in (
            "BTCUSDT-PERP.BINANCE",
            "DOGEUSDT-PERP.BINANCE",
            "ETHUSDT-PERP.BINANCE",
            "SOLUSDT-PERP.BINANCE",
        )
    },
    excluded={},
)
CATALOG_SNAPSHOT = build_venue_catalog_snapshot(
    binding="BINANCE_USDM",
    captured_at_ms=NOW,
    stale_after_ms=86_400_000,
    instruments=tuple(
        VenueInstrumentCatalogEntryV1(
            provider_instrument_id=f"{symbol}USDT",
            provider_symbol=f"{symbol}USDT",
            venue="binance.usdm",
            canonical_asset=symbol,
            canonical_namespace="crypto",
            product_kind="linear_perpetual",
            active=True,
            settlement_asset="USDT",
            margin_asset="USDT",
            price_increment="0.01",
            size_increment="0.001",
            min_quantity="0.001",
            raw_metadata_sha256=(str(index) * 64),
        )
        for index, symbol in enumerate(("BTC", "DOGE", "ETH", "SOL"), start=1)
    ),
)


@pytest.fixture(scope="module")
def conn(postgres_module_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    repos = repositories_for_connection(connection)
    connection.execute(
        "UPDATE trading_runtime_state SET nautilus_ready = false, "
        "nautilus_unexpected_exposure = false, nautilus_bootstrap_account_zero_at_ms = %s "
        "WHERE id = 1",
        (NOW,),
    )
    assert repos.trading.append_and_activate_execution_capability_snapshot(
        CAPABILITY_SNAPSHOT,
        created_at_ms=NOW,
    )
    repos.trading.store_venue_catalog_snapshot(snapshot=CATALOG_SNAPSHOT, now_ms=NOW)
    AUTHORITY["blacklist"] = repos.trading.blacklist_snapshot(now_ms=NOW, materialize_expiry=True)
    connection.commit()
    yield connection
    connection.close()


def _case(connection: Any, *, case_id: str = "case-intent-1") -> None:
    connection.execute("TRUNCATE trading_intents, trading_orders, trading_cases CASCADE")
    _reset_authority(connection)
    connection.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
          strategy_config_digest, primary_source_key, supplemental_source_keys,
          manifest, manifest_sha256, state, policy_decision, policy_reason,
          capital_disposition, capital_reason, observed_at_ms, created_at_ms, updated_at_ms
        ) VALUES (%s, 'crypto:SOL', 'oi', 'binance_oi_smart_money_long_v2',
                  'binance_oi_smart_money_long_v2', %s, %s, '[]'::jsonb,
                  '{}'::jsonb, %s, 'RUNNING', 'not_run', 'not_run',
                  'not_applicable', NULL, %s, %s, %s)
        """,
        (case_id, "0" * 64, f"source-{case_id}", "1" * 64, NOW, NOW, NOW),
    )
    connection.commit()


def _intent(
    *,
    case_id: str = "case-intent-1",
    case_manifest_sha256: str = "1" * 64,
    created_at_ms: int = NOW,
) -> TradeIntent:
    blacklist = AUTHORITY["blacklist"]
    return TradeIntent.create(
        case_id=case_id,
        case_manifest_sha256=case_manifest_sha256,
        execution_capability_snapshot_sha256=CAPABILITY_SNAPSHOT.snapshot_sha256,
        blacklist_snapshot=blacklist,
        instrument_id="SOLUSDT-PERP.BINANCE",
        underlying_key="crypto:SOL",
        created_at_ms=created_at_ms,
        reference_price=Decimal("60000"),
        target_notional_usd=Decimal("10"),
    )


def _allow_entry(connection: Any) -> None:
    connection.execute(
        """
        UPDATE trading_runtime_state
           SET control = 'RUNNING', nautilus_ready = true, nautilus_unexpected_exposure = false
         WHERE id = 1
        """
    )


def _reset_authority(connection: Any) -> None:
    connection.execute("DELETE FROM trading_symbol_blacklist WHERE base_symbol NOT IN ('BTC', 'ETH', 'CL')")
    connection.execute(
        """
        UPDATE trading_runtime_state
           SET control = 'PAUSED', blacklist_revision = 0,
               active_capability_snapshot_sha256 = %s,
               active_capability_included_count = %s,
               nautilus_ready = false, nautilus_unexpected_exposure = false,
               nautilus_bootstrap_account_zero_at_ms = NULL
         WHERE id = 1
        """,
        (CAPABILITY_SNAPSHOT.snapshot_sha256, len(CAPABILITY_SNAPSHOT.included)),
    )
    repositories_for_connection(connection).trading.store_venue_catalog_snapshot(
        snapshot=CATALOG_SNAPSHOT,
        now_ms=NOW,
    )
    connection.execute(
        """
        UPDATE trading_binding_runtime
           SET credential_state = 'unconfigured', credential_fingerprint = NULL,
               runtime_state = 'stopped', account_state = 'unknown',
               heartbeat_at_ms = NULL, reason = 'credentials_unconfigured', updated_at_ms = %s
         WHERE binding = 'BINANCE_USDM'
        """,
        (NOW,),
    )


def test_replay_success_receipt_is_idempotent_content_bound_and_append_only(conn: Any) -> None:
    conn.execute("TRUNCATE trading_replay_runs")
    receipt = ReplayReceiptV1(
        run_id="7" * 64,
        spec_sha256="7" * 64,
        created_at_ms=NOW,
        artifact_path="/tracefold-artifacts/replay/7/replay.json",
        artifact_sha256="8" * 64,
        source_count=3,
        directional_count=1,
        terminal_outcome_count=3,
    )
    repos = repositories_for_connection(conn)

    assert repos.trading.insert_replay_receipt(receipt) is True
    assert repos.trading.insert_replay_receipt(receipt) is False
    assert ReplayReceiptV1.model_validate(repos.trading.replay_receipt(receipt.run_id)) == receipt
    conn.commit()

    with pytest.raises(RaiseException, match="trading_append_only_mutation_forbidden"):
        conn.execute("UPDATE trading_replay_runs SET source_count = 4 WHERE run_id = %s", (receipt.run_id,))
    conn.rollback()
    with pytest.raises(RaiseException, match="trading_append_only_mutation_forbidden"):
        conn.execute("DELETE FROM trading_replay_runs WHERE run_id = %s", (receipt.run_id,))
    conn.rollback()


class _RunnerDb:
    """One bounded read and one bounded transaction over a real connection.

    `fail_capital_settle` makes the Case's terminal capital transition return `False` inside the
    commit, proving the policy and capital facts stay atomic.
    """

    def __init__(self, connection: Any, *, fail_capital_settle: bool = False) -> None:
        self.connection = connection
        self.fail_capital_settle = fail_capital_settle

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        del timeout_seconds
        repos = repositories_for_connection(self.connection)
        if name == "trading_capital_disposition_commit" and self.fail_capital_settle:
            trading = repos.trading
            original = trading.settle_case

            def refuse_capital_block(**kwargs: Any) -> bool:
                if kwargs.get("capital_disposition") == "blocked":
                    return False
                return bool(original(**kwargs))

            trading.settle_case = refuse_capital_block  # type: ignore[method-assign]
        with self.connection.transaction():
            return fn(repos)

    async def read(self, _name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        del timeout_seconds
        return fn(repositories_for_connection(self.connection))


def _executable_manifest(
    *,
    instrument: InstrumentRef | None = None,
    catalog_sha256: str | None = None,
) -> TradingCaseManifest:
    selected = instrument or InstrumentRef(
        exchange_id="binance",
        venue="binance.perp",
        provider_symbol="SOLUSDT",
        base_symbol="SOL",
        instrument_class="crypto",
        quote_asset="USDT",
        observed_at_ms=NOW,
    )
    oi = OiTradeCandidate(
        event_id=f"event-{selected.base_symbol.lower()}",
        observed_at_ms=NOW,
        verdict_created_at_ms=NOW,
        base_symbol=selected.base_symbol,
        venue="binance",
        oi_direction="rise",
        oi_change_bps=1_548,
        oi_value_usd=250_000_000,
        whale_long_profit_bps=1,
        whale_oi_ratio_bps=6_000,
        rank_in_window=1,
        final_decision="push",
        source_rule="opening_move_with_whale_concentration",
        source_strategy_id="oi_5m",
        source_contract_version="oi_source_v1",
        measurement_window_ms=300_000,
        learning_epoch=NEWS_GENERATION,
        program_version="news_oi_signal_v1",
        program_sha256="a" * 64,
        policy_version="news_triage_policy_v10",
        editorial_origin="telemetry_deterministic",
        editorial_sha256="b" * 64,
        scored_judgment_sha256="c" * 64,
        runtime_manifest_sha="d" * 64,
        metric_version="oi_signal_v1",
    )
    return TradingCaseManifest(
        primary_trigger=OiMarketTrigger(
            source_key=oi.source_key,
            observed_at_ms=NOW,
            persisted_at_ms=NOW,
            venue="binance",
        ),
        contexts=FrozenPolicyContext(
            oi=oi,
            market=FrozenMarketContext(
                mark_price=Decimal("100"),
                observed_at_ms=NOW,
                pre_move_bps=200,
                pre_move_lookback_ms=3_600_000,
            ),
        ),
        policy_id=CAPITAL_POLICY.policy_id,
        policy_version=CAPITAL_POLICY.policy_version,
        policy_config=CAPITAL_POLICY.config_snapshot,
        policy_config_digest=CAPITAL_POLICY.config_digest,
        underlying_key=f"crypto:{selected.base_symbol}",
        base_symbol=selected.base_symbol,
        cutoff_ms=NOW,
        instrument=selected,
        venue_catalog_snapshot_sha256=catalog_sha256 or CATALOG_SNAPSHOT.snapshot_sha256,
    )


def _pending_executable_case(
    connection: Any,
    *,
    manifest: TradingCaseManifest | None = None,
    case_id: str = "case-sol",
) -> TradingCaseManifest:
    connection.execute("TRUNCATE trading_intents, trading_orders, trading_cases CASCADE")
    _reset_authority(connection)
    manifest = manifest or _executable_manifest()
    repos = repositories_for_connection(connection)
    admission = _admission_row(manifest)
    assert repos.trading.create_case(case_id=case_id, manifest=manifest, admission=admission, now_ms=NOW)
    connection.commit()
    return manifest


def _admission_row(manifest: TradingCaseManifest) -> dict[str, Any]:
    return {
        "source_key": manifest.primary_trigger.source_key,
        "gate_version": ADMISSION_VERSION,
        "gate_config_digest": "0" * 64,
        "trigger_kind": "oi",
        "underlying_key": manifest.underlying_key,
        "source_observed_at_ms": NOW,
        "status": "CASE_CREATED",
        "stage": "freeze",
        "reason": "case_created",
        "retryable": False,
        "evidence": {},
        "case_id": None,
    }


def _capital_lane(connection: Any, *, fail_capital_settle: bool = False) -> CapitalLane:
    async def _bars(_symbol: str, _start: int, _end: int) -> tuple[()]:
        return ()

    return CapitalLane(
        db=_RunnerDb(connection, fail_capital_settle=fail_capital_settle),
        config=CapitalLaneConfig(target_notional_usd=Decimal("7.5")),
        bars=_bars,
        oi_projection=lambda *_args: (),
        # The News generation this lane may advance a Case under (#314 review). It matches the epoch the
        # fixture manifests are frozen with; the superseded-generation test is where they disagree.
        news_generation=NEWS_GENERATION,
        clock=lambda: NOW + 1_000,
    )


def _observe_close(
    repos: Any,
    intent: TradeIntent,
    *,
    position_id: str,
    avg_exit_price: Decimal,
    closed_at_ms: int,
    realized_pnl_amount: Decimal | None = None,
    realized_pnl_currency: str | None = None,
    commissions_by_currency: dict[str, str] | None = None,
) -> Any:
    observed = repos.trading.record_position_closed_observed(
        intent.intent_id,
        instrument_id=intent.instrument_id,
        account_id="BINANCE-001",
        position_id=position_id,
        closing_client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        local_quantity=Decimal(0),
        avg_exit_price=avg_exit_price,
        closed_at_ms=closed_at_ms,
        realized_pnl_amount=realized_pnl_amount,
        realized_pnl_currency=realized_pnl_currency,
        commissions_by_currency=commissions_by_currency,
        now_ms=closed_at_ms,
    )
    assert observed is not None
    assert observed.execution_state == "IN_FLIGHT"
    assert observed.execution_phase == "EXIT"
    assert observed.flat_verified_at_ms is None
    assert observed.commissions_by_currency == commissions_by_currency
    return observed


def test_the_lane_refuses_a_case_frozen_under_a_superseded_news_generation(conn: Any) -> None:
    """A Case that outlived the News bundle it was reasoned under must not become an Intent (#314 review).

    The sequence is ordinary rather than exotic: a Case is frozen, left undecided, and a deployment moves
    the News bundle — a prompt edit or a model re-slot does that without moving `program_version` or
    `policy_version`, which are the only other upstream pins this path holds. The projection that creates
    Cases joins the running epoch, so a stale row cannot enter; nothing but this check stops one that is
    already persisted from advancing.
    """

    _pending_executable_case(conn)
    lane = _capital_lane(conn)
    lane._news_generation = "bundle_deadbeef"

    assert asyncio.run(lane._decide_one()) is CaseState.BLOCKED

    row = conn.execute(
        "SELECT state, policy_decision, policy_reason, capital_disposition, capital_reason "
        "FROM trading_cases WHERE case_id = 'case-sol'"
    ).fetchone()
    assert dict(row) == {
        "state": "BLOCKED",
        "policy_decision": "not_run",
        "policy_reason": "source_generation_retired",
        "capital_disposition": "not_applicable",
        "capital_reason": None,
    }
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0


def test_no_key_lane_persists_long_and_capital_block_without_an_intent(conn: Any) -> None:
    """#350 F2P: Decision runs under PAUSED Capital and preserves both independent answers."""

    _pending_executable_case(conn)

    assert asyncio.run(_capital_lane(conn)._decide_one()) is CaseState.BLOCKED

    row = conn.execute(
        "SELECT state, policy_decision, policy_reason, policy_checks, "
        "       capital_disposition, capital_reason "
        "FROM trading_cases WHERE case_id = 'case-sol'"
    ).fetchone()
    assert dict(row) | {"policy_checks": None} == {
        "state": "BLOCKED",
        "policy_decision": "long",
        "policy_reason": "smart_money_momentum_long",
        "policy_checks": None,
        "capital_disposition": "blocked",
        "capital_reason": "capital_paused",
    }
    # The frozen evidence travels with the Case, so a console can explain it without today's config.
    checks = row["policy_checks"]["checks"]
    assert {check["check"] for check in checks} == {
        "source_measurement_window_ms",
        "oi_direction",
        "oi_change_bps",
        "whale_oi_ratio_bps",
        "whale_long_profit_bps",
        "pre_move_bps",
    }
    assert all(check["passed"] for check in checks)
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM trading_orders").fetchone()["n"] == 0


def test_running_capital_still_blocks_no_key_long_as_credentials_unconfigured(conn: Any) -> None:
    doge = InstrumentRef(
        exchange_id="binance",
        venue="binance.perp",
        provider_symbol="DOGEUSDT",
        base_symbol="DOGE",
        instrument_class="crypto",
        quote_asset="USDT",
        observed_at_ms=NOW,
    )
    _pending_executable_case(conn, manifest=_executable_manifest(instrument=doge))
    conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
    conn.commit()

    assert asyncio.run(_capital_lane(conn)._decide_one()) is CaseState.BLOCKED
    row = conn.execute(
        "SELECT policy_decision, capital_disposition, capital_reason FROM trading_cases WHERE case_id = 'case-sol'"
    ).fetchone()
    assert dict(row) == {
        "policy_decision": "long",
        "capital_disposition": "blocked",
        "capital_reason": "credentials_unconfigured",
    }
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0


def test_a_failed_capital_transition_leaves_the_case_claimable(conn: Any) -> None:
    """The LONG and capital block are one transition; neither may be partially persisted."""

    _pending_executable_case(conn)

    with pytest.raises(RuntimeError, match="trading_case_block_transition_failed"):
        asyncio.run(_capital_lane(conn, fail_capital_settle=True)._decide_one())
    conn.rollback()

    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    case = conn.execute("SELECT state, policy_reason FROM trading_cases WHERE case_id = 'case-sol'").fetchone()
    # `RUNNING` with an expired lease is claimable again; it is emphatically not a terminal state, and
    # `intent_admission_blocked` — the catch-all this replaces — no longer exists to be written.
    assert case["state"] == "RUNNING"
    assert case["policy_reason"] == "not_run"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("exchange_id", "hyperliquid"),
        ("venue", "hl.perp"),
        ("instrument_class", "equity"),
        ("provider_symbol", "SOL"),
        ("base_symbol", "ETH"),
        ("quote_asset", "USDC"),
    ),
)
def test_a_public_catalog_identity_never_grants_execution(
    conn: Any,
    field: str,
    value: str,
) -> None:
    """The public catalog freezes evidence; it is not an execution permission or adapter registry."""

    exact = _executable_manifest().instrument
    altered = InstrumentRef.model_validate({**exact.model_dump(), field: value})
    _pending_executable_case(conn, manifest=_executable_manifest(instrument=altered))

    assert asyncio.run(_capital_lane(conn)._decide_one()) is CaseState.BLOCKED

    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    case = conn.execute(
        "SELECT state, policy_decision, capital_reason FROM trading_cases WHERE case_id = 'case-sol'"
    ).fetchone()
    assert dict(case) == {
        "state": "BLOCKED",
        "policy_decision": "long",
        "capital_reason": "capital_paused",
    }


def test_a_catalog_pointer_that_moves_after_freeze_blocks_the_case(conn: Any) -> None:
    """A Case must retain the exact public catalog evidence it was frozen against."""

    _pending_executable_case(conn, manifest=_executable_manifest(catalog_sha256="9" * 64))
    conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
    conn.execute(
        "UPDATE trading_binding_runtime SET credential_state = 'configured', reason = NULL "
        "WHERE binding = 'BINANCE_USDM'"
    )
    conn.commit()

    assert asyncio.run(_capital_lane(conn)._decide_one()) is CaseState.BLOCKED

    case = conn.execute("SELECT state, capital_reason FROM trading_cases WHERE case_id = 'case-sol'").fetchone()
    assert dict(case) == {"state": "BLOCKED", "capital_reason": "catalog_mismatch"}
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0


def test_an_operator_deny_list_cannot_turn_paused_capital_into_execution(conn: Any) -> None:

    _pending_executable_case(conn)
    repos = repositories_for_connection(conn)
    with conn.transaction():
        repos.trading.blacklist_upsert(base_symbol="SOL", reason="operator", expires_at_ms=None, now_ms=NOW)
    conn.commit()

    assert asyncio.run(_capital_lane(conn)._decide_one()) is CaseState.BLOCKED

    case = conn.execute(
        "SELECT state, policy_decision, capital_reason FROM trading_cases WHERE case_id = 'case-sol'"
    ).fetchone()
    assert dict(case) == {
        "state": "BLOCKED",
        "policy_decision": "long",
        "capital_reason": "capital_paused",
    }
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    conn.execute("DELETE FROM trading_symbol_blacklist WHERE base_symbol = 'SOL'")
    conn.commit()


def test_two_workers_deciding_the_same_case_produce_one_capital_block_and_no_intent(conn: Any) -> None:
    """#331 F2P 6. The claim is exclusive; the loser reads no Case and writes nothing."""

    _pending_executable_case(conn)
    first = _capital_lane(conn)
    second = _capital_lane(conn)

    assert asyncio.run(first._decide_one()) is CaseState.BLOCKED
    assert asyncio.run(second._decide_one()) is None
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0


def test_two_concurrent_connections_claiming_one_case_write_one_block(conn: Any) -> None:
    """The claim remains exclusive against real PostgreSQL concurrency.

    `FOR UPDATE SKIP LOCKED` is what makes the claim exclusive, and the loser's `SKIP LOCKED` returns no
    row rather than blocking — so the second worker does no work at all instead of racing to a second
    terminal receipt. The terminal receipt both read afterwards is the one the winner committed.
    """

    _pending_executable_case(conn)
    barrier = Barrier(2)

    def compete(run_id: str) -> CaseState | None:
        contender = connect_postgres_test(read_only=False)
        try:
            lane = _capital_lane(contender)
            lane._run_id = run_id
            barrier.wait(timeout=5)
            decided = asyncio.run(lane._decide_one())
            contender.commit()
            return decided
        finally:
            contender.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ("run-a", "run-b")))

    assert sorted(results, key=lambda item: item is None) == [CaseState.BLOCKED, None]
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    case = conn.execute("SELECT state FROM trading_cases WHERE case_id = 'case-sol'").fetchone()
    assert case["state"] == "BLOCKED"


def test_a_postgres_fault_inside_the_capital_commit_rolls_back_and_never_terminalises(conn: Any) -> None:
    """#331 comment F2P 5. A PostgreSQL fault is not a business refusal and must not consume the Case.

    The old writer caught every `Exception` from the emission transaction and wrote
    `BLOCKED / intent_admission_blocked` — so a timeout, a serialization failure and a genuine capability
    change were one statistic, and the Source that caused it was consumed forever.
    """

    _pending_executable_case(conn)
    lane = _capital_lane(conn)
    original = lane._db.tx  # type: ignore[attr-defined]

    async def explode(name: str, fn: Any, *, timeout_seconds: float) -> Any:
        if name == "trading_capital_disposition_commit":
            raise OperationalError("server closed the connection unexpectedly")
        return await original(name, fn, timeout_seconds=timeout_seconds)

    lane._db.tx = explode  # type: ignore[attr-defined, method-assign]

    with pytest.raises(OperationalError):
        asyncio.run(lane._decide_one())
    conn.rollback()

    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    case = conn.execute("SELECT state, policy_reason FROM trading_cases WHERE case_id = 'case-sol'").fetchone()
    assert case["state"] == "RUNNING"
    assert case["policy_reason"] == "not_run"
    # And the retired catch-all is not written under any name.
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM trading_cases WHERE policy_reason = 'intent_admission_blocked'"
        ).fetchone()["n"]
        == 0
    )


def test_a_case_that_settles_only_to_the_three_current_terminal_states(conn: Any) -> None:
    """The two historical states stay readable and have no path back into the table (#331)."""

    _pending_executable_case(conn)
    repos = repositories_for_connection(conn)
    claimed = repos.trading.claim_case(run_id="run-1", lease_ms=60_000, now_ms=NOW)
    assert claimed is not None
    for retired in (CaseState.POLICY_REJECTED, CaseState.ORDER_PREPARED, CaseState.PENDING):
        with pytest.raises(ValueError, match="trading_case_terminal_state_retired"):
            repos.trading.settle_case(
                case_id="case-sol",
                run_id="run-1",
                state=retired,
                policy_decision="no_trade",
                policy_reason="whatever",
                capital_disposition="not_applicable",
                capital_reason=None,
                now_ms=NOW,
            )
    conn.rollback()


def test_capability_replacement_requires_a_fresh_zero_proof_and_no_nonterminal_intent(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    replacement = CAPABILITY_SNAPSHOT.model_copy(update={"app_revision": "replacement-revision"})

    conn.execute(
        "UPDATE trading_runtime_state SET control = 'PAUSED', nautilus_ready = true, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = %s WHERE id = 1",
        (NOW - 15_001,),
    )
    assert not repos.trading.append_and_activate_execution_capability_snapshot(
        replacement,
        created_at_ms=NOW,
    )

    assert repos.trading.insert_intent(_intent())
    conn.execute(
        "UPDATE trading_runtime_state SET control = 'PAUSED', nautilus_ready = true, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    assert not repos.trading.append_and_activate_execution_capability_snapshot(
        replacement,
        created_at_ms=NOW,
    )
    conn.execute("DELETE FROM trading_intents")

    conn.execute(
        "UPDATE trading_runtime_state SET control = 'PAUSED', nautilus_ready = true, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM trading_intents WHERE execution_state IN "
            "('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')"
        ).fetchone()["n"]
        == 0
    )
    assert repos.trading.append_and_activate_execution_capability_snapshot(
        replacement,
        created_at_ms=NOW,
    )
    runtime = repos.trading.runtime_state()
    assert runtime is not None
    assert runtime["active_capability_snapshot_sha256"] == replacement.snapshot_sha256
    assert runtime["nautilus_ready"] is False
    assert runtime["nautilus_readiness_reason"] == "capability_snapshot_changed"
    _reset_authority(conn)


def test_capability_replacement_accepts_a_fresh_zero_claim_recovery_proof(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    replacement = CAPABILITY_SNAPSHOT.model_copy(update={"app_revision": "recovered-revision"})
    conn.execute(
        "UPDATE trading_runtime_state SET control = 'PAUSED', nautilus_ready = false, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = NULL, "
        "nautilus_bootstrap_account_zero_at_ms = NULL WHERE id = 1"
    )
    assert not repos.trading.set_nautilus_bootstrap_account_zero(
        verified_at_ms=NOW,
        now_ms=NOW,
        expected_capability_snapshot_sha256="f" * 64,
    )
    assert repos.trading.runtime_state()["nautilus_bootstrap_account_zero_at_ms"] is None

    assert repos.trading.insert_intent(_intent())
    assert not repos.trading.set_nautilus_bootstrap_account_zero(
        verified_at_ms=NOW,
        now_ms=NOW,
        expected_capability_snapshot_sha256=CAPABILITY_SNAPSHOT.snapshot_sha256,
    )
    conn.execute("DELETE FROM trading_intents")

    assert repos.trading.set_nautilus_bootstrap_account_zero(
        verified_at_ms=NOW,
        now_ms=NOW,
        expected_capability_snapshot_sha256=CAPABILITY_SNAPSHOT.snapshot_sha256,
    )

    conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
    assert repos.trading.insert_intent(_intent())
    assert repos.trading.set_nautilus_bootstrap_account_zero(
        verified_at_ms=None,
        now_ms=NOW + 1,
        expected_capability_snapshot_sha256=CAPABILITY_SNAPSHOT.snapshot_sha256,
    )
    assert repos.trading.runtime_state()["nautilus_bootstrap_account_zero_at_ms"] is None
    conn.execute("DELETE FROM trading_intents")
    conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
    assert repos.trading.set_nautilus_bootstrap_account_zero(
        verified_at_ms=NOW + 2,
        now_ms=NOW + 2,
        expected_capability_snapshot_sha256=CAPABILITY_SNAPSHOT.snapshot_sha256,
    )

    assert repos.trading.append_and_activate_execution_capability_snapshot(replacement, created_at_ms=NOW + 2)
    runtime = repos.trading.runtime_state()
    assert runtime is not None
    assert runtime["active_capability_snapshot_sha256"] == replacement.snapshot_sha256
    assert runtime["nautilus_bootstrap_account_zero_at_ms"] is None
    _reset_authority(conn)


def test_capability_replacement_accepts_zero_proof_across_bounded_provider_load(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    replacement = CAPABILITY_SNAPSHOT.model_copy(update={"app_revision": "provider-load-revision"})
    conn.execute(
        "UPDATE trading_runtime_state SET control = 'PAUSED', nautilus_ready = false, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = NULL, "
        "nautilus_bootstrap_account_zero_at_ms = %s WHERE id = 1",
        (NOW,),
    )

    assert repos.trading.append_and_activate_execution_capability_snapshot(
        replacement,
        created_at_ms=NOW + 60_000,
    )
    _reset_authority(conn)


def test_initial_capability_activation_also_requires_a_fresh_zero_proof(conn: Any) -> None:
    repos = repositories_for_connection(conn)
    initial = CAPABILITY_SNAPSHOT.model_copy(update={"app_revision": "initial-revision"})
    conn.execute(
        "UPDATE trading_runtime_state SET control = 'PAUSED', active_capability_snapshot_sha256 = NULL, "
        "active_capability_included_count = 0, nautilus_ready = false, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = NULL, "
        "nautilus_bootstrap_account_zero_at_ms = NULL WHERE id = 1"
    )

    assert not repos.trading.append_and_activate_execution_capability_snapshot(initial, created_at_ms=NOW)
    assert repos.trading.runtime_state()["active_capability_snapshot_sha256"] is None

    conn.execute(
        "UPDATE trading_runtime_state SET nautilus_bootstrap_account_zero_at_ms = %s WHERE id = 1",
        (NOW - 300_001,),
    )
    assert not repos.trading.append_and_activate_execution_capability_snapshot(initial, created_at_ms=NOW)

    conn.execute(
        "UPDATE trading_runtime_state SET nautilus_ready = true, nautilus_heartbeat_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    assert not repos.trading.append_and_activate_execution_capability_snapshot(initial, created_at_ms=NOW)

    conn.execute(
        "UPDATE trading_runtime_state SET nautilus_ready = false, "
        "nautilus_bootstrap_account_zero_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    assert repos.trading.append_and_activate_execution_capability_snapshot(initial, created_at_ms=NOW)
    runtime = repos.trading.runtime_state()
    assert runtime["active_capability_snapshot_sha256"] == initial.snapshot_sha256
    assert runtime["nautilus_bootstrap_account_zero_at_ms"] is None
    _reset_authority(conn)


def test_capability_activation_cannot_be_overwritten_by_old_engine_readiness(conn: Any) -> None:
    repos = repositories_for_connection(conn)
    replacement = CAPABILITY_SNAPSHOT.model_copy(update={"app_revision": "serialized-replacement"})
    conn.execute(
        "UPDATE trading_runtime_state SET control = 'PAUSED', nautilus_ready = true, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    started = Event()

    def activate() -> bool:
        contender = connect_postgres_test(read_only=False)
        try:
            contender_repos = repositories_for_connection(contender)
            with contender.transaction():
                started.set()
                return contender_repos.trading.append_and_activate_execution_capability_snapshot(
                    replacement,
                    created_at_ms=NOW,
                )
        finally:
            contender.close()

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with conn.transaction():
            runtime = repos.trading.nautilus_runtime_state(for_update=True)
            assert runtime is not None
            future = pool.submit(activate)
            assert started.wait(timeout=5)
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.5)
            repos.trading.set_nautilus_runtime(
                heartbeat_at_ms=NOW,
                ready=True,
                readiness_reason="ready",
                unexpected_exposure=False,
                now_ms=NOW,
            )
        assert future.result(timeout=5) is True
    finally:
        pool.shutdown(wait=True)

    runtime = repos.trading.runtime_state()
    assert runtime is not None
    assert runtime["active_capability_snapshot_sha256"] == replacement.snapshot_sha256
    assert runtime["nautilus_ready"] is False
    assert runtime["nautilus_readiness_reason"] == "capability_snapshot_changed"
    _reset_authority(conn)


def test_trade_intent_round_trips_as_one_immutable_material_fact(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)

    assert repos.trading.insert_intent(intent) is True
    conn.commit()

    stored = repos.trading.intent(intent.intent_id)
    assert stored == intent
    assert len(intent.intent_id) == 64


def test_intent_projection_schema_has_no_duplicate_timestamp_or_poll_index(conn: Any) -> None:
    columns = {
        row[0]
        for row in conn.execute(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'trading_intents'
            """
        ).fetchall()
    }
    indexes = {
        row[0]
        for row in conn.execute(
            """
            SELECT indexname
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND tablename = 'trading_intents'
            """
        ).fetchall()
    }

    assert "engine_seen_at_ms" not in columns
    assert "ix_trading_intents_poll" not in indexes


def test_intent_case_manifest_identity_must_match_the_referenced_case(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)

    with pytest.raises(ForeignKeyViolation):
        repos.trading.insert_intent(_intent(case_manifest_sha256="3" * 64))
    conn.rollback()


def test_entry_fence_is_the_single_durable_permission_for_an_exposure_increase(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(intent) is True
    conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED', nautilus_ready = false WHERE id = 1")
    conn.commit()

    # `UNAVAILABLE`, and it says why (#331). The old `None` meant this, a stale dispatch, an expired
    # TTL and a spent daily fence at once, so an engine held back by readiness looked exactly like one
    # with nothing to do.
    paused = repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000)
    assert (paused.disposition, paused.reason, paused.outcome) == ("UNAVAILABLE", "runtime_not_ready", None)
    conn.commit()

    conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
    conn.commit()
    not_ready = repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_500)
    assert (not_ready.disposition, not_ready.reason) == ("UNAVAILABLE", "runtime_not_ready")
    conn.commit()

    _allow_entry(conn)
    conn.commit()
    fence = repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 2_000)
    conn.commit()

    assert (fence.disposition, fence.reason) == ("GRANTED", "entry_fence_granted")
    fenced = fence.outcome
    assert fenced is not None
    assert fenced.execution_state == "IN_FLIGHT"
    assert fenced.execution_phase == "ENTRY"
    assert fenced.entry_client_order_id == deterministic_client_order_id(intent.intent_id, "entry")
    assert fenced.entry_fenced_at_ms == NOW + 2_000

    # A duplicate scan or restart can read this projection, but cannot acquire a second submit fence.
    again = repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 3_000)
    assert (again.disposition, again.reason) == ("UNAVAILABLE", "intent_not_claimable")
    conn.commit()
    recovered = repos.trading.intent_outcome(intent.intent_id)
    assert recovered == fenced


def test_blacklist_change_after_emission_is_rechecked_and_frozen_at_the_entry_fence(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(intent) is True
    repos.trading.blacklist_upsert(
        base_symbol="SOL",
        reason="operator",
        expires_at_ms=None,
        now_ms=NOW + 500,
    )
    _allow_entry(conn)
    conn.commit()

    fence = repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000)
    conn.commit()

    assert fence.disposition == "REFUSED"
    refused = fence.outcome
    assert refused is not None
    assert (refused.execution_state, refused.terminal_outcome, refused.reason_code) == (
        "TERMINAL",
        "REJECTED",
        "blacklisted",
    )
    evidence = conn.execute(
        "SELECT blacklist_revision_at_emission, blacklist_revision_at_fence, "
        "blacklist_snapshot_sha256_at_fence, blacklist_snapshot_payload_at_fence "
        "FROM trading_intents WHERE intent_id = %s",
        (intent.intent_id,),
    ).fetchone()
    assert evidence["blacklist_revision_at_emission"] == 0
    assert evidence["blacklist_revision_at_fence"] == 1
    assert len(evidence["blacklist_snapshot_sha256_at_fence"]) == 64
    assert any(
        row["underlying_key"] == "crypto:SOL" for row in evidence["blacklist_snapshot_payload_at_fence"]["active_rows"]
    )

    assert repos.trading.blacklist_delete(base_symbol="SOL", now_ms=NOW + 2_000) == 1
    conn.commit()


def test_expired_blacklist_does_not_kill_a_pending_entry_fence(conn: Any) -> None:
    _case(conn)
    db_now_ms = int(
        conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms").fetchone()[
            "now_ms"
        ]
    )
    intent = _intent(created_at_ms=db_now_ms - 1_000)
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(intent) is True
    repos.trading.blacklist_upsert(
        base_symbol="SOL",
        reason="timed_operator_hold",
        expires_at_ms=db_now_ms - 1,
        now_ms=db_now_ms - 500,
    )
    _allow_entry(conn)
    conn.commit()

    conn.execute("SET ROLE tracefold_nautilus")
    fenced = repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=db_now_ms).outcome
    conn.commit()
    conn.execute("RESET ROLE")
    conn.commit()

    assert fenced is not None
    assert (fenced.execution_state, fenced.execution_phase) == ("IN_FLIGHT", "ENTRY")
    evidence = conn.execute(
        "SELECT blacklist_revision_at_fence, blacklist_snapshot_payload_at_fence "
        "FROM trading_intents WHERE intent_id = %s",
        (intent.intent_id,),
    ).fetchone()
    assert evidence["blacklist_revision_at_fence"] == 2
    assert all(
        row["underlying_key"] != "crypto:SOL" for row in evidence["blacklist_snapshot_payload_at_fence"]["active_rows"]
    )


def test_two_database_transactions_competing_for_one_entry_fence_have_one_winner(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    conn.commit()
    barrier = Barrier(2)

    def compete(engine_identity: str) -> IntentOutcome | None:
        contender = connect_postgres_test(read_only=False)
        try:
            contender_repos = repositories_for_connection(contender)
            barrier.wait(timeout=5)
            result = contender_repos.trading.fence_entry(
                intent.intent_id,
                engine_identity=engine_identity,
                now_ms=NOW + 2_000,
            ).outcome
            contender.commit()
            return result
        finally:
            contender.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ("nt-a", "nt-b")))

    assert sum(result is not None for result in results) == 1
    stored = repos.trading.intent_outcome(intent.intent_id)
    assert stored is not None
    assert stored.execution_state == "IN_FLIGHT"
    assert stored.engine_identity in {"nt-a", "nt-b"}


def test_a_closed_thesis_frees_the_lane_for_another_entry_the_same_day(conn: Any) -> None:
    """#348 inverts this test. It used to prove the one-entry-per-UTC-day fence, code and index both.

    Exposure was never what that fence bounded — `ux_trading_intents_one_active` is a unique index
    admitting a single nonterminal Intent, and it is untouched. The daily cap bounded *throughput*,
    and the cost was a blind spot on exactly the days the lane worked: every frame after the first
    entry was refused before the policy ran, so the lane could not say which of them it should have
    taken. Once the first thesis is flat, the second may enter the same day.
    """

    _case(conn)
    repos = repositories_for_connection(conn)
    first = _intent()
    assert repos.trading.insert_intent(first) is True
    _allow_entry(conn)
    assert repos.trading.fence_entry(first.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000).granted
    assert repos.trading.record_entry_fill(
        first.intent_id,
        actual_quantity=Decimal("0.0001"),
        avg_entry_price=Decimal("60000"),
        position_id="position-1",
        opened_at_ms=NOW + 2_000,
        now_ms=NOW + 2_000,
    )
    assert repos.trading.record_close_submitted(
        first.intent_id,
        client_order_id=deterministic_client_order_id(first.intent_id, "close"),
        position_id="position-1",
        quantity=Decimal("0.0001"),
        submitted_at_ms=NOW + 3_000,
        now_ms=NOW + 3_000,
    )
    _observe_close(
        repos,
        first,
        position_id="position-1",
        avg_exit_price=Decimal("60001"),
        closed_at_ms=NOW + 4_000,
    )
    assert repos.trading.record_closed_flat(
        first.intent_id,
        position_id="position-1",
        authoritative_quantity=Decimal("0"),
        avg_exit_price=Decimal("60001"),
        closed_at_ms=NOW + 4_000,
        flat_verified_at_ms=NOW + 4_000,
        realized_pnl_amount=None,
        realized_pnl_currency=None,
        commissions_by_currency={},
        now_ms=NOW + 4_000,
    )
    _case_without_reset(conn, case_id="case-intent-2")
    second = _intent(case_id="case-intent-2", case_manifest_sha256="3" * 64)
    assert repos.trading.insert_intent(second) is True
    conn.commit()

    granted = repos.trading.fence_entry(second.intent_id, engine_identity="nt-1", now_ms=NOW + 5_000)
    assert granted.disposition == "GRANTED"
    conn.commit()


def test_database_allows_only_one_nonterminal_intent_globally(conn: Any) -> None:
    _case(conn, case_id="case-intent-1")
    first = _intent()
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(first) is True
    conn.commit()

    _case_without_reset(conn, case_id="case-intent-2")
    second = _intent(case_id="case-intent-2", case_manifest_sha256="3" * 64)
    with pytest.raises(UniqueViolation):
        repos.trading.insert_intent(second)
    conn.rollback()


def test_runtime_roles_enforce_the_intent_column_ownership_boundary(conn: Any) -> None:
    privileges = dict(
        conn.execute(
            """
            SELECT
              has_table_privilege('tracefold_workers', 'trading_intents', 'SELECT') AS workers_select,
              has_table_privilege('tracefold_workers', 'trading_intents', 'INSERT') AS workers_table_insert,
              has_column_privilege('tracefold_workers', 'trading_intents', 'case_id', 'INSERT')
                AS workers_intent_insert,
              has_column_privilege('tracefold_workers', 'trading_intents', 'execution_state', 'INSERT')
                AS workers_execution_insert,
              has_table_privilege('tracefold_workers', 'trading_intents', 'UPDATE') AS workers_update,
              has_column_privilege('tracefold_workers', 'trading_intents', 'execution_state', 'UPDATE')
                AS workers_execution_update,
              has_table_privilege('tracefold_nautilus', 'trading_intents', 'SELECT') AS nautilus_select,
              has_table_privilege('tracefold_nautilus', 'trading_intents', 'INSERT') AS nautilus_insert,
              has_table_privilege('tracefold_nautilus', 'trading_intents', 'UPDATE') AS nautilus_table_update,
              has_column_privilege('tracefold_nautilus', 'trading_intents', 'execution_state', 'UPDATE')
                AS nautilus_execution_update,
              has_column_privilege('tracefold_nautilus', 'trading_intents', 'case_id', 'UPDATE')
                AS nautilus_identity_update,
              has_table_privilege('tracefold_nautilus', 'trading_cases', 'UPDATE') AS nautilus_case_update,
              has_column_privilege('tracefold_nautilus', 'trading_runtime_state', 'id', 'SELECT')
                AS nautilus_runtime_id_select,
              has_column_privilege('tracefold_nautilus', 'trading_runtime_state', 'control', 'SELECT')
                AS nautilus_control_select,
              has_column_privilege('tracefold_nautilus', 'trading_runtime_state', 'orders_today', 'SELECT')
                AS nautilus_counter_select,
              has_table_privilege('tracefold_nautilus', 'trading_symbol_blacklist', 'DELETE')
                AS nautilus_blacklist_delete,
              has_function_privilege(
                'tracefold_nautilus', 'materialize_trading_blacklist_expiry()', 'EXECUTE'
              ) AS nautilus_expiry_execute,
              has_function_privilege(
                'tracefold_workers', 'materialize_trading_blacklist_expiry()', 'EXECUTE'
              ) AS workers_expiry_execute,
              has_function_privilege(
                'tracefold_serve', 'materialize_trading_blacklist_expiry()', 'EXECUTE'
              ) AS serve_expiry_execute,
              has_table_privilege('tracefold_serve', 'trading_intents', 'SELECT') AS serve_select,
              has_table_privilege('tracefold_serve', 'trading_intents', 'INSERT') AS serve_insert
            """
        ).fetchone()
    )

    assert privileges == {
        "workers_select": True,
        "workers_table_insert": False,
        "workers_intent_insert": True,
        "workers_execution_insert": False,
        "workers_update": False,
        "workers_execution_update": False,
        "nautilus_select": True,
        "nautilus_insert": False,
        "nautilus_table_update": False,
        "nautilus_execution_update": True,
        "nautilus_identity_update": False,
        "nautilus_case_update": False,
        "nautilus_runtime_id_select": True,
        "nautilus_control_select": True,
        "nautilus_counter_select": False,
        "nautilus_blacklist_delete": False,
        "nautilus_expiry_execute": True,
        "workers_expiry_execute": True,
        "serve_expiry_execute": False,
        "serve_select": True,
        "serve_insert": False,
    }


def test_real_nautilus_role_can_poll_fence_and_heartbeat_but_not_read_trading_counters(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    conn.commit()

    conn.execute("SET ROLE tracefold_nautilus")
    active = repos.trading.active_intent()
    assert active is not None and active[0] == intent
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000).granted
    repos.trading.set_nautilus_runtime(
        heartbeat_at_ms=NOW + 1_000,
        ready=True,
        readiness_reason="ready",
        unexpected_exposure=False,
        now_ms=NOW + 1_000,
    )
    repos.trading.set_nautilus_bootstrap_account_zero(
        verified_at_ms=None,
        now_ms=NOW + 1_000,
        expected_capability_snapshot_sha256=CAPABILITY_SNAPSHOT.snapshot_sha256,
    )
    assert repos.trading.nautilus_runtime_state(for_update=True) is not None
    conn.commit()

    with pytest.raises(InsufficientPrivilege):
        repos.trading.runtime_state()
    conn.rollback()
    conn.execute("RESET ROLE")
    conn.commit()


def test_unfenced_expiry_is_terminal_and_releases_the_single_active_slot(conn: Any) -> None:
    _case(conn, case_id="case-intent-1")
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    conn.commit()

    expired = repos.trading.expire_unfenced_intent(intent.intent_id, now_ms=intent.valid_until_ms)
    conn.commit()

    assert expired is not None
    assert (expired.execution_state, expired.terminal_outcome, expired.reason_code) == (
        "TERMINAL",
        "EXPIRED",
        "intent_expired",
    )
    assert expired.entry_fenced_at_ms is None

    _case_without_reset(conn, case_id="case-intent-2")
    assert repos.trading.insert_intent(_intent(case_id="case-intent-2", case_manifest_sha256="3" * 64)) is True
    conn.commit()


def test_authoritative_refusal_before_submit_is_rejected_without_exposure(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    conn.commit()

    rejected = repos.trading.record_rejected_without_exposure(
        intent.intent_id,
        reason_code="risk_denied",
        authoritative_quantity=Decimal("0"),
        entry_client_order_id=None,
        now_ms=NOW + 1_000,
    )
    conn.commit()

    assert rejected is not None
    assert (rejected.execution_state, rejected.terminal_outcome, rejected.reason_code) == (
        "TERMINAL",
        "REJECTED",
        "risk_denied",
    )
    assert rejected.entry_fenced_at_ms is None


def test_fenced_authoritative_rejection_requires_the_exact_entry_and_zero_exposure(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    fenced = repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000).outcome
    conn.commit()
    assert fenced is not None and fenced.entry_client_order_id is not None

    assert (
        repos.trading.record_rejected_without_exposure(
            intent.intent_id,
            reason_code="risk_denied",
            authoritative_quantity=Decimal("0.0001"),
            entry_client_order_id=fenced.entry_client_order_id,
            now_ms=NOW + 2_000,
        )
        is None
    )
    rejected = repos.trading.record_rejected_without_exposure(
        intent.intent_id,
        reason_code="risk_denied",
        authoritative_quantity=Decimal("0"),
        entry_client_order_id=fenced.entry_client_order_id,
        now_ms=NOW + 3_000,
    )
    conn.commit()

    assert rejected is not None
    assert (rejected.execution_state, rejected.terminal_outcome) == ("TERMINAL", "REJECTED")
    assert rejected.flat_verified_at_ms == NOW + 3_000


def test_unknown_entry_outcome_becomes_manual_review_and_never_a_rejection(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    conn.commit()
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000).granted
    conn.commit()

    review = repos.trading.mark_manual_review(
        intent.intent_id,
        reason_code="entry_outcome_unknown",
        now_ms=NOW + 2_000,
    )
    conn.commit()

    assert review is not None
    assert review.execution_state == "MANUAL_REVIEW"
    assert review.terminal_outcome is None
    assert review.reason_code == "entry_outcome_unknown"


def test_manual_review_only_follows_a_fence_and_authoritative_facts_resume_automation(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    assert (
        repos.trading.mark_manual_review(
            intent.intent_id,
            reason_code="entry_outcome_unknown",
            now_ms=NOW + 500,
        )
        is None
    )
    _allow_entry(conn)
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000).granted
    assert repos.trading.mark_manual_review(
        intent.intent_id,
        reason_code="entry_outcome_unknown",
        now_ms=NOW + 1_100,
    )
    conn.commit()

    recovered_fill = repos.trading.record_entry_fill(
        intent.intent_id,
        actual_quantity=Decimal("0.0001"),
        avg_entry_price=Decimal("60000"),
        position_id="position-1",
        opened_at_ms=NOW + 1_200,
        now_ms=NOW + 1_300,
    )

    assert recovered_fill is not None
    assert (recovered_fill.execution_state, recovered_fill.execution_phase) == ("IN_FLIGHT", "PROTECTION")
    assert recovered_fill.reason_code is None


def test_manual_protection_and_close_unknowns_converge_only_from_authoritative_facts(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000).granted
    assert repos.trading.record_entry_fill(
        intent.intent_id,
        actual_quantity=Decimal("0.0001"),
        avg_entry_price=Decimal("60000"),
        position_id="position-1",
        opened_at_ms=NOW + 1_100,
        now_ms=NOW + 1_100,
    )
    stop_id = deterministic_client_order_id(intent.intent_id, "stop")
    stop = repos.trading.record_stop_submitted(
        intent.intent_id,
        client_order_id=stop_id,
        generation=0,
        previous_client_order_id=None,
        quantity=Decimal("0.0001"),
        now_ms=NOW + 1_200,
    )
    assert stop is not None and stop.stop_client_order_id is not None
    assert repos.trading.mark_manual_review(
        intent.intent_id,
        reason_code="protection_unproven",
        now_ms=NOW + 1_300,
    )
    recovered_protection = repos.trading.record_protected(
        intent.intent_id,
        accepted_client_order_id=stop.stop_client_order_id,
        protection_order_id="venue-stop-1",
        protected_quantity=Decimal("0.0001"),
        stop_price=Decimal("58800"),
        protected_at_ms=NOW + 1_400,
        now_ms=NOW + 1_400,
    )
    assert recovered_protection is not None
    assert recovered_protection.execution_state == "OPEN_PROTECTED"
    assert recovered_protection.reason_code is None

    assert repos.trading.record_close_submitted(
        intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        position_id="position-1",
        quantity=Decimal("0.0001"),
        submitted_at_ms=NOW + 2_000,
        now_ms=NOW + 2_000,
    )
    assert repos.trading.mark_manual_review(
        intent.intent_id,
        reason_code="close_outcome_unknown",
        now_ms=NOW + 2_100,
    )
    _observe_close(
        repos,
        intent,
        position_id="position-1",
        avg_exit_price=Decimal("60010"),
        closed_at_ms=NOW + 2_200,
    )
    closed = repos.trading.record_closed_flat(
        intent.intent_id,
        position_id="position-1",
        authoritative_quantity=Decimal("0"),
        avg_exit_price=Decimal("60010"),
        closed_at_ms=NOW + 2_200,
        flat_verified_at_ms=NOW + 2_300,
        realized_pnl_amount=None,
        realized_pnl_currency=None,
        commissions_by_currency={},
        now_ms=NOW + 2_300,
    )
    conn.commit()
    assert closed is not None
    assert (closed.execution_state, closed.terminal_outcome, closed.reason_code) == (
        "TERMINAL",
        "CLOSED_FLAT",
        None,
    )


def test_nautilus_poll_and_runtime_heartbeat_share_the_business_row(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    repos.trading.set_nautilus_runtime(
        heartbeat_at_ms=NOW + 500,
        ready=True,
        readiness_reason="ready",
        unexpected_exposure=False,
        now_ms=NOW + 500,
    )
    conn.commit()

    active = repos.trading.active_intent()
    runtime = repos.trading.runtime_state()

    assert active is not None
    assert active[0] == intent
    assert active[1].execution_state == "PENDING"
    assert runtime is not None
    assert runtime["nautilus_heartbeat_at_ms"] == NOW + 500
    assert runtime["nautilus_ready"] is True
    assert runtime["nautilus_readiness_reason"] == "ready"
    assert runtime["nautilus_unexpected_exposure"] is False


def test_the_case_projection_links_its_intent_without_joining_the_execution_lifecycle(conn: Any) -> None:
    """#331: the Case aggregate answers with one nullable `intent_id`, never with an execution state."""

    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    conn.commit()

    rows = repos.trading.console_cases(since_ms=NOW - 1, limit=10)

    row = next(item for item in rows if item["case_id"] == "case-intent-1")
    assert row["intent_id"] == intent.intent_id
    assert row["state"] == "RUNNING"
    assert row["strategy_id"] == "binance_oi_smart_money_long_v2"
    for retired in ("execution_state", "terminal_outcome", "reason_code", "intent_version", "mode", "regime"):
        assert retired not in row


def _case_without_reset(connection: Any, *, case_id: str) -> None:
    connection.execute("UPDATE trading_cases SET state = 'ORDER_PREPARED' WHERE state = 'RUNNING'")
    connection.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
          strategy_config_digest, primary_source_key, supplemental_source_keys,
          manifest, manifest_sha256, state, policy_decision, policy_reason,
          capital_disposition, capital_reason, observed_at_ms, created_at_ms, updated_at_ms
        ) VALUES (%s, 'crypto:BTC', 'oi', 'binance_oi_smart_money_long_v2',
                  'binance_oi_smart_money_long_v2', %s, %s, '[]'::jsonb,
                  '{}'::jsonb, %s, 'RUNNING', 'not_run', 'not_run',
                  'not_applicable', NULL, %s, %s, %s)
        """,
        (case_id, "0" * 64, f"source-{case_id}", "3" * 64, NOW, NOW, NOW),
    )
    connection.commit()


def test_fake_execution_closes_the_five_state_demo_loop_without_a_second_entry(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    conn.commit()

    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000).granted
    conn.commit()
    protection = repos.trading.record_entry_fill(
        intent.intent_id,
        actual_quantity=Decimal("100"),
        avg_entry_price=Decimal("0.1"),
        position_id="position-1",
        opened_at_ms=NOW + 1_100,
        now_ms=NOW + 1_200,
    )
    assert protection is not None
    assert (protection.execution_state, protection.execution_phase) == ("IN_FLIGHT", "PROTECTION")

    submitted_stop = repos.trading.record_stop_submitted(
        intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "stop"),
        generation=0,
        previous_client_order_id=None,
        quantity=Decimal("100"),
        now_ms=NOW + 1_300,
    )
    assert submitted_stop is not None
    assert submitted_stop.stop_client_order_id == deterministic_client_order_id(intent.intent_id, "stop")
    protected = repos.trading.record_protected(
        intent.intent_id,
        accepted_client_order_id=submitted_stop.stop_client_order_id,
        protection_order_id="stop-venue-1",
        protected_quantity=Decimal("100"),
        stop_price=Decimal("0.098"),
        protected_at_ms=NOW + 1_400,
        now_ms=NOW + 1_400,
    )
    conn.commit()
    assert protected is not None
    assert (protected.execution_state, protected.execution_phase) == ("OPEN_PROTECTED", "PROTECTION")

    close_id = deterministic_client_order_id(intent.intent_id, "close")
    with pytest.raises(ValueError, match="close_identity_invalid"):
        repos.trading.record_close_submitted(
            intent.intent_id,
            client_order_id="tf-c-wrong",
            position_id="position-1",
            quantity=Decimal("100"),
            submitted_at_ms=NOW + 2_000,
            now_ms=NOW + 2_000,
        )
    assert (
        repos.trading.record_close_submitted(
            intent.intent_id,
            client_order_id=close_id,
            position_id="wrong-position",
            quantity=Decimal("100"),
            submitted_at_ms=NOW + 2_000,
            now_ms=NOW + 2_000,
        )
        is None
    )
    exiting = repos.trading.record_close_submitted(
        intent.intent_id,
        client_order_id=close_id,
        position_id="position-1",
        quantity=Decimal("100"),
        submitted_at_ms=NOW + 2_000,
        now_ms=NOW + 2_000,
    )
    assert exiting is not None
    assert exiting.close_client_order_id == deterministic_client_order_id(intent.intent_id, "close")
    _observe_close(
        repos,
        intent,
        position_id="position-1",
        avg_exit_price=Decimal("0.101"),
        closed_at_ms=NOW + 2_100,
        realized_pnl_amount=Decimal("0.10"),
        realized_pnl_currency="USDT",
        commissions_by_currency={"USDT": "0.02"},
    )
    recovered_unknown = repos.trading.record_position_closed_observed(
        intent.intent_id,
        instrument_id=intent.instrument_id,
        account_id="BINANCE-001",
        position_id="position-1",
        closing_client_order_id=close_id,
        local_quantity=Decimal(0),
        avg_exit_price=Decimal("0.101"),
        closed_at_ms=NOW + 2_100,
        realized_pnl_amount=Decimal("0.10"),
        realized_pnl_currency="USDT",
        commissions_by_currency=None,
        now_ms=NOW + 2_150,
    )
    assert recovered_unknown is not None
    assert recovered_unknown.commissions_by_currency == {"USDT": "0.02"}
    unverified_latency = repos.trading.stage_latency_ms(since_ms=NOW - 1)
    assert unverified_latency["position_opened_to_closed_flat"] == {"n": 0}
    assert (
        repos.trading.record_closed_flat(
            intent.intent_id,
            position_id="position-1",
            authoritative_quantity=Decimal("0.0001"),
            avg_exit_price=Decimal("0.101"),
            closed_at_ms=NOW + 2_100,
            flat_verified_at_ms=NOW + 2_200,
            realized_pnl_amount=Decimal("0.10"),
            realized_pnl_currency="USDT",
            commissions_by_currency=None,
            now_ms=NOW + 2_200,
        )
        is None
    )
    closed = repos.trading.record_closed_flat(
        intent.intent_id,
        position_id="position-1",
        authoritative_quantity=Decimal("0"),
        avg_exit_price=Decimal("0.101"),
        closed_at_ms=NOW + 2_100,
        flat_verified_at_ms=NOW + 2_200,
        realized_pnl_amount=Decimal("0.10"),
        realized_pnl_currency="USDT",
        commissions_by_currency=None,
        now_ms=NOW + 2_200,
    )
    conn.commit()

    assert closed is not None
    assert (closed.execution_state, closed.execution_phase, closed.terminal_outcome) == (
        "TERMINAL",
        "EXIT",
        "CLOSED_FLAT",
    )
    assert closed.flat_verified_at_ms == NOW + 2_200
    assert closed.commissions_by_currency == {"USDT": "0.02"}
    verified_latency = repos.trading.stage_latency_ms(since_ms=NOW - 1)
    assert verified_latency["position_opened_to_closed_flat"] == {"n": 1, "p50": 1_100, "p95": 1_100}
    assert not repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 3_000).granted


def test_position_change_uses_a_new_deterministic_stop_generation(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    conn.commit()
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000).granted
    assert repos.trading.record_entry_fill(
        intent.intent_id,
        actual_quantity=Decimal("100"),
        avg_entry_price=Decimal("0.1"),
        position_id="position-1",
        opened_at_ms=NOW + 1_100,
        now_ms=NOW + 1_100,
    )
    first = repos.trading.record_stop_submitted(
        intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "stop"),
        generation=0,
        previous_client_order_id=None,
        quantity=Decimal("100"),
        now_ms=NOW + 1_200,
    )
    assert first is not None
    assert first.stop_generation == 0
    assert first.stop_client_order_id is not None
    assert len(first.stop_client_order_id) <= 36
    assert repos.trading.record_protected(
        intent.intent_id,
        accepted_client_order_id=first.stop_client_order_id,
        protection_order_id="stop-venue-1",
        protected_quantity=Decimal("100"),
        stop_price=Decimal("0.098"),
        protected_at_ms=NOW + 1_300,
        now_ms=NOW + 1_300,
    )
    conn.commit()

    repriced = repos.trading.record_position_changed(
        intent.intent_id,
        position_id="position-1",
        actual_quantity=Decimal("100"),
        avg_entry_price=Decimal("0.101"),
        now_ms=NOW + 1_350,
    )
    assert repriced is not None
    assert repriced.avg_entry_price == Decimal("0.101")

    changed = repos.trading.record_position_changed(
        intent.intent_id,
        position_id="position-1",
        actual_quantity=Decimal("80"),
        avg_entry_price=Decimal("0.101"),
        now_ms=NOW + 1_400,
    )
    assert changed is not None
    assert (changed.execution_state, changed.execution_phase) == ("IN_FLIGHT", "PROTECTION")
    assert changed.protected_quantity == Decimal("100")
    assert changed.avg_entry_price == Decimal("0.101")
    latest = repos.trading.record_position_changed(
        intent.intent_id,
        position_id="position-1",
        actual_quantity=Decimal("60"),
        avg_entry_price=Decimal("0.102"),
        now_ms=NOW + 1_450,
    )
    assert latest is not None
    assert latest.actual_quantity == Decimal("60")
    assert latest.avg_entry_price == Decimal("0.102")

    replacement = repos.trading.prepare_stop_replacement(
        intent.intent_id,
        canceled_client_order_id=first.stop_client_order_id,
        submitted_client_order_id=deterministic_client_order_id(
            intent.intent_id,
            "stop",
            previous_client_order_id=first.stop_client_order_id,
        ),
        generation=1,
        quantity=Decimal("60"),
        now_ms=NOW + 1_500,
    )
    assert replacement is not None
    assert replacement.stop_generation == 1
    assert replacement.stop_client_order_id != first.stop_client_order_id
    assert len(replacement.stop_client_order_id or "") <= 36
    assert (
        repos.trading.record_protected(
            intent.intent_id,
            accepted_client_order_id=first.stop_client_order_id,
            protection_order_id="stale-stop-venue",
            protected_quantity=Decimal("60"),
            stop_price=Decimal("0.098"),
            protected_at_ms=NOW + 1_550,
            now_ms=NOW + 1_550,
        )
        is None
    )
    protected = repos.trading.record_protected(
        intent.intent_id,
        accepted_client_order_id=replacement.stop_client_order_id,
        protection_order_id="stop-venue-2",
        protected_quantity=Decimal("60"),
        stop_price=Decimal("0.098"),
        protected_at_ms=NOW + 1_600,
        now_ms=NOW + 1_600,
    )
    conn.commit()

    assert protected is not None
    assert protected.execution_state == "OPEN_PROTECTED"
    assert protected.protected_quantity == protected.actual_quantity == Decimal("60")

    assert repos.trading.record_close_submitted(
        intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        position_id="position-1",
        quantity=Decimal("60"),
        submitted_at_ms=NOW + 1_700,
        now_ms=NOW + 1_700,
    )
    exit_changed = repos.trading.record_position_changed(
        intent.intent_id,
        position_id="position-1",
        actual_quantity=Decimal("40"),
        avg_entry_price=Decimal("0.102"),
        now_ms=NOW + 1_800,
    )
    assert exit_changed is not None
    assert (exit_changed.execution_state, exit_changed.execution_phase) == ("IN_FLIGHT", "EXIT")

    exit_replacement_id = deterministic_client_order_id(
        intent.intent_id,
        "stop",
        previous_client_order_id=replacement.stop_client_order_id,
    )
    exit_replacement = repos.trading.prepare_stop_replacement(
        intent.intent_id,
        canceled_client_order_id=replacement.stop_client_order_id,
        submitted_client_order_id=exit_replacement_id,
        generation=2,
        quantity=Decimal("40"),
        now_ms=NOW + 1_900,
    )
    assert exit_replacement is not None
    exit_protected = repos.trading.record_protected(
        intent.intent_id,
        accepted_client_order_id=exit_replacement_id,
        protection_order_id="stop-venue-3",
        protected_quantity=Decimal("40"),
        stop_price=Decimal("0.098"),
        protected_at_ms=NOW + 2_000,
        now_ms=NOW + 2_000,
    )
    conn.commit()

    assert exit_protected is not None
    assert (exit_protected.execution_state, exit_protected.execution_phase) == ("IN_FLIGHT", "EXIT")
    assert exit_protected.protected_quantity == exit_protected.actual_quantity == Decimal("40")
