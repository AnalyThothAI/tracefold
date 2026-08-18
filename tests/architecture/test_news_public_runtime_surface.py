"""News V3 runtime boundaries: broker adapter isolation, pure modules, read-only serve/tool seams."""

from __future__ import annotations

import ast
from pathlib import Path

from tracefold import news

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"
NEWS_ROOT = SRC / "news"

PUBLIC_NEWS_INTERFACE = {
    "ANALYST_POLICY_VERSION",
    "ANALYST_PROMPT_VERSION",
    "DEFAULT_POLICY",
    "GATE_POLICY_VERSION",
    "OPENNEWS_SOURCE_ID",
    "TRIAGE_POLICY_VERSION",
    "TRIAGE_PROMPT_VERSION",
    "AnalystVerdict",
    "DecidePolicy",
    "NewsFeedEntry",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "TriageVerdict",
    "apply_control",
    "parse_control",
    "parse_opennews_message",
}

IO_MODULE_ROOTS = {"aio_pika", "psycopg", "httpx", "aiohttp", "websockets", "requests"}
PURE_NEWS_MODULES = (
    "gate.py",
    "storyline.py",
    "triage_rules.py",
    "analyst_rules.py",
    "control.py",
    "tokens.py",
    "minhash.py",
    "titles.py",
)
RETIRED_NEWS_MODULES = (
    "push.py",
    "runtime.py",
    "sources.py",
    "story_projection.py",
    "story_store.py",
    "projection.py",
    "projection_worker.py",
    "brief.py",
    "brief_store.py",
    "ranking.py",
    "classification.py",
    "title_presentation.py",
    "title_presentation_store.py",
    "translation.py",
)
WRITE_REPOSITORY_METHODS = (
    "insert_event",
    "insert_verdict",
    "insert_label",
    "upsert_item",
    "add_member",
    "begin_delivery",
    "settle_delivery",
    "write_control",
    "open_incident",
    "close_open_incidents",
    "complete_recovery",
    "update_ingest_state",
    "mark_event_published",
    "mark_verdict_published",
    "set_context_line",
    "record_mark",
    "expire_bands",
    "purge_before",
    "terminalize_interrupted_deliveries",
)


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
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules.add(str(node.module or ""))
    return modules


def _called_attribute_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_news_root_exposes_only_public_models_and_opennews_parser() -> None:
    assert set(news.__all__) == PUBLIC_NEWS_INTERFACE
    assert "__getattr__" not in news.__dict__
    leaked_implementation = {
        "NewsRepository",
        "NewsPipeline",
        "DeduperConsumer",
        "TriageConsumer",
        "RabbitMQBus",
        "admit_item",
        "build_story_projection",
        "rebuild_all_news_for_maintenance",
    }
    assert leaked_implementation.isdisjoint(news.__dict__)


def test_retired_news_modules_are_gone() -> None:
    present = [name for name in RETIRED_NEWS_MODULES if (NEWS_ROOT / name).exists()]
    assert present == []
    assert not (SRC / "integrations" / "news_ai.py").exists()
    assert not (SRC / "integrations" / "news_feeds" / "__init__.py").exists()
    assert not (SRC / "market" / "radar" / "__init__.py").exists()


def test_aio_pika_is_imported_only_by_the_rabbitmq_adapter() -> None:
    offenders = sorted(
        str(path.relative_to(ROOT))
        for path in SRC.rglob("*.py")
        if "aio_pika" in _imported_roots(path) and path != SRC / "integrations" / "rabbitmq.py"
    )
    assert offenders == []
    assert "aio_pika" in _imported_roots(SRC / "integrations" / "rabbitmq.py")


def test_pure_news_modules_do_not_import_io_clients() -> None:
    violations = {
        name: sorted(_imported_roots(NEWS_ROOT / name) & IO_MODULE_ROOTS)
        for name in PURE_NEWS_MODULES
        if _imported_roots(NEWS_ROOT / name) & IO_MODULE_ROOTS
    }
    assert violations == {}
    for name in PURE_NEWS_MODULES:
        modules = _imported_modules(NEWS_ROOT / name)
        assert not any(module.startswith("tracefold.news.repository") for module in modules), name
        assert not any(module.startswith("tracefold.news.consumers") for module in modules), name
        assert not any(module.startswith("tracefold.integrations") for module in modules), name


def test_news_package_does_not_import_retired_token_radar() -> None:
    offenders = sorted(
        str(path.relative_to(ROOT))
        for path in NEWS_ROOT.rglob("*.py")
        if any(module.startswith("tracefold.market.radar") for module in _imported_modules(path))
    )
    assert offenders == []


def test_analyst_evidence_bundle_and_call_are_read_only_and_tool_free() -> None:
    evidence = NEWS_ROOT / "analyst_evidence.py"
    analyst = NEWS_ROOT / "agents" / "analyst.py"
    for path in (evidence, analyst):
        modules = _imported_modules(path)
        assert not any(module.startswith(("tracefold.news.consumers", "tracefold.news.events")) for module in modules)
        assert not any(module.startswith("tracefold.integrations") for module in modules)
        assert _imported_roots(path).isdisjoint(IO_MODULE_ROOTS | {"deepagents", "langgraph", "langchain"})
        called = _called_attribute_names(path)
        assert called.isdisjoint(set(WRITE_REPOSITORY_METHODS) | {"tx", "transaction", "publish"})
    assert not (NEWS_ROOT / "agents" / "tools.py").exists()
    assert "with_structured_output" in analyst.read_text(encoding="utf-8")


def test_serve_news_routes_are_read_only_and_broker_free() -> None:
    routes = SRC / "app" / "http" / "routes_news.py"
    modules = _imported_modules(routes)
    assert not any(module.startswith(("tracefold.news.consumers", "tracefold.news.bus")) for module in modules)
    assert not any(module.startswith("tracefold.integrations.rabbitmq") for module in modules)
    assert _imported_roots(routes).isdisjoint(IO_MODULE_ROOTS)
    called = _called_attribute_names(routes)
    assert called.isdisjoint(set(WRITE_REPOSITORY_METHODS) | {"transaction", "publish"})
    assert {"list_feed", "event_detail", "status_snapshot"} <= called


def test_opennews_production_has_no_subscription_or_rest_search_path() -> None:
    integration_root = SRC / "integrations" / "opennews"
    production_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            integration_root / "__init__.py",
            integration_root / "client.py",
            SRC / "app" / "workers.py",
            NEWS_ROOT / "consumers.py",
        )
    )
    assert "news.subscribe" not in production_text
    assert "OpenNewsRestClient" not in production_text
    assert "opennews_rest_client" not in production_text
