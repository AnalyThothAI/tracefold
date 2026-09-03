"""Architecture proof for the engine-neutral Signal boundary."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "tracefold"
TRADING = SRC / "trading"
NEWS = SRC / "news"

SIGNAL_PATH = (
    "trading/signal_lane.py",
    "trading/policy.py",
    "trading/admission.py",
    "trading/sources.py",
    "trading/market_context.py",
    "trading/contracts.py",
    "trading/telemetry.py",
    "trading/storage/root.py",
    "trading/storage/lane.py",
    "trading/storage/gate.py",
    "trading/storage/queries.py",
    "app/workers/wiring/trading.py",
    "app/workers/wiring/news_to_trading.py",
    "app/trading_config.py",
)
# Offline and one-shot (#459 Stage A). Never imported by the Signal path, never on a live clock, and
# the reason the Signal path's capability ban does not reach it: reading a sealed corpus off disk is
# what it is for.
RESEARCH = {
    "trading/research/oi_corpus.py",
    "trading/research/oi_replay.py",
}
EXECUTION_PATH = {
    "trading/execution_contracts.py",
    "trading/notification_policy.py",
    "trading/operator_control.py",
    "trading/storage/execution_stream.py",
}
BANNED_FRAMEWORKS = {"autogen", "crewai", "deepagents", "dspy", "langchain", "langgraph", "langsmith"}
BANNED_CAPABILITIES = {"boto3", "httpx", "os", "pathlib", "requests", "shutil", "socket", "subprocess"}
# Any `news_`-prefixed name at all: the tables, the version pins, the trace keys. Trading may not
# spell one, in SQL, in a `Literal`, in a reason string or in a comment.
NEWS_NAME_RE = re.compile(r"\bnews_[a-z0-9_]+", re.I)
WRITE_SQL_TABLE_RE = re.compile(r"\b(?:DELETE\s+FROM|INSERT\s+INTO|UPDATE)\s+(?P<table>[a-z][a-z0-9_]*)", re.I)
SQL_TABLE_RE = re.compile(r"\b(?:DELETE\s+FROM|INSERT\s+INTO|FROM|JOIN|UPDATE)\s+(?P<table>[a-z][a-z0-9_]*)", re.I)
_SQL_KEYWORDS = {
    "batch",
    "candidate",
    "delivered",
    "identity_guard",
    "inserted",
    "jsonb_array_elements",
    "jsonb_each",
    "lateral",
    "of",
    "offered",
    "select",
    "set",
    "skip",
    "unnest",
    "values",
}


def _module_name(path: Path) -> str:
    if not path.is_relative_to(SRC.parent):
        return path.stem
    parts = list(path.relative_to(SRC.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                parts = package.split(".")
                base_parts = parts[: len(parts) - node.level + 1]
                base = ".".join([*base_parts, node.module] if node.module else base_parts)
            if base:
                result.add(base)
                result.update(f"{base}.{alias.name}" for alias in node.names)
    return result


def _sql_literals(path: Path) -> list[str]:
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


def _trading_sources() -> list[Path]:
    return sorted(path for path in TRADING.rglob("*.py") if "__pycache__" not in path.parts)


def test_signal_path_manifest_is_complete_and_every_trading_module_is_classified() -> None:
    assert SIGNAL_PATH
    assert [relative for relative in SIGNAL_PATH if not (SRC / relative).is_file()] == []
    classified = set(SIGNAL_PATH) | EXECUTION_PATH | RESEARCH
    unclassified = [
        str(path.relative_to(SRC))
        for path in _trading_sources()
        if path.name != "__init__.py" and str(path.relative_to(SRC)) not in classified
    ]
    assert unclassified == []


def test_signal_path_has_no_model_framework_shell_filesystem_or_arbitrary_http_client() -> None:
    offenders: list[str] = []
    for relative in SIGNAL_PATH:
        path = SRC / relative
        if path.is_relative_to(SRC / "app"):
            continue
        roots = {module.split(".")[0] for module in _imports(path)}
        offenders.extend(f"{relative}:{name}" for name in sorted((BANNED_FRAMEWORKS | BANNED_CAPABILITIES) & roots))
    assert offenders == []


def test_offline_news_research_does_not_expand_the_signal_path(tmp_path: Path) -> None:
    fixture = tmp_path / "research.py"
    fixture.write_text("import dspy\nfrom deepagents import Agent\n", encoding="utf-8")
    assert {name.split(".")[0] for name in _imports(fixture)} == {"deepagents", "dspy"}
    assert fixture not in [SRC / relative for relative in SIGNAL_PATH]


def test_news_and_trading_never_import_each_other() -> None:
    news_offenders = [
        f"{path.relative_to(ROOT)}:{module}"
        for path in NEWS.rglob("*.py")
        if "__pycache__" not in path.parts
        for module in _imports(path)
        if module == "tracefold.trading" or module.startswith("tracefold.trading.")
    ]
    trading_offenders = [
        f"{path.relative_to(ROOT)}:{module}"
        for path in _trading_sources()
        for module in _imports(path)
        if module == "tracefold.news" or module.startswith("tracefold.news.")
    ]
    assert news_offenders == []
    assert trading_offenders == []


def test_relative_imports_resolve_to_full_module_paths() -> None:
    modules = _imports(TRADING / "signal_lane.py")
    assert "tracefold.trading.policy" in modules
    assert "tracefold.trading.storage.root" in modules


def test_trading_sql_reads_and_writes_only_trading_tables() -> None:
    offenders: list[str] = []
    for path in _trading_sources():
        source = ";\n".join(_sql_literals(path))
        for pattern in (WRITE_SQL_TABLE_RE, SQL_TABLE_RE):
            for match in pattern.finditer(source):
                table = match.group("table").lower()
                if table not in _SQL_KEYWORDS and not table.startswith("trading_"):
                    offenders.append(f"{path.relative_to(ROOT)}:{table}")
    assert offenders == []


def test_trading_names_no_news_identity_anywhere_in_its_source() -> None:
    """#510 PR-4. A News version bump must not be able to reach this package.

    `news_oi_signal_v3`, `news_triage_policy_v12` and `news_judgment_v2` were `Literal`s on the
    candidate contract, re-checked in the Signal lane and executed a third time in News's SELECT, so
    #504's policy v11 -> v12 bump could only ship by editing Trading. Nothing upstream calls itself is
    a Trading rule: the measurements, the two clocks and the venue are, and none of them is spelled
    `news_`. The scan is over raw source rather than string constants because a pin restated in a
    comment is the same coupling one edit later.
    """

    offenders = [
        f"{path.relative_to(ROOT)}:{match.group(0)}"
        for path in _trading_sources()
        for match in NEWS_NAME_RE.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_news_never_reads_or_writes_a_trading_table() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in NEWS.rglob("*.py")
        if "__pycache__" not in path.parts
        for match in SQL_TABLE_RE.finditer(";\n".join(_sql_literals(path)))
        if match.group("table").lower().startswith("trading_")
    ]
    assert offenders == []


def test_signal_path_has_no_execution_owner_or_nautilus_dependency() -> None:
    forbidden = ("capital_authority", "intent", "execution_policy", "quote_authority", "capabilities", "bindings")
    offenders = [
        f"{relative}:{module}"
        for relative in SIGNAL_PATH
        if relative.startswith("trading/")
        for module in _imports(SRC / relative)
        if "nautilus" in module or any(name in module for name in forbidden)
    ]
    assert offenders == []


def test_legacy_execution_cluster_is_deleted_without_forwarders() -> None:
    retired = (
        "trading/capital_lane.py",
        "trading/capital_authority.py",
        "trading/intent.py",
        "trading/execution_policy.py",
        "trading/quote_authority.py",
        "trading/adapter_contracts.py",
        "trading/capabilities.py",
        "trading/bindings.py",
        "trading/contract_receipt.py",
        "trading/storage/authority.py",
        "trading/storage/intents.py",
        "trading/storage/capabilities.py",
        "trading/storage/bindings.py",
        "trading/storage/control.py",
        "app/nautilus/database.py",
        "integrations/nautilus/strategy.py",
        "integrations/nautilus/messages.py",
        "integrations/nautilus/execution_adapter.py",
    )
    assert [relative for relative in retired if (SRC / relative).exists()] == []


def test_live_wiring_uses_source_native_public_bars_and_no_model_runner() -> None:
    modules = _imports(SRC / "app/workers/wiring/trading.py")
    assert "tracefold.integrations.venues.fetch_binance_candles" in modules
    assert "tracefold.integrations.venues.fetch_hyperliquid_candles" in modules
    assert not any("dspy" in module.lower() for module in modules)


def test_package_root_exports_only_current_app_facing_values() -> None:
    from tracefold import trading

    assert trading.__all__ == [
        "EXECUTION_STRATEGY_ID",
        # The identity shapes and the durable append bounds every Trading fact is checked against.
        # The Runtime reads them from here instead of re-typing its own copies (#510 E).
        "IDENTITY_PATTERN",
        "MARKET_KEY_PATTERN",
        "MAX_OBSERVATION_APPEND_BATCH",
        "MAX_OBSERVATION_APPEND_BYTES",
        "SHA256_PATTERN",
        "AlphaDecision",
        "Bar",
        "CaseState",
        "ExecutionAccountOrder",
        "ExecutionAccountPosition",
        "ExecutionAccountSnapshot",
        "ExecutionObservationV1",
        "OiTradeCandidate",
        "OperatorCommandError",
        "OperatorIntentV1",
        "ParsedOperatorCommand",
        "PreparedOperatorIntent",
        "TradeSignalV1",
        "TradingCaseManifest",
        "canonical_sha256",
        "parse_operator_command",
        "postgres_text_valid",
        "prepare_execution_observations",
        "prepare_parsed_operator_intent",
    ]
    assert "TradingRepository" not in trading.__dict__
    assert "CapitalLane" not in trading.__dict__
    assert "TradeIntent" not in trading.__dict__
    assert "__getattr__" not in trading.__dict__


def test_disabled_trading_constructs_no_signal_lane() -> None:
    from tracefold.app.workers.wiring.trading import _wire_signal_lane
    from tracefold.platform.config.models import Settings

    settings = Settings()
    assert settings.trading.enabled is False
    assert _wire_signal_lane(settings=settings, db=object()) is None  # type: ignore[arg-type]


def test_enabled_signal_wiring_fault_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    from tracefold.app.workers.wiring import trading as wiring
    from tracefold.platform.config.models import Settings

    def fail_config(_settings: Settings) -> object:
        raise RuntimeError("signal_lane_wiring_fault")

    class Database:
        def heavy_business(self) -> object:
            return object()

    monkeypatch.setattr(wiring, "signal_lane_config", fail_config)
    with pytest.raises(RuntimeError, match="signal_lane_wiring_fault"):
        wiring._wire_signal_lane(  # type: ignore[arg-type]
            settings=Settings(trading={"enabled": True}),
            db=Database(),
        )


def test_enabled_signal_wiring_reads_no_news_learning_arm() -> None:
    """#510 PR-4: the seam hands the lane a projection reader, not a News cohort label."""

    import inspect

    from tracefold.app.workers.wiring import trading as wiring
    from tracefold.trading.signal_lane import SignalLane

    assert "news_generation" not in inspect.signature(SignalLane.__init__).parameters
    modules = _imports(SRC / "app/workers/wiring/trading.py")
    assert "tracefold.app.learning_runtime.active_arm_manifest" not in modules
    assert not any("learning" in module for module in modules)
    assert not hasattr(wiring, "epoch_id_for_bundle")


