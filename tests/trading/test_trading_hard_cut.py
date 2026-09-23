"""Analysis owns current Signals and obsolete OI v5 modules stay removed."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracefold.app.http.app import create_app
from tracefold.platform.config.models import Settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "retired",
    (
        {"order": {"fixed_notional_usd": "10"}},
        {"bindings": {"binance": {}}},
        {"capital": {"mode": "paused"}},
        {"venues": {"hyperliquid_enabled": True}},
        {"nautilus": {"accept_intents": True}},
        {"candidates": {"max_age_seconds": 300}},
    ),
)
def test_retired_execution_configuration_fails_closed(retired: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"trading": retired})


def test_public_http_is_case_execution_replay_and_readiness_only() -> None:
    """#537 PR-5, #589 PR-2, #683. Retired execution surfaces stay deleted.

    The Signal list and the raw observation stream were two more public shapes over the ledgers
    `/api/trading/executions` already reads folded, and nothing in the browser called either. The two
    admission-ledger routes left on the same terms: #553 PR-1 deleted the OI frame table that joined
    each row to its Event, which was their only browser reader.
    """

    schema = create_app(settings=Settings(ws_token="schema-test")).openapi()
    paths = set(schema["paths"])

    assert {path for path in paths if path.startswith("/api/trading/")} == {
        "/api/trading/status",
        "/api/trading/cases",
        "/api/trading/cases/{case_id}/replay",
        "/api/trading/executions",
    }
    assert "/api/trading/execution/commands" not in paths
    for retired in ("/api/trading/gate", "/api/trading/gate/{event_id}"):
        assert retired not in paths, retired


def test_active_signal_path_has_no_execution_or_nautilus_import() -> None:
    paths = (
        ROOT / "tracefold/trading/engine/policy.py",
        ROOT / "tracefold/trading/engine/target.py",
        ROOT / "tracefold/trading/engine/features.py",
    )
    forbidden = (
        "nautilus",
        "capital_authority",
        "intent",
        "execution_policy",
        "quote_authority",
        "capabilities",
        "bindings",
    )
    for path in paths:
        modules = {
            node.module or ""
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom)
        }
        assert not any(token in module for module in modules for token in forbidden)


def test_legacy_execution_modules_are_deleted_instead_of_forwarded() -> None:
    retired = (
        "tracefold/trading/signal_lane.py",
        "tracefold/trading/policy.py",
        "tracefold/trading/admission.py",
        "tracefold/trading/sources.py",
        "tracefold/trading/market_context.py",
        "tracefold/trading/storage/lane.py",
        "tracefold/trading/contracts.py",
        "tracefold/trading/telemetry.py",
        "tracefold/app/trading_config.py",
        "tracefold/app/workers/wiring/trading.py",
        "tracefold/app/workers/wiring/news_to_trading.py",
        "tracefold/trading/capital_authority.py",
        "tracefold/trading/intent.py",
        "tracefold/trading/execution_policy.py",
        "tracefold/trading/quote_authority.py",
        "tracefold/trading/adapter_contracts.py",
        "tracefold/trading/capabilities.py",
        "tracefold/trading/bindings.py",
        "tracefold/trading/contract_receipt.py",
        "tracefold/app/nautilus/database.py",
        "tracefold/integrations/nautilus/strategy.py",
        "tracefold/integrations/nautilus/messages.py",
        "tracefold/integrations/nautilus/execution_adapter.py",
    )
    assert [name for name in retired if (ROOT / name).exists()] == []
