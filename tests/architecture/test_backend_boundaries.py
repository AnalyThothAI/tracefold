from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"
BUSINESS_PACKAGES = ("news",)
ALLOWED_BUSINESS_DEPENDENCIES = {
    "news": {"news", "platform"},
}
# Exact private seams for the composition root and concrete adapters. They are
# implementation collaborators of the public News capabilities, not
# product callers or compatibility interfaces; every new edge must be named.
ALLOWED_INTERNAL_BUSINESS_IMPORTS = {
    "src/tracefold/app/cli/commands/news.py": {
        # #143: the offline baseline harness. The CLI reads the corpus and prints the receipt; DSPy stays
        # behind this module, so the "no dspy in the CLI layer" boundary is unaffected.
        "tracefold.news.agents.program_baseline",
        "tracefold.news.agents.program_compiler_launcher",
        "tracefold.news.agents.program_compiler_proxy",
        "tracefold.news.agents.program_compiler_sandbox",
        "tracefold.news.agents.program_compiler_security",
        "tracefold.news.agents.program_compiler_source",
        "tracefold.news.agents.program_compiler_trusted",
        "tracefold.news.agents.programs.candidates",
        "tracefold.news.agents.semantic_program",
        "tracefold.news.bus",
        "tracefold.news.candidate_evaluator",
        "tracefold.news.review",
        "tracefold.news.eval.replay",
        "tracefold.news.eval.why",
    },
    "src/tracefold/app/query_audit.py": {
        "tracefold.news.query_specs",
    },
    "src/tracefold/app/learning_runtime.py": {
        "tracefold.news.agents.semantic_program",
    },
    "src/tracefold/app/repositories.py": {
        "tracefold.news.instruments_repository",
        "tracefold.news.price_repository",
        "tracefold.news.repository",
    },
    "src/tracefold/app/workers/__init__.py": {
        "tracefold.news.agents.programs.candidates",
        "tracefold.news.agents.semantic_program",
        "tracefold.news.canary",
        "tracefold.news.consumers",
    },
    "src/tracefold/integrations/opennews/client.py": {"tracefold.news.opennews"},
    # The venue adapters are the #75 analogue of the OpenNews adapter: they parse a provider catalogue into the
    # pure `Instrument` shape the News package owns.
    "src/tracefold/integrations/venues/binance.py": {"tracefold.news.instruments"},
    # #88: the price adapters are the same seam one layer over — they parse provider quotes and candles into
    # the pure `ProviderQuote` / `Candle` shapes the News package owns.
    "src/tracefold/integrations/venues/candles.py": {"tracefold.news.pricing"},
    "src/tracefold/integrations/venues/quotes.py": {"tracefold.news.pricing"},
    "src/tracefold/integrations/venues/hyperliquid.py": {"tracefold.news.instruments"},
    "src/tracefold/integrations/venues/us_reference.py": {"tracefold.news.instruments"},
    "src/tracefold/integrations/rabbitmq.py": {"tracefold.news.bus"},
}
# News V3 cross-domain reads: none since the Analyst lane was retired (#57). Every edge
# would have to be named here; no News module may write another business package's tables.
ALLOWED_READ_ONLY_CROSS_DOMAIN_TABLES: dict[str, set[str]] = {}
WRITE_SQL_TABLE_RE = re.compile(
    r"\b(?:DELETE\s+FROM|INSERT\s+INTO|UPDATE)\s+(?P<table>[a-z][a-z0-9_]*)",
    re.IGNORECASE,
)
FORBIDDEN_CURRENT_IDENTITY_PARTS = {
    "attempt_id",
    "computed_at_ms",
    "generation",
    "generation_id",
    "published_at_ms",
    "run_id",
    "snapshot_id",
}
SCHEMA_TABLE_RE = re.compile(r"^## `(?P<table>[a-z][a-z0-9_]*)`$", re.MULTILINE)
SQL_TABLE_RE = re.compile(
    r"\b(?:DELETE\s+FROM|INSERT\s+INTO|FROM|JOIN|UPDATE)\s+(?P<table>[a-z][a-z0-9_]*)",
    re.IGNORECASE,
)
PLATFORM_TABLES = {
    "alembic_version",
    "workers_runtime",
}
RETIRED_NEWS_RUNTIME_MARKERS = (
    "news_item_process",
    "news_page_projection",
    "news_page_rows",
    "news_provider_items",
    "news_projection_dirty_targets",
    "news_intel",
    "cryptopanic",
    "NewsAnalysisWorker",
    "StoryInterface",
    "news_story_articles",
    "news_story_analyses",
    "news_story_analysis_attempts",
    "news_analysis",
    "verification_status",
    "evidence_set_hash",
    "news_brief_plan",
    "news_ai_publish",
    "NewsStoryProjectWorker",
    "NewsBriefPlanWorker",
    "NewsAiPublishWorker",
    "NewsStoryTitleTranslation",
    "news_story_title_translations",
    "news_title_translation",
    "title_translation_configured",
    "rebuild-news-stories",
    "/api/news/items",
    "/api/news/sources/status",
    "/api/news/stories?",
    "/api/news/brief/history",
    "/analysis/requests",
    "/news/items",
    "NewsInterface",
    "news_story_source_facets",
    "news_story_category_facets",
    "news_brief_runs",
    "news_brief_publications",
    "refresh_projection_summary_for_maintenance",
)


