from __future__ import annotations

import hashlib
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

from scripts import ci_plan, verification_topology
from tests.support import evidence, profile

pytestmark = pytest.mark.slow


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
                "not live and not scheduled",
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
    (root / "scripts").mkdir()
    (root / "tracefold" / "platform" / "postgres").mkdir(parents=True)
    (root / "web").mkdir()
    shutil.copy2(Path(evidence.__file__).resolve(), root / "tests" / "support" / "evidence.py")
    shutil.copy2(Path(profile.__file__).resolve(), root / "tests" / "support" / "profile.py")
    shutil.copy2(Path(verification_topology.__file__).resolve(), root / "scripts" / "verification_topology.py")
    for package in (
        root / "tests",
        root / "tests" / "support",
        root / "scripts",
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
        "[tool.pytest.ini_options]\n"
        "testpaths = ['tests']\n"
        "markers = ['live: external provider test', 'scheduled: production-duration diagnostic']\n",
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
    process_env.pop("PYTHONWARNINGS", None)
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


def test_evidence_rejects_runxfail_even_when_it_turns_an_xpass_into_a_pass(
    evidence_repository: _EvidenceRepository,
) -> None:
    xfail_module = evidence_repository.root / "tests" / "test_passing_xfail.py"
    xfail_module.write_text(
        "import pytest\n@pytest.mark.xfail\ndef test_passing_xfail(): assert True\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", str(xfail_module.relative_to(evidence_repository.root))],
        cwd=evidence_repository.root,
        check=True,
    )

    result, manifest = evidence_repository.run("--runxfail", env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0
    assert "evidence_runxfail_forbidden" in manifest["errors"]


def test_evidence_rejects_override_ini_that_hides_a_failing_item_in_a_collected_module(
    evidence_repository: _EvidenceRepository,
) -> None:
    for name in ("test_pass.py", "test_fail.py"):
        (evidence_repository.root / "tests" / name).unlink()
    mixed_module = evidence_repository.root / "tests" / "test_mixed.py"
    mixed_module.write_text(
        "def visible(): assert True\ndef hidden_failure(): assert False\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=evidence_repository.root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tracefold Test",
            "-c",
            "user.email=tests@tracefold.invalid",
            "commit",
            "-qm",
            "override ini fixture",
        ],
        cwd=evidence_repository.root,
        check=True,
    )

    result, manifest = evidence_repository.run("--override-ini=python_functions=visible")

    assert result.returncode != 0
    assert "evidence_override_ini_forbidden" in manifest["errors"]


def test_evidence_rejects_last_failed_selection(evidence_repository: _EvidenceRepository) -> None:
    result, manifest = evidence_repository.run("--lf", "--ignore=tests/test_fail.py")

    assert result.returncode != 0
    assert "evidence_collection_option_forbidden:--lf" in manifest["errors"]


def test_evidence_requires_every_tracked_test_module(evidence_repository: _EvidenceRepository) -> None:
    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_IGNORE": "1"})

    assert result.returncode != 0
    assert "evidence_tracked_test_module_not_collected:tests/test_fail.py" in manifest["errors"]


@pytest.mark.parametrize("marker", ["live", "scheduled"])
def test_explicitly_deselected_tracked_module_participates_in_collection_without_being_selected(
    evidence_repository: _EvidenceRepository, marker: str
) -> None:
    deselected_module = evidence_repository.root / "tests" / f"test_{marker}_only.py"
    deselected_module.write_text(
        f"import pytest\n@pytest.mark.{marker}\ndef test_{marker}_only(): assert True\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", str(deselected_module.relative_to(evidence_repository.root))],
        cwd=evidence_repository.root,
        check=True,
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert manifest["selected"] == manifest["passed"] == 2
    assert not any(deselected_module.name in error for error in manifest["errors"])


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
    output.write_text('{"schema_version":"tracefold_test_evidence_v3","overall":"success"}\n', encoding="utf-8")

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "python-hermetic",
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "tracefold_test_evidence_v3"
    assert manifest["overall"] == "failure"
    assert {
        "python_inventory_missing",
        "required_lane_manifest_missing:python-hermetic",
    } == set(manifest["errors"])


def test_aggregate_replaces_stale_green_output_when_a_lane_is_invalid(tmp_path: Path) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    (lane_dir / "python-hermetic.json").write_text("not json\n", encoding="utf-8")
    output.write_text('{"schema_version":"tracefold_test_evidence_v3","overall":"success"}\n', encoding="utf-8")

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "python-hermetic",
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["overall"] == "failure"
    assert {
        "python_inventory_missing",
        "required_lane_manifest_invalid:python-hermetic",
    } == set(manifest["errors"])


