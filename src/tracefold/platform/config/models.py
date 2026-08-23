from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from tracefold.platform.paths import app_home, app_log_path


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"  # noqa: S104 -- configurable API bind address; defaults to all interfaces intentionally
    port: int = 8765


class PostgresConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serve_dsn: str = "postgresql://tracefold_serve@postgres:5432/tracefold"
    workers_dsn: str = "postgresql://tracefold_workers@postgres:5432/tracefold"
    migrate_dsn: str = "postgresql://tracefold_migrate@postgres:5432/tracefold"
    serve_password_file: str | None = "postgres_serve_password"
    workers_password_file: str | None = "postgres_workers_password"
    migrate_password_file: str | None = "postgres_migrate_password"
    connect_timeout_seconds: float = 5.0

    @field_validator("serve_dsn", "workers_dsn", "migrate_dsn", mode="before")
    @classmethod
    def parse_dsn(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("postgres role DSN is required")
        return normalized

    @field_validator(
        "serve_password_file",
        "workers_password_file",
        "migrate_password_file",
        mode="before",
    )
    @classmethod
    def parse_optional_path(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    postgres: PostgresConfig = Field(default_factory=PostgresConfig)


class _LlmEndpointConfig(BaseModel):
    """One complete direct model endpoint."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = Field(default=None, repr=False)
    model: str | None = None

    @field_validator("api_key", "model", mode="before")
    @classmethod
    def parse_optional_string(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("base_url", mode="before")
    @classmethod
    def parse_optional_base_url(cls, value: Any) -> str | None:
        normalized = str(value or "").strip().rstrip("/")
        return normalized or None

    @model_validator(mode="after")
    def require_complete_configuration(self) -> _LlmEndpointConfig:
        configured = (self.api_key, self.base_url, self.model)
        if any(configured) and not all(configured):
            raise ValueError(self.incomplete_error_code)
        return self

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    @property
    def incomplete_error_code(self) -> str:
        return "llm_endpoint_configuration_incomplete"


class LlmFallbackConfig(_LlmEndpointConfig):
    """A second endpoint used only when the primary Program route fails (issue #65)."""

    @property
    def incomplete_error_code(self) -> str:
        return "llm_fallback_configuration_incomplete"


class LlmReaderCardConfig(_LlmEndpointConfig):
    """Optional primary endpoint dedicated to the ReaderCard Predictor."""


class LlmReaderCardFallbackConfig(_LlmEndpointConfig):
    """Optional ReaderCard endpoint for the all-or-none fallback route."""

    @property
    def incomplete_error_code(self) -> str:
        return "llm_reader_card_fallback_configuration_incomplete"


class LlmCompilerReflectionConfig(_LlmEndpointConfig):
    """The GEPA reflection endpoint — deliberately not the task endpoint (#143).

    DSPy's own guidance is that "when optimizing smaller models, it's worthwhile to use a larger model as the
    `reflection_lm`", and until now the compiler passed the task endpoint object for both. That made the local
    27B student its own teacher, gave the reflection call the task route's 1,200-token ceiling (it has to emit
    a whole new instruction) and its 20 s route deadline, and pointed a multi-hour optimization run at the same
    single-slot GPU production Triage runs on.
    """

    @property
    def incomplete_error_code(self) -> str:
        return "llm_compiler_reflection_configuration_incomplete"


class LlmCompilerTariffConfig(BaseModel):
    """Trusted, secret-free worst-case rates for the optional cold compiler."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    tariff_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    input_token_overhead: int | None = Field(default=None, gt=0, le=100_000)
    task_input_microusd_per_million: int | None = Field(default=None, gt=0)
    task_output_microusd_per_million: int | None = Field(default=None, gt=0)
    reflection_input_microusd_per_million: int | None = Field(default=None, gt=0)
    reflection_output_microusd_per_million: int | None = Field(default=None, gt=0)
    metric_judge_input_microusd_per_million: int | None = Field(default=None, gt=0)
    metric_judge_output_microusd_per_million: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_complete_tariff(self) -> LlmCompilerTariffConfig:
        values = (
            self.tariff_id,
            self.input_token_overhead,
            self.task_input_microusd_per_million,
            self.task_output_microusd_per_million,
            self.reflection_input_microusd_per_million,
            self.reflection_output_microusd_per_million,
            self.metric_judge_input_microusd_per_million,
            self.metric_judge_output_microusd_per_million,
        )
        if any(value is not None for value in values) and not all(value is not None for value in values):
            raise ValueError("llm_news_compiler_tariff_incomplete")
        return self

    @property
    def configured(self) -> bool:
        return self.tariff_id is not None


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = Field(default=None, repr=False)
    news_triage_model: str | None = None
    trading_decision_model: str | None = None
    news_reader_card: LlmReaderCardConfig = Field(default_factory=LlmReaderCardConfig)
    news_triage_fallback: LlmFallbackConfig = Field(default_factory=LlmFallbackConfig)
    news_reader_card_fallback: LlmReaderCardFallbackConfig = Field(default_factory=LlmReaderCardFallbackConfig)
    news_compiler_reflection: LlmCompilerReflectionConfig = Field(default_factory=LlmCompilerReflectionConfig)
    news_compiler_tariff: LlmCompilerTariffConfig = Field(default_factory=LlmCompilerTariffConfig)

    @field_validator("api_key", "news_triage_model", "trading_decision_model", mode="before")
    @classmethod
    def parse_optional_string(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("base_url", mode="before")
    @classmethod
    def parse_optional_base_url(cls, value: Any) -> str | None:
        normalized = str(value or "").strip().rstrip("/")
        return normalized or None

    @model_validator(mode="after")
    def require_complete_direct_configuration(self) -> LlmConfig:
        configured = (self.api_key, self.base_url, self.news_triage_model)
        if any(configured) and not all(configured):
            raise ValueError("llm_direct_configuration_incomplete")
        if self.news_triage_fallback.configured and not all(configured):
            raise ValueError("llm_fallback_without_primary")
        if self.news_reader_card.configured and not all(configured):
            raise ValueError("llm_reader_card_without_primary")
        if self.news_reader_card_fallback.configured and not self.news_triage_fallback.configured:
            raise ValueError("llm_reader_card_fallback_without_event_fallback")
        return self


class NewsPushSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    feishu_webhook_url: str | None = None
    feishu_signing_secret: str | None = None
    min_interval_seconds: float = 0.6

    @field_validator("feishu_webhook_url", "feishu_signing_secret", mode="before")
    @classmethod
    def parse_optional_secret(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_pacing(self) -> NewsPushSettings:
        if self.min_interval_seconds < 0 or self.min_interval_seconds > 60:
            raise ValueError("news_push_min_interval_invalid")
        return self


class NewsBrokerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str | None = Field(default=None, repr=False)
    name_prefix: str = ""
    connect_timeout_seconds: float = 10.0

    @field_validator("url", mode="before")
    @classmethod
    def parse_url(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"amqp", "amqps"} or not parsed.hostname:
            raise ValueError("news_broker_url_invalid")
        return normalized

    @field_validator("name_prefix", mode="before")
    @classmethod
    def parse_prefix(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if normalized and not re.fullmatch(r"[a-z0-9_.-]{1,32}", normalized):
            raise ValueError("news_broker_name_prefix_invalid")
        return normalized


class NewsTriageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concurrency: int = 4
    circuit_failures: int = 3
    circuit_open_seconds: float = 60.0

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsTriageSettings:
        if not 1 <= self.concurrency <= 32:
            raise ValueError("news_triage_concurrency_invalid")
        return self


class NewsOiSettings(BaseModel):
    """Operator-owned thresholds for the deterministic open-interest lane (tracefold.news.oi_signals)."""

    model_config = ConfigDict(extra="forbid")

    window_ms: int = 4 * 3_600_000
    # The opening moves of a run, by count. This is the knob that decides volume.
    max_rank_in_window: int = 2
    # A frame must *exceed* this: the rule is 大于 80%, so exactly 8000 does not qualify.
    whale_oi_ratio_above_bps: int = 8_000
    oi_change_at_least_bps: int = 0


class NewsPolicySettings(BaseModel):
    """The four operator-owned v10 duplicate/safety knobs used by ``decide()``."""

    model_config = ConfigDict(extra="forbid")

    restatement_drop: bool = True
    # This is duplicate evidence, not a reader quota. Zero disables the
    # deterministic similarity check.
    similarity_max: float = 0.25
    # Exchange listing/delisting frames share one wire template but name different instruments, so
    # they are exempt from the restatement drop and the similarity throttle.
    listing_exempt_from_duplicate: bool = True
    # #154: an artifact already this old when the provider pushed it is a replay, not news. Only
    # x/twitter frames carry their own publication time; everything else is unaffected. Zero disables.
    stale_source_max_age_s: int = 12 * 60 * 60

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsPolicySettings:
        if not 0.0 <= float(self.similarity_max) <= 1.0:
            raise ValueError("news_policy_similarity_max_invalid")
        if int(self.stale_source_max_age_s) < 0:
            raise ValueError("news_policy_stale_source_max_age_s_invalid")
        return self


class NewsRetentionSettings(BaseModel):
    """How long News keeps material facts. Two tiers, because the corpus and the audit trail have different
    lifetimes (#81): a raw Item nobody judged is storage, an Item behind a judged or labelled Event is evidence.

    The 30-day purge deletes `news_items`, and the FK chain cascades to `news_events` and from there to every
    verdict, delivery, member, asset, band **and operator label** — so the whole learning plane had a 30-day
    lifetime and any release gate built on it would go blind after a month.
    """

    model_config = ConfigDict(extra="forbid")

    raw_days: int = 30
    judged_days: int = 365

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsRetentionSettings:
        if not 1 <= self.raw_days <= 3650:
            raise ValueError("news_retention_raw_days_invalid")
        if not self.raw_days <= self.judged_days <= 3650:
            raise ValueError("news_retention_judged_days_invalid")
        return self


class NewsGateSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suppress_low_signal: bool = False


class NewsVenuesSettings(BaseModel):
    """Instrument-universe snapshot (#75). Read-only, unauthenticated public catalogues; no credentials."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    binance: bool = True
    hyperliquid: bool = True
    # #91: the US listed-symbol directory. Not a venue — a reference tier that only tells the Gate a ticker is a
    # stock, and never overrides a symbol a real venue lists.
    us_reference: bool = True
    snapshot_period_hours: float = 6.0

    @model_validator(mode="after")
    def validate_period(self) -> NewsVenuesSettings:
        if not 0.5 <= self.snapshot_period_hours <= 168.0:
            raise ValueError("news_venues_snapshot_period_invalid")
        return self


class NewsWatchlistEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market_type: str = "any"

    @field_validator("symbol", mode="before")
    @classmethod
    def parse_symbol(cls, value: Any) -> str:
        normalized = str(value or "").strip().upper().replace("XYZ-", "")
        if not re.fullmatch(r"[A-Z0-9._-]{1,16}", normalized):
            raise ValueError("news_watchlist_symbol_invalid")
        return normalized


class NewsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = True
    opennews_token: str | None = None
    broker: NewsBrokerSettings = Field(default_factory=NewsBrokerSettings)
    triage: NewsTriageSettings = Field(default_factory=NewsTriageSettings)
    push: NewsPushSettings = Field(default_factory=NewsPushSettings)
    policy: NewsPolicySettings = Field(default_factory=NewsPolicySettings)
    oi: NewsOiSettings = Field(default_factory=NewsOiSettings)
    retention: NewsRetentionSettings = Field(default_factory=NewsRetentionSettings)
    gate: NewsGateSettings = Field(default_factory=NewsGateSettings)
    venues: NewsVenuesSettings = Field(default_factory=NewsVenuesSettings)
    watchlist: tuple[NewsWatchlistEntry, ...] = ()

    @field_validator("opennews_token", mode="before")
    @classmethod
    def parse_opennews_token(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("watchlist", mode="before")
    @classmethod
    def parse_watchlist(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            raise ValueError("news_watchlist_invalid")
        return tuple(value)

    @property
    def watchlist_symbols(self) -> frozenset[str]:
        return frozenset(entry.symbol for entry in self.watchlist)


class TradingCandidateSettings(BaseModel):
    """What may become a Trading case at all (#104). Every bound here is a universe filter, not sizing."""

    model_config = ConfigDict(extra="forbid")

    max_age_seconds: int = 300
    news_lookback_seconds: int = 3_600
    oi_lookback_seconds: int = 1_800
    symbol_cooldown_seconds: int = 1_800
    max_rank_in_window: int = 2
    # 20M, not the 1M a "universe-quality floor" suggests. `docs/research/oi-agent-design-2026-08-22.md`
    # §1.5 measured the 10-50M OI bucket as the *worst* (+4h -0.77%, 48% win) and >200M as the best; a
    # one-million floor admits the losing bucket wholesale.
    min_oi_value_usd: int = 20_000_000
    max_dspy_cases_per_day: int = 12

    @model_validator(mode="after")
    def validate_bounds(self) -> TradingCandidateSettings:
        if not 30 <= self.max_age_seconds <= 3_600:
            raise ValueError("trading_candidate_max_age_invalid")
        if not 1 <= self.max_rank_in_window <= 10:
            raise ValueError("trading_candidate_rank_invalid")
        if not 0 <= self.max_dspy_cases_per_day <= 500:
            raise ValueError("trading_candidate_dspy_budget_invalid")
        return self


class TradingRegimeSettings(BaseModel):
    """The OI/price quadrant band. See `tracefold.trading.regime` for the measurement behind the defaults."""

    model_config = ConfigDict(extra="forbid")

    lookback_seconds: int = 3_600
    min_price_move_bps: int = 100
    max_price_move_bps: int = 600

    @model_validator(mode="after")
    def validate_band(self) -> TradingRegimeSettings:
        if not 300 <= self.lookback_seconds <= 86_400:
            raise ValueError("trading_regime_lookback_invalid")
        if self.min_price_move_bps < 0 or self.max_price_move_bps <= self.min_price_move_bps:
            # A maximum is mandatory: with only a floor the rule keeps exactly the chasing trades the
            # measured inverted-U rejects.
            raise ValueError("trading_regime_band_invalid")
        return self


class TradingPolicySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_short: bool = False
    live_min_surprise: int = 2
    live_max_price_in: int = 1
    min_whale_long_profit_bps: int = 9_500

    @model_validator(mode="after")
    def validate_bounds(self) -> TradingPolicySettings:
        if not 0 <= self.live_min_surprise <= 3:
            raise ValueError("trading_policy_surprise_invalid")
        if not 0 <= self.live_max_price_in <= 3:
            raise ValueError("trading_policy_price_in_invalid")
        if not 0 <= self.min_whale_long_profit_bps <= 100_000:
            raise ValueError("trading_policy_whale_profit_invalid")
        return self


class TradingVenueSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: tuple[str, ...] = ("binance", "hyperliquid")
    binance_enabled: bool = True
    hyperliquid_enabled: bool = True

    @field_validator("priority", mode="before")
    @classmethod
    def parse_priority(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ("binance", "hyperliquid")
        if not isinstance(value, list | tuple):
            raise ValueError("trading_venue_priority_invalid")
        return tuple(str(item).strip().lower() for item in value)

    @model_validator(mode="after")
    def validate_priority(self) -> TradingVenueSettings:
        allowed = {"binance", "hyperliquid"}
        if not self.priority or set(self.priority) - allowed:
            raise ValueError("trading_venue_priority_invalid")
        if len(set(self.priority)) != len(self.priority):
            raise ValueError("trading_venue_priority_duplicate")
        return self

    @property
    def enabled(self) -> tuple[str, ...]:
        flags = {"binance": self.binance_enabled, "hyperliquid": self.hyperliquid_enabled}
        return tuple(venue for venue in self.priority if flags.get(venue, False))


class TradingOrderSettings(BaseModel):
    """Deterministic order parameters. Nothing here is ever chosen by a model."""

    model_config = ConfigDict(extra="forbid")

    fixed_notional_usd: Decimal = Decimal("50")
    leverage: int = 1
    fixed_stop_bps: int = 200
    take_profit_bps: int = 0
    max_holding_seconds: int = 1_800
    max_spread_bps: int = 30
    max_open_underlyings: int = 2
    max_orders_per_day: int = 4

    @field_validator("fixed_notional_usd", mode="before")
    @classmethod
    def parse_notional(cls, value: Any) -> Decimal:
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError) as exc:
            raise ValueError("trading_order_notional_invalid") from exc

    @model_validator(mode="after")
    def validate_bounds(self) -> TradingOrderSettings:
        if not Decimal("1") <= self.fixed_notional_usd <= Decimal("10000"):
            raise ValueError("trading_order_notional_invalid")
        if self.leverage != 1:
            raise ValueError("trading_order_leverage_must_be_one")
        if not 20 <= self.fixed_stop_bps <= 2_000:
            raise ValueError("trading_order_stop_invalid")
        if self.take_profit_bps and self.take_profit_bps <= self.fixed_stop_bps:
            raise ValueError("trading_order_take_profit_invalid")
        if not 60 <= self.max_holding_seconds <= 86_400:
            raise ValueError("trading_order_max_holding_invalid")
        if not 1 <= self.max_open_underlyings <= 10:
            raise ValueError("trading_order_open_underlyings_invalid")
        if not 1 <= self.max_orders_per_day <= 100:
            raise ValueError("trading_order_daily_cap_invalid")
        return self

    @property
    def worst_case_daily_loss_usd(self) -> Decimal:
        """`fixed_notional x fixed_stop_bps x max_orders_per_day` — the envelope the operator signs off."""

        return (
            self.fixed_notional_usd * Decimal(self.fixed_stop_bps) / Decimal(10_000) * Decimal(self.max_orders_per_day)
        )


class TradingOpenTradeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    base_url: str | None = None
    token_file: str | None = "opentrade_token"
    request_timeout_seconds: float = 8.0

    @field_validator("base_url", mode="before")
    @classmethod
    def parse_base_url(cls, value: Any) -> str | None:
        normalized = str(value or "").strip().rstrip("/")
        return normalized or None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token_file)


class TradingSettings(BaseModel):
    """`tracefold.trading`. Disabled by default; paper never reads the OpenTrade token."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    mode: Literal["paper", "live_reviewed", "live_bounded"] = "paper"
    poll_seconds: float = 2.0
    account_ref: str = "default"
    candidates: TradingCandidateSettings = Field(default_factory=TradingCandidateSettings)
    regime: TradingRegimeSettings = Field(default_factory=TradingRegimeSettings)
    policy: TradingPolicySettings = Field(default_factory=TradingPolicySettings)
    venues: TradingVenueSettings = Field(default_factory=TradingVenueSettings)
    order: TradingOrderSettings = Field(default_factory=TradingOrderSettings)
    opentrade: TradingOpenTradeSettings = Field(default_factory=TradingOpenTradeSettings)

    @model_validator(mode="after")
    def validate_mode(self) -> TradingSettings:
        if not 0.5 <= self.poll_seconds <= 60.0:
            raise ValueError("trading_poll_seconds_invalid")
        if self.mode != "paper" and not self.opentrade.configured:
            # Startup, not first order: a live mode with no provider contract would discover it only
            # once a case had already been decided.
            raise ValueError("trading_live_mode_requires_opentrade")
        if self.mode != "paper" and not self.venues.enabled:
            raise ValueError("trading_live_mode_requires_enabled_venue")
        return self

    @property
    def is_live(self) -> bool:
        return self.mode in ("live_reviewed", "live_bounded")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    _config_dir: Path = PrivateAttr(default_factory=app_home)

    ws_token: str | None = None
    api: ApiConfig = Field(default_factory=ApiConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    news: NewsSettings = Field(default_factory=NewsSettings)
    trading: TradingSettings = Field(default_factory=TradingSettings)

    def set_config_dir(self, value: Path) -> None:
        self._config_dir = value

    @property
    def app_home(self) -> Path:
        return self._config_dir

    def postgres_dsn(self, role: Literal["serve", "workers", "migrate"]) -> str:
        return cast(str, getattr(self.storage.postgres, f"{role}_dsn"))

    def postgres_password_file(
        self,
        role: Literal["serve", "workers", "migrate"],
    ) -> Path | None:
        value = cast(str | None, getattr(self.storage.postgres, f"{role}_password_file"))
        if not value:
            return None
        configured = Path(value).expanduser()
        if configured.is_absolute():
            return configured
        return self._config_dir / configured

    @property
    def log_file(self) -> Path:
        return app_log_path(self._config_dir)

    @field_validator("ws_token", mode="before")
    @classmethod
    def parse_optional_ws_token(cls, value: Any) -> str | None:
        if value is None:
            return None
        token = str(value).strip()
        return token or None


@dataclass(frozen=True, slots=True)
class NewsPushAvailability:
    requested: bool
    delivery_available: bool
    reason: str | None
    feishu_webhook_url_configured: bool
    feishu_signing_secret_configured: bool


def news_push_availability(settings: Settings) -> NewsPushAvailability:
    push = settings.news.push
    requested = push.enabled
    webhook_configured = bool(push.feishu_webhook_url)
    reason: str | None = None
    if requested and not settings.news.enabled:
        reason = "news_item_push_news_disabled"
    elif requested and not webhook_configured:
        reason = "news_item_push_feishu_webhook_missing"
    elif requested and not _is_feishu_webhook_url(push.feishu_webhook_url):
        reason = "news_item_push_feishu_webhook_invalid"
    return NewsPushAvailability(
        requested=requested,
        delivery_available=requested and reason is None,
        reason=reason,
        feishu_webhook_url_configured=webhook_configured,
        feishu_signing_secret_configured=bool(push.feishu_signing_secret),
    )


@dataclass(frozen=True, slots=True)
class NewsModelAvailability:
    triage_configured: bool
    triage_model: str | None
    reader_card_model: str | None
    reader_card_dedicated: bool
    triage_fallback_model: str | None = None
    reader_card_fallback_model: str | None = None
    reader_card_fallback_dedicated: bool = False

    @property
    def program_configured(self) -> bool:
        return bool(self.triage_configured and self.triage_model and self.reader_card_model)


def news_model_availability(settings: Settings) -> NewsModelAvailability:
    direct = bool(settings.llm.api_key and _is_http_base_url(settings.llm.base_url))
    triage = direct and bool(settings.llm.news_triage_model)
    reader = settings.llm.news_reader_card
    reader_ok = triage and reader.configured and _is_http_base_url(reader.base_url)
    fallback = settings.llm.news_triage_fallback
    fallback_ok = triage and fallback.configured and _is_http_base_url(fallback.base_url)
    reader_fallback = settings.llm.news_reader_card_fallback
    reader_fallback_ok = fallback_ok and reader_fallback.configured and _is_http_base_url(reader_fallback.base_url)
    return NewsModelAvailability(
        triage_configured=triage,
        triage_model=settings.llm.news_triage_model if triage else None,
        reader_card_model=(
            reader.model if reader_ok else settings.llm.news_triage_model if triage and not reader.configured else None
        ),
        reader_card_dedicated=bool(reader_ok),
        triage_fallback_model=fallback.model if fallback_ok else None,
        reader_card_fallback_model=(
            reader_fallback.model
            if reader_fallback_ok
            else fallback.model
            if fallback_ok and not reader_fallback.configured
            else None
        ),
        reader_card_fallback_dedicated=bool(reader_fallback_ok),
    )


def _is_feishu_webhook_url(value: str | None) -> bool:
    if value is None:
        return False
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    hook_id = parsed.path.removeprefix("/open-apis/bot/v2/hook/")
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "open.feishu.cn"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and hook_id
        and hook_id != parsed.path
        and "/" not in hook_id
    )


def _is_http_base_url(value: str | None) -> bool:
    if value is None:
        return False
    try:
        parsed = urlsplit(value)
        _port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )
