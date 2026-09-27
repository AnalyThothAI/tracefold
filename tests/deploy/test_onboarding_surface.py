"""Behavioral lifecycle tests: exercise the sequencer, not shell spelling in Make."""

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
RUNTIME_IMAGE = "sha256:" + "b" * 64


class FakeDeployment(Deployment):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(ROOT, {"HOME": str(tmp_path), "IMAGE_ID": APP_IMAGE})
        services = {}
        for name in ("postgres", "rabbitmq", "rabbitmq-policy", "migrate", *APP_SERVICES, "nautilus"):
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
        self.runtime_exists = False
        self.runtime_running = False
        self.execution_enabled = False
        self.bad_config = False
        self.bad_images: set[str] = set()
        self.bad_health: set[str] = set()
        self.states: dict[str, str] = {}
        self.ready_image = APP_IMAGE
        self.ready_manifest = "manifest"
        self.runtime_http_down = False

    def config_data(self) -> dict:
        return {"trading": {"enabled": True, "execution": {"enabled": self.execution_enabled}}}

    def run(self, *args: str, capture: bool = False, timeout: float | None = None) -> str:
        self.calls.append(args)
        if args[: len(self.prefix)] == tuple(self.prefix):
            command = args[len(self.prefix) :]
            if command[0] == "ps":
                service = command[-1]
                if service == "nautilus":
                    exists = self.runtime_exists if "--all" in command else self.runtime_running
                    return "nautilus-id" if exists else ""
                return service + "-id"
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
            if command[0] == "stop" and command[-1] == "nautilus":
                self.runtime_running = False
            if command[0] == "up" and command[-1] == "nautilus":
                self.runtime_exists = self.runtime_running = True
            return ""
        if args[:2] == ("docker", "wait"):
            return self.migration_exit
        if args[:3] == ("docker", "image", "inspect"):
            return RUNTIME_IMAGE if "runtime" in args[-1] or args[-1] == RUNTIME_IMAGE else APP_IMAGE
        if args[:2] == ("docker", "run"):
            return self.target_head if "-c" in args else "{}"
        if args[:2] == ("docker", "inspect"):
            expression, service = args[3], args[4].removesuffix("-id")
            if expression == "{{.Image}}":
                if service in self.bad_images:
                    return "sha256:" + "c" * 64
                return RUNTIME_IMAGE if service == "nautilus" else APP_IMAGE
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
        if service == "nautilus":
            if self.runtime_http_down:
                raise OSError("offline")
            return '{"entries_armed":false,"entry_block_reason":"paused"}'
        if path == "/":
            return "<!doctype html><html></html>"
        return json.dumps({"image_digest": self.ready_image, "runtime_manifest_sha": self.ready_manifest})

    def commands(self) -> list[tuple[str, ...]]:
        return [call[len(self.prefix) :] for call in self.calls if call[: len(self.prefix)] == tuple(self.prefix)]


@pytest.fixture
def deployment(tmp_path: Path) -> FakeDeployment:
    return FakeDeployment(tmp_path)


@pytest.mark.parametrize("action", ["up", "deploy-image"])
@pytest.mark.parametrize("execution", [True, False])
def test_application_release_never_moves_execution(deployment: FakeDeployment, action: str, execution: bool) -> None:
    deployment.runtime_exists = deployment.runtime_running = execution
    deployment.execution_enabled = execution
    deployment.execute(action)
    mutations = [c for c in deployment.commands() if c[0] in {"up", "stop", "rm"}]
    assert mutations
    assert all("nautilus" not in c for c in mutations)
    assert all("gh" not in c and "uv" not in c for c in deployment.calls)


