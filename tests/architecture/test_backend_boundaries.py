from __future__ import annotations

import ast
import re
from pathlib import Path

from tracefold.app.worker_manifest import all_worker_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"
BUSINESS_PACKAGES = ("market", "news", "macro", "notifications")
ALLOWED_BUSINESS_DEPENDENCIES = {
    "market": {"market", "platform"},
    "news": {"news", "platform"},
    "macro": {"macro", "market", "platform"},
    "notifications": {"notifications", "platform"},
}
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
    "checkpoint_blobs",
    "checkpoint_migrations",
    "checkpoint_writes",
    "checkpoints",
    "worker_queue_terminal_events",
}
RETIRED_NEWS_RUNTIME_MARKERS = (
    "news_item_process",
    "news_page_projection",
    "news_page_rows",
    "news_provider_items",
    "news_projection_dirty_targets",
    "news_intel",
    "opennews",
    "cryptopanic",
    "NewsAnalysisWorker",
    "StoryInterface",
    "news_story_articles",
    "news_story_analyses",
    "news_story_analysis_attempts",
    "news_analysis",
    "verification_status",
    "evidence_set_hash",
    "news_ingest",
    "news_story_project",
    "news_brief_plan",
    "news_ai_publish",
    "NewsIngestWorker",
    "NewsStoryProjectWorker",
    "NewsBriefPlanWorker",
    "NewsAiPublishWorker",
    "rebuild-news-stories",
    "/api/news/items",
    "/api/news/sources/status",
    "/api/news/stories?",
    "/api/news/brief/history",
    "/analysis/requests",
    "/news/items",
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


def test_backend_has_only_the_hard_cut_package_shape() -> None:
    assert {path.name for path in SRC.iterdir() if path.is_dir() and path.name != "__pycache__"} == {
        "app",
        "integrations",
        "macro",
        "market",
        "news",
        "notifications",
        "platform",
    }
    assert not (SRC / "domains").exists()
    for retired in ("operations", "runtime", "surfaces"):
        assert not (SRC / "app" / retired).exists()
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


def test_external_consumers_use_business_package_roots_only() -> None:
    violations: list[str] = []
    for package in BUSINESS_PACKAGES:
        prefix = f"tracefold.{package}."
        for path in _python_files(SRC):
            if path.relative_to(SRC).parts[0] == package:
                continue
            violations.extend(
                f"{path.relative_to(ROOT)} -> {imported}" for imported in _imports(path) if imported.startswith(prefix)
            )
    assert violations == []


def test_business_sql_uses_only_owned_tables() -> None:
    schema = (ROOT / "docs" / "generated" / "db-schema.md").read_text(encoding="utf-8")
    tables = set(SCHEMA_TABLE_RE.findall(schema))
    table_owners = {table: _business_table_owner(table) for table in tables if table not in PLATFORM_TABLES}
    violations: list[str] = []
    for package in BUSINESS_PACKAGES:
        for path in _python_files(SRC / package):
            for table in SQL_TABLE_RE.findall(path.read_text(encoding="utf-8")):
                owner = table_owners.get(table.lower())
                if owner is not None and owner != package:
                    violations.append(f"{path.relative_to(ROOT)} -> {table} ({owner})")
    assert violations == []


def test_worker_manifest_has_one_writer_per_current_read_model() -> None:
    manifests = all_worker_manifests()
    names = [manifest.name for manifest in manifests]
    tables = [table for manifest in manifests for table, _identity in manifest.current_read_model_identities]
    assert len(names) == len(set(names))
    assert len(tables) == len(set(tables))


def test_news_runtime_has_exactly_the_three_worldmonitor_pipeline_workers() -> None:
    news_workers = {manifest.name for manifest in all_worker_manifests() if manifest.name.startswith("news_")}
    assert news_workers == {
        "news_pipeline",
        "news_ai_classify",
        "news_world_brief",
    }


def test_current_read_models_use_stable_product_keys() -> None:
    violations = {
        f"{manifest.name}:{table}": sorted(set(identity) & FORBIDDEN_CURRENT_IDENTITY_PARTS)
        for manifest in all_worker_manifests()
        for table, identity in manifest.current_read_model_identities
        if set(identity) & FORBIDDEN_CURRENT_IDENTITY_PARTS
    }
    assert violations == {}


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


def _business_table_owner(table: str) -> str:
    if table.startswith("news_"):
        return "news"
    if table.startswith("macro_"):
        return "macro"
    if table.startswith("notification"):
        return "notifications"
    return "market"
