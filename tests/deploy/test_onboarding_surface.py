"""Application image deployment and DEMO executor maintenance gates."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.deploy import APP_SERVICES, MUTATIONS, Deployment, DeploymentError, deployment_lock

pytestmark = pytest.mark.deploy
ROOT = Path(__file__).resolve().parents[2]
APP_IMAGE = "sha256:" + "a" * 64


class FakeDeployment(Deployment):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(ROOT, {"HOME": str(tmp_path), "IMAGE_ID": APP_IMAGE})
        services = {}
        for name in ("postgres", "rabbitmq", "rabbitmq-policy", "migrate", *APP_SERVICES):
            services[name] = {
                "image": "fixture-image",
                "volumes": [
                    {"target": "/root/.tracefold/config.yaml", "source": str(tmp_path / "operator/config.yaml")}
                ],
                "ports": [{"host_ip": "127.0.0.1", "published": str({"serve": 8765, "workers": 8766}.get(name, 8767))}],
            }
        self._model = {"name": "tracefold-test", "services": services}
        self.calls: list[tuple[str, ...]] = []
        self.migration_exit = "0"
        self.db_head = "head"
        self.target_head = "head"
        self.execution_enabled = False
        self.legacy_running = False
        self.bad_config = False
        self.bad_images: set[str] = set()
        self.bad_health: set[str] = set()
        self.states: dict[str, str] = {}
        self.ready_image = APP_IMAGE
        self.ready_manifest = "manifest"

    def config_data(self) -> dict:
        return {"trading": {"enabled": True, "execution": {"enabled": self.execution_enabled}}}

    def run(self, *args: str, capture: bool = False, timeout: float | None = None) -> str:
        self.calls.append(args)
        if args[: len(self.prefix)] == tuple(self.prefix):
            command = args[len(self.prefix) :]
            if command[0] == "ps":
                return command[-1] + "-id"
            if command[:2] == ("config", "--images"):
                return "fixture-image"
            if command[0] == "run":
                if command[-1] == "config":
                    if self.bad_config:
                        raise DeploymentError("invalid config")
                    return json.dumps({"ok": True, "data": self.config_data()})
                return json.dumps({"ok": True, "data": {"runtime_manifest_sha": "manifest"}})
            if command[0] == "exec":
                if command[-1] == "config":
                    return json.dumps({"ok": True, "data": self.config_data()})
                return self.db_head
            return ""
        if args[:2] == ("docker", "ps"):
            return "legacy-id" if self.legacy_running else ""
        if args[:2] == ("docker", "wait"):
            return self.migration_exit
        if args[:3] == ("docker", "image", "inspect"):
            return APP_IMAGE
        if args[:2] == ("docker", "run"):
            return self.target_head if "-c" in args else "{}"
        if args[:2] == ("docker", "inspect"):
            expression, service = args[3], args[4].removesuffix("-id")
            if expression == "{{.Image}}":
                return "sha256:" + "c" * 64 if service in self.bad_images else APP_IMAGE
            if "State.Status" in expression:
                return self.states.get(service, "exited" if service in {"migrate", "rabbitmq-policy"} else "running")
            if "State.ExitCode" in expression:
                return self.migration_exit
            if "State.Health" in expression:
                return "unhealthy" if service in self.bad_health else "healthy"
        if args[0] == "git":
            return "f" * 40 if args[1] == "rev-parse" else ""
        raise AssertionError(f"unexpected invocation: {args}")

    def http(self, service: str, path: str) -> str:
        if path == "/":
            return "<!doctype html><html></html>"
        return json.dumps({"image_digest": self.ready_image, "runtime_manifest_sha": self.ready_manifest})

    def commands(self) -> list[tuple[str, ...]]:
        return [call[len(self.prefix) :] for call in self.calls if call[: len(self.prefix)] == tuple(self.prefix)]


@pytest.fixture
def deployment(tmp_path: Path) -> FakeDeployment:
    return FakeDeployment(tmp_path)


@pytest.mark.parametrize("action", ["up", "deploy-image"])
def test_migration_precedes_every_application_role(deployment: FakeDeployment, action: str) -> None:
    deployment.execute(action)
    calls = deployment.calls
    stop = next(i for i, c in enumerate(calls) if c[-(len(APP_SERVICES) + 1) :] == ("stop", *APP_SERVICES))
    wait = calls.index(("docker", "wait", "migrate-id"))
    app_up = next(i for i, c in enumerate(calls) if "up" in c and c[-len(APP_SERVICES) :] == APP_SERVICES)
    assert stop < wait < app_up
    assert "executor" in calls[app_up]
    assert deployment.env["TRACEFOLD_APP_IMAGE"] == APP_IMAGE
    assert deployment.env["TRACEFOLD_IMAGE_DIGEST"] == APP_IMAGE


@pytest.mark.parametrize("action", ["up", "deploy-image", "db-migrate"])
def test_failed_migration_never_starts_readers(deployment: FakeDeployment, action: str) -> None:
    deployment.migration_exit = "17"
    with pytest.raises(DeploymentError, match="migrate exited 17"):
        deployment.execute(action)
    assert not [c for c in deployment.commands() if c[0] == "up" and "serve" in c]


@pytest.mark.parametrize("action", ["up", "deploy-image"])
def test_invalid_config_is_rejected_before_stop(deployment: FakeDeployment, action: str) -> None:
    deployment.bad_config = True
    with pytest.raises(DeploymentError, match="invalid config"):
        deployment.execute(action)
    assert not [c for c in deployment.commands() if c[0] == "stop"]


@pytest.mark.parametrize("action", ["up", "db-migrate"])
def test_schema_change_requires_flattened_execution_window(deployment: FakeDeployment, action: str) -> None:
    deployment.execution_enabled = True
    deployment.target_head = "new-head"
    with pytest.raises(DeploymentError, match="maintenance window"):
        deployment.execute(action)
    assert not [c for c in deployment.commands() if c[0] in {"stop", "up"}]


def test_legacy_runtime_blocks_migration(deployment: FakeDeployment) -> None:
    deployment.legacy_running = True
    with pytest.raises(DeploymentError, match="legacy Nautilus"):
        deployment.execute("up")
    assert not [c for c in deployment.commands() if c[0] == "stop"]


def test_deploy_image_requires_matching_head_and_exact_image(deployment: FakeDeployment) -> None:
    deployment.execute("deploy-image")
    assert not [c for c in deployment.calls if c[0] in {"git", "gh", "uv"}]
    deployment.db_head = "incompatible"
    deployment.calls.clear()
    with pytest.raises(DeploymentError, match="Alembic heads differ"):
        deployment.execute("deploy-image")
    assert not [c for c in deployment.commands() if c[0] == "stop"]


@pytest.mark.parametrize("image", ["latest", "", "sha256:abc"])
def test_deploy_image_requires_full_digest(deployment: FakeDeployment, image: str) -> None:
    deployment.env["IMAGE_ID"] = image
    with pytest.raises(DeploymentError, match="complete local ID"):
        deployment.execute("deploy-image")


@pytest.mark.parametrize("service", ["rabbitmq-policy", "migrate", *APP_SERVICES])
def test_every_role_uses_the_requested_image(deployment: FakeDeployment, service: str) -> None:
    deployment.bad_images.add(service)
    with pytest.raises(DeploymentError, match="immutable image"):
        deployment.execute("deploy-image")


@pytest.mark.parametrize("field", ["ready_image", "ready_manifest"])
def test_workers_identity_is_proven_after_start(deployment: FakeDeployment, field: str) -> None:
    setattr(deployment, field, "wrong")
    with pytest.raises(DeploymentError, match=r"image_digest|manifest"):
        deployment.execute("up")


def test_down_preserves_named_volumes(deployment: FakeDeployment) -> None:
    deployment.execute("down")
    assert deployment.commands() == [("down",)]


def test_status_checks_executor_and_one_shots(deployment: FakeDeployment) -> None:
    deployment.bad_health.add("workers")
    with pytest.raises(DeploymentError, match="workers"):
        deployment.execute("status")
    deployment.bad_health.clear()
    deployment.states["executor"] = "exited"
    with pytest.raises(DeploymentError, match="executor"):
        deployment.execute("status")


def test_db_migrate_leaves_application_stopped(deployment: FakeDeployment) -> None:
    deployment.execute("db-migrate")
    assert ("stop", *APP_SERVICES) in deployment.commands()
    assert not [c for c in deployment.commands() if c[0] == "up" and "serve" in c]


@pytest.mark.parametrize("action", sorted(MUTATIONS))
def test_all_mutations_share_lock(deployment: FakeDeployment, action: str) -> None:
    directory = Path(deployment.env["HOME"]) / ".cache/tracefold/deploy"
    with deployment_lock(directory, deployment.project), pytest.raises(DeploymentError, match="already in progress"):
        deployment.execute(action)
    assert not deployment.calls


def test_lock_is_released_on_crash(tmp_path: Path) -> None:
    code = (
        "from pathlib import Path; from scripts.deploy import deployment_lock; import time; "
        f'lock=deployment_lock(Path({str(tmp_path)!r}), "test"); lock.__enter__(); '
        'print("locked", flush=True); time.sleep(60)'
    )
    child = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "locked"
        with pytest.raises(DeploymentError, match="already in progress"), deployment_lock(tmp_path, "test"):
            pass
    finally:
        child.kill()
        child.wait(timeout=5)
    with deployment_lock(tmp_path, "test"):
        pass
