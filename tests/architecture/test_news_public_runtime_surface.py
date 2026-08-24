"""News V3 runtime boundaries: broker adapter isolation, pure modules, read-only serve/tool seams."""

from __future__ import annotations

import ast
from pathlib import Path

import tracefold.news.program as news_agents
from tracefold import news

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"
NEWS_ROOT = SRC / "news"

PUBLIC_NEWS_INTERFACE = {
    "EditorialEnvelope",
    "NEWS_RETRIEVAL_SHA256",
    "NewsFeedEntry",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "ProgramTrace",
    "ProgramUsage",
    "ReaderCardSemanticView",
    "ReaderReceipt",
    "ScoredJudgment",
    "SemanticJudge",
    "SemanticJudgeError",
    "SemanticJudgment",
    "TradeRelevanceV1",
    "TriageContext",
    "TriageVerdict",
}

IO_MODULE_ROOTS = {"aio_pika", "psycopg", "httpx", "aiohttp", "websockets", "requests"}
PURE_NEWS_MODULES = (
    "events/gate.py",
    "market_review/pricing.py",
    "health.py",
    "outcome.py",
    "events/storyline.py",
    "timeline.py",
    "triage_rules.py",
    "oi_signals.py",
    "events/tokens.py",
    "events/minhash.py",
    "events/titles.py",
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


def test_news_root_exposes_only_stable_values_and_ports() -> None:
    assert set(news.__all__) == PUBLIC_NEWS_INTERFACE
    assert "__getattr__" not in news.__dict__
    leaked_implementation = {
        "NewsRepository",
        "NewsPipeline",
        "DeduperConsumer",
        "TriageConsumer",
        "RabbitMQBus",
        "CandidateEvaluator",
        "DecidePolicy",
        "ReviewDesk",
        "admit_item",
        "build_story_projection",
        "canonical_sha",
        "parse_opennews_message",
        "rebuild_all_news_for_maintenance",
        "status_health",
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
        assert not any(module.startswith("tracefold.news.storage") for module in modules), name
        assert not any(module.startswith("tracefold.news.pipeline") for module in modules), name
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
        assert not (NEWS_ROOT / "program" / retired).exists()
    assert not (NEWS_ROOT / "program" / "prompts" / "NEWS_ANALYST.md").exists()
    pipeline = "\n".join(path.read_text(encoding="utf-8") for path in sorted((NEWS_ROOT / "pipeline").glob("*.py")))
    assert "AnalystConsumer" not in pipeline
    assert "news.deep" not in pipeline and "render_followup_card" not in pipeline
    assert not (NEWS_ROOT / "program" / "triage_model.py").exists()
    program = (NEWS_ROOT / "program" / "graph.py").read_text(encoding="utf-8")
    assert "EventSemantics" in program and "ReaderCard" in program and "_assemble" in program


def test_dspy_is_local_to_program_implementation_and_langchain_is_retired() -> None:
    """#129: callers depend on SemanticJudge; only Program/compiler implementation may import DSPy."""

    workers_root = SRC / "app" / "workers" / "root.py"
    workers_dspy_wiring = set((SRC / "app" / "workers" / "wiring").rglob("*.py"))
    if not workers_root.exists():
        workers_dspy_wiring.add(SRC / "app" / "workers" / "__init__.py")
    allowed = {
        NEWS_ROOT / "program" / "graph.py",
        NEWS_ROOT / "learning" / "compiler" / "root.py",
        NEWS_ROOT / "learning" / "compiler" / "proxy.py",
        # #143. The metric the optimizer maximizes and the metric an operator reads before a RulePack edit have
        # to be the same bytes, and the compiler module may be imported by exactly one runner — so the shared
        # scoring truth and the offline `dspy.Evaluate` harness live beside the Program instead.
        NEWS_ROOT / "learning" / "metric.py",
        NEWS_ROOT / "learning" / "baseline.py",
        # The code-owned GEPA instruction proposer. It travels with the compiler image and is the reason the
        # reflection model can see the RulePacks it is amending.
        NEWS_ROOT / "learning" / "proposer.py",
        # #148. The semantic-equivalence judge is part of the *metric*, not the Program: it never renders into
        # a prompt, never enters the QualityKernel, and cannot change `program_sha256`.
        NEWS_ROOT / "learning" / "judge.py",
        # The review drafter. Also metric-side: it proposes rubrics for a human to accept and has no write
        # authority anywhere — see `test_the_drafter_writes_nothing_to_the_review_plane`.
        NEWS_ROOT / "learning" / "review_drafter.py",
        # #104: the Trading lane's single `dspy.Predict`. It is the one online decision outside News
        # that calls a model, and it lives in its own capability rather than being smuggled into a
        # News module. DeepAgents/LangGraph/ReAct stay absent tree-wide, which the next assertion keeps.
        SRC / "trading" / "decision" / "program.py",
        # #104: the App wiring builds the Trading LM the same way it builds the News ones. The Workers
        # lifecycle root is intentionally excluded; PR 2 moves construction under `workers/wiring/`.
    } | workers_dspy_wiring
    dspy_offenders = sorted(
        str(path.relative_to(ROOT))
        for path in SRC.rglob("*.py")
        if "dspy" in _imported_roots(path) and path not in allowed
    )
    assert dspy_offenders == []
    langchain_offenders = sorted(
        str(path.relative_to(ROOT)) for path in SRC.rglob("*.py") if "langchain" in _imported_roots(path)
    )
    assert langchain_offenders == []


def test_semantic_judge_contract_has_public_locality() -> None:
    """#134: callers learn the framework-neutral Interface from ``tracefold.news`` only."""

    for symbol in (
        "SemanticJudge",
        "SemanticJudgeError",
        "TriageContext",
        "SemanticJudgment",
        "ProgramTrace",
        "ProgramUsage",
    ):
        assert symbol in news.__all__
        assert getattr(news, symbol) is not None
        assert not hasattr(news_agents, symbol)


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
            *(sorted((NEWS_ROOT / "storage").glob("*.py"))),
            *(sorted((NEWS_ROOT / "pipeline").glob("*.py"))),
            NEWS_ROOT / "learning" / "evaluator.py",
            SRC / "platform" / "config" / "models.py",
        )
    )
    assert {token for token in retired if token in runtime} == set()


def test_serve_news_routes_are_read_only_and_broker_free() -> None:
    routes = [SRC / "app" / "http" / "routes" / name for name in ("feed.py", "events.py", "review.py", "status.py")]
    modules = set().union(*(_imported_modules(path) for path in routes))
    assert not any(module.startswith(("tracefold.news.pipeline", "tracefold.news.bus")) for module in modules)
    assert not any(module.startswith("tracefold.integrations.rabbitmq") for module in modules)
    assert set().union(*(_imported_roots(path) for path in routes)).isdisjoint(IO_MODULE_ROOTS)
    called = set().union(*(_called_attribute_names(path) for path in routes))
    assert called.isdisjoint(set(WRITE_REPOSITORY_METHODS) | {"transaction", "publish"})
    assert {"list_feed", "event_detail", "status_snapshot"} <= called


def test_opennews_production_has_no_subscription_or_rest_search_path() -> None:
    production_text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (SRC / "integrations" / "opennews", SRC / "app" / "workers", NEWS_ROOT)
        for path in sorted(root.rglob("*.py"))
    )
    assert "news.subscribe" not in production_text
    assert "OpenNewsRestClient" not in production_text
    assert "opennews_rest_client" not in production_text
