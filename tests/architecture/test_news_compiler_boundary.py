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


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules.add(str(node.module or ""))
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
