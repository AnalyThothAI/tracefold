from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tracefold.platform.config.models import Settings, onchain_trading_availability

CHANNEL_ID = -1001234567890
OPERATOR_ID = 123456789
BOT_TOKEN = "123456:" + "a" * 32
USER = str(OPERATOR_ID)


def _secure(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def _onchain_profile(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "enabled": True,
        "providers": {
            "okx": {
                "enabled": True,
                "api_key_file": f"trading_profiles/quotes/{USER}/okx_api_key",
                "api_secret_file": f"trading_profiles/quotes/{USER}/okx_api_secret",
                "passphrase_file": f"trading_profiles/quotes/{USER}/okx_passphrase",
            },
            "oneinch": {"enabled": False},
        },
    }
    value.update(overrides)
    return value


def _settings(
    tmp_path: Path,
    *,
    onchain: dict[str, object],
    settlement_assets: list[dict[str, object]] | None = None,
) -> Settings:
    trading: dict[str, object] = {
        "telegram_profiles": [{"user_id": OPERATOR_ID, "onchain": onchain}],
    }
    if settlement_assets is not None:
        trading["onchain"] = {"settlement_assets": settlement_assets}
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_ids": [CHANNEL_ID, OPERATOR_ID],
                },
            },
            "trading": trading,
        }
    )
    settings.set_config_dir(tmp_path)
    return settings


def _ethereum_asset() -> dict[str, object]:
    return {
        "chain_id": 1,
        "chain_name": "Ethereum",
        "symbol": "USDC",
        "contract_address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "decimals": 6,
        "quote_amount": "10",
        "rpc_url": "https://ethereum.example.invalid",
    }


def test_onchain_route_analysis_can_be_enabled_without_futures_manual_trading(tmp_path: Path) -> None:
    _secure(tmp_path / "telegram_bot_token", BOT_TOKEN)
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/okx_api_key", "okx-key")
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/okx_api_secret", "okx-secret")
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/okx_passphrase", "okx-passphrase")
    settings = _settings(tmp_path, onchain=_onchain_profile())

    availability = onchain_trading_availability(settings)
    profile = settings.trading.telegram_profile(OPERATOR_ID)

    assert profile is not None
    assert profile.manual.enabled is False
    assert availability.interaction_available is True
    assert availability.configured_quote_providers == ("okx",)
    assert availability.binance_reason == "binance_general_web3_swap_api_unpublished"


def test_okx_and_oneinch_execution_share_one_manual_wallet_authority(tmp_path: Path) -> None:
    _secure(tmp_path / "telegram_bot_token", BOT_TOKEN)
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/okx_api_key", "okx-key")
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/okx_api_secret", "okx-secret")
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/okx_passphrase", "okx-passphrase")
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/oneinch_api_key", "oneinch-key")
    _secure(tmp_path / f"trading_profiles/onchain/{USER}/evm_private_key", "0x" + "0" * 63 + "1")
    settings = _settings(
        tmp_path,
        onchain=_onchain_profile(
            wallet={
                "address": "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf",
                "private_key_file": f"trading_profiles/onchain/{USER}/evm_private_key",
                "live_trading_acknowledged": True,
            },
            providers={
                "okx": {
                    "enabled": True,
                    "api_key_file": f"trading_profiles/quotes/{USER}/okx_api_key",
                    "api_secret_file": f"trading_profiles/quotes/{USER}/okx_api_secret",
                    "passphrase_file": f"trading_profiles/quotes/{USER}/okx_passphrase",
                },
                "oneinch": {
                    "enabled": True,
                    "api_key_file": f"trading_profiles/quotes/{USER}/oneinch_api_key",
                },
            },
        ),
        settlement_assets=[_ethereum_asset()],
    )

    availability = onchain_trading_availability(settings)
    profile = settings.trading.telegram_profile(OPERATOR_ID)

    assert profile is not None
    assert availability.configured_quote_providers == ("okx", "oneinch")
    assert availability.executable_providers == ("okx", "oneinch")
    assert availability.execution_available is True
    assert availability.execution_reason is None
    assert settings.trading_onchain_wallet_private_key_file(profile) == (
        tmp_path / f"trading_profiles/onchain/{USER}/evm_private_key"
    )


