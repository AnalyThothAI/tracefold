from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.deploy

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
    trading_control = tmp_path / "trading-control"
    trading_control.write_text("PAUSED\n", encoding="utf-8")
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
if [ "$1" = "run" ]; then printf '%s\\n' 20260824_0303; exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "config" ]; then
  printf '%s\\n' 'postgres:18-bookworm@sha256:pinned' "$TRACEFOLD_APP_IMAGE"
  exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "run" ]; then
  if [ -n "${TRACEFOLD_TEST_ROLE_PROVISION:-}" ]; then printf '%s\\n' "$*" > "$TRACEFOLD_TEST_ROLE_PROVISION"; fi
  case "$*" in
    *"--entrypoint tracefold serve trading status"*)
      printf '{"ok":true,"data":{"active_capability_snapshot_sha256":"%s"}}\\n' "$TRACEFOLD_TEST_ACTIVE_CAPABILITY_SHA"
      ;;
    *"nautilus tracefold nautilus run --bootstrap-zero-claims"*)
      printf '%s\\n' "$*" > "$TRACEFOLD_TEST_CAPABILITY_BOOTSTRAP"
      if [ "$(cat "$TRACEFOLD_TEST_TRADING_CONTROL")" != "PAUSED" ]; then
        printf '%s\\n' nautilus_bootstrap_requires_paused >&2
        exit 1
      fi
      ;;
    *"workers trading refresh-capabilities"*)
      : > "$TRACEFOLD_TEST_CAPABILITY_REFRESH"
      printf '%s\\n' PAUSED > "$TRACEFOLD_TEST_TRADING_CONTROL"
      ;;
    *"--entrypoint tracefold migrate config"*)
      printf '%s' '{"ok":true,"data":{"trading":{"enabled":'
      printf '%s' "$TRACEFOLD_TEST_TRADING_ENABLED"
      printf '%s' ',"nautilus":{"credentials_configured":'
      printf '%s' "$TRACEFOLD_TEST_NAUTILUS_CREDENTIALS_CONFIGURED"
      printf '}}}}\\n'
      ;;
  esac
  exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "exec" ]; then
  case "$*" in
    *news_learning_artifacts*) printf '%s\\n' "$TRACEFOLD_TEST_RECEIPT" ;;
    *nautilus_bootstrap_account_zero_at_ms*) printf '%s\\n' "$TRACEFOLD_TEST_BOOTSTRAP_ACCOUNT_ZERO" ;;
    *to_regclass*) printf '%s\\n' "$TRACEFOLD_TEST_SCHEMA_STATE" ;;
    *alembic_version*trading_cases_state_check*) printf '%s\\n' "$TRACEFOLD_TEST_MIGRATION_STATE" ;;
    *) printf '%s\\n' "$TRACEFOLD_TEST_DB_HEAD" ;;
  esac
  exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "stop" ]; then
  : > "$TRACEFOLD_TEST_SERVICES_STOPPED"
  printf '%s\n' "$*" > "$TRACEFOLD_TEST_STOP_ARGS"
  if [ -n "${TRACEFOLD_TEST_DEPLOY_BLOCK:-}" ]; then
    : > "${TRACEFOLD_TEST_DEPLOY_BLOCK}.entered"
    while [ ! -e "${TRACEFOLD_TEST_DEPLOY_BLOCK}.release" ]; do sleep 0.01; done
  fi
