from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.profile import PROFILE_REPORT_SCHEMA_VERSION, PROFILE_SCHEMA_VERSION, build_report, main

pytestmark = pytest.mark.contract


def _profile(lane: str, nodeids: list[str]) -> dict[str, object]:
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "lane": lane,
        "selected_nodeids": nodeids,
        "inventory_sha256": _inventory_sha(nodeids),
        "phase_seconds": {"setup_seconds": 1.0, "call_seconds": 2.0, "teardown_seconds": 0.5},
        "modules": [],
        "cases": [{"nodeid": nodeid, "outcome": "passed"} for nodeid in nodeids],
    }


def _baseline() -> dict[str, object]:
    return {
        "issue": 335,
        "commit_sha": "before",
        "ci_run": 1,
        "inventory": {
            "unique_deterministic_nodeids": 3,
            "total_entrypoint_executions": 6,
            "duplicate_executions": 3,
        },
        "lane_inventories": {
            "quality": {"selected": 1, "inventory_sha256": _inventory_sha(["tests/a.py::test_a"])},
            "fast": {
                "selected": 2,
                "inventory_sha256": _inventory_sha(["tests/a.py::test_a", "tests/b.py::test_b"]),
            },
            "deterministic-full": {
                "selected": 3,
                "inventory_sha256": _inventory_sha(["tests/a.py::test_a", "tests/b.py::test_b", "tests/c.py::test_c"]),
            },
        },
        "duration_ratchets": {
            "lane:deterministic-full": {
                "baseline_seconds": 10.0,
                "regression_multiplier": 1.2,
                "required_consecutive_samples": 3,
                "target_seconds": 5.0,
            }
        },
    }


def _inventory_sha(nodeids: list[str]) -> str:
    return hashlib.sha256("\n".join(nodeids).encode()).hexdigest()


def test_profile_report_exposes_entrypoint_duplication_and_full_inventory() -> None:
    report = build_report(
        {
            "quality": _profile("quality", ["tests/a.py::test_a"]),
            "fast": _profile("fast", ["tests/a.py::test_a", "tests/b.py::test_b"]),
            "deterministic-full": _profile(
                "deterministic-full",
                ["tests/a.py::test_a", "tests/b.py::test_b", "tests/c.py::test_c"],
            ),
        },
        baseline=_baseline(),
    )

    assert report["schema_version"] == PROFILE_REPORT_SCHEMA_VERSION
    assert report["inventory"] == {
        "unique_deterministic_nodeids": 3,
        "total_entrypoint_executions": 6,
        "duplicate_executions": 3,
        "duplicate_nodeids": {
            "tests/a.py::test_a": ["deterministic-full", "fast", "quality"],
            "tests/b.py::test_b": ["deterministic-full", "fast"],
        },
        "missing_from_deterministic_full": [],
        "baseline_delta": {
            "unique_deterministic_nodeids": 0,
            "total_entrypoint_executions": 0,
            "duplicate_executions": 0,
        },
        "baseline_lane_changes": {
            "deterministic-full": {"selected_delta": 0, "inventory_changed": False},
            "fast": {"selected_delta": 0, "inventory_changed": False},
            "quality": {"selected_delta": 0, "inventory_changed": False},
        },
    }


def test_profile_report_detects_one_for_one_nodeid_replacement() -> None:
    profiles = {
        "quality": _profile("quality", ["tests/replacement.py::test_replacement"]),
        "fast": _profile("fast", ["tests/a.py::test_a", "tests/b.py::test_b"]),
        "deterministic-full": _profile(
            "deterministic-full",
            ["tests/a.py::test_a", "tests/b.py::test_b", "tests/c.py::test_c"],
        ),
    }

    report = build_report(profiles, baseline=_baseline())

    assert report["inventory"]["baseline_delta"]["unique_deterministic_nodeids"] == 0
    assert report["inventory"]["baseline_lane_changes"]["quality"] == {
        "selected_delta": 0,
        "inventory_changed": True,
    }