def _lane_payload(lane: str, *, selected: int = 1, passed: int = 1, **overrides: Any) -> dict[str, Any]:
    root = Path(evidence.__file__).resolve().parents[2]
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    tree_sha = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    payload: dict[str, Any] = {
        "schema_version": "tracefold_test_lane_v3",
        "lane": lane,
        "required": True,
        "status": "success",
        "commit_sha": commit_sha,
        "git_tree_sha": tree_sha,
        "uv_lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
        "package_lock_sha256": hashlib.sha256((root / "web" / "package-lock.json").read_bytes()).hexdigest(),
        "plan_sha256": evidence._plan_sha256(),
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
        "worktree": {"sealed": True, "clean": True, "changes": []},
    }
    if lane in evidence.PYTHON_LANES:
        nodeids = [f"tests/test_fixture.py::test_{index}" for index in range(selected)]
        payload.update(
            {
                "selected_nodeids": nodeids,
                "inventory_nodeids": nodeids,
                "inventory_count": len(nodeids),
                "inventory_sha256": evidence._nodeids_sha256(nodeids),
                "migration_head": evidence._migration_head(),
            }
        )
    payload.update(overrides)
    return payload


def _playwright_selection(selected: int) -> dict[str, Any]:
    default_grep = [{"flags": "", "source": ".*"}]
    return {
        "configFile": "playwright.full-stack.config.ts",
        "forbidOnly": True,
        "fullyParallel": False,
        "grep": default_grep,
        "grepInvert": [],
        "invocation": ["test", "--config=playwright.full-stack.config.ts"],
        "maxFailures": 0,
        "projects": [
            {
                "browserName": "chromium",
                "grep": default_grep,
                "grepInvert": [],
                "name": "required-chromium",
                "repeatEach": 1,
                "retries": 0,
                "testDir": "tests/e2e/full-stack",
                "testIgnore": [],
                "testMatch": [{"literal": "**/*.@(spec|test).?(c|m)[jt]s?(x)"}],
            }
        ],
        "schemaVersion": "tracefold_playwright_selection_v1",
        "selectedTestIds": [f"fixture-{index}" for index in range(selected)],
        "selectedTestFiles": ["tests/e2e/full-stack/fastapi-news-smoke.spec.ts"],
        "shard": None,
    }


def test_aggregate_fails_when_a_required_lane_is_empty(tmp_path: Path) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    (lane_dir / "python-hermetic.json").write_text(
        json.dumps(_lane_payload("python-hermetic", selected=0, passed=0)), encoding="utf-8"
    )

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "python-hermetic",
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["overall"] == "failure"
    assert "required_lane_empty:python-hermetic" in manifest["errors"]


@pytest.mark.parametrize("field", ["failed", "skipped", "xfailed", "xpassed", "rerun", "unhandled"])
def test_aggregate_fails_on_every_non_green_required_lane_outcome(tmp_path: Path, field: str) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    (lane_dir / "python-hermetic.json").write_text(
        json.dumps(_lane_payload("python-hermetic", **{field: 1})), encoding="utf-8"
    )

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "python-hermetic",
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["overall"] == "failure"
    assert f"required_lane_not_green:python-hermetic:{field}=1" in manifest["errors"]


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
    assert manifest["schema_version"] == "tracefold_test_lane_v3"
    assert manifest["lane"] == "typecheck"
    assert manifest["status"] == "success"
    assert manifest["selected"] == manifest["passed"] == 1
    assert manifest["failed"] == manifest["skipped"] == manifest["unhandled"] == 0
    assert manifest["tool_versions"]["fixture"] == "1.0"
    assert manifest["worktree"] == {"sealed": False, "clean": False, "changes": []}


def test_lane_manifest_is_accepted_only_after_the_exact_tree_is_sealed(tmp_path: Path) -> None:
    output = tmp_path / "lane.json"
    unsealed = _lane_payload("frontend-unit", worktree={"sealed": False, "clean": False, "changes": []})
    output.write_text(json.dumps(unsealed), encoding="utf-8")

    assert evidence.main(("seal-clean", "--manifest", str(output))) == 0

    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["status"] == "success"
    assert manifest["worktree"] == {"sealed": True, "clean": True, "changes": []}


def test_aggregate_rejects_an_unsealed_green_lane(tmp_path: Path) -> None:
    lane_dir = tmp_path / "lanes"
    lane_dir.mkdir()
    payload = _lane_payload(
        "frontend-unit",
        worktree={"sealed": False, "clean": False, "changes": []},
    )
    (lane_dir / "frontend-unit.json").write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "manifest.json"

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "frontend-unit",
        )
    )

    assert result != 0
    assert "required_lane_worktree_not_clean:frontend-unit" in json.loads(output.read_text(encoding="utf-8"))["errors"]


