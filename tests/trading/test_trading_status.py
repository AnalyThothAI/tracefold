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
        "execution_backend": "opentrade_reviewed",
        "execution_configured": True,
        "live_mode_supported": True,
        "live_ready": False,
        "live_readiness": "not_proven",
    }


def test_status_reports_an_unreadable_provider_contract_without_exposing_the_token(tmp_path: Path) -> None:
    missing = tmp_path / "missing-token"
    capability = _execution_capability(_live_settings(missing))
    assert capability["execution_configured"] is False
    assert capability["live_ready"] is False


def test_the_http_route_and_the_cli_answer_readiness_identically(tmp_path: Path) -> None:
    """#207 PR-W4. Two callers, one answer — or the console and the operator disagree about live readiness.

    The HTTP route duplicates six lines of branching rather than importing the CLI command, because that
    command loads settings, opens a repository session and returns exit codes. This is the pin that makes the
    duplication safe: it fails the moment either side gains a branch the other does not.
    """

    from tracefold.app.http.routes.trading import _execution_capability as http_capability

    token_file = tmp_path / "opentrade_token"
    token_file.write_text("test-token", encoding="utf-8")
    token_file.chmod(0o600)
    for settings in (
        Settings(ws_token="t"),
        Settings.model_validate({"trading": {"enabled": True, "mode": "paper"}}),
        _live_settings(token_file),
        _live_settings(tmp_path / "missing-token"),
    ):
        assert http_capability(settings) == _execution_capability(settings)


def test_the_http_route_never_offers_live_and_never_leaks_a_frozen_payload() -> None:
    """The two things this surface must never do, checked against the schema rather than the prose.

    A browser cannot reach an order write — there is no such route — but it could still be handed the frozen
    provider request body or the frozen decision manifest, which is how a read surface becomes an
    exfiltration surface one column at a time.
    """

    from tracefold.app.http.schemas import trading as schemas

    order_fields = set(schemas.TradingOrderData.model_fields)
    case_fields = set(schemas.TradingCaseData.model_fields)
    for leaked in ("payload", "payload_sha256", "manifest", "manifest_sha256", "account_ref", "remote_order_id"):
        assert leaked not in order_fields, leaked
        assert leaked not in case_fields, leaked
    # `live_ready` is a fact the page renders, and there is no field that could be read as an offer to change
    # it: no `live_enabled`, no `can_go_live`, nothing writable.
    readiness = set(schemas.TradingReadinessData.model_fields)
    assert "live_ready" in readiness and "live_readiness" in readiness
    assert not {field for field in readiness if field.startswith(("set_", "enable_", "allow_"))}
