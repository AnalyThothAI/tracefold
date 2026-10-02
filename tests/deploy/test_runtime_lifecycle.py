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


@pytest.mark.parametrize("actual", ["20261001_0424", "20261002_0425"])
def test_migrate_proves_image_head_before_application_start(actual, capsys):
    from scripts.deploy import Deployment, DeploymentError

    class FakeDeployment:
        migrate = Deployment.migrate

        def __init__(self):
            self.calls = []

        def compose(self, *args, **kwargs):
            self.calls.append(args)

        def container(self, service):
            return "migration-container"

        def run(self, *args, **kwargs):
            return "0"

        def inspect(self, container, format):
            return "sha256:test"

        def image_head(self, image):
            return "20261002_0425"

        def database_head(self):
            return actual

    deploy = FakeDeployment()
    if actual == "20261001_0424":
        with pytest.raises(DeploymentError, match="migration head mismatch"):
            deploy.migrate()
    else:
        deploy.migrate()
        assert "Migration head verified: 20261002_0425" in capsys.readouterr().out
        assert ("logs", "--no-color", "migrate") in deploy.calls
