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
    "收益率0730.md",
}
GENERATED_FILES = {
    "README.md",
    "cli-help.md",
    "db-schema.md",
    "openapi.json",
    "score-versions.md",
    "ws-protocol.md",
}
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\((?P<target>[^)]+)\)")


def test_docs_have_one_current_surface() -> None:
    assert {path.name for path in DOCS.glob("*.md")} == CANONICAL_DOCS
    assert {path.name for path in DOCS.iterdir() if path.is_dir()} == {"agents", "generated"}
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

    assert {line.strip() for line in result.stdout.splitlines() if "scripts/regen_" in line} == {
        "uv run python scripts/regen_cli_help.py --check",
        "uv run python scripts/regen_score_versions.py --check",
        "uv run python scripts/regen_ws_protocol.py --check",
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


def test_agent_router_shared_blocks_match() -> None:
    def shared_block(path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        return text.split("<!-- BEGIN SHARED AGENT ROUTER -->", 1)[1].split(
            "<!-- END SHARED AGENT ROUTER -->",
            1,
        )[0]

    assert shared_block(ROOT / "AGENTS.md") == shared_block(ROOT / "CLAUDE.md")
