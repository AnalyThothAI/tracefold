from __future__ import annotations

import pytest
from pydantic import ValidationError

from tracefold.platform.config.models import (
    Settings,
    manual_trading_profile_availability,
    onchain_trading_profile_availability,
)

USER_A = 8385255219
USER_B = 8385255220


def _profile(user_id: int) -> dict[str, object]:
    user = str(user_id)
    return {
        "user_id": user_id,
        "manual": {
            "enabled": True,
            "live_trading_acknowledged": True,
            "account_ref": f"tg-{user}",
            "api_key_file": f"trading_profiles/manual/{user}/binance_api_key",
            "api_secret_file": f"trading_profiles/manual/{user}/binance_api_secret",
        },
        "onchain": {
            "enabled": True,
            "providers": {
                "okx": {
                    "enabled": True,
                    "api_key_file": f"trading_profiles/quotes/{user}/okx_api_key",
                    "api_secret_file": f"trading_profiles/quotes/{user}/okx_api_secret",
                    "passphrase_file": f"trading_profiles/quotes/{user}/okx_passphrase",
                },
                "oneinch": {"enabled": False},
                "binance": {"enabled": True},
            },
            "wallet": {
                "address": f"0x{user_id:040x}",
                "private_key_file": f"trading_profiles/onchain/{user}/evm_private_key",
                "live_trading_acknowledged": True,
            },
        },
    }


def _settings(*profiles: dict[str, object]) -> Settings:
    users = [int(profile["user_id"]) for profile in profiles]
    return Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_ids": [-1001234567890, -987654321, *users],
                },
            },
            "trading": {"telegram_profiles": list(profiles)},
        }
    )


def test_each_private_user_resolves_only_its_own_complete_trading_profile() -> None:
    settings = _settings(_profile(USER_A), _profile(USER_B))

    profile_a = settings.trading.telegram_profile(USER_A)
    profile_b = settings.trading.telegram_profile(USER_B)

    assert profile_a is not None and profile_b is not None
    assert profile_a.manual.account_ref == f"tg-{USER_A}"
    assert profile_b.manual.account_ref == f"tg-{USER_B}"
    assert settings.trading_manual_api_key_file(profile_a) != settings.trading_manual_api_key_file(profile_b)
    assert settings.trading_onchain_wallet_private_key_file(profile_a) != (
        settings.trading_onchain_wallet_private_key_file(profile_b)
    )
    assert manual_trading_profile_availability(settings, profile_a, inspect_secret_files=False).interaction_available
    assert onchain_trading_profile_availability(settings, profile_b, inspect_secret_files=False).interaction_available


def test_profile_must_be_an_exact_private_delivery_target() -> None:
    profile = _profile(USER_A)
    with pytest.raises(ValidationError, match="telegram_trading_profile_delivery_target_missing"):
        Settings.model_validate(
            {
                "news": {
                    "enabled": True,
                    "push": {
                        "enabled": True,
                        "telegram_bot_token_file": "telegram_bot_token",
                        "telegram_chat_ids": [-1001234567890],
                    },
                },
                "trading": {"telegram_profiles": [profile]},
            }
        )


def test_two_users_cannot_reuse_the_same_secret_file() -> None:
    profile_a = _profile(USER_A)
    profile_b = _profile(USER_B)
    profile_b["manual"]["api_key_file"] = profile_a["manual"]["api_key_file"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="telegram_trading_profile_secret_reuse"):
        _settings(profile_a, profile_b)


def test_two_users_cannot_bind_the_same_onchain_wallet() -> None:
    profile_a = _profile(USER_A)
    profile_b = _profile(USER_B)
    profile_b["onchain"]["wallet"]["address"] = profile_a["onchain"]["wallet"]["address"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="telegram_trading_profile_wallet_duplicate"):
        _settings(profile_a, profile_b)
