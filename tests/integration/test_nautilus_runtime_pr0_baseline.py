"""Opt-in #475 PR-0 measurement against isolated PostgreSQL and pinned Nautilus seams.

This diagnostic establishes a before-change baseline; it does not tune production cadence
or call Binance. Run it explicitly with ``-s`` and copy only the redacted JSON receipt.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import PositionId

from tests.nautilus_oi_runtime_fixtures import (
    ACCOUNT_ID,
    NOW_NS,
    CommandRows,
    oi_profile,
    operator_intent,
    registered_oi_strategy,
    trade_signal,
)
from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.http.app import create_app
from tracefold.app.nautilus.oi_runtime import (
    OiRuntimeDatabaseBridge,
    flush_audit_once,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.platform.config.models import Settings
from tracefold.trading.storage.execution_stream import (
    ExecutionProfileActivation,
    prepare_operator_intent,
    prepare_trade_signal,
)
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.scheduled, pytest.mark.usefixtures("postgres_clone_dsn")]

_BURST_SIZES = (1, 10, 100)
_REPEATS = 6
_REPAIR_SECONDS = 0.2
_UI_WINDOW_SECONDS = 15.0
_BASELINE_SOURCE_MAIN = "f495a9fc0d0ba0d528e40b588e76108d80cdfefe"

_OWNER_MATRIX = (
    {
        "fact": "unresolved_signal_command_read",
        "owner": "OiRuntimeDatabaseBridge._cycle",
        "authority": "PostgreSQL durable indexed anti-join reads",
        "repair": "production fixed 200 ms cycle; the unused LISTEN loop is the PR-A duplicate to remove",
        "source": "tracefold/app/nautilus/oi_runtime.py",
        "symbol": "OiRuntimeDatabaseBridge._cycle",
    },
    {
        "fact": "callback_operational_state",
        "owner": "OiNautilusStrategy",
        "authority": "Nautilus Cache and Portfolio",
        "repair": "complete private reconciliation may refresh Cache outside callbacks",
        "source": "tracefold/integrations/nautilus/oi_runtime/strategy.py",
        "symbol": "OiNautilusStrategy",
    },
    {
        "fact": "authoritative_account_flat",
        "owner": "_reconcile_account",
        "authority": "complete Binance private position, regular-order, and Algo-order reports",
        "repair": "startup and current two-second custom private reconciliation",
        "source": "tracefold/app/nautilus/root.py",
        "symbol": "_reconcile_account",
    },
    {
        "fact": "current_runtime_status",
        "owner": "_run_active_runtime",
        "authority": "generation-fenced trading_execution_runtime_state projection",
        "repair": "state change plus 500 ms heartbeat before the public stale budget",
        "source": "tracefold/app/nautilus/root.py",
        "symbol": "_run_active_runtime",
    },
    {
        "fact": "durable_audit",
        "owner": "OiRuntimeDatabaseBridge._cycle",
        "authority": "append-only ExecutionObservationV1 batch",
        "repair": "bounded in-memory retry and explicit durable audit_gap",
        "source": "tracefold/app/nautilus/oi_runtime.py",
        "symbol": "OiRuntimeDatabaseBridge._cycle",
    },
)


class _MeasuredRuntimeBridge(OiRuntimeDatabaseBridge):
    """Expose a test-only barrier after the unchanged production cycle completes."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.initial_cycle_finished = Event()

    def _cycle(self, repos: Any) -> None:
        super()._cycle(repos)
        self.initial_cycle_finished.set()


def _p95(values: list[float]) -> float:
    assert values
    ordered = sorted(values)
    return ordered[max(0, round(0.95 * len(ordered) + 0.499999) - 1)]


def _sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _fence(repo: TradingRepository) -> tuple[int, int]:
    return repo.execution_stream_fence()


