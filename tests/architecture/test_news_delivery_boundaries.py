"""#759 delivery scheduling cannot regain provider or notification policy ownership."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "tracefold" / "news" / "pipeline"
DECISION_MODULES = {
    "tracefold.news.notifications.policy",
    "tracefold.news.notifications.planner",
    "tracefold.news.notifications.reader",
    "tracefold.news.notifications.novelty",
}


def _tree(module: str) -> ast.Module:
    return ast.parse((PIPELINE / f"{module}.py").read_text(encoding="utf-8"))


def _import_names(tree: ast.Module) -> set[str]:
    imports: set[str] = set()
    package = "tracefold.news.pipeline"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = ".".join(package.split(".")[: 1 - node.level]) if node.level > 1 else package
            base = ".".join(part for part in (prefix, node.module) if part) if node.level else node.module or ""
            imports.add(base)
            imports.update(f"{base}.{alias.name}" for alias in node.names)
    return imports


def _imports(module: str) -> set[str]:
    return _import_names(_tree(module))


def _has_prefix(imports: set[str], prefixes: set[str]) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for name in imports for prefix in prefixes)


def _assert_no_forbidden_imports(imports: set[str], forbidden: set[str]) -> None:
    assert not _has_prefix(imports, forbidden), sorted(imports)


def test_notification_scheduler_owns_no_render_quote_or_provider_work() -> None:
    forbidden = {
        "tracefold.news.delivery",
        "tracefold.news.delivery_contracts",
        "tracefold.news.feishu_card",
        "tracefold.news.reader_card",
        "tracefold.news.tradability",
        "tracefold.news.market_review.pricing",
    }
    imports = _imports("delivery")
    assert not _has_prefix(imports, forbidden)
    assert not any(name.startswith(("tracefold.integrations", "httpx", "requests")) for name in imports)
    provider_calls = {
        node.func.attr
        for node in ast.walk(_tree("delivery"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } & {"preflight", "send", "send_card", "edit_card", "send_reserved_card", "send_prepared_card"}
    assert provider_calls == set(), sorted(provider_calls)


def test_provider_adapter_cannot_reinterpret_news_value_or_novelty() -> None:
    _assert_no_forbidden_imports(_imports("notification_sender"), DECISION_MODULES)
    _assert_no_forbidden_imports(_imports("send_entry"), DECISION_MODULES)
    assert not _has_prefix(_imports("delivery_quotes"), {"tracefold.news.notifications"})


def test_callers_use_the_public_shared_entry_reservation() -> None:
    for module in ("delivery", "notification_sender", "delivery_enrichment"):
        private_accesses = [
            node.attr
            for node in ast.walk(_tree(module))
            if isinstance(node, ast.Attribute)
            and node.attr.startswith("_")
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "send_entry"
        ]
        assert private_accesses == [], (module, private_accesses)


def test_boundary_scan_rejects_relative_absolute_and_namespace_decision_imports() -> None:
    for module in ("policy", "planner", "reader", "novelty"):
        for source in (
            f"from ..notifications.{module} import decide",
            f"from tracefold.news.notifications.{module} import decide",
            f"from ..notifications import {module}",
        ):
            imports = _import_names(ast.parse(source))
            with pytest.raises(AssertionError):
                _assert_no_forbidden_imports(imports, DECISION_MODULES)