fi
if [ "$1" = "compose" ] && [ "$2" = "up" ]; then
  printf '%s\n' "$*" > "$TRACEFOLD_TEST_UP_ARGS"
  case " $* " in *" nautilus "*) : > "$TRACEFOLD_TEST_NAUTILUS_RECREATED" ;; esac
  exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "ps" ]; then
  if [ "$#" -eq 3 ] && [ "$3" = "-q" ]; then
    printf '%s' "${TRACEFOLD_TEST_RUNNING_CONTAINERS:-}"
    exit 0
  fi
  service=''
  for argument do service="$argument"; done
  case "$service" in
    postgres|rabbitmq|migrate|serve|workers) printf '%s\\n' "${service}-id" ;;
    nautilus)
      if [ -e "$TRACEFOLD_TEST_NAUTILUS_RECREATED" ]; then
        printf '%s\\n' nautilus-id
      elif [ "${TRACEFOLD_TEST_NAUTILUS_PRESENT:-}" = "1" ]; then
        case " $* " in
          *" --all "*) printf '%s\\n' nautilus-id ;;
          *) if [ "${TRACEFOLD_TEST_NAUTILUS_STATUS:-running}" = "running" ]; then printf '%s\\n' nautilus-id; fi ;;
        esac
      fi
      ;;
  esac
  exit 0
fi
if [ "$1" = "inspect" ]; then
  format="$3"
  container_id="$4"
  case "$format" in
    *State.Status*)
      if [ "$container_id" = "migrate-id" ]; then
        printf '%s\\n' exited
      elif [ "$container_id" = "nautilus-id" ] && [ ! -e "$TRACEFOLD_TEST_NAUTILUS_RECREATED" ]; then
        printf '%s\\n' "${TRACEFOLD_TEST_NAUTILUS_STATUS:-running}"
      else
        printf '%s\\n' running
      fi
      ;;
    *State.ExitCode*) printf '%s\\n' 0 ;;
    *State.Health*) printf '%s\\n' healthy ;;
    *Image*)
      case "$container_id" in
        migrate-id) printf '%s\\n' "$TRACEFOLD_TEST_MIGRATE_IMAGE" ;;
        serve-id) printf '%s\\n' "$TRACEFOLD_TEST_SERVE_IMAGE" ;;
        workers-id) printf '%s\\n' "$TRACEFOLD_TEST_WORKERS_IMAGE" ;;
        nautilus-id) printf '%s\\n' "$TRACEFOLD_TEST_NAUTILUS_IMAGE" ;;
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
if [ "$1" = "run" ] && [ "$2" = "tracefold" ] && [ "$3" = "config" ]; then
  printf '%s' '{"ok":true,"data":{"trading":{"enabled":'
  printf '%s' "$TRACEFOLD_TEST_TRADING_ENABLED"
  printf '%s' ',"nautilus":{"credentials_configured":'
  printf '%s' "$TRACEFOLD_TEST_NAUTILUS_CREDENTIALS_CONFIGURED"
  printf '}}}}\\n'
  exit 0
fi
if [ "$1" = "run" ] && [ "$2" = "python" ] && [ "$3" = "-c" ]; then
  case "$4" in
    *credentials_configured*|*trading*enabled*|*active_capability_snapshot_sha256*) shift 2; exec python3 "$@" ;;
  esac
