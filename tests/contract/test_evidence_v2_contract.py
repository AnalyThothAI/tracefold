from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings

from tests.support import evidence


@dataclass(frozen=True)
class _EvidenceRepository:
    root: Path
    manifest: Path
    env: dict[str, str]

    def run(
        self, *extra_args: str, env: dict[str, str] | None = None
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "tests.support.evidence",
                "-p",
                "_hypothesis_pytestplugin",
                "-m",
                "not live",
                f"--evidence-manifest={self.manifest}",
                *extra_args,
            ],
            cwd=self.root,
            env={**self.env, **(env or {})},
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        return result, json.loads(self.manifest.read_text(encoding="utf-8"))


@pytest.fixture
def evidence_repository(tmp_path: Path) -> _EvidenceRepository:
    root = tmp_path / "repository"
    (root / "tests" / "support").mkdir(parents=True)
    (root / "tracefold" / "platform" / "postgres").mkdir(parents=True)
    (root / "web").mkdir()
    shutil.copy2(Path(evidence.__file__).resolve(), root / "tests" / "support" / "evidence.py")
    for package in (
        root / "tests",
        root / "tests" / "support",
        root / "tracefold",
        root / "tracefold" / "platform",
        root / "tracefold" / "platform" / "postgres",
    ):
        (package / "__init__.py").write_text("", encoding="utf-8")
    (root / "tracefold" / "platform" / "postgres" / "migrations.py").write_text(
        "def latest_migration_version(): return 'fixture-head'\n", encoding="utf-8"
    )
    (root / "tests" / "conftest.py").write_text(
        "import os\n"
        "from hypothesis import settings\n"
        "collect_ignore = ['test_fail.py'] if os.environ.get('TRACEFOLD_FIXTURE_IGNORE') == '1' else []\n"
        "settings.register_profile('ci', max_examples=5, database=None, derandomize=True, print_blob=True)\n"
        "settings.load_profile('ci')\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_pass.py").write_text("def test_pass(): assert True\n", encoding="utf-8")
    (root / "tests" / "test_fail.py").write_text(
        "import os\ndef test_fail(): assert os.environ.get('TRACEFOLD_FIXTURE_FAIL') != '1'\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\nmarkers = ['live: external provider test']\n",
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("fixture lock\n", encoding="utf-8")
    (root / "web" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    executable_dir = root / "bin"
    executable_dir.mkdir()
    git = shutil.which("git")
    assert git is not None
    (executable_dir / "git").symlink_to(git)
    for name, output in (("node", "v22.0.0-test"), ("uv", "uv 0.0.0-test")):
        executable = executable_dir / name
        executable.write_text(f"#!/bin/sh\nprintf '{output}\\n'\n", encoding="utf-8")
        executable.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tracefold Test",
            "-c",
            "user.email=tests@tracefold.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=root,
        check=True,
    )
    process_env = os.environ.copy()
    process_env.pop("GITHUB_SHA", None)
    process_env.pop("PYTEST_ADDOPTS", None)
    process_env.update(
        {
            "PATH": str(executable_dir),
            "PYTHONPATH": str(root),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "TRACEFOLD_TEST_EVIDENCE": "1",
            "TRACEFOLD_FIXTURE_FAIL": "1",
        }
    )
    return _EvidenceRepository(root=root, manifest=root / "evidence.json", env=process_env)


def test_evidence_rejects_ignored_tracked_test_module(evidence_repository: _EvidenceRepository) -> None:
    result, manifest = evidence_repository.run("--ignore=tests/test_fail.py")

    assert result.returncode != 0
    assert "evidence_collection_option_forbidden:--ignore" in manifest["errors"]


def test_evidence_rejects_positional_test_subset(evidence_repository: _EvidenceRepository) -> None:
    result, manifest = evidence_repository.run("tests/test_pass.py")

    assert result.returncode != 0
    assert "evidence_test_root_must_be_complete" in manifest["errors"]


def test_evidence_rejects_pytest_addopts(evidence_repository: _EvidenceRepository) -> None:
    result, manifest = evidence_repository.run(env={"PYTEST_ADDOPTS": "--ignore=tests/test_fail.py"})

    assert result.returncode != 0
    assert "evidence_pytest_addopts_forbidden" in manifest["errors"]


def test_evidence_rejects_last_failed_selection(evidence_repository: _EvidenceRepository) -> None:
    result, manifest = evidence_repository.run("--lf", "--ignore=tests/test_fail.py")

    assert result.returncode != 0
    assert "evidence_collection_option_forbidden:--lf" in manifest["errors"]


def test_evidence_requires_every_tracked_test_module(evidence_repository: _EvidenceRepository) -> None:
    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_IGNORE": "1"})

    assert result.returncode != 0
    assert "evidence_tracked_test_module_not_collected:tests/test_fail.py" in manifest["errors"]


def test_live_only_tracked_module_participates_in_collection_without_being_selected(
    evidence_repository: _EvidenceRepository,
) -> None:
    live_module = evidence_repository.root / "tests" / "test_live_only.py"
    live_module.write_text(
        "import pytest\n@pytest.mark.live\ndef test_live_only(): assert True\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "tests/test_live_only.py"], cwd=evidence_repository.root, check=True)

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert manifest["selected"] == manifest["passed"] == 2
    assert not any("test_live_only.py" in error for error in manifest["errors"])


@pytest.mark.parametrize(
    ("option", "canonical"),
    [
        ("--ignore-glob=tests/test_fail.py", "--ignore-glob"),
        ("--ff", "--ff"),
        ("--deselect=tests/test_fail.py::test_fail", "--deselect"),
        ("--sw", "--sw"),
    ],
)
def test_evidence_rejects_other_partial_execution_options(
    evidence_repository: _EvidenceRepository, option: str, canonical: str
) -> None:
    result, manifest = evidence_repository.run(option, "--ignore=tests/test_fail.py")

    assert result.returncode != 0
    assert f"evidence_collection_option_forbidden:{canonical}" in manifest["errors"]


def test_aggregate_fails_when_a_required_lane_manifest_is_missing(tmp_path: Path) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    output.write_text('{"schema_version":"tracefold_test_evidence_v2","overall":"success"}\n', encoding="utf-8")

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "python",
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "tracefold_test_evidence_v2"
    assert manifest["overall"] == "failure"
    assert manifest["errors"] == ["required_lane_manifest_missing:python"]


def test_aggregate_replaces_stale_green_output_when_a_lane_is_invalid(tmp_path: Path) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    (lane_dir / "python.json").write_text("not json\n", encoding="utf-8")
    output.write_text('{"schema_version":"tracefold_test_evidence_v2","overall":"success"}\n', encoding="utf-8")

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "python",
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["overall"] == "failure"
    assert manifest["errors"] == ["required_lane_manifest_invalid:python"]


def _lane_payload(lane: str, *, selected: int = 1, passed: int = 1, **overrides: Any) -> dict[str, Any]:
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(evidence.__file__).resolve().parents[2],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    tree_sha = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=Path(evidence.__file__).resolve().parents[2],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    payload: dict[str, Any] = {
        "schema_version": "tracefold_test_lane_v2",
        "lane": lane,
        "required": True,
        "status": "success",
        "commit_sha": commit_sha,
        "git_tree_sha": tree_sha,
        "selected": selected,
        "passed": passed,
        "failed": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "rerun": 0,
        "unhandled": 0,
        "errors": [],
        "tool_versions": {"fixture": "1"},
    }
    payload.update(overrides)
    return payload


def test_aggregate_fails_when_a_required_lane_is_empty(tmp_path: Path) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    (lane_dir / "python.json").write_text(json.dumps(_lane_payload("python", selected=0, passed=0)), encoding="utf-8")

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "python",
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["overall"] == "failure"
    assert "required_lane_empty:python" in manifest["errors"]


@pytest.mark.parametrize("field", ["failed", "skipped", "xfailed", "xpassed", "rerun", "unhandled"])
def test_aggregate_fails_on_every_non_green_required_lane_outcome(tmp_path: Path, field: str) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    (lane_dir / "python.json").write_text(json.dumps(_lane_payload("python", **{field: 1})), encoding="utf-8")

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "python",
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["overall"] == "failure"
    assert f"required_lane_not_green:python:{field}=1" in manifest["errors"]


def test_aggregate_rejects_not_applicable_required_lane(tmp_path: Path) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    (lane_dir / "browser.json").write_text(
        json.dumps(_lane_payload("browser", status="not_applicable")), encoding="utf-8"
    )

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "browser",
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert "required_lane_status_not_success:browser:not_applicable" in manifest["errors"]


def test_record_command_writes_a_green_nonempty_lane(tmp_path: Path) -> None:
    output = tmp_path / "typecheck.json"

    result = evidence.main(
        (
            "record-command",
            "--lane",
            "typecheck",
            "--output",
            str(output),
            "--tool",
            "fixture=1.0",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        )
    )

    assert result == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "tracefold_test_lane_v2"
    assert manifest["lane"] == "typecheck"
    assert manifest["status"] == "success"
    assert manifest["selected"] == manifest["passed"] == 1
    assert manifest["failed"] == manifest["skipped"] == manifest["unhandled"] == 0
    assert manifest["tool_versions"]["fixture"] == "1.0"


def test_record_command_persists_failure_and_returns_the_command_status(tmp_path: Path) -> None:
    output = tmp_path / "build.json"

    result = evidence.main(
        (
            "record-command",
            "--lane",
            "build",
            "--output",
            str(output),
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        )
    )

    assert result == 7
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["status"] == "failure"
    assert manifest["selected"] == manifest["failed"] == 1
    assert manifest["passed"] == 0
    assert manifest["errors"] == ["command_exit_nonzero:7"]


def _vitest_plain_test(case_id: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "file": "fixture.test.ts",
        "name": case_id,
        "mode": "run",
        "fails": False,
        "only": False,
        "state": "passed",
        "finalState": "passed",
        "retry": 0,
        "retries": 0,
        "retryCount": 0,
        "repeatCount": 0,
        "flaky": False,
        "errors": [],
    }


def _vitest_semantics_report(*tests: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "allowOnly": False,
        "moduleErrors": [],
        "numExpectedFailures": 0,
        "numFailedTests": 0,
        "numFlakyTests": 0,
        "numOnlyTests": 0,
        "numPassedTests": len(tests),
        "numPendingTests": 0,
        "numRepeatedTests": 0,
        "numRetriedTests": 0,
        "numTodoTests": 0,
        "numTotalTests": len(tests),
        "reason": "passed",
        "schemaVersion": "tracefold_vitest_report_v1",
        "success": True,
        "tests": list(tests),
        "unhandledErrors": [],
    }
    report.update(overrides)
    return report


def test_record_vitest_uses_the_reporters_actual_outcomes(tmp_path: Path) -> None:
    report = tmp_path / "vitest.json"
    output = tmp_path / "frontend-unit.json"
    report.write_text(
        json.dumps(
            _vitest_semantics_report(
                _vitest_plain_test("case-1"),
                _vitest_plain_test("case-2"),
                _vitest_plain_test("case-3"),
            )
        ),
        encoding="utf-8",
    )

    result = evidence.main(
        (
            "record-vitest",
            "--lane",
            "frontend-unit",
            "--input",
            str(report),
            "--output",
            str(output),
        )
    )

    assert result == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["selected"] == manifest["passed"] == 3
    assert manifest["failed"] == manifest["skipped"] == manifest["unhandled"] == 0
    assert manifest["status"] == "success"
    assert manifest["tool_versions"]["vitest"] == "4.1.5"


def test_record_vitest_fails_on_reported_unhandled_errors(tmp_path: Path) -> None:
    report = tmp_path / "vitest.json"
    output = tmp_path / "frontend-unit.json"
    report.write_text(
        json.dumps(
            _vitest_semantics_report(
                _vitest_plain_test("case-1"),
                success=False,
                unhandledErrors=[{"message": "unhandled request"}],
            )
        ),
        encoding="utf-8",
    )

    result = evidence.main(
        (
            "record-vitest",
            "--lane",
            "frontend-unit",
            "--input",
            str(report),
            "--output",
            str(output),
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["unhandled"] == 1
    assert manifest["status"] == "failure"


@pytest.mark.parametrize(
    ("test_changes", "report_changes", "manifest_field"),
    [
        ({"fails": True}, {"numExpectedFailures": 1}, "xfailed"),
        ({"retry": 1, "retryCount": 1, "flaky": True}, {"numRetriedTests": 1, "numFlakyTests": 1}, "rerun"),
        ({"repeats": 1, "repeatCount": 1}, {"numRepeatedTests": 1}, "rerun"),
        ({"only": True}, {"numOnlyTests": 1}, None),
    ],
)
def test_record_vitest_rejects_non_plain_pass_semantics(
    tmp_path: Path,
    test_changes: dict[str, Any],
    report_changes: dict[str, Any],
    manifest_field: str | None,
) -> None:
    report_path = tmp_path / "vitest.json"
    output = tmp_path / "frontend-unit.json"
    case = {**_vitest_plain_test("case-1"), **test_changes}
    report_path.write_text(
        json.dumps(_vitest_semantics_report(case, success=False, **report_changes)),
        encoding="utf-8",
    )

    result = evidence.main(
        (
            "record-vitest",
            "--lane",
            "frontend-unit",
            "--input",
            str(report_path),
            "--output",
            str(output),
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["status"] == "failure"
    if manifest_field is not None:
        assert manifest[manifest_field] == 1


def test_record_vitest_replaces_stale_green_output_when_report_is_invalid(tmp_path: Path) -> None:
    report = tmp_path / "vitest.json"
    output = tmp_path / "frontend-unit.json"
    report.write_text("not json\n", encoding="utf-8")
    output.write_text(json.dumps(_lane_payload("frontend-unit")), encoding="utf-8")

    result = evidence.main(
        (
            "record-vitest",
            "--lane",
            "frontend-unit",
            "--input",
            str(report),
            "--output",
            str(output),
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["status"] == "failure"
    assert manifest["selected"] == 0
    assert manifest["errors"] == ["vitest_report_invalid"]


def test_record_playwright_uses_the_reporters_actual_outcomes(tmp_path: Path) -> None:
    report = tmp_path / "playwright.json"
    output = tmp_path / "browser.json"
    report.write_text(
        json.dumps(
            {
                "config": {
                    "forbidOnly": True,
                    "grep": {},
                    "grepInvert": None,
                    "projects": [{"repeatEach": 1, "retries": 0}],
                    "shard": None,
                },
                "suites": [
                    {
                        "specs": [
                            {
                                "tests": [
                                    {
                                        "expectedStatus": "passed",
                                        "status": "expected",
                                        "results": [{"status": "passed", "retry": 0}],
                                    },
                                    {
                                        "expectedStatus": "passed",
                                        "status": "expected",
                                        "results": [{"status": "passed", "retry": 0}],
                                    },
                                ]
                            }
                        ]
                    }
                ],
                "stats": {
                    "expected": 2,
                    "unexpected": 0,
                    "flaky": 0,
                    "skipped": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    result = evidence.main(
        (
            "record-playwright",
            "--lane",
            "browser",
            "--input",
            str(report),
            "--output",
            str(output),
        )
    )

    assert result == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["selected"] == manifest["passed"] == 2
    assert manifest["failed"] == manifest["skipped"] == manifest["rerun"] == 0
    assert manifest["status"] == "success"
    assert manifest["tool_versions"]["playwright"] == "1.60.0"


def test_record_playwright_rejects_an_expected_failure_reported_as_expected(
    tmp_path: Path,
) -> None:
    report = tmp_path / "playwright.json"
    output = tmp_path / "browser.json"
    report.write_text(
        json.dumps(
            {
                "config": {
                    "forbidOnly": True,
                    "grep": {},
                    "grepInvert": None,
                    "projects": [{"repeatEach": 1, "retries": 0}],
                    "shard": None,
                },
                "suites": [
                    {
                        "specs": [
                            {
                                "tests": [
                                    {
                                        "expectedStatus": "failed",
                                        "status": "expected",
                                        "results": [{"status": "failed", "retry": 0}],
                                    }
                                ]
                            }
                        ]
                    }
                ],
                "stats": {"expected": 1, "unexpected": 0, "flaky": 0, "skipped": 0},
            }
        ),
        encoding="utf-8",
    )

    result = evidence.main(
        (
            "record-playwright",
            "--lane",
            "browser",
            "--input",
            str(report),
            "--output",
            str(output),
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["status"] == "failure"
    assert manifest["selected"] == manifest["xfailed"] == 1
    assert manifest["passed"] == 0
    assert "playwright_expected_status_forbidden:failed" in manifest["errors"]


@pytest.mark.parametrize(("field", "manifest_field"), [("flaky", "rerun"), ("skipped", "skipped")])
def test_record_playwright_rejects_flaky_or_skipped_required_cases(
    tmp_path: Path, field: str, manifest_field: str
) -> None:
    report = tmp_path / "playwright.json"
    output = tmp_path / "browser.json"
    stats = {"expected": 1, "unexpected": 0, "flaky": 0, "skipped": 0}
    stats[field] = 1
    report.write_text(json.dumps({"stats": stats}), encoding="utf-8")

    result = evidence.main(
        (
            "record-playwright",
            "--lane",
            "browser",
            "--input",
            str(report),
            "--output",
            str(output),
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest[manifest_field] == 1
    assert manifest["status"] == "failure"


def test_aggregate_rejects_malformed_or_cross_revision_lane(tmp_path: Path) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    (lane_dir / "python.json").write_text(
        json.dumps(
            _lane_payload(
                "different-name",
                passed=0,
                schema_version="tracefold_test_evidence_v1",
                required=False,
                commit_sha="0" * 40,
                git_tree_sha="1" * 40,
                errors=["nested failure"],
                tool_versions={},
            )
        ),
        encoding="utf-8",
    )

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "python",
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["overall"] == "failure"
    assert {
        "required_lane_schema_invalid:python",
        "required_lane_name_mismatch:python",
        "required_lane_not_required:python",
        "required_lane_pass_count_mismatch:python",
        "required_lane_has_errors:python",
        "required_lane_tool_versions_missing:python",
        "required_lane_commit_mismatch:python",
        "required_lane_tree_mismatch:python",
    } <= set(manifest["errors"])


def test_aggregate_succeeds_only_after_every_required_lane_is_green(tmp_path: Path) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    for lane, selected in (("python", 3), ("frontend-unit", 2)):
        (lane_dir / f"{lane}.json").write_text(
            json.dumps(_lane_payload(lane, selected=selected, passed=selected)), encoding="utf-8"
        )

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "python",
            "--required-lane",
            "frontend-unit",
        )
    )

    assert result == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["overall"] == "success"
    assert manifest["errors"] == []
    assert set(manifest["lanes"]) == {"python", "frontend-unit"}


def test_canonical_aggregate_requires_an_independent_resource_lane() -> None:
    result = subprocess.run(
        ["make", "--dry-run", "test-evidence"],
        cwd=Path(evidence.__file__).resolve().parents[2],
        capture_output=True,
        check=True,
        text=True,
    )

    assert "--required-lane resource" in result.stdout
    assert "resource.json" in result.stdout


def test_ci_hypothesis_profile_is_deterministic_and_replayable() -> None:
    profile = settings.get_profile("ci")

    assert profile.derandomize is True
    assert profile.database is None
    assert profile.print_blob is True
    assert settings.get_profile("nightly").derandomize is False


def test_python_lane_records_replay_and_tool_identity(evidence_repository: _EvidenceRepository) -> None:
    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert manifest["schema_version"] == "tracefold_test_lane_v2"
    assert manifest["lane"] == "python"
    assert manifest["status"] == "success"
    assert manifest["selected"] == manifest["passed"] == 2
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["git_tree_sha"])
    assert {"python", "pytest", "hypothesis", "uv", "node"} <= set(manifest["tool_versions"])
    assert {plugin["name"] for plugin in manifest["pytest_plugins"]} == {
        "hypothesis",
        "tracefold-evidence",
    }
    assert manifest["hypothesis"] == {
        "profile": "ci",
        "derandomize": True,
        "database": None,
        "print_blob": True,
        "replay_policy": "derandomize",
    }


def test_python_lane_rejects_transitive_pytest_plugin_autoload(
    evidence_repository: _EvidenceRepository,
) -> None:
    result, manifest = evidence_repository.run(
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "0", "TRACEFOLD_FIXTURE_FAIL": "0"}
    )

    assert result.returncode != 0
    assert "evidence_pytest_plugin_autoload_must_be_disabled" in manifest["errors"]


def test_python_lane_fails_on_a_real_unhandled_thread_exception(
    evidence_repository: _EvidenceRepository,
) -> None:
    (evidence_repository.root / "tests" / "test_unhandled.py").write_text(
        "import threading\n"
        "def test_unhandled_thread_exception():\n"
        "    def explode(): raise RuntimeError('background exploded')\n"
        "    thread = threading.Thread(target=explode)\n"
        "    thread.start()\n"
        "    thread.join()\n",
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0, result.stdout + result.stderr
    assert manifest["unhandled"] >= 1
    assert manifest["status"] == "failure"
    assert any(error.startswith("evidence_python_unhandled:") for error in manifest["errors"])


def test_python_lane_rejects_a_case_filter_that_hides_unhandled_thread_exceptions(
    evidence_repository: _EvidenceRepository,
) -> None:
    (evidence_repository.root / "tests" / "test_unhandled.py").write_text(
        "import threading\n"
        "import pytest\n"
        "@pytest.mark.filterwarnings('ignore::pytest.PytestUnhandledThreadExceptionWarning')\n"
        "def test_hidden_thread_exception():\n"
        "    def explode(): raise RuntimeError('background exploded')\n"
        "    thread = threading.Thread(target=explode)\n"
        "    thread.start()\n"
        "    thread.join()\n",
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0
    assert any(
        "evidence_unhandled_warning_filter_forbidden:tests/test_unhandled.py::test_hidden_thread_exception" in error
        for error in manifest["errors"]
    )


def test_python_lane_rejects_a_cli_filter_that_hides_unhandled_thread_exceptions(
    evidence_repository: _EvidenceRepository,
) -> None:
    result, manifest = evidence_repository.run(
        "-W",
        "ignore::pytest.PytestUnhandledThreadExceptionWarning",
        env={"TRACEFOLD_FIXTURE_FAIL": "0"},
    )

    assert result.returncode != 0
    assert (
        "evidence_unhandled_warning_filter_forbidden:ignore::pytest.PytestUnhandledThreadExceptionWarning"
        in manifest["errors"]
    )


def test_python_lane_rejects_an_ini_filter_that_hides_unhandled_runtime_warnings(
    evidence_repository: _EvidenceRepository,
) -> None:
    pyproject = evidence_repository.root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8") + "filterwarnings = ['ignore::RuntimeWarning']\n",
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0
    assert "evidence_unhandled_warning_filter_forbidden:ignore::RuntimeWarning" in manifest["errors"]


def test_python_lane_fails_on_an_unretrieved_asyncio_task_exception(
    evidence_repository: _EvidenceRepository,
) -> None:
    (evidence_repository.root / "tests" / "test_asyncio_unhandled.py").write_text(
        "import asyncio\n"
        "async def scenario():\n"
        "    async def explode(): raise RuntimeError('task exploded')\n"
        "    asyncio.create_task(explode())\n"
        "    await asyncio.sleep(0)\n"
        "def test_unretrieved_task_exception():\n"
        "    asyncio.run(scenario())\n",
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0
    assert manifest["unhandled"] == 1
    assert any("evidence_python_unhandled:asyncio_task" in error for error in manifest["errors"])


def test_python_lane_rejects_an_empty_declared_required_marker(
    evidence_repository: _EvidenceRepository,
) -> None:
    pyproject = evidence_repository.root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            "markers = ['live: external provider test']",
            "markers = ['live: external provider test', 'golden: required evidence lane']",
        ),
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0
    assert "evidence_required_marker_lane_empty:golden" in manifest["errors"]
    assert manifest["marker_lanes"]["golden"] == {
        "status": "failure",
        "selected": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "rerun": 0,
        "unhandled": 0,
    }


def test_python_lane_records_actual_required_marker_outcomes(
    evidence_repository: _EvidenceRepository,
) -> None:
    pyproject = evidence_repository.root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            "markers = ['live: external provider test']",
            "markers = ['live: external provider test', 'golden: required evidence lane']",
        ),
        encoding="utf-8",
    )
    (evidence_repository.root / "tests" / "test_pass.py").write_text(
        "import pytest\n@pytest.mark.golden\ndef test_pass(): assert True\n", encoding="utf-8"
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert manifest["marker_lanes"]["golden"] == {
        "status": "success",
        "selected": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "rerun": 0,
        "unhandled": 0,
    }


def test_python_lane_records_failed_required_marker_outcome(
    evidence_repository: _EvidenceRepository,
) -> None:
    pyproject = evidence_repository.root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            "markers = ['live: external provider test']",
            "markers = ['live: external provider test', 'golden: required evidence lane']",
        ),
        encoding="utf-8",
    )
    (evidence_repository.root / "tests" / "test_fail.py").write_text(
        "import os\nimport pytest\n@pytest.mark.golden\n"
        "def test_fail(): assert os.environ.get('TRACEFOLD_FIXTURE_FAIL') != '1'\n",
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run()

    assert result.returncode != 0
    assert manifest["marker_lanes"]["golden"]["status"] == "failure"
    assert manifest["marker_lanes"]["golden"]["selected"] == 1
    assert manifest["marker_lanes"]["golden"]["passed"] == 0
    assert manifest["marker_lanes"]["golden"]["failed"] == 1
