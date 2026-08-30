from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.deploy

# What "deploys application source" means, mechanically: something that puts *this repository's*
# code — the image built from it, or its Alembic revisions — in front of production.
_DEPLOYS_APPLICATION_SOURCE = (
    re.compile(r"docker compose[^\n]*\bup\b"),
    re.compile(r"docker compose[^\n]*\bbuild\b"),
    re.compile(r"\btracefold db migrate\b"),
    re.compile(r"docker run[^\n]*tracefold"),
)
# The one production-touching recipe that ships no application code: a script baked into the
# postgres image, run against an offline volume to create a database role. Requiring a green main
# for it would make a recovery harder without making a deployment safer.
_RUNS_AN_IMAGE_OWNED_SCRIPT = re.compile(r"--entrypoint /usr/local/bin/tracefold-provision-")
_PRIVATE_TARGET = re.compile(r"make --no-print-directory (_[a-z-]+)")


def _make_targets() -> tuple[str, ...]:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    return tuple(sorted(set(re.findall(r"^([a-zA-Z0-9_][a-zA-Z0-9_.-]*):", makefile, flags=re.MULTILINE))))


def _dry_run(target: str) -> str:
    return subprocess.run(
        ["make", "--dry-run", target],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    ).stdout


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


def _check(
    root: Path,
    *,
    conclusion: str = "success",
    app_id: int = 15_368,
    full: bool = True,
) -> dict[str, object]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True, text=True
    ).stdout.strip()
    return {
        "name": "ci-gate",
        "status": "completed",
        "conclusion": conclusion,
        "app": {"id": app_id},
        "details_url": "https://github.com/AnalyThothAI/tracefold/actions/runs/123/job/456",
        "tracefold_workflow_run": {
            "event": "push" if full else "pull_request",
            "head_branch": "main" if full else "feature",
            "head_sha": head,
            "status": "completed",
            "conclusion": "success",
            "path": ".github/workflows/ci.yml",
        },
    }


def test_verifier_cli_ignores_an_ambient_github_enterprise_host(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    scripts = root / "scripts"
    scripts.mkdir()
    verifier = scripts / "require_main_ci.py"
    shutil.copy2(ROOT / "scripts" / "require_main_ci.py", verifier)
    subprocess.run(["git", "add", "scripts/require_main_ci.py"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tracefold Test",
            "-c",
            "user.email=tests@tracefold.invalid",
            "commit",
            "-qm",
            "add verifier",
        ],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=root, check=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True, text=True
    ).stdout.strip()
    fake_gh.write_text(
        "#!/bin/sh\n"
        "host=''\n"
        "endpoint=''\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        "    --hostname) host=$2; shift 2 ;;\n"
        "    *) endpoint=$1; shift ;;\n"
        "  esac\n"
        "done\n"
        '[ "$host" = github.com ] || exit 64\n'
        'case "$endpoint" in\n'
        "  */actions/runs/123) printf '%s\\n' "
        f'\'{{"event":"push","head_branch":"main","head_sha":"{head}",'
        '"status":"completed","conclusion":"success","path":".github/workflows/ci.yml"}\' ;;\n'
        "  *) printf '%s\\n' "
        '\'{"check_runs":[{"id":1,"name":"ci-gate","status":"completed",'
        '"conclusion":"success","app":{"id":15368},'
        '"details_url":"https://github.com/AnalyThothAI/tracefold/actions/runs/123/job/456"}]}\' ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)

    result = subprocess.run(
        [sys.executable, str(verifier)],
        cwd=root,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_HOST": "github.invalid"},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "deployment source verified:" in result.stdout


def test_exact_main_sha_with_actions_ci_gate_can_deploy(tmp_path: Path) -> None:
    from scripts.require_main_ci import require_main_ci

    root = _repository(tmp_path)
    assert require_main_ci(root, {"check_runs": [_check(root)]})


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
        require_main_ci(root, {"check_runs": [_check(root)]})


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("missing", "main_ci_gate_missing"),
        ("failure", "main_ci_gate_not_success"),
        ("spoofed", "main_ci_gate_wrong_integration"),
        ("selected", "main_ci_gate_not_full_plan"),
    ],
)
def test_missing_failed_or_spoofed_ci_gate_blocks_deployment(tmp_path: Path, case: str, error: str) -> None:
    from scripts.require_main_ci import require_main_ci

    root = _repository(tmp_path)
    if case == "missing":
        payload: dict[str, object] = {"check_runs": []}
    elif case == "failure":
        payload = {"check_runs": [_check(root, conclusion="failure")]}
    elif case == "spoofed":
        payload = {"check_runs": [_check(root, app_id=1)]}
    else:
        payload = {"check_runs": [_check(root, full=False)]}
    with pytest.raises(RuntimeError, match=error):
        require_main_ci(root, payload)


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
        require_main_ci(root, {"check_runs": [_check(root)]})


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
        require_main_ci(root, {"check_runs": [_check(root)]})


def test_secondary_worktree_cannot_deploy_the_primary_stack(tmp_path: Path) -> None:
    from scripts.require_main_ci import require_main_ci

    root = _repository(tmp_path)
    worktree = tmp_path / "secondary"
    subprocess.run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], cwd=root, check=True)

    with pytest.raises(RuntimeError, match="deployment_checkout_not_primary"):
        require_main_ci(worktree, {"check_runs": [_check(root)]})


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
        require_main_ci(root, {"check_runs": [_check(root)]})


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
def test_the_locked_deployment_entries_hold_the_lock_and_the_gate(target: str, private_target: str) -> None:
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


def test_every_entry_that_deploys_application_source_requires_the_exact_main_gate() -> None:
    """Derived from the Makefile, because a list of names cannot notice an entry nobody added to it.

    The previous version of this contract named `up` and `deploy-image`. That is exactly why
    `make db-migrate` went ungated for as long as it did: `OPERATIONS.md` names it beside `make up`
    as an operator migration entry, it applies Alembic revisions to the production database from
    whatever tree invoked it, and its only prerequisite was that git, uv and docker existed — but it
    was not in the list, so nothing looked. Classifying by what a recipe *does* removes the list.

    Three things are deliberately not in scope, and saying so here is cheaper than rediscovering it:
    `db-provision-nautilus-role` runs a script baked into the *postgres* image against an offline
    volume and ships no application code; the `*-preflight` targets are read-only proofs; and
    `down`, `status`, `logs` and the `*-shell` targets observe or stop what is already running.
    """

    ungated: list[str] = []
    for target in _make_targets():
        recipe = _dry_run(target)
        if _RUNS_AN_IMAGE_OWNED_SCRIPT.search(recipe):
            continue
        if not any(pattern.search(recipe) for pattern in _DEPLOYS_APPLICATION_SOURCE):
            continue
        # A public entry may reach the verifier through the private target it takes the lock for,
        # which `make --dry-run` does not expand for it.
        reachable = recipe + "".join(_dry_run(private) for private in _PRIVATE_TARGET.findall(recipe))
        if "scripts/require_main_ci.py" not in reachable:
            ungated.append(target)

    assert ungated == []