@pytest.mark.parametrize("action", ["up", "deploy-image"])
def test_migration_completion_precedes_app_start(deployment: FakeDeployment, action: str) -> None:
    deployment.execute(action)
    calls = deployment.calls
    stop = next(i for i, c in enumerate(calls) if c[-4:] == ("stop", *APP_SERVICES))
    wait = calls.index(("docker", "wait", "migrate-id"))
    app_up = next(i for i, c in enumerate(calls) if "up" in c and c[-3:] == APP_SERVICES)
    assert stop < wait < app_up
    assert "--no-deps" in calls[app_up]
    assert "--wait" in calls[app_up]
    assert deployment.env["TRACEFOLD_APP_IMAGE"] == APP_IMAGE
    assert deployment.env["TRACEFOLD_IMAGE_DIGEST"] == APP_IMAGE


@pytest.mark.parametrize("action", ["up", "deploy-image", "db-migrate"])
def test_failed_migration_never_starts_readers(deployment: FakeDeployment, action: str) -> None:
    deployment.migration_exit = "17"
    with pytest.raises(DeploymentError, match="migrate exited 17"):
        deployment.execute(action)
    assert not [c for c in deployment.commands() if c[0] == "up" and "serve" in c]


@pytest.mark.parametrize("action", ["up", "deploy-image", "runtime-up"])
def test_invalid_config_is_rejected_before_stop(deployment: FakeDeployment, action: str) -> None:
    deployment.bad_config = True
    with pytest.raises(DeploymentError, match="invalid config"):
        deployment.execute(action)
    assert not [c for c in deployment.commands() if c[0] == "stop"]


@pytest.mark.parametrize("action", ["up", "db-migrate"])
def test_migration_never_changes_schema_under_execution(deployment: FakeDeployment, action: str) -> None:
    deployment.runtime_exists = deployment.runtime_running = True
    deployment.target_head = "new-head"
    with pytest.raises(DeploymentError, match="maintenance window"):
        deployment.execute(action)
    assert not [c for c in deployment.commands() if c[0] in {"stop", "up"}]


def test_rollback_needs_matching_image_database_not_current_git_head(deployment: FakeDeployment) -> None:
    deployment.execute("deploy-image")
    assert not [c for c in deployment.calls if c[0] in {"git", "gh", "uv"}]
    assert not [c for c in deployment.commands() if c[0] == "build"]
    deployment.db_head = "incompatible"
    deployment.calls.clear()
    with pytest.raises(DeploymentError, match="Alembic heads differ"):
        deployment.execute("deploy-image")
    assert not [c for c in deployment.commands() if c[0] == "stop"]


@pytest.mark.parametrize("image", ["latest", "", "sha256:abc"])
def test_rollback_requires_full_image_id(deployment: FakeDeployment, image: str) -> None:
    deployment.env["IMAGE_ID"] = image
    with pytest.raises(DeploymentError, match="complete local ID"):
        deployment.execute("deploy-image")
    assert not deployment.commands()


@pytest.mark.parametrize("service", ["rabbitmq-policy", "migrate", *APP_SERVICES])
def test_each_application_role_must_use_requested_image(deployment: FakeDeployment, service: str) -> None:
    deployment.bad_images.add(service)
    with pytest.raises(DeploymentError, match="immutable image"):
        deployment.execute("deploy-image")


@pytest.mark.parametrize("field", ["ready_image", "ready_manifest"])
def test_ready_identity_is_proven_after_start(deployment: FakeDeployment, field: str) -> None:
    setattr(deployment, field, "wrong")
    with pytest.raises(DeploymentError, match=r"image_digest|manifest"):
        deployment.execute("up")


def test_runtime_restart_pins_actual_image_without_build_git_or_migrate(deployment: FakeDeployment) -> None:
    deployment.runtime_exists = deployment.runtime_running = deployment.execution_enabled = True
    deployment.execute("runtime-restart")
    assert deployment.env["TRACEFOLD_RUNTIME_IMAGE"] == RUNTIME_IMAGE
    assert not [c for c in deployment.calls if c[0] in {"git", "gh", "uv"}]
    changes = [c for c in deployment.commands() if c[0] in {"up", "stop"}]
    assert changes[0] == ("stop", "nautilus")  # Compose owns the 90-second grace period.
    assert changes[1][-1] == "nautilus" and "--no-deps" in changes[1]
    assert not [c for c in deployment.commands() if c[0] == "build" or c[-1] == "migrate"]


