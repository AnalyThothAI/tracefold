"""Manual Telegram trading is disabled by default and fails closed on authority ambiguity."""

from __future__ import annotations

from pathlib import Path

from tracefold.platform.config.models import Settings, manual_trading_availability

CHANNEL_ID = -1001234567890
OPERATOR_ID = 123456789
BOT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345"


def _secure(path: Path, value: str) -> None:
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def _settings(tmp_path: Path, *, manual: dict[str, object] | None = None) -> Settings:
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_id": CHANNEL_ID,
                },
            },
            "trading": {"manual": manual or {}},
        }
    )
    settings.set_config_dir(tmp_path)
    return settings


def test_manual_trading_is_disabled_by_default(tmp_path: Path) -> None:
    availability = manual_trading_availability(_settings(tmp_path))

    assert availability.requested is False
    assert availability.interaction_available is False
    assert availability.reason is None


def test_manual_trading_requires_authorized_user_and_distinct_secure_credentials(tmp_path: Path) -> None:
    _secure(tmp_path / "telegram_bot_token", BOT_TOKEN)
    _secure(tmp_path / "binance_manual_demo_api_key", "manual-key")
    _secure(tmp_path / "binance_manual_demo_api_secret", "manual-secret")
    settings = _settings(
        tmp_path,
        manual={"enabled": True, "authorized_user_ids": [OPERATOR_ID]},
    )

    availability = manual_trading_availability(settings)

    assert availability.interaction_available is True
    assert availability.authorized_user_count == 1
    assert availability.credentials_configured is True
    assert availability.venue == "binance_usdm_demo"


def test_manual_trading_rejects_missing_operator_allowlist(tmp_path: Path) -> None:
    _secure(tmp_path / "telegram_bot_token", BOT_TOKEN)
    _secure(tmp_path / "binance_manual_demo_api_key", "manual-key")
    _secure(tmp_path / "binance_manual_demo_api_secret", "manual-secret")

    availability = manual_trading_availability(_settings(tmp_path, manual={"enabled": True}))

    assert availability.interaction_available is False
    assert availability.reason == "manual_trading_authorized_users_missing"


def test_manual_trading_rejects_auto_account_credential_reuse_even_across_different_files(
    tmp_path: Path,
) -> None:
    _secure(tmp_path / "telegram_bot_token", BOT_TOKEN)
    _secure(tmp_path / "binance_demo_api_key", "same-key")
    _secure(tmp_path / "binance_demo_api_secret", "same-secret")
    _secure(tmp_path / "binance_manual_demo_api_key", "same-key")
    _secure(tmp_path / "binance_manual_demo_api_secret", "same-secret")
    settings = _settings(
        tmp_path,
        manual={"enabled": True, "authorized_user_ids": [OPERATOR_ID]},
    )
    settings.trading.enabled = True

    availability = manual_trading_availability(settings)

    assert availability.interaction_available is False
    assert availability.reason == "manual_trading_account_credential_reuse"


def test_serve_reports_configuration_without_reading_manual_secret_files(tmp_path: Path) -> None:
    availability = manual_trading_availability(
        _settings(
            tmp_path,
            manual={"enabled": True, "authorized_user_ids": [OPERATOR_ID]},
        ),
        inspect_secret_files=False,
    )

    assert availability.interaction_available is True
    assert availability.credentials_configured is True
