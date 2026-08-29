from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tests.support import evidence

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\((?P<target>[^)]+)\)")


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
    sources = [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "CLAUDE.md",
        ROOT / "notebooks" / "README.md",
        *DOCS.glob("*.md"),
        *(DOCS / "agents").glob("*.md"),
        DOCS / "generated" / "README.md",
    ]
    missing: list[str] = []
    for source in sources:
        for match in MARKDOWN_LINK_RE.finditer(source.read_text(encoding="utf-8")):
            target = match.group("target").strip().strip("<>")
            if target.startswith(("http://", "https://", "#")):
                continue
            target_path = target.split("#", 1)[0]
            if target_path and not (source.parent / target_path).resolve().exists():
                missing.append(f"{source.relative_to(ROOT)} -> {target}")
    assert missing == []
