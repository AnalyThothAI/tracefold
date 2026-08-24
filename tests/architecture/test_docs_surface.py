from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
CANONICAL_DOCS = {
    "ARCHITECTURE.md",
    "CONTRACTS.md",
    "DEVELOPMENT.md",
    "FRONTEND.md",
    "OPERATIONS.md",
    "SECURITY.md",
    "SETUP.md",
}
GENERATED_FILES = {
    "README.md",
    "cli-help.md",
    "db-schema.md",
    "openapi.json",
    "refactor-baseline-9441ce99.json",
}
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\((?P<target>[^)]+)\)")


def test_docs_have_one_current_surface() -> None:
    assert {path.name for path in DOCS.glob("*.md")} == CANONICAL_DOCS
    assert {path.name for path in DOCS.iterdir() if path.is_dir()} == {
        "agents",
        "generated",
        "research",
    }
    assert not (DOCS / "sdd").exists()
    assert not (DOCS / "references").exists()


def test_generated_docs_are_bounded_and_reproducible() -> None:
    generated = DOCS / "generated"
    actual = {path.relative_to(generated).as_posix() for path in generated.rglob("*") if path.is_file()}
    assert actual == GENERATED_FILES


def test_make_check_runs_database_free_generated_drift_checks() -> None:
    result = subprocess.run(
        ["make", "--dry-run", "check"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )

    assert {
        line.strip()
        for line in result.stdout.splitlines()
        if "regen_cli_help" in line or "tests.support.refactor_baseline" in line or "sync_agent_router" in line
    } == {
        "uv run python scripts/regen_cli_help.py --check",
        "uv run python -m tests.support.refactor_baseline --check",
        "uv run python scripts/sync_agent_router.py --check",
    }


def test_current_documentation_links_resolve() -> None:
    sources = [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "CLAUDE.md",
        *(DOCS / name for name in CANONICAL_DOCS),
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


def test_agent_router_shared_blocks_come_from_the_canonical_source() -> None:
    """#162 PR7-B4: each router is compared with `docs/agents/shared-router.md`, not with the other one.

    Comparing them with each other only ever proved they had drifted together — which is exactly what
    happened: both carried a copy of the architecture that `docs/ARCHITECTURE.md` already owned.
    """

    def shared_block(path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        return text.split("<!-- BEGIN SHARED AGENT ROUTER -->", 1)[1].split(
            "<!-- END SHARED AGENT ROUTER -->",
            1,
        )[0]

    canonical = shared_block(DOCS / "agents" / "shared-router.md")
    assert shared_block(ROOT / "AGENTS.md") == canonical
    assert shared_block(ROOT / "CLAUDE.md") == canonical


def test_the_routers_keep_only_routing_and_their_own_tool_protocol() -> None:
    """A router that restates the system becomes a second, stale description of it."""

    for name in ("AGENTS.md", "CLAUDE.md"):
        router = ROOT / name
        lines = len(router.read_text(encoding="utf-8").splitlines())
        assert lines <= 200, f"{name} is {lines} lines; substantive detail belongs under docs/"
