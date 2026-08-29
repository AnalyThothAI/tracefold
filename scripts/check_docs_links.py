#!/usr/bin/env python3
"""Fail when a repository Markdown document links to a missing local target."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\((?P<target>[^)]+)\)")


def documentation_sources(root: Path) -> tuple[Path, ...]:
    docs = root / "docs"
    sources = {
        root / "README.md",
        root / "AGENTS.md",
        root / "CLAUDE.md",
        root / "notebooks" / "README.md",
        *docs.glob("*.md"),
        *(docs / "agents").glob("*.md"),
        docs / "generated" / "README.md",
    }
    return tuple(sorted(source for source in sources if source.is_file()))


def missing_links(root: Path) -> tuple[str, ...]:
    missing: list[str] = []
    for source in documentation_sources(root):
        for match in _MARKDOWN_LINK_RE.finditer(source.read_text(encoding="utf-8")):
            target = match.group("target").strip().strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_path = target.split("#", 1)[0]
            if target_path and not (source.parent / target_path).resolve().exists():
                missing.append(f"{source.relative_to(root)} -> {target}")
    return tuple(sorted(missing))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    missing = missing_links(root)
    if missing:
        sys.stderr.write("documentation links missing:\n" + "\n".join(missing) + "\n")
        return 1
    print(f"documentation links valid: {len(documentation_sources(root))} sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["documentation_sources", "main", "missing_links"]
