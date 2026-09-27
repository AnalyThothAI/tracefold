#!/usr/bin/env python3
"""Generate a tracked-file navigation map without importing application code or reading secrets."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

OUTPUT = Path("docs/generated/repository-map.md")


def tracked_paths(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True)
    return tuple(sorted({Path(name) for name in result.stdout.split("\0") if name and (root / name).is_file()}))


def owner(path: Path) -> str:
    value = path.as_posix()
    if value.startswith("web/"):
        return "docs/FRONTEND.md"
    if value.startswith(("tests/",)):
        return "docs/TESTING.md"
    if value.startswith("tracefold/news/chain_tape/") or "wallet" in path.name:
        return "docs/modules/wallets.md"
    if value.startswith(tuple(f"tracefold/news/{part}/" for part in ("learning", "eval", "review", "release"))):
        return "docs/modules/learning.md"
    if value.startswith("tracefold/news/"):
        return (
            "docs/modules/oi.md"
            if path.stem
            in {"oi_signals", "oi_contracts", "liquidations", "smart_money", "market_notifications", "market_contracts"}
            else "docs/modules/news.md"
        )
    if value.startswith(("tracefold/app/nautilus/", "tracefold/integrations/nautilus/")):
        return "docs/modules/execution.md"
    if value.startswith(
        ("tracefold/trading/", "tracefold/app/trading", "tracefold/app/analysis", "tracefold/integrations/marketdata/")
    ):
        return "docs/modules/trading.md"
    if value.startswith("tracefold/"):
        return "docs/modules/platform.md"
    if value.startswith(("notebooks/", "datasets/")):
        return "notebooks/README.md"
    if value.startswith(("scripts/", ".github/")):
        return "docs/DEVELOPMENT.md"
    return "docs/README.md"


def section(path: Path) -> str:
    parts = path.parts
    if len(parts) == 1:
        return "Repository root"
    if parts[0] in {"tracefold", "web", "docs", "tests", "notebooks"} and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0]


def clean(text: str, limit: int = 170) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return (
        text.replace("|", "\\|")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("`", "")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def description(root: Path, path: Path) -> str:
    if path == OUTPUT:
        return "Generated tracked-file navigation map (this document)."
    if path.suffix == ".py":
        tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path.as_posix())
        doc = ast.get_docstring(tree)
        declared = [
            node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        ]
        summary = (
            clean(doc.split("\n\n", 1)[0])
            if doc
            else "Python module"
            if path.name != "__init__.py"
            else "Package entry point"
        )
        if declared:
            suffix = ", ".join(declared[:5])
            if len(declared) > 5:
                suffix += f"; +{len(declared) - 5} more"
            summary += ". Declarations: " + clean(suffix, 170)
        return summary
    if path.suffix == ".md":
        heading = next(
            (
                line.lstrip("# ")
                for line in (root / path).read_text(encoding="utf-8").splitlines()
                if line.startswith("# ")
            ),
            "Markdown document",
        )
        return clean(heading)
    return {
        ".tsx": "React component, route, hook or test; see its feature and frontend guide.",
        ".ts": "TypeScript source, contract, configuration or test; see the owning directory.",
        ".css": "Styles owned by the local feature or shared token/shell boundary.",
        ".json": "Structured contract, fixture, data, evidence or dependency metadata; path identifies its owner.",
        ".jsonl": "Line-oriented dataset or evidence records; not a runtime configuration file.",
        ".yaml": "Tracked configuration/build/test declaration; not the operator's credential-bearing config.",
        ".yml": "Tracked workflow/configuration declaration; not the operator's credential-bearing config.",
        ".sql": "Schema, test or research SQL; inspect its owner before execution.",
        ".ipynb": "Offline research notebook; not imported by runtime processes.",
        ".sh": "Shell workflow or maintenance helper; inspect before execution.",
        ".svg": "Vector asset.",
        ".png": "Image/visual evidence asset.",
        ".toml": "Project/tool configuration.",
        ".lock": "Resolved dependency lock.",
    }.get(path.suffix, "Tracked repository artifact; see its directory and linked owner.")


def render(root: Path, paths: tuple[Path, ...]) -> str:
    groups: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(set(paths)):
        groups[section(path)].append(path)
    lines = [
        "# Repository file map",
        "",
        "Generated by `scripts/regen_repository_map.py` from Git-tracked files and Python AST declarations.",
        "Do not hand-edit. This is a navigation inventory, not a claim that every line was manually audited.",
        "Application modules are never imported; ignored operator files and credentials are not scanned.",
        "Stage new intended files before regeneration so the inventory follows the tracked source boundary.",
        "",
        f"**{len(set(paths))} tracked files**, grouped by owning source area.",
        "",
        "[Handbook](../README.md) · [Architecture](../ARCHITECTURE.md) · [Generator contract](README.md)",
        "",
        "| Area | Files |",
        "| --- | ---: |",
    ]
    for name, members in sorted(groups.items()):
        lines.append(f"| `{name}` | {len(members)} |")
    for name, members in sorted(groups.items()):
        lines.extend(
            ["", f"## {name}", "", "| Tracked file | Declared purpose / navigation | Guide |", "| --- | --- | --- |"]
        )
        for path in members:
            target = "../../" + quote(path.as_posix(), safe="/._-")
            guide = owner(path)
            guide_target = "../../" + guide
            guide_name = Path(guide).stem
            lines.append(
                f"| [`{path.as_posix()}`]({target}) | {description(root, path)} | [{guide_name}]({guide_target}) |"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    text = render(root, tracked_paths(root))
    output = root / OUTPUT
    if args.check:
        if not output.exists() or output.read_text(encoding="utf-8") != text:
            print(
                "Repository map drifted. Stage intended new files, then run "
                "python scripts/regen_repository_map.py --write"
            )
            return 1
        print("Repository file map is current")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
