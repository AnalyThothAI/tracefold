"""`tracefold.trading` boundaries: what the online capital path may import, reach, and be.

**The harness itself is the first thing #331 fixes.** Three of these tests used to pass without
checking anything:

* the capital-write dependency test scanned `tracefold/trading/execution/*.py`, a directory that no
  longer exists. An empty glob is zero offenders, so it was green while the real execution code sat in
  `integrations/nautilus/strategy.py` and `app/nautilus/*`. The manifest below is explicit and every
  entry is asserted to exist, so a file that moves fails this file rather than silently emptying it.
* `_imported_roots` took the first segment of an absolute import, so `from tracefold.trading import
  TradeIntent` inside News yielded `tracefold` and the boundary test could never see it. Imports are
  now resolved to full dotted module paths, relative imports included.
* the "no agent framework" test scanned all of `src/tracefold`, which made an unrelated offline News
  research module's dependency a capital-architecture failure. Capability tests now cover the
  reachable online capital path and nothing else.
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "tracefold"
TRADING = SRC / "trading"
NEWS = SRC / "news"

# Every module that can reach a capital write, named rather than globbed. A path that stops existing
# fails `test_the_capital_path_manifest_is_complete`, which is the point: the previous glob answered
# "no offenders" for a directory that had been deleted.
CAPITAL_PATH: tuple[str, ...] = (
    "trading/capital_lane.py",
    "trading/policy.py",
    "trading/admission.py",
    "trading/sources.py",
    "trading/blacklist.py",
    "trading/routing.py",
    "trading/market_context.py",
    "trading/contracts.py",
    "trading/capabilities.py",
    "trading/catalog.py",
    "trading/intent.py",
    "trading/execution_policy.py",
    "trading/quote_authority.py",
    "trading/telemetry.py",
    "trading/storage/root.py",
    "trading/storage/lane.py",
    "trading/storage/cases.py",
    "trading/storage/control.py",
    "trading/storage/capabilities.py",
    "trading/storage/catalog.py",
    "trading/storage/gate.py",
    "trading/storage/intents.py",
    "trading/storage/queries.py",
    "trading/storage/sql_values.py",
    # The execution authority. It is not in `tracefold.trading`, and pretending otherwise is what let
    # the old test pass over an empty directory.
    "integrations/nautilus/strategy.py",
    "integrations/nautilus/capabilities.py",
    "app/nautilus/database.py",
    "app/nautilus/root.py",
    # The composition seam that decides what the lane is given.
    "app/workers/wiring/trading.py",
    "app/workers/wiring/news_to_trading.py",
    "app/trading_config.py",
)

BANNED_FRAMEWORKS = ("deepagents", "langgraph", "langchain", "langsmith", "autogen", "crewai", "dspy")
BANNED_CAPABILITIES = ("subprocess", "shutil", "socket", "requests", "boto3")
WRITE_SQL_TABLE_RE = re.compile(r"\b(?:DELETE\s+FROM|INSERT\s+INTO|UPDATE)\s+(?P<table>[a-z][a-z0-9_]*)", re.IGNORECASE)
SQL_TABLE_RE = re.compile(r"\b(?:DELETE\s+FROM|INSERT\s+INTO|FROM|JOIN|UPDATE)\s+(?P<table>[a-z][a-z0-9_]*)", re.I)
# `DO UPDATE SET` and `FOR UPDATE SKIP LOCKED` both put a keyword where the regex expects a table.
_SQL_KEYWORDS = frozenset({"set", "skip", "select", "lateral", "jsonb_each", "values"})


def _module_name(path: Path) -> str:
    if not path.is_relative_to(SRC.parent):
        # A fixture written outside the tree still has to be parseable: the fixture tests below are
        # what prove this helper can see what the previous one could not.
        return path.stem
    relative = path.relative_to(SRC.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _sql_literals(path: Path) -> list[str]:
    """Every string constant in the file. Comments and docstrings are prose, not statements.

    Scanning raw file text made a sentence like "the UPDATE matched nothing" an offending table name,
    which is exactly the "regex pretending to be a parser" the #331 comment rules out. Literals are
    joined with a statement terminator so a fragment ending in `FOR UPDATE` cannot bind to the first
    word of an unrelated constant.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings
    ]


