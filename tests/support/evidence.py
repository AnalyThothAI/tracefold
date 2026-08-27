"""Fail-closed pytest evidence mode for merge and release verification."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

SCHEMA_VERSION = "tracefold_test_evidence_v1"
_ALLOWED_DESELECTED_MARKERS = ("live",)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RECORDER_KEY: pytest.StashKey[Any] = pytest.StashKey()


@dataclass
class _RecorderSlot:
    current: _EvidenceRecorder | None = None


_ACTIVE_RECORDER = _RecorderSlot()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--evidence-manifest",
        action="store",
        default=None,
        help="write the tracefold_test_evidence_v1 JSON manifest",
    )


def pytest_configure(config: pytest.Config) -> None:
    if not _enabled():
        return
    recorder = _EvidenceRecorder(root=_REPO_ROOT)
    config.stash[_RECORDER_KEY] = recorder
    _ACTIVE_RECORDER.current = recorder
    if str(config.option.markexpr).replace(" ", "") != "notlive":
        recorder.errors.append("evidence_marker_expression_must_be_not_live")
    if str(config.option.keyword or "").strip():
        recorder.errors.append("evidence_keyword_deselection_forbidden")
    if int(config.option.maxfail or 0) != 0:
        recorder.errors.append("evidence_maxfail_forbidden")
    if config.pluginmanager.hasplugin("rerunfailures"):
        recorder.errors.append("evidence_rerun_plugin_forbidden")


def pytest_deselected(items: list[pytest.Item]) -> None:
    config = items[0].config if items else None
    recorder = _recorder(config)
    if recorder is None:
        return
    for item in items:
        if item.get_closest_marker("live") is None:
            recorder.errors.append(f"evidence_unexpected_deselection:{item.nodeid}")


def pytest_collection_finish(session: pytest.Session) -> None:
    recorder = _recorder(session.config)
    if recorder is None:
        return
    recorder.selected = len(session.items)
    for item in session.items:
        if item.get_closest_marker("live") is not None:
            recorder.errors.append(f"evidence_live_test_selected:{item.nodeid}")


def pytest_collectreport(report: pytest.CollectReport) -> None:
    recorder = _ACTIVE_RECORDER.current
    if recorder is not None and report.skipped:
        recorder.skipped.add(report.nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    recorder = _ACTIVE_RECORDER.current
    if recorder is None:
        return
    nodeid = report.nodeid
    was_xfail = bool(getattr(report, "wasxfail", False))
    if report.outcome == "rerun":
        recorder.rerun.add(nodeid)
    elif report.skipped:
        (recorder.xfailed if was_xfail else recorder.skipped).add(nodeid)
    elif report.failed:
        recorder.failed.add(nodeid)
    elif report.when == "call" and report.passed:
        (recorder.xpassed if was_xfail else recorder.passed).add(nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    recorder = _recorder(session.config)
    if recorder is None:
        return
    recorder.session_failures = int(session.testsfailed)
    if recorder.selected != len(recorder.observed):
        recorder.errors.append("evidence_selected_outcome_count_mismatch")
    manifest_path = session.config.getoption("--evidence-manifest")
    if not manifest_path:
        recorder.errors.append("evidence_manifest_path_required")
        manifest_path = "artifacts/test-evidence/manifest.json"
    recorder.write(Path(str(manifest_path)))
    if recorder.not_green or exitstatus != pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_unconfigure(config: pytest.Config) -> None:
    _ACTIVE_RECORDER.current = None


def _enabled() -> bool:
    return os.environ.get("TRACEFOLD_TEST_EVIDENCE") == "1"


def _recorder(config: pytest.Config | None) -> _EvidenceRecorder | None:
    if config is None or not _enabled():
        return None
    return config.stash.get(_RECORDER_KEY, None)


@dataclass
class _EvidenceRecorder:
    root: Path
    selected: int = 0
    passed: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    skipped: set[str] = field(default_factory=set)
    xfailed: set[str] = field(default_factory=set)
    xpassed: set[str] = field(default_factory=set)
    rerun: set[str] = field(default_factory=set)
    session_failures: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def observed(self) -> set[str]:
        return self.passed | self.failed | self.skipped | self.xfailed | self.xpassed | self.rerun

    @property
    def not_green(self) -> bool:
        return bool(
            self.failed
            or self.skipped
            or self.xfailed
            or self.xpassed
            or self.rerun
            or self.session_failures
            or self.errors
        )

    def write(self, path: Path) -> None:
        commit_sha = _capture(("git", "rev-parse", "HEAD"), cwd=self.root)
        github_sha = os.environ.get("GITHUB_SHA")
        if github_sha and github_sha != commit_sha:
            self.errors.append("evidence_github_sha_mismatch")
        node_version = _capture(("node", "--version"), cwd=self.root, required=False)
        if node_version == "unavailable":
            self.errors.append("evidence_node_unavailable")
        failed_count = max(len(self.failed), self.session_failures, int(bool(self.errors)))
        passed_count = len(self.passed - self.failed - self.skipped - self.xfailed - self.xpassed - self.rerun)
        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "commit_sha": commit_sha,
            "python_version": platform.python_version(),
            "node_version": node_version,
            "uv_lock_sha256": _sha256(self.root / "uv.lock"),
            "package_lock_sha256": _sha256(self.root / "web" / "package-lock.json"),
            "migration_head": _migration_head(),
            "selected": self.selected,
            "passed": passed_count,
            "failed": failed_count,
            "skipped": len(self.skipped),
            "xfailed": len(self.xfailed),
            "xpassed": len(self.xpassed),
            "rerun": len(self.rerun),
            "explicitly_deselected_markers": list(_ALLOWED_DESELECTED_MARKERS),
            "errors": sorted(set(self.errors)),
        }
        destination = path if path.is_absolute() else self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)


def _capture(command: tuple[str, ...], *, cwd: Path, required: bool = True) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, check=required, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        if required:
            raise
        return "unavailable"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unavailable"


def tested_head_changes(root: Path) -> tuple[str, ...]:
    """Return every tracked or untracked path that is not represented by HEAD."""

    result = subprocess.run(
        ("git", "status", "--porcelain=v1", "--untracked-files=normal"),
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def main(argv: Sequence[str] | None = None) -> int:
    """Fail unless the repository exactly matches the tested HEAD."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments != ("--assert-clean",):
        sys.stderr.write("usage: python -m tests.support.evidence --assert-clean\n")
        return 2
    changes = tested_head_changes(_REPO_ROOT)
    if not changes:
        return 0
    sys.stderr.write("evidence_tested_head_dirty:\n" + "\n".join(changes) + "\n")
    return 1


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _migration_head() -> str:
    from tracefold.platform.postgres.migrations import latest_migration_version

    return latest_migration_version()


__all__ = ["SCHEMA_VERSION", "main", "tested_head_changes"]


if __name__ == "__main__":
    raise SystemExit(main())
