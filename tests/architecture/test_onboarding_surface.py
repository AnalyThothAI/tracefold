from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEST_IMAGE_ID = "sha256:" + "a" * 64


def _deploy_image_sandbox(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(ROOT / "Makefile", repo / "Makefile")
    shutil.copy(ROOT / "compose.yaml", repo / "compose.yaml")
    (repo / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "with_deployment_lock.py", repo / "scripts" / "with_deployment_lock.py")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tracefold Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "Makefile", "compose.yaml"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", head], cwd=repo, check=True)

    external_activity = tmp_path / "external-activity"
    services_stopped = tmp_path / "services-stopped"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu
: > "$TRACEFOLD_TEST_EXTERNAL_ACTIVITY"
if [ "$1" = "info" ]; then exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then printf '%s\\n' "$TRACEFOLD_TEST_IMAGE"; exit 0; fi
if [ "$1" = "run" ]; then printf '%s\\n' 20260822_0295; exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "config" ]; then
  printf '%s\\n' 'postgres:18-bookworm@sha256:pinned' "$TRACEFOLD_APP_IMAGE"
  exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "run" ]; then exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "exec" ]; then
  case "$*" in
    *news_learning_artifacts*) printf '%s\\n' "$TRACEFOLD_TEST_RECEIPT" ;;
    *) printf '%s\\n' "$TRACEFOLD_TEST_DB_HEAD" ;;
  esac
  exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "stop" ]; then
  : > "$TRACEFOLD_TEST_SERVICES_STOPPED"
  if [ -n "${TRACEFOLD_TEST_DEPLOY_BLOCK:-}" ]; then
    : > "${TRACEFOLD_TEST_DEPLOY_BLOCK}.entered"
    while [ ! -e "${TRACEFOLD_TEST_DEPLOY_BLOCK}.release" ]; do sleep 0.01; done
  fi
fi
if [ "$1" = "compose" ] && [ "$2" = "ps" ]; then
  service=''
  for argument do service="$argument"; done
  case "$service" in
    postgres|rabbitmq|migrate|serve|workers) printf '%s\\n' "${service}-id" ;;
  esac
  exit 0
fi
if [ "$1" = "inspect" ]; then
  format="$3"
  container_id="$4"
  case "$format" in
    *State.Status*)
      if [ "$container_id" = "migrate-id" ]; then printf '%s\\n' exited; else printf '%s\\n' running; fi
      ;;
    *State.ExitCode*) printf '%s\\n' 0 ;;
    *State.Health*) printf '%s\\n' healthy ;;
    *Image*)
      case "$container_id" in
        migrate-id) printf '%s\\n' "$TRACEFOLD_TEST_MIGRATE_IMAGE" ;;
        serve-id) printf '%s\\n' "$TRACEFOLD_TEST_SERVE_IMAGE" ;;
        workers-id) printf '%s\\n' "$TRACEFOLD_TEST_WORKERS_IMAGE" ;;
      esac
      ;;
  esac
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        """#!/bin/sh
set -eu
: > "$TRACEFOLD_TEST_EXTERNAL_ACTIVITY"
if [ "$1" = "run" ] && [ "$2" = "python" ] && [ "$3" = "scripts/with_deployment_lock.py" ]; then
  shift 3
  exec python3 scripts/with_deployment_lock.py "$@"
fi
case "$*" in
  *image_digest*) printf '%s\\n' "$TRACEFOLD_TEST_READY_IMAGE" ;;
  *) printf '%s\\n' 20260822_0295 ;;
esac
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        """#!/bin/sh
set -eu
: > "$TRACEFOLD_TEST_EXTERNAL_ACTIVITY"
url=''
for argument do url="$argument"; done
case "$url" in
  */readyz) printf '{"ok":true,"image_digest":"%s"}\\n' "$TRACEFOLD_TEST_READY_IMAGE" ;;
  */) printf '<html></html>\\n' ;;
esac
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TRACEFOLD_TEST_EXTERNAL_ACTIVITY": str(external_activity),
        "TRACEFOLD_TEST_SERVICES_STOPPED": str(services_stopped),
        "TRACEFOLD_TEST_DB_HEAD": "20260822_0295",
        "TRACEFOLD_TEST_IMAGE": TEST_IMAGE_ID,
        "TRACEFOLD_TEST_MIGRATE_IMAGE": TEST_IMAGE_ID,
        "TRACEFOLD_TEST_READY_IMAGE": TEST_IMAGE_ID,
        "TRACEFOLD_TEST_RECEIPT": "ok",
        "TRACEFOLD_TEST_SERVE_IMAGE": TEST_IMAGE_ID,
        "TRACEFOLD_TEST_WORKERS_IMAGE": TEST_IMAGE_ID,
    }
    return repo, external_activity, services_stopped, env


def test_one_command_onboarding_has_one_public_lifecycle() -> None:
    result = subprocess.run(
        ["make", "help"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    targets = {line.split(maxsplit=1)[0] for line in result.stdout.splitlines() if line}

    assert result.returncode == 0, result.stderr
    assert {"up", "status", "logs", "down"} <= targets
    assert {
        "docker-up",
        "docker-status",
        "docker-logs",
        "docker-down",
    }.isdisjoint(targets)


def test_status_rejects_a_still_running_migration(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu
if [ "$1" = "info" ]; then exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "ps" ]; then
  service=""
  for argument do service="$argument"; done
  if [ "$service" = "migrate" ]; then printf '%s\\n' migrate-id; else printf '%s\\n' "${service}-id"; fi
  exit 0
fi
if [ "$1" = "inspect" ]; then
  format="$3"
  case "$format" in
    *State.Status*) printf '%s\\n' running ;;
    *State.ExitCode*) printf '%s\\n' 0 ;;
    *State.Health*) printf '%s\\n' healthy ;;
  esac
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        """#!/bin/sh
url=''
for argument do url="$argument"; done
case "$url" in */) printf '<html></html>' ;; esac
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o700)
    fake_uv = bin_dir / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_uv.chmod(0o700)

    result = subprocess.run(
        ["make", "status"],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "migrate: state=running exit_code=0" in result.stderr


def test_deploy_image_dry_run_never_invokes_external_tools(tmp_path: Path) -> None:
    repo, external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)

    result = subprocess.run(
        ["make", "-n", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not external_activity.exists()


def test_up_dry_run_never_invokes_external_tools(tmp_path: Path) -> None:
    repo, external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)

    result = subprocess.run(
        ["make", "-n", "up"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not external_activity.exists()


def test_deployment_lock_is_released_by_the_os_when_the_owner_crashes(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    gate = tmp_path / "crashing-owner"
    owner = subprocess.Popen(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env={**env, "TRACEFOLD_TEST_DEPLOY_BLOCK": str(gate)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not gate.with_suffix(".entered").exists() and time.monotonic() < deadline:
            if owner.poll() is not None:
                stdout, stderr = owner.communicate()
                raise AssertionError(f"lock owner exited early: stdout={stdout!r} stderr={stderr!r}")
            time.sleep(0.01)
        assert gate.with_suffix(".entered").exists(), "lock owner never reached deployment"
        os.killpg(owner.pid, signal.SIGKILL)
        assert owner.wait(timeout=2.0) != 0

        successor = subprocess.run(
            ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
            cwd=repo,
            env=env,
            capture_output=True,
            check=False,
            text=True,
            timeout=5.0,
        )
        assert successor.returncode == 0, successor.stderr
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=2.0)


def test_up_and_deploy_image_share_one_cross_process_deployment_lock(tmp_path: Path) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    gate = tmp_path / "deploy-gate"
    first = subprocess.Popen(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env={**env, "TRACEFOLD_TEST_DEPLOY_BLOCK": str(gate)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not gate.with_suffix(".entered").exists() and time.monotonic() < deadline:
            if first.poll() is not None:
                stdout, stderr = first.communicate()
                raise AssertionError(f"first deployment exited early: stdout={stdout!r} stderr={stderr!r}")
            time.sleep(0.01)
        assert gate.with_suffix(".entered").exists(), "first deployment never reached its mutation boundary"

        second = subprocess.run(
            ["make", "up"],
            cwd=repo,
            env=env,
            capture_output=True,
            check=False,
            text=True,
            timeout=5.0,
        )

        assert second.returncode != 0
        assert "deployment is already in progress" in second.stderr
    finally:
        gate.with_suffix(".release").touch()
        try:
            first_stdout, first_stderr = first.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            first.kill()
            first_stdout, first_stderr = first.communicate(timeout=2.0)

    assert first.returncode == 0, f"stdout={first_stdout!r} stderr={first_stderr!r}"
    assert services_stopped.exists()


def test_deploy_image_rejects_database_head_mismatch_before_stopping_services(tmp_path: Path) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_DB_HEAD"] = "20260823_9999"

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "database Alembic head" in result.stderr
    assert not services_stopped.exists()


def test_deploy_image_rejects_tracked_primary_changes_before_stopping_services(tmp_path: Path) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    (repo / "compose.yaml").write_text("name: changed\n", encoding="utf-8")

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "tracked or staged changes" in result.stderr
    assert not services_stopped.exists()


def test_deploy_image_rejects_staged_primary_changes_before_stopping_services(tmp_path: Path) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    (repo / "compose.yaml").write_text("name: staged-change\n", encoding="utf-8")
    subprocess.run(["git", "add", "compose.yaml"], cwd=repo, check=True)

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "tracked or staged changes" in result.stderr
    assert not services_stopped.exists()


def test_deploy_image_rejects_main_that_is_not_origin_main(tmp_path: Path) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    (repo / "local-only.txt").write_text("ahead\n", encoding="utf-8")
    subprocess.run(["git", "add", "local-only.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "local-only"], cwd=repo, check=True)

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "origin/main" in result.stderr
    assert not services_stopped.exists()


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("compose.override.yaml", "services: {}\n"),
        (
            "src/tracefold/platform/postgres/alembic/versions/untracked_revision.py",
            'revision = "untracked"\n',
        ),
    ],
)
def test_deploy_image_rejects_relevant_untracked_inputs_before_stopping_services(
    tmp_path: Path,
    relative_path: str,
    content: str,
) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "untracked deployment input" in result.stderr
    assert not services_stopped.exists()


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("compose.override.yaml", "services: {}\n"),
        (
            "src/tracefold/platform/postgres/alembic/versions/ignored_revision.py",
            'revision = "ignored"\n',
        ),
    ],
)
def test_deploy_image_rejects_gitignored_deployment_inputs_before_stopping_services(
    tmp_path: Path,
    relative_path: str,
    content: str,
) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    (repo / ".gitignore").write_text(f"/{relative_path}\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore local deployment input"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", head], cwd=repo, check=True)
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "untracked deployment input" in result.stderr
    assert not services_stopped.exists()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("COMPOSE_FILE", "untrusted-compose.yaml"),
        ("COMPOSE_PROJECT_NAME", "not-tracefold"),
        ("COMPOSE_ENV_FILES", "untrusted-compose.env"),
        ("COMPOSE_PROFILES", "untrusted-profile"),
    ],
)
def test_deploy_image_rejects_inherited_compose_stack_variables_before_stopping_services(
    tmp_path: Path,
    variable: str,
    value: str,
) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    env[variable] = value

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "inherited Compose stack variables" in result.stderr
    assert not services_stopped.exists()


def test_deploy_image_rejects_gitignored_dotenv_before_stopping_services(tmp_path: Path) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore dotenv"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", head], cwd=repo, check=True)
    (repo / ".env").write_text("COMPOSE_FILE=untrusted.yaml\n", encoding="utf-8")

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "untracked deployment input" in result.stderr
    assert not services_stopped.exists()


def test_deploy_image_rejects_a_runtime_container_with_the_wrong_image(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_WORKERS_IMAGE"] = "sha256:" + "b" * 64

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "workers container image" in result.stderr
    assert "Tracefold deployed exact local image" not in result.stdout


def test_deploy_image_rejects_workers_ready_identity_mismatch(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_READY_IMAGE"] = "sha256:" + "c" * 64

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "Workers readiness image_digest" in result.stderr
    assert "Tracefold deployed exact local image" not in result.stdout


def test_deploy_image_rejects_missing_exact_active_deployment_receipt(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_RECEIPT"] = "mismatch"

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "active/deployment receipt" in result.stderr
    assert "Tracefold deployed exact local image" not in result.stdout


def test_deploy_image_allows_an_unrelated_untracked_research_notebook(tmp_path: Path) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    research = repo / "docs" / "research"
    research.mkdir(parents=True)
    (research / "trading-agent-72h-event-study.ipynb").write_text("{}\n", encoding="utf-8")

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"Tracefold deployed exact local image {TEST_IMAGE_ID}." in result.stdout
    assert services_stopped.exists()
