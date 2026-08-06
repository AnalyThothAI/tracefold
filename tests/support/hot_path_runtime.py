from __future__ import annotations

from pathlib import Path

from tests.postgres_test_utils import postgres_settings_storage
from tracefold.platform.config.settings import Settings

WS_TOKEN = "hot-path-token"
FIXED_NOW_MS = 1_777_729_877_581
EVENT_ID = "gmgn:twitter_monitor_token:fixture-internal-001"
AUTHOR_HANDLE = "fixture_signal"
SYMBOL = "MIRROR"
CHAIN_ID = "eip155:56"
ADDRESS = "0x8f32420f2e3728c49399b00dd0a796602d984444"
MARKET_TARGET_TYPE = "chain_token"
MARKET_TARGET_ID = f"{CHAIN_ID}:{ADDRESS}"


def backend_hot_path_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        ws_token=WS_TOKEN,
        storage=postgres_settings_storage(),
        providers={
            "okx": {
                "dex_base_url": "",
            },
            "binance": {"enabled": False},
            "macro_sources": {"enabled": False},
        },
    )
    settings.set_config_dir(tmp_path / "gmgn-hot-path-home")
    return settings


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {WS_TOKEN}"}
