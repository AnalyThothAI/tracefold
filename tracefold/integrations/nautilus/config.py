"""The one closed Nautilus mainnet runtime shape for both Production V3 bindings."""

from __future__ import annotations

import platform
import sys
from collections.abc import Mapping, Sequence
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
from nautilus_trader.adapters.hyperliquid import (
    HYPERLIQUID,
    HyperliquidDataClientConfig,
    HyperliquidExecClientConfig,
    HyperliquidProductType,
)
from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.config import (
    CacheConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.model.identifiers import ClientId, InstrumentId, TraderId

from tracefold.trading import VenueBinding


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


@dataclass(frozen=True, slots=True)
class BinanceCredentials:
    api_key: str
    api_secret: str


@dataclass(frozen=True, slots=True)
class HyperliquidCredentials:
    private_key: str
    account_address: str


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
    instrument_ids_by_binding: Mapping[VenueBinding, Sequence[InstrumentId]],
    binance_credentials: BinanceCredentials | None,
    hyperliquid_credentials: HyperliquidCredentials | None,
) -> TradingNodeConfig:
    """Build zero, one, or two clients inside the single lifecycle process."""

    unknown = set(instrument_ids_by_binding).difference({"BINANCE_USDM", "HYPERLIQUID_PERP"})
    if unknown:
        raise ValueError("nautilus_binding_set_invalid")
    binance_ids = frozenset(instrument_ids_by_binding.get("BINANCE_USDM", ()))
    hyperliquid_ids = frozenset(instrument_ids_by_binding.get("HYPERLIQUID_PERP", ()))
    data_clients: dict[str, object] = {}
    exec_clients: dict[str, object] = {}
    external_clients: list[ClientId] = []
    if binance_credentials is not None:
        provider = BinanceInstrumentProviderConfig(
            load_ids=binance_ids,
            # The adapter derives the account fee tier once. Per-symbol reads can exhaust startup I/O.
            query_commission_rates=False,
        )
        data_clients[BINANCE] = BinanceDataClientConfig(
            api_key=binance_credentials.api_key,
            api_secret=binance_credentials.api_secret,
            account_type=BinanceAccountType.USDT_FUTURES,
            environment=BinanceEnvironment.LIVE,
            instrument_provider=provider,
        )
        exec_clients[BINANCE] = BinanceExecClientConfig(
            api_key=binance_credentials.api_key,
            api_secret=binance_credentials.api_secret,
            account_type=BinanceAccountType.USDT_FUTURES,
            environment=BinanceEnvironment.LIVE,
            instrument_provider=provider,
            use_reduce_only=True,
            futures_leverages={BinanceSymbol(item.symbol.value.removesuffix("-PERP")): 1 for item in binance_ids},
            max_retries=None,
        )
        external_clients.append(ClientId(BINANCE))
    if hyperliquid_credentials is not None:
        provider = InstrumentProviderConfig(load_ids=hyperliquid_ids, log_warnings=True)
        products = (HyperliquidProductType.PERP, HyperliquidProductType.PERP_HIP3)
        data_clients[HYPERLIQUID] = HyperliquidDataClientConfig(
            instrument_provider=provider,
            product_types=products,
        )
        exec_clients[HYPERLIQUID] = HyperliquidExecClientConfig(
            instrument_provider=provider,
            private_key=hyperliquid_credentials.private_key,
            account_address=hyperliquid_credentials.account_address,
            product_types=products,
            # These transport retries belong to the pinned adapter contract. The coordinator still
            # query-first reconciles an ambiguous submit and never emits a second economic entry.
            max_retries=3,
            retry_delay_initial_ms=250,
            retry_delay_max_ms=2_000,
            normalize_prices=True,
        )
        external_clients.append(ClientId(HYPERLIQUID))
    return TradingNodeConfig(
        trader_id=TraderId("TRACEFOLD-001"),
        logging=LoggingConfig(log_level="WARNING", log_colors=False, use_pyo3=True),
        cache=CacheConfig(database=None, flush_on_start=False),
        data_engine=LiveDataEngineConfig(external_clients=external_clients),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
            inflight_check_interval_ms=0,
            open_check_interval_secs=5.0,
            open_check_open_only=False,
            position_check_interval_secs=30.0,
        ),
        data_clients=data_clients,
        exec_clients=exec_clients,
        timeout_connection=30.0,
        timeout_reconciliation=30.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=10.0,
    )


__all__ = [
    "NAUTILUS_LINUX_WHEELS",
    "NAUTILUS_RELEASE",
    "BinanceCredentials",
    "HyperliquidCredentials",
    "NautilusRelease",
    "build_node_config",
    "installed_nautilus_wheel_identity",
    "linux_release_wheel_identity",
]
