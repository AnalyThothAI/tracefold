"""The one supported Nautilus/Binance runtime shape for #283."""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from importlib.metadata import distribution
from typing import Final

from nautilus_trader.adapters.binance import (
    BINANCE,
    BinanceAccountType,
    BinanceDataClientConfig,
    BinanceExecClientConfig,
    BinanceInstrumentProviderConfig,
)
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.common.symbol import BinanceSymbol
from nautilus_trader.config import (
    CacheConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.model.identifiers import ClientId, InstrumentId, TraderId


@dataclass(frozen=True, slots=True)
class NautilusRelease:
    version: str
    git_tag: str
    git_commit: str


NAUTILUS_RELEASE = NautilusRelease(
    version="1.231.0",
    git_tag="v1.231.0",
    git_commit="27a8e54e7ac3c57d6cbf8891f0283dfbaee97317",
)

NAUTILUS_LINUX_WHEELS: Final = {
    "x86_64": (
        "cp313-cp313-manylinux_2_35_x86_64",
        "429ea61c33a32cd8498d39e0ea95ebaa12b8dbfc25c71fbaba845f2b05e8ab91",
    ),
    "aarch64": (
        "cp313-cp313-manylinux_2_35_aarch64",
        "e536d7c925b3c475bef4f3f8e75196944f6b8758710e41da1109b8b837001690",
    ),
}


def linux_release_wheel_identity(machine: str) -> str:
    try:
        tag, sha256 = NAUTILUS_LINUX_WHEELS[machine]
    except KeyError:
        raise ValueError("nautilus_linux_wheel_architecture_unsupported") from None
    return f"{tag}@sha256:{sha256}"


def installed_nautilus_wheel_identity() -> str:
    """Return release evidence in Linux images and an explicit dev identity elsewhere."""

    machine = platform.machine()
    if sys.platform != "linux" or sys.version_info[:2] != (3, 13):
        return f"development@{sys.platform}-{machine}-py{sys.version_info.major}{sys.version_info.minor}"
    identity = linux_release_wheel_identity(machine)
    tag = identity.split("@", 1)[0]
    wheel_metadata = distribution("nautilus-trader").read_text("WHEEL") or ""
    if f"Tag: {tag}" not in wheel_metadata:
        raise RuntimeError("nautilus_installed_wheel_tag_mismatch")
    return identity


def build_node_config(
    *,
    api_key: str,
    api_secret: str,
    instrument_id: InstrumentId,
) -> TradingNodeConfig:
    """Build the exact public-v1, one-instrument Demo node configuration."""

    provider = BinanceInstrumentProviderConfig(
        load_ids=frozenset({instrument_id}),
        query_commission_rates=True,
    )
    return TradingNodeConfig(
        trader_id=TraderId("TRACEFOLD-001"),
        logging=LoggingConfig(log_level="WARNING", log_colors=False, use_pyo3=True),
        cache=CacheConfig(database=None, flush_on_start=False),
        data_engine=LiveDataEngineConfig(external_clients=[ClientId(BINANCE)]),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
            inflight_check_interval_ms=0,
            open_check_interval_secs=5.0,
            open_check_open_only=False,
            position_check_interval_secs=30.0,
        ),
        data_clients={
            BINANCE: BinanceDataClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                account_type=BinanceAccountType.USDT_FUTURES,
                environment=BinanceEnvironment.DEMO,
                instrument_provider=provider,
            )
        },
        exec_clients={
            BINANCE: BinanceExecClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                account_type=BinanceAccountType.USDT_FUTURES,
                environment=BinanceEnvironment.DEMO,
                instrument_provider=provider,
                use_reduce_only=True,
                futures_leverages={BinanceSymbol("SOLUSDT"): 1},
                max_retries=None,
            )
        },
        timeout_connection=30.0,
        timeout_reconciliation=30.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=10.0,
    )


__all__ = [
    "NAUTILUS_LINUX_WHEELS",
    "NAUTILUS_RELEASE",
    "NautilusRelease",
    "build_node_config",
    "installed_nautilus_wheel_identity",
    "linux_release_wheel_identity",
]
