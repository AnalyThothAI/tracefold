from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text, secret_file_configured
from tracefold.platform.paths import app_home, app_log_path

_TELEGRAM_BOT_TOKEN_RE = re.compile(r"^[0-9]{6,15}:[A-Za-z0-9_-]{30,80}$")


def _parse_telegram_delivery_target_id(value: object, *, error_code: str) -> int:
    if isinstance(value, bool):
        raise ValueError(error_code)
    normalized = str(value).strip()
    if re.fullmatch(r"(?:[1-9][0-9]{5,15}|-[1-9][0-9]{5,15})", normalized) is None:
        raise ValueError(error_code)
    return int(normalized)


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"  # noqa: S104 -- configurable API bind address; defaults to all interfaces intentionally
    port: int = 8765


class PostgresConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serve_dsn: str = "postgresql://tracefold_serve@postgres:5432/tracefold"
    workers_dsn: str = "postgresql://tracefold_workers@postgres:5432/tracefold"
    migrate_dsn: str = "postgresql://tracefold_migrate@postgres:5432/tracefold"
    nautilus_dsn: str = "postgresql://tracefold_nautilus@postgres:5432/tracefold"
    onchain_dsn: str = "postgresql://tracefold_onchain@postgres:5432/tracefold"
    serve_password_file: str | None = "postgres_serve_password"
    workers_password_file: str | None = "postgres_workers_password"
    migrate_password_file: str | None = "postgres_migrate_password"
    nautilus_password_file: str | None = "postgres_nautilus_password"
    onchain_password_file: str | None = "postgres_onchain_password"
    connect_timeout_seconds: float = 5.0

    @field_validator("serve_dsn", "workers_dsn", "migrate_dsn", "nautilus_dsn", "onchain_dsn", mode="before")
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
        "nautilus_password_file",
        "onchain_password_file",
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


class LlmRequestConfig(BaseModel):
    """Provider-neutral controls for one OpenAI-compatible request envelope."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    send_temperature: bool | None = None
    temperature: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    structured_output: Literal["auto", "json_schema", "json_object", "prompt_json"] = "auto"
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_transport_owned_fields(self) -> LlmRequestConfig:
        owned = {
            "api_key",
            "api_base",
            "base_url",
            "max_tokens",
            "messages",
            "model",
            "response_format",
            "stream",
            "temperature",
        }
        overlap = owned.intersection(self.extra_body)
        if overlap:
            raise ValueError(f"llm_request_extra_body_owned:{','.join(sorted(overlap))}")
        return self


class _LlmEndpointConfig(BaseModel):
    """One complete direct model endpoint."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = Field(default=None, repr=False)
    model: str | None = None
    request: LlmRequestConfig = Field(default_factory=LlmRequestConfig)

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


class LlmReaderCardConfig(_LlmEndpointConfig):
    """Optional primary endpoint dedicated to the ReaderCard Predictor."""


class LlmReaderCardFallbackConfig(_LlmEndpointConfig):
    """Optional ReaderCard endpoint dedicated to one complete fallback route."""

    @property
    def incomplete_error_code(self) -> str:
        return "llm_reader_card_fallback_configuration_incomplete"


class LlmFallbackConfig(_LlmEndpointConfig):
    """One complete fallback Program route, in operator-declared order."""

    reader_card: LlmReaderCardFallbackConfig = Field(default_factory=LlmReaderCardFallbackConfig)

    @model_validator(mode="after")
    def require_event_endpoint_for_reader(self) -> LlmFallbackConfig:
        if self.reader_card.configured and not self.configured:
            raise ValueError("llm_reader_card_fallback_without_event_fallback")
        return self

    @property
    def incomplete_error_code(self) -> str:
        return "llm_fallback_configuration_incomplete"


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


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = Field(default=None, repr=False)
    news_triage_model: str | None = None
    request: LlmRequestConfig = Field(default_factory=LlmRequestConfig)
    news_reader_card: LlmReaderCardConfig = Field(default_factory=LlmReaderCardConfig)
    news_fallbacks: tuple[LlmFallbackConfig, ...] = Field(default_factory=tuple, max_length=3)
    news_compiler_reflection: LlmCompilerReflectionConfig = Field(default_factory=LlmCompilerReflectionConfig)

    @field_validator("api_key", "news_triage_model", mode="before")
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
        if self.news_fallbacks and not all(configured):
            raise ValueError("llm_fallback_without_primary")
        if self.news_reader_card.configured and not all(configured):
            raise ValueError("llm_reader_card_without_primary")
        route_ids = tuple((fallback.base_url, fallback.model) for fallback in self.news_fallbacks)
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("llm_fallback_route_duplicate")
        return self


class NewsPushSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    feishu_webhook_url: str | None = None
    feishu_signing_secret: str | None = None
    telegram_bot_token_file: str | None = None
    telegram_chat_ids: tuple[int, ...] = ()
    min_interval_seconds: float = 0.6

    @field_validator("feishu_webhook_url", "feishu_signing_secret", mode="before")
    @classmethod
    def parse_optional_secret(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("telegram_bot_token_file", mode="before")
    @classmethod
    def parse_optional_token_file(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("telegram_chat_ids", mode="before")
    @classmethod
    def parse_telegram_chat_ids(cls, value: Any) -> tuple[int, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("news_push_telegram_chat_ids_invalid")
        try:
            targets = tuple(
                _parse_telegram_delivery_target_id(
                    item,
                    error_code="news_push_telegram_chat_ids_invalid",
                )
                for item in value
            )
        except ValueError:
            raise ValueError("news_push_telegram_chat_ids_invalid") from None
        if len(targets) > 32 or len(set(targets)) != len(targets):
            raise ValueError("news_push_telegram_chat_ids_invalid")
        return targets

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
    # The opening eligible moves of a run, by count. This is the knob that decides volume.
    max_rank_in_window: int = 2
    # A frame must *exceed* this: the rule is 大于 80%, so exactly 8000 does not qualify.
    whale_oi_ratio_above_bps: int = 8_000
    oi_change_at_least_bps: int = 0

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsOiSettings:
        # A 1-10 band, because this is "the opening N moves of a run": a mistyped 1000 would silently
        # mean "every frame", and the 持仓异动 window card renders one rank slot per unit. Fail at
        # startup instead. Trading carried a rank ceiling of its own until #348 retired it; this one
        # is the *notification* gate's and is unrelated to capital.
        if not 1 <= self.max_rank_in_window <= 10:
            raise ValueError("news_oi_max_rank_invalid")
        if not 300_000 <= self.window_ms <= 86_400_000:
            raise ValueError("news_oi_window_invalid")
        if self.whale_oi_ratio_above_bps < 0 or self.oi_change_at_least_bps < 0:
            raise ValueError("news_oi_threshold_invalid")
        return self


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
    okx: bool = True
    lighter: bool = True
    bitget: bool = True
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
    """What may be admitted to the capital lane at all (#104/#331).

    Every bound here is a universe or timing filter, never sizing and never Alpha. The policy's own
    thresholds are code-owned and frozen onto each Case, so an operator cannot move a capital rule
    without a versioned identity moving with it.
    """

    model_config = ConfigDict(extra="forbid")

    max_age_seconds: int = 300
    # 20M, not the 1M a "universe-quality floor" suggests. `docs/research/oi-agent-design-2026-08-22.md`
    # §1.5 measured the 10-50M OI bucket as the *worst* (+4h -0.77%, 48% win) and >200M as the best; a
    # one-million floor admits the losing bucket wholesale.
    min_oi_value_usd: int = 20_000_000

    @model_validator(mode="after")
    def validate_bounds(self) -> TradingCandidateSettings:
        if not 30 <= self.max_age_seconds <= 3_600:
            raise ValueError("trading_candidate_max_age_invalid")
        return self


class TradingOrderSettings(BaseModel):
    """The sole operator execution value left after the #283 authority hard cut."""

    model_config = ConfigDict(extra="forbid")

    fixed_notional_usd: Decimal = Decimal("10")

    @field_validator("fixed_notional_usd", mode="before")
    @classmethod
    def parse_notional(cls, value: Any) -> Decimal:
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError) as exc:
            raise ValueError("trading_order_notional_invalid") from exc

    @model_validator(mode="after")
    def validate_bounds(self) -> TradingOrderSettings:
        if not Decimal("0") < self.fixed_notional_usd <= Decimal("10"):
            raise ValueError("trading_order_notional_invalid")
        return self


class TradingBinanceUsdmSettings(BaseModel):
    """Operator-owned credential paths; #356 owns any provider client that may use them."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key_file: str | None = "binance_usdm_api_key"
    api_secret_file: str | None = "binance_usdm_api_secret"

    @field_validator("api_key_file", "api_secret_file", mode="before")
    @classmethod
    def parse_optional_secret_path(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


class TradingHyperliquidPerpSettings(BaseModel):
    """Agent-wallet inputs only; #357 owns account preflight and adapter construction."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    private_key_file: str | None = "hyperliquid_private_key"
    account_address: str | None = None

    @field_validator("private_key_file", "account_address", mode="before")
    @classmethod
    def parse_optional_value(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


class TradingBindingsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    binance_usdm: TradingBinanceUsdmSettings = Field(default_factory=TradingBinanceUsdmSettings)
    hyperliquid_perp: TradingHyperliquidPerpSettings = Field(default_factory=TradingHyperliquidPerpSettings)


class TradingManualRiskSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notional_deviation_limit_bps: int = Field(default=5_000, ge=0, le=50_000)
    tight_stop_deviation_limit_bps: int = Field(default=5_000, ge=0, le=50_000)
    wide_stop_deviation_limit_bps: int = Field(default=10_000, ge=0, le=50_000)
    max_account_risk_bps: int = Field(default=1_000, gt=0, le=10_000)
    high_risk_loss_multiple_bps: int = Field(default=15_000, ge=10_000, le=100_000)
    min_leverage: int = Field(default=1, ge=1, le=125)
    max_leverage: int = Field(default=20, ge=1, le=125)

    @model_validator(mode="after")
    def validate_leverage_range(self) -> TradingManualRiskSettings:
        if self.max_leverage < self.min_leverage:
            raise ValueError("manual_trading_leverage_range_invalid")
        return self


class TradingManualPresetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leverage: int = Field(ge=1, le=125)
    stop_loss_bps: int = Field(gt=0, lt=10_000)
    take_profit_bps: int = Field(gt=0, le=100_000)
    account_risk_bps: int = Field(gt=0, le=10_000)
    min_notional_usd: Decimal = Decimal("5")
    max_notional_usd: Decimal = Decimal("10")

    @field_validator("min_notional_usd", "max_notional_usd", mode="before")
    @classmethod
    def parse_notional_bound(cls, value: object) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (ArithmeticError, ValueError) as exc:
            raise ValueError("manual_trading_notional_bound_invalid") from exc
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError("manual_trading_notional_bound_invalid")
        return parsed

    @model_validator(mode="after")
    def validate_notional_range(self) -> TradingManualPresetSettings:
        if self.max_notional_usd < self.min_notional_usd:
            raise ValueError("manual_trading_notional_range_invalid")
        return self


class TradingManualAccountSettings(BaseModel):
    """One Telegram user's dedicated Binance USD-M live account."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    live_trading_acknowledged: bool = False
    venue: Literal["binance_usdm_live"] = "binance_usdm_live"
    account_ref: str = Field(default="binance-manual-live", pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    api_key_file: str | None = None
    api_secret_file: str | None = None

    @field_validator("api_key_file", "api_secret_file", mode="before")
    @classmethod
    def parse_optional_secret_path(cls, value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


class TradingManualSettings(BaseModel):
    """Shared manual-futures risk policy; account authority lives in Telegram profiles."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    risk: TradingManualRiskSettings = Field(default_factory=TradingManualRiskSettings)
    tight_stop: TradingManualPresetSettings = Field(
        default_factory=lambda: TradingManualPresetSettings(
            leverage=10,
            stop_loss_bps=100,
            take_profit_bps=200,
            account_risk_bps=200,
        )
    )
    wide_stop: TradingManualPresetSettings = Field(
        default_factory=lambda: TradingManualPresetSettings(
            leverage=2,
            stop_loss_bps=2_000,
            take_profit_bps=10_000,
            account_risk_bps=100,
        )
    )


class TradingOnchainSettlementAssetSettings(BaseModel):
    """One operator-funded quote asset and its execution RPC."""

    model_config = ConfigDict(extra="forbid")

    chain_id: int = Field(gt=0)
    chain_name: str = Field(min_length=1, max_length=40)
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,19}$")
    contract_address: str = Field(pattern=r"^0x[0-9a-f]{40}$")
    decimals: int = Field(ge=0, le=255)
    quote_amount: Decimal = Decimal("10")
    rpc_url: str | None = Field(default=None, repr=False)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return str(value).strip().upper()

    @field_validator("contract_address", mode="before")
    @classmethod
    def normalize_contract(cls, value: object) -> str:
        return str(value).strip().lower()

    @field_validator("quote_amount", mode="before")
    @classmethod
    def parse_quote_amount(cls, value: object) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (ArithmeticError, ValueError) as exc:
            raise ValueError("onchain_quote_amount_invalid") from exc
        if not parsed.is_finite() or not Decimal("0") < parsed <= Decimal("1000"):
            raise ValueError("onchain_quote_amount_invalid")
        return parsed

    @field_validator("rpc_url", mode="before")
    @classmethod
    def parse_rpc_url(cls, value: object) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("onchain_rpc_url_invalid")
        return normalized

    @property
    def quote_amount_raw(self) -> int:
        return int(self.quote_amount * (Decimal(10) ** self.decimals))


class TradingOnchainOkxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = True
    api_key_file: str | None = None
    api_secret_file: str | None = None
    passphrase_file: str | None = None

    @field_validator("api_key_file", "api_secret_file", "passphrase_file", mode="before")
    @classmethod
    def parse_secret_path(cls, value: object) -> str | None:
        if value is None:
            return None
        return str(value).strip() or None


class TradingOnchainOneInchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = True
    api_key_file: str | None = None

    @field_validator("api_key_file", mode="before")
    @classmethod
    def parse_secret_path(cls, value: object) -> str | None:
        if value is None:
            return None
        return str(value).strip() or None


class TradingOnchainBinanceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class TradingOnchainWalletSettings(BaseModel):
    """The one manual EVM wallet shared by every executable onchain route."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    address: str | None = None
    private_key_file: str | None = None
    live_trading_acknowledged: bool = False

    @field_validator("address", mode="before")
    @classmethod
    def parse_address(cls, value: object) -> str | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if re.fullmatch(r"0x[0-9a-f]{40}", normalized) is None:
            raise ValueError("onchain_wallet_address_invalid")
        return normalized

    @field_validator("private_key_file", mode="before")
    @classmethod
    def parse_private_key_path(cls, value: object) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None


class TradingOnchainProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    okx: TradingOnchainOkxSettings = Field(default_factory=TradingOnchainOkxSettings)
    oneinch: TradingOnchainOneInchSettings = Field(default_factory=TradingOnchainOneInchSettings)
    binance: TradingOnchainBinanceSettings = Field(default_factory=TradingOnchainBinanceSettings)


ONCHAIN_EXECUTION_SETTLEMENT_CATALOG_V1: tuple[tuple[int, str, str, int], ...] = (
    (1, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "USDC", 6),
    (56, "0x55d398326f99059ff775485246999027b3197955", "USDT", 18),
    (8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USDC", 6),
    (42161, "0xaf88d065e77c8cc2239327c5edb3a432268e5831", "USDC", 6),
    (4663, "0x5fc5360d0400a0fd4f2af552add042d716f1d168", "USDG", 6),
)
_ONCHAIN_EXECUTION_CHAIN_NAMES_V1 = {
    1: "Ethereum",
    56: "BNB Chain",
    8453: "Base",
    42161: "Arbitrum One",
    4663: "Robinhood Chain",
}
_ONCHAIN_LIVE_EXECUTION_SETTLEMENT_IDENTITIES = frozenset(ONCHAIN_EXECUTION_SETTLEMENT_CATALOG_V1)


def _default_onchain_settlement_assets() -> tuple[TradingOnchainSettlementAssetSettings, ...]:
    return tuple(
        TradingOnchainSettlementAssetSettings(
            chain_id=chain_id,
            chain_name=_ONCHAIN_EXECUTION_CHAIN_NAMES_V1[chain_id],
            symbol=symbol,
            contract_address=contract_address,
            decimals=decimals,
        )
        for chain_id, contract_address, symbol, decimals in ONCHAIN_EXECUTION_SETTLEMENT_CATALOG_V1
    )


def onchain_execution_settlement_supported(asset: TradingOnchainSettlementAssetSettings) -> bool:
    """Return whether code and schema bind this exact settlement identity for signing."""

    return (
        asset.chain_id,
        asset.contract_address,
        asset.symbol,
        asset.decimals,
    ) in _ONCHAIN_LIVE_EXECUTION_SETTLEMENT_IDENTITIES


class TradingOnchainSettings(BaseModel):
    """Shared onchain discovery, route, settlement, and RPC policy."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    slippage_bps: int = Field(default=100, gt=0, le=5_000)
    discovery_chain_ids: tuple[int, ...] = (1, 56, 8453, 42161, 4663)
    settlement_assets: tuple[TradingOnchainSettlementAssetSettings, ...] = Field(
        default_factory=_default_onchain_settlement_assets
    )

    @field_validator("discovery_chain_ids", mode="before")
    @classmethod
    def parse_discovery_chain_ids(cls, value: object) -> tuple[int, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("onchain_discovery_chains_invalid")
        try:
            chains = tuple(int(str(item)) for item in value)
        except ValueError as exc:
            raise ValueError("onchain_discovery_chains_invalid") from exc
        if not 1 <= len(chains) <= 32 or any(chain <= 0 for chain in chains) or len(set(chains)) != len(chains):
            raise ValueError("onchain_discovery_chains_invalid")
        return chains

    @model_validator(mode="after")
    def validate_settlement_assets(self) -> TradingOnchainSettings:
        if not 1 <= len(self.settlement_assets) <= 12:
            raise ValueError("onchain_settlement_assets_invalid")
        chains = [asset.chain_id for asset in self.settlement_assets]
        if len(set(chains)) != len(chains):
            raise ValueError("onchain_settlement_chain_duplicate")
        return self

    @property
    def chain_ids(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys((*self.discovery_chain_ids, *(asset.chain_id for asset in self.settlement_assets))))


class TradingOnchainAccountSettings(BaseModel):
    """One Telegram user's route-provider credentials and one shared-per-route EVM signer."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    providers: TradingOnchainProviderSettings = Field(default_factory=TradingOnchainProviderSettings)
    wallet: TradingOnchainWalletSettings = Field(default_factory=TradingOnchainWalletSettings)


class TradingTelegramProfileSettings(BaseModel):
    """The sole identity-to-trading-authority binding for one Telegram private user."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    user_id: int = Field(gt=0)
    manual: TradingManualAccountSettings = Field(default_factory=TradingManualAccountSettings)
    onchain: TradingOnchainAccountSettings = Field(default_factory=TradingOnchainAccountSettings)

    @model_validator(mode="after")
    def require_enabled_lane(self) -> TradingTelegramProfileSettings:
        if not self.manual.enabled and not self.onchain.enabled:
            raise ValueError("telegram_trading_profile_lane_missing")
        return self


class TradingSettings(BaseModel):
    """Decision Plane configuration plus two closed, credential-optional bindings (#350)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    candidates: TradingCandidateSettings = Field(default_factory=TradingCandidateSettings)
    order: TradingOrderSettings = Field(default_factory=TradingOrderSettings)
    bindings: TradingBindingsSettings = Field(default_factory=TradingBindingsSettings)
    manual: TradingManualSettings = Field(default_factory=TradingManualSettings)
    onchain: TradingOnchainSettings = Field(default_factory=TradingOnchainSettings)
    telegram_profiles: tuple[TradingTelegramProfileSettings, ...] = ()

    @field_validator("telegram_profiles", mode="before")
    @classmethod
    def parse_telegram_profiles(cls, value: object) -> object:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("telegram_trading_profiles_invalid")
        return value

    @model_validator(mode="after")
    def validate_telegram_profiles(self) -> TradingSettings:
        if len(self.telegram_profiles) > 16:
            raise ValueError("telegram_trading_profiles_invalid")
        user_ids = [profile.user_id for profile in self.telegram_profiles]
        if len(set(user_ids)) != len(user_ids):
            raise ValueError("telegram_trading_profile_user_duplicate")
        account_refs = [profile.manual.account_ref for profile in self.telegram_profiles if profile.manual.enabled]
        if len(set(account_refs)) != len(account_refs):
            raise ValueError("telegram_trading_profile_account_duplicate")
        wallet_addresses = [
            profile.onchain.wallet.address
            for profile in self.telegram_profiles
            if profile.onchain.enabled and profile.onchain.wallet.address is not None
        ]
        if len(set(wallet_addresses)) != len(wallet_addresses):
            raise ValueError("telegram_trading_profile_wallet_duplicate")
        secret_paths: list[str] = []
        for profile in self.telegram_profiles:
            manual = profile.manual
            onchain = profile.onchain
            if manual.enabled:
                secret_paths.extend(path for path in (manual.api_key_file, manual.api_secret_file) if path)
            if onchain.enabled:
                okx = onchain.providers.okx
                oneinch = onchain.providers.oneinch
                secret_paths.extend(
                    path
                    for path in (
                        okx.api_key_file,
                        okx.api_secret_file,
                        okx.passphrase_file,
                        oneinch.api_key_file,
                        onchain.wallet.private_key_file,
                    )
                    if path
                )
        if len(set(secret_paths)) != len(secret_paths):
            raise ValueError("telegram_trading_profile_secret_reuse")
        return self

    def telegram_profile(self, user_id: int) -> TradingTelegramProfileSettings | None:
        return next((profile for profile in self.telegram_profiles if profile.user_id == user_id), None)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    _config_dir: Path = PrivateAttr(default_factory=app_home)

    ws_token: str | None = None
    api: ApiConfig = Field(default_factory=ApiConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    news: NewsSettings = Field(default_factory=NewsSettings)
    trading: TradingSettings = Field(default_factory=TradingSettings)

    @model_validator(mode="after")
    def require_compose_profile_secret_names(self) -> Settings:
        """Keep every user's authority in a lane-specific, read-only mounted directory."""

        if self.trading.telegram_profiles and self.news.push.telegram_bot_token_file != "telegram_bot_token":
            raise ValueError("telegram_trading_bot_token_name_invalid")
        targets = set(self.news.push.telegram_chat_ids)
        for profile in self.trading.telegram_profiles:
            user = str(profile.user_id)
            if profile.user_id not in targets:
                raise ValueError("telegram_trading_profile_delivery_target_missing")
            if profile.manual.enabled:
                manual_expected = (
                    (profile.manual.api_key_file, f"trading_profiles/manual/{user}/binance_api_key"),
                    (profile.manual.api_secret_file, f"trading_profiles/manual/{user}/binance_api_secret"),
                )
                if any(actual != required for actual, required in manual_expected):
                    raise ValueError("manual_trading_profile_secret_name_invalid")
            if profile.onchain.enabled:
                okx = profile.onchain.providers.okx
                oneinch = profile.onchain.providers.oneinch
                onchain_expected = (
                    (okx.api_key_file, f"trading_profiles/quotes/{user}/okx_api_key"),
                    (okx.api_secret_file, f"trading_profiles/quotes/{user}/okx_api_secret"),
                    (okx.passphrase_file, f"trading_profiles/quotes/{user}/okx_passphrase"),
                    (oneinch.api_key_file, f"trading_profiles/quotes/{user}/oneinch_api_key"),
                    (profile.onchain.wallet.private_key_file, f"trading_profiles/onchain/{user}/evm_private_key"),
                )
                configured = tuple((actual, required) for actual, required in onchain_expected if actual is not None)
                if any(actual != required for actual, required in configured):
                    raise ValueError("onchain_trading_profile_secret_name_invalid")
        return self

    def set_config_dir(self, value: Path) -> None:
        self._config_dir = value

    @property
    def app_home(self) -> Path:
        return self._config_dir

    def postgres_dsn(self, role: Literal["serve", "workers", "migrate", "nautilus", "onchain"]) -> str:
        return cast(str, getattr(self.storage.postgres, f"{role}_dsn"))

    def postgres_password_file(
        self,
        role: Literal["serve", "workers", "migrate", "nautilus", "onchain"],
    ) -> Path | None:
        value = cast(str | None, getattr(self.storage.postgres, f"{role}_password_file"))
        if not value:
            return None
        configured = Path(value).expanduser()
        if configured.is_absolute():
            return configured
        return self._config_dir / configured

    def news_telegram_bot_token_file(self) -> Path | None:
        return self._configured_path(self.news.push.telegram_bot_token_file)

    def trading_binance_usdm_api_key_file(self) -> Path | None:
        return self._configured_path(self.trading.bindings.binance_usdm.api_key_file)

    def trading_binance_usdm_api_secret_file(self) -> Path | None:
        return self._configured_path(self.trading.bindings.binance_usdm.api_secret_file)

    def trading_hyperliquid_private_key_file(self) -> Path | None:
        return self._configured_path(self.trading.bindings.hyperliquid_perp.private_key_file)

    def trading_manual_api_key_file(self, profile: TradingTelegramProfileSettings) -> Path | None:
        return self._configured_path(profile.manual.api_key_file)

    def trading_manual_api_secret_file(self, profile: TradingTelegramProfileSettings) -> Path | None:
        return self._configured_path(profile.manual.api_secret_file)

    def trading_onchain_okx_api_key_file(self, profile: TradingTelegramProfileSettings) -> Path | None:
        return self._configured_path(profile.onchain.providers.okx.api_key_file)

    def trading_onchain_okx_api_secret_file(self, profile: TradingTelegramProfileSettings) -> Path | None:
        return self._configured_path(profile.onchain.providers.okx.api_secret_file)

    def trading_onchain_okx_passphrase_file(self, profile: TradingTelegramProfileSettings) -> Path | None:
        return self._configured_path(profile.onchain.providers.okx.passphrase_file)

    def trading_onchain_oneinch_api_key_file(self, profile: TradingTelegramProfileSettings) -> Path | None:
        return self._configured_path(profile.onchain.providers.oneinch.api_key_file)

    def trading_onchain_wallet_private_key_file(self, profile: TradingTelegramProfileSettings) -> Path | None:
        return self._configured_path(profile.onchain.wallet.private_key_file)

    def _configured_path(self, value: str | None) -> Path | None:
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
    provider: Literal["feishu", "telegram"] | None
    feishu_webhook_url_configured: bool
    feishu_signing_secret_configured: bool
    telegram_bot_token_file_configured: bool
    telegram_target_count: int


@dataclass(frozen=True, slots=True)
class ManualTradingAvailability:
    requested: bool
    interaction_available: bool
    reason: str | None
    venue: Literal["binance_usdm_live"]
    authorized_user_count: int
    credentials_configured: bool
    available_user_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class OnchainTradingAvailability:
    requested: bool
    interaction_available: bool
    reason: str | None
    authorized_user_count: int
    configured_quote_providers: tuple[Literal["okx", "oneinch"], ...]
    executable_providers: tuple[Literal["okx", "oneinch"], ...]
    execution_available: bool
    execution_reason: str | None
    wallet_address_configured: bool
    wallet_private_key_configured: bool
    rpc_chain_ids: tuple[int, ...]
    okx_credentials_configured: bool
    oneinch_credentials_configured: bool
    binance_reason: Literal["binance_general_web3_swap_api_unpublished"]
    available_user_ids: tuple[int, ...] = ()


def news_push_availability(settings: Settings, *, inspect_secret_file: bool = True) -> NewsPushAvailability:
    push = settings.news.push
    requested = push.enabled
    webhook_configured = bool(push.feishu_webhook_url)
    feishu_configured = bool(push.feishu_webhook_url or push.feishu_signing_secret)
    token_file_configured = (
        _telegram_bot_token_file_configured(settings.news_telegram_bot_token_file())
        if inspect_secret_file
        else bool(push.telegram_bot_token_file)
    )
    telegram_configured = bool(push.telegram_bot_token_file or push.telegram_chat_ids)
    provider: Literal["feishu", "telegram"] | None = (
        None if feishu_configured == telegram_configured else "feishu" if feishu_configured else "telegram"
    )
    reason: str | None = None
    if requested and not settings.news.enabled:
        reason = "news_item_push_news_disabled"
    elif requested and feishu_configured and telegram_configured:
        reason = "news_item_push_provider_conflict"
    elif requested and provider == "telegram" and not token_file_configured:
        reason = "news_item_push_telegram_bot_token_unavailable"
    elif requested and provider == "telegram" and not push.telegram_chat_ids:
        reason = "news_item_push_telegram_targets_missing"
    elif requested and not webhook_configured and provider != "telegram":
        reason = "news_item_push_feishu_webhook_missing"
    elif requested and provider == "feishu" and not _is_feishu_webhook_url(push.feishu_webhook_url):
        reason = "news_item_push_feishu_webhook_invalid"
    return NewsPushAvailability(
        requested=requested,
        delivery_available=requested and reason is None,
        reason=reason,
        provider=provider,
        feishu_webhook_url_configured=webhook_configured,
        feishu_signing_secret_configured=bool(push.feishu_signing_secret),
        telegram_bot_token_file_configured=token_file_configured,
        telegram_target_count=len(push.telegram_chat_ids),
    )


def manual_trading_availability(
    settings: Settings,
    *,
    inspect_secret_files: bool = True,
) -> ManualTradingAvailability:
    """Aggregate the independently resolved Telegram manual-account profiles."""

    rows = tuple(
        manual_trading_profile_availability(settings, profile, inspect_secret_files=inspect_secret_files)
        for profile in settings.trading.telegram_profiles
        if profile.manual.enabled
    )
    requested = bool(rows)
    reason = next((row.reason for row in rows if row.reason is not None), None)
    available_user_ids = tuple(
        profile.user_id
        for profile, row in zip(
            (profile for profile in settings.trading.telegram_profiles if profile.manual.enabled),
            rows,
            strict=True,
        )
        if row.interaction_available
    )
    return ManualTradingAvailability(
        requested=requested,
        interaction_available=bool(requested and len(available_user_ids) == len(rows)),
        reason=reason,
        venue="binance_usdm_live",
        authorized_user_count=len(rows),
        credentials_configured=bool(rows and all(row.credentials_configured for row in rows)),
        available_user_ids=available_user_ids,
    )


def manual_trading_profile_availability(
    settings: Settings,
    profile: TradingTelegramProfileSettings,
    *,
    inspect_secret_files: bool = True,
) -> ManualTradingAvailability:
    manual = profile.manual
    requested = manual.enabled
    manual_key_path = settings.trading_manual_api_key_file(profile)
    manual_secret_path = settings.trading_manual_api_secret_file(profile)
    credentials_configured = (
        secret_file_configured(manual_key_path) and secret_file_configured(manual_secret_path)
        if inspect_secret_files
        else bool(manual.api_key_file and manual.api_secret_file)
    )
    push = news_push_availability(settings, inspect_secret_file=inspect_secret_files)
    reason: str | None = None
    if requested and not manual.live_trading_acknowledged:
        reason = "manual_live_trading_not_acknowledged"
    elif requested and (push.provider != "telegram" or not push.delivery_available):
        reason = "manual_trading_telegram_delivery_unavailable"
    elif requested and profile.user_id not in settings.news.push.telegram_chat_ids:
        reason = "manual_trading_private_target_missing"
    elif requested and not credentials_configured:
        reason = "manual_trading_credentials_unavailable"
    elif requested and _manual_reuses_auto_credentials(
        settings,
        profile,
        inspect_secret_files=inspect_secret_files,
    ):
        reason = "manual_trading_account_credential_reuse"
    return ManualTradingAvailability(
        requested=requested,
        interaction_available=bool(requested and reason is None),
        reason=reason,
        venue=manual.venue,
        authorized_user_count=1 if requested else 0,
        credentials_configured=credentials_configured,
        available_user_ids=(profile.user_id,) if requested and reason is None else (),
    )


def onchain_trading_availability(
    settings: Settings,
    *,
    inspect_secret_files: bool = True,
) -> OnchainTradingAvailability:
    """Aggregate independent Telegram onchain profiles without merging their authority."""

    rows = tuple(
        onchain_trading_profile_availability(settings, profile, inspect_secret_files=inspect_secret_files)
        for profile in settings.trading.telegram_profiles
        if profile.onchain.enabled
    )
    requested = bool(rows)
    reason = next((row.reason for row in rows if row.reason is not None), None)
    available_user_ids = tuple(
        profile.user_id
        for profile, row in zip(
            (profile for profile in settings.trading.telegram_profiles if profile.onchain.enabled),
            rows,
            strict=True,
        )
        if row.interaction_available
    )
    providers = tuple(dict.fromkeys(provider for row in rows for provider in row.configured_quote_providers))
    executable = tuple(dict.fromkeys(provider for row in rows for provider in row.executable_providers))
    execution_reason = next((row.execution_reason for row in rows if row.execution_reason is not None), None)
    return OnchainTradingAvailability(
        requested=requested,
        interaction_available=bool(requested and len(available_user_ids) == len(rows)),
        reason=reason,
        authorized_user_count=len(rows),
        configured_quote_providers=providers,
        executable_providers=executable,
        execution_available=any(row.execution_available for row in rows),
        execution_reason=execution_reason,
        wallet_address_configured=bool(rows and all(row.wallet_address_configured for row in rows)),
        wallet_private_key_configured=bool(rows and all(row.wallet_private_key_configured for row in rows)),
        rpc_chain_ids=tuple(dict.fromkeys(chain for row in rows for chain in row.rpc_chain_ids)),
        okx_credentials_configured=any(row.okx_credentials_configured for row in rows),
        oneinch_credentials_configured=any(row.oneinch_credentials_configured for row in rows),
        binance_reason="binance_general_web3_swap_api_unpublished",
        available_user_ids=available_user_ids,
    )


def onchain_trading_profile_availability(
    settings: Settings,
    profile: TradingTelegramProfileSettings,
    *,
    inspect_secret_files: bool = True,
) -> OnchainTradingAvailability:
    onchain = settings.trading.onchain
    account = profile.onchain
    requested = account.enabled
    okx = account.providers.okx
    oneinch = account.providers.oneinch
    if inspect_secret_files:
        okx_configured = bool(
            secret_file_configured(settings.trading_onchain_okx_api_key_file(profile))
            and secret_file_configured(settings.trading_onchain_okx_api_secret_file(profile))
            and secret_file_configured(settings.trading_onchain_okx_passphrase_file(profile))
        )
        oneinch_configured = secret_file_configured(settings.trading_onchain_oneinch_api_key_file(profile))
        wallet_private_key_configured = secret_file_configured(
            settings.trading_onchain_wallet_private_key_file(profile)
        )
    else:
        okx_configured = bool(okx.api_key_file and okx.api_secret_file and okx.passphrase_file)
        oneinch_configured = bool(oneinch.api_key_file)
        wallet_private_key_configured = bool(account.wallet.private_key_file)
    providers: list[Literal["okx", "oneinch"]] = []
    if okx.enabled and okx_configured:
        providers.append("okx")
    if oneinch.enabled and oneinch_configured:
        providers.append("oneinch")
    push = news_push_availability(settings, inspect_secret_file=inspect_secret_files)
    reason: str | None = None
    if requested and (push.provider != "telegram" or not push.delivery_available):
        reason = "onchain_telegram_delivery_unavailable"
    elif requested and profile.user_id not in settings.news.push.telegram_chat_ids:
        reason = "onchain_private_target_missing"
    rpc_assets = tuple(asset for asset in onchain.settlement_assets if asset.rpc_url)
    rpc_chain_ids = tuple(asset.chain_id for asset in rpc_assets if onchain_execution_settlement_supported(asset))
    executable_provider_rows: list[Literal["okx", "oneinch"]] = []
    if okx.enabled and okx_configured:
        executable_provider_rows.append("okx")
    if oneinch.enabled and oneinch_configured:
        executable_provider_rows.append("oneinch")
    executable_providers = tuple(executable_provider_rows)
    execution_reason: str | None = None
    if requested and not account.wallet.live_trading_acknowledged:
        execution_reason = "onchain_live_trading_not_acknowledged"
    elif requested and account.wallet.address is None:
        execution_reason = "onchain_wallet_address_missing"
    elif requested and not wallet_private_key_configured:
        execution_reason = "onchain_wallet_private_key_unavailable"
    elif requested and rpc_assets and not rpc_chain_ids:
        execution_reason = "onchain_execution_settlement_unsupported"
    elif requested and not rpc_chain_ids:
        execution_reason = "onchain_execution_rpc_unavailable"
    elif requested and not executable_providers:
        execution_reason = "onchain_calldata_verifier_unavailable"
    return OnchainTradingAvailability(
        requested=requested,
        interaction_available=bool(requested and reason is None),
        reason=reason,
        authorized_user_count=1 if requested else 0,
        configured_quote_providers=tuple(providers),
        executable_providers=executable_providers,
        execution_available=bool(requested and reason is None and execution_reason is None),
        execution_reason=execution_reason,
        wallet_address_configured=account.wallet.address is not None,
        wallet_private_key_configured=wallet_private_key_configured,
        rpc_chain_ids=rpc_chain_ids,
        okx_credentials_configured=okx_configured,
        oneinch_credentials_configured=oneinch_configured,
        binance_reason="binance_general_web3_swap_api_unpublished",
        available_user_ids=(profile.user_id,) if requested and reason is None else (),
    )


def _manual_reuses_auto_credentials(
    settings: Settings,
    profile: TradingTelegramProfileSettings,
    *,
    inspect_secret_files: bool,
) -> bool:
    manual_paths = (settings.trading_manual_api_key_file(profile), settings.trading_manual_api_secret_file(profile))
    auto_paths = (settings.trading_binance_usdm_api_key_file(), settings.trading_binance_usdm_api_secret_file())
    if any(manual is not None and manual == auto for manual, auto in zip(manual_paths, auto_paths, strict=True)):
        return True
    if not inspect_secret_files:
        return False
    for manual, auto in zip(manual_paths, auto_paths, strict=True):
        if manual is None or auto is None:
            continue
        try:
            if read_secure_secret_text(manual) == read_secure_secret_text(auto):
                return True
        except SecretFileError:
            continue
    return False


@dataclass(frozen=True, slots=True)
class NewsModelAvailability:
    triage_configured: bool
    triage_model: str | None
    reader_card_model: str | None
    reader_card_dedicated: bool
    triage_fallback_models: tuple[str, ...] = ()
    reader_card_fallback_models: tuple[str, ...] = ()
    reader_card_fallback_dedicated: tuple[bool, ...] = ()

    @property
    def program_configured(self) -> bool:
        return bool(self.triage_configured and self.triage_model and self.reader_card_model)


def news_model_availability(settings: Settings) -> NewsModelAvailability:
    direct = bool(settings.llm.api_key and _is_http_base_url(settings.llm.base_url))
    triage = direct and bool(settings.llm.news_triage_model)
    reader = settings.llm.news_reader_card
    reader_ok = triage and reader.configured and _is_http_base_url(reader.base_url)
    fallback_routes = tuple(
        fallback
        for fallback in settings.llm.news_fallbacks
        if (
            triage
            and fallback.configured
            and _is_http_base_url(fallback.base_url)
            and (not fallback.reader_card.configured or _is_http_base_url(fallback.reader_card.base_url))
        )
    )
    return NewsModelAvailability(
        triage_configured=triage,
        triage_model=settings.llm.news_triage_model if triage else None,
        reader_card_model=(
            reader.model if reader_ok else settings.llm.news_triage_model if triage and not reader.configured else None
        ),
        reader_card_dedicated=bool(reader_ok),
        triage_fallback_models=tuple(fallback.model for fallback in fallback_routes if fallback.model is not None),
        reader_card_fallback_models=tuple(
            cast(
                str,
                (
                    fallback.reader_card.model
                    if fallback.reader_card.configured and _is_http_base_url(fallback.reader_card.base_url)
                    else fallback.model
                ),
            )
            for fallback in fallback_routes
            if fallback.model is not None
        ),
        reader_card_fallback_dedicated=tuple(
            bool(fallback.reader_card.configured and _is_http_base_url(fallback.reader_card.base_url))
            for fallback in fallback_routes
        ),
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


def _telegram_bot_token_file_configured(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        token = read_secure_secret_text(path)
    except SecretFileError:
        return False
    return _TELEGRAM_BOT_TOKEN_RE.fullmatch(token) is not None


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