fi
case "$*" in
  *image_digest*) printf '%s\\n' "$TRACEFOLD_TEST_READY_IMAGE" ;;
  *) printf '%s\\n' 20260824_0303 ;;
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
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        '#!/bin/sh\n[ "$1 $2" = "auth status" ]\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TRACEFOLD_TEST_EXTERNAL_ACTIVITY": str(external_activity),
        "TRACEFOLD_TEST_SERVICES_STOPPED": str(services_stopped),
        "TRACEFOLD_TEST_STOP_ARGS": str(tmp_path / "stop-args"),
        "TRACEFOLD_TEST_UP_ARGS": str(tmp_path / "up-args"),
        "TRACEFOLD_TEST_DB_HEAD": "20260824_0303",
        "TRACEFOLD_TEST_SCHEMA_STATE": "existing",
        "TRACEFOLD_TEST_MIGRATION_STATE": "20260829_0329|t|t",
        "TRACEFOLD_TEST_IMAGE": TEST_IMAGE_ID,
        "TRACEFOLD_TEST_MIGRATE_IMAGE": TEST_IMAGE_ID,
        "TRACEFOLD_TEST_READY_IMAGE": TEST_IMAGE_ID,
        "TRACEFOLD_TEST_RECEIPT": "ok",
        "TRACEFOLD_TEST_SERVE_IMAGE": TEST_IMAGE_ID,
        "TRACEFOLD_TEST_WORKERS_IMAGE": TEST_IMAGE_ID,
        "TRACEFOLD_TEST_NAUTILUS_IMAGE": TEST_IMAGE_ID,
        "TRACEFOLD_TEST_NAUTILUS_CREDENTIALS_CONFIGURED": "false",
        "TRACEFOLD_TEST_TRADING_ENABLED": "false",
        "TRACEFOLD_TEST_TRADING_CONTROL": str(trading_control),
        "TRACEFOLD_TEST_NAUTILUS_RECREATED": str(tmp_path / "nautilus-recreated"),
        "TRACEFOLD_TEST_CAPABILITY_BOOTSTRAP": str(tmp_path / "capability-bootstrap"),
        "TRACEFOLD_TEST_CAPABILITY_REFRESH": str(tmp_path / "capability-refresh"),
        "TRACEFOLD_TEST_BOOTSTRAP_ACCOUNT_ZERO": "ready",
        "TRACEFOLD_TEST_ACTIVE_CAPABILITY_SHA": "a" * 64,
        "TRACEFOLD_TEST_ROLE_PROVISION": str(tmp_path / "role-provision"),
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
    assert {"up", "status", "logs", "down", "db-provision-nautilus-role", "trading-hard-cut-preflight"} <= targets
    assert {
        "docker-up",
        "docker-status",
        "docker-logs",
        "docker-down",
    }.isdisjoint(targets)


