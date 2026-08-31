from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import check_mandatory_docs_links

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
BEGIN = "<!-- BEGIN SHARED AGENT ROUTER -->"
END = "<!-- END SHARED AGENT ROUTER -->"


def test_make_check_runs_each_canonical_drift_checker_once_without_external_resources() -> None:
    result = subprocess.run(
        ["make", "--dry-run", "check"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    commands = result.stdout

    for checker in (
        "scripts/regen_cli_help.py --check",
        "scripts/sync_agent_router.py --check",
        "scripts/check_mandatory_docs_links.py",
    ):
        assert commands.count(checker) == 1, checker
    assert "not slow" in commands
    assert "not scheduled" in commands
    assert commands.count("python -m pytest") == 1
    assert all(tool not in commands for tool in ("docker", "npx", "npm "))


def test_default_test_target_selects_only_hermetic_lanes() -> None:
    result = subprocess.run(
        ["make", "--dry-run", "test"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    commands = result.stdout

    assert commands.count("python -m pytest") == 1
    for excluded in (
        "integration",
        "deploy",
        "e2e",
        "golden",
        "live",
        "slow",
        "scheduled",
        "external_codegen",
    ):
        assert f"not {excluded}" in commands
    assert all(tool not in commands for tool in ("docker", "testcontainers", "uvicorn", "npx", "npm "))


def test_complete_verification_uses_fixed_native_runner_contracts() -> None:
    result = subprocess.run(
        ["make", "--dry-run", "test-ci"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    commands = result.stdout

    assert "TRACEFOLD_HYPOTHESIS_PROFILE=ci" in commands
    assert "TRACEFOLD_TEST_RESOURCES_REQUIRED=1" in commands
    assert "--junitxml=" in commands
    assert "--reporter=json" in commands
    assert "scripts/require_test_reports.py" in commands
    for target in (
        "ci-quality-static",
        "ci-python-hermetic",
        "ci-postgres-behavior",
        "ci-migration",
        "ci-runtime-broker",
        "ci-deploy-e2e",
        "ci-test-integrity",
        "ci-frontend",
    ):
        assert target in commands
    assert "not live" in commands
    assert "not scheduled" in commands
    assert "tests.support" not in commands
    assert "manifest" not in commands


def test_current_documentation_links_resolve() -> None:
    assert check_mandatory_docs_links.missing_links(ROOT) == ()


def test_generated_agent_routers_share_only_the_small_router_and_one_canonical_worktree_policy() -> None:
    source = (DOCS / "agents" / "shared-router.md").read_text(encoding="utf-8")
    shared = source.split(BEGIN, 1)[1].split(END, 1)[0]
    invariant_section = shared.split("## Invariants", 1)[1].split("## Task routing", 1)[0]

    assert 5 <= sum(line.startswith("- ") for line in invariant_section.splitlines()) <= 10
    assert "## Agent skills" not in shared
    assert len(shared.splitlines()) <= 90
    for router_name in ("AGENTS.md", "CLAUDE.md"):
        router = (ROOT / router_name).read_text(encoding="utf-8")
        assert router.split(BEGIN, 1)[1].split(END, 1)[0] == shared
        appendix = router.split(END, 1)[1]
        assert "docs/agents/worktrees.md" in appendix
        assert "| Task surface |" not in appendix
        assert "npm ci" not in appendix
        assert "make sync" not in appendix
        assert "make up" not in appendix