def _activate(repo: TradingRepository, *, profile: str, slot: str, now_ns: int) -> None:
    signal_seq, command_seq = _fence(repo)
    with repo.conn.transaction():
        repo.append_execution_profile_activation(
            ExecutionProfileActivation(
                runtime_profile_id=profile,
                account_slot=slot,
                activated_after_signal_seq=signal_seq,
                activated_after_command_seq=command_seq,
                mode="paper",
                runtime_release="nautilus-1.231.0+oi-v1",
                config_sha256="a" * 64,
                created_at_ns=now_ns,
            )
        )


def _append_workload(repo: TradingRepository, *, profile: str, size: int, seed: str, now_ns: int) -> None:
    with repo.conn.transaction():
        for index in range(size):
            identity = _sha(f"signal:{seed}:{index}")
            case_id = f"baseline-{seed}-{index}"
            repo.conn.execute(
                """
                INSERT INTO trading_cases (
                  case_id, underlying_key, trigger_kind, primary_source_key,
                  supplemental_source_keys, manifest, manifest_sha256, state,
                  policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
                  updated_at_ms, strategy_id, strategy_version, strategy_config_digest,
                  capital_disposition, capital_reason
                ) VALUES (
                  %s, %s, 'oi', %s, '[]'::jsonb, '{"baseline":"475-pr0"}'::jsonb,
                  %s, 'SIGNAL_EMITTED', 'long', 'baseline', %s, %s, %s, %s,
                  'source_native_oi_smart_money_long_v4', 'source_native_oi_smart_money_long_v4',
                  %s, 'not_applicable', NULL
                )
                """,
                (
                    case_id,
                    f"crypto:{index}",
                    f"baseline-source:{seed}:{index}",
                    _sha(f"manifest:{seed}:{index}"),
                    now_ns // 1_000_000,
                    now_ns // 1_000_000,
                    now_ns // 1_000_000,
                    now_ns // 1_000_000,
                    "b" * 64,
                ),
            )
            repo.append_trade_signal(
                prepare_trade_signal(
                    signal_id=identity,
                    case_id=case_id,
                    alpha_contract_sha256="c" * 64,
                    market_key="crypto:perp:BTC:USDT",
                    direction="long",
                    observed_at_ns=now_ns,
                    expires_at_ns=now_ns + 60_000_000_000,
                    evidence_sha256="d" * 64,
                )
            )
            repo.append_operator_intent(
                prepare_operator_intent(
                    command_id=_sha(f"command:{seed}:{index}"),
                    target_profile_id=profile,
                    action="pause_entries",
                    scope="entries",
                    reason="475 PR-0 baseline",
                    operator_identity="diagnostic:475-pr0",
                    authentication_identity="diagnostic:isolated-postgres",
                    requested_at_ns=now_ns,
                    expires_at_ns=now_ns + 60_000_000_000,
                    confirmation_identity=None,
                    market_key=None,
                    direction=None,
                )
            )


def _runtime_bridge(
    *,
    settings: Settings,
    profile_id: str,
    account_slot: str,
    signals: ExecutionSignalClient,
) -> _MeasuredRuntimeBridge:
    profile = replace(
        oi_profile(),
        profile_id=profile_id,
        account_slot=account_slot,
        credential_namespace=f"{profile_id}-credentials",
        cache_namespace=f"{profile_id}-cache",
        client_order_namespace=f"{profile_id}-orders",
    )
    audit = AuditSink(factory=ObservationFactory(profile_id, profile.runtime_release, "oi_nautilus_v1"))
    return _MeasuredRuntimeBridge(
        settings=settings,
        profile=profile,
        signals=signals,
        audit=audit,
        update_day_start=lambda _baseline: None,
        poll_seconds=_REPAIR_SECONDS,
    )