def _executable_names(path: Path) -> set[str]:
    """Identifiers and string constants a reader could actually reach at runtime.

    Deliberately not the file text: a docstring that *names* a retired concept in order to explain why
    it is gone is documentation, and forbidding it would delete the only record of the decision.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg) or (isinstance(node, ast.keyword) and node.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
            names.update(alias.asname for alias in node.names if alias.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            names.update(alias.name for alias in node.names)
    names.update(_sql_literals(path))
    return names


def _imported_modules(path: Path) -> set[str]:
    """Every module this file imports, as a full dotted path, relative imports resolved.

    Resolution matters (#331 §1): the previous helper returned the *first segment* of an absolute
    import, so every `tracefold.*` import collapsed to `tracefold` and no cross-package rule could
    ever fire.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                parts = package.split(".")
                base_parts = parts[: len(parts) - node.level + 1]
                base = ".".join([*base_parts, node.module] if node.module else base_parts)
            if base:
                modules.add(base)
                modules.update(f"{base}.{alias.name}" for alias in node.names)
    return modules


def _capital_path_files() -> list[Path]:
    return [SRC / relative for relative in CAPITAL_PATH]


def _trading_sources() -> list[Path]:
    return sorted(path for path in TRADING.rglob("*.py") if "__pycache__" not in path.parts)


def test_the_capital_path_manifest_is_complete() -> None:
    """Fail closed on an empty or stale scan set, which is how the old harness went silently green."""

    assert CAPITAL_PATH, "the capital-path manifest may never be empty"
    missing = [relative for relative in CAPITAL_PATH if not (SRC / relative).is_file()]
    assert missing == []
    # Every `tracefold.trading` module is either on the capital path or a research module. Nothing may
    # be neither, because "neither" is how a file leaves the scan set without anyone noticing.
    research = {"trading/replay.py", "trading/research/oi_replay.py", "trading/storage/replay.py"}
    covered = set(CAPITAL_PATH) | research
    unclassified = [
        str(path.relative_to(SRC))
        for path in _trading_sources()
        if path.name != "__init__.py" and str(path.relative_to(SRC)) not in covered
    ]
    assert unclassified == []


def test_the_online_capital_path_holds_no_agent_framework_or_model_client() -> None:
    """Scoped to the capital path (#331 §1.3), so an offline News research import is not an offender."""

    offenders: list[str] = []
    for path in _capital_path_files():
        modules = _imported_modules(path)
        roots = {module.split(".")[0] for module in modules}
        offenders.extend(f"{path.relative_to(ROOT)}:{banned}" for banned in BANNED_FRAMEWORKS if banned in roots)
    assert offenders == []


def test_offline_news_research_may_import_an_agent_framework(tmp_path: Path) -> None:
    """The capital-path scan must not reach an unrelated bounded context (#331 comment F2P 3)."""

    fixture = tmp_path / "offline_research.py"
    fixture.write_text("import dspy\nfrom deepagents import Agent\n", encoding="utf-8")
    assert {module.split(".")[0] for module in _imported_modules(fixture)} == {"dspy", "deepagents"}
    # The offender set is the capital path, and this fixture is not on it. The banned-framework test
    # therefore cannot fail because an offline News research module imports an agent framework.
    assert fixture not in _capital_path_files()


def test_the_capital_path_reaches_no_shell_filesystem_or_arbitrary_http_client() -> None:
    offenders: list[str] = []
    for path in _capital_path_files():
        if path.is_relative_to(SRC / "app") or path.is_relative_to(SRC / "integrations"):
            # App composition and the venue adapters legitimately own process and provider I/O; what
            # they may not do is give the business package a client, which the import rules below and
            # the injected-port shape of `CapitalLane` are what enforce.
            continue
        roots = {module.split(".")[0] for module in _imported_modules(path)}
        offenders.extend(
            f"{path.relative_to(ROOT)}:{banned}"
            for banned in (*BANNED_CAPABILITIES, "os", "pathlib", "httpx")
            if banned in roots
        )
    assert offenders == []


def test_news_never_imports_trading() -> None:
    """An absolute cross-domain import is now visible (#331 §1.2); it used to resolve to `tracefold`."""

    offenders = [
        f"{path.relative_to(ROOT)}:{module}"
        for path in NEWS.rglob("*.py")
        if "__pycache__" not in path.parts
        for module in _imported_modules(path)
        if module == "tracefold.trading" or module.startswith("tracefold.trading.")
    ]
    assert offenders == []


def test_trading_never_imports_news() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{module}"
        for path in _trading_sources()
        for module in _imported_modules(path)
        if module == "tracefold.news" or module.startswith("tracefold.news.")
    ]
    assert offenders == []


