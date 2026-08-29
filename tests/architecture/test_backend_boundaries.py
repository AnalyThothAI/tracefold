from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "tracefold"
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
        # The code-owned Program contract: the version every verdict row is stamped with, the route
        # budget the composition seam builds its LM clients against, and the computed identity of that
        # code (#314) — which the composition root stamps onto the arm manifest and the epoch it opens,
        # for the same reason it stamps `PROGRAM_VERSION`. #193 moved these off the Artifact, where they
        # were optimizer-shaped state they never were.
        "tracefold.news.program.runtime",
        "tracefold.news.program.identity",
        "tracefold.news.learning.baseline",
        "tracefold.news.review.drafter",
        # The one offline optimization capability. App composition may invoke it, while the capability
        # tests assert that it cannot reach database review, candidate registration or canary promotion.
        "tracefold.news.learning.optimizer",
        # #202 §8: freezing a corpus, admitting a candidate and judging one are three objects now, and
        # the CLI composes them where the old evaluator hid the composition. `learning freeze --role
        # validation` is the one command that needs both: the release plane admits the candidate, and only
        # then does the freeze get to refuse the window.
        "tracefold.news.learning.dataset",
        "tracefold.news.learning.ledger",
        "tracefold.news.release.candidate",
        "tracefold.news.program.resources.candidates",
        "tracefold.news.program.artifact",
        "tracefold.news.program.lm",
        "tracefold.news.program.module",
        "tracefold.news.program.routing",
        "tracefold.news.artifact_identity",
        "tracefold.news.bus",
        "tracefold.news.release.canary",
        "tracefold.news.learning.contracts",
        "tracefold.news.learning.evaluate",
        # #199. The framework-neutral objective: which accepted cases GEPA may optimize, which ones hold it
        # honest, and which ones are somebody else's defect. `readiness` is the CLI that publishes it, so
        # this is the one module here that is neither the optimizer nor the release plane.
        "tracefold.news.learning.objective",
        "tracefold.news.learning.taxonomy",
        "tracefold.news.eval.replay",
        "tracefold.news.eval.why",
        "tracefold.news.review.desk",
        "tracefold.news.program.contracts",
    ),
    "app.composition": (
        # The code-owned Program contract: the version every verdict row is stamped with, the route
        # budget the composition seam builds its LM clients against, and the computed identity of that
        # code (#314) — which the composition root stamps onto the arm manifest and the epoch it opens,
        # for the same reason it stamps `PROGRAM_VERSION`. #193 moved these off the Artifact, where they
        # were optimizer-shaped state they never were.
        "tracefold.news.program.runtime",
        "tracefold.news.program.identity",
        "tracefold.news.program.artifact",
        "tracefold.news.program.lm",
        "tracefold.news.program.module",
        "tracefold.news.program.routing",
        # Post-delivery relationship verification is a content-addressed model adapter composed by App.
        # It cannot change admission or the semantic Program and is scheduled only after send settlement.
        "tracefold.news.program.progression_review",
        "tracefold.news.artifact_identity",
        "tracefold.news.learning.contracts",
        "tracefold.news.learning.evaluate",
        "tracefold.news.market_review.storage",
        "tracefold.news.storage.query_specs",
        "tracefold.news.program.contracts",
        "tracefold.news.storage.root",
        "tracefold.news.search",
        "tracefold.trading.storage.root",
        # #269/#286. Three surfaces have to describe the *same* capital rules — the Workers wiring that
        # executes them, the CLI replay that reports what they did, and the HTTP status the console
        # reads — so `app/trading_config.py` assembles them from settings once and every reader gets
        # the same digest. #286 extends that assembly to the runtime's regime, trade and notional config
        # so replay cannot silently use defaults. These are pure code-owned values; composition is App's,
        # which is why this belongs here rather than in `app.http` or either business package.
        "tracefold.trading.admission",
        "tracefold.trading.capital_lane",
        "tracefold.trading.contracts",
        "tracefold.trading.market_context",
        "tracefold.trading.policy",
    ),
    "app.http": (
        "tracefold.news.health",
        "tracefold.news.market_review.instruments",
        "tracefold.news.market_review.pricing",
        # #207 PR-W4: the measurement version that is half of `OiTradeCandidate.source_key`. The 成案 badge
        # rebuilds `oi:{event_id}:{metric_version}` to ask whether one Event became a case, and a literal
        # here would stop matching the day `oi_signals` bumps it — silently, as "no case".
        "tracefold.news.oi_signals",
        "tracefold.news.review.desk",
        "tracefold.trading.intent",
    ),
    "app.trading_cli": (
        "tracefold.trading.contracts",
        # #265 PR-C's read-only replay. It exists precisely so the report and the scanner are the same
        # code: it drives the production source stage, the Candidate Gate and the strategy rather than
        # re-implementing a funnel that would drift the first time a rule moved. That means importing
        # the pure modules by name, which is what these five entries are — every one of them a pure
        # function over frozen values, with no storage, provider or execution path behind it.
        "tracefold.trading.blacklist",
        "tracefold.trading.capabilities",
        "tracefold.trading.contracts",
        "tracefold.trading.execution_policy",
        "tracefold.trading.intent",
        "tracefold.trading.market_context",
        "tracefold.trading.policy",
        "tracefold.trading.replay",
        "tracefold.trading.research.oi_replay",
        # The OI lane's measurement version, so the replay reads the same rows the scanner does. A
        # literal here would silently stop matching the day `oi_signals` bumps it.
        "tracefold.news.oi_signals",
    ),
    "app.workers": (
        # The code-owned Program contract: the version every verdict row is stamped with, the route
        # budget the composition seam builds its LM clients against, and the computed identity of that
        # code (#314) — which the composition root stamps onto the arm manifest and the epoch it opens,
        # for the same reason it stamps `PROGRAM_VERSION`. #193 moved these off the Artifact, where they
        # were optimizer-shaped state they never were.
        "tracefold.news.program.runtime",
        "tracefold.news.program.identity",
        "tracefold.news.program.resources.candidates",
        "tracefold.news.program.artifact",
        # The News transport error vocabulary. The composition root's database adapter is the one place
        # that turns a lane's admission timeout into the Defer/Transient distinction the broker acts on.
        "tracefold.news.bus",
        "tracefold.news.release.canary",
        "tracefold.news.learning.contracts",
        "tracefold.news.learning.evaluate",
        "tracefold.news.oi_signals",
        "tracefold.news.pipeline",
        "tracefold.news.market_review.loops",
        "tracefold.news.program.contracts",
        # The News-owned row contract for the Trading handoff. Only the composition root's mapper reads
        # it, and it reads the contract rather than the repository: the SELECTs stay News's business.
        "tracefold.news.storage.trade_projection",
        "tracefold.news.triage_rules",
        "tracefold.trading.capabilities",
        "tracefold.trading.capital_lane",
        "tracefold.trading.catalog",
        "tracefold.trading.contracts",
    ),
    "app.nautilus": ("tracefold.trading.intent",),
    "integrations.opennews": ("tracefold.news.opennews",),
    "integrations.rabbitmq": ("tracefold.news.bus",),
    "integrations.venues": (
        "tracefold.news.market_review.instruments",
        "tracefold.news.market_review.pricing",
        "tracefold.news.tradability",
    ),
    "integrations.nautilus": (
        "tracefold.trading.execution_policy",
        "tracefold.trading.replay",
    ),
}
# Concrete integration families may own one business-facing adapter. This is a module-family rule,
# not a filename inventory: converting `opentrade.py` into an `opentrade/` package keeps the seam.
INTEGRATION_BUSINESS_ADAPTER_FAMILIES = {
    "nautilus": {"trading"},
    "opentrade": {"trading"},
    "trading_catalog": {"trading"},
}
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
    elif (
        parts[:4] == ["tracefold", "app", "cli", "commands"] and len(parts) > 4 and parts[4].startswith("trading")
    ) or parts == ["tracefold", "app", "cli", "replay_artifacts"]:
        family = "app.trading_cli"
    elif parts[:3] == ["tracefold", "app", "workers"]:
        family = "app.workers"
    elif parts[:3] == ["tracefold", "app", "http"]:
        family = "app.http"
    elif parts[:3] == ["tracefold", "app", "nautilus"]:
        family = "app.nautilus"
    elif parts[:2] == ["tracefold", "app"] and len(parts) == 3:
        family = "app.composition"
    elif parts[:3] == ["tracefold", "integrations", "opennews"]:
        family = "integrations.opennews"
    elif parts[:3] == ["tracefold", "integrations", "venues"]:
        family = "integrations.venues"
    elif parts[:3] == ["tracefold", "integrations", "nautilus"]:
        family = "integrations.nautilus"
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
    assert _private_import_allowed("tracefold.app.http.routes.review", "tracefold.news.review.desk")
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