def _python_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts and "alembic/versions" not in path.as_posix()
    )


def _imports(path: Path) -> set[str]:
    imports: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_backend_has_only_the_expected_package_shape() -> None:
    assert {path.name for path in SRC.iterdir() if path.is_dir() and path.name != "__pycache__"} == {
        "app",
        "integrations",
        "news",
        "platform",
    }
    assert not (SRC / "domains").exists()
    assert not (SRC / "market").exists()
    for retired in ("operations", "runtime", "surfaces"):
        assert not (SRC / "app" / retired).exists()
    for retired in (
        "provider_types.py",
        "providers.py",
        "market_providers.py",
        "provider_ownership.py",
        "provider_operations.py",
        "reference_data.py",
        "read_models.py",
    ):
        assert not (SRC / "app" / retired).exists()
    for retired in ("gmgn", "binance", "okx"):
        assert not (SRC / "integrations" / retired).exists()
    for retired in ("routes_search.py", "routes_events.py", "routes_market.py", "ws.py"):
        assert not (SRC / "app" / "http" / retired).exists()
    for retired in ("db", "logging", "runtime"):
        assert not (SRC / "platform" / retired).exists()
    assert not list(SRC.rglob("*_intel"))
    generic_business_layers = {"queries", "read_models", "repositories", "runtime", "services", "types"}
    assert not [
        path.relative_to(ROOT).as_posix()
        for package in BUSINESS_PACKAGES
        for path in (SRC / package).rglob("*")
        if path.is_dir() and path.name in generic_business_layers
    ]


def test_gmgn_lane_and_token_radar_are_removed_from_the_backend() -> None:
    """#47 removed Radar, #50 removed the whole GMGN lane: no social events, token identity, DEX/CEX
    market data, live WebSocket journal, or Search/Token Case readers remain anywhere in the backend."""

    markers = (
        "token_radar",
        "/token-radar",
        "tracefold.market",
        "gmgn",
        "integrations.binance",
        "usdm_futures",
        "binance_cex",
        "persisted_live",
        "market_tick",
        "registry_assets",
        "token_intent",
        "cex_token",
        "price_feeds",
        "raw_frames",
        "/api/recent",
        "/api/search",
        "/api/token-case",
        "/api/live-market",
        "@app.websocket",
        "persistedlivebroadcaster",
    )
    violations = [
        f"{path.relative_to(ROOT).as_posix()}:{marker}"
        for path in _python_files(SRC)
        for marker in markers
        if marker in path.read_text(encoding="utf-8").lower()
    ]
    assert violations == []


def test_business_dependency_dag_is_one_way() -> None:
    violations: dict[str, list[str]] = {}
    for owner, allowed in ALLOWED_BUSINESS_DEPENDENCIES.items():
        for path in _python_files(SRC / owner):
            dependencies = {
                imported.split(".")[1]
                for imported in _imports(path)
                if imported.startswith("tracefold.") and len(imported.split(".")) > 1
            }
            unexpected = sorted(dependencies - allowed)
            if unexpected:
                violations[path.relative_to(ROOT).as_posix()] = unexpected
    assert violations == {}


def test_platform_does_not_depend_on_app_business_or_integrations() -> None:
    forbidden = {"app", "integrations", *BUSINESS_PACKAGES}
    violations: dict[str, list[str]] = {}
    for path in _python_files(SRC / "platform"):
        dependencies = {
            imported.split(".")[1]
            for imported in _imports(path)
            if imported.startswith("tracefold.") and len(imported.split(".")) > 1
        }
        unexpected = sorted(dependencies & forbidden)
        if unexpected:
            violations[path.relative_to(ROOT).as_posix()] = unexpected
    assert violations == {}


def test_integrations_do_not_depend_on_app() -> None:
    violations = [
        f"{path.relative_to(ROOT)} -> {imported}"
        for path in _python_files(SRC / "integrations")
        for imported in _imports(path)
        if imported == "tracefold.app" or imported.startswith("tracefold.app.")
    ]
    assert violations == []


def test_external_consumers_use_declared_business_interfaces() -> None:
    violations: list[str] = []
    for package in BUSINESS_PACKAGES:
        prefix = f"tracefold.{package}."
        for path in _python_files(SRC):
            if path.relative_to(SRC).parts[0] == package:
                continue
            relative = path.relative_to(ROOT).as_posix()
            allowed_internal_seams = ALLOWED_INTERNAL_BUSINESS_IMPORTS.get(relative, set())
            violations.extend(
                f"{path.relative_to(ROOT)} -> {imported}"
                for imported in _imports(path)
                if imported.startswith(prefix) and imported not in allowed_internal_seams
            )
    assert violations == []


