from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.deploy


def _repository(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    (root / "tracked.txt").write_text("one\n", encoding="utf-8")
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(
        "name: CI\non: [push]\njobs:\n  gate:\n    name: ci-gate\n    runs-on: ubuntu-24.04\n    steps: []\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tracefold Test",
            "-c",
            "user.email=tests@tracefold.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=root, check=True)
    subprocess.run(["git", "push", "-qu", "origin", "main"], cwd=root, check=True)
    return root


def _check(*, conclusion: str = "success", app_id: int = 15_368) -> dict[str, object]:
    return {
        "name": "ci-gate",
        "status": "completed",
        "conclusion": conclusion,
        "app": {"id": app_id},
    }


def test_check_runs_ignores_an_ambient_github_enterprise_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.require_main_ci import _check_runs

    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        "host=''\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        "    --hostname) host=$2; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        '[ "$host" = github.com ] || exit 64\n'
        "printf '{\"check_runs\": []}\\n'\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("GH_HOST", "github.invalid")

    assert _check_runs("a" * 40) == {"check_runs": []}


def test_exact_main_sha_with_actions_ci_gate_can_deploy(tmp_path: Path) -> None:
    from scripts.require_main_ci import require_main_ci

    assert require_main_ci(_repository(tmp_path), {"check_runs": [_check()]})


def test_duplicate_ci_gate_definition_blocks_deployment(tmp_path: Path) -> None:
    from scripts.require_main_ci import require_main_ci

    root = _repository(tmp_path)
    duplicate = root / ".github" / "workflows" / "duplicate.yml"
    duplicate.write_text(
        "name: Duplicate\non: [push]\njobs:\n  other:\n    name: ci-gate\n    runs-on: ubuntu-24.04\n    steps: []\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".github/workflows/duplicate.yml"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tracefold Test",
            "-c",
            "user.email=tests@tracefold.invalid",
            "commit",
            "-qm",
            "duplicate gate",
        ],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=root, check=True)

    with pytest.raises(RuntimeError, match="deployment_ci_gate_definition_not_unique"):
        require_main_ci(root, {"check_runs": [_check()]})


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"check_runs": []}, "main_ci_gate_missing"),
        ({"check_runs": [_check(conclusion="failure")]}, "main_ci_gate_not_success"),
        ({"check_runs": [_check(app_id=1)]}, "main_ci_gate_wrong_integration"),
    ],
)
def test_missing_failed_or_spoofed_ci_gate_blocks_deployment(
    tmp_path: Path, payload: dict[str, object], error: str
) -> None:
    from scripts.require_main_ci import require_main_ci

    with pytest.raises(RuntimeError, match=error):
        require_main_ci(_repository(tmp_path), payload)


def test_commit_ahead_of_verified_origin_main_blocks_deployment(tmp_path: Path) -> None:
    from scripts.require_main_ci import require_main_ci

    root = _repository(tmp_path)
    (root / "tracked.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tracefold Test",
            "-c",
            "user.email=tests@tracefold.invalid",
            "commit",
            "-qm",
            "ahead",
        ],
        cwd=root,
        check=True,
    )

    with pytest.raises(RuntimeError, match="deployment_head_not_origin_main"):
        require_main_ci(root, {"check_runs": [_check()]})


@pytest.mark.parametrize("state", ["unstaged", "staged", "untracked", "ignored-env"])
def test_dirty_deployment_source_blocks_even_when_head_ci_is_green(tmp_path: Path, state: str) -> None:
    from scripts.require_main_ci import require_main_ci

    root = _repository(tmp_path)
    if state == "unstaged":
        (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    elif state == "staged":
        (root / "tracked.txt").write_text("staged\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    elif state == "untracked":
        (root / "Dockerfile.local").write_text("FROM scratch\n", encoding="utf-8")
    else:
        (root / ".git" / "info" / "exclude").write_text(".env\n", encoding="utf-8")
        (root / ".env").write_text("COMPOSE_FILE=surprise.yaml\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="deployment_source_dirty"):
        require_main_ci(root, {"check_runs": [_check()]})


def test_secondary_worktree_cannot_deploy_the_primary_stack(tmp_path: Path) -> None:
    from scripts.require_main_ci import require_main_ci

    root = _repository(tmp_path)
    worktree = tmp_path / "secondary"
    subprocess.run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], cwd=root, check=True)

    with pytest.raises(RuntimeError, match="deployment_checkout_not_primary"):
        require_main_ci(worktree, {"check_runs": [_check()]})


def test_stale_local_main_cannot_redeploy_an_old_green_sha(tmp_path: Path) -> None:
    from scripts.require_main_ci import require_main_ci

    root = _repository(tmp_path)
    updater = tmp_path / "updater"
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(tmp_path / "origin.git"), str(updater)],
        check=True,
    )
    (updater / "new.txt").write_text("new remote main\n", encoding="utf-8")
    subprocess.run(["git", "add", "new.txt"], cwd=updater, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tracefold Test",
            "-c",
            "user.email=tests@tracefold.invalid",
            "commit",
            "-qm",
            "advance remote",
        ],
        cwd=updater,
        check=True,
    )
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=updater, check=True)

    with pytest.raises(RuntimeError, match="deployment_head_not_remote_main"):
        require_main_ci(root, {"check_runs": [_check()]})


@pytest.mark.parametrize(
    "variable",
    [
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_ENV_FILES",
        "COMPOSE_PROFILES",
        "COMPOSE_PATH_SEPARATOR",
        "COMPOSE_DISABLE_ENV_FILE",
    ],
)
def test_deployment_refuses_inherited_compose_topology(variable: str) -> None:
    from scripts.require_main_ci import require_clean_deployment_environment

    with pytest.raises(RuntimeError, match=f"deployment_compose_environment_forbidden:{variable}"):
        require_clean_deployment_environment({variable: "surprise"})


def test_up_locks_compose_to_the_primary_repository_stack() -> None:
    result = subprocess.run(
        ["make", "--dry-run", "_up-locked"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )

    assert 'COMPOSE_FILE="$(pwd -P)/compose.yaml"' in result.stdout
    assert "COMPOSE_PROJECT_NAME=tracefold" in result.stdout
    assert "refuses inherited Compose stack variables" in result.stdout
    assert "with_deployment_lock.py --assert-held" in result.stdout
    assert "scripts/require_main_ci.py" in result.stdout


def test_boolean_environment_flag_cannot_invoke_the_private_deployment_target() -> None:
    result = subprocess.run(
        ["make", "--no-print-directory", "_up-locked"],
        cwd=ROOT,
        env={**os.environ, "TRACEFOLD_DEPLOY_LOCK_HELD": "1"},
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "inherited lock fd is missing" in result.stderr


@pytest.mark.parametrize(("target", "private_target"), [("up", "_up-locked"), ("deploy-image", "_deploy-image-locked")])
def test_every_deployment_entry_requires_the_main_ci_gate(target: str, private_target: str) -> None:
    public = subprocess.run(
        ["make", "--dry-run", target],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    locked = subprocess.run(
        ["make", "--dry-run", private_target],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )

    assert "scripts/with_deployment_lock.py" in public.stdout
    assert "with_deployment_lock.py --assert-held" in locked.stdout
    assert "scripts/require_main_ci.py" in locked.stdout
