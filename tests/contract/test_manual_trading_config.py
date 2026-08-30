"""Manual Telegram trading is disabled by default and bound to one private user."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tracefold.platform.config.models import Settings, manual_trading_availability

CHANNEL_ID = -1001234567890
OPERATOR_ID = 123456789
BOT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345"
USER = str(OPERATOR_ID)


def _secure(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def _manual(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "enabled": True,
        "live_trading_acknowledged": True,
        "account_ref": f"tg-{USER}",
        "api_key_file": f"trading_profiles/manual/{USER}/binance_api_key",
        "api_secret_file": f"trading_profiles/manual/{USER}/binance_api_secret",
    }
    value.update(overrides)
    return value


def _settings(
    tmp_path: Path,
    *,
    manual: dict[str, object] | None = None,
    bot_token_file: str | None = None,
    bindings: dict[str, object] | None = None,
) -> Settings:
    trading: dict[str, object] = {}
    if manual is not None:
        trading["telegram_profiles"] = [{"user_id": OPERATOR_ID, "manual": manual}]
    if bindings is not None:
        trading["bindings"] = bindings
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": bot_token_file or "telegram_bot_token",
                    "telegram_chat_ids": [CHANNEL_ID, OPERATOR_ID],
                },
            },
            "trading": trading,
        }
    )
    settings.set_config_dir(tmp_path)
    return settings


def test_manual_trading_is_disabled_by_default(tmp_path: Path) -> None:
    availability = manual_trading_availability(_settings(tmp_path))

    assert availability.requested is False
    assert availability.interaction_available is False
    assert availability.reason is None


def test_manual_trading_uses_only_the_closed_live_usdm_venue(tmp_path: Path) -> None:
    _secure(tmp_path / "telegram_bot_token", BOT_TOKEN)
    _secure(tmp_path / f"trading_profiles/manual/{USER}/binance_api_key", "manual-key")
    _secure(tmp_path / f"trading_profiles/manual/{USER}/binance_api_secret", "manual-secret")

    settings = _settings(tmp_path, manual=_manual())
    availability = manual_trading_availability(settings)
    profile = settings.trading.telegram_profile(OPERATOR_ID)

    assert profile is not None
    assert profile.manual.venue == "binance_usdm_live"
    assert availability.interaction_available is True
    assert availability.venue == "binance_usdm_live"

    with pytest.raises(ValidationError):
        _settings(tmp_path, manual=_manual(venue="binance_usdm_demo"))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("api_key_file", "custom_manual_key"),
        ("api_secret_file", "/var/empty/custom_manual_secret"),
    ),
)
def test_manual_trading_requires_user_scoped_secret_names(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match="manual_trading_profile_secret_name_invalid"):
        _settings(tmp_path, manual=_manual(**{field: value}))


def test_trading_profile_requires_the_fixed_bot_token_mount(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="telegram_trading_bot_token_name_invalid"):
        _settings(tmp_path, manual=_manual(), bot_token_file="custom_telegram_token")


def test_manual_live_trading_requires_an_explicit_operator_acknowledgement(tmp_path: Path) -> None:
    _secure(tmp_path / "telegram_bot_token", BOT_TOKEN)
    _secure(tmp_path / f"trading_profiles/manual/{USER}/binance_api_key", "manual-key")
    _secure(tmp_path / f"trading_profiles/manual/{USER}/binance_api_secret", "manual-secret")

    availability = manual_trading_availability(_settings(tmp_path, manual=_manual(live_trading_acknowledged=False)))

    assert availability.interaction_available is False
    assert availability.reason == "manual_live_trading_not_acknowledged"


def test_manual_trading_requires_distinct_secure_credentials(tmp_path: Path) -> None:
    _secure(tmp_path / "telegram_bot_token", BOT_TOKEN)
    _secure(tmp_path / "binance_usdm_api_key", "same-key")
    _secure(tmp_path / "binance_usdm_api_secret", "same-secret")
    _secure(tmp_path / f"trading_profiles/manual/{USER}/binance_api_key", "same-key")
    _secure(tmp_path / f"trading_profiles/manual/{USER}/binance_api_secret", "same-secret")
    settings = _settings(
        tmp_path,
        manual=_manual(),
        bindings={
            "binance_usdm": {
                "api_key_file": "binance_usdm_api_key",
                "api_secret_file": "binance_usdm_api_secret",
            }
        },
    )

    availability = manual_trading_availability(settings)

    assert availability.interaction_available is False
    assert availability.reason == "manual_trading_account_credential_reuse"


def test_serve_reports_configuration_without_reading_manual_secret_files(tmp_path: Path) -> None:
    availability = manual_trading_availability(
        _settings(tmp_path, manual=_manual()),
        inspect_secret_files=False,
    )

    assert availability.interaction_available is True
    assert availability.credentials_configured is True
