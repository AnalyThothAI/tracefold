from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tracefold.app.workers.wiring.manual_trading import (
    TelegramTradingProfileControllers,
    TelegramTradingUpdateRouter,
)
from tracefold.app.workers.wiring.onchain_trading import (
    OnchainRouteGateway,
    onchain_sources_from_news_projection,
    wire_onchain_controller,
)
from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.news import TelegramManualTradeProjectionV1
from tracefold.platform.config.models import Settings
from tracefold.trading import (
    OnchainProviderToken,
    OnchainProviderUnavailable,
    OnchainRouteQuote,
)

NOW = 1_900_000_000_000
TARGET = "a" * 64
CHANNEL_ID = -1001234567890
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
HYPE = "0x1111111111111111111111111111111111111111"


def _secure(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def _projection() -> TelegramManualTradeProjectionV1:
    return TelegramManualTradeProjectionV1(
        projection_version="telegram_manual_trade_projection_v1",
        event_id="event-42",
        opened_at_ms=NOW,
        final_decision="push",
        degraded=False,
        direction="bearish",
        title_zh="正文出现 BTC 和 SOL，但 TG 标的事实只有 HYPE 与 ETH",
        displayed_assets=("HYPE", "ETH"),
    )


def test_onchain_news_source_uses_only_sent_card_targets_even_for_bearish_news() -> None:
    sources = onchain_sources_from_news_projection(_projection(), message_id=42, target_sha256=TARGET)

    assert tuple(source.ticker for source in sources) == ("HYPE", "ETH")
    assert all(source.delivery_message_id == 42 for source in sources)


class _Okx:
    async def close(self) -> None:
        return None

    async def search_tokens(self, ticker: str, *, chain_ids: tuple[int, ...]) -> tuple[OnchainProviderToken, ...]:
        assert ticker == "HYPE" and chain_ids == (1,)
        return (
            OnchainProviderToken(
                provider="okx",
                chain_id=1,
                chain_name="Ethereum",
                contract_address=HYPE,
                symbol="HYPE",
                name="Hyperliquid",
                decimals=18,
                verified=False,
            ),
        )

    async def quote(self, request: object) -> OnchainRouteQuote:
        return OnchainRouteQuote(
            provider="okx",
            chain_id=1,
            input_contract=USDC,
            output_contract=HYPE,
            input_amount_raw=10_000_000,
            expected_output_raw=1_000_000_000_000_000_000,
            minimum_output_raw=990_000_000_000_000_000,
            slippage_bps=100,
            latency_ms=100,
            received_at_ms=NOW,
            expires_at_ms=NOW + 10_000,
        )


class _Binance:
    async def close(self) -> None:
        return None

    async def search_tokens(self, _ticker: str, *, chain_ids: tuple[int, ...]) -> tuple[OnchainProviderToken, ...]:
        del chain_ids
        raise OnchainProviderUnavailable("binance_general_web3_swap_api_unpublished")

    async def quote(self, _request: object) -> OnchainRouteQuote:
        raise OnchainProviderUnavailable("binance_general_web3_swap_api_unpublished")


def test_gateway_keeps_binance_unavailable_visible_while_okx_route_remains_usable() -> None:
    gateway = OnchainRouteGateway(
        providers={"okx": _Okx(), "binance": _Binance()},
        settlement_assets={
            1: SimpleNamespace(
                chain_id=1,
                symbol="USDC",
                contract_address=USDC,
                decimals=6,
                quote_amount_raw=10_000_000,
            )
        },
        slippage_bps=100,
        clock_ms=lambda: NOW,
    )

    resolution = asyncio.run(gateway.resolve("HYPE"))
    result = asyncio.run(gateway.quote(resolution.candidates[0]))
    asyncio.run(gateway.close())

    assert len(resolution.candidates) == 1
    assert resolution.provider_errors == ("binance_general_web3_swap_api_unpublished",)
    assert result.analysis.winner_provider == "okx"
    assert result.provider_errors == ("binance_general_web3_swap_api_unpublished",)


def test_wiring_keeps_oneinch_usable_when_enabled_okx_credentials_are_empty(tmp_path: Path) -> None:
    user_id = 222222
    key_file = f"trading_profiles/quotes/{user_id}/oneinch_api_key"
    _secure(tmp_path / key_file, "oneinch-key")
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_ids": [CHANNEL_ID, user_id],
                },
            },
            "trading": {
                "telegram_profiles": [
                    {
                        "user_id": user_id,
                        "onchain": {
                            "enabled": True,
                            "providers": {
                                "okx": {"enabled": True},
                                "oneinch": {"enabled": True, "api_key_file": key_file},
                                "binance": {"enabled": True},
                            },
                        },
                    }
                ]
            },
        }
    )
    settings.set_config_dir(tmp_path)
    profile = settings.trading.telegram_profile(user_id)
    assert profile is not None

    controller = wire_onchain_controller(
        settings=settings,
        profile=profile,
        database=object(),  # type: ignore[arg-type]
        bot=object(),  # type: ignore[arg-type]
        target_sha256=TARGET,
    )

    assert controller is not None
    asyncio.run(controller.close())