def test_a_cross_domain_import_inside_news_is_detected(tmp_path: Path) -> None:
    """#331 comment F2P 2: the boundary test must fail on this fixture, and did not before."""

    fixture = tmp_path / "leaky.py"
    fixture.write_text("from tracefold.trading import TradeIntent\n", encoding="utf-8")
    modules = _imported_modules(fixture)
    assert any(module == "tracefold.trading" or module.startswith("tracefold.trading.") for module in modules)


def test_relative_imports_resolve_to_full_module_paths() -> None:
    modules = _imported_modules(TRADING / "capital_lane.py")
    assert "tracefold.trading.policy" in modules
    assert "tracefold.trading.storage.root" in modules


def test_trading_writes_only_trading_tables() -> None:
    offenders: list[str] = []
    for path in _trading_sources():
        for match in WRITE_SQL_TABLE_RE.finditer(";\n".join(_sql_literals(path))):
            table = match.group("table").lower()
            if table in _SQL_KEYWORDS:
                continue  # `ON CONFLICT ... DO UPDATE SET` is not a table name
            if not table.startswith("trading_"):
                offenders.append(f"{path.relative_to(ROOT)}:{table}")
    assert offenders == []


def test_trading_reads_only_trading_tables() -> None:
    """The News projection is the seam. Trading never reaches through to a News table itself."""

    offenders: list[str] = []
    for path in _trading_sources():
        for match in SQL_TABLE_RE.finditer(";\n".join(_sql_literals(path))):
            table = match.group("table").lower()
            if table in _SQL_KEYWORDS:
                continue
            if table.startswith("news_"):
                offenders.append(f"{path.relative_to(ROOT)}:{table}")
    assert offenders == []


def test_news_never_reads_or_writes_a_trading_table() -> None:
    offenders: list[str] = []
    for path in NEWS.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        offenders.extend(
            str(path.relative_to(ROOT))
            for match in SQL_TABLE_RE.finditer(";\n".join(_sql_literals(path)))
            if match.group("table").lower().startswith("trading_")
        )
    assert offenders == []


def test_current_production_code_contains_no_target_instrument_literal() -> None:
    """Historical migrations retain V1 evidence; current runtime permission cannot name a target."""

    target = re.compile(r"[A-Z0-9]+USDT-PERP\.BINANCE")
    offenders = [
        str(path.relative_to(ROOT))
        for path in SRC.rglob("*.py")
        if "__pycache__" not in path.parts
        and "alembic/versions" not in path.as_posix()
        and target.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_trading_does_not_reach_through_the_app_repository_session_for_news() -> None:
    offenders: list[str] = []
    for path in _trading_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        repository_names = {"repos"}
        changed = True
        while changed:
            changed = False
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if not isinstance(value, ast.Name) or value.id not in repository_names:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in repository_names:
                        repository_names.add(target.id)
                        changed = True
        for node in ast.walk(tree):
            direct = (
                isinstance(node, ast.Attribute)
                and node.attr == "news"
                and isinstance(node.value, ast.Name)
                and node.value.id in repository_names
            )
            dynamic = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in repository_names
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "news"
            )
            if direct or dynamic:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