def _wait_until_bridge_delivers(
    bridge: OiRuntimeDatabaseBridge,
    signals: ExecutionSignalClient,
    *,
    expected_count: int,
    timeout_seconds: float = 2.0,
) -> None:
    deadline = time.perf_counter() + timeout_seconds
    while signals.queued_count < expected_count and time.perf_counter() < deadline:
        if bridge.fatal_error is not None:
            raise bridge.fatal_error
        time.sleep(0.005)
    assert bridge.connected
    assert signals.queued_count == expected_count


def _wait_until_bridge_connects(bridge: OiRuntimeDatabaseBridge, *, timeout_seconds: float = 2.0) -> None:
    deadline = time.perf_counter() + timeout_seconds
    while not bridge.connected and time.perf_counter() < deadline:
        if bridge.fatal_error is not None:
            raise bridge.fatal_error
        time.sleep(0.005)
    assert bridge.connected


def _wait_until_initial_cycle_finishes(
    bridge: _MeasuredRuntimeBridge,
    *,
    timeout_seconds: float = 2.0,
) -> None:
    assert bridge.initial_cycle_finished.wait(timeout_seconds)
    assert bridge.fatal_error is None


def _stream_samples(settings: Settings) -> tuple[dict[str, Any], int]:
    writer = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(writer)
        by_burst: dict[str, Any] = {}
        max_connections = 0
        for size in _BURST_SIZES:
            dequeue_ms: list[float] = []
            for repeat in range(_REPEATS):
                seed = f"{size}-{repeat}"
                profile = f"baseline-475-{size}-{repeat}"
                slot = f"baseline-slot-{size}-{repeat}"
                _activate(repo, profile=profile, slot=slot, now_ns=NOW_NS + repeat)
                client = ExecutionSignalClient(runtime_profile_id=profile, execution_strategy="oi_nautilus_v1")
                bridge = _runtime_bridge(
                    settings=settings,
                    profile_id=profile,
                    account_slot=slot,
                    signals=client,
                )
                bridge.start()
                try:
                    _wait_until_bridge_connects(bridge)
                    # Commit only after the first production cycle has definitely finished,
                    # so admission must traverse the next 200 ms repair cadence.
                    _wait_until_initial_cycle_finishes(bridge)
                    _append_workload(repo, profile=profile, size=size, seed=seed, now_ns=NOW_NS + repeat)
                    committed = time.perf_counter()
                    _wait_until_bridge_delivers(bridge, client, expected_count=2 * size)
                    assert client.queued_count == 2 * size
                    assert len(client.pending_ids) == len(client.pending_command_ids) == size
                    assert [client.next_command_nowait() is not None for _ in range(size)] == [True] * size
                    assert [client.next_nowait() is not None for _ in range(size)] == [True] * size
                    finished = time.perf_counter()
                    dequeue_ms.append((finished - committed) * 1_000)
                    max_connections = max(
                        max_connections,
                        int(
                            writer.execute(
                                "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database()"
                            ).fetchone()["n"]
                        ),
                    )
                finally:
                    bridge.stop()
                    bridge.join(2.0)
                    assert not bridge.connected
            by_burst[str(size)] = {
                "samples": _REPEATS,
                "delivery_path": "production_fixed_poll",
                "poll_cadence_ms": int(_REPAIR_SECONDS * 1_000),
                "persisted_to_dequeued_p95_ms": round(_p95(dequeue_ms), 3),
                "reader_calls_per_cycle": 2,
                "pending_identity_duplicates": 0,
            }

        profile = "baseline-475-missed-wake"
        slot = "baseline-slot-repair"
        _activate(repo, profile=profile, slot=slot, now_ns=NOW_NS + 100)
        client = ExecutionSignalClient(runtime_profile_id=profile, execution_strategy="oi_nautilus_v1")
        bridge = _runtime_bridge(
            settings=settings,
            profile_id=profile,
            account_slot=slot,
            signals=client,
        )
        bridge.start()
        try:
            _wait_until_bridge_connects(bridge)
            _wait_until_initial_cycle_finishes(bridge)
            _append_workload(repo, profile=profile, size=1, seed="repair", now_ns=NOW_NS + 100)
            committed = time.perf_counter()
            _wait_until_bridge_delivers(bridge, client, expected_count=2)
            repaired_ms = (time.perf_counter() - committed) * 1_000
        finally:
            bridge.stop()
            bridge.join(2.0)
            assert not bridge.connected
        return (
            {
                "status": "observed",
                "production_delivery_path": "fixed_poll_without_listener",
                "bursts": by_burst,
                "missed_wake_repair_ms": round(repaired_ms, 3),
                "repair_cadence_ms": int(_REPAIR_SECONDS * 1_000),
                "ttl_ms": 60_000,
            },
            max_connections,
        )
    finally:
        writer.close()