def test_business_sql_uses_only_owned_tables() -> None:
    schema = (ROOT / "docs" / "generated" / "db-schema.md").read_text(encoding="utf-8")
    tables = set(SCHEMA_TABLE_RE.findall(schema))
    table_owners = {table: _business_table_owner(table) for table in tables if table not in PLATFORM_TABLES}
    violations: list[str] = []
    for package in BUSINESS_PACKAGES:
        for path in _python_files(SRC / package):
            relative = path.relative_to(ROOT).as_posix()
            source = path.read_text(encoding="utf-8")
            read_only_allowed = ALLOWED_READ_ONLY_CROSS_DOMAIN_TABLES.get(relative, set())
            for table in SQL_TABLE_RE.findall(source):
                owner = table_owners.get(table.lower())
                if owner is not None and owner != package and table.lower() not in read_only_allowed:
                    violations.append(f"{relative} -> {table} ({owner})")
            for table in WRITE_SQL_TABLE_RE.findall(source):
                owner = table_owners.get(table.lower())
                if owner is not None and owner != package:
                    violations.append(f"{relative} writes {table} ({owner})")
    assert violations == []


def test_worker_runtime_v2_has_one_public_root_and_no_retired_framework() -> None:
    workers_source = (SRC / "app" / "workers" / "__init__.py").read_text(encoding="utf-8")
    assert '__all__ = ["run_workers"]' in workers_source
    assert "async def run_workers(" in workers_source
    retired = (
        SRC / "app" / "worker_manifest.py",
        SRC / "app" / "worker_runtime_supervisor.py",
        SRC / "app" / "worker_status.py",
        SRC / "app" / "runtime_claim_recovery.py",
        SRC / "app" / "model_generation_coordinator.py",
        SRC / "platform" / "workers" / "worker_base.py",
        SRC / "platform" / "workers" / "worker_result.py",
        SRC / "platform" / "workers" / "factory.py",
    )
    assert [path.relative_to(ROOT).as_posix() for path in retired if path.exists()] == []


def test_legacy_news_runtime_contract_is_absent_outside_migration_history() -> None:
    roots = (SRC, ROOT / "web" / "src", ROOT / "docs")
    suffixes = {".css", ".md", ".py", ".ts", ".tsx", ".yaml", ".yml"}
    violations: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            relative = path.relative_to(ROOT).as_posix()
            if "alembic/versions/" in relative or relative.startswith("docs/generated/"):
                continue
            content = path.read_text(encoding="utf-8").lower()
            violations.extend(
                f"{relative}:{marker}" for marker in RETIRED_NEWS_RUNTIME_MARKERS if marker.lower() in content
            )
    assert violations == []


def test_news_v3_has_one_pipeline_wiring_and_one_broker_adapter() -> None:
    news_source = "\n".join(path.read_text(encoding="utf-8") for path in _python_files(SRC / "news"))
    workers_source = (SRC / "app" / "workers" / "__init__.py").read_text(encoding="utf-8")

    assert news_source.count("class NewsPipeline:") == 1
    assert news_source.count("class DeduperConsumer:") == 1
    assert news_source.count("class DelivererConsumer:") == 1
    assert workers_source.count("NewsPipeline(") == 1
    assert workers_source.count("RabbitMQBus(") == 1
    assert "NewsAcquisition(" not in workers_source
    assert "NewsStoryProjectionWorker(" not in workers_source
    for retired in ("projection.py", "story_projection.py", "story_store.py", "brief.py", "push.py", "runtime.py"):
        assert not (SRC / "news" / retired).exists(), retired


def test_public_news_has_no_personalization_or_parallel_product_infrastructure() -> None:
    paths = [*_python_files(SRC / "news")]
    forbidden_import_roots = {
        "aio_pika",
        "celery",
        "chromadb",
        "faiss",
        "kafka",
        "multiprocessing",
        "pinecone",
        "qdrant_client",
        "redis",
        "sentence_transformers",
        "subprocess",
    }
    import_violations = [
        f"{path.relative_to(ROOT)} -> {imported}"
        for path in paths
        for imported in _imports(path)
        if imported.split(".")[0] in forbidden_import_roots
        and not (
            imported.split(".")[0] == "subprocess" and path == SRC / "news" / "agents" / "program_compiler_launcher.py"
        )
    ]
    assert import_violations == []

    forbidden_product_markers = (
        "personalized_filter",
        "user_preference",
        "embedding_provider",
        "vector_store",
        "topic_quota",
    )
    marker_violations = [
        f"{path.relative_to(ROOT)}:{marker}"
        for path in paths
        for marker in forbidden_product_markers
        if marker in path.read_text(encoding="utf-8").lower()
    ]
    assert marker_violations == []


def test_news_kiss_retired_tables_have_no_production_owner() -> None:
    retired_tables = {
        "news_identity_features",
        "news_similarity_edges",
        "news_story_aliases",
        "news_story_input_state",
        "news_projection_frontiers",
    }
    violations: list[str] = []
    for path in _python_files(SRC):
        source = path.read_text(encoding="utf-8")
        violations.extend(f"{path.relative_to(ROOT)}:{table}" for table in retired_tables if table in source)
    assert violations == []


def _business_table_owner(table: str) -> str:
    if table.startswith("news_"):
        return "news"
    raise AssertionError(f"unowned business table: {table}")
