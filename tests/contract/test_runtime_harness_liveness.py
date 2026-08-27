from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.browser import run_full_stack_smoke
from tests.golden import conftest as golden_conftest


class _Process:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.log: Any = None

    def poll(self) -> int | None:
        return self.returncode


def _popen_pair(worker: _Process, serve: _Process) -> Callable[..., _Process]:
    pending = iter((worker, serve))

    def popen(*_args: object, stdout: Any, **_kwargs: object) -> _Process:
        process = next(pending)
        process.log = stdout
        return process

    return popen


async def _async_noop(*_args: object, **_kwargs: object) -> None:
    return None


def test_browser_success_fails_when_workers_exited_after_persisting_the_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = _Process()
    serve = _Process()
    monkeypatch.setattr(run_full_stack_smoke.subprocess, "Popen", _popen_pair(worker, serve))

    def run_playwright(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        worker.returncode = 17
        worker.log.write("workers root failed after persisting the fact\n")
        worker.log.flush()
        return subprocess.CompletedProcess(args=(), returncode=0)

    monkeypatch.setattr(run_full_stack_smoke.subprocess, "run", run_playwright)
    monkeypatch.setattr(run_full_stack_smoke, "_require_resources", lambda *_args: None)
    monkeypatch.setattr(run_full_stack_smoke, "_reset_postgres", lambda *_args: None)
    monkeypatch.setattr(run_full_stack_smoke, "_unused_port", lambda: 43101)
    monkeypatch.setattr(run_full_stack_smoke, "_wait_for_http", lambda *_args: None)
    monkeypatch.setattr(run_full_stack_smoke, "_wait_for_logged_port", lambda *_args: 43102)
    monkeypatch.setattr(run_full_stack_smoke, "_publish_opennews", _async_noop)
    monkeypatch.setattr(run_full_stack_smoke, "_wait_for_service_fact", lambda *_args: None)
    monkeypatch.setattr(run_full_stack_smoke, "_terminate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_full_stack_smoke, "_delete_topology", _async_noop)
    monkeypatch.setattr(
        run_full_stack_smoke.sys,
        "argv",
        [
            "run_full_stack_smoke.py",
            "--playwright-json",
            str(tmp_path / "playwright.json"),
            "--playwright-selection",
            str(tmp_path / "playwright-selection.json"),
        ],
    )

    with pytest.raises(AssertionError, match="workers root failed after persisting the fact"):
        run_full_stack_smoke.main()


def test_golden_teardown_fails_when_serve_exited_after_the_assertions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = _Process()
    serve = _Process()
    monkeypatch.setattr(golden_conftest.subprocess, "Popen", _popen_pair(worker, serve))
    monkeypatch.setattr(golden_conftest, "_reset_postgres", lambda *_args: None)
    monkeypatch.setattr(golden_conftest, "_unused_port", lambda: 43201)
    monkeypatch.setattr(golden_conftest, "_wait_for_http", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(golden_conftest, "_wait_for_logged_port", lambda *_args, **_kwargs: 43202)
    monkeypatch.setattr(golden_conftest, "_terminate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(golden_conftest, "_delete_topology", _async_noop)

    fixture = golden_conftest.golden_runtime.__wrapped__(
        "postgresql://example/tracefold_test",
        "amqp://example/",
        tmp_path,
    )
    next(fixture)
    serve.returncode = 23
    serve.log.write("serve root failed after the golden assertions\n")
    serve.log.flush()

    with pytest.raises(AssertionError, match="serve root failed after the golden assertions"):
        next(fixture)


def test_browser_success_fails_when_serve_readiness_regresses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = _Process()
    serve = _Process()
    monkeypatch.setattr(run_full_stack_smoke.subprocess, "Popen", _popen_pair(worker, serve))

    def run_playwright(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        serve.log.write("serve readiness regressed after browser assertions\n")
        serve.log.flush()
        return subprocess.CompletedProcess(args=(), returncode=0)

    monkeypatch.setattr(run_full_stack_smoke.subprocess, "run", run_playwright)
    monkeypatch.setattr(run_full_stack_smoke, "_require_resources", lambda *_args: None)
    monkeypatch.setattr(run_full_stack_smoke, "_reset_postgres", lambda *_args: None)
    monkeypatch.setattr(run_full_stack_smoke, "_unused_port", lambda: 43301)
    monkeypatch.setattr(run_full_stack_smoke, "_wait_for_http", lambda *_args: None)
    monkeypatch.setattr(run_full_stack_smoke, "_wait_for_logged_port", lambda *_args: 43302)
    monkeypatch.setattr(run_full_stack_smoke, "_publish_opennews", _async_noop)
    monkeypatch.setattr(run_full_stack_smoke, "_wait_for_service_fact", lambda *_args: None)
    monkeypatch.setattr(run_full_stack_smoke, "_terminate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_full_stack_smoke, "_delete_topology", _async_noop)
    monkeypatch.setattr(
        run_full_stack_smoke.sys,
        "argv",
        [
            "run_full_stack_smoke.py",
            "--playwright-json",
            str(tmp_path / "playwright.json"),
            "--playwright-selection",
            str(tmp_path / "playwright-selection.json"),
        ],
    )

    def readiness(url: str, **_kwargs: object) -> httpx.Response:
        if url == "http://127.0.0.1:43301/readyz":
            return httpx.Response(200, text="worker ready")
        return httpx.Response(503, text="serve not ready")

    monkeypatch.setattr(run_full_stack_smoke.httpx, "get", readiness)

    with pytest.raises(AssertionError, match="serve readiness regressed after browser assertions"):
        run_full_stack_smoke.main()


def test_golden_teardown_fails_when_serve_readiness_regresses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = _Process()
    serve = _Process()
    monkeypatch.setattr(golden_conftest.subprocess, "Popen", _popen_pair(worker, serve))
    monkeypatch.setattr(golden_conftest, "_reset_postgres", lambda *_args: None)
    monkeypatch.setattr(golden_conftest, "_unused_port", lambda: 43401)
    monkeypatch.setattr(golden_conftest, "_wait_for_http", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(golden_conftest, "_wait_for_logged_port", lambda *_args, **_kwargs: 43402)
    monkeypatch.setattr(golden_conftest, "_terminate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(golden_conftest, "_delete_topology", _async_noop)

    def readiness(url: str, **_kwargs: object) -> httpx.Response:
        if url == "http://127.0.0.1:43401/readyz":
            return httpx.Response(200, text="worker ready")
        return httpx.Response(503, text="serve not ready")

    monkeypatch.setattr(golden_conftest.httpx, "get", readiness)
    fixture = golden_conftest.golden_runtime.__wrapped__(
        "postgresql://example/tracefold_test",
        "amqp://example/",
        tmp_path,
    )
    next(fixture)
    serve.log.write("serve readiness regressed after golden assertions\n")
    serve.log.flush()

    with pytest.raises(AssertionError, match="serve readiness regressed after golden assertions"):
        next(fixture)


@pytest.mark.parametrize("harness", ["browser", "golden"])
def test_final_readiness_fails_if_workers_exit_during_the_serve_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    harness: str,
) -> None:
    module = run_full_stack_smoke if harness == "browser" else golden_conftest
    worker = _Process()
    serve = _Process()
    worker_port = 43501
    serve_port = 43502
    monkeypatch.setattr(module.subprocess, "Popen", _popen_pair(worker, serve))
    monkeypatch.setattr(module, "_reset_postgres", lambda *_args: None)
    monkeypatch.setattr(module, "_unused_port", lambda: worker_port)
    monkeypatch.setattr(module, "_wait_for_http", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_wait_for_logged_port", lambda *_args, **_kwargs: serve_port)
    monkeypatch.setattr(module, "_terminate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_delete_topology", _async_noop)

    def readiness(url: str, **_kwargs: object) -> httpx.Response:
        if url == f"http://127.0.0.1:{worker_port}/readyz":
            return httpx.Response(200, text="worker ready")
        worker.returncode = 17
        worker.log.write("workers exited during the serve readiness check\n")
        worker.log.flush()
        return httpx.Response(200, text="serve ready")

    monkeypatch.setattr(module.httpx, "get", readiness)
    if harness == "browser":
        monkeypatch.setattr(module, "_require_resources", lambda *_args: None)
        monkeypatch.setattr(module, "_publish_opennews", _async_noop)
        monkeypatch.setattr(module, "_wait_for_service_fact", lambda *_args: None)
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(args=(), returncode=0),
        )
        monkeypatch.setattr(
            module.sys,
            "argv",
            [
                "run_full_stack_smoke.py",
                "--playwright-json",
                str(tmp_path / "playwright.json"),
                "--playwright-selection",
                str(tmp_path / "playwright-selection.json"),
            ],
        )
        invoke = module.main
    else:
        fixture = module.golden_runtime.__wrapped__(
            "postgresql://example/tracefold_test",
            "amqp://example/",
            tmp_path,
        )
        next(fixture)

        def invoke() -> None:
            next(fixture)

    with pytest.raises(AssertionError, match="workers exited during the serve readiness check"):
        invoke()
