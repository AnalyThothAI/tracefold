from __future__ import annotations

from pathlib import Path

from tracefold.app.cli.commands.trading import _execution_capability
from tracefold.platform.config.models import Settings


def _live_settings(token_file: Path) -> Settings:
    return Settings.model_validate(
        {
            "trading": {
                "enabled": True,
                "mode": "live_reviewed",
                "live_symbol": "DOGE",
                "venues": {"binance_enabled": True, "hyperliquid_enabled": False},
                "order": {"fixed_notional_usd": 10, "max_open_underlyings": 1, "max_orders_per_day": 1},
                "opentrade": {"base_url": "https://example.invalid", "token_file": str(token_file)},
            }
        }
    )


def test_status_never_reports_live_ready_from_configuration_alone(tmp_path: Path) -> None:
    token_file = tmp_path / "opentrade_token"
    token_file.write_text("test-token", encoding="utf-8")
    token_file.chmod(0o600)
    assert _execution_capability(_live_settings(token_file)) == {
        "execution_backend": "opentrade_read_only",
        "execution_configured": True,
        "live_mode_supported": False,
        "live_ready": False,
        "live_readiness": "not_proven",
    }


def test_status_reports_an_unreadable_provider_contract_without_exposing_the_token(tmp_path: Path) -> None:
    missing = tmp_path / "missing-token"
    capability = _execution_capability(_live_settings(missing))
    assert capability["execution_configured"] is False
    assert capability["live_ready"] is False
