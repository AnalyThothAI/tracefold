from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

PROFILE_SCHEMA_VERSION = "tracefold_test_profile_v1"
PROFILE_REPORT_SCHEMA_VERSION = "tracefold_test_profile_report_v1"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROFILE_PATH_ENV = "TRACEFOLD_TEST_PROFILE_PATH"
_PROFILE_LANE_ENV = "TRACEFOLD_TEST_PROFILE_LANE"
_ENTRYPOINTS: dict[str, tuple[str, ...]] = {
    "quality": (
        "tests/architecture",
        "tests/contract",
        "-m",
        "(architecture or contract) and not generated and not external_codegen and not slow and not scheduled",
    ),
    "fast": (
        "tests",
        "-m",
        "not integration and not deploy and not e2e and not golden and not live and not slow and not scheduled "
        "and not external_codegen",
    ),
    "deterministic-full": ("tests", "-m", "not live and not scheduled"),
}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("tracefold test profile")
    group.addoption("--test-profile", help="write a tracefold_test_profile_v1 JSON document")
    group.addoption("--test-profile-lane", help="stable name of the profiled test lane")


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("--test-profile") or os.environ.get(_PROFILE_PATH_ENV)
    if not path:
        return
    lane = config.getoption("--test-profile-lane") or os.environ.get(_PROFILE_LANE_ENV)
    if not lane:
        raise pytest.UsageError("test profile output requires --test-profile-lane or TRACEFOLD_TEST_PROFILE_LANE")
    recorder = _ProfileRecorder(root=Path(str(config.rootpath)), lane=str(lane), started=time.monotonic())
    config.stash[_RECORDER_KEY] = recorder


def pytest_collection_finish(session: pytest.Session) -> None:
    recorder = _recorder(session.config)
    if recorder is not None:
        recorder.selected_nodeids = sorted(item.nodeid for item in session.items)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    outcome = yield
    report = outcome.get_result()
    recorder = _recorder(item.config)
    if recorder is None or report.when not in {"setup", "call", "teardown"}:
        return
    recorder.phases[report.nodeid][report.when] += float(report.duration)
    recorder.outcomes[report.nodeid] = str(report.outcome)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    recorder = _recorder(session.config)
    if recorder is None:
        return
    path = session.config.getoption("--test-profile") or os.environ.get(_PROFILE_PATH_ENV)
    if path:
        recorder.write(Path(str(path)), exitstatus=exitstatus)


@dataclass
class _ProfileRecorder:
    root: Path
    lane: str
    started: float
    selected_nodeids: list[str] = field(default_factory=list)
    phases: dict[str, dict[str, float]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(float)))
    outcomes: dict[str, str] = field(default_factory=dict)

    def write(self, path: Path, *, exitstatus: int) -> None:
        cases = []
        modules: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"selected": 0, "setup_seconds": 0.0, "call_seconds": 0.0, "teardown_seconds": 0.0}
        )
        for nodeid in self.selected_nodeids:
            phase = self.phases.get(nodeid, {})
            setup = float(phase.get("setup", 0.0))
            call = float(phase.get("call", 0.0))
            teardown = float(phase.get("teardown", 0.0))
            module = nodeid.split("::", 1)[0]
            cases.append(
                {
                    "nodeid": nodeid,
                    "module": module,
                    "outcome": self.outcomes.get(nodeid, "not_run"),
                    "setup_seconds": setup,
                    "call_seconds": call,
                    "teardown_seconds": teardown,
                    "total_seconds": setup + call + teardown,
                }
            )
            module_row = modules[module]
            module_row["selected"] = int(module_row["selected"]) + 1
            for name, value in (("setup_seconds", setup), ("call_seconds", call), ("teardown_seconds", teardown)):
                module_row[name] = float(module_row[name]) + value

        module_rows = []
        for module, values in modules.items():
            row = {"module": module, **values}
            row["total_seconds"] = sum(float(values[name]) for name in _PHASE_FIELDS)
            module_rows.append(row)

        phase_seconds = {name: sum(float(case[name]) for case in cases) for name in _PHASE_FIELDS}
        payload = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "lane": self.lane,
            "commit_sha": _git("rev-parse", "HEAD", cwd=self.root),
            "git_tree_sha": _git("rev-parse", "HEAD^{tree}", cwd=self.root),
            "python_version": platform.python_version(),
            "pytest_version": pytest.__version__,
            "exitstatus": int(exitstatus),
            "selected": len(self.selected_nodeids),
            "selected_nodeids": self.selected_nodeids,
            "inventory_sha256": _digest_lines(self.selected_nodeids),
            "wall_seconds": time.monotonic() - self.started,
            "phase_seconds": phase_seconds,
            "cases": sorted(cases, key=lambda row: (-float(row["total_seconds"]), str(row["nodeid"]))),
            "modules": sorted(
                module_rows,
                key=lambda row: (-float(row["total_seconds"]), str(row["module"])),
            ),
        }
        _write_json(path, payload, root=self.root)


