"""The execution runtime has its own deployment lifecycle (#537 PR-2).

One image and one `make up` used to serve five services, so every News, Serve or Workers merge
stopped and force-recreated the one process that owns a live Binance account: 26 restarts in
56.7 hours, and three of eleven Signals lost to `expired` / `account_stale`. These contracts pin
the separation that replaced it — what a deploy may name, what the runtime depends on, how long it
is given to shut down, what the runtime movers are allowed to do, and where the Compose bindings
that decide whether a container gets recreated are declared.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.deploy

ROOT = Path(__file__).resolve().parents[2]
_LINE_CONTINUATION = re.compile(r"\\\n\s*")
_RUNTIME_TARGETS = ("runtime-build", "runtime-up", "runtime-restart", "runtime-down", "runtime-logs")
_COMPOSE_BINDING = re.compile(r"\$\{(TRACEFOLD_[A-Z_]*(?:HOST|PORT)):-([^}]*)\}")
_MAKEFILE_DEFAULT = re.compile(r"^(TRACEFOLD_[A-Z_]+) \?= (.*)$", re.MULTILINE)
# `stop_grace_period` is the SIGKILL deadline. Anything longer than it turns the ordinary stop into
# a kill, which skips `singleton.release()` and strands the account-slot advisory lock.
_STOP_GRACE_SECONDS = 90


def _dry_run(target: str) -> str:
    recipe = subprocess.run(
        ["make", "--dry-run", target],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    ).stdout
    return _LINE_CONTINUATION.sub(" ", recipe)


def _statements(recipe: str) -> list[str]:
    return [statement.strip() for line in recipe.splitlines() for statement in line.split(";")]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))


def _makefile() -> str:
    return (ROOT / "Makefile").read_text(encoding="utf-8")


@pytest.mark.parametrize("target", ("_up-locked", "_deploy-image-locked"))
def test_no_deployment_entry_starts_or_stops_the_execution_runtime(target: str) -> None:
    for statement in _statements(_dry_run(target)):
        if re.match(r"^docker compose (?:-\S+\s+)*(?:up|stop)\b", statement):
            assert "nautilus" not in statement, statement


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

    stops = [
        statement
        for statement in _makefile().split("\n")
        if "docker compose stop" in statement and "nautilus" in statement
    ]
    assert stops, "the Makefile must still stop the runtime somewhere"
    for statement in stops:
        assert f"-t {_STOP_GRACE_SECONDS}" in statement, statement


def test_make_down_refuses_while_the_execution_runtime_still_exists() -> None:
    """`docker compose down` removes the project's containers and network, runtime included."""

    recipe = _dry_run("down")
    refusal = recipe.index("execution runtime is running and owns live exposure")
    teardown = recipe.index("docker compose down")

    assert refusal < teardown
    assert "runtime-down" in recipe


@pytest.mark.parametrize("target", ("runtime-up", "runtime-restart", "runtime-down"))
def test_restoring_the_runtime_needs_no_github_no_build_and_no_migration(target: str) -> None:
    """Bringing exposure back under management must not depend on the outside world.

    A rollback deliberately runs an image older than `origin/main`, an incident is exactly when
    github.com or an authenticated `gh` may be unavailable, and re-running migrations under the
    process that owns the account is what `make up`'s own guard exists to prevent.
    """

    recipe = _dry_run(target)

    assert "require_main_ci" not in recipe
    assert not re.search(r"\bgh ", recipe)
    assert not re.search(r"docker (?:compose )?build\b", recipe)
    assert "db migrate" not in recipe


def test_building_the_runtime_image_still_takes_the_exact_main_gate() -> None:
    public = _dry_run("runtime-build")
    private = _dry_run("_runtime-build-locked")

    assert "scripts/with_deployment_lock.py" in public
    assert "with_deployment_lock.py --assert-held" in private
    assert "scripts/require_main_ci.py" in private
    assert "docker compose build nautilus" in private
    assert "tracefold-runtime:$revision" in private


def test_every_published_compose_binding_is_declared_once_in_the_makefile() -> None:
    """The trap this closes: six of the twelve were only ever operator shell state.

    `compose.yaml` renders `${TRACEFOLD_POSTGRES_PORT:-56532}` into a published port. An operator
    who exported it for one command and not the next changed the rendered service definition, and
    Compose then recreated the container holding the database.
    """

    bindings = dict(_COMPOSE_BINDING.findall((ROOT / "compose.yaml").read_text(encoding="utf-8")))
    declared = dict(_MAKEFILE_DEFAULT.findall(_makefile()))
    exported = _makefile()

    assert len(bindings) == 12, sorted(bindings)
    for name, default in bindings.items():
        assert declared.get(name) == default, name
        assert re.search(rf"^export .*\b{name}\b", exported, re.MULTILINE), name
    assert not (ROOT / ".env").exists()


def test_the_project_interpreter_is_pinned_to_the_image_interpreter() -> None:
    recipe = _dry_run("preflight")

    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.13"
    assert "sys.version_info[:2] == (3, 13)" in recipe
    # Deploying is not the place to gate on a venue's clock: Binance rejects an out-of-`recvWindow`
    # request itself, with its own error (#537 PR-6).
    assert "fapi.binance.com" not in recipe


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