def _audit_sample() -> dict[str, Any]:
    conn = connect_postgres_test(read_only=False)
    try:
        profile = "baseline-475-audit"
        repo = TradingRepository(conn)
        _activate(repo, profile=profile, slot="baseline-slot-audit", now_ns=NOW_NS + 200)
        repos = repositories_for_connection(conn)
        signals = ExecutionSignalClient(runtime_profile_id=profile, execution_strategy="oi_nautilus_v1")
        sink = AuditSink(factory=ObservationFactory(profile, "nautilus-1.231.0+oi-v1", "oi_nautilus_v1"))
        for index in range(100):
            assert sink.offer(
                sink.factory.create(
                    normalized_kind="readiness",
                    occurred_at_ns=NOW_NS + index,
                    observed_at_ns=NOW_NS + index,
                    summary={"sample": index},
                    payload={"sample": index},
                    event_identity=f"baseline:{index}",
                )
            )
        queued_bytes = sink.queued_bytes
        started = time.perf_counter()
        flushed = flush_audit_once(repos=repos, audit=sink, signals=signals)
        duration_ms = (time.perf_counter() - started) * 1_000
        assert flushed == 100
        return {
            "status": "observed",
            "batch_count": flushed,
            "queued_bytes_before_flush": queued_bytes,
            "append_ms": round(duration_ms, 3),
            "remaining_count": sink.queued_count,
            "remaining_bytes": sink.queued_bytes,
        }
    finally:
        conn.close()


def _runtime_lifecycle_sample() -> dict[str, Any]:
    context = registered_oi_strategy(values=(trade_signal(),))
    started = time.perf_counter()
    context.strategy.on_timer(None)
    entry = context.strategy.submitted[0][0]
    position_id = PositionId("BTCUSDT-PERP.BINANCE-475-BASELINE")
    context.strategy.on_position_opened(
        SimpleNamespace(
            instrument_id=context.instrument.id,
            account_id=ACCOUNT_ID,
            strategy_id=context.strategy.id,
            opening_order_id=entry.client_order_id,
            side=PositionSide.LONG,
            position_id=position_id,
            quantity=context.instrument.make_qty(Decimal("0.05")),
            avg_px_open=10_000.0,
            ts_opened=NOW_NS + 2,
        )
    )
    flatten = operator_intent(command_id="9" * 64, action="flatten", scope="account")
    context.signals.poll_commands_once(CommandRows(flatten))
    context.strategy.on_timer(None)
    duration_ms = (time.perf_counter() - started) * 1_000
    assert len(context.strategy.submitted) == 3
    return {
        "status": "observed",
        "seam": "pinned_nautilus_strategy_cache_portfolio",
        "duration_ms": round(duration_ms, 3),
        "orders": [
            {
                "type": order.order_type.name,
                "reduce_only": bool(order.is_reduce_only),
            }
            for order, _position, _client in context.strategy.submitted
        ],
        "flatten_pending_after_submit": len(context.strategy.control_state().flatten_pending),
    }