def test_seal_clean_persists_dirty_state_and_invalidates_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    (root / "web").mkdir(parents=True)
    (root / "uv.lock").write_text("uv\n", encoding="utf-8")
    (root / "web" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (root / "tracked.txt").write_text("clean\n", encoding="utf-8")
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
    monkeypatch.setattr(evidence, "_REPO_ROOT", root)
    output = root / "lane.json"
    output.write_text(
        json.dumps(evidence._lane_payload(lane="fixture", selected=1, passed=1, root=root)), encoding="utf-8"
    )
    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    assert evidence.main(("seal-clean", "--manifest", str(output))) != 0

    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["status"] == "failure"
    assert manifest["worktree"]["sealed"] is True
    assert manifest["worktree"]["clean"] is False
    assert " M tracked.txt" in manifest["worktree"]["changes"]
    assert "evidence_tested_head_dirty" in manifest["errors"]


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


def test_record_command_rejects_a_cross_revision_github_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "quality-static.json"
    monkeypatch.setenv("GITHUB_SHA", "0" * 40)

    result = evidence.main(
        (
            "record-command",
            "--lane",
            "quality-static",
            "--output",
            str(output),
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        )
    )

    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert result == 1
    assert manifest["status"] == "failure"
    assert manifest["errors"] == ["evidence_github_sha_mismatch"]


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
    test_files = sorted({str(test["file"]) for test in tests})
    report: dict[str, Any] = {
        "allowOnly": False,
        "invocation": ["run", *test_files],
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
        "numXfailedTests": 0,
        "numXpassedTests": 0,
        "reason": "passed",
        "schemaVersion": "tracefold_vitest_report_v3",
        "success": True,
        "testFiles": test_files,
        "tests": list(tests),
        "unhandledErrors": [],
    }
    report.update(overrides)
    return report


def test_record_vitest_uses_the_reporters_actual_outcomes(tmp_path: Path) -> None:
    report = tmp_path / "vitest.json"
    output = tmp_path / "frontend-fixture.json"
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
            "frontend-fixture",
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
    output = tmp_path / "frontend-fixture.json"
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
            "frontend-fixture",
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
        (
            {"fails": True},
            {"numExpectedFailures": 1, "numPassedTests": 0, "numXfailedTests": 1},
            "xfailed",
        ),
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
    output = tmp_path / "frontend-fixture.json"
    case = {**_vitest_plain_test("case-1"), **test_changes}
    report_path.write_text(
        json.dumps(_vitest_semantics_report(case, success=False, **report_changes)),
        encoding="utf-8",
    )

    result = evidence.main(
        (
            "record-vitest",
            "--lane",
            "frontend-fixture",
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
    output = tmp_path / "frontend-fixture.json"
    report.write_text("not json\n", encoding="utf-8")
    output.write_text(json.dumps(_lane_payload("frontend-fixture")), encoding="utf-8")

    result = evidence.main(
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

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["status"] == "failure"
    assert manifest["selected"] == 0
    assert manifest["errors"] == ["vitest_report_invalid"]


def _playwright_plain_report(selected: int) -> dict[str, Any]:
    return {
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
                            }
                            for _ in range(selected)
                        ]
                    }
                ]
            }
        ],
        "stats": {
            "expected": selected,
            "unexpected": 0,
            "flaky": 0,
            "skipped": 0,
        },
    }


