"""Fail-closed pytest evidence mode for merge and release verification."""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import threading
import tomllib
import warnings
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from _pytest.config import parse_warning_filter
from hypothesis import __version__ as hypothesis_version
from hypothesis import settings

LANE_SCHEMA_VERSION = "tracefold_test_lane_v2"
AGGREGATE_SCHEMA_VERSION = "tracefold_test_evidence_v2"
SCHEMA_VERSION = LANE_SCHEMA_VERSION
_ALLOWED_DESELECTED_MARKERS = ("live", "scheduled")
_RESOURCE_MARKERS = ("deploy", "e2e", "external_codegen", "golden", "integration", "slow")
_REQUIRED_MARKER_LANES = (
    "architecture",
    "contract",
    "deploy",
    "e2e",
    "external_codegen",
    "generated",
    "golden",
    "integration",
    "property",
    "slow",
)
_NON_GREEN_OUTCOME_FIELDS = ("failed", "skipped", "xfailed", "xpassed", "rerun", "unhandled")
_ALLOWED_MODULE_PLUGINS = {
    "_hypothesis_pytestplugin": ("hypothesis", hypothesis_version),
    "tests.support.evidence": ("tracefold-evidence", LANE_SCHEMA_VERSION),
    "tests.support.profile": ("tracefold-test-profile", "tracefold_test_profile_v1"),
}
_REQUIRED_PYTEST_CORE_MODULES = (
    "_pytest.threadexception",
    "_pytest.unraisableexception",
    "_pytest.warnings",
)
_FORBIDDEN_COLLECTION_OPTIONS = (
    "--collect-only",
    "--confcutdir",
    "--continue-on-collection-errors",
    "--deselect",
    "--failed-first",
    "--ff",
    "--ignore",
    "--ignore-glob",
    "--keep-duplicates",
    "--keepduplicates",
    "--last-failed",
    "--lf",
    "--new-first",
    "--nf",
    "--noconftest",
    "--pyargs",
    "--stepwise",
    "--stepwise-skip",
    "--sw",
)
_REQUIRED_VITEST_ROOTS = {
    "frontend-architecture": ("web/tests/architecture",),
    "frontend-unit": ("web/tests/unit", "web/tests/component", "web/tests/routes"),
}
_REQUIRED_PLAYWRIGHT_ROOTS = {"browser": ("web/tests/e2e/full-stack",)}
_VITEST_TEST_FILE_SUFFIXES = tuple(
    f".{kind}.{extension}"
    for kind in ("test", "spec")
    for extension in ("js", "jsx", "ts", "tsx", "cjs", "cts", "mjs", "mts")
)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RECORDER_KEY: pytest.StashKey[Any] = pytest.StashKey()


@dataclass
class _RecorderSlot:
    current: _EvidenceRecorder | None = None


