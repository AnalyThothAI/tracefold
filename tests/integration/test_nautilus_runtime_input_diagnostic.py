"""Opt-in #475 current Runtime measurement against PostgreSQL and pinned Nautilus seams.

The immutable PR-0 receipt remains under ``docs/research``. This scheduled diagnostic
measures the checked-out Runtime for like-for-like comparisons and never calls Binance.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Condition, Event
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

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
    RuntimeStateProjector,
    flush_audit_once,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.platform.config.models import Settings
from tracefold.trading.storage.execution_stream import (
    ExecutionProfileActivation,
    ExecutionRuntimeState,
    prepare_operator_intent,
    prepare_trade_signal,
)
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.scheduled, pytest.mark.usefixtures("postgres_clone_dsn")]

_BURST_SIZES = (1, 10, 100)
_REPEATS = 6
_REPAIR_SECONDS = 0.2
_UI_WINDOW_SECONDS = 15.0
_ROOT = Path(__file__).resolve().parents[2]
_PR0_BASELINE_RECEIPT = "docs/research/oi-runtime-pr0-baseline-2026-09-01.json"
_SCHEDULED_RECEIPT = _ROOT / "artifacts/scheduled/oi-runtime-input-diagnostic.json"

_OWNER_MATRIX = (
    {
        "fact": "unresolved_signal_command_read",
        "owner": "OiRuntimeDatabaseBridge._cycle",
        "authority": "PostgreSQL durable indexed anti-join reads",
        "repair": "the same Bridge owns LISTEN wake and bounded 200 ms indexed-query repair",
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
        "repair": "startup, explicit ambiguity/flatten wake, and five-second stale-budget refresh",
        "source": "tracefold/app/nautilus/root.py",
        "symbol": "_reconcile_account",
    },
    {
        "fact": "current_runtime_status",
        "owner": "RuntimeStateProjector",
        "authority": "generation-fenced trading_execution_runtime_state projection",
        "repair": "semantic change on the next bridge cycle plus 500 ms heartbeat before the public stale budget",
        "source": "tracefold/app/nautilus/oi_runtime.py",
        "symbol": "RuntimeStateProjector",
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
        self._cycle_condition = Condition()
        self._completed_cycles = 0

    @property
    def completed_cycles(self) -> int:
        with self._cycle_condition:
            return self._completed_cycles

    def _cycle(self, repos: Any) -> None:
        super()._cycle(repos)
        with self._cycle_condition:
            self._completed_cycles += 1
            self.initial_cycle_finished.set()
            self._cycle_condition.notify_all()

    def wait_for_cycle_after(self, completed_cycles: int, timeout_seconds: float) -> bool:
        with self._cycle_condition:
            return self._cycle_condition.wait_for(
                lambda: self._completed_cycles > completed_cycles,
                timeout=timeout_seconds,
            )


def _p95(values: list[float]) -> float:
    assert values
    ordered = sorted(values)
    return ordered[max(0, round(0.95 * len(ordered) + 0.499999) - 1)]


def _discard_wake(_conn: object, timeout_seconds: float) -> bool:
    time.sleep(timeout_seconds)
    return False


def _sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _require_clean_tracked_tree() -> str:
    measured_git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    worktree_status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if worktree_status:
        raise RuntimeError("oi_runtime_input_diagnostic_dirty_worktree")
    return measured_git_sha


def _current_rss_bytes() -> int:
    statm = Path("/proc/self/statm")
    if statm.is_file():
        resident_pages = int(statm.read_text(encoding="utf-8").split()[1])
        return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
    rss_kib = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return int(rss_kib) * 1_024


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


def _append_workload(
    repo: TradingRepository,
    *,
    profile: str,
    size: int,
    seed: str,
    now_ns: int,
    before_commit: Callable[[], None],
) -> None:
    with repo.conn.transaction():
        for index in range(size):
            identity = _sha(f"signal:{seed}:{index}")
            case_id = f"runtime-input-{seed}-{index}"
            repo.conn.execute(
                """
                INSERT INTO trading_cases (
                  case_id, underlying_key, trigger_kind, primary_source_key,
                  supplemental_source_keys, manifest, manifest_sha256, state,
                  policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
                  updated_at_ms, strategy_id, strategy_version, strategy_config_digest,
                  capital_disposition, capital_reason
                ) VALUES (
                  %s, %s, 'oi', %s, '[]'::jsonb, '{"diagnostic":"475-runtime-input"}'::jsonb,
                  %s, 'SIGNAL_EMITTED', 'long', 'runtime_input_diagnostic', %s, %s, %s, %s,
                  'source_native_oi_smart_money_long_v4', 'source_native_oi_smart_money_long_v4',
                  %s, 'not_applicable', NULL
                )
                """,
                (
                    case_id,
                    f"crypto:{index}",
                    f"runtime-input-source:{seed}:{index}",
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
                    reason="475 Runtime input diagnostic",
                    operator_identity="diagnostic:475-runtime-input",
                    authentication_identity="diagnostic:isolated-postgres",
                    requested_at_ns=now_ns,
                    expires_at_ns=now_ns + 60_000_000_000,
                    confirmation_identity=None,
                    market_key=None,
                    direction=None,
                )
            )
        before_commit()


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
        cache_namespace=f"{profile_id}-cache",
        client_order_namespace=f"{profile_id}-orders",
    )
    audit = AuditSink(factory=ObservationFactory(profile_id, profile.runtime_release, "oi_nautilus_v1"))
    singleton = AccountSlotSingleton(
        account_slot=account_slot,
        try_acquire=lambda _slot: True,
        release=lambda _slot: True,
        heartbeat=lambda: True,
    )
    assert singleton.acquire() is True
    activation = ExecutionProfileActivation(
        runtime_profile_id=profile_id,
        account_slot=account_slot,
        activated_after_signal_seq=0,
        activated_after_command_seq=0,
        mode="paper",
        runtime_release=profile.runtime_release,
        config_sha256="a" * 64,
        created_at_ns=NOW_NS,
    )
    return _MeasuredRuntimeBridge(
        settings=settings,
        profile=profile,
        signals=signals,
        audit=audit,
        update_day_start=lambda _baseline: None,
        singleton=singleton,
        # Nothing is ever offered here, so the projection step is the no-op this diagnostic wants:
        # it measures the input path, not the current-state path.
        projector=RuntimeStateProjector(
            initial=_runtime_state(profile_id=profile_id, account_slot=account_slot),
            activation=activation,
            recovery_inputs=((), ()),
        ),
        poll_seconds=_REPAIR_SECONDS,
    )


def _runtime_state(*, profile_id: str, account_slot: str) -> ExecutionRuntimeState:
    return ExecutionRuntimeState(
        account_slot=account_slot,
        runtime_profile_id=profile_id,
        mode="paper",
        runtime_release="nautilus-1.231.0+oi-v1",
        config_sha256="a" * 64,
        runtime_id=uuid4(),
        runtime_revision="b" * 40,
        image_digest="unversioned",
        credential_fingerprint="d" * 64,
        lifecycle_state="starting",
        alive=True,
        execution_safe=False,
        entries_armed=False,
        control_plane_ready=False,
        singleton_ready=True,
        credential_ready=True,
        activation_ready=True,
        startup_reconciled=False,
        portfolio_ready=False,
        audit_ready=False,
        day_start_ready=False,
        unexpected_exposure=False,
        account_flat=True,
        positions_count=0,
        open_orders_count=0,
        protection_status="not_applicable",
        reconciliation_observed_at_ns=NOW_NS,
        heartbeat_at_ns=NOW_NS,
        entry_block_reason="runtime_starting",
        started_at_ns=NOW_NS,
        updated_at_ns=NOW_NS,
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


def _wait_until_next_cycle_finishes(
    bridge: _MeasuredRuntimeBridge,
    *,
    timeout_seconds: float = 2.0,
) -> None:
    completed_cycles = bridge.completed_cycles
    assert bridge.wait_for_cycle_after(completed_cycles, timeout_seconds)
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
                profile = f"runtime-input-475-{size}-{repeat}"
                slot = f"runtime-input-slot-{size}-{repeat}"
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
                    # Commit at one fixed phase after a completed production cycle so the
                    # transaction's NOTIFY measures the normal wake path, not construction time.
                    _wait_until_initial_cycle_finishes(bridge)
                    _append_workload(
                        repo,
                        profile=profile,
                        size=size,
                        seed=seed,
                        now_ns=NOW_NS + repeat,
                        before_commit=lambda _bridge=bridge: _wait_until_next_cycle_finishes(_bridge),
                    )
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
                "delivery_path": "production_listen_notify_with_indexed_repair",
                "poll_cadence_ms": int(_REPAIR_SECONDS * 1_000),
                "persisted_to_dequeued_p95_ms": round(_p95(dequeue_ms), 3),
                "reader_calls_per_cycle": 2,
                "pending_identity_duplicates": 0,
            }

        profile = "runtime-input-475-missed-wake"
        slot = "runtime-input-slot-repair"
        _activate(repo, profile=profile, slot=slot, now_ns=NOW_NS + 100)
        client = ExecutionSignalClient(runtime_profile_id=profile, execution_strategy="oi_nautilus_v1")
        bridge = _runtime_bridge(
            settings=settings,
            profile_id=profile,
            account_slot=slot,
            signals=client,
        )
        with patch(
            "tracefold.app.nautilus.oi_runtime.wait_for_execution_stream_wake",
            side_effect=_discard_wake,
        ):
            bridge.start()
            try:
                _wait_until_bridge_connects(bridge)
                _wait_until_initial_cycle_finishes(bridge)
                _append_workload(
                    repo,
                    profile=profile,
                    size=1,
                    seed="repair",
                    now_ns=NOW_NS + 100,
                    before_commit=lambda: _wait_until_next_cycle_finishes(bridge),
                )
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
                "production_delivery_path": "listen_notify_with_indexed_timeout_repair",
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
        profile = "runtime-input-475-audit"
        repo = TradingRepository(conn)
        _activate(repo, profile=profile, slot="runtime-input-slot-audit", now_ns=NOW_NS + 200)
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
                    event_identity=f"runtime-input:{index}",
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
    settings = Settings(ws_token="475-runtime-input", storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    app = create_app(settings=settings)
    latencies: list[float] = []
    deadline = time.perf_counter() + _UI_WINDOW_SECONDS
    with TestClient(app) as client:
        while time.perf_counter() < deadline:
            started = time.perf_counter()
            response = client.get("/api/trading/status", params={"token": "475-runtime-input"})
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


def test_emit_runtime_input_diagnostic(tmp_path: Path) -> None:
    measured_git_sha = _require_clean_tracked_tree()
    rss_before = _current_rss_bytes()
    settings = Settings(ws_token="475-runtime-input", storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "bridge-home")
    before = resource.getrusage(resource.RUSAGE_SELF)
    stream, max_connections = _stream_samples(settings)
    audit = _audit_sample()
    lifecycle = _runtime_lifecycle_sample()
    http = _http_sample(tmp_path)
    after = resource.getrusage(resource.RUSAGE_SELF)
    rss_after = _current_rss_bytes()
    report = {
        "schema_version": "tracefold_oi_runtime_input_diagnostic_v1",
        "comparison_baseline_receipt": _PR0_BASELINE_RECEIPT,
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
                "rss_bytes_before": rss_before,
                "rss_bytes_after": rss_after,
                "rss_delta_bytes": rss_after - rss_before,
            },
        },
        "interpretation": {
            "normal_stream_slo_ms": 250,
            "missed_wake_ttl_fraction": "1/3",
            "duplicate_economic_orders": {
                "status": "not_observed",
                "reason": "requires_replay_or_concurrent_admission_workload",
                "count": None,
            },
            "duplicate_active_protections": {
                "status": "not_observed",
                "reason": "requires_replay_or_concurrent_admission_workload",
                "count": None,
            },
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
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    _SCHEDULED_RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    _SCHEDULED_RECEIPT.write_text(serialized, encoding="utf-8")
    print("TRACEFOLD_RUNTIME_BASELINE=" + json.dumps(report, sort_keys=True))
