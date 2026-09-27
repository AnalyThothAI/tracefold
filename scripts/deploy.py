"""Compose lifecycle orchestration using Python 3.10+ and the standard library.

Make exposes commands, Compose owns topology, and this module owns sequencing.
Nothing here grants trading authority or downgrades a database.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
APP_SERVICES = ("serve", "workers", "analysis")
APP_IMAGE_SERVICES = ("rabbitmq-policy", "migrate", *APP_SERVICES)
ACTIONS = (
    "init",
    "config",
    "build",
    "up",
    "deploy-image",
    "status",
    "status-app",
    "logs",
    "down",
    "db-migrate",
    "db-health",
    "runtime-build",
    "runtime-up",
    "runtime-restart",
    "runtime-down",
    "runtime-status",
    "runtime-logs",
    "serve-shell",
    "workers-shell",
    "topology",
)
MUTATIONS = frozenset(
    {
        "init",
        "build",
        "up",
        "deploy-image",
        "down",
        "db-migrate",
        "runtime-build",
        "runtime-up",
        "runtime-restart",
        "runtime-down",
    }
)
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
HEAD_QUERY = "SELECT version_num FROM alembic_version LIMIT 1"
HEAD_CODE = (
    "from tracefold.platform.postgres.migrations import latest_migration_version; print(latest_migration_version())"
)
READ_ONLY_SQL = (
    "PGPASSWORD=$(cat /run/secrets/postgres_database_password); "
    'PGOPTIONS="-c default_transaction_read_only=on"; export PGPASSWORD PGOPTIONS; '
    'exec psql -X -A -t -v ON_ERROR_STOP=1 -U tracefold -d tracefold -c "$1"'
)


class DeploymentError(RuntimeError):
    """An actionable lifecycle failure without credential values."""


@contextmanager
def deployment_lock(directory: Path, project: str) -> Iterator[None]:
    """One OS-released lock per user/project, shared by independent clones."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / f"{project}.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError(f"deployment is already in progress for {project}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Deployment:
    def __init__(self, root: Path = ROOT, environ: dict[str, str] | None = None) -> None:
        self.root = root.resolve()
        self.env = dict(os.environ if environ is None else environ)
        for key in (
            "COMPOSE_FILE",
            "COMPOSE_ENV_FILES",
            "COMPOSE_PROFILES",
            "COMPOSE_PATH_SEPARATOR",
            "COMPOSE_DISABLE_ENV_FILE",
            "COMPOSE_REMOVE_ORPHANS",
        ):
            self.env.pop(key, None)
        env_file = self.root / ".env"
        self.prefix = [
            "docker",
            "compose",
            "--project-directory",
            str(self.root),
            "--env-file",
            str(env_file) if env_file.is_file() else os.devnull,
            "-f",
            str(self.root / "compose.yaml"),
            "--profile",
            "execution",
        ]
        self._model: dict[str, Any] | None = None
        self.wait_seconds = str(int(self.env.get("TRACEFOLD_COMPOSE_WAIT_SECONDS") or "300"))

    def run(self, *args: str, capture: bool = False, timeout: float | None = None) -> str:
        result = subprocess.run(
            args,
            cwd=self.root,
            env=self.env,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            timeout=timeout,
        )
        return result.stdout.strip() if capture else ""

    def compose(self, *args: str, capture: bool = False) -> str:
        return self.run(*self.prefix, *args, capture=capture)

    @property
    def model(self) -> dict[str, Any]:
        if self._model is None:
            # Never print this document: Compose environments can contain passwords.
            self._model = json.loads(self.compose("config", "--format", "json", capture=True))
        return self._model

    @property
    def project(self) -> str:
        name = str(self.model["name"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise DeploymentError("invalid Compose project name")
        return name

    @property
    def home(self) -> Path:
        for mount in self.model["services"]["migrate"]["volumes"]:
            if mount["target"] == "/root/.tracefold/config.yaml":
                return Path(mount["source"]).parent
        raise DeploymentError("Compose is missing the operator config mount")

    def container(self, service: str, *, all_states: bool = True) -> str:
        options = ("--all",) if all_states else ()
        return self.compose("ps", *options, "-q", service, capture=True)

    def inspect(self, container: str, expression: str) -> str:
        return self.run("docker", "inspect", "--format", expression, container, capture=True)

    def image_id(self, image: str) -> str:
        resolved = self.run("docker", "image", "inspect", "--format", "{{.Id}}", image, capture=True)
        if not IMAGE_ID.fullmatch(resolved):
            raise DeploymentError("Docker did not return a complete local image ID")
        return resolved

    def revision(self) -> str:
        revision = self.run("git", "rev-parse", "--verify", "HEAD", capture=True)
        changed = self.run("git", "status", "--porcelain", "--untracked-files=normal", capture=True)
        return revision + ("-dirty" if changed else "")

    def build(self, *, runtime: bool = False) -> str:
        revision = self.revision()
        image = f"{self.project}-runtime:{revision}" if runtime else f"{self.project}-app:local"
        self.env["TRACEFOLD_BUILD_REVISION"] = revision
        self.env["TRACEFOLD_RUNTIME_IMAGE" if runtime else "TRACEFOLD_APP_IMAGE"] = image
        self.compose("build", "nautilus" if runtime else "migrate")
        resolved = self.image_id(image)
        print(f"Built {image} ({resolved})", flush=True)
        return resolved

    def existing_app_image(self) -> str:
        for service in APP_SERVICES:
            container = self.container(service)
            if container:
                return self.inspect(container, "{{.Image}}")
        image = self.compose("config", "--images", "migrate", capture=True).splitlines()[-1]
        return self.image_id(image)

    def select_app_image(self, image: str) -> None:
        self.env["TRACEFOLD_APP_IMAGE"] = image
        self.env["TRACEFOLD_IMAGE_DIGEST"] = image

    def initialize(self, image: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.run(
            "docker",
            "run",
            "--rm",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "HOME=/operator",
            "-e",
            "TRACEFOLD_HOME=/operator/.tracefold",
            "--mount",
            f"type=bind,source={self.home},target=/operator/.tracefold",
            "--entrypoint",
            "tracefold",
            image,
            "init",
            capture=True,
        )
        print(f"Operator configuration: {self.home / 'config.yaml'} (existing contents preserved)", flush=True)

    def app_command(self, *args: str) -> dict[str, Any]:
        result = self.compose(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "tracefold",
            "migrate",
            *args,
            capture=True,
        )
        document = json.loads(result)
        if document.get("ok") is not True:
            raise DeploymentError(f"application command failed: {' '.join(args)}")
        return document["data"]

    def database_head(self) -> str:
        return self.compose(
            "exec",
            "-T",
            "postgres",
            "sh",
            "-eu",
            "-c",
            READ_ONLY_SQL,
            "sh",
            HEAD_QUERY,
            capture=True,
        )

    def image_head(self, image: str) -> str:
        return self.run("docker", "run", "--rm", "--entrypoint", "python", image, "-c", HEAD_CODE, capture=True)

    def require_migration_window(self, image: str) -> None:
        if self.container("nautilus", all_states=False) and self.database_head() != self.image_head(image):
            raise DeploymentError("schema change requires a maintenance window: stop the execution runtime first")

    def migrate(self) -> None:
        self.compose("up", "-d", "--no-build", "--force-recreate", "rabbitmq-policy", "migrate")
        container = self.container("migrate")
        if not container:
            raise DeploymentError("migrate container is missing; application roles were not started")
        code = self.run("docker", "wait", container, capture=True)
        if code != "0":
            self.compose("logs", "--no-color", "--tail=50", "migrate", "rabbitmq-policy")
            raise DeploymentError(f"migrate exited {code}; serve, workers and analysis were not started")

    def apply(self, image: str, *, expected_manifest: str | None = None) -> None:
        self.select_app_image(image)
        self.compose("stop", *APP_SERVICES)
        self.migrate()
        self.compose(
            "up",
            "-d",
            "--no-build",
            "--force-recreate",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            self.wait_seconds,
            *APP_SERVICES,
        )
        for service in APP_IMAGE_SERVICES:
            container = self.container(service)
            if not container or self.inspect(container, "{{.Image}}") != image:
                raise DeploymentError(f"{service} is not running the requested immutable image")
        self.status_app()
        ready = json.loads(self.http("workers", "/readyz"))
        if ready.get("image_digest") != image:
            raise DeploymentError("Workers readiness image_digest does not match the deployed image")
        if expected_manifest is not None and ready.get("runtime_manifest_sha") != expected_manifest:
            raise DeploymentError("Workers runtime manifest does not equal the configured target")

    def up(self) -> None:
        image = self.build()
        self.initialize(image)
        self.select_app_image(image)
        self.app_command("config")
        self.require_migration_window(image)
        manifest = self.app_command("runtime-manifest")["runtime_manifest_sha"]
        self.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", self.wait_seconds, "postgres")
        self.apply(image, expected_manifest=manifest)
        print(f"Tracefold ready at {self.service_url('serve')}", flush=True)

    def deploy_image(self) -> None:
        image = self.env.get("IMAGE_ID", "")
        if not IMAGE_ID.fullmatch(image) or self.image_id(image) != image:
            raise DeploymentError("Pass a complete local ID: make deploy-image IMAGE_ID=sha256:<64 hex>")
        self.select_app_image(image)
        self.app_command("config")
        head = self.database_head()
        if not head or self.image_head(image) != head:
            raise DeploymentError("image and database Alembic heads differ; no services were stopped")
        self.apply(image)
        print(f"Deployed exact local image {image}", flush=True)

    def service_url(self, service: str) -> str:
        variable = {"serve": "API", "workers": "WORKERS", "nautilus": "NAUTILUS"}[service]
        override = self.env.get(f"TRACEFOLD_{variable}_URL")
        if override:
            return override.rstrip("/")
        binding = self.model["services"][service]["ports"][0]
        host = binding.get("host_ip", "127.0.0.1")
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)  # noqa: S104 -- client URL, not a bind
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{binding['published']}"

    def http(self, service: str, path: str) -> str:
        # Local readiness must not leave this host through an ambient corporate proxy.
        opener = build_opener(ProxyHandler({}))
        with opener.open(self.service_url(service) + path, timeout=5) as response:
            return response.read().decode("utf-8")

    def check_container(self, service: str, *, completed: bool = False) -> None:
        container = self.container(service)
        if not container:
            raise DeploymentError(f"{service}: missing")
        state = self.inspect(container, "{{.State.Status}}")
        if completed:
            code = self.inspect(container, "{{.State.ExitCode}}")
            if state != "exited" or code != "0":
                raise DeploymentError(f"{service}: state={state} exit_code={code}")
        else:
            health = self.inspect(container, "{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}")
            if state != "running" or health != "healthy":
                raise DeploymentError(f"{service}: state={state} health={health}")

    def status_app(self) -> None:
        self.compose("ps", "--all")
        failures = []
        for service in ("postgres", "rabbitmq", *APP_SERVICES, "rabbitmq-policy", "migrate"):
            try:
                self.check_container(service, completed=service in {"rabbitmq-policy", "migrate"})
            except DeploymentError as exc:
                failures.append(str(exc))
        for service, path in (("serve", "/readyz"), ("workers", "/readyz"), ("serve", "/")):
            try:
                body = self.http(service, path)
                if path == "/" and not re.search(r"<(!doctype html|html)", body, re.I):
                    raise DeploymentError("console HTML missing")
            except (OSError, DeploymentError) as exc:
                failures.append(f"{service}{path}: {type(exc).__name__}")
        if failures:
            raise DeploymentError("; ".join(failures))
        print("Application containers, one-shot jobs, readiness and console are healthy", flush=True)

    def read_config(self) -> dict[str, Any]:
        for service in (*APP_SERVICES, "nautilus"):
            if self.container(service, all_states=False):
                payload = self.compose("exec", "-T", service, "tracefold", "config", capture=True)
                return json.loads(payload)["data"]
        self.select_app_image(self.existing_app_image())
        return self.app_command("config")

    def runtime_up(self, image: str | None = None) -> None:
        image = image or self.env.get("RUNTIME_IMAGE") or f"{self.project}-runtime:{self.revision()}"
        resolved = self.image_id(image)
        self.env["TRACEFOLD_RUNTIME_IMAGE"] = resolved
        self.env["TRACEFOLD_IMAGE_DIGEST"] = resolved
        configured = json.loads(
            self.compose(
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "tracefold",
                "nautilus",
                "config",
                capture=True,
            )
        )["data"]
        if configured["trading"]["execution"]["enabled"] is not True:
            raise DeploymentError("trading.execution.enabled is false; no execution runtime was started")
        database_head = self.database_head()
        if not database_head or self.image_head(resolved) != database_head:
            raise DeploymentError("runtime image and database Alembic heads differ; runtime was not stopped")
        previous = self.container("nautilus")
        if previous:
            print(f"Previous runtime image: {self.inspect(previous, '{{.Image}}')}", flush=True)
        self.compose("stop", "nautilus")
        self.compose(
            "up",
            "-d",
            "--no-build",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            self.wait_seconds,
            "nautilus",
        )
        if self.inspect(self.container("nautilus"), "{{.Image}}") != resolved:
            raise DeploymentError("runtime container does not use the requested immutable image")
        self.runtime_status()

    def runtime_down(self) -> None:
        self.compose("stop", "nautilus")
        self.compose("rm", "-f", "nautilus")

    def runtime_status(self) -> None:
        enabled = self.read_config()["trading"]["execution"]["enabled"]
        container = self.container("nautilus", all_states=False)
        if not enabled:
            if container:
                raise DeploymentError("execution runtime: disabled but nautilus is still running")
            print("execution runtime: disabled (operator selected)")
            return
        self.check_container("nautilus")
        print(f"Execution runtime image: {self.inspect(container, '{{.Image}}')}")
        try:
            print(f"Execution runtime readyz: {self.http('nautilus', '/readyz')}")
        except OSError:
            print("Execution runtime readyz: unreachable; inspect runtime-logs", file=sys.stderr)
        # Entries paused or blocked is not a reason to restart the exposure owner.

    def dispatch(self, action: str) -> None:
        if action == "init":
            self.initialize(self.build())
        elif action == "build":
            self.build()
        elif action == "up":
            self.up()
        elif action == "deploy-image":
            self.deploy_image()
        elif action == "config":
            print(json.dumps({"ok": True, "data": self.read_config()}, ensure_ascii=False, indent=2))
        elif action == "topology":
            print(
                json.dumps(
                    {
                        "project": self.project,
                        "config": str(self.home / "config.yaml"),
                        "compose_file": str(self.root / "compose.yaml"),
                        "services": sorted(self.model["services"]),
                        "urls": {service: self.service_url(service) for service in ("serve", "workers", "nautilus")},
                    },
                    indent=2,
                )
            )
        elif action == "status-app":
            self.status_app()
        elif action == "status":
            failures = []
            for report in (self.status_app, self.runtime_status):
                try:
                    report()
                except (DeploymentError, subprocess.CalledProcessError, OSError) as exc:
                    failures.append(str(exc))
            if failures:
                raise DeploymentError("; ".join(failures))
        elif action in {"logs", "runtime-logs"}:
            services = ("nautilus",) if action == "runtime-logs" else tuple(self.model["services"])
            self.compose("logs", "-f", "--tail=100", *services)
        elif action == "down":
            self.runtime_down()
            self.compose("down")  # Never remove named volumes.
        elif action == "db-migrate":
            image = self.build()
            self.initialize(image)
            self.select_app_image(image)
            self.app_command("config")
            self.require_migration_window(image)
            self.compose("stop", *APP_SERVICES)
            self.migrate()
            print("Migration completed. Application roles remain stopped; start them with make up.")
        elif action == "db-health":
            self.compose("exec", "-T", "workers", "tracefold", "db", "health")
        elif action == "runtime-build":
            self.build(runtime=True)
        elif action == "runtime-up":
            self.runtime_up()
        elif action == "runtime-restart":
            container = self.container("nautilus")
            if not container:
                raise DeploymentError("no execution runtime container to restart; use make runtime-up")
            self.runtime_up(self.inspect(container, "{{.Image}}"))
        elif action == "runtime-down":
            self.runtime_down()
        elif action == "runtime-status":
            self.runtime_status()
        elif action in {"serve-shell", "workers-shell"}:
            self.compose("exec", action.removesuffix("-shell"), "/bin/sh")
        else:
            raise DeploymentError(f"unknown action: {action}")

    def execute(self, action: str) -> None:
        if action in MUTATIONS:
            directory = Path(self.env.get("HOME", str(Path.home()))) / ".cache" / "tracefold" / "deploy"
            with deployment_lock(directory, self.project):
                self.dispatch(action)
        else:
            self.dispatch(action)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    args = parser.parse_args(argv)
    try:
        Deployment().execute(args.action)
    except (DeploymentError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        print(f"Deployment failed: {exc}. Inspect make topology and make logs.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
