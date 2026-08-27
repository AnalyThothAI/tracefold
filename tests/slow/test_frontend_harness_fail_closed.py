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


@pytest.mark.parametrize(
    ("fixture", "expected_process_status", "manifest_field", "expected_count"),
    [
        ("expected-failure", 0, "xfailed", 1),
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


def test_playwright_evidence_rejects_test_fail_even_when_the_runner_exits_zero(
    tmp_path: Path,
) -> None:
    report = tmp_path / "playwright.json"
    result = subprocess.run(
        [
            NODE,
            str(PLAYWRIGHT),
            "test",
            "--config",
            "tests/fixtures/playwright-semantics/playwright.config.ts",
        ],
        cwd=WEB,
        env=_child_env(TRACEFOLD_PLAYWRIGHT_SEMANTICS_REPORT=str(report)),
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
            "--output",
            str(lane),
        )
    )
    manifest = json.loads(lane.read_text(encoding="utf-8"))
    assert record_status != 0
    assert manifest["status"] == "failure"
    assert manifest["selected"] == manifest["xfailed"] == 1
    assert manifest["passed"] == 0
