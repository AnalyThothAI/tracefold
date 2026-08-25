"""Import boundary for the one offline optimizer.

Until #202 this file guarded a container platform: which module could load the untrusted optimizer, which
could name the launcher, which could reach the proxy. That boundary existed to answer "where were these two
strings produced". The platform is gone, so the question this file asks changed with it — not *where* the
optimization runs, but *what the process running it can reach*.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"
CLI_NEWS = tuple(sorted((SRC / "app" / "cli" / "commands").glob("news_*.py")))
OFFLINE_ENTRY_MODULE = "tracefold.news.learning.optimizer"
RESEARCH_CLI = SRC / "app" / "cli" / "commands" / "news_learning_experiment.py"


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


def test_the_news_specific_compiler_platform_is_gone() -> None:
    """#202 §6.2 and §12: deleted, not wrapped.

    The image, the launcher, the metered proxy sidecar, the seccomp policy, the tariff, the three-party
    build attestation and the runner are all one answer to a question the write-set makes moot — GEPA
    returns two strings, and `dspy.GEPA` cannot return anything else. A re-introduction under another name
    would defeat the whole point, so the absence is asserted by path and by import.
    """

    assert not (SRC / "news" / "learning" / "compiler").exists()
    assert not (SRC / "news" / "learning" / "proposer.py").exists()
    offenders = {
        path.relative_to(ROOT).as_posix()
        for path in [*SRC.rglob("*.py"), *(ROOT / "tests").rglob("*.py")]
        if any(module.startswith("tracefold.news.learning.compiler") for module in _imports(path))
    }
    assert offenders == set()
    assert "AS compiler" not in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_the_one_offline_entry_point_has_named_importers() -> None:
    """Who may start an optimization is a decision, not an accident."""

    importers = {
        path.relative_to(ROOT).as_posix() for path in SRC.rglob("*.py") if OFFLINE_ENTRY_MODULE in _imports(path)
    }
    assert importers == {"src/tracefold/app/cli/commands/news_learning_experiment.py"}


def test_only_the_research_cli_loads_the_optimizer_in_process() -> None:
    """One named exception among the News CLI modules, asserted rather than assumed.

    The release seam — `register`, `evaluate`, `canary` — holds promotion authority, so it must not import
    DSPy or the optimizer at all. Adding a second module that does is a decision someone has to make here.
    """

    for path in CLI_NEWS:
        modules = _imports(path)
        if path == RESEARCH_CLI:
            assert OFFLINE_ENTRY_MODULE in modules
            continue
        assert OFFLINE_ENTRY_MODULE not in modules, path
        assert "dspy" not in modules, path


def test_the_offline_entry_point_reaches_no_database_review_or_release_seam() -> None:
    """The offline job's powers, asserted from its import graph rather than from its docstring.

    It may read a frozen corpus it was handed and talk to three model endpoints. It may not register a
    candidate, arm a canary, promote anything, or open a database session — an `ADVANCE` is a proposal, and
    a process that could ship one would make every gate downstream advisory (#202 §3.2).
    """

    forbidden = {
        "tracefold.news.learning.canary",
        "tracefold.news.learning.evaluator",
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

    forbidden = {"tracefold.news.learning.canary", "tracefold.news.learning.optimizer"}
    experiment = SRC / "news" / "learning" / "experiment"
    for path in sorted(experiment.rglob("*.py")):
        modules = _imports(path)
        assert not (modules & forbidden), (path, sorted(modules & forbidden))
        assert not any(module.startswith("tracefold.news.storage") for module in modules), path
