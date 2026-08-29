"""#189: business semantics choose the runtime; no generic External Data runtime chooses the business."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import get_args, get_type_hints

from tracefold.news.pipeline.root import NewsPipeline
from tracefold.trading.capital_lane import CapitalLane

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"
SEMANTIC_CLASSES = {"capital_truth", "derived_work", "durable_event", "latest_state"}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots.add(str(node.module or "").split(".")[0])
    return roots


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _production_stage_types() -> set[type[object]]:
    """Every business stage the Workers process composes, discovered rather than listed.

    Trading contributes exactly one stage since #331: the capital lane is a deep module, not a
    pipeline of runners, so it is named here directly instead of being read off a container's
    annotations.
    """

    stages: set[type[object]] = {CapitalLane}
    for annotation in get_type_hints(NewsPipeline).values():
        for candidate in get_args(annotation) or (annotation,):
            if (
                isinstance(candidate, type)
                and candidate.__module__.startswith(("tracefold.news", "tracefold.trading"))
                and not getattr(candidate, "_is_protocol", False)
            ):
                stages.add(candidate)
    return stages


def _emits_external_data_telemetry(stage: type[object]) -> bool:
    path = Path(inspect.getfile(stage))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    declaration = next(
        node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == stage.__name__
    )
    return any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "observe_provider_call")
            or (isinstance(node.func, ast.Attribute) and node.func.attr.startswith("record_external_data_"))
        )
        for node in ast.walk(declaration)
    )


def test_production_composition_requires_explicit_external_data_classification_and_telemetry() -> None:
    """Composition discovers stages independently; annotations cannot hide an incomplete new collector."""

    missing: list[str] = []
    invalid: dict[str, list[str]] = {}
    missing_telemetry: list[str] = []
    exempt: dict[str, str] = {}
    observed: set[str] = set()
    for stage in _production_stage_types():
        key = f"{stage.__module__}.{stage.__name__}"
        reason = getattr(stage, "external_data_exempt_reason", None)
        if reason is not None:
            exempt[key] = str(reason)
            continue
        values = [str(value) for value in getattr(stage, "work_semantics", ())]
        if not values:
            missing.append(key)
            continue
        observed.update(values)
        unexpected = sorted(set(values) - SEMANTIC_CLASSES)
        if unexpected:
            invalid[key] = unexpected
        if set(values) != {"durable_event"} and not _emits_external_data_telemetry(stage):
            missing_telemetry.append(key)

    assert missing == []
    assert invalid == {}
    assert missing_telemetry == []
    assert set(exempt.values()) == {"internal_maintenance"}
    assert observed == SEMANTIC_CLASSES


def test_database_callbacks_cannot_be_async_provider_or_model_calls() -> None:
    """Business DB ports take sync callbacks, so awaited external I/O stays outside their checkout."""

    violations: list[str] = []
    for owner in ("news", "trading"):
        for path in (SRC / owner).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            async_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)}
            for node in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
                if not (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"read", "tx"}
                    and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    continue
                callback = node.args[1]
                if (
                    isinstance(callback, ast.Call)
                    and isinstance(callback.func, ast.Attribute)
                    and callback.func.attr == "partial"
                    and callback.args
                ):
                    callback = callback.args[0]
                callback_name = (
                    callback.id
                    if isinstance(callback, ast.Name)
                    else callback.attr
                    if isinstance(callback, ast.Attribute)
                    else None
                )
                if callback_name in async_names or any(isinstance(item, ast.Await) for item in ast.walk(callback)):
                    violations.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")

    assert violations == []


def test_business_packages_keep_provider_network_runtimes_behind_the_app_integration_seam() -> None:
    provider_runtime_roots = {
        "aio_pika",
        "aiohttp",
        "apscheduler",
        "binance",
        "ccxt",
        "celery",
        "dramatiq",
        "httpx",
        "hyperliquid",
        "nautilus_trader",
        "requests",
        "temporalio",
        "websockets",
    }
    violations = {
        path.relative_to(ROOT).as_posix(): sorted(_imported_roots(path) & provider_runtime_roots)
        for owner in ("news", "trading")
        for path in (SRC / owner).rglob("*.py")
        if _imported_roots(path) & provider_runtime_roots
    }

    assert violations == {}


def test_rabbitmq_is_composed_for_durable_news_only_at_the_app_seam() -> None:
    app_news_wiring = SRC / "app" / "workers" / "wiring" / "news.py"
    assert "tracefold.integrations.rabbitmq" in _imported_modules(app_news_wiring)

    violations = [
        path.relative_to(ROOT).as_posix()
        for path in (SRC / "news" / "market_review").rglob("*.py")
        if "tracefold.integrations.rabbitmq" in _imported_modules(path)
    ]

    assert violations == []
