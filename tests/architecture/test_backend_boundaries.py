from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "tracefold"
BUSINESS_PACKAGES = ("news", "trading")
ALLOWED_BUSINESS_DEPENDENCIES = {
    "news": {"news", "platform"},
    # #104: Trading is a sibling capability, not a News extension. It never imports News and News
    # never imports it; `tracefold.app` is the only seam that knows both, and it is what turns a
    # public News projection row into a Trading trigger.
    "trading": {"trading", "platform"},
}
# Concrete integration families may own one business-facing adapter. This is a module-family rule,
# not a filename inventory: converting `opentrade.py` into an `opentrade/` package keeps the seam.
INTEGRATION_BUSINESS_ADAPTER_FAMILIES = {
    "nautilus": {"trading"},
    "marketdata": {"trading"},
    "robinhood_chain": {"news"},
    "robinhoodtrenches": {"news"},
    "opentrade": {"trading"},
    "trading_catalog": {"trading"},
}
# The one place a schema revision may run a business parser, named file by file and module by module
# so the exception cannot spread. A migration that reparses stored rows has to ask the business
# question the production code asks; the alternative is a second copy of the parser frozen in the
# revision, able to disagree with the one every live frame goes through -- which is exactly what
# `20260906_0370` exists to stop being true of the smart-money backlog (#562). Nothing outside
# `tracefold/platform/postgres/alembic/versions/` may appear here, and a revision is immutable, so an
# entry is never edited once merged.
BUSINESS_PARSER_MIGRATIONS: dict[str, frozenset[str]] = {
    "tracefold/platform/postgres/alembic/versions/20260906_0370_news_smart_money_reparse.py": frozenset(
        {
            "tracefold.news.events.facts",
            "tracefold.news.smart_money",
            "tracefold.news.source_contracts",
        }
    ),
}
# News V3 cross-domain reads: none since the Analyst lane was retired (#57). Every edge
# would have to be named here; no News module may write another business package's tables.
ALLOWED_READ_ONLY_CROSS_DOMAIN_TABLES: dict[str, set[str]] = {}
WRITE_SQL_TABLE_RE = re.compile(
    r"\b(?:DELETE\s+FROM|INSERT\s+INTO|MERGE\s+INTO|TRUNCATE(?:\s+TABLE)?|UPDATE)\s+"
    r'(?:ONLY\s+)?(?:public\.)?"?(?P<table>[a-z][a-z0-9_]*)"?',
    re.IGNORECASE,
)
SCHEMA_TABLE_RE = re.compile(r"^## `(?P<table>[a-z][a-z0-9_]*)`$", re.MULTILINE)
SQL_TABLE_RE = re.compile(
    r"\b(?:COPY|DELETE\s+FROM|INSERT\s+INTO|MERGE\s+INTO|TRUNCATE(?:\s+TABLE)?|FROM|JOIN|UPDATE)\s+"
    r'(?:ONLY\s+)?(?:public\.)?"?(?P<table>[a-z][a-z0-9_]*)"?',
    re.IGNORECASE,
)
PLATFORM_TABLES = {
    "alembic_version",
    "workers_runtime",
}
# Existing database adapters that legitimately own SQL without being storage modules. Keep this small:
# App is the composition seam, ReviewDesk/evaluation_history predate the storage package split, and moving
# them is not part of PostgreSQL governance. New product SQL belongs in its owner's storage family.
SQL_LOCATION_EXCEPTIONS = frozenset(
    {
        "tracefold/app/cli/commands/db.py",
        "tracefold/app/cli/commands/news_learning.py",
        "tracefold/app/cli/commands/news_learning_runtime.py",
        "tracefold/app/query_audit.py",
        "tracefold/app/workers/runtime.py",
        "tracefold/news/learning/evaluation_history.py",
        "tracefold/news/review/desk.py",
    }
)


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


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


def _migration_parser_imports(path: Path) -> frozenset[str]:
    """The business modules this file is named as allowed to run, or nothing."""

    return BUSINESS_PARSER_MIGRATIONS.get(path.relative_to(ROOT).as_posix(), frozenset())


def test_retired_taxonomy_lifecycle_has_no_module_or_runtime_wiring() -> None:
    for module in (
        "tracefold.news.learning.taxonomy",
        "tracefold.news.learning.taxonomy_shadow",
        "tracefold.news.learning.taxonomy_evaluation",
    ):
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:
            assert exc.name == module
        else:  # pragma: no cover - the assertion describes the retired public import surface
            raise AssertionError(f"retired taxonomy module remains importable: {module}")

    from tracefold.app.cli.commands.news_learning_composition import NewsProgramRuntimeComposition
    from tracefold.app.learning_runtime import NewsRuntimeModels
    from tracefold.news.storage.learning import LearningStorage

    assert not hasattr(NewsProgramRuntimeComposition, "taxonomy_shadow_program")
    # The runtime model composition has no taxonomy or progression slot of any kind (#706).
    assert not [name for name in NewsRuntimeModels.__dataclass_fields__ if "taxonomy" in name or "progression" in name]
    for retired_storage_read in (
        "taxonomy_candidate_registration",
        "taxonomy_active_deployment",
        "taxonomy_shadow_artifacts",
        "taxonomy_regression_sources",
        "taxonomy_gold_sources",
    ):
        assert not hasattr(LearningStorage, retired_storage_read)


