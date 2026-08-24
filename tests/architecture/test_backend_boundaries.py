from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"
BUSINESS_PACKAGES = ("news", "trading")
ALLOWED_BUSINESS_DEPENDENCIES = {
    "news": {"news", "platform"},
    # #104: Trading is a sibling capability, not a News extension. It never imports News and News
    # never imports it; `tracefold.app` is the only seam that knows both, and it is what turns a
    # public News projection row into a Trading candidate.
    "trading": {"trading", "platform"},
}
# Private implementation imports are ownership rules, not historical file exceptions. Only the
# named composition families and concrete adapter families may reach the named private contracts.
PRIVATE_BUSINESS_IMPORT_RULES = {
    "app.news_cli": (
        "tracefold.news.agents.program_baseline",
        "tracefold.news.agents.program_review_drafter",
        "tracefold.news.agents.program_compiler_launcher",
        "tracefold.news.agents.program_compiler_proxy",
        "tracefold.news.agents.program_compiler_sandbox",
        "tracefold.news.agents.program_compiler_security",
        "tracefold.news.agents.program_compiler_source",
        "tracefold.news.agents.program_compiler_trusted",
        "tracefold.news.agents.programs.candidates",
        "tracefold.news.agents.semantic_program",
        "tracefold.news.artifact_identity",
        "tracefold.news.bus",
        "tracefold.news.canary",
        "tracefold.news.candidate_evaluator",
        "tracefold.news.eval.replay",
        "tracefold.news.eval.why",
        "tracefold.news.recording_replay",
        "tracefold.news.review",
        "tracefold.news.semantic_contract",
    ),
    "app.composition": (
        "tracefold.news.agents.semantic_program",
        "tracefold.news.artifact_identity",
        "tracefold.news.candidate_evaluator",
        "tracefold.news.market_review.storage",
        "tracefold.news.query_specs",
        "tracefold.news.semantic_contract",
        "tracefold.news.storage.root",
        "tracefold.trading.storage.root",
    ),
    "app.http": (
        "tracefold.news.health",
        "tracefold.news.market_review.instruments",
        "tracefold.news.market_review.pricing",
        "tracefold.news.review",
    ),
    "app.trading_cli": ("tracefold.trading.contracts",),
    "app.workers": (
        "tracefold.news.agents.programs.candidates",
        "tracefold.news.agents.semantic_program",
        # The News transport error vocabulary. The composition root's database adapter is the one place
        # that turns a lane's admission timeout into the Defer/Transient distinction the broker acts on.
        "tracefold.news.bus",
        "tracefold.news.canary",
        "tracefold.news.candidate_evaluator",
        "tracefold.news.oi_signals",
        "tracefold.news.pipeline",
        "tracefold.news.market_review.loops",
        "tracefold.news.semantic_contract",
        # The News-owned row contract for the Trading handoff. Only the composition root's mapper reads
        # it, and it reads the contract rather than the repository: the SELECTs stay News's business.
        "tracefold.news.storage.trade_projection",
        "tracefold.news.triage_rules",
        "tracefold.trading.candidate.eligibility",
        "tracefold.trading.contracts",
        "tracefold.trading.decision.policy",
        "tracefold.trading.decision.program",
        "tracefold.trading.decision.regime",
        "tracefold.trading.execution.order",
        "tracefold.trading.pipeline.candidate",
        "tracefold.trading.pipeline.root",
        "tracefold.trading.pipeline.runtime",
    ),
    "integrations.opennews": ("tracefold.news.opennews",),
    "integrations.rabbitmq": ("tracefold.news.bus",),
    "integrations.venues": ("tracefold.news.market_review.instruments", "tracefold.news.market_review.pricing"),
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


def _module_exists(module: str) -> bool:
    if not module.startswith("tracefold."):
        return False
    relative = module.split(".")[1:]
    return SRC.joinpath(*relative).with_suffix(".py").exists() or SRC.joinpath(*relative, "__init__.py").exists()


def _imports(path: Path) -> set[str]:
    imports: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _resolved_from_module(path, node)
            if not module:
                continue
            imports.add(module)
            # ``from tracefold.news import consumers`` imports the private submodule just as surely
            # as ``import tracefold.news.pipeline.root``. Record that edge without mistaking public
            # symbols exported by the package root for modules.
            imports.update(
                candidate
                for alias in node.names
                if alias.name != "*"
                if _module_exists(candidate := f"{module}.{alias.name}")
            )
    return imports


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


def _business_dependencies(path: Path) -> set[str]:
    dependencies: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolved_from_module(path, node)
            if resolved == "tracefold":
                dependencies.update(alias.name for alias in node.names if alias.name in BUSINESS_PACKAGES)
                continue
            names = (resolved,) if resolved else ()
        else:
            continue
        for imported in names:
            parts = imported.split(".")
            if len(parts) > 1 and parts[0] == "tracefold" and parts[1] in BUSINESS_PACKAGES:
                dependencies.add(parts[1])
    return dependencies


def _private_import_allowed(importer: str, imported: str) -> bool:
    parts = importer.split(".")
    family: str | None = None
    if parts[:4] == ["tracefold", "app", "cli", "commands"] and len(parts) > 4 and parts[4].startswith("news"):
        family = "app.news_cli"
    elif parts == ["tracefold", "app", "cli", "commands", "trading"]:
        family = "app.trading_cli"
    elif parts[:3] == ["tracefold", "app", "workers"]:
        family = "app.workers"
    elif parts[:3] == ["tracefold", "app", "http"]:
        family = "app.http"
    elif parts[:2] == ["tracefold", "app"] and len(parts) == 3:
        family = "app.composition"
    elif parts[:3] == ["tracefold", "integrations", "opennews"]:
        family = "integrations.opennews"
    elif parts[:3] == ["tracefold", "integrations", "venues"]:
        family = "integrations.venues"
    elif parts == ["tracefold", "integrations", "rabbitmq"]:
        family = "integrations.rabbitmq"
    allowed_imports = PRIVATE_BUSINESS_IMPORT_RULES.get(family or "", ())
    return any(imported == allowed or imported.startswith(f"{allowed}.") for allowed in allowed_imports)


def test_private_business_import_rules_follow_consumer_families() -> None:
    assert _private_import_allowed(
        "tracefold.app.cli.commands.news_learning",
        "tracefold.news.agents.program_baseline",
    )
    assert _private_import_allowed("tracefold.app.repository_session", "tracefold.news.storage.root")
    assert _private_import_allowed("tracefold.app.http.routes.review", "tracefold.news.review")
    assert not _private_import_allowed("tracefold.app.http.routes.review", "tracefold.news.storage.root")


def test_backend_has_only_the_expected_package_shape() -> None:
    assert {path.name for path in SRC.iterdir() if path.is_dir() and path.name != "__pycache__"} == {
        "app",
        "integrations",
        "news",
        "platform",
        # #104: the second business capability. `docs/ARCHITECTURE.md` names the seam; the dependency
        # test above is what keeps it from becoming a News extension.
        "trading",
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
            dependencies = _business_dependencies(path)
            unexpected = sorted(dependencies - allowed)
            if unexpected:
                violations[path.relative_to(ROOT).as_posix()] = unexpected
    assert violations == {}


def test_relative_sibling_imports_are_resolved_before_dag_classification() -> None:
    node = ast.parse("from ..trading import Candidate\n").body[0]
    assert isinstance(node, ast.ImportFrom)
    assert _resolved_from_module(SRC / "news" / "probe.py", node) == "tracefold.trading"


def test_app_is_the_only_top_level_package_that_may_know_both_businesses() -> None:
    allowed = {
        "app": {"news", "trading"},
        "integrations": {"news"},
        "news": {"news"},
        "platform": set(),
        "trading": {"trading"},
    }
    violations: dict[str, list[str]] = {}
    for owner, owner_allowed in allowed.items():
        for path in _python_files(SRC / owner):
            unexpected = sorted(_business_dependencies(path) - owner_allowed)
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
            importer = _module_name(path)
            violations.extend(
                f"{path.relative_to(ROOT)} -> {imported}"
                for imported in _imports(path)
                if imported.startswith(prefix) and not _private_import_allowed(importer, imported)
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
    from tracefold.app import workers

    assert workers.__all__ == ["run_workers"]
    implementations = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files(SRC / "app" / "workers")
        for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_workers"
    ]
    assert len(implementations) == 1
    assert callable(workers.run_workers)
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
    workers_source = "\n".join(path.read_text(encoding="utf-8") for path in _python_files(SRC / "app" / "workers"))

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
    # #104: table prefix is the ownership claim. A `trading_*` table read or written from a News
    # module — or the reverse — fails here before it can become a cross-domain dependency.
    if table.startswith("trading_"):
        return "trading"
    raise AssertionError(f"unowned business table: {table}")
