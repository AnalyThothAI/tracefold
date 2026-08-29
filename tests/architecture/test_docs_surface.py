from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts import check_docs_links
from tests.support import evidence

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
        "scripts/check_docs_links.py",
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


def test_evidence_target_selects_every_deterministic_lane_and_excludes_opt_in_diagnostics() -> None:
    result = subprocess.run(
        ["make", "--dry-run", "test-evidence"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    commands = result.stdout

    assert "TRACEFOLD_TEST_EVIDENCE=1" in commands
    assert "-p tests.support.evidence" in commands
    assert '-m "not live and not scheduled"' in commands
    assert "--evidence-manifest=" in commands
    assert "--resource-evidence-manifest=" not in commands
    assert "--junitxml=" in commands
    for lane in (
        "quality-static",
        "python-hermetic",
        "postgres-behavior",
        "migration",
        "runtime-process",
        "frontend-python",
        "trust-root",
        "frontend-typecheck",
        "frontend-lint",
        "frontend-architecture",
        "frontend-unit",
        "frontend-format",
        "frontend-build",
        "browser",
    ):
        assert f"--required-lane {lane}" in commands
    for lane in evidence.PYTHON_LANES:
        assert f'--evidence-lane="{lane}"' in commands


def test_current_documentation_links_resolve() -> None:
    assert check_docs_links.missing_links(ROOT) == ()


def test_agent_task_router_executes_four_unambiguous_acceptance_dry_runs() -> None:
    cases = {
        "docs/SETUP.md": ("docs-only", {"quality-static"}),
        "src/tracefold/news/domain/event.py": (
            "pure Python",
            {"quality-static", "python-hermetic", "postgres-behavior", "runtime-process"},
        ),
        "src/tracefold/platform/postgres/database.py": (
            "PostgreSQL",
            {"quality-static", "python-hermetic", "postgres-behavior", "migration", "runtime-process"},
        ),
        "web/src/routes/News.tsx": (
            "frontend",
            {
                "quality-static",
                "frontend-python",
                "frontend-typecheck",
                "frontend-lint",
                "frontend-architecture",
                "frontend-unit",
                "frontend-format",
                "frontend-build",
                "browser",
            },
        ),
    }
    for changed_path, (surface, required_lanes) in cases.items():
        result = subprocess.run(
            [
                "uv",
                "run",
                "python",
                "scripts/agent_task_dry_run.py",
                "--changed-path",
                changed_path,
                "--tested-sha",
                "1" * 40,
                "--base-sha",
                "0" * 40,
            ],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
        )
        receipt = json.loads(result.stdout)

        assert receipt["surface"] == surface
        assert receipt["changed_path"] == changed_path
        assert receipt["router_conflicts"] == []
        assert receipt["must_read"]
        assert receipt["bootstrap"]
        assert receipt["development_tests"]
        assert set(receipt["required_lanes"]) == required_lanes


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