def test_record_playwright_uses_the_reporters_actual_outcomes_and_resource_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "playwright.json"
    selection = tmp_path / "playwright-selection.json"
    output = tmp_path / "browser.json"
    report.write_text(json.dumps(_playwright_plain_report(2)), encoding="utf-8")
    selection.write_text(json.dumps(_playwright_selection(2)), encoding="utf-8")
    monkeypatch.setattr(
        evidence,
        "_resource_identity",
        lambda required: (
            {"postgresql-server": "17", "rabbitmq-server": "4.1"},
            {name: {"fixture": True} for name in required},
            [],
        ),
    )

    result = evidence.main(
        (
            "record-playwright",
            "--lane",
            "browser",
            "--input",
            str(report),
            "--selection",
            str(selection),
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
    assert set(manifest["metadata"]["resources"]) == {"postgresql", "rabbitmq"}


@pytest.mark.parametrize("tool", ["vitest", "playwright"])
@pytest.mark.parametrize(
    ("runtime_version", "expected_error"),
    [
        ("999.0.0", "lock_runtime_version_mismatch"),
        ("unavailable", "runtime_version_unavailable"),
    ],
)
def test_node_test_evidence_fails_closed_on_runtime_version_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    runtime_version: str,
    expected_error: str,
) -> None:
    report = tmp_path / f"{tool}.json"
    output = tmp_path / f"{tool}-lane.json"
    monkeypatch.setattr(evidence, "_node_module_version", lambda package: runtime_version)
    if tool == "vitest":
        report.write_text(
            json.dumps(_vitest_semantics_report(_vitest_plain_test("case-1"))),
            encoding="utf-8",
        )
        arguments = (
            "record-vitest",
            "--lane",
            "frontend-fixture",
            "--input",
            str(report),
            "--output",
            str(output),
        )
    else:
        selection = tmp_path / "playwright-selection.json"
        report.write_text(json.dumps(_playwright_plain_report(1)), encoding="utf-8")
        selection.write_text(json.dumps(_playwright_selection(1)), encoding="utf-8")
        arguments = (
            "record-playwright",
            "--lane",
            "browser-fixture",
            "--input",
            str(report),
            "--selection",
            str(selection),
            "--output",
            str(output),
        )

    result = evidence.main(arguments)

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert f"{tool}_{expected_error}" in manifest["errors"]
    assert manifest["tool_versions"][tool] == runtime_version


def test_record_playwright_rejects_an_expected_failure_reported_as_expected(
    tmp_path: Path,
) -> None:
    report = tmp_path / "playwright.json"
    selection = tmp_path / "playwright-selection.json"
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
    selection.write_text(json.dumps(_playwright_selection(1)), encoding="utf-8")

    result = evidence.main(
        (
            "record-playwright",
            "--lane",
            "browser",
            "--input",
            str(report),
            "--selection",
            str(selection),
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
    selection = tmp_path / "playwright-selection.json"
    output = tmp_path / "browser.json"
    stats = {"expected": 1, "unexpected": 0, "flaky": 0, "skipped": 0}
    stats[field] = 1
    report.write_text(json.dumps({"stats": stats}), encoding="utf-8")
    selection.write_text(json.dumps(_playwright_selection(2)), encoding="utf-8")

    result = evidence.main(
        (
            "record-playwright",
            "--lane",
            "browser",
            "--input",
            str(report),
            "--selection",
            str(selection),
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
    (lane_dir / "python-hermetic.json").write_text(
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
            "python-hermetic",
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["overall"] == "failure"
    assert {
        "required_lane_schema_invalid:python-hermetic",
        "required_lane_name_mismatch:python-hermetic",
        "required_lane_not_required:python-hermetic",
        "required_lane_pass_count_mismatch:python-hermetic",
        "required_lane_has_errors:python-hermetic",
        "required_lane_tool_versions_missing:python-hermetic",
        "required_lane_commit_mismatch:python-hermetic",
        "required_lane_tree_mismatch:python-hermetic",
    } <= set(manifest["errors"])


@pytest.mark.parametrize(
    ("field", "error"),
    [
        ("uv_lock_sha256", "required_lane_uv_lock_mismatch:frontend-unit"),
        ("package_lock_sha256", "required_lane_package_lock_mismatch:frontend-unit"),
        ("plan_sha256", "required_lane_plan_mismatch:frontend-unit"),
    ],
)
def test_aggregate_rejects_cross_lock_or_plan_lane(tmp_path: Path, field: str, error: str) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    payload = _lane_payload("frontend-unit", **{field: "0" * 64})
    (lane_dir / "frontend-unit.json").write_text(json.dumps(payload), encoding="utf-8")

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "frontend-unit",
        )
    )

    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert result != 0
    assert error in manifest["errors"]


def test_aggregate_succeeds_only_after_every_required_lane_is_green(tmp_path: Path) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    for lane, selected in (("python-hermetic", 3), ("frontend-unit", 2)):
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
            "python-hermetic",
            "--required-lane",
            "frontend-unit",
        )
    )

    assert result == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["overall"] == "success"
    assert manifest["errors"] == []
    assert set(manifest["lanes"]) == {"python-hermetic", "frontend-unit"}


def test_selected_plan_aggregate_records_every_not_required_lane_without_requiring_its_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "tested_head_changes", lambda root: ())
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    plan_path = tmp_path / "plan.json"
    lane_dir.mkdir()
    root = Path(evidence.__file__).resolve().parents[2]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True, text=True
    ).stdout.strip()
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=("README.md",),
        tested_sha=head,
        base_sha="0" * 40,
    )
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("TRACEFOLD_CI_PLAN_SHA256", plan["plan_sha256"])
    (lane_dir / "quality-static.json").write_text(json.dumps(_lane_payload("quality-static")), encoding="utf-8")

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--plan",
            str(plan_path),
        )
    )

    assert result == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["overall"] == "success"
    assert manifest["plan_sha256"] == plan["plan_sha256"]
    assert manifest["required_lanes"] == ["quality-static"]
    assert set(manifest["not_required"]) == set(evidence.REQUIRED_LANES) - {"quality-static"}
    assert manifest["not_required"]["runtime-process"] == "no_changed_surface_requires_lane"
    assert manifest["inventory"]["scope"] == "selected"


