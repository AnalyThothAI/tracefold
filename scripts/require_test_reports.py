"""Fail closed on pseudo-green outcomes in native test-runner reports."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET


class ReportError(ValueError):
    """A required native test report is missing, invalid, empty, or non-green."""


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReportError(f"required_test_report_missing:{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"required_test_report_invalid_json:{path}") from exc
    if not isinstance(payload, dict):
        raise ReportError(f"required_test_report_invalid_json:{path}")
    return payload


def require_junit(path: Path) -> int:
    """Require at least one executed JUnit case and no non-green outcome."""

    try:
        root = ET.parse(path).getroot()
    except FileNotFoundError as exc:
        raise ReportError(f"required_test_report_missing:{path}") from exc
    except (OSError, ET.ParseError) as exc:
        raise ReportError(f"required_test_report_invalid_junit:{path}") from exc
    if _local_name(root.tag) not in {"testsuite", "testsuites"}:
        raise ReportError(f"required_test_report_invalid_junit:{path}")
    suites = [element for element in root.iter() if _local_name(element.tag) == "testsuite"]
    if not suites:
        raise ReportError(f"required_test_report_invalid_junit:{path}")
    cases = [element for element in root.iter() if _local_name(element.tag) == "testcase"]
    if not cases:
        raise ReportError(f"required_test_report_empty:{path}")
    aggregate_tests = 0
    for suite in suites:
        aggregate_tests += _junit_counter(suite, "tests", path)
        if any(_junit_counter(suite, name, path) for name in ("errors", "failures", "skipped")):
            raise ReportError(f"required_test_report_non_green:{path}:suite-counter")
    # Pytest can count internally exercised outcomes in the suite total without
    # serializing them as top-level cases. Under-counting concrete cases is
    # still invalid; exact equality is not a portable JUnit invariant.
    if aggregate_tests < len(cases):
        raise ReportError(f"required_test_report_count_mismatch:{path}")
    if any(_local_name(element.tag) in {"error", "failure", "skipped"} for element in root.iter()):
        raise ReportError(f"required_test_report_non_green:{path}:outcome")
    return len(cases)


def require_vitest_json(path: Path) -> int:
    """Require a complete native Vitest JSON run with no snapshot mutation."""

    report = _read_json(path)
    total_suites = _integer(report, "numTotalTestSuites", path)
    passed_suites = _integer(report, "numPassedTestSuites", path)
    total = _integer(report, "numTotalTests", path)
    rejected_counts = {
        name: _integer(report, name, path)
        for name in (
            "numFailedTestSuites",
            "numPendingTestSuites",
            "numFailedTests",
            "numPendingTests",
            "numTodoTests",
        )
    }
    passed = _integer(report, "numPassedTests", path)
    if total_suites <= 0 or total <= 0:
        raise ReportError(f"required_test_report_empty:{path}")
    if (
        report.get("success") is not True
        or passed_suites != total_suites
        or passed != total
        or any(rejected_counts.values())
    ):
        raise ReportError(f"required_test_report_non_green:{path}")

    snapshot = report.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ReportError(f"required_test_report_invalid_vitest:{path}")
    snapshot_counts = (
        "added",
        "filesAdded",
        "filesRemoved",
        "filesUnmatched",
        "filesUpdated",
        "unchecked",
        "unmatched",
        "updated",
    )
    if snapshot.get("failure") is not False or snapshot.get("didUpdate") is not False:
        raise ReportError(f"required_test_report_snapshot_mutated:{path}")
    if any(_integer(snapshot, name, path) for name in snapshot_counts):
        raise ReportError(f"required_test_report_snapshot_mutated:{path}")

    assertions: list[Mapping[str, Any]] = []
    for result in _mapping_list(report.get("testResults"), "testResults", path):
        if result.get("status") != "passed" or result.get("message"):
            raise ReportError(f"required_test_report_non_green:{path}")
        assertions.extend(_mapping_list(result.get("assertionResults"), "assertionResults", path))
    if len(assertions) != total:
        raise ReportError(f"required_test_report_count_mismatch:{path}")
    if any(assertion.get("status") != "passed" or bool(assertion.get("failureMessages")) for assertion in assertions):
        raise ReportError(f"required_test_report_non_green:{path}")
    return total


def require_playwright_json(path: Path) -> int:
    """Require one-shot, no-skip Playwright semantics from its native JSON report."""

    report = _read_json(path)
    config = report.get("config")
    stats = report.get("stats")
    if not isinstance(config, dict) or not isinstance(stats, dict):
        raise ReportError(f"required_test_report_invalid_playwright:{path}")
    if config.get("forbidOnly") is not True or config.get("maxFailures") != 0:
        raise ReportError(f"required_test_report_unsafe_playwright_config:{path}")
    if config.get("updateSnapshots") != "none":
        raise ReportError(f"required_test_report_snapshot_policy_invalid:{path}")
    projects = _mapping_list(config.get("projects"), "projects", path)
    if not projects or any(project.get("retries") != 0 or project.get("repeatEach") != 1 for project in projects):
        raise ReportError(f"required_test_report_retry_policy_invalid:{path}")

    expected = _integer(stats, "expected", path)
    if expected <= 0:
        raise ReportError(f"required_test_report_empty:{path}")
    if any(_integer(stats, name, path) for name in ("skipped", "unexpected", "flaky")):
        raise ReportError(f"required_test_report_non_green:{path}")
    if report.get("errors"):
        raise ReportError(f"required_test_report_non_green:{path}")

    tests = list(_playwright_tests(report.get("suites"), path))
    if len(tests) != expected:
        raise ReportError(f"required_test_report_count_mismatch:{path}")
    for test in tests:
        results = _mapping_list(test.get("results"), "results", path)
        if (
            test.get("expectedStatus") != "passed"
            or test.get("status") != "expected"
            or test.get("annotations")
            or len(results) != 1
        ):
            raise ReportError(f"required_test_report_non_green:{path}")
        result = results[0]
        if result.get("status") != "passed" or result.get("retry") != 0 or result.get("errors"):
            raise ReportError(f"required_test_report_non_green:{path}")
    return len(tests)


def _playwright_tests(raw_suites: object, path: Path) -> Iterable[Mapping[str, Any]]:
    for suite in _mapping_list(raw_suites, "suites", path):
        yield from _playwright_tests(suite.get("suites", []), path)
        for spec in _mapping_list(suite.get("specs", []), "specs", path):
            if spec.get("ok") is not True:
                raise ReportError(f"required_test_report_non_green:{path}")
            yield from _mapping_list(spec.get("tests"), "tests", path)


def _mapping_list(value: object, field: str, path: Path) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ReportError(f"required_test_report_invalid_field:{path}:{field}")
    return value


def _integer(payload: Mapping[str, Any], field: str, path: Path) -> int:
    value = payload.get(field)
    if type(value) is not int:
        raise ReportError(f"required_test_report_invalid_field:{path}:{field}")
    return value


def _junit_counter(suite: Any, field: str, path: Path) -> int:
    raw = suite.attrib.get(field)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ReportError(f"required_test_report_invalid_field:{path}:{field}") from exc
    if value < 0:
        raise ReportError(f"required_test_report_invalid_field:{path}:{field}")
    return value


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def main(arguments: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", action="append", default=[], type=Path)
    parser.add_argument("--vitest-json", action="append", default=[], type=Path)
    parser.add_argument("--playwright-json", action="append", default=[], type=Path)
    options = parser.parse_args(arguments)
    requested = (*options.junit, *options.vitest_json, *options.playwright_json)
    if not requested:
        parser.error("at least one native test report is required")
    try:
        total = sum(require_junit(path) for path in options.junit)
        total += sum(require_vitest_json(path) for path in options.vitest_json)
        total += sum(require_playwright_json(path) for path in options.playwright_json)
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"required native test reports passed: {total} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
