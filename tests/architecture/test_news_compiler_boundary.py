"""Import boundary for the cold optimizer and its privileged host seam."""

from __future__ import annotations

import ast
from pathlib import Path

from tracefold.news.learning.compiler.source_identity import compiler_source_sha256, proxy_source_sha256

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"
CLI_NEWS = tuple(sorted((SRC / "app" / "cli" / "commands").glob("news_*.py")))
COMPILER = SRC / "news" / "learning" / "compiler"
OPTIMIZER_MODULE = "tracefold.news.learning.compiler.root"
GEPA_CORE_MODULE = "tracefold.news.learning.compiler.gepa"
# #193 PR-C. The experiment loop runs GEPA in this process on purpose, and it is its own module for exactly
# that reason. It is not the trusted seam: it holds no promotion authority, reads the database once as
# `serve`, writes only into an operator-owned run directory, and emits a candidate no gate can accept. The
# trusted seam below stays clean, and `compile` still runs the optimizer only inside the container.
EXPERIMENT_CLI = SRC / "app" / "cli" / "commands" / "news_learning_experiment.py"


def _package(path: Path) -> list[str]:
    """The dotted package this file lives in, so a relative import can be read as the module it means.

    Dropping the last part is right for both kinds of file: `news/foo.py` and `news/__init__.py` both sit
    in the package `tracefold.news`, which is what `from . import x` resolves against in either one.
    """

    return list(path.relative_to(SRC.parent).with_suffix("").parts[:-1])


def _imports(path: Path) -> set[str]:
    """Every module this file names, relative ones resolved.

    Resolution is the point. `from ..compiler.root import ...` reaches the trusted optimizer exactly as
    surely as the absolute spelling does, and a boundary that only reads absolute imports is a boundary
    one keystroke gets around.
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


def test_optimizer_is_imported_only_by_the_fixed_container_runner() -> None:
    importers = {path.relative_to(ROOT).as_posix() for path in SRC.rglob("*.py") if OPTIMIZER_MODULE in _imports(path)}
    assert importers == {"src/tracefold/news/learning/compiler/runner.py"}


def test_host_cli_and_trusted_seam_do_not_import_optimizer_or_gepa() -> None:
    for path in (*CLI_NEWS, COMPILER / "trusted.py", COMPILER / "launcher.py"):
        modules = _imports(path)
        assert OPTIMIZER_MODULE not in modules, path
        assert "gepa" not in modules, path
        assert "dspy" not in modules, path


def test_only_the_experiment_cli_reaches_the_in_process_optimizer_core() -> None:
    """One named exception, asserted rather than assumed.

    The string checks above would let a new CLI module import the shared core unnoticed, because
    `tracefold.news.learning.compiler.gepa` is not the literal string `"gepa"`. This says which module is
    allowed to, so adding a second one is a decision someone has to make on purpose.
    """

    importers = {path.relative_to(ROOT).as_posix() for path in CLI_NEWS if GEPA_CORE_MODULE in _imports(path)}
    assert importers == {EXPERIMENT_CLI.relative_to(ROOT).as_posix()}


def test_the_experiment_plane_cannot_reach_promotion_or_the_trusted_compile_seam() -> None:
    """Research scaffolding, structurally: it may read the corpus, and it names nothing that can ship.

    This proves the absence of a direct import, not the absence of a write — the experiment plane reaches
    the corpus through `CandidateEvaluator`, and a determined caller could reach further through it. What
    it does buy is that promoting a winner cannot be written here by accident: registering a candidate,
    launching a container and moving a canary all live behind names this package is forbidden to say.
    """

    forbidden = {
        f"tracefold.news.learning.compiler.{name}"
        for name in ("launcher", "proxy", "root", "runner", "sandbox", "trusted")
    } | {"tracefold.news.learning.canary"}
    experiment = SRC / "news" / "learning" / "experiment"
    for path in (EXPERIMENT_CLI, *sorted(experiment.rglob("*.py"))):
        modules = _imports(path)
        assert not (modules & forbidden), (path, sorted(modules & forbidden))
        assert not any(module.startswith("tracefold.news.storage") for module in modules), path


def test_compiler_source_identity_includes_package_initializer_before_secrets_mount(tmp_path: Path) -> None:
    tracefold = tmp_path / "tracefold"
    news = tracefold / "news"
    commands = tracefold / "app" / "cli" / "commands"
    commands.mkdir(parents=True)
    news.mkdir()
    (tracefold / "__init__.py").write_text("PACKAGE_SENTINEL = 'reviewed'\n", encoding="utf-8")
    (news / "__init__.py").write_text("NEWS_SENTINEL = 'reviewed'\n", encoding="utf-8")
    (commands / "news_learning.py").write_text("CLI_SENTINEL = 'reviewed'\n", encoding="utf-8")
    (commands.parent / "parser.py").write_text("PARSER_SENTINEL = 'reviewed'\n", encoding="utf-8")

    compiler_before = compiler_source_sha256(tracefold_root=tracefold)
    proxy_before = proxy_source_sha256(tracefold_root=tracefold)
    (tracefold / "__init__.py").write_text("PACKAGE_SENTINEL = 'malicious-before-import'\n", encoding="utf-8")

    assert compiler_source_sha256(tracefold_root=tracefold) != compiler_before
    assert proxy_source_sha256(tracefold_root=tracefold) != proxy_before

    (tracefold / "__init__.py").write_text("PACKAGE_SENTINEL = 'reviewed'\n", encoding="utf-8")
    (commands / "news_learning.py").write_text("CLI_SENTINEL = 'malicious-before-import'\n", encoding="utf-8")

    assert compiler_source_sha256(tracefold_root=tracefold) != compiler_before
    assert proxy_source_sha256(tracefold_root=tracefold) != proxy_before
