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

    # Sources, not the directory: anyone who had the package imported before pulling this change is left
    # with a stray `__pycache__/`, and a bare `.exists()` fails on their checkout for a reason that has
    # nothing to do with the boundary. What must be gone is every module.
    assert list((SRC / "news" / "learning" / "compiler").rglob("*.py")) == []
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


def test_only_the_release_plane_can_admit_a_candidate() -> None:
    """`AdmittedCandidate` is the token that keeps the dependency one-way.

    A value anyone could construct would make the check it exists for a formality: `freeze_dataset` would
    be back to trusting its caller that a candidate was admitted. Naming the one producer is what makes
    "the release plane admitted this" a fact rather than an assertion in a docstring.
    """

    producers = {
        path.relative_to(ROOT).as_posix()
        for path in sorted(SRC.rglob("*.py"))
        if "AdmittedCandidate(" in path.read_text(encoding="utf-8")
    }
    # Exactly one, and it is not the module that declares the type: `dataset.py` names
    # `AdmittedCandidate` in a signature and never fills one in.
    assert producers == {"src/tracefold/news/release/candidate.py"}


def test_operator_scripts_call_methods_that_the_class_they_name_actually_has() -> None:
    """The failure mode a three-way split creates, caught where a checker cannot reach.

    `mypy src` guards production, and `attr-defined` is on for `tracefold.app.*` precisely so that a
    method moved out of `CandidateEvaluator` cannot keep being called on it. `tests/support` is outside
    that target, and the operator entry points there are `# pragma: no cover` because they need a live
    database — so a call to a method that moved is caught by nobody until an operator runs the script
    and gets an `AttributeError` instead of a regenerated fixture.

    This resolves the same thing mypy would: names bound to a constructor call in one of those scripts,
    and every attribute later read off them.
    """

    import importlib

    offenders: list[str] = []
    for path in sorted((ROOT / "tests" / "support").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # `from tracefold.x import C` — the only classes this can resolve, which is the point: a script
        # reaching for a class it never imported by name is not a moved-method question.
        imported = {
            alias.asname or alias.name: (node.module, alias.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tracefold.")
            for alias in node.names
        }
        # Only classes: `x = some_function(...)` binds a return value, whose type this walk cannot know.
        classes = {
            local: obj
            for local, (module, original) in imported.items()
            if isinstance(obj := getattr(importlib.import_module(module), original, None), type)
        }
        for scope in ast.walk(tree):
            if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            bound: dict[str, str] = {}
            for node in ast.walk(scope):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id in classes
                ):
                    bound[node.targets[0].id] = node.value.func.id
            for node in ast.walk(scope):
                if not isinstance(node, ast.Attribute):
                    continue
                # `name.attr` where name is a local instance, and the chained `C(...).attr`.
                if isinstance(node.value, ast.Name) and node.value.id in bound:
                    class_name = bound[node.value.id]
                elif (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id in classes
                ):
                    class_name = node.value.func.id
                else:
                    continue
                if not _resolves(classes[class_name], node.attr):
                    offenders.append(f"{path.relative_to(ROOT).as_posix()}: {class_name}.{node.attr}")
    assert offenders == []


def _resolves(owner: type, name: str) -> bool:
    """What an instance of `owner` answers to.

    `hasattr` on the class is not that: a pydantic field and a dataclass field are declared on the class
    and only materialise on an instance, so a check built on `hasattr` alone reports every `window.from_ms`
    as missing and the guard becomes noise nobody reads.
    """

    if hasattr(owner, name):
        return True
    if name in getattr(owner, "model_fields", {}) or name in getattr(owner, "__dataclass_fields__", {}):
        return True
    return any(name in vars(base).get("__annotations__", {}) for base in owner.__mro__)