def test_duration_ratchet_needs_three_consecutive_significant_regressions() -> None:
    profiles = {"deterministic-full": _profile("deterministic-full", ["tests/a.py::test_a"])}
    profiles["deterministic-full"]["phase_seconds"] = {
        "setup_seconds": 4.0,
        "call_seconds": 9.0,
        "teardown_seconds": 0.0,
    }
    two_prior_regressions = [
        {"duration_observations": {"lane:deterministic-full": 13.0}},
        {"duration_observations": {"lane:deterministic-full": 13.5}},
    ]

    report = build_report(profiles, baseline=_baseline(), history=two_prior_regressions)
    assert report["duration_ratchets"]["lane:deterministic-full"]["status"] == "regression"

    interrupted = [two_prior_regressions[0], {"duration_observations": {"lane:deterministic-full": 11.0}}]
    report = build_report(profiles, baseline=_baseline(), history=interrupted)
    assert report["duration_ratchets"]["lane:deterministic-full"]["status"] == "within_ratchet"


def test_ratchet_command_blocks_only_after_two_historical_and_one_current_regression(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profiles"
    history_dir = tmp_path / "history"
    profile_dir.mkdir()
    history_dir.mkdir()
    profiles = {
        "quality": _profile("quality", ["tests/a.py::test_a"]),
        "fast": _profile("fast", ["tests/a.py::test_a", "tests/b.py::test_b"]),
        "deterministic-full": _profile(
            "deterministic-full",
            ["tests/a.py::test_a", "tests/b.py::test_b", "tests/c.py::test_c"],
        ),
    }
    profiles["deterministic-full"]["phase_seconds"] = {
        "setup_seconds": 4.0,
        "call_seconds": 9.0,
        "teardown_seconds": 0.0,
    }
    for lane, profile_data in profiles.items():
        (profile_dir / f"{lane}.json").write_text(json.dumps(profile_data), encoding="utf-8")
    for index, duration in enumerate((13.0, 13.5), start=1):
        (history_dir / f"report-{index}.json").write_text(
            json.dumps({"duration_observations": {"lane:deterministic-full": duration}}),
            encoding="utf-8",
        )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(_baseline()), encoding="utf-8")

    exit_code = main(
        (
            "aggregate",
            "--profile-dir",
            str(profile_dir),
            "--history-dir",
            str(history_dir),
            "--baseline",
            str(baseline),
            "--output",
            str(tmp_path / "report.json"),
            "--enforce-ratchets",
        )
    )

    assert exit_code == 1


def test_pytest_plugin_records_selected_nodeids_and_all_three_phases(tmp_path: Path) -> None:
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(
        "import pytest\n@pytest.fixture()\ndef value():\n    yield 3\ndef test_value(value):\n    assert value == 3\n",
        encoding="utf-8",
    )
    profile_path = tmp_path / "profile.json"
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            str(test_file),
            "-p",
            "tests.support.profile",
            f"--test-profile={profile_path}",
            "--test-profile-lane=probe",
        ),
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert profile["schema_version"] == PROFILE_SCHEMA_VERSION
    assert profile["lane"] == "probe"
    assert profile["selected"] == 1
    assert profile["selected_nodeids"] == [f"{test_file.name}::test_value"]
    assert set(profile["phase_seconds"]) == {"setup_seconds", "call_seconds", "teardown_seconds"}
    assert profile["cases"][0]["outcome"] == "passed"


def test_pytest_plugin_does_not_overwrite_a_call_failure_with_passing_teardown(tmp_path: Path) -> None:
    test_file = tmp_path / "test_failure.py"
    test_file.write_text("def test_failure():\n    assert False\n", encoding="utf-8")
    profile_path = tmp_path / "profile.json"

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            str(test_file),
            "-p",
            "tests.support.profile",
            f"--test-profile={profile_path}",
            "--test-profile-lane=probe",
        ),
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert profile["cases"][0]["outcome"] == "failed"
