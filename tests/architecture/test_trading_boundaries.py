"""`tracefold.trading` boundaries (#104): what it may import, what it may reach, and what it may not be.

The dependency direction is checked generically in `test_backend_boundaries`. What this file adds is
the set of claims specific to a capability that moves money:

* the online path holds no agent framework at all — not DeepAgents, LangGraph, ReAct or a tool loop;
* Trading owns no News table and News owns no Trading table;
* nothing in the package reads a credential, a filesystem or a shell;
* a disabled Trading context constructs no program, no adapter and no runner;
* a live mode without a provider contract fails at startup, not at the first order.
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


def test_the_public_interface_is_the_package_root() -> None:
    from tracefold import trading

    assert "TradingRepository" in trading.__all__
    assert "decide" in trading.__all__
    assert "__getattr__" not in trading.__dict__


def test_a_disabled_trading_context_constructs_nothing() -> None:
    from tracefold.app.workers.wiring.trading import _wire_trading_pipeline
    from tracefold.platform.config.models import Settings

    settings = Settings()
    assert settings.trading.enabled is False
    assert _wire_trading_pipeline(settings=settings, db=object()) is None  # type: ignore[arg-type]


def test_a_live_mode_without_a_provider_contract_fails_at_startup() -> None:
    from pydantic import ValidationError

    from tracefold.platform.config.models import TradingSettings

    with pytest.raises(ValidationError, match="trading_live_mode_requires_opentrade"):
        TradingSettings(enabled=True, mode="live_bounded")


def test_a_live_mode_without_an_execution_adapter_refuses_to_compose() -> None:
    from tracefold.trading import TradingConfig, build_pipeline

    with pytest.raises(ValueError, match="trading_live_mode_requires_execution_adapter"):
        build_pipeline(
            db=object(),
            config=TradingConfig(mode="live_bounded"),
            bars=lambda _venue: None,
            candidate_projection=lambda *_: ((), ()),
            instrument_projection=lambda *_: (),
        )


def test_the_regime_band_must_have_a_ceiling() -> None:
    """A floor-only pre-move filter keeps exactly the chasing trades the measurement rejects."""

    from pydantic import ValidationError

    from tracefold.platform.config.models import TradingRegimeSettings

    with pytest.raises(ValidationError, match="trading_regime_band_invalid"):
        TradingRegimeSettings(min_price_move_bps=600, max_price_move_bps=100)


def test_the_pipeline_exposes_exactly_two_runners() -> None:
    from tracefold.trading import TradingConfig, build_pipeline

    pipeline = build_pipeline(
        db=object(),
        config=TradingConfig(),
        bars=lambda _venue: None,
        candidate_projection=lambda *_: ((), ()),
        instrument_projection=lambda *_: (),
    )
    assert [name for name, _ in pipeline.runners()] == ["trading-candidate", "trading-reconcile"]


def test_the_worst_case_daily_envelope_is_derivable_from_configuration_alone() -> None:
    from tracefold.platform.config.models import TradingOrderSettings

    order = TradingOrderSettings()
    # notional 50 x 200 bps x 4 orders = 4 USD. An operator can sign off on a multiplication.
    assert order.worst_case_daily_loss_usd == Decimal("4")


def test_a_trading_wiring_failure_never_takes_the_workers_process_down() -> None:
    """Trading's failure being local has to include its own composition.

    A `live_reviewed` mode with an OpenTrade base URL and token file is config-valid and documented,
    but no execution adapter ships yet, so `build_pipeline` raises. Unguarded, that propagated out of
    `_wire_components` into the fatal path and crash-looped the process — News never started either.
    """

    from tracefold.app.workers.wiring import trading as workers_module

    calls: list[str] = []

    def _boom(**_: object) -> object:
        calls.append("built")
        raise ValueError("trading_live_mode_requires_execution_adapter")

    original = workers_module.build_trading_pipeline
    workers_module.build_trading_pipeline = _boom  # type: ignore[assignment]
    try:
        settings = _live_reviewed_settings()
        assert workers_module._wire_trading_pipeline(settings=settings, db=_FakeDb()) is None
    finally:
        workers_module.build_trading_pipeline = original  # type: ignore[assignment]
    assert calls == ["built"]


def test_the_bar_fetcher_reads_the_same_venue_switch_the_router_reads() -> None:
    """Two switches for one decision is the shape #126 removed from the Strategy allowlist.

    Routing read `trading.venues`; candle fetching read `news.venues`. With the two disagreeing the
    router picked a venue whose fetcher returned None and every case died at `no_price_fail_closed`
    with nothing naming the contradiction.
    """

    from tracefold.app.workers.wiring.trading import _trading_bar_fetcher
    from tracefold.platform.config.models import Settings

    settings = Settings.model_validate(
        {
            "news": {"venues": {"binance": False, "hyperliquid": False}},
            "trading": {"enabled": True, "venues": {"binance_enabled": True, "hyperliquid_enabled": False}},
        }
    )
    factory = _trading_bar_fetcher(settings)
    assert factory("binance") is not None
    assert factory("hyperliquid") is None


class _FakeDb:
    def heavy_business(self) -> object:  # pragma: no cover - only the attribute is needed
        return self


def _live_reviewed_settings() -> object:
    from tracefold.platform.config.models import Settings

    return Settings.model_validate(
        {
            "trading": {
                "enabled": True,
                "mode": "live_reviewed",
                "opentrade": {"base_url": "https://example.invalid", "token_file": "opentrade_token"},
            }
        }
    )
