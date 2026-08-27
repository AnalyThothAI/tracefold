"""Capability boundaries for the offline optimizer and release authority."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"


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


def test_the_offline_entry_point_reaches_no_database_review_or_release_seam() -> None:
    """The offline job's powers, asserted from its import graph rather than from its docstring.

    It may read a frozen corpus it was handed and talk to three model endpoints. It may not register a
    candidate, arm a canary, promote anything, or open a database session — an `ADVANCE` is a proposal, and
    a process that could ship one would make every gate downstream advisory (#202 §3.2).
    """

    forbidden = {
        "tracefold.news.release.canary",
        "tracefold.news.learning.evaluate",
        "tracefold.news.learning.projection",
        "tracefold.news.learning.review",
        "tracefold.news.learning.review_drafter",
        "tracefold.platform.postgres",
        "psycopg",
    }
    modules = _imports(SRC / "news" / "learning" / "optimizer.py")
    assert not (modules & forbidden), sorted(modules & forbidden)
    assert not any(module.startswith("tracefold.news.storage") for module in modules)


def test_the_research_package_cannot_reach_promotion_or_the_release_seam() -> None:
    """Research scaffolding, structurally: it may read the corpus, and it names nothing that can ship.

    This proves the absence of a direct import, not the absence of a write — the research plane reaches the
    corpus through `CandidateEvaluator`, and a determined caller could reach further through it. What it
    buys is that promoting a winner cannot be written here by accident.
    """

    forbidden = {"tracefold.news.release.canary", "tracefold.news.learning.optimizer"}
    experiment = SRC / "news" / "learning" / "experiment"
    for path in sorted(experiment.rglob("*.py")):
        modules = _imports(path)
        assert not (modules & forbidden), (path, sorted(modules & forbidden))
        assert not any(module.startswith("tracefold.news.storage") for module in modules), path


def test_the_learning_plane_never_reaches_into_the_release_plane() -> None:
    """#202 §8, the direction that makes the split real rather than cosmetic.

    Release reads frozen datasets; datasets never reach back. Before the cut, `freeze_dataset(role=
    "validation")` called candidate validation, and candidate validation re-derived the Objective Plan
    from `development_compile_export` — a cycle, and the reason freezing a corpus and admitting a
    candidate could not be told apart. `freeze_dataset` now takes an `AdmittedCandidate` instead.

    `evaluate.py` is the one exception and it is the right way round: judging a candidate needs to know
    which candidate, so it composes the registry. It still decides no state — it returns evidence.
    """

    learning = SRC / "news" / "learning"
    offenders = {
        path.relative_to(ROOT).as_posix()
        for path in sorted(learning.rglob("*.py"))
        if path.name != "evaluate.py" and any(module.startswith("tracefold.news.release") for module in _imports(path))
    }
    assert offenders == set()