def test_workers_declares_one_signal_task_and_app_owns_its_loop() -> None:
    import asyncio

    from tracefold.app.workers.task_contract import worker_business_runners
    from tracefold.trading.signal_lane import LaneTurn, SignalLane

    turns = 0

    class Lane:
        async def advance(self) -> LaneTurn:
            nonlocal turns
            turns += 1
            return LaneTurn(outcome="HALTED", reason="disabled")

    async def exercise() -> None:
        runners = worker_business_runners(news_pipeline=None, signal_lane=Lane())  # type: ignore[arg-type]
        assert [label for label, _run in runners] == ["trading-signal-lane"]
        stop = asyncio.Event()
        stop.set()
        await asyncio.gather(*(run(stop) for _label, run in runners))

    asyncio.run(exercise())
    assert not hasattr(SignalLane, "run")
    assert turns == 0


def test_execution_configuration_has_no_alpha_sizing_or_route() -> None:
    from pydantic import ValidationError

    from tracefold.platform.config.models import TradingSettings

    settings = TradingSettings()
    assert settings.execution.mode == "disabled"
    for retired in ("order", "capital", "bindings", "venues", "fixed_notional_usd"):
        with pytest.raises(ValidationError):
            TradingSettings.model_validate({retired: {}})


def test_source_native_bar_fetcher_accepts_candidate_and_no_execution_route() -> None:
    import inspect

    from tracefold.app.workers.wiring.trading import _source_native_bars

    assert list(inspect.signature(_source_native_bars).parameters) == ["candidate", "start_ms", "end_ms"]