def test_selected_plan_aggregate_rejects_a_manifest_for_a_not_required_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "tested_head_changes", lambda root: ())
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    plan_path = tmp_path / "plan.json"
    lane_dir.mkdir()
    root = Path(evidence.__file__).resolve().parents[2]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True, text=True
    ).stdout.strip()
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=("README.md",),
        tested_sha=head,
        base_sha="0" * 40,
    )
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("TRACEFOLD_CI_PLAN_SHA256", plan["plan_sha256"])
    for lane in ("quality-static", "runtime-process"):
        (lane_dir / f"{lane}.json").write_text(json.dumps(_lane_payload(lane)), encoding="utf-8")

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--plan",
            str(plan_path),
        )
    )

    assert result != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert "not_required_lane_manifest_present:runtime-process" in manifest["errors"]


def test_selected_python_plan_proves_the_complete_union_of_each_required_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "tested_head_changes", lambda root: ())
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    plan_path = tmp_path / "plan.json"
    lane_dir.mkdir()
    root = Path(evidence.__file__).resolve().parents[2]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True, text=True
    ).stdout.strip()
    plan = ci_plan.build_plan(
        event="pull_request",
        changed_paths=("src/tracefold/news/events/facts.py",),
        tested_sha=head,
        base_sha="0" * 40,
    )
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("TRACEFOLD_CI_PLAN_SHA256", plan["plan_sha256"])
    inventory = [f"tests/test_fixture.py::test_{index}" for index in range(4)]
    ownership = {lane: [] for lane in evidence.PYTHON_LANES}
    ownership.update(
        {
            "python-hermetic": [inventory[0]],
            "postgres-behavior": [inventory[1]],
            "runtime-process": inventory[2:],
        }
    )
    (lane_dir / "quality-static.json").write_text(json.dumps(_lane_payload("quality-static")), encoding="utf-8")
    for lane in ("python-hermetic", "postgres-behavior", "runtime-process"):
        selected = ownership[lane]
        payload = _lane_payload(lane, selected=len(selected), passed=len(selected))
        payload.update(
            {
                "selected_nodeids": selected,
                "inventory_nodeids": inventory,
                "ownership_nodeids": ownership,
                "inventory_count": len(inventory),
                "inventory_sha256": evidence._nodeids_sha256(inventory),
            }
        )
        (lane_dir / f"{lane}.json").write_text(json.dumps(payload), encoding="utf-8")

    arguments = (
        "aggregate",
        "--lane-dir",
        str(lane_dir),
        "--output",
        str(output),
        "--plan",
        str(plan_path),
    )
    assert evidence.main(arguments) == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["inventory"]["expected"] == manifest["inventory"]["executed"] == 4

    runtime_path = lane_dir / "runtime-process.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime["selected_nodeids"] = runtime["selected_nodeids"][:1]
    runtime["selected"] = runtime["passed"] = 1
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

    assert evidence.main(arguments) != 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert "required_lane_owner_selection_mismatch:runtime-process" in manifest["errors"]
    assert manifest["inventory"]["missing"] == [inventory[3]]


@pytest.mark.parametrize(
    ("second_selection", "expected_error", "inventory_field"),
    [
        (["tests/test_fixture.py::test_1"], "python_inventory_missing_nodeids:1", "missing"),
        (
            ["tests/test_fixture.py::test_0", "tests/test_fixture.py::test_1", "tests/test_fixture.py::test_2"],
            "python_inventory_duplicate_nodeids:1",
            "duplicates",
        ),
    ],
)
def test_v3_union_fails_closed_on_missing_or_duplicate_nodeids(
    tmp_path: Path,
    second_selection: list[str],
    expected_error: str,
    inventory_field: str,
) -> None:
    lane_dir = tmp_path / "lanes"
    output = tmp_path / "manifest.json"
    lane_dir.mkdir()
    inventory = [f"tests/test_fixture.py::test_{index}" for index in range(3)]
    selections = {
        "python-hermetic": [inventory[0]],
        "trust-root": second_selection,
    }
    for lane, selected_nodeids in selections.items():
        payload = _lane_payload(lane, selected=len(selected_nodeids), passed=len(selected_nodeids))
        payload.update(
            {
                "selected_nodeids": selected_nodeids,
                "inventory_nodeids": inventory,
                "inventory_count": len(inventory),
                "inventory_sha256": evidence._nodeids_sha256(inventory),
            }
        )
        (lane_dir / f"{lane}.json").write_text(json.dumps(payload), encoding="utf-8")

    result = evidence.main(
        (
            "aggregate",
            "--lane-dir",
            str(lane_dir),
            "--output",
            str(output),
            "--required-lane",
            "python-hermetic",
            "--required-lane",
            "trust-root",
        )
    )

    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert result != 0
    assert expected_error in manifest["errors"]
    assert manifest["inventory"][inventory_field]


