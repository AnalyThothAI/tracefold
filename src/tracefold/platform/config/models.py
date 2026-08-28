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
    serve_password_file: str | None = "postgres_serve_password"
    workers_password_file: str | None = "postgres_workers_password"
    migrate_password_file: str | None = "postgres_migrate_password"
    nautilus_password_file: str | None = "postgres_nautilus_password"
    connect_timeout_seconds: float = 5.0

    @field_validator("serve_dsn", "workers_dsn", "migrate_dsn", "nautilus_dsn", mode="before")
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


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = Field(default=None, repr=False)
    news_triage_model: str | None = None
    trading_decision_model: str | None = None
    request: LlmRequestConfig = Field(default_factory=LlmRequestConfig)
    news_reader_card: LlmReaderCardConfig = Field(default_factory=LlmReaderCardConfig)
    news_triage_fallback: LlmFallbackConfig = Field(default_factory=LlmFallbackConfig)
    news_reader_card_fallback: LlmReaderCardFallbackConfig = Field(default_factory=LlmReaderCardFallbackConfig)
    news_compiler_reflection: LlmCompilerReflectionConfig = Field(default_factory=LlmCompilerReflectionConfig)

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
    telegram_bot_token_file: str | None = None
    telegram_chat_id: int | None = None
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

    @field_validator("telegram_chat_id", mode="before")
    @classmethod
    def parse_private_channel_id(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise ValueError("news_push_telegram_chat_id_invalid")
        normalized = str(value).strip()
        if not re.fullmatch(r"-100[1-9][0-9]{5,15}", normalized):
            raise ValueError("news_push_telegram_chat_id_invalid")
        return int(normalized)

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
        # The same 1-10 band `TradingCandidateSettings.max_rank_in_window` carries, for the same reason: this
        # is "the opening N moves of a run", and a mistyped 1000 would silently mean "every frame" — and now
        # also renders a rank slot per unit in the 持仓异动 window card. Fail at startup instead.
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
        # #211 made the scan window `max_age + max(lookback)`, so these two are no longer only fusion
        # rules — they size the query the candidate runner issues every `poll_seconds` against the same
        # PostgreSQL the News plane runs on. Six hours is well past any point-in-time context a trade
        # decision can use, and it keeps the widest reachable horizon inside the read's row ceiling.
        if not 60 <= self.news_lookback_seconds <= 21_600:
            raise ValueError("trading_candidate_news_lookback_invalid")
        if not 60 <= self.oi_lookback_seconds <= 21_600:
            raise ValueError("trading_candidate_oi_lookback_invalid")
        if not 0 <= self.symbol_cooldown_seconds <= 86_400:
            raise ValueError("trading_candidate_symbol_cooldown_invalid")
        if not 1 <= self.max_rank_in_window <= 10:
            raise ValueError("trading_candidate_rank_invalid")
        if not 0 <= self.max_dspy_cases_per_day <= 500:
            raise ValueError("trading_candidate_dspy_budget_invalid")
        return self


class TradingRegimeSettings(BaseModel):
    """The OI/price quadrant band. See `tracefold.trading.decision.regime` for the measured defaults."""

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

    min_whale_long_profit_bps: int = 9_500

    @model_validator(mode="after")
    def validate_bounds(self) -> TradingPolicySettings:
        if not 0 <= self.min_whale_long_profit_bps <= 100_000:
            raise ValueError("trading_policy_whale_profit_invalid")
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


class TradingNautilusSettings(BaseModel):
    """Credential paths for the sole Binance USD-M Demo execution authority."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key_file: str | None = "binance_demo_api_key"
    api_secret_file: str | None = "binance_demo_api_secret"

    @field_validator("api_key_file", "api_secret_file", mode="before")
    @classmethod
    def parse_optional_secret_path(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


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


class TradingManualSettings(BaseModel):
    """One operator-bound Binance Demo manual account controlled from the News Telegram bot."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    venue: Literal["binance_usdm_demo"] = "binance_usdm_demo"
    account_ref: str = Field(default="binance-manual-demo-1", pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    authorized_user_ids: tuple[int, ...] = ()
    api_key_file: str | None = "binance_manual_demo_api_key"
    api_secret_file: str | None = "binance_manual_demo_api_secret"
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

    @field_validator("api_key_file", "api_secret_file", mode="before")
    @classmethod
    def parse_optional_secret_path(cls, value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("authorized_user_ids", mode="before")
    @classmethod
    def parse_authorized_users(cls, value: object) -> tuple[int, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("manual_trading_authorized_users_invalid")
        users: list[int] = []
        for item in value:
            if isinstance(item, bool):
                raise ValueError("manual_trading_authorized_users_invalid")
            try:
                user_id = int(str(item))
            except ValueError as exc:
                raise ValueError("manual_trading_authorized_users_invalid") from exc
            if user_id <= 0:
                raise ValueError("manual_trading_authorized_users_invalid")
            users.append(user_id)
        if len(users) > 8 or len(set(users)) != len(users):
            raise ValueError("manual_trading_authorized_users_invalid")
        return tuple(users)


class TradingSettings(BaseModel):
    """`tracefold.trading`: decision plane plus one Binance USD-M Demo Intent sink."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    candidates: TradingCandidateSettings = Field(default_factory=TradingCandidateSettings)
    regime: TradingRegimeSettings = Field(default_factory=TradingRegimeSettings)
    policy: TradingPolicySettings = Field(default_factory=TradingPolicySettings)
    order: TradingOrderSettings = Field(default_factory=TradingOrderSettings)
    nautilus: TradingNautilusSettings = Field(default_factory=TradingNautilusSettings)
    manual: TradingManualSettings = Field(default_factory=TradingManualSettings)


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

    def postgres_dsn(self, role: Literal["serve", "workers", "migrate", "nautilus"]) -> str:
        return cast(str, getattr(self.storage.postgres, f"{role}_dsn"))

    def postgres_password_file(
        self,
        role: Literal["serve", "workers", "migrate", "nautilus"],
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

    def trading_nautilus_api_key_file(self) -> Path | None:
        return self._configured_path(self.trading.nautilus.api_key_file)

    def trading_nautilus_api_secret_file(self) -> Path | None:
        return self._configured_path(self.trading.nautilus.api_secret_file)

    def trading_manual_api_key_file(self) -> Path | None:
        return self._configured_path(self.trading.manual.api_key_file)

    def trading_manual_api_secret_file(self) -> Path | None:
        return self._configured_path(self.trading.manual.api_secret_file)

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
    telegram_chat_id_configured: bool


@dataclass(frozen=True, slots=True)
class ManualTradingAvailability:
    requested: bool
    interaction_available: bool
    reason: str | None
    venue: Literal["binance_usdm_demo"]
    authorized_user_count: int
    credentials_configured: bool


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
    telegram_configured = bool(push.telegram_bot_token_file or push.telegram_chat_id)
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
    elif requested and provider == "telegram" and push.telegram_chat_id is None:
        reason = "news_item_push_telegram_chat_id_missing"
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
        telegram_chat_id_configured=push.telegram_chat_id is not None,
    )


def manual_trading_availability(
    settings: Settings,
    *,
    inspect_secret_files: bool = True,
) -> ManualTradingAvailability:
    """Resolve the complete operator, Telegram, and account authority without exposing any secret."""

    manual = settings.trading.manual
    requested = manual.enabled
    manual_key_path = settings.trading_manual_api_key_file()
    manual_secret_path = settings.trading_manual_api_secret_file()
    if inspect_secret_files:
        credentials_configured = secret_file_configured(manual_key_path) and secret_file_configured(manual_secret_path)
    else:
        credentials_configured = bool(manual.api_key_file and manual.api_secret_file)
    push = news_push_availability(settings, inspect_secret_file=inspect_secret_files)
    reason: str | None = None
    if requested and (push.provider != "telegram" or not push.delivery_available):
        reason = "manual_trading_telegram_delivery_unavailable"
    elif requested and not manual.authorized_user_ids:
        reason = "manual_trading_authorized_users_missing"
    elif requested and not credentials_configured:
        reason = "manual_trading_credentials_unavailable"
    elif (
        requested
        and settings.trading.enabled
        and _manual_reuses_auto_credentials(
            settings,
            inspect_secret_files=inspect_secret_files,
        )
    ):
        reason = "manual_trading_account_credential_reuse"
    return ManualTradingAvailability(
        requested=requested,
        interaction_available=bool(requested and reason is None),
        reason=reason,
        venue=manual.venue,
        authorized_user_count=len(manual.authorized_user_ids),
        credentials_configured=credentials_configured,
    )


def _manual_reuses_auto_credentials(settings: Settings, *, inspect_secret_files: bool) -> bool:
    manual_paths = (settings.trading_manual_api_key_file(), settings.trading_manual_api_secret_file())
    auto_paths = (settings.trading_nautilus_api_key_file(), settings.trading_nautilus_api_secret_file())
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
