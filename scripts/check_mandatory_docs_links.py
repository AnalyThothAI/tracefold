#!/usr/bin/env python3
"""Check local Markdown paths, heading anchors and reference links without network access."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

_INLINE_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(\s*(?P<target><[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
_REFERENCE_RE = re.compile(r"!?\[(?P<label>[^\]\n]+)\]\[(?P<ref>[^\]\n]*)\]")
_DEFINITION_RE = re.compile(r"^ {0,3}\[(?P<ref>[^\]]+)\]:\s*(?P<target><[^>]+>|\S+)", re.MULTILINE)


def documentation_sources(root: Path) -> tuple[Path, ...]:
    candidates = {
        *(root / name for name in ("README.md", "AGENTS.md", "CLAUDE.md", "CONTEXT.md")),
        *(root / ".github").rglob("*.md"),
        *(root / "docs").rglob("*.md"),
        *(root / "notebooks").rglob("*.md"),
    }
    return tuple(sorted(path for path in candidates if path.is_file()))


def without_fences(text: str) -> str:
    lines: list[str] = []
    fence = ""
    width = 0
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence, width = token[0], len(token)
            elif token[0] == fence and len(token) >= width and not line[marker.end() :].strip():
                fence = ""
            continue
        if not fence:
            lines.append(line)
    return "\n".join(lines)


def anchors(text: str) -> frozenset[str]:
    text = without_fences(text)
    found: set[str] = set(re.findall(r'\bid=["\']([^"\']+)["\']', text))
    headings: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            headings.append(heading.group(1))
        elif index and re.fullmatch(r" {0,3}(?:=+|-+)\s*", line) and lines[index - 1].strip():
            headings.append(lines[index - 1].strip())
    for raw_heading in headings:
        heading = re.sub(r"<[^>]*>", "", raw_heading)
        heading = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", heading)
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        candidate = slug
        count = 0
        while candidate in found:
            count += 1
            candidate = f"{slug}-{count}"
        found.add(candidate)
    return frozenset(found)


def local_targets(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    text = without_fences(text)
    definitions = {m.group("ref").casefold(): m.group("target") for m in _DEFINITION_RE.finditer(text)}
    targets = [m.group("target") for m in _INLINE_LINK_RE.finditer(text)]
    targets.extend(definitions.values())
    unresolved = []
    reference_text = re.sub(r"(`+).*?\1", "", text)
    for match in _REFERENCE_RE.finditer(reference_text):
        reference = (match.group("ref") or match.group("label")).casefold()
        if reference not in definitions:
            unresolved.append(reference)
    return tuple(targets), tuple(unresolved)


def missing_links(root: Path) -> tuple[str, ...]:
    errors: list[str] = []
    anchor_cache: dict[Path, frozenset[str]] = {}
    for source in documentation_sources(root):
        targets, unresolved = local_targets(source.read_text(encoding="utf-8"))
        name = source.relative_to(root).as_posix()
        errors.extend(f"{name}: undefined reference [{reference}]" for reference in unresolved)
        for target in targets:
            parsed = urlsplit(target.strip("<>"))
            if parsed.scheme or parsed.netloc:
                continue
            path = (source.parent / unquote(parsed.path)).resolve() if parsed.path else source.resolve()
            if not path.exists():
                errors.append(f"{name}: missing local target {target}")
                continue
            if parsed.fragment and path.is_file() and path.suffix == ".md":
                if path not in anchor_cache:
                    anchor_cache[path] = anchors(path.read_text(encoding="utf-8"))
                if unquote(parsed.fragment) not in anchor_cache[path]:
                    errors.append(f"{name}: missing Markdown anchor {target}")
    return tuple(sorted(set(errors)))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = missing_links(root)
    for error in errors:
        print(error)
    if errors:
        print(f"Documentation has {len(errors)} local-link errors")
        return 1
    print(f"Documentation links and anchors resolve in {len(documentation_sources(root))} Markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
