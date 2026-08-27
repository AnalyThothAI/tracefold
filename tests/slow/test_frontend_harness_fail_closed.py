from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.support import evidence

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
NODE = "node"
VITEST = WEB / "node_modules" / "vitest" / "vitest.mjs"
PLAYWRIGHT = WEB / "node_modules" / "@playwright" / "test" / "cli.js"
pytestmark = pytest.mark.slow


def _child_env(**updates: str) -> dict[str, str]:
    env = {**os.environ, "NO_COLOR": "1", **updates}
    env.pop("FORCE_COLOR", None)
    return env


def _run_vitest_fixture(
    fixture: str,
    *,
    config: str,
    report: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _child_env()
    if report is not None:
        env["TRACEFOLD_VITEST_SEMANTICS_REPORT"] = str(report)
    return subprocess.run(
        [
            NODE,
            str(VITEST),
            "run",
            "--config",
            config,
            "--no-color",
            "--allowOnly=false",
            *(["--reporter=./tests/support/evidenceReporter.ts"] if report is not None else []),
            fixture,
        ],
        cwd=WEB,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


def _record_vitest(report: Path, output: Path) -> tuple[int, dict[str, Any]]:
    returncode = evidence.main(
        (
            "record-vitest",
            "--lane",
            "frontend-fixture",
            "--input",
            str(report),
            "--output",
            str(output),
        )
    )
    return returncode, json.loads(output.read_text(encoding="utf-8"))


def test_runtime_error_guard_fails_closed_and_keeps_allowances_case_local() -> None:
    failed = _run_vitest_fixture(
        "tests/fixtures/runtime-error-guard/fail-closed.fixture.ts",
        config="tests/fixtures/runtime-error-guard/vitest.config.ts",
    )
    failed_output = failed.stdout + failed.stderr

    assert failed.returncode != 0
    assert "Unexpected console.error in test case" in failed_output
    assert "Unexpected unhandled rejection in test case" in failed_output
    assert "Runtime error allowlists require a non-empty reason" in failed_output

    allowed = _run_vitest_fixture(
        "tests/fixtures/runtime-error-guard/allowed-errors.fixture.ts",
        config="tests/fixtures/runtime-error-guard/vitest.config.ts",
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr

    global_allowlist = _run_vitest_fixture(
        "tests/fixtures/runtime-error-guard/global-allowlist.fixture.ts",
        config="tests/fixtures/runtime-error-guard/vitest.config.ts",
    )
    assert global_allowlist.returncode != 0
    assert "Runtime error allowlists are case-local" in global_allowlist.stdout + global_allowlist.stderr


def test_runtime_error_guard_fails_on_lifecycle_console_errors(tmp_path: Path) -> None:
    report = tmp_path / "after-all.json"
    result = _run_vitest_fixture(
        "tests/fixtures/runtime-error-guard/after-all.fixture.ts",
        config="tests/fixtures/runtime-error-guard/vitest.config.ts",
        report=report,
    )

    assert result.returncode != 0
    semantic_report = json.loads(report.read_text(encoding="utf-8"))
    assert any(
        "Unexpected console.error outside a test case" in error["message"] for error in semantic_report["moduleErrors"]
    )
    record_status, manifest = _record_vitest(report, tmp_path / "after-all-lane.json")
    assert record_status != 0
    assert manifest["status"] == "failure"
    assert manifest["unhandled"] > 0


@pytest.mark.parametrize(
    ("fixture", "expected_process_status", "manifest_field", "expected_count"),
    [
        ("expected-failure", 0, "xfailed", 1),
        ("unexpected-pass", 1, "xpassed", 1),
        ("retry-repeat", 0, "rerun", 2),
        ("only", 1, "failed", 1),
        ("final-failure", 1, "failed", 1),
        ("unhandled", 1, "unhandled", 1),
    ],
)
def test_vitest_evidence_rejects_non_plain_pass_runtime_semantics(
    tmp_path: Path,
    fixture: str,
    expected_process_status: int,
    manifest_field: str,
    expected_count: int,
) -> None:
    report = tmp_path / f"{fixture}.json"
    result = _run_vitest_fixture(
        f"tests/fixtures/evidence-reporter/{fixture}.fixture.ts",
        config="tests/fixtures/evidence-reporter/vitest.config.ts",
        report=report,
    )

    assert int(result.returncode != 0) == expected_process_status, result.stdout + result.stderr
    record_status, manifest = _record_vitest(report, tmp_path / f"{fixture}-lane.json")
    assert record_status != 0
    assert manifest["status"] == "failure"
    assert manifest[manifest_field] == expected_count


def test_vitest_evidence_rejects_a_green_partial_architecture_run(tmp_path: Path) -> None:
    report = tmp_path / "partial-architecture.json"
    result = _run_vitest_fixture(
        "tests/architecture/cssArchitectureHarness.test.ts",
        config="vite.config.ts",
        report=report,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    lane = tmp_path / "frontend-architecture.json"
    record_status = evidence.main(
        (
            "record-vitest",
            "--lane",
            "frontend-architecture",
            "--input",
            str(report),
            "--output",
            str(lane),
        )
    )
    manifest = json.loads(lane.read_text(encoding="utf-8"))
    assert record_status != 0
    assert manifest["status"] == "failure"
    assert manifest["selected"] == manifest["passed"] > 0
    assert any(error.startswith("vitest_tracked_test_module_not_executed:") for error in manifest["errors"])


@pytest.mark.parametrize(
    ("fixture", "runner_status", "manifest_field"),
    [
        ("expected-failure.spec.ts", 0, "xfailed"),
        ("unexpected-pass.spec.ts", 1, "xpassed"),
    ],
)
def test_playwright_evidence_reports_expected_failure_outcomes_exclusively(
    tmp_path: Path,
    fixture: str,
    runner_status: int,
    manifest_field: str,
) -> None:
    report = tmp_path / "playwright.json"
    selection = tmp_path / "playwright-selection.json"
    result = subprocess.run(
        [
            NODE,
            str(PLAYWRIGHT),
            "test",
            "--config",
            "tests/fixtures/playwright-semantics/playwright.config.ts",
        ],
        cwd=WEB,
        env=_child_env(
            TRACEFOLD_PLAYWRIGHT_FIXTURE=fixture,
            TRACEFOLD_PLAYWRIGHT_SELECTION_OUTPUT=str(selection),
            TRACEFOLD_PLAYWRIGHT_SEMANTICS_REPORT=str(report),
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert int(result.returncode != 0) == runner_status, result.stdout + result.stderr
    lane = tmp_path / "playwright-lane.json"
    record_status = evidence.main(
        (
            "record-playwright",
            "--lane",
            "browser-fixture",
            "--input",
            str(report),
            "--selection",
            str(selection),
            "--output",
            str(lane),
        )
    )
    manifest = json.loads(lane.read_text(encoding="utf-8"))
    assert record_status != 0
    assert manifest["status"] == "failure"
    assert manifest["selected"] == manifest[manifest_field] == 1
    assert manifest["passed"] == manifest["failed"] == 0
    assert manifest["xfailed"] + manifest["xpassed"] == 1


def test_playwright_evidence_rejects_cli_grep_partial_selection(tmp_path: Path) -> None:
    report = tmp_path / "playwright.json"
    selection = tmp_path / "playwright-selection.json"
    result = subprocess.run(
        [
            NODE,
            str(PLAYWRIGHT),
            "test",
            "--config",
            "tests/fixtures/playwright-semantics/playwright.config.ts",
            "--grep",
            "selected",
        ],
        cwd=WEB,
        env=_child_env(
            TRACEFOLD_PLAYWRIGHT_FIXTURE="partial-selection.spec.ts",
            TRACEFOLD_PLAYWRIGHT_SELECTION_OUTPUT=str(selection),
            TRACEFOLD_PLAYWRIGHT_SEMANTICS_REPORT=str(report),
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    lane = tmp_path / "playwright-lane.json"
    record_status = evidence.main(
        (
            "record-playwright",
            "--lane",
            "browser-fixture",
            "--input",
            str(report),
            "--selection",
            str(selection),
            "--output",
            str(lane),
        )
    )
    manifest = json.loads(lane.read_text(encoding="utf-8"))
    assert record_status != 0
    assert manifest["status"] == "failure"
    assert manifest["selected"] == manifest["passed"] == 1
    assert "playwright_partial_selection_forbidden" in manifest["errors"]
