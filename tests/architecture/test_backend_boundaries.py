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
        "tracefold.news.program.artifact_tool",
        "tracefold.news.program.lm",
        "tracefold.news.program.module",
        "tracefold.news.program.routing",
        "tracefold.news.artifact_identity",
        "tracefold.news.bus",
        "tracefold.news.release.canary",
        "tracefold.news.release.runtime",
        "tracefold.news.learning.contracts",
        "tracefold.news.learning.evaluate",
        # #199. The framework-neutral objective: which accepted cases GEPA may optimize, which ones hold it
        # honest, and which ones are somebody else's defect. `readiness` is the CLI that publishes it, so
        # this is the one module here that is neither the optimizer nor the release plane.
        "tracefold.news.learning.objective",
        # #437: the existing recorded Dataset baseline invokes one pure taxonomy metric.
        "tracefold.news.learning.taxonomy_metric",
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
        # #433-A. App owns the dormant transport's query-audit registration and restore-drill seed.
        # Neither path activates a producer or consumer; both compose the Trading-owned storage seam.
        "tracefold.trading.storage.execution_stream",
        # #269/#286. Three surfaces have to describe the *same* capital rules — the Workers wiring that
        # executes them, the CLI replay that reports what they did, and the HTTP status the console
        # reads — so `app/trading_config.py` assembles them from settings once and every reader gets
        # the same digest. #286 extends that assembly to the runtime's regime, trade and notional config
        # so replay cannot silently use defaults. These are pure code-owned values; composition is App's,
        # which is why this belongs here rather than in `app.http` or either business package.
        "tracefold.trading.admission",
        "tracefold.trading.signal_lane",
        "tracefold.trading.contracts",
        "tracefold.trading.market_context",
        "tracefold.trading.policy",
        "tracefold.trading.storage.queries",
        "tracefold.trading.storage.gate",
        # #537 PR-5. `GET /api/trading/status` runs exactly one statement over `trading_cases` -- the
        # lane's own liveness probe -- and the query-plan audit registers the production constant
        # rather than a copy of the SQL that an edit can leave behind.
        "tracefold.trading.storage.lane",
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
        # #377's credential-free CLI is the App composition seam for the evidence clock.  These
        # modules are pure contracts and transformations; provider and PostgreSQL I/O stay in App.
        "tracefold.trading.contract_receipt",
        "tracefold.trading.evidence_clock",
        "tracefold.trading.evidence_research",
        "tracefold.trading.evidence_verification",
        "tracefold.trading.routing",
        # The OI lane's measurement version, so the replay reads the same rows the scanner does. A
        # literal here would silently stop matching the day `oi_signals` bumps it.
        "tracefold.news.oi_signals",
    ),
    # #572 PR-1. The JSON-RPC adapter answers in the tape's own address and topic encoding rather than
    # keeping a second copy of it, the same way the venue catalogue adapters answer in the instrument
    # vocabulary. `evm` is pure string work with no network, no ABI library and no business rule.
    "integrations.robinhood_chain": ("tracefold.news.chain_tape.evm",),
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
        "tracefold.news.release.runtime",
        "tracefold.news.learning.contracts",
        "tracefold.news.learning.evaluate",
        "tracefold.news.oi_signals",
        "tracefold.news.pipeline",
        # #553 PR-2. The market notification loop is one News-owned object with one business action,
        # `advance()`. App composes it, declares its capability key and owns its tick, exactly as it
        # does the Trading Signal lane's; the rules and the durable state stay inside News.
        "tracefold.news.market_notifications",
        # #572 PR-1. The wallet tape joins the same way and for the same reason: one News-owned object
        # with one business action, `advance()`. App composes it, builds the two provider adapters that
        # News may not name, declares its capability key and owns its tick.
        "tracefold.news.chain_tape",
        "tracefold.news.market_review.loops",
        # The database composition adapter constructs narrow callback views from the concrete
        # repositories; no business package imports the App adapter in return.
        "tracefold.news.market_review.storage",
        "tracefold.news.program.contracts",
        "tracefold.news.storage.root",
        # The News-owned row contract for the Trading handoff. Only the composition root's mapper reads
        # it, and it reads the contract rather than the repository: the SELECTs stay News's business.
        "tracefold.news.storage.trade_projection",
        "tracefold.news.triage_rules",
        "tracefold.trading.signal_lane",
        "tracefold.trading.contracts",
        # #537 PR-3: the one table of supported source venues. The bar fetcher at this seam reads
        # which provider family answers a venue and how that venue spells the market off it, rather
        # than keeping a fourth copy of that mapping.
        "tracefold.trading.sources",
        "tracefold.trading.storage.root",
    ),
    "app.nautilus": (
        # #433-B: the dormant Runtime composition root materializes Trading-owned execution rows,
        # prepares bounded Observation batches, and supplies the wake channel to the PostgreSQL
        # integration. Nautilus adapters receive only public values and narrow callables.
        "tracefold.trading.storage.execution_stream",
    ),
    "integrations.opennews": ("tracefold.news.opennews",),
    "integrations.rabbitmq": (
        "tracefold.news.bus",
        # #400: the broker owns retry, and the News-owned policy document is what says so. The adapter
        # applies and verifies that contract; it does not decide it.
        "tracefold.news.broker_policy",
        # The adapter reports fatal transport settlement through the News-owned, platform-implemented
        # low-cardinality telemetry port; it does not reach storage or pipeline implementation.
        "tracefold.news.telemetry",
    ),
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
    "robinhood_chain": {"news"},
    "opentrade": {"trading"},
    "trading_catalog": {"trading"},
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