# ---------------------------------------------------------------------------- deletion evidence
def test_the_retired_lane_cluster_has_no_executable_reference() -> None:
    """#331 §6: the value of this Issue is proved by what is gone, not by what was added."""

    retired = (
        "tracefold/trading/pipeline",
        "tracefold/trading/candidate",
        "tracefold/trading/decision",
        "tracefold/trading/strategy",
        "tracefold/trading/research/event_study.py",
        "tracefold/trading/storage/evaluations.py",
    )
    executable_offenders: list[str] = []
    for name in retired:
        path = SRC.parent / name
        if path.is_file():
            executable_offenders.append(name)
        elif path.is_dir():
            executable_offenders.extend(str(candidate.relative_to(SRC.parent)) for candidate in path.rglob("*.py"))
    assert executable_offenders == []

    names = (
        "CandidateRunner",
        "TradingPipeline",
        "TradingDecisionProgram",
        "LiquidationShadowRunner",
        "plan_triggers",
        "strategy_from_manifest",
        "capital_strategy_id",
        "merge_funnel",
        "bump_dspy_calls",
        "intent_admission_blocked",
        "StrategyPermission",
        "venue_priority",
    )
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts or "alembic/versions" in path.as_posix():
            continue
        reachable = _executable_names(path)
        offenders.extend(f"{path.relative_to(ROOT)}:{name}" for name in names if name in reachable)
    assert offenders == []


def test_live_wiring_reaches_no_hyperliquid_bar_no_model_and_no_shadow_runner() -> None:
    """The Decision catalog may read HL metadata; the Binance capital path may not read HL bars."""

    wiring = SRC / "app/workers/wiring/trading.py"
    reachable = {name.lower() for name in _executable_names(wiring) if isinstance(name, str)}
    for banned in ("fetch_hyperliquid_candles", "dspy", "tradingdecisionprogram", "liquidationshadowrunner"):
        assert banned not in reachable, banned
    modules = _imported_modules(wiring)
    assert "tracefold.integrations.venues.fetch_binance_candles" in modules
    assert "tracefold.integrations.trading_catalog.fetch_hyperliquid_perp_catalog" in modules
    assert not any("fetch_hyperliquid_candles" in module.lower() for module in modules)
    assert not any("dspy" in module.lower() for module in modules)


def test_the_package_root_exports_only_app_facing_values_and_ports() -> None:
    from tracefold import trading

    assert trading.__all__ == [
        "ACTIVE_INTENT_STATES",
        "BAR_FIDELITY_VERSION",
        "INTENT_POLICY_SHA256",
        "MAX_RECEIVE_AGE_NS",
        "Bar",
        "BlacklistSnapshotV1",
        "CapitalRuntimeV1",
        "CaseState",
        "DecisionRuntimeV1",
        "ExecutionCapabilitySnapshotV1",
        "ExecutionInstrumentCapabilityV1",
        "ExecutionQuote",
        "ExecutionQuoteAuditV1",
        "ExecutionQuoteRejectionV1",
        "ExecutionQuoteSnapshotV1",
        "ExecutionUniverseCandidateRow",
        "InstrumentRef",
        "IntentOutcome",
        "IntentReasonCode",
        "ProviderInstrumentCandidateV1",
        "RejectedReason",
        "ReplayArtifactV1",
        "ReplayBarV1",
        "ReplayExecutionIntentV1",
        "ReplayReceiptV1",
        "ReplayScenarioCapabilityV1",
        "ReplaySpecV1",
        "ReplayTerminalOutcomeV1",
        "StableCapabilityExclusionV1",
        "SubmissionFenceV1",
        "TradeIntent",
        "TradingCaseManifest",
        "VenueBinding",
        "VenueBindingRuntimeV1",
        "VenueInstrumentCatalogEntryV1",
        "VenueInstrumentCatalogSnapshotV1",
        "build_venue_catalog_snapshot",
        "deterministic_client_order_id",
        "validate_entry_quote",
    ]
    assert "TradingRepository" not in trading.__dict__
    assert "CapitalLane" not in trading.__dict__
    assert "__getattr__" not in trading.__dict__