def test_okx_only_configuration_is_executable_with_the_same_wallet(tmp_path: Path) -> None:
    _secure(tmp_path / "telegram_bot_token", BOT_TOKEN)
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/okx_api_key", "okx-key")
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/okx_api_secret", "okx-secret")
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/okx_passphrase", "okx-passphrase")
    _secure(tmp_path / f"trading_profiles/onchain/{USER}/evm_private_key", "0x" + "0" * 63 + "1")
    settings = _settings(
        tmp_path,
        onchain=_onchain_profile(
            wallet={
                "address": "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf",
                "private_key_file": f"trading_profiles/onchain/{USER}/evm_private_key",
                "live_trading_acknowledged": True,
            }
        ),
        settlement_assets=[_ethereum_asset()],
    )

    availability = onchain_trading_availability(settings)

    assert availability.configured_quote_providers == ("okx",)
    assert availability.executable_providers == ("okx",)
    assert availability.execution_available is True
    assert availability.execution_reason is None


def test_custom_settlement_asset_remains_analysis_only(tmp_path: Path) -> None:
    _secure(tmp_path / "telegram_bot_token", BOT_TOKEN)
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/okx_api_key", "okx-key")
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/okx_api_secret", "okx-secret")
    _secure(tmp_path / f"trading_profiles/quotes/{USER}/okx_passphrase", "okx-passphrase")
    _secure(tmp_path / f"trading_profiles/onchain/{USER}/evm_private_key", "0x" + "0" * 63 + "1")
    custom_asset = _ethereum_asset() | {
        "contract_address": "0x2222222222222222222222222222222222222222",
        "symbol": "USDX",
    }
    settings = _settings(
        tmp_path,
        onchain=_onchain_profile(
            wallet={
                "address": "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf",
                "private_key_file": f"trading_profiles/onchain/{USER}/evm_private_key",
                "live_trading_acknowledged": True,
            }
        ),
        settlement_assets=[custom_asset],
    )

    availability = onchain_trading_availability(settings)

    assert availability.interaction_available is True
    assert availability.execution_available is False
    assert availability.execution_reason == "onchain_execution_settlement_unsupported"
    assert availability.rpc_chain_ids == ()


def test_onchain_discovery_remains_interactive_without_quote_provider_credentials(tmp_path: Path) -> None:
    _secure(tmp_path / "telegram_bot_token", BOT_TOKEN)
    settings = _settings(
        tmp_path,
        onchain=_onchain_profile(providers={"okx": {"enabled": False}, "oneinch": {"enabled": False}}),
    )

    availability = onchain_trading_availability(settings)

    assert availability.interaction_available is True
    assert availability.reason is None
    assert availability.configured_quote_providers == ()
    assert availability.execution_available is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("api_key_file", "custom-okx-key"),
        ("api_secret_file", "/var/empty/custom-okx-secret"),
        ("passphrase_file", "custom-okx-passphrase"),
    ),
)
def test_onchain_profile_uses_user_scoped_secret_names(field: str, value: str) -> None:
    provider = _onchain_profile()["providers"]
    assert isinstance(provider, dict)
    okx = provider["okx"]
    assert isinstance(okx, dict)
    okx[field] = value

    with pytest.raises(ValidationError, match="onchain_trading_profile_secret_name_invalid"):
        Settings.model_validate(
            {
                "news": {
                    "enabled": True,
                    "push": {
                        "enabled": True,
                        "telegram_bot_token_file": "telegram_bot_token",
                        "telegram_chat_ids": [CHANNEL_ID, OPERATOR_ID],
                    },
                },
                "trading": {
                    "telegram_profiles": [{"user_id": OPERATOR_ID, "onchain": _onchain_profile(providers=provider)}]
                },
            }
        )


def test_onchain_settlement_assets_are_unique_chain_contract_identities() -> None:
    duplicate = _ethereum_asset()
    with pytest.raises(ValidationError, match="onchain_settlement_chain_duplicate"):
        Settings.model_validate({"trading": {"onchain": {"settlement_assets": [duplicate, duplicate]}}})