@pytest.mark.parametrize(
    ("path", "markers", "owner"),
    [
        ("tests/news/test_news_v3_pure.py", set(), "python-hermetic"),
        ("tests/news/test_news_v3_pure.py", {"slow", "external_codegen"}, "python-hermetic"),
        ("tests/contract/test_cli.py", {"contract"}, "python-hermetic"),
        ("tests/contract/test_ci_impact_plan.py", {"contract"}, "trust-root"),
        ("tests/contract/test_evidence_v3_contract.py", {"contract", "slow"}, "trust-root"),
        ("tests/deploy/test_main_ci_gate.py", {"deploy"}, "trust-root"),
        ("tests/contract/test_openapi_codegen.py", {"contract"}, "frontend-python"),
        ("tests/contract/test_hook_installer.py", {"contract"}, "runtime-process"),
        ("tests/integration/test_news_v3_pipeline.py", {"integration"}, "postgres-behavior"),
        ("tests/integration/test_cli_resources.py", {"integration"}, "runtime-process"),
        ("tests/integration/test_trading_migration.py", {"integration"}, "migration"),
        ("tests/integration/test_workers_runtime_v2.py", {"integration", "slow"}, "runtime-process"),
        ("tests/integration/test_news_bus_rabbitmq.py", {"integration"}, "runtime-process"),
        ("tests/e2e/test_serve_process_smoke.py", set(), "runtime-process"),
        ("tests/golden/test_news_production_pipeline.py", set(), "runtime-process"),
        ("tests/slow/test_frontend_harness_fail_closed.py", {"slow"}, "trust-root"),
    ],
)
def test_phase_one_ownership_has_one_explicit_primary_lane(path: str, markers: set[str], owner: str) -> None:
    assert evidence.primary_lane_owner(path, markers) == owner
    assert owner in evidence.PYTHON_LANES


def test_plan_identity_binds_every_required_lane_and_its_command(monkeypatch: pytest.MonkeyPatch) -> None:
    original = evidence._plan_sha256()
    commands = dict(evidence._FULL_PLAN_COMMANDS)
    commands["frontend-build"] = ("npm --prefix web run build:mutated",)
    monkeypatch.setattr(evidence, "_FULL_PLAN_COMMANDS", commands)

    assert set(commands) == set(evidence.REQUIRED_LANES)
    assert evidence._plan_sha256() != original


def test_canonical_aggregate_requires_every_primary_resource_owner() -> None:
    result = subprocess.run(
        ["make", "--dry-run", "test-evidence"],
        cwd=Path(evidence.__file__).resolve().parents[2],
        capture_output=True,
        check=True,
        text=True,
    )

    assert "--required-lane postgres-behavior" in result.stdout
    assert "--required-lane migration" in result.stdout
    assert "--required-lane runtime-process" in result.stdout
    assert "--required-lane resource" not in result.stdout


def test_ci_hypothesis_profile_is_deterministic_and_replayable() -> None:
    profile = settings.get_profile("ci")

    assert profile.derandomize is True
    assert profile.database is None
    assert profile.print_blob is True
    assert settings.get_profile("nightly").derandomize is False


def test_python_lane_records_replay_and_tool_identity(evidence_repository: _EvidenceRepository) -> None:
    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert manifest["schema_version"] == "tracefold_test_lane_v3"
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


def test_python_lane_allows_the_code_owned_profile_recorder(evidence_repository: _EvidenceRepository) -> None:
    profile_path = evidence_repository.root.parent / "profile.json"
    result, manifest = evidence_repository.run(
        "-p",
        "tests.support.profile",
        f"--test-profile={profile_path}",
        "--test-profile-lane=python",
        env={"TRACEFOLD_FIXTURE_FAIL": "0"},
    )

    assert result.returncode == 0, result.stdout + result.stderr + json.dumps(manifest, indent=2)
    assert {plugin["name"] for plugin in manifest["pytest_plugins"]} == {
        "hypothesis",
        "tracefold-evidence",
        "tracefold-test-profile",
    }
    profile_manifest = json.loads(profile_path.read_text(encoding="utf-8"))
    assert profile_manifest["schema_version"] == profile.PROFILE_SCHEMA_VERSION
    assert profile_manifest["selected"] == 2