def test_delivery_adapters_never_import_the_market_notification_loop() -> None:
    """#562: what a failed send proved is a transport contract, not something a business loop lends out.

    `COMMIT_PHASE_*` was defined in `news/market_notifications.py` and reached the Feishu and Telegram
    adapters through the package root, so importing `tracefold.news` for a two-string vocabulary pulled
    in the market loop -- a dependency pointing from the transport into the business rules it serves.
    """

    from tracefold.news import delivery_contracts, market_notifications

    assert set(delivery_contracts.__all__) == {
        "COMMIT_PHASE_NOT_SENT",
        "COMMIT_PHASE_UNKNOWN",
        "DELIVERY_FAILURE_REFUSED",
        "DELIVERY_FAILURE_RETRIABLE",
        "DELIVERY_FAILURE_UNKNOWN",
        "classify_delivery_failure",
        # #604 N3: how long a refusal asked the caller to wait is the same kind of transport fact as
        # what it proved about the message, so both lanes read it from here and neither from the other.
        "RETRY_AFTER_MAX_SECONDS",
        "retry_after_ms",
    }
    # #604 N1: reading that vocabulary is one function, and both delivery loops call it. A second
    # copy of "may this be sent again" is how the News lane came to settle a rate limit `terminal`
    # while the market lane retried the same error from the same adapter. Neither loop reads the
    # attributes itself any more, which is what stops the two answers drifting apart again.
    assert [name for name in market_notifications.__all__ if name.startswith("DELIVERY_FAILURE")] == []
    for loop_module in ("news/market_notifications.py", "news/pipeline/delivery.py"):
        source = (SRC / loop_module).read_text(encoding="utf-8")
        assert 'getattr(exc, "commit_phase"' not in source, loop_module
        assert 'getattr(exc, "retryable"' not in source, loop_module
    assert [name for name in market_notifications.__all__ if name.startswith("COMMIT_PHASE")] == []

    loop = "tracefold.news.market_notifications"
    violations = [
        f"{path.relative_to(ROOT)} -> {imported}"
        for path in [*_python_files(SRC / "integrations"), SRC / "news" / "__init__.py"]
        for imported in _imports(path)
        if imported == loop or imported.startswith(f"{loop}.")
    ]
    assert violations == []


def test_delivery_adapters_import_the_card_model_and_never_a_renderer_or_a_loop() -> None:
    """#562 PR-C: a channel serializer stands beside the other one, never downstream of it.

    The Telegram adapter used to be handed Feishu's card JSON and read the card back out of it, so the
    two channels were in series and every market card lost its family, its event time and its market
    body on the way through the second parse. Both adapters now serialize the same `ReaderCard`. What
    that leaves them allowed to know is the card model and the reader-facing formats
    (`reader_card`, `card_format`, `delivery_contracts`, and the values module carrying the presentation
    and the receipt): not the two renderers that fill the card, not the pipeline that delivers it, and
    not the market loop that owns the other one.
    """

    forbidden = ("tracefold.news.delivery", "tracefold.news.pipeline", "tracefold.news.market_notifications")
    violations = [
        f"{path.relative_to(ROOT)} -> {imported}"
        for path in _python_files(SRC / "integrations")
        for imported in _imports(path)
        for module in forbidden
        if imported == module or imported.startswith(f"{module}.")
    ]
    assert violations == []

    from tracefold import news

    for name in ("ReaderCard", "quote_line", "card_clock", "LINKABLE_TICKER_RE", "NOVELTY_ZH"):
        assert name in news.__all__
    for renderer in ("render_first_card", "render_market_card", "feishu_card"):
        assert renderer not in news.__all__


def test_business_dependency_dag_is_one_way() -> None:
    violations: dict[str, list[str]] = {}
    for owner, allowed in ALLOWED_BUSINESS_DEPENDENCIES.items():
        for path in _python_files(SRC / owner):
            dependencies = _business_dependencies(path)
            unexpected = sorted(dependencies - allowed)
            if unexpected:
                violations[path.relative_to(ROOT).as_posix()] = unexpected
    assert violations == {}


def test_business_packages_do_not_own_argparse_cli_semantics() -> None:
    violations = [
        path.relative_to(ROOT).as_posix()
        for package in BUSINESS_PACKAGES
        for path in _python_files(SRC / package)
        if "argparse" in _imports(path)
    ]
    assert violations == []


