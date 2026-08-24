import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def test_runtime_code_has_no_default_or_nested_thread_pools() -> None:
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            default_pool = name == "asyncio.to_thread"
            unbounded_pool = (
                name in {"ThreadPoolExecutor", "concurrent.futures.ThreadPoolExecutor"}
                and not node.args
                and not any(keyword.arg == "max_workers" for keyword in node.keywords)
            )
            if default_pool or unbounded_pool:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert violations == []


def test_deployment_grace_covers_the_full_workers_shutdown_budget() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))

    assert compose["services"]["serve"]["stop_grace_period"] == "40s"
    assert compose["services"]["workers"]["stop_grace_period"] == "40s"
