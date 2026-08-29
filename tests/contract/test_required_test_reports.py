from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.require_test_reports import (
    ReportError,
    require_junit,
    require_playwright_json,
    require_vitest_json,
)

pytestmark = pytest.mark.contract


def test_junit_requires_executed_plain_passes(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite tests="1" errors="0" failures="0" skipped="0">'
        '<testcase name="green" /></testsuite></testsuites>',
        encoding="utf-8",
    )

    assert require_junit(report) == 1

    report.write_text("<testsuites />", encoding="utf-8")
    with pytest.raises(ReportError, match="required_test_report_invalid_junit"):
        require_junit(report)

    report.write_text("<testsuites>", encoding="utf-8")
    with pytest.raises(ReportError, match="required_test_report_invalid_junit"):
        require_junit(report)


@pytest.mark.parametrize("outcome", ["failure", "error", "skipped"])
def test_junit_rejects_every_non_green_outcome(tmp_path: Path, outcome: str) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        f'<testsuite tests="1" errors="0" failures="0" skipped="0">'
        f'<testcase name="pseudo-green"><{outcome} /></testcase></testsuite>',
        encoding="utf-8",
    )

    with pytest.raises(ReportError, match="required_test_report_non_green"):
        require_junit(report)


@pytest.mark.parametrize("counter", ["errors", "failures", "skipped"])
def test_junit_rejects_suite_level_non_green_counters(tmp_path: Path, counter: str) -> None:
    report = tmp_path / "junit.xml"
    counters = {"errors": 0, "failures": 0, "skipped": 0, counter: 1}
    outcome = counter[:-1] if counter != "skipped" else "skipped"
    report.write_text(
        '<testsuite tests="1" '
        f'errors="{counters["errors"]}" failures="{counters["failures"]}" '
        f'skipped="{counters["skipped"]}"><testcase name="pseudo-green" />'
        f"<{outcome} /></testsuite>",
        encoding="utf-8",
    )

    with pytest.raises(ReportError, match="required_test_report_non_green"):
        require_junit(report)


def test_junit_rejects_an_aggregate_that_undercounts_concrete_cases(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite tests="0" errors="0" failures="0" skipped="0"><testcase name="unreported" /></testsuite>',
        encoding="utf-8",
    )

    with pytest.raises(ReportError, match="required_test_report_count_mismatch"):
        require_junit(report)


def _vitest_report() -> dict[str, object]:
    return {
        "numTotalTestSuites": 1,
        "numPassedTestSuites": 1,
        "numFailedTestSuites": 0,
        "numPendingTestSuites": 0,
        "numTotalTests": 1,
        "numPassedTests": 1,
        "numFailedTests": 0,
        "numPendingTests": 0,
        "numTodoTests": 0,
        "snapshot": {
            "added": 0,
            "failure": False,
            "filesAdded": 0,
            "filesRemoved": 0,
            "filesRemovedList": [],
            "filesUnmatched": 0,
            "filesUpdated": 0,
            "unchecked": 0,
            "uncheckedKeysByFile": [],
            "unmatched": 0,
            "updated": 0,
            "didUpdate": False,
        },
        "success": True,
        "testResults": [
            {
                "assertionResults": [
                    {
                        "fullName": "plain pass",
                        "status": "passed",
                        "failureMessages": [],
                    }
                ],
                "status": "passed",
                "message": "",
                "name": "required.test.ts",
            }
        ],
    }


def test_vitest_native_json_requires_plain_passes(tmp_path: Path) -> None:
    report = tmp_path / "vitest.json"
    report.write_text(json.dumps(_vitest_report()), encoding="utf-8")

    assert require_vitest_json(report) == 1


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("numTotalTestSuites", 0, "required_test_report_empty"),
        ("numPassedTestSuites", 0, "required_test_report_non_green"),
        ("numTotalTests", 0, "required_test_report_empty"),
        ("numPendingTests", 1, "required_test_report_non_green"),
        ("numTodoTests", 1, "required_test_report_non_green"),
        ("numFailedTests", 1, "required_test_report_non_green"),
    ],
)
def test_vitest_native_json_rejects_empty_or_non_green_counts(
    tmp_path: Path, field: str, value: int, error: str
) -> None:
    payload = _vitest_report()
    payload[field] = value
    report = tmp_path / "vitest.json"
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReportError, match=error):
        require_vitest_json(report)


def test_vitest_native_json_rejects_retry_history_and_snapshot_updates(tmp_path: Path) -> None:
    payload = _vitest_report()
    test_results = payload["testResults"]
    assert isinstance(test_results, list)
    assertion_results = test_results[0]["assertionResults"]
    assert isinstance(assertion_results, list)
    assertion_results[0]["failureMessages"] = ["failed before retry"]
    retry_report = tmp_path / "retry.json"
    retry_report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReportError, match="required_test_report_non_green"):
        require_vitest_json(retry_report)

    payload = _vitest_report()
    snapshot = payload["snapshot"]
    assert isinstance(snapshot, dict)
    snapshot["filesAdded"] = 1
    snapshot_report = tmp_path / "snapshot.json"
    snapshot_report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReportError, match="required_test_report_snapshot_mutated"):
        require_vitest_json(snapshot_report)


def _playwright_report() -> dict[str, object]:
    return {
        "config": {
            "forbidOnly": True,
            "maxFailures": 0,
            "updateSnapshots": "none",
            "projects": [{"name": "required-chromium", "repeatEach": 1, "retries": 0}],
        },
        "suites": [
            {
                "title": "required.spec.ts",
                "suites": [],
                "specs": [
                    {
                        "ok": True,
                        "tests": [
                            {
                                "annotations": [],
                                "expectedStatus": "passed",
                                "status": "expected",
                                "results": [{"status": "passed", "retry": 0, "errors": []}],
                            }
                        ],
                    }
                ],
            }
        ],
        "errors": [],
        "stats": {"expected": 1, "skipped": 0, "unexpected": 0, "flaky": 0},
    }


def test_playwright_native_json_requires_one_shot_plain_passes(tmp_path: Path) -> None:
    report = tmp_path / "playwright.json"
    report.write_text(json.dumps(_playwright_report()), encoding="utf-8")

    assert require_playwright_json(report) == 1


@pytest.mark.parametrize("fault", ["skipped", "unexpected", "flaky", "retry", "expected-failure", "snapshot"])
def test_playwright_native_json_rejects_pseudo_green_semantics(tmp_path: Path, fault: str) -> None:
    payload = copy.deepcopy(_playwright_report())
    stats = payload["stats"]
    config = payload["config"]
    suites = payload["suites"]
    assert isinstance(stats, dict)
    assert isinstance(config, dict)
    assert isinstance(suites, list)
    test = suites[0]["specs"][0]["tests"][0]
    if fault in {"skipped", "unexpected", "flaky"}:
        stats[fault] = 1
    elif fault == "retry":
        test["results"][0]["retry"] = 1
    elif fault == "expected-failure":
        test["expectedStatus"] = "failed"
    else:
        config["updateSnapshots"] = "missing"
    report = tmp_path / "playwright.json"
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReportError, match="required_test_report_"):
        require_playwright_json(report)
