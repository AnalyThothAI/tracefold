"""Closed Binance USD-M configuration for the OI Runtime."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from nautilus_trader.adapters.binance import (
    BINANCE,
    BinanceAccountType,
    BinanceDataClientConfig,
    BinanceExecClientConfig,
    BinanceInstrumentProviderConfig,
)
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.config import (
    CacheConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.identifiers import AccountId, ClientId, InstrumentId, TraderId
from nautilus_trader.model.instruments import CryptoPerpetual

from tracefold.trading import IDENTITY_PATTERN, MARKET_KEY_PATTERN, market_key

_IDENTITY = re.compile(IDENTITY_PATTERN)
_MARKET_KEY = re.compile(MARKET_KEY_PATTERN)
# Nautilus reconciles the venue at start over this many minutes of order and fill history, and never
# less than a day: a position opened just inside the longest holding time must still find its entry.
_MIN_RECONCILIATION_LOOKBACK_MINS = 1_440
_RECONCILIATION_LOOKBACK_MARGIN_MINS = 60
# Nautilus' own continuous checks. Five seconds is the invariant cadence the Strategy converges on too.
CONTINUOUS_CHECK_SECONDS = 5.0
# Nautilus' own WARN and ERROR lines, kept on disk beside `nautilus.log` so a reconciliation decision
# outlives the container that made it (#680 PR-3): at most 1 + 5 files of 10 MiB each.
NAUTILUS_LOG_FILE_NAME = "nautilus-engine"
NAUTILUS_LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
NAUTILUS_LOG_FILE_BACKUPS = 5

# What a Runtime that is actually going to trade can be. `disabled` is not one of them: `run_nautilus`
# returns on it before any profile exists (#589 PR-2).
ActiveRuntimeMode = Literal["paper", "live"]


class _SecretValue(str):
    def __repr__(self) -> str:
        return "<redacted>"


@dataclass(frozen=True, slots=True)
class OiInstrumentRoute:
    """One explicit Alpha market to Binance instrument mapping."""

    market_key: str
    instrument_id: InstrumentId
    stop_distance_bps: int

    def __post_init__(self) -> None:
        if _MARKET_KEY.fullmatch(self.market_key) is None:
            raise ValueError("oi_runtime_market_key_invalid")
        if self.instrument_id.venue.value != BINANCE:
            raise ValueError("oi_runtime_instrument_venue_invalid")
        if not 1 <= self.stop_distance_bps <= 5_000:
            raise ValueError("oi_runtime_stop_distance_invalid")


def route_catalogue(instruments: Iterable[Any], *, stop_distance_bps: int) -> tuple[OiInstrumentRoute, ...]:
    """The markets this Runtime may enter: Binance's own USDT-settled crypto perpetuals, trading now.

    Binance lists stock and commodity perpetuals under the same `CryptoPerpetual` shape with
    `contractType = TRADIFI_PERPETUAL`; they need a separate agreement on the account and the Demo venue
    refused one with `-4411` (#680 RC8). Only `contractType = PERPETUAL` in `TRADING` status is a route.
    A symbol whose base asset cannot be a market key (a non-ASCII meme listing) is skipped, and two
    instruments claiming one market key is a catalogue this Runtime cannot route unambiguously.
    """

    routes: dict[str, OiInstrumentRoute] = {}
    for instrument in instruments:
        if not isinstance(instrument, CryptoPerpetual):
            continue
        if instrument.quote_currency != USDT or instrument.settlement_currency != USDT:
            continue
        info = instrument.info or {}
        if str(info.get("status")) != "TRADING" or str(info.get("contractType")) != "PERPETUAL":
            continue
        key = market_key(instrument.base_currency.code)
        if _MARKET_KEY.fullmatch(key) is None:
            continue
        if key in routes:
            raise RuntimeError("oi_runtime_market_route_ambiguous")
        routes[key] = OiInstrumentRoute(
            market_key=key, instrument_id=instrument.id, stop_distance_bps=stop_distance_bps
        )
    return tuple(routes[key] for key in sorted(routes))


@dataclass(frozen=True, slots=True)
class OiRiskLimits:
    """The operator's entry and sizing policy, as the Runtime enforces it."""

    risk_fraction_per_trade: Decimal
    max_risk_per_trade_usd: Decimal
    max_positions: int
    max_leverage: int
    max_daily_loss_usd: Decimal
    max_spread_fraction_of_stop: Decimal
    post_stop_cooldown_ns: int
    market_stale_after_ns: int

    def __post_init__(self) -> None:
        if not Decimal("0") < self.risk_fraction_per_trade <= Decimal("1"):
            raise ValueError("oi_runtime_risk_fraction_invalid")
        if self.max_risk_per_trade_usd <= 0:
            raise ValueError("oi_runtime_risk_limit_invalid")
        if not 1 <= self.max_positions <= 100:
            raise ValueError("oi_runtime_max_positions_invalid")
        if not 1 <= self.max_leverage <= 125:
            raise ValueError("oi_runtime_max_leverage_invalid")
        if self.max_daily_loss_usd <= 0:
            raise ValueError("oi_runtime_daily_loss_invalid")
        if not Decimal("0") < self.max_spread_fraction_of_stop <= Decimal("1"):
            raise ValueError("oi_runtime_max_spread_invalid")
        if self.post_stop_cooldown_ns < 0 or self.market_stale_after_ns <= 0:
            raise ValueError("oi_runtime_clock_limit_invalid")


@dataclass(frozen=True, slots=True)
class OiExitPolicy:
    take_profit_bps: int
    max_holding_ns: int
    policy_id: Literal["oi_fixed_v1"] = "oi_fixed_v1"

    def __post_init__(self) -> None:
        if not 1 <= self.take_profit_bps <= 50_000 or self.max_holding_ns <= 0:
            raise ValueError("oi_runtime_exit_policy_invalid")


@dataclass(frozen=True, slots=True)
class OiRuntimeProfile:
    """The account slot this Runtime executes for, and the policy it executes under.

    `account_slot` plus `mode` is the whole execution identity (#520). `namespace` is that identity
    spelled once: the Nautilus trader id and every deterministic client order id derive from it.
    """

    mode: ActiveRuntimeMode
    account_slot: str
    account_id: AccountId
    namespace: str
    routes: tuple[OiInstrumentRoute, ...]
    risk: OiRiskLimits
    exit_policy: OiExitPolicy

    def __post_init__(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError("oi_runtime_mode_invalid")
        for value, reason in (
            (self.account_slot, "oi_runtime_account_slot_invalid"),
            (self.namespace, "oi_runtime_namespace_invalid"),
        ):
            if _IDENTITY.fullmatch(value) is None:
                raise ValueError(reason)
        market_keys = tuple(route.market_key for route in self.routes)
        instrument_ids = tuple(route.instrument_id for route in self.routes)
        if len(market_keys) != len(set(market_keys)) or len(instrument_ids) != len(set(instrument_ids)):
            raise ValueError("oi_runtime_route_identity_duplicate")
        if not self.routes:
            raise ValueError("oi_runtime_routes_missing")

    @property
    def reconciliation_lookback_mins(self) -> int:
        holding_mins = math.ceil(self.exit_policy.max_holding_ns / 60_000_000_000)
        return max(_MIN_RECONCILIATION_LOOKBACK_MINS, holding_mins + _RECONCILIATION_LOOKBACK_MARGIN_MINS)


@dataclass(frozen=True, slots=True)
class BinanceRuntimeCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ValueError("oi_runtime_credentials_invalid")


def _trader_id(profile: OiRuntimeProfile) -> TraderId:
    digest = hashlib.sha256(profile.namespace.encode()).hexdigest()[:12].upper()
    return TraderId(f"OI-{digest}")


def _instance_id(profile: OiRuntimeProfile) -> UUID4:
    """Stable per account slot and mode, which is what the namespace already carries (#537 PR-4)."""

    digest = hashlib.sha256(f"tracefold:oi-runtime:{profile.account_slot}:{profile.mode}".encode()).digest()
    value = uuid.UUID(bytes=digest[:16], version=4)
    return UUID4.from_str(str(value))


def binance_environment(mode: ActiveRuntimeMode) -> BinanceEnvironment:
    """The one place `paper` becomes Binance's demo environment and `live` becomes production."""

    return BinanceEnvironment.DEMO if mode == "paper" else BinanceEnvironment.LIVE


def build_oi_node_config(
    profile: OiRuntimeProfile,
    credentials: BinanceRuntimeCredentials,
    *,
    log_directory: Path | None = None,
) -> TradingNodeConfig:
    """The pinned paper/live graph, in which Nautilus owns every order and position (#680).

    Startup reconciliation rebuilds the Cache from the venue before the Strategy starts; the open-order
    and position checks keep it converged every five seconds after that, over open orders only so the
    REST weight stays inside the live budget. There is no Cache database: the venue is the store, and a
    restart is the same reconciliation a start is.

    Reconciliation applies the venue's own orders and fills and never invents one to make the Cache
    match a position report (`generate_missing_orders=False`, #680 PR-3). On 1.231.0 the Binance
    adapter answers a positionRisk error (`-1021`) with no position reports at all, and the 5 s position
    check read that as "flat" and closed a position the venue still held with a synthetic fill (Path B).
    What the flag gives up is adopting, at startup, a venue position with no fill inside the lookback;
    the Strategy's venue-truth invariant names that position instead, blocks entries on it, and
    `/flatten account` can close it.

    With `log_directory`, Nautilus' WARN and ERROR lines also go to a size-rotated file there.
    """

    environment = binance_environment(profile.mode)
    instrument_ids = frozenset(route.instrument_id for route in profile.routes)
    provider = BinanceInstrumentProviderConfig(
        load_ids=instrument_ids,
        query_commission_rates=False,
    )
    data = BinanceDataClientConfig(
        api_key=_SecretValue(credentials.api_key),
        api_secret=_SecretValue(credentials.api_secret),
        account_type=BinanceAccountType.USDT_FUTURES,
        environment=environment,
        instrument_provider=provider,
    )
    execution = BinanceExecClientConfig(
        api_key=_SecretValue(credentials.api_key),
        api_secret=_SecretValue(credentials.api_secret),
        account_type=BinanceAccountType.USDT_FUTURES,
        environment=environment,
        instrument_provider=provider,
        use_reduce_only=True,
        # Sizing already caps gross notional at the configured leverage. Avoid an
        # account-wide burst of per-symbol leverage mutations during catalogue load.
        futures_leverages=None,
        # A transport retry cannot decide whether an economic order exists; Nautilus' in-flight
        # check queries the order instead.
        max_retries=None,
    )
    client_id = ClientId(BINANCE)
    return TradingNodeConfig(
        trader_id=_trader_id(profile),
        instance_id=_instance_id(profile),
        logging=LoggingConfig(
            log_level="WARNING",
            log_colors=False,
            use_pyo3=True,
            **(
                {}
                if log_directory is None
                else {
                    "log_level_file": "WARNING",
                    "log_directory": str(log_directory),
                    "log_file_name": NAUTILUS_LOG_FILE_NAME,
                    "log_file_max_size": NAUTILUS_LOG_FILE_MAX_BYTES,
                    "log_file_max_backup_count": NAUTILUS_LOG_FILE_BACKUPS,
                }
            ),
        ),
        cache=CacheConfig(
            database=None,
            flush_on_start=False,
            use_trader_prefix=True,
            use_instance_id=True,
        ),
        data_engine=LiveDataEngineConfig(external_clients=[client_id]),
        risk_engine=LiveRiskEngineConfig(bypass=False),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
            reconciliation_lookback_mins=profile.reconciliation_lookback_mins,
            reconciliation_instrument_ids=None,
            filter_unclaimed_external_orders=False,
            filter_position_reports=False,
            generate_missing_orders=False,
            inflight_check_interval_ms=2_000,
            inflight_check_threshold_ms=5_000,
            inflight_check_retries=5,
            open_check_interval_secs=CONTINUOUS_CHECK_SECONDS,
            open_check_open_only=True,
            position_check_interval_secs=CONTINUOUS_CHECK_SECONDS,
            graceful_shutdown_on_exception=True,
        ),
        data_clients={BINANCE: data},
        exec_clients={BINANCE: execution},
        timeout_connection=30.0,
        timeout_reconciliation=60.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=10.0,
    )


__all__ = [
    "CONTINUOUS_CHECK_SECONDS",
    "NAUTILUS_LOG_FILE_BACKUPS",
    "NAUTILUS_LOG_FILE_MAX_BYTES",
    "NAUTILUS_LOG_FILE_NAME",
    "ActiveRuntimeMode",
    "BinanceRuntimeCredentials",
    "OiExitPolicy",
    "OiInstrumentRoute",
    "OiRiskLimits",
    "OiRuntimeProfile",
    "binance_environment",
    "build_oi_node_config",
    "route_catalogue",
]