_ACTIVE_RECORDER = _RecorderSlot()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(
    early_config: pytest.Config, parser: pytest.Parser, args: list[str]
) -> Generator[None]:
    del parser, args
    if _enabled():
        recorder = _EvidenceRecorder(root=_REPO_ROOT)
        early_config.stash[_RECORDER_KEY] = recorder
        _ACTIVE_RECORDER.current = recorder
        _install_process_unhandled_hooks(recorder)
        # This outermost wrapper registers before pytest's own capture wrapper. Config cleanups use LIFO order,
        # so evidence is finalized after core background-exception and repository/conftest cleanup callbacks.
        early_config.add_cleanup(lambda: _finalize_evidence(early_config, recorder))
    yield


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--evidence-manifest",
        action="store",
        default=None,
        help="write the tracefold_test_lane_v2 JSON manifest",
    )
    parser.addoption(
        "--resource-evidence-manifest",
        action="store",
        default=None,
        help="write the independently aggregated resource lane manifest",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    if not _enabled():
        return
    recorder = config.stash.get(_RECORDER_KEY, None)
    if recorder is None:  # Defensive fallback for a non-canonical, late plugin registration.
        recorder = _EvidenceRecorder(root=_REPO_ROOT)
        config.stash[_RECORDER_KEY] = recorder
        _ACTIVE_RECORDER.current = recorder
        _install_process_unhandled_hooks(recorder)
        config.add_cleanup(lambda: _finalize_evidence(config, recorder))
    recorder.original_event_loop_policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(_EvidenceEventLoopPolicy(recorder.original_event_loop_policy, recorder))
    if os.environ.get("PYTEST_ADDOPTS", "").strip():
        recorder.errors.append("evidence_pytest_addopts_forbidden")
    if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") != "1":
        recorder.errors.append("evidence_pytest_plugin_autoload_must_be_disabled")
    recorder.pytest_plugins, forbidden_plugins, missing_core_plugins = _observed_pytest_plugins(config)
    recorder.errors.extend(f"evidence_pytest_plugin_forbidden:{name}" for name in forbidden_plugins)
    recorder.errors.extend(f"evidence_pytest_core_plugin_missing:{name}" for name in missing_core_plugins)
    if not any(plugin["name"] == "hypothesis" for plugin in recorder.pytest_plugins):
        recorder.errors.append("evidence_hypothesis_plugin_must_be_explicit")
    recorder.hypothesis = _hypothesis_metadata()
    if recorder.hypothesis != {
        "profile": "ci",
        "derandomize": True,
        "database": None,
        "print_blob": True,
        "replay_policy": "derandomize",
    }:
        recorder.errors.append("evidence_hypothesis_profile_not_replayable")
    declared_markers = _declared_markers(config)
    if _is_tracefold_project(_REPO_ROOT):
        for marker in _REQUIRED_MARKER_LANES:
            if marker not in declared_markers:
                recorder.errors.append(f"evidence_required_marker_not_declared:{marker}")
        recorder.required_markers = list(_REQUIRED_MARKER_LANES)
    else:
        recorder.required_markers = sorted(set(_REQUIRED_MARKER_LANES) & declared_markers)
    if str(config.option.markexpr).replace(" ", "") != "notliveandnotscheduled":
        recorder.errors.append("evidence_marker_expression_must_exclude_live_and_scheduled")
    if str(config.option.keyword or "").strip():
        recorder.errors.append("evidence_keyword_deselection_forbidden")
    if int(config.option.maxfail or 0) != 0:
        recorder.errors.append("evidence_maxfail_forbidden")
    if bool(config.option.runxfail):
        recorder.errors.append("evidence_runxfail_forbidden")
    if config.pluginmanager.hasplugin("rerunfailures"):
        recorder.errors.append("evidence_rerun_plugin_forbidden")
    expected_test_root = (_REPO_ROOT / "tests").resolve()
    selected_roots: list[Path] = []
    for argument in config.args:
        raw_path = str(argument).split("::", 1)[0]
        selected_path = Path(raw_path)
        if not selected_path.is_absolute():
            selected_path = _REPO_ROOT / selected_path
        selected_roots.append(selected_path.resolve())
    if selected_roots != [expected_test_root]:
        recorder.errors.append("evidence_test_root_must_be_complete")
    for option in _FORBIDDEN_COLLECTION_OPTIONS:
        if any(argument == option or argument.startswith(f"{option}=") for argument in config.invocation_params.args):
            recorder.errors.append(f"evidence_collection_option_forbidden:{option}")
    if any(
        argument in {"-o", "--override-ini"} or argument.startswith("-o=") or argument.startswith("--override-ini=")
        for argument in config.invocation_params.args
    ):
        recorder.errors.append("evidence_override_ini_forbidden")
    warning_filters = [
        *((str(value), False) for value in config.getini("filterwarnings")),
        *((str(value), True) for value in (config.getoption("pythonwarnings") or [])),
        *((value.strip(), True) for value in os.environ.get("PYTHONWARNINGS", "").split(",") if value.strip()),
    ]
    for warning_filter, escape in warning_filters:
        if _warning_filter_can_hide_unhandled(warning_filter, escape=escape):
            recorder.errors.append(f"evidence_unhandled_warning_filter_forbidden:{warning_filter}")


def pytest_sessionstart(session: pytest.Session) -> None:
    recorder = _recorder(session.config)
    if recorder is None:
        return
    recorder.session = session


def _install_process_unhandled_hooks(recorder: _EvidenceRecorder) -> None:
    recorder.previous_thread_excepthook = threading.excepthook
    recorder.previous_unraisablehook = sys.unraisablehook
    recorder.previous_showwarnmsg = getattr(warnings, "_showwarnmsg", None)

    def record_thread(args: Any) -> None:
        _record_python_unhandled(recorder, "thread_exception")
        recorder.previous_thread_excepthook(args)

    def record_unraisable(args: Any) -> None:
        _record_python_unhandled(recorder, "unraisable_exception")
        recorder.previous_unraisablehook(args)

    def record_warning(message: Any) -> None:
        kind = _unhandled_kind(message.category, str(message.message))
        if kind is not None:
            _record_python_unhandled(recorder, kind)
        recorder.previous_showwarnmsg(message)

    recorder.thread_excepthook = record_thread
    recorder.unraisablehook = record_unraisable
    recorder.showwarnmsg = record_warning
    threading.excepthook = record_thread
    sys.unraisablehook = record_unraisable
    if recorder.previous_showwarnmsg is None:
        recorder.errors.append("evidence_warning_capture_unavailable")
    else:
        warnings._showwarnmsg = record_warning  # type: ignore[attr-defined]


def pytest_deselected(items: list[pytest.Item]) -> None:
    config = items[0].config if items else None
    recorder = _recorder(config)
    if recorder is None:
        return
    for item in items:
        if not any(item.get_closest_marker(marker) is not None for marker in _ALLOWED_DESELECTED_MARKERS):
            recorder.errors.append(f"evidence_unexpected_deselection:{item.nodeid}")
            continue
        path = item.path.resolve()
        if path.is_relative_to(recorder.root.resolve()):
            recorder.allowed_deselected_modules.add(path.relative_to(recorder.root.resolve()).as_posix())


def pytest_collection_finish(session: pytest.Session) -> None:
    recorder = _recorder(session.config)
    if recorder is None:
        return
    recorder.selected = len(session.items)
    collected_modules = {
        item.path.resolve().relative_to(recorder.root.resolve()).as_posix()
        for item in session.items
        if item.path.resolve().is_relative_to(recorder.root.resolve())
    } | recorder.allowed_deselected_modules
    for module in sorted(_tracked_test_modules(recorder.root) - collected_modules):
        recorder.errors.append(f"evidence_tracked_test_module_not_collected:{module}")
    for marker in recorder.required_markers:
        recorder.marker_items[marker] = {
            item.nodeid for item in session.items if item.get_closest_marker(marker) is not None
        }
        if not recorder.marker_items[marker]:
            recorder.errors.append(f"evidence_required_marker_lane_empty:{marker}")
    for item in session.items:
        for marker in _ALLOWED_DESELECTED_MARKERS:
            if item.get_closest_marker(marker) is not None:
                recorder.errors.append(f"evidence_{marker}_test_selected:{item.nodeid}")
        for marker in item.iter_markers("filterwarnings"):
            for warning_filter in marker.args:
                if _warning_filter_can_hide_unhandled(str(warning_filter), escape=False):
                    recorder.errors.append(
                        f"evidence_unhandled_warning_filter_forbidden:{item.nodeid}:{warning_filter}"
                    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    recorder = _recorder(item.config)
    if recorder is not None:
        recorder.current_nodeid = item.nodeid


def pytest_runtest_teardown(item: pytest.Item) -> None:
    recorder = _recorder(item.config)
    if recorder is not None and recorder.current_nodeid == item.nodeid:
        recorder.current_nodeid = ""


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


def pytest_warning_recorded(warning_message: Any, when: str, nodeid: str, location: Any) -> None:
    del location
    recorder = _ACTIVE_RECORDER.current
    if recorder is None:
        return
    kind = _unhandled_warning_kind(warning_message)
    if kind is None:
        return
    if recorder.showwarnmsg is not None:
        # The process-level hook sees warnings before pytest records them and remains installed through Config cleanup.
        return
    owner = nodeid or f"<{when}>"
    recorder.unhandled += 1
    if nodeid:
        recorder.unhandled_items.add(nodeid)
    recorder.errors.append(f"evidence_python_unhandled:{kind}:{owner}")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    recorder = _recorder(session.config)
    if recorder is None:
        return
    recorder.session = session
    recorder.initial_exitstatus = int(exitstatus)


def pytest_unconfigure(config: pytest.Config) -> None:
    del config


def _finalize_evidence(config: pytest.Config, recorder: _EvidenceRecorder) -> None:
    """Write evidence after core background-exception and repository/conftest cleanup."""

    try:
        session = recorder.session
        if session is None:
            return
        for _ in range(5):
            gc.collect()
        recorder.session_failures = int(session.testsfailed)
        final_exitstatus = int(session.exitstatus)
        observed_exitstatus = (
            final_exitstatus if final_exitstatus != int(pytest.ExitCode.OK) else recorder.initial_exitstatus
        )
        if observed_exitstatus != int(pytest.ExitCode.OK):
            recorder.errors.append(f"evidence_pytest_exitstatus_nonzero:{observed_exitstatus}")
        if recorder.selected != len(recorder.observed):
            recorder.errors.append("evidence_selected_outcome_count_mismatch")
        manifest_path = config.getoption("--evidence-manifest")
        if not manifest_path:
            recorder.errors.append("evidence_manifest_path_required")
            manifest_path = "artifacts/test-evidence/manifest.json"
        resource_manifest = config.getoption("--resource-evidence-manifest")
        resource_path = Path(str(resource_manifest)) if resource_manifest else None
        if _is_tracefold_project(recorder.root) and resource_path is None:
            recorder.errors.append("evidence_resource_manifest_path_required")
        recorder.write(Path(str(manifest_path)), resource_path=resource_path)
        if recorder.not_green and int(session.exitstatus) == int(pytest.ExitCode.OK):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
    finally:
        if threading.excepthook is recorder.thread_excepthook:
            threading.excepthook = recorder.previous_thread_excepthook
        if sys.unraisablehook is recorder.unraisablehook:
            sys.unraisablehook = recorder.previous_unraisablehook
        if getattr(warnings, "_showwarnmsg", None) is recorder.showwarnmsg:
            warnings._showwarnmsg = recorder.previous_showwarnmsg  # type: ignore[attr-defined]
        if recorder.original_event_loop_policy is not None:
            asyncio.set_event_loop_policy(recorder.original_event_loop_policy)
        _ACTIVE_RECORDER.current = None


def _record_python_unhandled(
    recorder: _EvidenceRecorder,
    kind: str,
) -> None:
    owner = recorder.current_nodeid or f"<{kind}>"
    recorder.unhandled += 1
    if recorder.current_nodeid:
        recorder.unhandled_items.add(recorder.current_nodeid)
    recorder.errors.append(f"evidence_python_unhandled:{kind}:{owner}")


def _enabled() -> bool:
    return os.environ.get("TRACEFOLD_TEST_EVIDENCE") == "1"


def _unhandled_warning_kind(warning_message: Any) -> str | None:
    category = getattr(warning_message, "category", Warning)
    return _unhandled_kind(category, str(getattr(warning_message, "message", "")))


def _unhandled_kind(category: type[Warning], message: str) -> str | None:
    if issubclass(category, pytest.PytestUnhandledThreadExceptionWarning):
        return "thread_exception"
    if issubclass(category, pytest.PytestUnraisableExceptionWarning):
        return "unraisable_exception"
    if issubclass(category, RuntimeWarning) and "coroutine" in message and "was never awaited" in message:
        return "coroutine_never_awaited"
    return None


def _warning_filter_can_hide_unhandled(value: str, *, escape: bool) -> bool:
    try:
        action, _, category, _, _ = parse_warning_filter(value, escape=escape)
    except (ImportError, pytest.UsageError):
        # An evidence run must not rely on a warning policy pytest cannot resolve consistently.
        return True
    if action != "ignore":
        return False
    protected_categories = (
        RuntimeWarning,
        pytest.PytestUnhandledThreadExceptionWarning,
        pytest.PytestUnraisableExceptionWarning,
    )
    return any(issubclass(protected, category) for protected in protected_categories)


class _EvidenceEventLoopPolicy(asyncio.AbstractEventLoopPolicy):
    def __init__(self, delegate: Any, recorder: _EvidenceRecorder) -> None:
        self._delegate = delegate
        self._recorder = recorder

    def get_event_loop(self) -> asyncio.AbstractEventLoop:
        return self._delegate.get_event_loop()

    def set_event_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self._delegate.set_event_loop(loop)

    def new_event_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._delegate.new_event_loop()
        delegated_handler = loop.get_exception_handler()
        install_handler = loop.set_exception_handler

        def handle_exception(event_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            _record_asyncio_unhandled(self._recorder, context)
            if delegated_handler is None:
                event_loop.default_exception_handler(context)
            else:
                delegated_handler(event_loop, context)

        def set_delegated_handler(handler: Any) -> None:
            nonlocal delegated_handler
            delegated_handler = handler

        def get_delegated_handler() -> Any:
            return delegated_handler

        install_handler(handle_exception)
        # Frameworks and tests may install their own diagnostic handler. Keep that handler in the chain instead of
        # allowing it to replace the evidence recorder; expose it through get_exception_handler so save/restore code
        # keeps working normally.
        loop.set_exception_handler = set_delegated_handler  # type: ignore[method-assign]
        loop.get_exception_handler = get_delegated_handler  # type: ignore[method-assign]
        return loop

    def get_child_watcher(self) -> Any:
        return self._delegate.get_child_watcher()

    def set_child_watcher(self, watcher: Any) -> None:
        self._delegate.set_child_watcher(watcher)


def _record_asyncio_unhandled(recorder: _EvidenceRecorder, context: dict[str, Any]) -> None:
    message = str(context.get("message", "")).lower()
    if "task exception" in message:
        kind = "asyncio_task"
    elif "future exception" in message:
        kind = "asyncio_future"
    elif "callback" in message:
        kind = "asyncio_callback"
    else:
        kind = "asyncio_loop"
    owner = recorder.current_nodeid or "<asyncio>"
    recorder.unhandled += 1
    if recorder.current_nodeid:
        recorder.unhandled_items.add(recorder.current_nodeid)
    recorder.errors.append(f"evidence_python_unhandled:{kind}:{owner}")


def _hypothesis_metadata() -> dict[str, Any]:
    active = settings.default
    return {
        "profile": settings.get_current_profile_name(),
        "derandomize": active.derandomize,
        "database": None if active.database is None else type(active.database).__name__,
        "print_blob": active.print_blob,
        "replay_policy": "derandomize" if active.derandomize else "random",
    }


def _declared_markers(config: pytest.Config) -> set[str]:
    return {
        declaration.split(":", 1)[0].split("(", 1)[0].strip()
        for declaration in config.getini("markers")
        if declaration.strip()
    }


def _observed_pytest_plugins(config: pytest.Config) -> tuple[list[dict[str, str]], list[str], list[str]]:
    """Record explicitly loaded module plugins and reject unversioned execution extensions.

    Pytest's own ``_pytest`` modules and repository conftests are bound to the tested Python/commit
    identities already. Every other module plugin can change collection or outcomes, so evidence mode
    permits only the two plugins named by the canonical command and reports what was actually registered.
    """

    observed: dict[str, dict[str, str]] = {}
    forbidden: set[str] = set()
    loaded_modules: set[str] = set()
    for _registered_name, plugin in config.pluginmanager.list_name_plugin():
        module_name = getattr(plugin, "__name__", None)
        if not isinstance(module_name, str):
            continue
        loaded_modules.add(module_name)
        if module_name.startswith("_pytest.") or _is_repository_conftest(plugin):
            continue
        identity = _ALLOWED_MODULE_PLUGINS.get(module_name)
        if identity is None:
            forbidden.add(module_name)
            observed[module_name] = {
                "name": module_name,
                "version": str(getattr(plugin, "__version__", "unavailable")),
            }
            continue
        name, version = identity
        observed[module_name] = {"name": name, "version": version}
    missing_core = sorted(set(_REQUIRED_PYTEST_CORE_MODULES) - loaded_modules)
    return sorted(observed.values(), key=lambda item: item["name"]), sorted(forbidden), missing_core


def _is_repository_conftest(plugin: Any) -> bool:
    path = getattr(plugin, "__file__", None)
    if not isinstance(path, str):
        return False
    resolved = Path(path).resolve()
    return resolved.name == "conftest.py" and resolved.is_relative_to(_REPO_ROOT.resolve())


def _is_tracefold_project(root: Path) -> bool:
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")).get("project", {})
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return project.get("name") == "tracefold"


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
    unhandled: int = 0
    unhandled_items: set[str] = field(default_factory=set)
    session_failures: int = 0
    errors: list[str] = field(default_factory=list)
    pytest_plugins: list[dict[str, str]] = field(default_factory=list)
    hypothesis: dict[str, Any] = field(default_factory=dict)
    required_markers: list[str] = field(default_factory=list)
    marker_items: dict[str, set[str]] = field(default_factory=dict)
    allowed_deselected_modules: set[str] = field(default_factory=set)
    current_nodeid: str = ""
    original_event_loop_policy: Any = None
    session: pytest.Session | None = None
    initial_exitstatus: int = int(pytest.ExitCode.OK)
    previous_thread_excepthook: Any = None
    previous_unraisablehook: Any = None
    previous_showwarnmsg: Any = None
    thread_excepthook: Any = None
    unraisablehook: Any = None
    showwarnmsg: Any = None

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
            or self.unhandled
            or self.session_failures
            or self.errors
        )

    def write(self, path: Path, *, resource_path: Path | None) -> None:
        commit_sha = _capture(("git", "rev-parse", "HEAD"), cwd=self.root)
        github_sha = os.environ.get("GITHUB_SHA")
        if github_sha and github_sha != commit_sha:
            self.errors.append("evidence_github_sha_mismatch")
        node_version = _capture(("node", "--version"), cwd=self.root, required=False)
        if node_version == "unavailable":
            self.errors.append("evidence_node_unavailable")
        uv_version = _capture(("uv", "--version"), cwd=self.root, required=False)
        if uv_version == "unavailable":
            self.errors.append("evidence_uv_unavailable")
        resource_payload: dict[str, Any] | None = None
        if _is_tracefold_project(self.root):
            resource_payload, resource_errors = self._resource_lane()
            self.errors.extend(f"evidence_resource:{error}" for error in resource_errors)
        failed_count = max(len(self.failed), self.session_failures)
        passed_count = len(self.passed - self.failed - self.skipped - self.xfailed - self.xpassed - self.rerun)
        manifest = _lane_payload(
            lane="python",
            selected=self.selected,
            passed=passed_count,
            failed=failed_count,
            skipped=len(self.skipped),
            xfailed=len(self.xfailed),
            xpassed=len(self.xpassed),
            rerun=len(self.rerun),
            unhandled=self.unhandled,
            errors=self.errors,
            tool_versions={
                "python": platform.python_version(),
                "pytest": pytest.__version__,
                "hypothesis": hypothesis_version,
                "uv": uv_version,
                "node": node_version,
            },
        )
        manifest.update(
            {
                "commit_sha": commit_sha,
                "uv_lock_sha256": _sha256(self.root / "uv.lock"),
                "package_lock_sha256": _sha256(self.root / "web" / "package-lock.json"),
                "migration_head": _migration_head(),
                "explicitly_deselected_markers": list(_ALLOWED_DESELECTED_MARKERS),
                "pytest_plugins": self.pytest_plugins,
                "hypothesis": self.hypothesis,
                "marker_lanes": {marker: self._marker_lane(marker) for marker in self.required_markers},
            }
        )
        _write_json(path, manifest)
        if resource_payload is not None and resource_path is not None:
            _write_json(resource_path, resource_payload)

    def _marker_lane(self, marker: str) -> dict[str, int | str]:
        return self._outcomes(self.marker_items.get(marker, set()))

    def _outcomes(self, selected_items: set[str]) -> dict[str, int | str]:
        failed = len(selected_items & self.failed)
        skipped = len(selected_items & self.skipped)
        xfailed = len(selected_items & self.xfailed)
        xpassed = len(selected_items & self.xpassed)
        rerun = len(selected_items & self.rerun)
        unhandled = len(selected_items & self.unhandled_items)
        passed = len(
            (selected_items & self.passed) - self.failed - self.skipped - self.xfailed - self.xpassed - self.rerun
        )
        selected = len(selected_items)
        not_green = bool(
            selected <= 0 or passed != selected or failed or skipped or xfailed or xpassed or rerun or unhandled
        )
        return {
            "status": "failure" if not_green else "success",
            "selected": selected,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "xfailed": xfailed,
            "xpassed": xpassed,
            "rerun": rerun,
            "unhandled": unhandled,
        }

    def _resource_lane(self) -> tuple[dict[str, Any], list[str]]:
        selected_items: set[str] = set()
        for marker in _RESOURCE_MARKERS:
            selected_items.update(self.marker_items.get(marker, set()))
        outcomes = self._outcomes(selected_items)
        tool_versions, resources, errors = _resource_identity()
        payload = _lane_payload(
            lane="resource",
            selected=int(outcomes["selected"]),
            passed=int(outcomes["passed"]),
            failed=int(outcomes["failed"]),
            skipped=int(outcomes["skipped"]),
            xfailed=int(outcomes["xfailed"]),
            xpassed=int(outcomes["xpassed"]),
            rerun=int(outcomes["rerun"]),
            unhandled=int(outcomes["unhandled"]),
            errors=errors,
            tool_versions=tool_versions,
            metadata={"markers": list(_RESOURCE_MARKERS), "resources": resources},
        )
        return payload, errors


def _capture(command: tuple[str, ...], *, cwd: Path, required: bool = True) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, check=required, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        if required:
            raise
        return "unavailable"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unavailable"


def _resource_identity() -> tuple[dict[str, str], dict[str, Any], list[str]]:
    tool_versions = {
        "python": platform.python_version(),
        "pytest": pytest.__version__,
        "uv": _capture(("uv", "--version"), cwd=_REPO_ROOT, required=False),
        "psycopg": _installed_version("psycopg"),
        "aio-pika": _installed_version("aio-pika"),
        "docker": _capture(("docker", "version", "--format", "{{.Server.Version}}"), cwd=_REPO_ROOT, required=False),
    }
    resources: dict[str, Any] = {}
    errors: list[str] = []

    dsn = os.environ.get("TRACEFOLD_TEST_POSTGRES_DSN", "")
    if not dsn:
        errors.append("postgresql_dsn_missing")
    else:
        try:
            import psycopg

            with psycopg.connect(dsn, connect_timeout=5) as connection:
                database, server_version = connection.execute(
                    "SELECT current_database(), current_setting('server_version')"
                ).fetchone()
            resources["postgresql"] = {
                "database": str(database),
                "server_version": str(server_version),
            }
            tool_versions["postgresql-server"] = str(server_version)
            if database != "tracefold_test":
                errors.append("postgresql_database_identity_invalid")
        except Exception as exc:  # pragma: no cover - exercised by the canonical resource lane
            errors.append(f"postgresql_identity_unavailable:{type(exc).__name__}")

    amqp_url = os.environ.get("TRACEFOLD_TEST_AMQP_URL", "")
    if not amqp_url:
        errors.append("rabbitmq_url_missing")
    else:
        try:
            properties = asyncio.run(_rabbitmq_server_properties(amqp_url))
            product = str(properties.get("product", ""))
            server_version = str(properties.get("version", ""))
            resources["rabbitmq"] = {"product": product, "server_version": server_version}
            tool_versions["rabbitmq-server"] = server_version or "unavailable"
            if product != "RabbitMQ" or not server_version:
                errors.append("rabbitmq_identity_invalid")
        except Exception as exc:  # pragma: no cover - exercised by the canonical resource lane
            errors.append(f"rabbitmq_identity_unavailable:{type(exc).__name__}")

    return tool_versions, resources, errors


async def _rabbitmq_server_properties(url: str) -> dict[str, Any]:
    import aiormq

    connection = await asyncio.wait_for(aiormq.connect(url), timeout=5)
    try:
        return dict(connection.server_properties or {})
    finally:
        await connection.close()


def _installed_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


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


def _tracked_test_modules(root: Path) -> set[str]:
    result = subprocess.run(
        ("git", "ls-files", "--", "tests/test_*.py", "tests/**/test_*.py"),
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    return {line for line in result.stdout.splitlines() if line}


def main(argv: Sequence[str] | None = None) -> int:
    """Fail unless the repository exactly matches the tested HEAD."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments == ("--assert-clean",):
        changes = tested_head_changes(_REPO_ROOT)
        if not changes:
            return 0
        sys.stderr.write("evidence_tested_head_dirty:\n" + "\n".join(changes) + "\n")
        return 1
    if arguments and arguments[0] == "aggregate":
        return _aggregate(arguments[1:])
    if arguments and arguments[0] == "record-command":
        return _record_command(arguments[1:])
    if arguments and arguments[0] == "record-vitest":
        return _record_vitest(arguments[1:])
    if arguments and arguments[0] == "record-playwright":
        return _record_playwright(arguments[1:])
    sys.stderr.write(
        "usage: python -m tests.support.evidence --assert-clean | "
        "aggregate --lane-dir DIR --output PATH --required-lane NAME [...]\n"
    )
    return 2


def _record_playwright(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.support.evidence record-playwright")
    parser.add_argument("--lane", required=True)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args(arguments)
    playwright_version, version_errors = _node_test_tool_version("playwright", "node_modules/@playwright/test")
    report = _read_json_report(options.input)
    if report is None:
        _write_json(
            options.output,
            _lane_payload(
                lane=options.lane,
                selected=0,
                passed=0,
                errors=("playwright_report_invalid", *version_errors),
                tool_versions={
                    **_parse_tool_versions(()),
                    "playwright": playwright_version,
                },
            ),
        )
        return 1
    stats = report.get("stats", {})
    expected = int(stats.get("expected", 0))
    failed = int(stats.get("unexpected", 0))
    reported_rerun = int(stats.get("flaky", 0))
    skipped = int(stats.get("skipped", 0))
    selected = expected + failed + reported_rerun + skipped
    unhandled = _entry_count(report.get("errors"))
    errors = list(version_errors)
    selection = _read_json_report(options.selection)
    errors.extend(_playwright_selection_errors(selection, lane=options.lane, selected=selected))
    config = report.get("config") if isinstance(report.get("config"), dict) else {}
    projects = config.get("projects") if isinstance(config.get("projects"), list) else []
    if config.get("forbidOnly") is not True:
        errors.append("playwright_forbid_only_must_be_true")
    if not projects or any(
        not isinstance(project, dict) or int(project.get("retries", 0)) != 0 or int(project.get("repeatEach", 1)) != 1
        for project in projects
    ):
        errors.append("playwright_retry_or_repeat_policy_forbidden")
    tests = _playwright_tests(report)
    xfailed = 0
    xpassed = 0
    retried_tests = 0
    for test in tests:
        expected_status = str(test.get("expectedStatus", ""))
        if expected_status != "passed":
            errors.append(f"playwright_expected_status_forbidden:{expected_status or 'missing'}")
        results = test.get("results")
        if not isinstance(results, list) or not results:
            errors.append("playwright_test_result_missing")
            continue
        retries = [int(result.get("retry", 0)) for result in results if isinstance(result, dict)]
        if any(retry > 0 for retry in retries):
            retried_tests += 1
        final_result = results[-1] if isinstance(results[-1], dict) else {}
        final_status = str(final_result.get("status", ""))
        if expected_status == "failed" and final_status == "failed":
            xfailed += 1
        elif expected_status == "failed" and final_status == "passed":
            xpassed += 1
        if expected_status == "passed" and (test.get("status") != "expected" or final_result.get("status") != "passed"):
            errors.append("playwright_test_not_plain_pass")
    if len(tests) != selected:
        errors.append("playwright_test_result_count_mismatch")
    rerun = max(reported_rerun, retried_tests)
    passed = max(expected - xfailed, 0)
    failed = max(failed - xpassed, 0)
    if passed + failed + skipped + reported_rerun + xfailed + xpassed != selected:
        errors.append("playwright_semantic_outcome_count_mismatch")
    if selected <= 0:
        errors.append("playwright_report_empty")
    if unhandled:
        errors.append(f"playwright_unhandled_errors:{unhandled}")
    payload = _lane_payload(
        lane=options.lane,
        selected=selected,
        passed=passed,
        failed=failed,
        skipped=skipped,
        xfailed=xfailed,
        xpassed=xpassed,
        rerun=rerun,
        unhandled=unhandled,
        errors=errors,
        tool_versions={**_parse_tool_versions(()), "playwright": playwright_version},
    )
    _write_json(options.output, payload)
    return int(payload["status"] != "success")


def _playwright_selection_errors(selection: dict[str, Any] | None, *, lane: str, selected: int) -> list[str]:
    if selection is None:
        return ["playwright_selection_report_invalid"]
    errors: list[str] = []
    default_grep = [{"flags": "", "source": ".*"}]
    if selection.get("schemaVersion") != "tracefold_playwright_selection_v1":
        errors.append("playwright_selection_schema_invalid")
    invocation = selection.get("invocation")
    if not isinstance(invocation, list) or any(not isinstance(value, str) for value in invocation):
        errors.append("playwright_invocation_invalid")
        invocation = []
    if _has_cli_option(invocation, {"-g", "--grep", "--grep-invert", "--last-failed", "--only-changed"}):
        errors.append("playwright_partial_selection_forbidden")
    if (
        selection.get("forbidOnly") is not True
        or selection.get("shard") is not None
        or selection.get("grep") != default_grep
        or selection.get("grepInvert") != []
    ):
        errors.append("playwright_partial_selection_forbidden")
    projects = selection.get("projects") if isinstance(selection.get("projects"), list) else []
    if len(projects) != 1 or any(
        not isinstance(project, dict)
        or (lane in _REQUIRED_PLAYWRIGHT_ROOTS and project.get("browserName") != "chromium")
        or int(project.get("repeatEach", 0)) != 1
        or int(project.get("retries", -1)) != 0
        for project in projects
    ):
        errors.append("playwright_required_project_selection_invalid")
    if any(
        isinstance(project, dict)
        and (project.get("grep") != default_grep or project.get("grepInvert") != [] or project.get("testIgnore") != [])
        for project in projects
    ):
        errors.append("playwright_partial_selection_forbidden")
    selected_ids = selection.get("selectedTestIds") if isinstance(selection.get("selectedTestIds"), list) else []
    if len(selected_ids) != selected or len(set(map(str, selected_ids))) != len(selected_ids):
        errors.append("playwright_selection_result_count_mismatch")
    raw_test_files = selection.get("selectedTestFiles")
    if not isinstance(raw_test_files, list) or any(not isinstance(value, str) for value in raw_test_files):
        errors.append("playwright_selected_test_files_invalid")
        selected_files: set[str] = set()
    else:
        selected_files = set(raw_test_files)
        if raw_test_files != sorted(selected_files):
            errors.append("playwright_selected_test_files_invalid")
    roots = _REQUIRED_PLAYWRIGHT_ROOTS.get(lane)
    if roots is not None:
        tracked_modules = _tracked_web_test_modules(roots)
        if tracked_modules is None:
            errors.append("playwright_tracked_test_modules_unavailable")
        else:
            errors.extend(
                f"playwright_tracked_test_module_not_executed:{missing_module}"
                for missing_module in sorted(tracked_modules - selected_files)
            )
            errors.extend(
                f"playwright_unexpected_test_module_executed:{unexpected_module}"
                for unexpected_module in sorted(selected_files - tracked_modules)
            )
    return errors


def _playwright_tests(report: dict[str, Any]) -> list[dict[str, Any]]:
    tests: list[dict[str, Any]] = []

    def visit_suite(suite: Any) -> None:
        if not isinstance(suite, dict):
            return
        for spec in suite.get("specs", []):
            if not isinstance(spec, dict):
                continue
            tests.extend(test for test in spec.get("tests", []) if isinstance(test, dict))
        for child in suite.get("suites", []):
            visit_suite(child)

    for suite in report.get("suites", []):
        visit_suite(suite)
    return tests


def _record_vitest(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.support.evidence record-vitest")
    parser.add_argument("--lane", required=True)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args(arguments)
    vitest_version, version_errors = _node_test_tool_version("vitest", "node_modules/vitest")
    report = _read_json_report(options.input)
    if report is None:
        _write_json(
            options.output,
            _lane_payload(
                lane=options.lane,
                selected=0,
                passed=0,
                errors=("vitest_report_invalid", *version_errors),
                tool_versions={
                    **_parse_tool_versions(()),
                    "vitest": vitest_version,
                },
            ),
        )
        return 1
    selected = int(report.get("numTotalTests", 0))
    reported_passed = int(report.get("numPassedTests", 0))
    failed = int(report.get("numFailedTests", 0))
    skipped = int(report.get("numPendingTests", 0)) + int(report.get("numTodoTests", 0))
    expected_failures = int(report.get("numExpectedFailures", 0))
    xfailed = int(report.get("numXfailedTests", 0))
    xpassed = int(report.get("numXpassedTests", 0))
    semantic_tests = report.get("tests") if isinstance(report.get("tests"), list) else []
    derived_expected_failures = sum(int(test.get("fails") is True) for test in semantic_tests if isinstance(test, dict))
    derived_xfailed = sum(
        int(test.get("fails") is True and test.get("finalState") == "passed")
        for test in semantic_tests
        if isinstance(test, dict)
    )
    derived_xpassed = sum(
        int(test.get("fails") is True and test.get("finalState") == "failed")
        for test in semantic_tests
        if isinstance(test, dict)
    )
    derived_only = sum(int(test.get("only") is True) for test in semantic_tests if isinstance(test, dict))
    derived_rerun = sum(
        int(_vitest_test_was_retried_or_repeated(test)) for test in semantic_tests if isinstance(test, dict)
    )
    only = int(report.get("numOnlyTests", 0))
    rerun = derived_rerun
    unhandled = _entry_count(report.get("unhandledErrors")) + _entry_count(report.get("moduleErrors"))
    if report.get("success") is False and failed == 0 and unhandled == 0:
        unhandled = int(not (expected_failures or only or rerun or skipped))
    errors = list(version_errors)
    if report.get("schemaVersion") != "tracefold_vitest_report_v3":
        errors.append("vitest_semantics_schema_invalid")
    errors.extend(_vitest_selection_errors(report, lane=options.lane, semantic_tests=semantic_tests))
    if report.get("success") is not True:
        errors.append("vitest_report_not_success")
    if report.get("reason") != "passed":
        errors.append(f"vitest_run_reason_not_passed:{report.get('reason', 'missing')}")
    if report.get("allowOnly") is not False:
        errors.append("vitest_allow_only_must_be_false")
    if selected <= 0:
        errors.append("vitest_report_empty")
    if reported_passed + failed + skipped + xfailed + xpassed != selected:
        errors.append("vitest_report_outcome_count_mismatch")
    if len(semantic_tests) != selected:
        errors.append("vitest_semantic_test_count_mismatch")
    if expected_failures != derived_expected_failures:
        errors.append("vitest_expected_failure_count_mismatch")
    if xfailed != derived_xfailed or xpassed != derived_xpassed or xfailed + xpassed != expected_failures:
        errors.append("vitest_expected_failure_outcome_mismatch")
    if only != derived_only:
        errors.append("vitest_only_count_mismatch")
    if expected_failures:
        errors.append(f"vitest_expected_failures:{expected_failures}")
    if only:
        errors.append(f"vitest_only_tests:{only}")
    if rerun:
        errors.append(f"vitest_retried_or_repeated_tests:{rerun}")
    errors.extend(
        f"vitest_test_not_plain_pass:{test.get('id', 'missing')}"
        for test in semantic_tests
        if isinstance(test, dict) and not _vitest_test_is_plain_pass(test)
    )
    if unhandled:
        errors.append(f"vitest_unhandled_errors:{unhandled}")
    payload = _lane_payload(
        lane=options.lane,
        selected=selected,
        passed=reported_passed,
        failed=failed,
        skipped=skipped,
        xfailed=xfailed,
        xpassed=xpassed,
        rerun=rerun,
        unhandled=unhandled,
        errors=errors,
        tool_versions={**_parse_tool_versions(()), "vitest": vitest_version},
    )
    _write_json(options.output, payload)
    return int(payload["status"] != "success")


def _vitest_selection_errors(report: dict[str, Any], *, lane: str, semantic_tests: list[Any]) -> list[str]:
    errors: list[str] = []
    invocation = report.get("invocation")
    if not isinstance(invocation, list) or any(not isinstance(value, str) for value in invocation):
        errors.append("vitest_invocation_invalid")
        invocation = []
    if _has_cli_option(invocation, {"-t", "--testNamePattern"}):
        errors.append("vitest_partial_selection_forbidden")

    raw_test_files = report.get("testFiles")
    if not isinstance(raw_test_files, list) or any(not isinstance(value, str) for value in raw_test_files):
        errors.append("vitest_test_files_invalid")
        test_files: list[str] = []
    else:
        test_files = raw_test_files
        if test_files != sorted(set(test_files)):
            errors.append("vitest_test_files_invalid")

    reported_files = set(test_files)
    semantic_files = {
        str(test.get("file")) for test in semantic_tests if isinstance(test, dict) and isinstance(test.get("file"), str)
    }
    if any(isinstance(test, dict) and not isinstance(test.get("file"), str) for test in semantic_tests):
        errors.append("vitest_semantic_test_file_missing")
    errors.extend(
        f"vitest_semantic_test_file_not_declared:{missing_file}"
        for missing_file in sorted(semantic_files - reported_files)
    )

    if lane not in _REQUIRED_VITEST_ROOTS:
        return errors

    tracked_modules = _tracked_vitest_modules(lane)
    if tracked_modules is None:
        errors.append("vitest_tracked_test_modules_unavailable")
        return errors
    errors.extend(
        f"vitest_tracked_test_module_not_executed:{missing_module}"
        for missing_module in sorted(tracked_modules - reported_files)
    )
    errors.extend(
        f"vitest_unexpected_test_module_executed:{unexpected_module}"
        for unexpected_module in sorted(reported_files - tracked_modules)
    )
    return errors


def _tracked_vitest_modules(lane: str) -> set[str] | None:
    return _tracked_web_test_modules(_REQUIRED_VITEST_ROOTS[lane])


def _tracked_web_test_modules(roots: tuple[str, ...]) -> set[str] | None:
    try:
        result = subprocess.run(
            ("git", "ls-files", "--", *roots),
            cwd=_REPO_ROOT,
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {
        path.removeprefix("web/") for path in result.stdout.splitlines() if path.endswith(_VITEST_TEST_FILE_SUFFIXES)
    }


def _has_cli_option(invocation: list[str], options: set[str]) -> bool:
    return any(
        argument in options or any(argument.startswith(f"{option}=") for option in options if option.startswith("--"))
        for argument in invocation
    )


def _vitest_test_was_retried_or_repeated(test: dict[str, Any]) -> bool:
    retry = test.get("retry", 0)
    configured_retry = int(retry.get("count", 0)) if isinstance(retry, dict) else int(retry or 0)
    return bool(
        configured_retry
        or int(test.get("retryCount", 0))
        or int(test.get("repeats", 0))
        or int(test.get("repeatCount", 0))
        or test.get("flaky") is True
    )


def _vitest_test_is_plain_pass(test: dict[str, Any]) -> bool:
    return bool(
        test.get("fails") is False
        and test.get("only") is False
        and test.get("mode") == "run"
        and test.get("state") == "passed"
        and test.get("finalState") == "passed"
        and not test.get("errors")
        and not _vitest_test_was_retried_or_repeated(test)
    )


def _entry_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return 1


def _read_json_report(path: Path) -> dict[str, Any] | None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return report if isinstance(report, dict) else None


def _package_lock_version(package: str) -> str:
    try:
        lock = json.loads((_REPO_ROOT / "web" / "package-lock.json").read_text(encoding="utf-8"))
        version = lock["packages"][package]["version"]
    except (KeyError, OSError, TypeError, json.JSONDecodeError):
        return "unavailable"
    return str(version)


def _node_module_version(package: str) -> str:
    try:
        manifest = json.loads((_REPO_ROOT / "web" / package / "package.json").read_text(encoding="utf-8"))
        version = manifest["version"]
    except (KeyError, OSError, TypeError, json.JSONDecodeError):
        return "unavailable"
    return str(version)


def _node_test_tool_version(tool: str, package: str) -> tuple[str, list[str]]:
    locked_version = _package_lock_version(package)
    runtime_version = _node_module_version(package)
    errors: list[str] = []
    if locked_version == "unavailable":
        errors.append(f"{tool}_lock_version_unavailable")
    if runtime_version == "unavailable":
        errors.append(f"{tool}_runtime_version_unavailable")
    elif locked_version not in {"unavailable", runtime_version}:
        errors.append(f"{tool}_lock_runtime_version_mismatch")
    return runtime_version, errors


def _record_command(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.support.evidence record-command")
    parser.add_argument("--lane", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tool", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args(arguments)
    command = tuple(options.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    tool_versions = _parse_tool_versions(options.tool)
    try:
        result = subprocess.run(command, cwd=_REPO_ROOT, check=False)
        returncode = result.returncode
    except OSError:
        returncode = 127
    errors = [] if returncode == 0 else [f"command_exit_nonzero:{returncode}"]
    payload = _lane_payload(
        lane=options.lane,
        selected=1,
        passed=int(returncode == 0),
        failed=int(returncode != 0),
        errors=errors,
        tool_versions=tool_versions,
    )
    _write_json(options.output, payload)
    return returncode


def _parse_tool_versions(values: Sequence[str]) -> dict[str, str]:
    versions = {
        "python": platform.python_version(),
        "uv": _capture(("uv", "--version"), cwd=_REPO_ROOT, required=False),
    }
    for value in values:
        name, separator, version = value.partition("=")
        if not separator or not name.strip() or not version.strip():
            raise ValueError(f"invalid tool version {value!r}; expected NAME=VERSION")
        versions[name.strip()] = version.strip()
    return versions


def _lane_payload(
    *,
    lane: str,
    selected: int,
    passed: int,
    failed: int = 0,
    skipped: int = 0,
    xfailed: int = 0,
    xpassed: int = 0,
    rerun: int = 0,
    unhandled: int = 0,
    errors: Sequence[str] = (),
    tool_versions: dict[str, str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    not_green = bool(
        selected <= 0 or passed != selected or failed or skipped or xfailed or xpassed or rerun or unhandled or errors
    )
    payload: dict[str, Any] = {
        "schema_version": LANE_SCHEMA_VERSION,
        "lane": lane,
        "required": True,
        "status": "failure" if not_green else "success",
        "commit_sha": _capture(("git", "rev-parse", "HEAD"), cwd=_REPO_ROOT),
        "git_tree_sha": _capture(("git", "rev-parse", "HEAD^{tree}"), cwd=_REPO_ROOT),
        "selected": selected,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "xfailed": xfailed,
        "xpassed": xpassed,
        "rerun": rerun,
        "unhandled": unhandled,
        "errors": sorted(set(errors)),
        "tool_versions": tool_versions or _parse_tool_versions(()),
    }
    if metadata:
        payload["metadata"] = metadata
    return payload


def _aggregate(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.support.evidence aggregate")
    parser.add_argument("--lane-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--required-lane", action="append", required=True)
    options = parser.parse_args(arguments)
    required_lanes = tuple(dict.fromkeys(options.required_lane))
    errors: list[str] = []
    lanes: dict[str, Any] = {}
    commit_sha = _capture(("git", "rev-parse", "HEAD"), cwd=_REPO_ROOT)
    git_tree_sha = _capture(("git", "rev-parse", "HEAD^{tree}"), cwd=_REPO_ROOT)
    for lane in required_lanes:
        path = options.lane_dir / f"{lane}.json"
        if not path.is_file():
            errors.append(f"required_lane_manifest_missing:{lane}")
            continue
        lane_manifest = _read_json_report(path)
        if lane_manifest is None:
            errors.append(f"required_lane_manifest_invalid:{lane}")
            continue
        lanes[lane] = lane_manifest
        if lane_manifest.get("schema_version") != LANE_SCHEMA_VERSION:
            errors.append(f"required_lane_schema_invalid:{lane}")
        if lane_manifest.get("lane") != lane:
            errors.append(f"required_lane_name_mismatch:{lane}")
        if lane_manifest.get("required") is not True:
            errors.append(f"required_lane_not_required:{lane}")
        if lane_manifest.get("selected", 0) <= 0:
            errors.append(f"required_lane_empty:{lane}")
        if lane_manifest.get("passed") != lane_manifest.get("selected"):
            errors.append(f"required_lane_pass_count_mismatch:{lane}")
        status = lane_manifest.get("status")
        if status != "success":
            errors.append(f"required_lane_status_not_success:{lane}:{status}")
        for field_name in _NON_GREEN_OUTCOME_FIELDS:
            value = lane_manifest.get(field_name, 0)
            if value != 0:
                errors.append(f"required_lane_not_green:{lane}:{field_name}={value}")
        if lane_manifest.get("errors"):
            errors.append(f"required_lane_has_errors:{lane}")
        if not lane_manifest.get("tool_versions"):
            errors.append(f"required_lane_tool_versions_missing:{lane}")
        if lane_manifest.get("commit_sha") != commit_sha:
            errors.append(f"required_lane_commit_mismatch:{lane}")
        if lane_manifest.get("git_tree_sha") != git_tree_sha:
            errors.append(f"required_lane_tree_mismatch:{lane}")
    manifest = {
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "commit_sha": commit_sha,
        "git_tree_sha": git_tree_sha,
        "required_lanes": list(required_lanes),
        "overall": "failure" if errors else "success",
        "errors": sorted(set(errors)),
        "lanes": lanes,
    }
    _write_json(options.output, manifest)
    return int(bool(errors))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    destination = path if path.is_absolute() else _REPO_ROOT / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _migration_head() -> str:
    from tracefold.platform.postgres.migrations import latest_migration_version

    return latest_migration_version()


__all__ = ["AGGREGATE_SCHEMA_VERSION", "LANE_SCHEMA_VERSION", "SCHEMA_VERSION", "main", "tested_head_changes"]


if __name__ == "__main__":
    raise SystemExit(main())