def test_python_lane_manifest_rejects_a_nonzero_session_exit_without_a_test_failure(
    evidence_repository: _EvidenceRepository,
) -> None:
    conftest = evidence_repository.root / "tests" / "conftest.py"
    conftest.write_text(
        conftest.read_text(encoding="utf-8")
        + "\nimport pytest\n"
        + "def pytest_sessionfinish(session, exitstatus):\n"
        + "  session.exitstatus = pytest.ExitCode.INTERRUPTED\n",
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode == int(pytest.ExitCode.INTERRUPTED), result.stdout + result.stderr
    assert manifest["failed"] == 0
    assert manifest["status"] == "failure"
    assert "evidence_pytest_exitstatus_nonzero:2" in manifest["errors"]


def test_python_lane_rejects_transitive_pytest_plugin_autoload(
    evidence_repository: _EvidenceRepository,
) -> None:
    result, manifest = evidence_repository.run(
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "0", "TRACEFOLD_FIXTURE_FAIL": "0"}
    )

    assert result.returncode != 0
    assert "evidence_pytest_plugin_autoload_must_be_disabled" in manifest["errors"]


@pytest.mark.parametrize("module_name", ["extra_plugin", "evilconftest"])
def test_python_lane_records_and_rejects_an_extra_explicit_plugin(
    evidence_repository: _EvidenceRepository, module_name: str
) -> None:
    (evidence_repository.root / f"{module_name}.py").write_text("__version__ = 'fixture-1'\n", encoding="utf-8")

    result, manifest = evidence_repository.run("-p", module_name, env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0
    assert f"evidence_pytest_plugin_forbidden:{module_name}" in manifest["errors"]
    plugins = {plugin["name"]: plugin["version"] for plugin in manifest["pytest_plugins"]}
    assert set(plugins) == {module_name, "hypothesis", "tracefold-evidence"}
    assert plugins[module_name] == "fixture-1"


@pytest.mark.parametrize(
    ("plugin_name", "module_name"),
    [
        ("threadexception", "_pytest.threadexception"),
        ("unraisableexception", "_pytest.unraisableexception"),
        ("warnings", "_pytest.warnings"),
    ],
)
def test_python_lane_requires_core_background_exception_plugins(
    evidence_repository: _EvidenceRepository, plugin_name: str, module_name: str
) -> None:
    result, manifest = evidence_repository.run("-p", f"no:{plugin_name}", env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0
    assert f"evidence_pytest_core_plugin_missing:{module_name}" in manifest["errors"]


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


@pytest.mark.parametrize(
    ("kind", "cleanup_body"),
    [
        (
            "thread_exception",
            "    def explode(): raise RuntimeError('late background exploded')\n"
            "    thread = threading.Thread(target=explode)\n"
            "    thread.start()\n"
            "    thread.join()\n",
        ),
        (
            "unraisable_exception",
            "    class BrokenCleanup:\n"
            "        def __del__(self): raise RuntimeError('late unraisable exploded')\n"
            "    broken = BrokenCleanup()\n"
            "    broken.cycle = broken\n"
            "    del broken\n"
            "    gc.collect()\n",
        ),
        (
            "coroutine_never_awaited",
            "    async def abandoned(): return None\n    abandoned()\n    gc.collect()\n",
        ),
    ],
)
def test_python_lane_fails_on_an_unhandled_exception_from_final_config_cleanup(
    evidence_repository: _EvidenceRepository,
    kind: str,
    cleanup_body: str,
) -> None:
    conftest = evidence_repository.root / "tests" / "conftest.py"
    conftest.write_text(
        conftest.read_text(encoding="utf-8")
        + "\nimport gc\nimport threading\n"
        + "def pytest_configure(config):\n"
        + "  def late_cleanup():\n"
        + cleanup_body
        + "  config.add_cleanup(late_cleanup)\n",
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0, result.stdout + result.stderr
    assert manifest["status"] == "failure"
    assert manifest["unhandled"] >= 1
    assert any(error.startswith(f"evidence_python_unhandled:{kind}:") for error in manifest["errors"])


def test_python_lane_records_an_unhandled_warning_from_a_later_initial_conftest_wrapper_cleanup(
    evidence_repository: _EvidenceRepository,
) -> None:
    (evidence_repository.root / "late_initial_cleanup_plugin.py").write_text(
        "import gc\n"
        "import pytest\n"
        "@pytest.hookimpl(wrapper=True, trylast=True)\n"
        "def pytest_load_initial_conftests(early_config):\n"
        "  def late_cleanup():\n"
        "    async def abandoned(): return None\n"
        "    abandoned()\n"
        "    gc.collect()\n"
        "  early_config.add_cleanup(late_cleanup)\n"
        "  yield\n",
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run("-p", "late_initial_cleanup_plugin", env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0, result.stdout + result.stderr
    assert manifest["status"] == "failure"
    assert any(error.startswith("evidence_python_unhandled:coroutine_never_awaited:") for error in manifest["errors"])


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


def test_python_lane_rejects_an_environment_filter_that_hides_unawaited_coroutines(
    evidence_repository: _EvidenceRepository,
) -> None:
    result, manifest = evidence_repository.run(
        env={"TRACEFOLD_FIXTURE_FAIL": "0", "PYTHONWARNINGS": "ignore::RuntimeWarning"},
    )

    assert result.returncode != 0
    assert "evidence_unhandled_warning_filter_forbidden:ignore::RuntimeWarning" in manifest["errors"]


def test_python_lane_rejects_a_qualified_ini_category_that_hides_unawaited_coroutines(
    evidence_repository: _EvidenceRepository,
) -> None:
    pyproject = evidence_repository.root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8") + "filterwarnings = ['ignore::builtins.RuntimeWarning']\n",
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0
    assert "evidence_unhandled_warning_filter_forbidden:ignore::builtins.RuntimeWarning" in manifest["errors"]


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


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (
            "    future = asyncio.get_running_loop().create_future()\n"
            "    future.set_exception(RuntimeError('future exploded'))\n"
            "    del future\n"
            "    gc.collect()\n"
            "    await asyncio.sleep(0)\n",
            "asyncio_future",
        ),
        (
            "    asyncio.get_running_loop().call_soon(lambda: 1 / 0)\n    await asyncio.sleep(0)\n",
            "asyncio_callback",
        ),
    ],
)
def test_python_lane_fails_on_other_event_loop_exceptions(
    evidence_repository: _EvidenceRepository,
    body: str,
    kind: str,
) -> None:
    (evidence_repository.root / "tests" / "test_asyncio_unhandled.py").write_text(
        "import asyncio\n"
        "import gc\n"
        "async def scenario():\n"
        f"{body}"
        "def test_unretrieved_asyncio_error():\n"
        "    asyncio.run(scenario())\n",
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0
    assert manifest["unhandled"] == 1
    assert any(f"evidence_python_unhandled:{kind}" in error for error in manifest["errors"])


def test_python_lane_keeps_recording_after_a_test_installs_an_asyncio_handler(
    evidence_repository: _EvidenceRepository,
) -> None:
    (evidence_repository.root / "tests" / "test_asyncio_unhandled.py").write_text(
        "import asyncio\n"
        "async def scenario():\n"
        "    seen = []\n"
        "    loop = asyncio.get_running_loop()\n"
        "    loop.set_exception_handler(lambda _loop, context: seen.append(context))\n"
        "    loop.call_soon(lambda: 1 / 0)\n"
        "    await asyncio.sleep(0)\n"
        "    assert len(seen) == 1\n"
        "def test_replaced_handler():\n"
        "    asyncio.run(scenario())\n",
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode != 0
    assert manifest["unhandled"] == 1
    assert any("evidence_python_unhandled:asyncio_callback" in error for error in manifest["errors"])


def test_split_python_lane_records_an_empty_cross_lane_marker_without_failing(
    evidence_repository: _EvidenceRepository,
) -> None:
    pyproject = evidence_repository.root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            "markers = ['live: external provider test', 'scheduled: production-duration diagnostic']",
            "markers = ['live: external provider test', 'scheduled: production-duration diagnostic', "
            "'golden: required evidence lane']",
        ),
        encoding="utf-8",
    )

    result, manifest = evidence_repository.run(env={"TRACEFOLD_FIXTURE_FAIL": "0"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "evidence_required_marker_lane_empty:golden" not in manifest["errors"]
    assert manifest["marker_lanes"]["golden"] == {
        "status": "not_owned",
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
            "markers = ['live: external provider test', 'scheduled: production-duration diagnostic']",
            "markers = ['live: external provider test', 'scheduled: production-duration diagnostic', "
            "'golden: required evidence lane']",
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
            "markers = ['live: external provider test', 'scheduled: production-duration diagnostic']",
            "markers = ['live: external provider test', 'scheduled: production-duration diagnostic', "
            "'golden: required evidence lane']",
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