def test_runtime_schema_mismatch_does_not_stop_owner(deployment: FakeDeployment) -> None:
    deployment.execution_enabled = True
    deployment.target_head = "different"
    with pytest.raises(DeploymentError, match="Alembic heads differ"):
        deployment.execute("runtime-up")
    assert not [c for c in deployment.commands() if c[0] == "stop"]


def test_disabled_execution_is_not_implicitly_activated(deployment: FakeDeployment) -> None:
    with pytest.raises(DeploymentError, match=r"execution\.enabled is false"):
        deployment.execute("runtime-up")
    assert not [c for c in deployment.commands() if c[0] in {"stop", "up"}]


def test_runtime_paused_and_unreachable_ready_payload_are_not_restart_signals(deployment: FakeDeployment) -> None:
    deployment.runtime_exists = deployment.runtime_running = deployment.execution_enabled = True
    deployment.execute("runtime-status")
    deployment.runtime_http_down = True
    deployment.execute("runtime-status")
    assert not [c for c in deployment.commands() if c[0] in {"up", "stop", "run"}]


def test_down_stops_execution_before_removing_stack_and_never_volumes(deployment: FakeDeployment) -> None:
    deployment.execute("down")
    assert deployment.commands() == [("stop", "nautilus"), ("rm", "-f", "nautilus"), ("down",)]


def test_logs_include_analysis_and_one_shot_policy(deployment: FakeDeployment) -> None:
    deployment.execute("logs")
    assert {"analysis", "rabbitmq-policy"} <= set(deployment.commands()[-1])


def test_status_reports_execution_even_when_application_failed(
    deployment: FakeDeployment, capsys: pytest.CaptureFixture
) -> None:
    deployment.bad_health.add("workers")
    with pytest.raises(DeploymentError, match="workers"):
        deployment.execute("status")
    assert "execution runtime: disabled" in capsys.readouterr().out


@pytest.mark.parametrize("service", ["migrate", "rabbitmq-policy"])
def test_status_rejects_a_still_running_one_shot(deployment: FakeDeployment, service: str) -> None:
    deployment.states[service] = "running"
    with pytest.raises(DeploymentError, match=f"{service}: state=running"):
        deployment.execute("status-app")


def test_db_migrate_leaves_application_stopped(deployment: FakeDeployment) -> None:
    deployment.execute("db-migrate")
    assert ("stop", *APP_SERVICES) in deployment.commands()
    assert not [c for c in deployment.commands() if c[0] == "up" and "serve" in c]


@pytest.mark.parametrize("action", sorted(MUTATIONS))
def test_all_mutations_share_lock_including_stops(deployment: FakeDeployment, action: str) -> None:
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


def test_every_make_lifecycle_target_is_phony_and_dry_run_is_offline(tmp_path: Path) -> None:
    from scripts.deploy import ACTIONS

    for action in ACTIONS:
        result = subprocess.run(["make", "-n", action], cwd=ROOT, check=True, capture_output=True, text=True)
        assert result.stdout.strip() == f"python3 scripts/deploy.py {action}"
    makefile = (ROOT / "Makefile").read_text()
    declared = " ".join(line for line in makefile.splitlines() if line.startswith(".PHONY:"))
    assert set(ACTIONS) <= set(declared.split())
    assert "$(shell" not in makefile


def test_context_does_not_accept_ambient_topology_or_require_auth(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME=isolated\n")
    deployment = Deployment(tmp_path, {"COMPOSE_FILE": "other.yml", "COMPOSE_PROFILES": "wrong"})
    assert "COMPOSE_FILE" not in deployment.env
    assert "COMPOSE_PROFILES" not in deployment.env
    assert str(tmp_path / ".env") in deployment.prefix
    assert str(tmp_path / "compose.yaml") in deployment.prefix
