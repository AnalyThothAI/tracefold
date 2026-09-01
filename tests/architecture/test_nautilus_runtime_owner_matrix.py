"""Executable owner and evidence boundary for #475 PR-0."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "docs" / "research" / "oi-runtime-pr0-baseline-2026-09-01.json"


def _receipt() -> dict[str, object]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _defined_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
            if isinstance(node, ast.ClassDef):
                symbols.update(
                    f"{node.name}.{child.name}"
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
    return symbols


def test_owner_matrix_names_one_concrete_owner_and_existing_symbol_per_fact() -> None:
    receipt = _receipt()
    rows = receipt["owner_matrix"]
    assert isinstance(rows, list)
    expected = {
        "unresolved_signal_command_read",
        "callback_operational_state",
        "authoritative_account_flat",
        "current_runtime_status",
        "durable_audit",
    }
    assert {row["fact"] for row in rows} == expected
    assert len({row["fact"] for row in rows}) == len(rows)
    for row in rows:
        source = ROOT / row["source"]
        assert source.is_file(), row
        assert row["symbol"] in _defined_symbols(source), row
        assert row["owner"] and row["authority"] and row["repair"]


def test_baseline_receipt_covers_every_issue_measurement_without_inventing_provider_evidence() -> None:
    receipt = _receipt()
    assert receipt["schema_version"] == "tracefold_oi_runtime_pr0_baseline_v1"
    assert receipt["baseline_source_main"] == "f495a9fc0d0ba0d528e40b588e76108d80cdfefe"
    assert len(receipt["measured_git_sha"]) == 40
    measurements = receipt["measurements"]
    assert set(measurements) == {
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
    assert measurements["stream_latency"]["status"] == "observed"
    assert measurements["database"]["status"] == "observed"
    assert measurements["audit_append"]["status"] == "observed"
    assert measurements["http_reads_15s"]["status"] == "observed"
    for provider_only in ("event_loop", "private_reconciliation"):
        assert measurements[provider_only]["status"] == "not_observed"
        assert measurements[provider_only]["reason"] == "requires_active_binance_demo_runtime"
    assert measurements["quote_subscriptions"]["synthetic_route_count"] == 525
    assert measurements["quote_subscriptions"]["is_production_collector"] is False


def test_pr0_does_not_create_an_oi_collector_or_a_second_execution_bus() -> None:
    forbidden = (
        "market_oi_snapshots",
        "class OiCollector",
        "class OICollector",
        "kafka",
        "redis execution",
        "rabbitmq execution",
    )
    runtime_roots = (
        ROOT / "tracefold/app/nautilus",
        ROOT / "tracefold/integrations/nautilus",
        ROOT / "tracefold/trading",
    )
    paths = [path for runtime_root in runtime_roots for path in runtime_root.rglob("*.py")]
    violations: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8").lower()
        violations.extend(f"{path.relative_to(ROOT)}:{token}" for token in forbidden if token.lower() in text)
    assert violations == []


def test_current_production_wiring_has_one_input_reconciliation_and_projection_owner_after_pr_b() -> None:
    root_source = (ROOT / "tracefold/app/nautilus/root.py").read_text(encoding="utf-8")
    bridge_source = (ROOT / "tracefold/app/nautilus/oi_runtime.py").read_text(encoding="utf-8")
    config_source = (ROOT / "tracefold/integrations/nautilus/oi_runtime/config.py").read_text(encoding="utf-8")
    signal_client_source = (ROOT / "tracefold/integrations/nautilus/oi_runtime/signal_client.py").read_text(
        encoding="utf-8"
    )
    strategy_source = (ROOT / "tracefold/integrations/nautilus/oi_runtime/strategy.py").read_text(encoding="utf-8")
    diagnostic_source = (ROOT / "tests/integration/test_nautilus_runtime_input_diagnostic.py").read_text(
        encoding="utf-8"
    )

    assert root_source.count("OiRuntimeDatabaseBridge(") == 1
    assert "run_signal_poll_loop" not in root_source
    assert "def run_signal_poll_loop(" not in signal_client_source
    assert bridge_source.count("install_execution_stream_listener(") == 1
    assert bridge_source.count("wait_for_execution_stream_wake(") == 1
    assert (
        "install_execution_stream_listener(repos.conn, channel=execution_stream_channel())\n"
        "                    with self._lock:\n"
        "                        self._connected = True"
    ) in bridge_source
    assert "poll_execution_inputs_once(" in bridge_source
    assert "flush_audit_once(" in bridge_source
    assert "load_or_record_day_start(" in bridge_source
    assert "for route in self._profile.routes:\n            self.subscribe_quote_ticks" in strategy_source
    assert "reports = await load_complete_binance_account_reports(client)" in root_source
    assert root_source.count('application_name="tracefold_nautilus_state"') == 1
    assert "class _RuntimeStateProjector:" in root_source
    assert "self._repos.trading.update_execution_runtime_state(candidate)" in root_source
    assert 'triggers=("startup",)' in root_source
    assert 'reconciliation_triggers.add("steady")' in root_source
    for reason in ("unknown_outcome", "protection_ambiguity", "flatten_pending"):
        assert f'self._request_reconciliation("{reason}")' in strategy_source
    assert "continuous_reconciliation" not in strategy_source
    assert "reconciliation=False" in config_source
    assert "open_check_interval_secs=5.0" in config_source
    assert "position_check_interval_secs=5.0" in config_source
    assert "class _MeasuredRuntimeBridge(OiRuntimeDatabaseBridge):" in diagnostic_source
    assert "super()._cycle(repos)" in diagnostic_source
    assert "before_commit=lambda _bridge=bridge: _wait_until_next_cycle_finishes(_bridge)" in diagnostic_source
    assert "install_execution_stream_listener" not in diagnostic_source
    assert '"tracefold_oi_runtime_input_diagnostic_v1"' in diagnostic_source
    assert "oi_runtime_input_diagnostic_dirty_worktree" in diagnostic_source
    assert "artifacts/scheduled/oi-runtime-input-diagnostic.json" in diagnostic_source
    assert "rss_delta_bytes" in diagnostic_source
