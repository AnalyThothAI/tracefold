"""Closed Binance USD-M configuration for the OI Runtime."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

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
from nautilus_trader.model.identifiers import AccountId, ClientId, InstrumentId, TraderId

from tracefold.trading import IDENTITY_PATTERN, MARKET_KEY_PATTERN

_IDENTITY = re.compile(IDENTITY_PATTERN)
_MARKET_KEY = re.compile(MARKET_KEY_PATTERN)

# What a Runtime that is actually going to trade can be. `disabled` is not one of them: `run_nautilus`
# returns on it before any profile exists, so a `RuntimeMode` alias that admitted it only made every
# path downstream re-prove that it was not looking at a Runtime that cannot trade (#589 PR-2).
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


# How much older than one private-reconciliation period the account and reconciliation clocks may be
# before an entry is refused. They are multiples of the period, not free numbers: the account clock is
# only ever as fresh as the last scan, so any budget at or below one period is expired for part of
# every cycle by construction (#510 B). Two periods tolerates one missed scan, three tolerates two,
# and both still refuse a Runtime that has genuinely stopped reconciling.
_ACCOUNT_STALE_PERIODS = 2
_RECONCILIATION_STALE_PERIODS = 3


@dataclass(frozen=True, slots=True)
class OiRiskLimits:
    """The smallest Runtime-owned gap policy beyond Nautilus RiskEngine."""

    risk_fraction_per_trade: Decimal
    max_risk_per_trade_usd: Decimal
    max_total_risk_usd: Decimal
    max_positions: int
    max_leverage: int
    max_daily_loss_usd: Decimal
    # Market freshness is owned by the quote stream, not by the private scan, so it stays its own
    # number. The two account clocks below are owned by the private scan and are derived from it.
    market_stale_after_ns: int
    reconciliation_interval_ns: int

    def __post_init__(self) -> None:
        if not Decimal("0") < self.risk_fraction_per_trade <= Decimal("1"):
            raise ValueError("oi_runtime_risk_fraction_invalid")
        if self.max_risk_per_trade_usd <= 0 or self.max_total_risk_usd <= 0:
            raise ValueError("oi_runtime_risk_limit_invalid")
        if self.max_risk_per_trade_usd > self.max_total_risk_usd:
            raise ValueError("oi_runtime_risk_limit_invalid")
        if not 1 <= self.max_positions <= 100:
            raise ValueError("oi_runtime_max_positions_invalid")
        if not 1 <= self.max_leverage <= 125:
            raise ValueError("oi_runtime_max_leverage_invalid")
        if self.max_daily_loss_usd <= 0:
            raise ValueError("oi_runtime_daily_loss_invalid")
        if min(self.market_stale_after_ns, self.reconciliation_interval_ns) <= 0:
            raise ValueError("oi_runtime_staleness_invalid")

    @property
    def account_stale_after_ns(self) -> int:
        return self.reconciliation_interval_ns * _ACCOUNT_STALE_PERIODS

    @property
    def reconciliation_stale_after_ns(self) -> int:
        return self.reconciliation_interval_ns * _RECONCILIATION_STALE_PERIODS

    @property
    def reconciliation_interval_seconds(self) -> float:
        return self.reconciliation_interval_ns / 1_000_000_000


@dataclass(frozen=True, slots=True)
class OiRuntimeProfile:
    """The account slot this Runtime executes for, and the policy it executes under.

    `account_slot` plus `mode` is the whole execution identity (#520), and now it is the only one:
    `runtime_release` and `config_sha256` rode along on the projection and on every observation
    naming what was running, and no reader ever named either (#537 PR-4).

    `namespace` is that identity spelled once. It was two fields, `cache_namespace` and
    `client_order_namespace`, and the composition root has only ever built both out of the same
    `tracefold:{account_slot}:{mode}` string: the Nautilus trader id and every deterministic client
    order id are derived from it, so two fields could only ever let a restart derive order ids under
    one namespace while claiming the Cache of another (#589 PR-2).
    """

    mode: ActiveRuntimeMode
    account_slot: str
    account_id: AccountId
    namespace: str
    routes: tuple[OiInstrumentRoute, ...]
    risk: OiRiskLimits

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
    """Stable per account slot and mode, which is what the namespace already carries.

    It was derived from the whole configuration digest instead, so every risk-limit edit gave the
    same Runtime a new Nautilus instance id and therefore a new Nautilus Cache namespace (#537 PR-4).
    """

    digest = hashlib.sha256(f"tracefold:oi-runtime:{profile.account_slot}:{profile.mode}".encode()).digest()
    value = uuid.UUID(bytes=digest[:16], version=4)
    return UUID4.from_str(str(value))


def binance_environment(mode: ActiveRuntimeMode) -> BinanceEnvironment:
    """The one place `paper` becomes Binance's demo environment and `live` becomes production.

    The catalogue discovery in the composition root carried a second copy of this ternary, so the
    two could have disagreed about which venue a Runtime was about to trade on (#537 PR-4).
    """

    return BinanceEnvironment.DEMO if mode == "paper" else BinanceEnvironment.LIVE


def build_oi_node_config(
    profile: OiRuntimeProfile,
    credentials: BinanceRuntimeCredentials,
) -> TradingNodeConfig:
    """Build the pinned paper/live graph."""

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
        # A transport retry cannot decide whether an economic order exists.
        max_retries=None,
    )
    client_id = ClientId(BINANCE)
    return TradingNodeConfig(
        trader_id=_trader_id(profile),
        instance_id=_instance_id(profile),
        logging=LoggingConfig(log_level="WARNING", log_colors=False, use_pyo3=True),
        cache=CacheConfig(
            database=None,
            flush_on_start=False,
            use_trader_prefix=True,
            use_instance_id=True,
        ),
        data_engine=LiveDataEngineConfig(external_clients=[client_id]),
        risk_engine=LiveRiskEngineConfig(bypass=False),
        exec_engine=LiveExecEngineConfig(
            # The App root owns the one complete startup proof, including Binance
            # Algo orders. Nautilus retains only its native continuous mechanics:
            # in-flight query-first and missing open-order/position event repair.
            reconciliation=False,
            reconciliation_instrument_ids=None,
            filter_unclaimed_external_orders=False,
            filter_position_reports=False,
            generate_missing_orders=True,
            inflight_check_interval_ms=2_000,
            inflight_check_threshold_ms=5_000,
            inflight_check_retries=0,
            open_check_interval_secs=5.0,
            open_check_open_only=False,
            position_check_interval_secs=5.0,
        ),
        data_clients={BINANCE: data},
        exec_clients={BINANCE: execution},
        timeout_connection=30.0,
        timeout_reconciliation=30.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=10.0,
    )


__all__ = [
    "ActiveRuntimeMode",
    "BinanceRuntimeCredentials",
    "OiInstrumentRoute",
    "OiRiskLimits",
    "OiRuntimeProfile",
    "binance_environment",
    "build_oi_node_config",
]