def test_wiring_never_exposes_trade_button_for_custom_settlement_asset(tmp_path: Path) -> None:
    user_id = 333333
    token_file = "telegram_bot_token"
    key_file = f"trading_profiles/quotes/{user_id}/okx_api_key"
    secret_file = f"trading_profiles/quotes/{user_id}/okx_api_secret"
    passphrase_file = f"trading_profiles/quotes/{user_id}/okx_passphrase"
    wallet_file = f"trading_profiles/onchain/{user_id}/evm_private_key"
    _secure(tmp_path / token_file, "123456:" + "a" * 32)
    _secure(tmp_path / key_file, "okx-key")
    _secure(tmp_path / secret_file, "okx-secret")
    _secure(tmp_path / passphrase_file, "okx-passphrase")
    _secure(tmp_path / wallet_file, "0x" + "0" * 63 + "1")
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": token_file,
                    "telegram_chat_ids": [CHANNEL_ID, user_id],
                },
            },
            "trading": {
                "onchain": {
                    "settlement_assets": [
                        {
                            "chain_id": 1,
                            "chain_name": "Ethereum",
                            "symbol": "USDX",
                            "contract_address": "0x2222222222222222222222222222222222222222",
                            "decimals": 6,
                            "quote_amount": 10,
                            "rpc_url": "https://ethereum.example.invalid",
                        }
                    ]
                },
                "telegram_profiles": [
                    {
                        "user_id": user_id,
                        "onchain": {
                            "enabled": True,
                            "wallet": {
                                "address": "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf",
                                "private_key_file": wallet_file,
                                "live_trading_acknowledged": True,
                            },
                            "providers": {
                                "okx": {
                                    "enabled": True,
                                    "api_key_file": key_file,
                                    "api_secret_file": secret_file,
                                    "passphrase_file": passphrase_file,
                                }
                            },
                        },
                    }
                ],
            },
        }
    )
    settings.set_config_dir(tmp_path)
    profile = settings.trading.telegram_profile(user_id)
    assert profile is not None

    controller = wire_onchain_controller(
        settings=settings,
        profile=profile,
        database=object(),  # type: ignore[arg-type]
        bot=object(),  # type: ignore[arg-type]
        target_sha256=TARGET,
    )

    assert controller is not None
    assert controller._execution_available is False
    assert controller._execution_assets == {}
    asyncio.run(controller.close())


class _Controller:
    def __init__(self) -> None:
        self.updates: list[TelegramTradingUpdate] = []

    async def close(self) -> None:
        return None

    async def handle(self, update: TelegramTradingUpdate) -> str:
        self.updates.append(update)
        return "handled"


class _Bot:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def answer(self, *_args: object, **_values: object) -> None:
        return None

    async def reply(
        self,
        *,
        source_message_id: int,
        text: str,
        keyboard: tuple[tuple[str, str], ...],
    ) -> int:
        assert source_message_id > 0
        assert keyboard
        self.replies.append(text)
        return 99

    async def reply_plain(self, *, source_message_id: int, text: str) -> int:
        assert source_message_id > 0
        self.replies.append(text)
        return 99


def test_shared_telegram_cursor_routes_each_actor_only_to_its_profile_controllers() -> None:
    manual = _Controller()
    onchain = _Controller()
    router = TelegramTradingUpdateRouter(
        profiles={
            111: TelegramTradingProfileControllers(
                bot=_Bot(),  # type: ignore[arg-type]
                manual=manual,  # type: ignore[arg-type]
                onchain=None,
            ),
            222: TelegramTradingProfileControllers(
                bot=_Bot(),  # type: ignore[arg-type]
                manual=None,
                onchain=onchain,
            ),
        },
        test_news=object(),  # type: ignore[arg-type]
    )
    base = TelegramTradingUpdate(
        update_id=1,
        callback_query_id="callback",
        actor_user_id=222,
        chat_id=222,
        chat_type="private",
        message_id=42,
        data="tf:onchain:v1",
        authorized=True,
    )

    assert asyncio.run(router.handle(base)) == "handled"
    assert onchain.updates[-1].authorized is True

    futures = TelegramTradingUpdate(
        update_id=2,
        callback_query_id="callback-futures",
        actor_user_id=222,
        chat_id=222,
        chat_type="private",
        message_id=42,
        data="tf:trade:v1",
        authorized=True,
    )
    assert asyncio.run(router.handle(futures)) == "manual_unavailable"
    assert manual.updates == []


def test_private_start_help_lists_only_the_profile_enabled_test_commands() -> None:
    futures_bot = _Bot()
    onchain_bot = _Bot()
    router = TelegramTradingUpdateRouter(
        profiles={
            111: TelegramTradingProfileControllers(
                bot=futures_bot,  # type: ignore[arg-type]
                manual=_Controller(),  # type: ignore[arg-type]
                onchain=None,
            ),
            222: TelegramTradingProfileControllers(
                bot=onchain_bot,  # type: ignore[arg-type]
                manual=None,
                onchain=_Controller(),  # type: ignore[arg-type]
            ),
        },
        test_news=object(),  # type: ignore[arg-type]
    )

    futures_help = TelegramTradingUpdate(
        update_id=3,
        callback_query_id="message:111:43",
        actor_user_id=111,
        chat_id=111,
        chat_type="private",
        message_id=43,
        data="tf:help:v1",
        authorized=True,
        update_kind="message",
    )
    onchain_help = TelegramTradingUpdate(
        update_id=4,
        callback_query_id="message:222:44",
        actor_user_id=222,
        chat_id=222,
        chat_type="private",
        message_id=44,
        data="tf:help:v1",
        authorized=True,
        update_kind="message",
    )

    assert asyncio.run(router.handle(futures_help)) == "help_sent"
    assert "/test_futures" in futures_bot.replies[-1]
    assert "/test_onchain" not in futures_bot.replies[-1]
    assert asyncio.run(router.handle(onchain_help)) == "help_sent"
    assert "/test_onchain" in onchain_bot.replies[-1]
    assert "/test_futures" not in onchain_bot.replies[-1]
