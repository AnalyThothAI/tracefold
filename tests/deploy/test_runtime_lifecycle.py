"""Compose process separation and service contracts; sequencing is tested behaviorally."""

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.deploy
ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_TARGETS = ("runtime-build", "runtime-up", "runtime-restart", "runtime-down", "runtime-logs")
_STOP_GRACE_SECONDS = 90


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


def test_the_runtime_lifecycle_is_a_public_operator_surface() -> None:
    listed = subprocess.run(
        ["make", "help"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    targets = {line.split(maxsplit=1)[0] for line in listed.splitlines() if line}

    assert set(_RUNTIME_TARGETS) <= targets
    assert "runtime-status" in targets
    # `status` is still one entry; it is the composition of the two halves, so an operator who runs
    # it after a deploy still learns whether the runtime is up.
    assert {"status", "status-app"} <= targets


def test_the_runtime_service_depends_on_postgres_alone_and_carries_its_own_image() -> None:
    nautilus = _compose()["services"]["nautilus"]

    assert nautilus["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert nautilus["profiles"] == ["execution"]
    # `unless-stopped`, not `on-failure:N`. A bounded restart policy gives up after N transient
    # failures, and what it gives up on is the process protecting an open position; a stale-image
    # crash loop costs one SELECT per attempt because the schema probe runs before anything else,
    # and `make runtime-up` refuses to start such an image in the first place.
    assert nautilus["restart"] == "unless-stopped"
    assert nautilus["image"].startswith("${TRACEFOLD_RUNTIME_IMAGE:-")
    assert nautilus["build"]["target"] == "runtime"


def test_the_shutdown_budget_covers_the_worst_case_the_runtime_can_produce() -> None:
    from tracefold.app.nautilus import root

    nautilus = _compose()["services"]["nautilus"]

    assert nautilus["stop_grace_period"] == f"{_STOP_GRACE_SECONDS}s"
    # Three sequential Nautilus stop budgets plus the bridge's final projection write. At the old
    # 40 s grace this arithmetic did not close, which made SIGKILL the normal exit.
    assert 3 * root._STOP_TIMEOUT_SECONDS + 20 <= _STOP_GRACE_SECONDS


def test_the_dockerfile_builds_a_runtime_stage_and_still_defaults_to_the_application() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages = [line.split(" AS ")[1].strip() for line in dockerfile.splitlines() if line.startswith("FROM ")]

    assert stages == ["web-builder", "python-deps", "base", "runtime", "app"]
    runtime_stage = dockerfile.split("FROM base AS runtime", 1)[1].split("FROM base AS app", 1)[0]
    application_stage = dockerfile.split("FROM base AS app", 1)[1]

    assert 'CMD ["tracefold", "nautilus", "run"]' in runtime_stage
    assert "EXPOSE 8767" in runtime_stage
    # No console bundle in the runtime image: nothing in it serves one, and copying it would make a
    # frontend-only change produce a new runtime image ID.
    assert "web/dist" not in runtime_stage
    assert "web/dist" in application_stage
    assert 'CMD ["tracefold", "serve"]' in application_stage


def test_project_python_still_matches_the_image() -> None:
    assert (ROOT / ".python-version").read_text().strip() == "3.13"
    assert "sys.version_info[:2] == (3, 13)" in (ROOT / "Dockerfile").read_text()
