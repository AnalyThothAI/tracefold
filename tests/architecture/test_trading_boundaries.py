"""`tracefold.trading` boundaries (#104): what it may import, what it may reach, and what it may not be.

The dependency direction is checked generically in `test_backend_boundaries`. What this file adds is
the set of claims specific to a capability that moves money:

* the online path holds no agent framework at all — not DeepAgents, LangGraph, ReAct or a tool loop;
* Trading owns no News table and News owns no Trading table;
* nothing in the package reads a credential, a filesystem or a shell;
* a disabled Trading context constructs no program or runner;
* the retired execution writers and backend switches are absent.
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

BANNED_FRAMEWORKS = ("deepagents", "langgraph", "langchain", "langsmith", "autogen", "crewai")
BANNED_CAPABILITIES = ("subprocess", "shutil", "socket", "requests", "boto3")
WRITE_SQL_TABLE_RE = re.compile(r"\b(?:DELETE\s+FROM|INSERT\s+INTO|UPDATE)\s+(?P<table>[a-z][a-z0-9_]*)", re.IGNORECASE)
SQL_TABLE_RE = re.compile(r"\b(?:DELETE\s+FROM|INSERT\s+INTO|FROM|JOIN|UPDATE)\s+(?P<table>[a-z][a-z0-9_]*)", re.I)
# `DO UPDATE SET` and `FOR UPDATE SKIP LOCKED` both put a keyword where the regex expects a table.
_SQL_KEYWORDS = frozenset({"set", "skip", "select", "lateral", "jsonb_each", "values"})


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _trading_sources() -> list[Path]:
    return sorted(path for path in TRADING.rglob("*.py") if "__pycache__" not in path.parts)


def test_the_online_trading_path_holds_no_agent_framework() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        roots = _imported_roots(path)
        offenders.extend(f"{path.relative_to(ROOT)}:{banned}" for banned in BANNED_FRAMEWORKS if banned in roots)
    assert offenders == []


def test_trading_reaches_no_shell_filesystem_or_arbitrary_http_client() -> None:
    offenders: list[str] = []
    for path in _trading_sources():
        roots = _imported_roots(path)
        offenders.extend(
            f"{path.relative_to(ROOT)}:{banned}"
            for banned in (*BANNED_CAPABILITIES, "os", "pathlib", "httpx")
            if banned in roots
        )
    # Provider I/O reaches Trading as an injected port, never as a client the package constructs.
    assert offenders == []


def test_execution_depends_only_on_contracts_and_execution_siblings() -> None:
    """Capital-write code must not reach back into candidate, pipeline, decision, or storage owners."""

    forbidden: list[str] = []
    for path in sorted((TRADING / "execution").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden.extend(
            f"{path.name}:{node.module}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 2 and node.module != "contracts"
        )
    assert forbidden == []


def test_trading_writes_only_trading_tables() -> None:
    offenders: list[str] = []
    for path in _trading_sources():
        for match in WRITE_SQL_TABLE_RE.finditer(path.read_text(encoding="utf-8")):
            table = match.group("table").lower()
            if table in _SQL_KEYWORDS:
                continue  # `ON CONFLICT ... DO UPDATE SET` is not a table name
            if not table.startswith("trading_"):
                offenders.append(f"{path.relative_to(ROOT)}:{table}")
    assert offenders == []


def test_trading_reads_only_trading_tables() -> None:
    """The News projections are the seam. Trading never reaches through to a News table itself."""

    offenders: list[str] = []
    for path in _trading_sources():
        for match in SQL_TABLE_RE.finditer(path.read_text(encoding="utf-8")):
            table = match.group("table").lower()
            if table in _SQL_KEYWORDS:
                continue
            if table.startswith("news_"):
                offenders.append(f"{path.relative_to(ROOT)}:{table}")
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


def test_news_never_reads_or_writes_a_trading_table() -> None:
    offenders: list[str] = []
    for path in NEWS.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        offenders.extend(
            str(path.relative_to(ROOT))
            for match in SQL_TABLE_RE.finditer(path.read_text(encoding="utf-8"))
            if match.group("table").lower().startswith("trading_")
        )
    assert offenders == []


def test_news_never_imports_trading() -> None:
    """A docstring may *name* the consumer; an import would make News depend on it."""

    offenders = [
        str(path.relative_to(ROOT))
        for path in NEWS.rglob("*.py")
        if "__pycache__" not in path.parts and "trading" in _imported_roots(path)
    ]
    assert offenders == []


def test_the_package_root_exports_only_app_facing_values_and_ports() -> None:
    from tracefold import trading

    assert trading.__all__ == [
        "ACTIVE_INTENT_STATES",
        "BAR_FIDELITY_VERSION",
        "INTENT_POLICY_SHA256",
        "Bar",
        "BlacklistSnapshotV1",
        "CaseState",
        "ExecutionCapabilitySnapshotV1",
        "ExecutionInstrumentCapabilityV1",
        "ExecutionUniverseCandidateRow",
        "InstrumentRef",
        "IntentOutcome",
        "IntentReasonCode",
        "ProviderInstrumentCandidateV1",
        "ReplayArtifactV1",
        "ReplayBarV1",
        "ReplayExecutionIntentV1",
        "ReplayReceiptV1",
        "ReplayScenarioCapabilityV1",
        "ReplaySpecV1",
        "ReplayTerminalOutcomeV1",
        "StableCapabilityExclusionV1",
        "TradeIntent",
        "TradingCaseManifest",
        "deterministic_client_order_id",
    ]
    assert "TradingRepository" not in trading.__dict__
    assert "CandidateRunner" not in trading.__dict__
    assert "decide" not in trading.__dict__
    assert "__getattr__" not in trading.__dict__


def test_a_disabled_trading_context_constructs_nothing() -> None:
    from tracefold.app.workers.wiring.trading import _wire_trading_pipeline
    from tracefold.platform.config.models import Settings

    settings = Settings()
    assert settings.trading.enabled is False
    assert _wire_trading_pipeline(settings=settings, db=object()) is None  # type: ignore[arg-type]


def test_trading_pipeline_exposes_only_the_candidate_runner() -> None:
    import asyncio

    from tracefold.app.workers.task_contract import worker_business_runners
    from tracefold.trading.pipeline.root import TradingPipeline

    started = False

    class Runner:
        async def run(self, *, stop_event: asyncio.Event) -> None:
            nonlocal started
            assert isinstance(stop_event, asyncio.Event)
            started = True

    async def exercise() -> None:
        runners = worker_business_runners(news_pipeline=None, trading_pipeline=TradingPipeline(candidate=Runner()))
        assert [label for label, _run in runners] == ["trading-candidate"]
        await asyncio.gather(*(run(asyncio.Event()) for _label, run in runners))

    asyncio.run(exercise())
    assert started


def test_execution_configuration_is_one_bounded_notional() -> None:
    from pydantic import ValidationError

    from tracefold.platform.config.models import TradingOrderSettings, TradingSettings

    assert TradingOrderSettings().fixed_notional_usd == Decimal("10")
    with pytest.raises(ValidationError):
        TradingOrderSettings(fixed_notional_usd=Decimal("10.01"))
    with pytest.raises(ValidationError):
        TradingSettings.model_validate({"mode": "paper"})


def test_bar_fetching_has_no_operator_backend_switch() -> None:
    from tracefold.app.workers.wiring.trading import _trading_bar_fetcher
    from tracefold.platform.config.models import Settings

    factory = _trading_bar_fetcher(Settings())
    assert factory("binance") is not None
    assert factory("hyperliquid") is not None
    assert factory("paper") is None
