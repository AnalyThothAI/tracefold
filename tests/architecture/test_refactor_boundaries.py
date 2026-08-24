"""Long-lived semantic boundaries retained after the Issue #162 migration."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"


def _production_files() -> list[Path]:
    return sorted(
        path
        for path in SRC.rglob("*.py")
        if "__pycache__" not in path.parts and "alembic/versions" not in path.as_posix()
    )


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(("tracefold", *parts))


def _resolved_from_module(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    importer_parts = _module_name(path).split(".")
    package_parts = importer_parts if path.name == "__init__.py" else importer_parts[:-1]
    keep = len(package_parts) - (node.level - 1)
    if keep < 0:
        return ""
    suffix = (node.module or "").split(".") if node.module else []
    return ".".join((*package_parts[:keep], *suffix))


def _module_exists(module: str) -> bool:
    if not module.startswith("tracefold."):
        return False
    relative = module.split(".")[1:]
    return SRC.joinpath(*relative).with_suffix(".py").exists() or SRC.joinpath(*relative, "__init__.py").exists()


def _import_targets(path: Path, tree: ast.AST) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolved_from_module(path, node)
            if resolved:
                imports.add(resolved)
                imports.update(f"{resolved}.{alias.name}" for alias in node.names if alias.name != "*")
    return imports


TYPE_EXPRESSION_NODES = (
    ast.Attribute,
    ast.BinOp,
    ast.BitOr,
    ast.Constant,
    ast.Load,
    ast.Name,
    ast.Subscript,
    ast.Tuple,
)


def test_package_initializers_are_declarative() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("__init__.py")):
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for index, node in enumerate(tree.body):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                violations.append(f"{relative}:{type(node).__name__}:{node.lineno}")
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if len(targets) != 1 or not isinstance(targets[0], ast.Name) or value is None:
                violations.append(f"{relative}:non_declarative_assignment:{node.lineno}")
                continue
            if targets[0].id == "__all__":
                try:
                    exported = ast.literal_eval(value)
                except (ValueError, TypeError):
                    exported = None
                if not isinstance(exported, (list, tuple)) or not all(isinstance(item, str) for item in exported):
                    violations.append(f"{relative}:non_literal_all:{node.lineno}")
                continue
            try:
                ast.literal_eval(value)
            except (ValueError, TypeError):
                if not all(isinstance(child, TYPE_EXPRESSION_NODES) for child in ast.walk(value)):
                    violations.append(f"{relative}:non_literal_assignment:{node.lineno}")
    assert violations == []


def test_workers_root_owns_lifecycle_but_not_capability_construction() -> None:
    root = SRC / "app" / "workers" / "root.py"
    forbidden = (
        "dspy",
        "tracefold.app.learning_runtime",
        "tracefold.app.llm",
        "tracefold.integrations",
        "tracefold.news",
        "tracefold.trading",
    )
    imports = _import_targets(root, ast.parse(root.read_text(encoding="utf-8"), filename=str(root)))
    assert sorted(imported for imported in imports if imported.startswith(forbidden)) == []


BUSINESS_WRITE_SQL_RE = re.compile(
    r"\b(?:DELETE\s+FROM|INSERT\s+INTO|UPDATE)\s+(?P<table>(?:news|trading)_[a-z0-9_]*)",
    re.IGNORECASE,
)


def test_app_composes_capabilities_but_never_writes_their_facts() -> None:
    violations = [
        f"{path.relative_to(ROOT).as_posix()}:{table}"
        for path in _production_files()
        if path.relative_to(SRC).parts[0] == "app"
        for table in BUSINESS_WRITE_SQL_RE.findall(path.read_text(encoding="utf-8"))
    ]
    assert violations == []


def _top_level_import_targets(path: Path, tree: ast.Module) -> set[str]:
    statements: list[ast.stmt] = list(tree.body)
    for node in tree.body:
        if isinstance(node, ast.If):
            statements.extend(node.body)
    imports: set[str] = set()
    for node in statements:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolved_from_module(path, node)
            if not resolved or not _module_exists(resolved):
                continue
            imports.add(resolved)
            imports.update(
                candidate
                for alias in node.names
                if alias.name != "*"
                if _module_exists(candidate := f"{resolved}.{alias.name}")
            )
    return imports


def _internal_import_graph(package: str) -> dict[str, set[str]]:
    prefix = f"tracefold.{package}."
    graph: dict[str, set[str]] = {}
    for path in _production_files():
        if path.relative_to(SRC).parts[0] != package:
            continue
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        graph[module] = {imported for imported in _top_level_import_targets(path, tree) if imported.startswith(prefix)}
    return {module: {target for target in targets if target in graph} for module, targets in graph.items()}


def _import_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    order: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []
    counter = 0

    def visit(node: str) -> None:
        nonlocal counter
        order[node] = low[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(graph[node]):
            if target not in order:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], order[target])
        if low[node] != order[node]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        if len(component) > 1 or node in graph[node]:
            cycles.append(sorted(component))

    for module in sorted(graph):
        if module not in order:
            visit(module)
    return cycles


def test_owning_packages_have_acyclic_internal_import_graphs() -> None:
    assert {package: _import_cycles(_internal_import_graph(package)) for package in ("app", "news", "trading")} == {
        "app": [],
        "news": [],
        "trading": [],
    }


CROSS_CONTEXT_BOUNDARY_MODULES = (
    "app/workers/wiring/news_to_trading.py",
    "news/pipeline/runtime.py",
    "news/storage/trade_projection.py",
    "trading/pipeline/runtime.py",
)


def _terminal_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_any_mapping(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and _terminal_name(node.value) in {"dict", "Mapping"}
        and any(_terminal_name(child) == "Any" for child in ast.walk(node.slice))
    )


def test_cross_context_boundaries_carry_named_typed_rows() -> None:
    violations = [
        f"{relative}:{node.lineno}:{ast.unparse(node)}"
        for relative in CROSS_CONTEXT_BOUNDARY_MODULES
        for node in ast.walk(ast.parse((SRC / relative).read_text(encoding="utf-8"), filename=relative))
        if _is_any_mapping(node)
    ]
    assert violations == []


def test_news_to_trading_mapper_matches_the_projection_version() -> None:
    from tracefold.app.workers.wiring.news_to_trading import MAPPED_NEWS_PROJECTION_VERSION
    from tracefold.news.storage.trade_projection import NEWS_TRADE_PROJECTION_VERSION

    assert MAPPED_NEWS_PROJECTION_VERSION == NEWS_TRADE_PROJECTION_VERSION


APP_DATABASE_METHODS = frozenset({"worker_session", "run_news", "run_business", "run_control", "heavy_business"})


def test_business_packages_never_reach_for_an_app_database_method() -> None:
    violations = [
        f"{path.relative_to(SRC).as_posix()}:{node.lineno}:{node.attr}"
        for path in _production_files()
        if path.relative_to(SRC).parts[0] in ("news", "trading")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if isinstance(node, ast.Attribute) and node.attr in APP_DATABASE_METHODS
    ]
    assert violations == []


def test_build_time_python_probes_resolve_real_modules() -> None:
    probed = re.findall(r"from (tracefold\.[\w.]+) import", (ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert probed
    assert [module for module in probed if not _module_exists(module)] == []
