"""The DEMO executor shares the application release and normal Compose lifecycle."""

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.deploy
ROOT = Path(__file__).resolve().parents[2]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


def test_executor_is_a_default_application_service() -> None:
    compose = _compose()
    app = compose["x-tracefold-app"]
    executor = compose["services"]["executor"]
    assert "profiles" not in executor
    assert executor["image"] == app["image"]
    assert executor["build"] == app["build"]
    assert executor["command"] == ["tracefold", "executor"]
    assert executor["restart"] == "unless-stopped"
    assert executor["depends_on"] == {
        "postgres": {"condition": "service_healthy"},
        "migrate": {"condition": "service_completed_successfully"},
    }
    assert "nautilus" not in compose["services"]


def test_public_lifecycle_has_no_separate_runtime_image() -> None:
    listed = subprocess.run(["make", "help"], cwd=ROOT, capture_output=True, check=True, text=True).stdout
    targets = {line.split(maxsplit=1)[0] for line in listed.splitlines() if line}
    assert {"up", "status", "down", "dev-executor"} <= targets
    assert not any(target.startswith("runtime-") for target in targets)

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages = [line.split(" AS ")[1].strip() for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert stages == ["web-builder", "python-deps", "base", "app"]
    assert (ROOT / ".python-version").read_text().strip() == "3.13"
