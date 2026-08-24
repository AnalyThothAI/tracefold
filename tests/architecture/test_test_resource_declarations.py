from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "tests" / "integration"
POSTGRES_HELPERS = {
    "connect_postgres_test",
    "postgres_settings_storage",
    "prepare_postgres_database",
    "reset_postgres_schema",
    "test_postgres_dsn",
}


def _fixture_is_declared(node: ast.AST, tree: ast.Module, fixture: str) -> bool:
    if isinstance(node, ast.Module) and any(
        fixture in {arg.arg for arg in (*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs)}
        for owner in ast.walk(node)
        if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        return True
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if fixture in {arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}:
            return True
        if any(fixture in ast.unparse(decorator) for decorator in node.decorator_list):
            return True
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and fixture in ast.unparse(statement):
            return True
    return False


def test_postgres_helpers_are_used_only_beneath_an_explicit_resource_fixture() -> None:
    violations: list[str] = []
    for path in sorted(INTEGRATION.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "tests.postgres_test_utils":
                continue
            aliases.update(alias.asname or alias.name for alias in node.names if alias.name in POSTGRES_HELPERS)
        if not aliases:
            continue
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            if call.func.id not in aliases:
                continue
            owner: ast.AST = call
            while owner in parents and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = parents[owner]
            if not _fixture_is_declared(owner, tree, "postgres_dsn"):
                violations.append(f"{path.relative_to(ROOT)}:{call.lineno}")

    assert violations == []


def test_rabbitmq_integration_uses_an_explicit_fixture_and_no_collection_time_skip() -> None:
    violations: list[str] = []
    for path in sorted(INTEGRATION.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rabbit_bus_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "tracefold.integrations.rabbitmq"
            for alias in node.names
            if alias.name == "RabbitMQBus"
        }
        owns_broker = any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in rabbit_bus_aliases
            for node in ast.walk(tree)
        )
        if not owns_broker:
            continue
        if not _fixture_is_declared(tree, tree, "rabbitmq_url"):
            violations.append(f"{path.relative_to(ROOT)}:missing rabbitmq_url fixture")
        if any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "pytest"
            and node.value.attr == "mark"
            and node.attr == "skipif"
            for node in ast.walk(tree)
        ):
            violations.append(f"{path.relative_to(ROOT)}:collection-time broker skip")

    assert violations == []


def test_resource_fixtures_are_never_started_by_directory_collection() -> None:
    for path in sorted((ROOT / "tests").glob("**/conftest.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        fixture_decorators = (
            decorator
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in node.decorator_list
            if "fixture" in ast.unparse(decorator)
        )
        assert all("autouse=True" not in ast.unparse(decorator).replace(" ", "") for decorator in fixture_decorators)