def test_up_never_bootstraps_legacy_capability_or_starts_an_adapter(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_TRADING_ENABLED"] = "true"
    env["TRACEFOLD_TEST_NAUTILUS_CREDENTIALS_CONFIGURED"] = "true"
    env["TRACEFOLD_TEST_ACTIVE_CAPABILITY_SHA"] = ""

    result = subprocess.run(
        ["make", "up"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not Path(env["TRACEFOLD_TEST_CAPABILITY_BOOTSTRAP"]).exists()
    assert not Path(env["TRACEFOLD_TEST_CAPABILITY_REFRESH"]).exists()
    assert not Path(env["TRACEFOLD_TEST_NAUTILUS_RECREATED"]).exists()


@pytest.mark.parametrize("target", ("up", "deploy-image"), ids=("make-up", "deploy-image"))
def test_deploy_does_not_consult_a_legacy_capability_pointer(tmp_path: Path, target: str) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_TRADING_ENABLED"] = "true"
    env["TRACEFOLD_TEST_NAUTILUS_CREDENTIALS_CONFIGURED"] = "true"
    control = Path(env["TRACEFOLD_TEST_TRADING_CONTROL"])
    control.write_text("RUNNING\n", encoding="utf-8")

    command = ["make", target]
    if target == "deploy-image":
        command.append(f"IMAGE_ID={TEST_IMAGE_ID}")
    result = subprocess.run(
        command,
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not Path(env["TRACEFOLD_TEST_CAPABILITY_BOOTSTRAP"]).exists()
    assert not Path(env["TRACEFOLD_TEST_CAPABILITY_REFRESH"]).exists()
    assert not Path(env["TRACEFOLD_TEST_NAUTILUS_RECREATED"]).exists()
    assert control.read_text(encoding="utf-8").strip() == "RUNNING"


def test_up_does_not_wait_for_a_retired_execution_bootstrap(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_TRADING_ENABLED"] = "true"
    env["TRACEFOLD_TEST_NAUTILUS_CREDENTIALS_CONFIGURED"] = "true"
    env["TRACEFOLD_TEST_ACTIVE_CAPABILITY_SHA"] = ""
    env["TRACEFOLD_TEST_BOOTSTRAP_ACCOUNT_ZERO"] = ""

    result = subprocess.run(
        ["make", "up", "TRACEFOLD_COMPOSE_WAIT_SECONDS=1"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not Path(env["TRACEFOLD_TEST_CAPABILITY_BOOTSTRAP"]).exists()
    assert not Path(env["TRACEFOLD_TEST_CAPABILITY_REFRESH"]).exists()


@pytest.mark.parametrize("auth_state", ["missing-cli", "unauthenticated", "authenticated"])
def test_verify_main_ci_preflights_the_active_github_dot_com_account(tmp_path: Path, auth_state: str) -> None:
    make = shutil.which("make")
    assert make is not None
    verifier_called = tmp_path / "verifier-called"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        '#!/bin/sh\n: > "$TRACEFOLD_TEST_VERIFY_MAIN_CI_CALLED"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    if auth_state != "missing-cli":
        fake_gh = tmp_path / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            "command=$1\n"
            "subcommand=$2\n"
            "shift 2\n"
            "active=0\n"
            "host=''\n"
            'while [ "$#" -gt 0 ]; do\n'
            '  case "$1" in\n'
            "    --active) active=1; shift ;;\n"
            "    --hostname) host=$2; shift 2 ;;\n"
            "    *) exit 64 ;;\n"
            "  esac\n"
            "done\n"
            '[ "$command $subcommand" = "auth status" ] || exit 64\n'
            '[ "$active" = 1 ] || exit 64\n'
            '[ "$host" = github.com ] || exit 64\n'
            'exit "$TRACEFOLD_TEST_GH_AUTH_EXIT"\n',
            encoding="utf-8",
        )
        fake_gh.chmod(0o700)

    result = subprocess.run(
        [make, "--no-print-directory", "verify-main-ci"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": str(tmp_path),
            "TRACEFOLD_TEST_GH_AUTH_EXIT": "1" if auth_state == "unauthenticated" else "0",
            "TRACEFOLD_TEST_VERIFY_MAIN_CI_CALLED": str(verifier_called),
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert (result.returncode == 0) is (auth_state == "authenticated")
    assert verifier_called.exists() is (auth_state == "authenticated")
    if auth_state == "missing-cli":
        assert "GitHub CLI is not installed or not on PATH" in result.stderr
    elif auth_state == "unauthenticated":
        assert "GitHub CLI is not authenticated for github.com" in result.stderr


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
    github_probe = tmp_path / "github-probe"
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        '#!/bin/sh\n: > "$TRACEFOLD_TEST_GH_PROBE"\nexit 99\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)

    result = subprocess.run(
        ["make", "status"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "TRACEFOLD_TEST_GH_PROBE": str(github_probe),
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "migrate: state=running exit_code=0" in result.stderr
    assert not github_probe.exists()


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


def test_trading_hard_cut_preflight_proves_one_ready_replica_and_drained_ledgers(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_NAUTILUS_PRESENT"] = "1"
    env["TRACEFOLD_TEST_DB_HEAD"] = "PAUSED|0|0|0"

    result = subprocess.run(
        ["make", "trading-hard-cut-preflight"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "venue flat, PAUSED, ledgers drained, one Nautilus replica" in result.stdout


@pytest.mark.parametrize(
    ("replica_present", "cut_state", "message"),
    (
        (False, "PAUSED|0|0|0", "exactly one Nautilus replica; found 0"),
        (True, "RUNNING|0|0|0", "observed RUNNING|0|0|0"),
        (True, "PAUSED|1|0|0", "observed PAUSED|1|0|0"),
        (True, "PAUSED|0|1|0", "observed PAUSED|0|1|0"),
        (True, "PAUSED|0|0|1", "observed PAUSED|0|0|1"),
    ),
)
def test_trading_hard_cut_preflight_fails_closed(
    tmp_path: Path,
    replica_present: bool,
    cut_state: str,
    message: str,
) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    if replica_present:
        env["TRACEFOLD_TEST_NAUTILUS_PRESENT"] = "1"
    env["TRACEFOLD_TEST_DB_HEAD"] = cut_state

    result = subprocess.run(
        ["make", "trading-hard-cut-preflight"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert message in result.stderr


def test_up_does_not_start_an_adapter_before_its_owner_issue(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_NAUTILUS_CREDENTIALS_CONFIGURED"] = "true"
    env["TRACEFOLD_TEST_TRADING_ENABLED"] = "true"

    result = subprocess.run(
        ["make", "up"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "nautilus" not in Path(env["TRACEFOLD_TEST_UP_ARGS"]).read_text(encoding="utf-8")


def test_up_automatically_enforces_the_pr2_preflight_before_stopping_services(tmp_path: Path) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_NAUTILUS_CREDENTIALS_CONFIGURED"] = "true"
    env["TRACEFOLD_TEST_TRADING_ENABLED"] = "true"
    env["TRACEFOLD_TEST_MIGRATION_STATE"] = "20260828_0316|f|f"
    env["TRACEFOLD_TEST_NAUTILUS_PRESENT"] = "1"
    env["TRACEFOLD_TEST_DB_HEAD"] = "PAUSED|0|0|0"

    result = subprocess.run(
        ["make", "up"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Trading hard-cut preflight passed" in result.stdout
    assert services_stopped.exists()


def test_up_refuses_the_pr2_migration_when_the_automatic_preflight_fails(tmp_path: Path) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_NAUTILUS_CREDENTIALS_CONFIGURED"] = "true"
    env["TRACEFOLD_TEST_TRADING_ENABLED"] = "true"
    env["TRACEFOLD_TEST_MIGRATION_STATE"] = "20260828_0316|f|f"
    env["TRACEFOLD_TEST_NAUTILUS_PRESENT"] = "1"
    env["TRACEFOLD_TEST_DB_HEAD"] = "RUNNING|0|0|0"

    result = subprocess.run(
        ["make", "up"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "observed RUNNING|0|0|0" in result.stderr
    assert not services_stopped.exists()


def test_db_migrate_automatically_enforces_the_pr2_preflight(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_MIGRATION_STATE"] = "20260828_0316|f|f"
    env["TRACEFOLD_TEST_NAUTILUS_PRESENT"] = "1"
    env["TRACEFOLD_TEST_DB_HEAD"] = "PAUSED|0|0|0"

    result = subprocess.run(
        ["make", "db-migrate"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Trading hard-cut preflight passed" in result.stdout


def test_db_migrate_enforces_the_v2_preflight_from_the_endpoint_epoch(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_MIGRATION_STATE"] = "20260828_0319|t|f"
    env["TRACEFOLD_TEST_NAUTILUS_PRESENT"] = "1"
    env["TRACEFOLD_TEST_DB_HEAD"] = "PAUSED|0|0|0"

    result = subprocess.run(
        ["make", "db-migrate"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Trading hard-cut preflight passed" in result.stdout


def test_db_migrate_enforces_the_db_only_quote_authority_preflight_without_nautilus(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_MIGRATION_STATE"] = "20260829_0328|t|t"
    env["TRACEFOLD_TEST_DB_HEAD"] = "PAUSED|0"

    result = subprocess.run(
        ["make", "db-migrate"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Intent Quote preflight passed: PAUSED and no recovery obligations" in result.stdout


@pytest.mark.parametrize("cut_state", ("RUNNING|0", "PAUSED|1"))
def test_db_migrate_refuses_0329_when_its_database_invariants_are_not_met(
    tmp_path: Path,
    cut_state: str,
) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_MIGRATION_STATE"] = "20260829_0328|t|t"
    env["TRACEFOLD_TEST_DB_HEAD"] = cut_state

    result = subprocess.run(
        ["make", "db-migrate"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert f"Intent Quote cut requires PAUSED|nonterminal_intents=0; observed {cut_state}" in result.stderr


def test_db_migrate_does_not_invent_a_cutover_for_a_fresh_database(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_SCHEMA_STATE"] = "fresh"

    result = subprocess.run(
        ["make", "db-migrate"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Fresh database: no PR 1 execution authority exists to cut over" in result.stdout


@pytest.mark.parametrize("target", ("up", "deploy-image"))
def test_deploy_runs_decision_without_demo_credentials_or_nautilus(
    tmp_path: Path,
    target: str,
) -> None:
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_TRADING_ENABLED"] = "true"
    command = ["make", target]
    if target == "deploy-image":
        command.append(f"IMAGE_ID={TEST_IMAGE_ID}")

    result = subprocess.run(command, cwd=repo, env=env, capture_output=True, check=False, text=True)

    assert result.returncode == 0, result.stderr
    assert services_stopped.exists()
    assert not Path(env["TRACEFOLD_TEST_NAUTILUS_RECREATED"]).exists()
    assert not Path(env["TRACEFOLD_TEST_CAPABILITY_BOOTSTRAP"]).exists()
    assert "execution adapters: not required (#356/#357 pending; capital paused)" in result.stdout


@pytest.mark.parametrize(
    ("trading_enabled", "credentials_configured", "expected_ok", "expected_message"),
    (
        ("true", "true", True, "execution adapters: not required (#356/#357 pending; capital paused)"),
        ("true", "false", True, "execution adapters: not required (#356/#357 pending; capital paused)"),
        ("false", "false", True, "execution adapters: not required (Trading disabled)"),
    ),
)
def test_status_does_not_require_an_adapter_before_its_owner_issue(
    tmp_path: Path,
    trading_enabled: str,
    credentials_configured: str,
    expected_ok: bool,
    expected_message: str,
) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_TRADING_ENABLED"] = trading_enabled
    env["TRACEFOLD_TEST_NAUTILUS_CREDENTIALS_CONFIGURED"] = credentials_configured

    result = subprocess.run(
        ["make", "status"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert (result.returncode == 0) is expected_ok
    assert expected_message in result.stdout + result.stderr


def test_exact_image_deploy_does_not_start_an_adapter_before_its_owner_issue(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_NAUTILUS_CREDENTIALS_CONFIGURED"] = "true"
    env["TRACEFOLD_TEST_TRADING_ENABLED"] = "true"

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "nautilus" not in Path(env["TRACEFOLD_TEST_UP_ARGS"]).read_text(encoding="utf-8")


def test_exact_image_deploy_does_not_recreate_nautilus_without_demo_credentials(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    env["TRACEFOLD_TEST_NAUTILUS_PRESENT"] = "1"

    result = subprocess.run(
        ["make", "deploy-image", f"IMAGE_ID={TEST_IMAGE_ID}"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "nautilus" not in Path(env["TRACEFOLD_TEST_UP_ARGS"]).read_text(encoding="utf-8")


def test_nautilus_role_provisioning_refuses_a_running_compose_stack(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    provisioned = Path(env["TRACEFOLD_TEST_ROLE_PROVISION"])
    env["TRACEFOLD_TEST_RUNNING_CONTAINERS"] = "postgres-id\n"

    result = subprocess.run(
        ["make", "db-provision-nautilus-role"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "offline-only" in result.stderr
    assert not provisioned.exists()


def test_nautilus_role_provisioning_uses_one_offline_single_user_container(tmp_path: Path) -> None:
    repo, _external_activity, _services_stopped, env = _deploy_image_sandbox(tmp_path)
    provisioned = Path(env["TRACEFOLD_TEST_ROLE_PROVISION"])

    result = subprocess.run(
        ["make", "db-provision-nautilus-role"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    invocation = provisioned.read_text(encoding="utf-8")
    assert "compose run --rm --no-deps --user postgres" in invocation
    assert "--entrypoint /usr/local/bin/tracefold-provision-nautilus-role postgres" in invocation


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
            "tracefold/platform/postgres/alembic/versions/untracked_revision.py",
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
            "tracefold/platform/postgres/alembic/versions/ignored_revision.py",
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
    # The untracked check is a positive allowlist of deployment inputs, so the research workspace
    # (#274) is allowed by construction rather than by a path carve-out. This pins that: an operator
    # drafting in `notebooks/` must never be the reason a deploy refuses.
    repo, _external_activity, services_stopped, env = _deploy_image_sandbox(tmp_path)
    notebooks = repo / "notebooks"
    notebooks.mkdir(parents=True)
    (notebooks / "trading-agent-72h-event-study.ipynb").write_text("{}\n", encoding="utf-8")

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
