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
        # The code-owned Program contract: the version every verdict row is stamped with, and the route
        # budget the composition seam builds its LM clients against. #193 moved these off the Artifact,
        # where they were optimizer-shaped state they never were, and into the factory that versions them.
        "tracefold.news.program.runtime",
        "tracefold.news.learning.baseline",
        "tracefold.news.learning.review_drafter",
        "tracefold.news.learning.compiler.launcher",
        "tracefold.news.learning.compiler.proxy",
        "tracefold.news.learning.compiler.sandbox",
        "tracefold.news.learning.compiler.security",
        "tracefold.news.learning.compiler.source_identity",
        "tracefold.news.learning.compiler.trusted",
        # #193 PR-C, and the only two names here that run an optimizer in this process. `gepa` is the one
        # bounded GEPA both planes share, and `experiment` is the operator's run directory around it. They
        # are reachable from exactly one CLI module — `tests/architecture/test_news_compiler_boundary.py`
        # asserts which — and neither can register a candidate or move a canary.
        "tracefold.news.learning.compiler.gepa",
        "tracefold.news.learning.experiment",
        "tracefold.news.program.resources.candidates",
        "tracefold.news.program.artifact",
        "tracefold.news.program.dspy_adapter",
        "tracefold.news.program.graph",
        "tracefold.news.artifact_identity",
        "tracefold.news.bus",
        "tracefold.news.learning.canary",
        "tracefold.news.learning.contracts",
        "tracefold.news.learning.evaluator",
        # #199. The framework-neutral objective: which accepted cases GEPA may optimize, which ones hold it
        # honest, and which ones are somebody else's defect. `readiness` is the CLI that publishes it, so
        # this is the one module here that is neither the optimizer nor the release plane.
        "tracefold.news.learning.objective",
        "tracefold.news.eval.replay",
        "tracefold.news.eval.why",
        "tracefold.news.learning.replay",
        "tracefold.news.learning.review",
        "tracefold.news.program.contracts",
    ),
    "app.composition": (
        # The code-owned Program contract: the version every verdict row is stamped with, and the route
        # budget the composition seam builds its LM clients against. #193 moved these off the Artifact,
        # where they were optimizer-shaped state they never were, and into the factory that versions them.
        "tracefold.news.program.runtime",
        "tracefold.news.program.artifact",
        "tracefold.news.program.dspy_adapter",
        "tracefold.news.program.graph",
        "tracefold.news.artifact_identity",
        "tracefold.news.learning.contracts",
        "tracefold.news.learning.evaluator",
        "tracefold.news.market_review.storage",
        "tracefold.news.query_specs",
        "tracefold.news.program.contracts",
        "tracefold.news.storage.root",
        "tracefold.trading.storage.root",
    ),
    "app.http": (
        "tracefold.news.health",
        "tracefold.news.market_review.instruments",
        "tracefold.news.market_review.pricing",
        "tracefold.news.learning.review",
    ),
    "app.trading_cli": ("tracefold.trading.contracts",),
    "app.workers": (
        # The code-owned Program contract: the version every verdict row is stamped with, and the route
        # budget the composition seam builds its LM clients against. #193 moved these off the Artifact,
        # where they were optimizer-shaped state they never were, and into the factory that versions them.
        "tracefold.news.program.runtime",
        "tracefold.news.program.resources.candidates",
        "tracefold.news.program.artifact",
        "tracefold.news.program.graph",
        # The News transport error vocabulary. The composition root's database adapter is the one place
        # that turns a lane's admission timeout into the Defer/Transient distinction the broker acts on.
        "tracefold.news.bus",
        "tracefold.news.learning.canary",
        "tracefold.news.learning.contracts",
        "tracefold.news.learning.evaluator",
        "tracefold.news.oi_signals",
        "tracefold.news.pipeline",
        "tracefold.news.market_review.loops",
        "tracefold.news.program.contracts",
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
# Concrete integration families may own one business-facing adapter. This is a module-family rule,
# not a filename inventory: converting `opentrade.py` into an `opentrade/` package keeps the seam.
INTEGRATION_BUSINESS_ADAPTER_FAMILIES = {"opentrade": {"trading"}}
# News V3 cross-domain reads: none since the Analyst lane was retired (#57). Every edge
# would have to be named here; no News module may write another business package's tables.
ALLOWED_READ_ONLY_CROSS_DOMAIN_TABLES: dict[str, set[str]] = {}
WRITE_SQL_TABLE_RE = re.compile(
    r"\b(?:DELETE\s+FROM|INSERT\s+INTO|UPDATE)\s+(?P<table>[a-z][a-z0-9_]*)",
    re.IGNORECASE,
)
SCHEMA_TABLE_RE = re.compile(r"^## `(?P<table>[a-z][a-z0-9_]*)`$", re.MULTILINE)
SQL_TABLE_RE = re.compile(
    r"\b(?:DELETE\s+FROM|INSERT\s+INTO|FROM|JOIN|UPDATE)\s+(?P<table>[a-z][a-z0-9_]*)",
    re.IGNORECASE,
)
PLATFORM_TABLES = {
    "alembic_version",
    "workers_runtime",
}


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
        "tracefold.news.learning.baseline",
    )
    assert _private_import_allowed("tracefold.app.repository_session", "tracefold.news.storage.root")
    assert _private_import_allowed("tracefold.app.http.routes.review", "tracefold.news.learning.review")
    assert not _private_import_allowed("tracefold.app.http.routes.review", "tracefold.news.storage.root")


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
            dependencies = _business_dependencies(path)
            relative = path.relative_to(SRC / "integrations") if owner == "integrations" else None
            integration_family = relative.parts[0].removesuffix(".py") if relative is not None else ""
            path_allowed = INTEGRATION_BUSINESS_ADAPTER_FAMILIES.get(integration_family, owner_allowed)
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


def _business_table_owner(table: str) -> str:
    if table.startswith("news_"):
        return "news"
    # #104: table prefix is the ownership claim. A `trading_*` table read or written from a News
    # module — or the reverse — fails here before it can become a cross-domain dependency.
    if table.startswith("trading_"):
        return "trading"
    raise AssertionError(f"unowned business table: {table}")