_PHASE_FIELDS = ("setup_seconds", "call_seconds", "teardown_seconds")
_RECORDER_KEY = pytest.StashKey[_ProfileRecorder]()


def _recorder(config: pytest.Config) -> _ProfileRecorder | None:
    return config.stash.get(_RECORDER_KEY, None)


def build_report(
    profiles: dict[str, dict[str, Any]],
    *,
    baseline: dict[str, Any],
    history: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    inventories = {lane: set(_strings(profile.get("selected_nodeids"))) for lane, profile in profiles.items()}
    full = inventories.get("deterministic-full", set())
    all_selected = set().union(*inventories.values()) if inventories else set()
    owners = {
        nodeid: sorted(lane for lane, inventory in inventories.items() if nodeid in inventory)
        for nodeid in sorted(all_selected)
    }
    duplicates = {nodeid: lanes for nodeid, lanes in owners.items() if len(lanes) > 1}
    total_executions = sum(len(inventory) for inventory in inventories.values())
    baseline_inventory = baseline.get("inventory", {})
    duration_observations = _duration_observations(profiles)
    ratchets = _evaluate_ratchets(
        baseline.get("duration_ratchets", {}),
        [*(_duration_history(report) for report in history), duration_observations],
    )
    return {
        "schema_version": PROFILE_REPORT_SCHEMA_VERSION,
        "baseline": {
            "issue": baseline.get("issue"),
            "commit_sha": baseline.get("commit_sha"),
            "ci_run": baseline.get("ci_run"),
        },
        "profiles": {
            lane: {
                "selected": len(inventories[lane]),
                "inventory_sha256": profile.get("inventory_sha256"),
                "wall_seconds": profile.get("wall_seconds"),
                "phase_seconds": profile.get("phase_seconds"),
                "top_modules": list(profile.get("modules", []))[:20],
                "top_cases": list(profile.get("cases", []))[:20],
            }
            for lane, profile in sorted(profiles.items())
        },
        "inventory": {
            "unique_deterministic_nodeids": len(full),
            "total_entrypoint_executions": total_executions,
            "duplicate_executions": total_executions - len(all_selected),
            "duplicate_nodeids": duplicates,
            "missing_from_deterministic_full": sorted(all_selected - full),
            "baseline_delta": {
                "unique_deterministic_nodeids": len(full)
                - int(baseline_inventory.get("unique_deterministic_nodeids", 0)),
                "total_entrypoint_executions": total_executions
                - int(baseline_inventory.get("total_entrypoint_executions", 0)),
                "duplicate_executions": total_executions
                - len(all_selected)
                - int(baseline_inventory.get("duplicate_executions", 0)),
            },
        },
        "duration_observations": duration_observations,
        "duration_ratchets": ratchets,
    }


def _duration_observations(profiles: dict[str, dict[str, Any]]) -> dict[str, float]:
    observations: dict[str, float] = {}
    for lane, profile in profiles.items():
        if not any(case.get("outcome") != "not_run" for case in profile.get("cases", [])):
            continue
        phase_seconds = profile.get("phase_seconds", {})
        observations[f"lane:{lane}"] = sum(float(phase_seconds.get(name, 0.0)) for name in _PHASE_FIELDS)
        for module in profile.get("modules", []):
            observations[f"module:{module['module']}"] = float(module.get("total_seconds", 0.0))
    return observations


def _duration_history(report: dict[str, Any]) -> dict[str, float]:
    values = report.get("duration_observations", {})
    return {str(key): float(value) for key, value in values.items()}


def _evaluate_ratchets(
    budgets: dict[str, Any],
    observations: Sequence[dict[str, float]],
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for key, raw_budget in sorted(budgets.items()):
        baseline_seconds = float(raw_budget["baseline_seconds"])
        multiplier = float(raw_budget.get("regression_multiplier", 1.25))
        required_samples = int(raw_budget.get("required_consecutive_samples", 3))
        threshold = baseline_seconds * multiplier
        samples = [float(observation[key]) for observation in observations if key in observation]
        recent = samples[-required_samples:]
        repeated_regression = len(recent) == required_samples and all(value > threshold for value in recent)
        results[key] = {
            "baseline_seconds": baseline_seconds,
            "target_seconds": raw_budget.get("target_seconds"),
            "regression_threshold_seconds": threshold,
            "required_consecutive_samples": required_samples,
            "observed_samples": samples,
            "status": "regression"
            if repeated_regression
            else ("insufficient_samples" if len(recent) < required_samples else "within_ratchet"),
        }
    return results


def _collect_profiles(output_dir: Path) -> dict[str, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    profiles: dict[str, dict[str, Any]] = {}
    for lane, selection in _ENTRYPOINTS.items():
        path = output_dir / f"{lane}.json"
        command = (
            sys.executable,
            "-m",
            "pytest",
            *selection,
            "--collect-only",
            "-q",
            "-p",
            "tests.support.profile",
            f"--test-profile={path}",
            f"--test-profile-lane={lane}",
        )
        completed = subprocess.run(command, cwd=_REPO_ROOT, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(
                f"test_profile_collection_failed:{lane}:{completed.returncode}\n{completed.stdout}\n{completed.stderr}"
            )
        profiles[lane] = _read_json(path)
    return profiles


def _aggregate_command(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.support.profile aggregate")
    parser.add_argument("--profile-dir", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--history", action="append", default=[], type=Path)
    parser.add_argument("--enforce-ratchets", action="store_true")
    options = parser.parse_args(arguments)
    profiles = {lane: _read_json(options.profile_dir / f"{lane}.json") for lane in _ENTRYPOINTS}
    report = build_report(
        profiles,
        baseline=_read_json(options.baseline),
        history=[_read_json(path) for path in options.history],
    )
    _write_json(options.output, report, root=_REPO_ROOT)
    inventory = report["inventory"]
    print(
        json.dumps(
            {
                "output": str(options.output),
                "selected": {lane: value["selected"] for lane, value in report["profiles"].items()},
                "unique_deterministic_nodeids": inventory["unique_deterministic_nodeids"],
                "total_entrypoint_executions": inventory["total_entrypoint_executions"],
                "duplicate_executions": inventory["duplicate_executions"],
                "missing_from_deterministic_full": len(inventory["missing_from_deterministic_full"]),
                "baseline_delta": inventory["baseline_delta"],
                "duration_ratchets": {key: value["status"] for key, value in report["duration_ratchets"].items()},
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    if options.enforce_ratchets and any(
        value["status"] == "regression" for value in report["duration_ratchets"].values()
    ):
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if not arguments or arguments[0] not in {"collect", "aggregate"}:
        raise SystemExit("usage: python -m tests.support.profile {collect|aggregate} ...")
    command = arguments.pop(0)
    if command == "collect":
        parser = argparse.ArgumentParser(prog="python -m tests.support.profile collect")
        parser.add_argument("--output-dir", required=True, type=Path)
        options = parser.parse_args(arguments)
        _collect_profiles(options.output_dir)
        return 0
    return _aggregate_command(arguments)


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _digest_lines(values: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(values).encode()).hexdigest()


def _git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"profile_json_object_required:{path}")
    return value


def _write_json(path: Path, payload: dict[str, Any], *, root: Path) -> None:
    destination = path if path.is_absolute() else root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["PROFILE_REPORT_SCHEMA_VERSION", "PROFILE_SCHEMA_VERSION", "build_report", "main"]
