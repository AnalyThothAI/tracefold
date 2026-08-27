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
    "EVENT_KINDS",
    "EditorialEnvelope",
    "EventKind",
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
    "SourceContractReason",
    "TradeRelevanceV1",
    "TriageContext",
    "TriageVerdict",
}

IO_MODULE_ROOTS = {"aio_pika", "psycopg", "httpx", "aiohttp", "websockets", "requests"}
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
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                imported = str(node.module or "")
            else:
                relative = path.relative_to(SRC).with_suffix("")
                package = ("tracefold", *relative.parts[:-1])
                keep = len(package) - (node.level - 1)
                suffix = tuple((node.module or "").split(".")) if node.module else ()
                imported = ".".join((*package[:keep], *suffix))
            if imported:
                modules.add(imported)
                modules.update(f"{imported}.{alias.name}" for alias in node.names if alias.name != "*")
    return modules


def _under(module: str, owners: tuple[str, ...]) -> bool:
    return any(module == owner or module.startswith(f"{owner}.") for owner in owners)


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


def test_aio_pika_is_imported_only_by_the_rabbitmq_adapter() -> None:
    offenders = sorted(
        str(path.relative_to(ROOT))
        for path in SRC.rglob("*.py")
        if "aio_pika" in _imported_roots(path) and path != SRC / "integrations" / "rabbitmq.py"
    )
    assert offenders == []
    assert "aio_pika" in _imported_roots(SRC / "integrations" / "rabbitmq.py")


def test_news_value_families_do_not_depend_on_io_or_runtime_owners() -> None:
    value_modules = sorted((*NEWS_ROOT.glob("*.py"), *(NEWS_ROOT / "events").rglob("*.py")))
    violations: dict[str, list[str]] = {}
    for path in value_modules:
        forbidden = sorted(_imported_roots(path) & IO_MODULE_ROOTS)
        forbidden.extend(
            module
            for module in sorted(_imported_modules(path))
            if _under(module, ("tracefold.integrations", "tracefold.news.pipeline", "tracefold.news.storage"))
        )
        if forbidden:
            violations[path.relative_to(NEWS_ROOT).as_posix()] = forbidden
    assert violations == {}


def test_dspy_is_confined_to_model_implementation_families() -> None:
    allowed_roots = (
        NEWS_ROOT / "program",
        NEWS_ROOT / "learning",
        # #202 §4.3. The review plane acquires human truth; the drafter is the one thing in it that asks a
        # model first, so a person has a rubric to accept or rewrite rather than a blank form. The companion
        # test below keeps that to the one module — a ReviewDesk that could call a model would be a desk
        # that could manufacture its own Gold.
        NEWS_ROOT / "review",
        SRC / "trading" / "decision",
        SRC / "app" / "workers" / "wiring",
    )
    offenders = [
        str(path.relative_to(ROOT))
        for path in SRC.rglob("*.py")
        if "dspy" in _imported_roots(path) and not any(root in path.parents for root in allowed_roots)
    ]
    assert offenders == []


def test_only_the_drafter_may_call_a_model_inside_the_review_plane() -> None:
    review = NEWS_ROOT / "review"
    callers = {path.name for path in review.rglob("*.py") if "dspy" in _imported_roots(path)}
    assert callers == {"drafter.py"}


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


def test_serve_news_routes_are_read_only_and_broker_free() -> None:
    # #256 removed `review.py` with the console page it served, and with it the only two writes the public
    # surface had. Every module named here is now a read, which is what the assertions below already said.
    routes = [SRC / "app" / "http" / "routes" / name for name in ("feed.py", "events.py", "status.py")]
    modules = set().union(*(_imported_modules(path) for path in routes))
    assert not any(_under(module, ("tracefold.news.pipeline", "tracefold.news.bus")) for module in modules)
    assert not any(_under(module, ("tracefold.integrations.rabbitmq",)) for module in modules)
    assert set().union(*(_imported_roots(path) for path in routes)).isdisjoint(IO_MODULE_ROOTS)
    called = set().union(*(_called_attribute_names(path) for path in routes))
    assert called.isdisjoint(set(WRITE_REPOSITORY_METHODS) | {"transaction", "publish"})
    assert {"list_feed", "event_detail", "status_snapshot"} <= called
