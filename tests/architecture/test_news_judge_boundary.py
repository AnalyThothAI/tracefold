"""Capability boundary of the operator's card judge and its calibration harness (#706)."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "tracefold"


def _package(path: Path) -> list[str]:
    """The dotted package this file lives in, so a relative import can be read as the module it means.

    Dropping the last part is right for both kinds of file: `news/foo.py` and `news/__init__.py` both sit
    in the package `tracefold.news`, which is what `from . import x` resolves against in either one.
    """

    base = SRC.parent if SRC in path.parents else ROOT
    return list(path.relative_to(base).with_suffix("").parts[:-1])


def _imports(path: Path) -> set[str]:
    """Every module this file names, relative ones resolved.

    Resolution is the point. `from ..optimizer import ...` reaches the optimizer exactly as surely as the
    absolute spelling does, and a boundary that only reads absolute imports is a boundary one keystroke
    gets around.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package(path)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                modules.add(str(node.module or ""))
                continue
            base = package[: len(package) - node.level + 1]
            modules.add(".".join([*base, *(str(node.module).split(".") if node.module else [])]))
    return modules


def test_the_card_judge_opens_no_database_and_reaches_no_runtime_owner() -> None:
    """The judge and its calibration read a fixed corpus and talk to one model endpoint, nothing else.

    The App composes the endpoint; the judge never reaches back into App, storage, the pipeline, the
    review desk or a database driver, so measuring the judge can never write, deliver or trade.
    """

    forbidden = ("tracefold.app", "tracefold.news.storage", "tracefold.news.pipeline", "tracefold.news.review")
    offenders = {
        path.relative_to(ROOT).as_posix(): sorted(
            module
            for module in _imports(path)
            if module.startswith(forbidden) or module.startswith(("tracefold.platform.postgres", "psycopg"))
        )
        for path in sorted((SRC / "news" / "learning").rglob("*.py"))
    }
    assert {path: modules for path, modules in offenders.items() if modules} == {}