def test_execution_quote_authority_is_trading_owned_and_nautilus_only_converts_ticks() -> None:
    domain = SRC / "trading/quote_authority.py"
    adapter = SRC / "integrations/nautilus/quote_authority.py"

    assert not any(module.startswith("nautilus_trader") for module in _imported_modules(domain))
    adapter_names = _executable_names(adapter)
    assert "execution_quote_from_nautilus" in adapter_names
    assert "validate_entry_quote" not in adapter_names


def test_trading_enabled_controls_only_decision_not_the_public_catalog() -> None:
    from tracefold.app.workers.wiring.trading import _wire_capital_lane, _wire_venue_catalog
    from tracefold.platform.config.models import Settings
    from tracefold.trading.catalog import VenueCatalog

    settings = Settings()
    assert settings.trading.enabled is False
    assert _wire_capital_lane(settings=settings, db=object()) is None  # type: ignore[arg-type]

    class Database:
        def heavy_business(self) -> object:
            return object()

    assert isinstance(_wire_venue_catalog(db=Database()), VenueCatalog)  # type: ignore[arg-type]


def test_an_enabled_trading_wiring_fault_propagates_instead_of_becoming_observer_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tracefold.app.workers.wiring import trading as wiring
    from tracefold.platform.config.models import Settings

    settings = Settings(trading={"enabled": True})

    def fail_generation(_settings: Settings) -> object:
        raise RuntimeError("generation_wiring_fault")

    class Database:
        def heavy_business(self) -> object:
            return object()

    monkeypatch.setattr(wiring, "active_arm_manifest", fail_generation)
    with pytest.raises(RuntimeError, match="generation_wiring_fault"):
        wiring._wire_capital_lane(settings=settings, db=Database())  # type: ignore[arg-type]


def test_the_workers_process_declares_exactly_one_capital_task_and_app_owns_its_loop() -> None:
    import asyncio

    from tracefold.app.workers.task_contract import worker_business_runners
    from tracefold.trading.capital_lane import CapitalLane, LaneTurn

    turns = 0

    class Lane:
        async def advance(self) -> LaneTurn:
            nonlocal turns
            turns += 1
            return LaneTurn(outcome="HALTED", reason="control_paused")

    async def exercise() -> None:
        runners = worker_business_runners(news_pipeline=None, capital_lane=Lane())  # type: ignore[arg-type]
        assert [label for label, _run in runners] == ["trading-capital-lane"]
        stop = asyncio.Event()
        stop.set()
        await asyncio.gather(*(run(stop) for _label, run in runners))

    asyncio.run(exercise())
    # The lane exposes one business action; the loop, the stop event and the poll interval are App's.
    assert not hasattr(CapitalLane, "run")
    assert not hasattr(CapitalLane, "runners")
    assert not hasattr(CapitalLane, "close")
    assert turns == 0  # a set stop event means the App loop never calls `advance`


def test_execution_configuration_is_one_bounded_notional() -> None:
    from pydantic import ValidationError

    from tracefold.platform.config.models import TradingOrderSettings, TradingSettings

    assert TradingOrderSettings().fixed_notional_usd == Decimal("10")
    with pytest.raises(ValidationError):
        TradingOrderSettings(fixed_notional_usd=Decimal("10.01"))
    with pytest.raises(ValidationError):
        TradingSettings.model_validate({"mode": "paper"})
    # No operator-owned Alpha threshold and no per-day model budget survive the hard cut.
    for retired in ("regime", "policy"):
        with pytest.raises(ValidationError):
            TradingSettings.model_validate({retired: {}})


def test_only_binance_bars_are_reachable_from_the_live_lane() -> None:
    import inspect

    from tracefold.app.workers.wiring.trading import _binance_bars

    signature = inspect.signature(_binance_bars)
    assert list(signature.parameters) == ["provider_symbol", "start_ms", "end_ms"]
