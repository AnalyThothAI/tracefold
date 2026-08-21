"""News V3 runtime boundaries: broker adapter isolation, pure modules, read-only serve/tool seams."""

from __future__ import annotations

import ast
from pathlib import Path

from tracefold import news

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"
NEWS_ROOT = SRC / "news"

PUBLIC_NEWS_INTERFACE = {
    "FACT_UNIT_VERSION",
    "DEFAULT_POLICY",
    "GATE_POLICY_VERSION",
    "OPENNEWS_SOURCE_ID",
    # #88: the public bounds of `/api/news/quotes` and `/api/news/review`, read by the HTTP layer from the
    # package root exactly like `status_health` rather than reaching into the pricing module.
    "QUOTE_REQUEST_SYMBOL_MAX",
    "REACTION_METRIC_VERSION",
    "READER_CONTRACT_VERSION",
    "REVIEW_DEFAULT_HOURS",
    "REVIEW_MAX_HOURS",
    "REVIEW_RUBRIC_VERSION",
    "TRIAGE_POLICY_VERSION",
    "LEARNING_EPOCH",
    "LEARNING_EPOCH_STARTED_AT_MS",
    "TRUSTED_ROOT_SHA",
    "ArmManifest",
    "BlindPairwiseSubmission",
    "CandidateEvaluator",
    "CandidateManifest",
    "ClosedWindow",
    "DatasetManifest",
    "DatasetSpec",
    "DecidePolicy",
    "DeskQuery",
    "EvaluationReport",
    "EvaluationRequest",
    "EventRubricSubmission",
    "ExternalMissSubmission",
    "FactUnit",
    "NewsFeedEntry",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "Outcome",
    "Principal",
    "ProposalReceipt",
    "ProgramTrace",
    "ProgramUsage",
    "ReaderReceipt",
    "ReviewDesk",
    "ReviewSubmission",
    "SemanticJudge",
    "SemanticJudgment",
    "TaskRef",
    "TriageVerdict",
    "TriageContext",
    "apply_control",
    "apply_canary_control",
    "event_outcome",
    "canonical_sha",
    # #87: the console's «符号落表» funnel segment folds two halves neither repository reaches across for —
    # News owns which tags an Event carried, the instrument universe owns what they name. The fold is pure,
    # so the HTTP route imports it from the package root like `status_health`.
    "grounding_rollup",
    "extract_fact_units",
    "parse_canary_control",
    "parse_control",
    "parse_opennews_message",
    "status_health",
}

IO_MODULE_ROOTS = {"aio_pika", "psycopg", "httpx", "aiohttp", "websockets", "requests"}
PURE_NEWS_MODULES = (
    "gate.py",
    "pricing.py",
    "health.py",
    "outcome.py",
    "storyline.py",
    "timeline.py",
    "triage_rules.py",
    "control.py",
    "tokens.py",
    "minhash.py",
    "titles.py",
)
RETIRED_NEWS_MODULES = (
    "analyst_evidence.py",
    "analyst_rules.py",
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
    "forget_sources_except",
    "replace_source_snapshot",
    "upsert_reaction",
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


def test_analyst_lane_is_retired() -> None:
    """Issue #57: one Event, one judgment, one card — no second model stage, no follow-up lane."""

    for retired in ("analyst_evidence.py", "analyst_rules.py"):
        assert not (NEWS_ROOT / retired).exists()
    for retired in ("analyst.py", "tools.py"):
        assert not (NEWS_ROOT / "agents" / retired).exists()
    assert not (NEWS_ROOT / "agents" / "prompts" / "NEWS_ANALYST.md").exists()
    consumers = (NEWS_ROOT / "consumers.py").read_text(encoding="utf-8")
    assert "AnalystConsumer" not in consumers
    assert "news.deep" not in consumers and "render_followup_card" not in consumers
    assert not (NEWS_ROOT / "agents" / "triage_model.py").exists()
    program = (NEWS_ROOT / "agents" / "semantic_program.py").read_text(encoding="utf-8")
    assert "EventSemantics" in program and "ReaderCard" in program and "_assemble" in program


def test_dspy_is_local_to_program_implementation_and_langchain_is_retired() -> None:
    """#129: callers depend on SemanticJudge; only Program/compiler implementation may import DSPy."""

    allowed = {
        NEWS_ROOT / "agents" / "semantic_program.py",
        NEWS_ROOT / "agents" / "program_compiler.py",
    }
    dspy_offenders = sorted(
        str(path.relative_to(ROOT))
        for path in SRC.rglob("*.py")
        if "dspy" in _imported_roots(path) and path not in allowed
    )
    assert dspy_offenders == []
    langchain_offenders = sorted(
        str(path.relative_to(ROOT))
        for path in SRC.rglob("*.py")
        if "langchain" in _imported_roots(path)
    )
    assert langchain_offenders == []


def test_reader_count_quota_interfaces_are_absent_from_runtime() -> None:
    """Policy v7 has one semantic decision plus content evidence, never a second editor based on card counts."""

    retired = {
        "hourly_cap",
        "sent_count_since",
        "reservation_status",
        "pushed_2h",
        "pushed_4h",
        "max_magnitude_2h",
        "max_magnitude_4h",
        "theme_cap_4h",
        "distinct_hard_cap_4h",
        "distinct_asset_cap_2h",
    }
    runtime = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            NEWS_ROOT / "triage_rules.py",
            NEWS_ROOT / "repository.py",
            NEWS_ROOT / "consumers.py",
            NEWS_ROOT / "candidate_evaluator.py",
            SRC / "platform" / "config" / "settings.py",
        )
    )
    assert {token for token in retired if token in runtime} == set()


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
            SRC / "app" / "workers" / "__init__.py",
            NEWS_ROOT / "consumers.py",
        )
    )
    assert "news.subscribe" not in production_text
    assert "OpenNewsRestClient" not in production_text
    assert "opennews_rest_client" not in production_text