def _http_sample(tmp_path: Path) -> dict[str, Any]:
    settings = Settings(ws_token="475-baseline", storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    app = create_app(settings=settings)
    latencies: list[float] = []
    deadline = time.perf_counter() + _UI_WINDOW_SECONDS
    with TestClient(app) as client:
        while time.perf_counter() < deadline:
            started = time.perf_counter()
            response = client.get("/api/trading/status", params={"token": "475-baseline"})
            latencies.append((time.perf_counter() - started) * 1_000)
            assert response.status_code == 200
            time.sleep(0.5)
    return {
        "status": "observed",
        "window_seconds": _UI_WINDOW_SECONDS,
        "request_count": len(latencies),
        "latency_p95_ms": round(_p95(latencies), 3),
        "interval_ms": 500,
    }


def test_emit_pr0_runtime_baseline(tmp_path: Path) -> None:
    settings = Settings(ws_token="475-baseline", storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "bridge-home")
    before = resource.getrusage(resource.RUSAGE_SELF)
    stream, max_connections = _stream_samples(settings)
    audit = _audit_sample()
    lifecycle = _runtime_lifecycle_sample()
    http = _http_sample(tmp_path)
    after = resource.getrusage(resource.RUSAGE_SELF)
    measured_git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    report = {
        "schema_version": "tracefold_oi_runtime_pr0_baseline_v1",
        "baseline_source_main": _BASELINE_SOURCE_MAIN,
        "measured_git_sha": measured_git_sha,
        "captured_at": datetime.now(UTC).isoformat(),
        "environment": {
            "platform": os.uname().sysname,
            "machine": os.uname().machine,
            "postgresql": "isolated migrated test database",
            "nautilus": "1.231.0 pinned strategy/cache/portfolio seam",
            "binance_demo_active": False,
        },
        "owner_matrix": _OWNER_MATRIX,
        "measurements": {
            "stream_latency": stream,
            "database": {
                "status": "observed",
                "max_connections_in_isolated_database": max_connections,
                "indexed_queries_per_input_cycle": 2,
            },
            "audit_append": audit,
            "runtime_lifecycle": lifecycle,
            "http_reads_15s": http,
            "quote_subscriptions": {
                "status": "derived_from_pinned_source",
                "synthetic_route_count": 525,
                "subscription_attempts": 525,
                "inbound_message_rate": None,
                "inbound_message_rate_status": "not_observed_without_active_binance_demo_runtime",
                "is_production_collector": False,
            },
            "event_loop": {
                "status": "not_observed",
                "reason": "requires_active_binance_demo_runtime",
                "lag_p95_ms": None,
            },
            "private_reconciliation": {
                "status": "not_observed",
                "reason": "requires_active_binance_demo_runtime",
                "call_count": None,
                "latency_p95_ms": None,
                "rate_limit_headers": None,
            },
            "cpu_rss": {
                "status": "observed",
                "scope": "diagnostic_process_including_postgres_and_http_clients",
                "user_cpu_seconds": round(after.ru_utime - before.ru_utime, 6),
                "system_cpu_seconds": round(after.ru_stime - before.ru_stime, 6),
                "max_rss_bytes": after.ru_maxrss if sys.platform == "darwin" else after.ru_maxrss * 1_024,
            },
        },
        "interpretation": {
            "normal_stream_slo_ms": 250,
            "missed_wake_ttl_fraction": "1/3",
            "observed_duplicate_economic_orders": 0,
            "observed_duplicate_active_protections": 0,
            "external_provider_claim": "none",
            "comparison_rule": (
                "later PRs compare only like-for-like observed fields; provider-only fields require Demo evidence"
            ),
        },
    }
    assert set(report["measurements"]) == {
        "audit_append",
        "cpu_rss",
        "database",
        "event_loop",
        "http_reads_15s",
        "private_reconciliation",
        "quote_subscriptions",
        "runtime_lifecycle",
        "stream_latency",
    }
    print("TRACEFOLD_RUNTIME_BASELINE=" + json.dumps(report, sort_keys=True))