def test_news_search_planner_is_consumed_only_by_the_news_read_path() -> None:
    """#336: processing lanes and Trading must not acquire a dependency on feed search semantics."""

    consumers = {
        _module_name(path)
        for path in _python_files(SRC)
        if any(
            imported == "tracefold.news.search" or imported.startswith("tracefold.news.search.")
            for imported in _imports(path)
        )
    }
    assert consumers == {
        "tracefold.app.repository_session",
        "tracefold.news.storage.feed",
    }


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
            dependencies = _business_dependencies(path)
            relative = path.relative_to(SRC / "integrations") if owner == "integrations" else None
            integration_family = relative.parts[0].removesuffix(".py") if relative is not None else ""
            path_allowed = INTEGRATION_BUSINESS_ADAPTER_FAMILIES.get(integration_family, owner_allowed)
            path_allowed = path_allowed | {module.split(".")[1] for module in _migration_parser_imports(path)}
            unexpected = sorted(dependencies - path_allowed)
            if owner != "app" and dependencies == {"news", "trading"}:
                unexpected = ["news+trading"]
            if unexpected:
                violations[path.relative_to(ROOT).as_posix()] = unexpected
    assert violations == {}


def test_platform_does_not_depend_on_app_business_or_integrations() -> None:
    forbidden = {"app", "integrations", *BUSINESS_PACKAGES}
    violations: dict[str, list[str]] = {}
    for path in _python_files(SRC / "platform"):
        named = _migration_parser_imports(path)
        dependencies = {
            imported.split(".")[1]
            for imported in _imports(path)
            if imported.startswith("tracefold.") and len(imported.split(".")) > 1 and imported not in named
        }
        unexpected = sorted(dependencies & forbidden)
        if unexpected:
            violations[path.relative_to(ROOT).as_posix()] = unexpected
    assert violations == {}


def test_only_the_named_revisions_run_a_business_parser() -> None:
    """The exception is exact: every named file exists, and every named module is really imported.

    A revision is immutable, so this list only ever grows by one entry per new revision that reparses
    stored rows. It fails closed in both directions -- an entry whose file or import has gone stale is
    a rule nobody is following any more, and a revision importing a business module it did not declare
    is the spread this list exists to prevent.
    """

    declared = {relative: sorted(modules) for relative, modules in BUSINESS_PARSER_MIGRATIONS.items()}
    versions = ROOT / "tracefold" / "platform" / "postgres" / "alembic" / "versions"
    actual = {
        path.relative_to(ROOT).as_posix(): sorted(
            imported for imported in _imports(path) if imported.startswith("tracefold.")
        )
        for path in _python_files(versions)
        if any(imported.startswith("tracefold.") for imported in _imports(path))
    }

    assert actual == declared


def test_integrations_do_not_depend_on_app() -> None:
    violations = [
        f"{path.relative_to(ROOT)} -> {imported}"
        for path in _python_files(SRC / "integrations")
        for imported in _imports(path)
        if imported == "tracefold.app" or imported.startswith("tracefold.app.")
    ]
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


def test_production_sql_lives_in_owned_storage_or_an_explicit_adapter() -> None:
    schema = (ROOT / "docs" / "generated" / "db-schema.md").read_text(encoding="utf-8")
    tables = set(SCHEMA_TABLE_RE.findall(schema))
    assert tables, "generated schema table scan must fail closed"

    sql_paths: set[str] = set()
    for path in [*_python_files(SRC), *sorted(SRC.rglob("*.sql"))]:
        if set(SQL_TABLE_RE.findall(path.read_text(encoding="utf-8"))) & tables:
            sql_paths.add(path.relative_to(ROOT).as_posix())
    assert sql_paths, "production SQL location scan must fail closed"

    violations = sorted(path for path in sql_paths if not _sql_location_allowed(path))
    assert violations == []


def test_app_composition_does_not_own_news_canary_release_semantics() -> None:
    """App may pass runtime facts, but News Release owns lineage, reasons, and transitions."""

    durable_reasons = {
        "selector_version_mismatch",
        "eligibility_profile_hash_mismatch",
        "rolling_profile_hash_mismatch",
        "candidate_manifest_missing_or_invalid",
        "candidate_bundle_mismatch",
        "candidate_parent_stale",
        "candidate_artifact_invalid",
        "candidate_runtime_invalid",
        "candidate_runtime_unavailable",
    }
    lineage_attributes = {
        "parent_stable_sha",
        "program_parent_sha256",
        "program_candidate_sha256",
    }
    violations: list[str] = []
    for path in _python_files(SRC / "app"):
        relative = path.relative_to(SRC)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "transition_canary":
                    violations.append(f"{relative.as_posix()} calls transition_canary")
            elif isinstance(node, ast.Attribute) and node.attr in lineage_attributes:
                violations.append(f"{relative.as_posix()} interprets {node.attr}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in durable_reasons:
                violations.append(f"{relative.as_posix()} owns durable reason {node.value}")
    assert violations == []


def _sql_location_allowed(relative: str) -> bool:
    path = Path(relative)
    return (
        "storage" in path.parts
        or path.stem.endswith("_storage")
        or relative.startswith("tracefold/platform/postgres/")
        or relative in SQL_LOCATION_EXCEPTIONS
    )


def _business_table_owner(table: str) -> str:
    if table.startswith("news_"):
        return "news"
    # #104: table prefix is the ownership claim. A `trading_*` table read or written from a News
    # module — or the reverse — fails here before it can become a cross-domain dependency.
    if table.startswith("trading_"):
        return "trading"
    raise AssertionError(f"unowned business table: {table}")