def _private_import_allowed(importer: str, imported: str) -> bool:
    parts = importer.split(".")
    family: str | None = None
    if parts[:4] == ["tracefold", "app", "cli", "commands"] and len(parts) > 4 and parts[4].startswith("news"):
        family = "app.news_cli"
    elif parts[:4] == ["tracefold", "app", "cli", "commands"] and len(parts) > 4 and parts[4].startswith("trading"):
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
    elif parts == ["tracefold", "integrations", "robinhood_chain"]:
        family = "integrations.robinhood_chain"
    allowed_imports = PRIVATE_BUSINESS_IMPORT_RULES.get(family or "", ())
    return any(imported == allowed or imported.startswith(f"{allowed}.") for allowed in allowed_imports)


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

    from tracefold.app.learning_runtime import NewsProgramRuntimeComposition
    from tracefold.news.storage.learning import LearningStorage

    assert not hasattr(NewsProgramRuntimeComposition, "taxonomy_shadow_program")
    for retired_storage_read in (
        "taxonomy_candidate_registration",
        "taxonomy_active_deployment",
        "taxonomy_shadow_artifacts",
        "taxonomy_regression_sources",
        "taxonomy_gold_sources",
    ):
        assert not hasattr(LearningStorage, retired_storage_read)


def test_private_business_import_rules_follow_consumer_families() -> None:
    assert _private_import_allowed(
        "tracefold.app.cli.commands.news_learning",
        "tracefold.news.learning.baseline",
    )
    assert _private_import_allowed("tracefold.app.repository_session", "tracefold.news.storage.root")
    assert _private_import_allowed("tracefold.app.http.routes.review", "tracefold.news.review.desk")
    assert not _private_import_allowed("tracefold.app.http.routes.review", "tracefold.news.storage.root")


def test_delivery_adapters_never_import_the_market_notification_loop() -> None:
    """#562: what a failed send proved is a transport contract, not something a business loop lends out.

    `COMMIT_PHASE_*` was defined in `news/market_notifications.py` and reached the Feishu and Telegram
    adapters through the package root, so importing `tracefold.news` for a two-string vocabulary pulled
    in the market loop -- a dependency pointing from the transport into the business rules it serves.
    """

    from tracefold.news import delivery_contracts, market_notifications

    assert set(delivery_contracts.__all__) == {"COMMIT_PHASE_NOT_SENT", "COMMIT_PHASE_UNKNOWN"}
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
