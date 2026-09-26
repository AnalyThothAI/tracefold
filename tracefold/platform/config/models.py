from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.platform.paths import app_home, app_log_path
from tracefold.platform.validation import SOCKS_PROXY_URL_SCHEMES, is_feishu_webhook_url, proxy_url_scheme

_TELEGRAM_BOT_TOKEN_RE = re.compile(r"^[0-9]{6,15}:[A-Za-z0-9_-]{30,80}$")


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"  # noqa: S104 -- configurable API bind address; defaults to all interfaces intentionally
    port: int = 8765
    # The console's public origin, as a reader reaches it -- not `host`/`port`, which is the uvicorn
    # bind address and says nothing about the address a browser outside this process can open. Only
    # the operator knows it (reverse proxy, LAN host, tunnel), so there is no default: unset means the
    # deployment has not named one, and a market card is then sent without its detail button (#553).
    public_url: str | None = None

    @field_validator("public_url", mode="before")
    @classmethod
    def parse_public_url(cls, value: Any) -> str | None:
        candidate = str(value or "").strip().rstrip("/")
        if not candidate:
            return None
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("api_public_url_not_absolute_http")
        if parsed.query or parsed.fragment:
            raise ValueError("api_public_url_has_query_or_fragment")
        return candidate


class PostgresConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dsn: str = "postgresql://tracefold@postgres:5432/tracefold"
    password_file: str | None = "postgres_database_password"
    connect_timeout_seconds: float = 5.0

    @field_validator("dsn", mode="before")
    @classmethod
    def parse_dsn(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("postgres DSN is required")
        return normalized

    @field_validator("password_file", mode="before")
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


class _SystemOneRouteConfig(BaseModel):
    """One optional, complete System One route: all three fields or none, and an HTTP(S) base URL."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    error_prefix: ClassVar[str]

    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = None
    model: str | None = None

    @field_validator("api_key", "base_url", "model", mode="before")
    @classmethod
    def normalize(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized.rstrip("/") or None

    @model_validator(mode="after")
    def complete_group(self) -> Self:
        fields = (self.api_key, self.base_url, self.model)
        if any(fields) and not all(fields):
            raise ValueError(f"{self.error_prefix}_configuration_incomplete")
        if self.base_url is not None and not _is_http_base_url(self.base_url):
            raise ValueError(f"{self.error_prefix}_base_url_invalid")
        return self

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


class TradingSemanticsConfig(_SystemOneRouteConfig):
    """An optional, complete System One route independent of News models."""

    error_prefix: ClassVar[str] = "trading_semantics"


class NewsJudgmentConfig(_SystemOneRouteConfig):
    """The optional News Jev judgment route (#706). It is never inferred from `trading_semantics`.

    Unset, every News judgment runs on the generative News endpoints.
    """

    error_prefix: ClassVar[str] = "news_judgment"


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = Field(default=None, repr=False)
    news_triage_model: str | None = None
    request: LlmRequestConfig = Field(default_factory=LlmRequestConfig)
    # Three optional endpoints with one shape. Only the triage fallback names its own incomplete-
    # configuration code, because `llm_fallback_without_primary` reads next to it; the other two
    # say `llm_endpoint_configuration_incomplete` and the field path in the error names which one
    # (#589 P-F12).
    news_reader_card: _LlmEndpointConfig = Field(default_factory=_LlmEndpointConfig)
    news_triage_fallback: LlmFallbackConfig = Field(default_factory=LlmFallbackConfig)
    news_reader_card_fallback: _LlmEndpointConfig = Field(default_factory=_LlmEndpointConfig)
    trading_semantics: TradingSemanticsConfig = Field(default_factory=TradingSemanticsConfig)
    news_judgment: NewsJudgmentConfig = Field(default_factory=NewsJudgmentConfig)

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
    telegram_chat_id: int | str | None = None
    # How this process reaches `api.telegram.org` when it cannot reach it directly. Empty means
    # directly. It is read as written and never reported back: a proxy URL commonly carries
    # credentials, so `tracefold config` says only whether one is configured.
    telegram_proxy_url: str | None = Field(default=None, repr=False)
    min_interval_seconds: float = 0.6

    @field_validator("feishu_webhook_url", "feishu_signing_secret", "telegram_proxy_url", mode="before")
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
    def parse_channel_target(cls, value: Any) -> int | str | None:
        """Read the operator's chat target. What a *valid* one looks like is the adapter's rule.

        Telegram addresses a channel by its Bot API id (`-100…`) or by its public `@name`, and this
        reads whichever the operator wrote: the number as a number, anything else as the text they
        typed. It refuses nothing, because the shape used to be written down twice -- here and in
        `TelegramNewsPushSender`, which is the code that actually talks to Telegram -- and the copy
        here was the more expensive of the two by far: a mistyped digit failed `Settings` validation,
        so the whole process could not start and no `tracefold config` or `/readyz` could say why. One
        typo, and reception, triage and the market loop were down with it. The adapter's refusal costs
        one delivery capability marked `unavailable` beside a running process (#562 §5 rows 1 and 8).
        """

        if value is None or value == "":
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        text = str(value).strip()
        try:
            return int(text)
        except ValueError:
            return text

    @model_validator(mode="after")
    def validate_pacing(self) -> NewsPushSettings:
        if self.min_interval_seconds < 0 or self.min_interval_seconds > 60:
            raise ValueError("news_push_min_interval_invalid")
        return self


class NewsBrokerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str | None = Field(default=None, repr=False)
    # The management HTTP API carries what AMQP cannot: the effective retry policy, ready/unacked/
    # delayed splits and pending at-least-once dead letters. Empty derives it from the AMQP host on the
    # standard management port, which is what every supported deployment runs.
    management_url: str | None = Field(default=None, repr=False)
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

    @field_validator("management_url", mode="before")
    @classmethod
    def parse_management_url(cls, value: Any) -> str | None:
        normalized = str(value or "").strip().rstrip("/")
        if not normalized:
            return None
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("news_broker_management_url_invalid")
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


class NewsChainTapeRosterSettings(BaseModel):
    """Source-list request range and cadence; every valid source address is monitored."""

    model_config = ConfigDict(extra="forbid")

    # Source request scope, not a ranking or a net-buy observation window.
    window: str = "30d"
    # How old a published list may be before the refresh task rebuilds it. One hour, unchanged from
    # the constant the collector used to carry (#649 §5.1), and now the operator's number rather than
    # a literal buried in the tape loop.
    refresh_interval_s: int = 3_600

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsChainTapeRosterSettings:
        # The window reaches the provider as a query parameter; anything but a short token is a
        # configuration error rather than a request to make.
        if not re.fullmatch(r"[0-9]{1,3}[dhwmy]", self.window):
            raise ValueError("news_chain_tape_roster_window_invalid")
        if not 60 <= self.refresh_interval_s <= 86_400:
            raise ValueError("news_chain_tape_roster_refresh_interval_invalid")
        return self


class NewsChainTapeRulesSettings(BaseModel):
    """The one fixed 30m net-buy rule; retired keys are explicit configuration errors.

    `net_buy_fast_n` was the second window's quorum and is now an unknown key: a deployment that still
    carries it fails to start, which is the intended way to notice that the second window is gone
    (#649 PR-3 §2).
    """

    model_config = ConfigDict(extra="forbid")
    net_buy_slow_n: int = Field(default=5, ge=2, le=400)
    min_net_buy_usd: Decimal = Field(default=Decimal("1000"), gt=0, le=Decimal("1e12"), allow_inf_nan=False)
    trigger_max_age_s: int = Field(default=60, ge=1, le=3600)


class NewsChainTapeSettings(BaseModel):
    """The Robinhood Chain wallet tape (#572 PR-1): read-only, disabled by default, store-only.

    Every value here is a runtime parameter, and none of them is a secret: both providers are public and
    unauthenticated, which is why `tracefold config` prints them as they are.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    notifications_enabled: bool = True
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    poll_interval_s: float = 2.0
    roster_provider_url: str = "https://rhtrenches.com"
    roster: NewsChainTapeRosterSettings = Field(default_factory=NewsChainTapeRosterSettings)
    rules: NewsChainTapeRulesSettings = Field(default_factory=NewsChainTapeRulesSettings)
    retention_days: int = 90

    @field_validator("rpc_url", "roster_provider_url", mode="before")
    @classmethod
    def parse_endpoint(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            return normalized
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("news_chain_tape_endpoint_invalid")
        return normalized.rstrip("/")

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsChainTapeSettings:
        if self.enabled and (not self.rpc_url or not self.roster_provider_url):
            raise ValueError("news_chain_tape_endpoint_missing")
        # Blocks are ~0.1 s apart and the overlap is 30 of them; a cadence beyond that window would make
        # the overlap stop overlapping.
        if not 0.5 <= self.poll_interval_s <= 60.0:
            raise ValueError("news_chain_tape_poll_interval_invalid")
        if not 1 <= self.retention_days <= 3650:
            raise ValueError("news_chain_tape_retention_days_invalid")
        return self


class NewsWatchlistEntry(BaseModel):
    """One operator-named symbol the Gate treats as an objective push.

    Symbol only. The entry used to carry a free-string `market_type` defaulting to `"any"` that nothing
    ever read: the Gate compares base symbols, and since #651 §6.2 `market_type` means one exact thing
    elsewhere -- `InstrumentClass` plus `unknown`, on a typed `MarketAsset`. Keeping a second, unrelated
    vocabulary under the same name in operator configuration was the part worth deleting.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str

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
    retention: NewsRetentionSettings = Field(default_factory=NewsRetentionSettings)
    venues: NewsVenuesSettings = Field(default_factory=NewsVenuesSettings)
    chain_tape: NewsChainTapeSettings = Field(default_factory=NewsChainTapeSettings)
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


class TradingExecutionCredentialsSettings(BaseModel):
    """Operator-owned Binance USD-M credential references; values never enter config output."""

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


class TradingExecutionRiskSettings(BaseModel):
    """The Runtime-owned entry and sizing policy, as operator-owned numbers (#510 E, #680).

    The Runtime reads this section once, when it starts, into the profile it runs with; a change to
    any of these numbers takes effect at the next Runtime restart and needs nothing else. None of
    them is a secret and `tracefold config` prints all of them.

    The stop distance stays a Runtime number: the Strategy places the stop, and neither the Case
    nor the Signal ever carries it. Equity, the risk fraction, leverage and venue filters bound
    entry size without separate dollar, position-count or daily-loss gates.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    risk_fraction_per_trade: Decimal = Decimal("0.01")
    max_leverage: int = 1
    stop_distance_bps: int = 100
    # The widest spread an entry may cross, as a fraction of the stop distance: 0.3 of a 100 bps stop
    # is 30 bps. An entry waits for a narrower book within its Signal's TTL rather than being refused
    # on one tick, and the refusal it ends with records the spread it measured.
    max_spread_fraction_of_stop: Decimal = Decimal("0.3")
    # After a stop-out, Signals for the same market are refused for this long; manual entries are not.
    post_stop_cooldown_seconds: int = 14_400
    market_stale_after_seconds: float = 5.0

    @model_validator(mode="after")
    def validate_bounds(self) -> TradingExecutionRiskSettings:
        # A single stop-out remains bounded by a fraction of equity.
        if not Decimal("0") < self.risk_fraction_per_trade <= Decimal("0.05"):
            raise ValueError("trading_execution_risk_fraction_invalid")
        # Sizing is fixed-risk and only clamps notional to `equity * leverage`, so leverage is a
        # notional ceiling, not a risk input. Twenty keeps a stop-out from reaching liquidation.
        if not 1 <= self.max_leverage <= 20:
            raise ValueError("trading_execution_max_leverage_invalid")
        # The same bound `OiInstrumentRoute` enforces: a stop inside one basis point is inside the
        # spread, and one at half the mark is not a stop.
        if not 1 <= self.stop_distance_bps <= 5_000:
            raise ValueError("trading_execution_stop_distance_invalid")
        # A spread as wide as the stop spends the whole stop on entry.
        if not Decimal("0") < self.max_spread_fraction_of_stop <= Decimal("1"):
            raise ValueError("trading_execution_max_spread_invalid")
        if not 0 <= self.post_stop_cooldown_seconds <= 604_800:
            raise ValueError("trading_execution_post_stop_cooldown_invalid")
        # A quote older than a minute is not a price to size an order against.
        if not 1.0 <= self.market_stale_after_seconds <= 60.0:
            raise ValueError("trading_execution_market_stale_invalid")
        return self


class TradingExitPolicySettings(BaseModel):
    """Execution defaults shared by every Binance connection."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    policy_id: Literal["oi_fixed_v1"] = "oi_fixed_v1"
    take_profit_bps: int = Field(ge=1, le=50_000)
    max_holding_seconds: int = Field(ge=1, le=2_592_000)


class TradingBinanceConnectionSettings(BaseModel):
    """Native Binance adapter target shared by market data and execution clients."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    environment: Literal["LIVE", "DEMO", "TESTNET"] | None = None


class TradingExecutionSettings(BaseModel):
    """The one Binance USD-M connection this deployment executes for."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    account_slot: str = "binance_usdm_primary"
    binance: TradingBinanceConnectionSettings = Field(default_factory=TradingBinanceConnectionSettings)
    credentials: TradingExecutionCredentialsSettings = Field(default_factory=TradingExecutionCredentialsSettings)
    risk: TradingExecutionRiskSettings = Field(default_factory=TradingExecutionRiskSettings)
    exit_policy: TradingExitPolicySettings | None = None

    @field_validator("account_slot")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}", value) is None:
            raise ValueError("trading_execution_identity_invalid")
        return value


class TradingVerifiedRouteSettings(BaseModel):
    """Operator-reviewed economic identity for a nonstandard native contract."""

    model_config = ConfigDict(extra="forbid")

    source_symbol: str = Field(pattern=r"^[A-Z0-9]+$")
    asset_id: str = Field(pattern=r"^crypto:[A-Z0-9]+$")
    native_symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")
    units_per_contract: Decimal = Field(gt=0)
    evidence_ref: str = Field(min_length=1, max_length=240)


class TradingAnalysisSettings(BaseModel):
    """One analysis deployment; model credentials use the existing LLM endpoint."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    model_name: str | None = None
    active_policy: Literal["entry_plan_v1"] = "entry_plan_v1"
    publish_signals: bool = False
    verified_routes: list[TradingVerifiedRouteSettings] = Field(default_factory=list)
    excluded_asset_ids: list[str] = Field(
        default_factory=lambda: [
            "commodity:CL",
            "crypto:BTC",
            "crypto:ETH",
            "crypto:USDT",
            "crypto:USDC",
        ]
    )
    root_ttl_seconds: int = Field(default=600, ge=60, le=3_600)
    max_active_cases: int = Field(default=8, ge=1, le=32)
    model_timeout_seconds: int = Field(default=60, ge=1, le=120)
    max_model_input_bytes: int = Field(default=65_536, ge=1_024, le=131_072)
    max_model_output_tokens: int = Field(default=2_000, ge=256, le=4_096)
    max_model_concurrent_calls: int = Field(default=2, ge=1, le=8)
    model_cost_budget_microusd: int | None = Field(default=5_000_000, ge=1)
    model_input_price_ceiling_usd_per_million: Decimal | None = Field(default=Decimal("100"), gt=0)
    model_output_price_ceiling_usd_per_million: Decimal | None = Field(default=Decimal("500"), gt=0)
    market_max_connections: int = Field(default=8, ge=1, le=32)
    market_max_cached_rows: int = Field(default=50_000, ge=1_000, le=200_000)
    market_weight_soft_limit_1m: int = Field(default=1_800, ge=100, le=5_000)

    @model_validator(mode="after")
    def validate_model_cost_budget(self) -> TradingAnalysisSettings:
        values = (
            self.model_cost_budget_microusd,
            self.model_input_price_ceiling_usd_per_million,
            self.model_output_price_ceiling_usd_per_million,
        )
        if any(value is not None for value in values) and any(value is None for value in values):
            raise ValueError("trading_analysis_model_cost_budget_incomplete")
        return self

    @field_validator("excluded_asset_ids")
    @classmethod
    def validate_exclusions(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(
            value not in {"commodity:CL", "crypto:BTC", "crypto:ETH", "crypto:USDT", "crypto:USDC"}
            and not re.fullmatch(r"crypto:[A-Z0-9]+", value)
            for value in values
        ):
            raise ValueError("trading_analysis_exclusions_invalid")
        return values


class TradingSettings(BaseModel):
    """Analysis process plus one cold Binance USD-M Runtime profile."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    analysis: TradingAnalysisSettings = Field(default_factory=TradingAnalysisSettings)
    execution: TradingExecutionSettings = Field(default_factory=TradingExecutionSettings)


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

    def postgres_password_file(self) -> Path | None:
        value = self.storage.postgres.password_file
        if not value:
            return None
        configured = Path(value).expanduser()
        if configured.is_absolute():
            return configured
        return self._config_dir / configured

    def news_telegram_bot_token_file(self) -> Path | None:
        return self._configured_path(self.news.push.telegram_bot_token_file)

    def trading_binance_usdm_api_key_file(self) -> Path | None:
        return self._configured_path(self.trading.execution.credentials.api_key_file)

    def trading_binance_usdm_api_secret_file(self) -> Path | None:
        return self._configured_path(self.trading.execution.credentials.api_secret_file)

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


# Telegram admits about 20 messages a minute to one chat, so anything under three seconds is a rate
# this deployment cannot sustain; Feishu's custom bot admits about 100, which is what the 0.6 s
# default was chosen against. The number stays the operator's -- this is advice printed beside it,
# never a bound on it (#604 N3).
TELEGRAM_MIN_INTERVAL_ADVICE_SECONDS: Final = 3.0
PACING_WARNING_TELEGRAM_INTERVAL: Final = "news_item_push_telegram_interval_below_provider_rate"


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
    # Whether an outbound proxy is configured, and never which one: the URL may carry credentials.
    telegram_proxy_configured: bool
    # Advice, not a fault. `reason` says why delivery is unavailable; this says the configuration is
    # complete and will still be rate limited by the provider it names. Nothing reads it to decide.
    pacing_warning: str | None


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
    proxy_scheme = proxy_url_scheme(push.telegram_proxy_url)
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
    elif requested and provider == "telegram" and push.telegram_proxy_url is not None and proxy_scheme is None:
        # Named here rather than left to the sender: httpx refuses an unroutable proxy with an error
        # this process could not translate into one capability's fact, and a channel that silently
        # ignored the proxy an operator configured would be the worse answer of the two.
        reason = "news_item_push_telegram_proxy_invalid"
    elif requested and provider == "telegram" and proxy_scheme in SOCKS_PROXY_URL_SCHEMES and not _socks_supported():
        reason = "news_item_push_telegram_proxy_socks_unsupported"
    elif requested and not webhook_configured and provider != "telegram":
        reason = "news_item_push_feishu_webhook_missing"
    elif requested and provider == "feishu" and not is_feishu_webhook_url(push.feishu_webhook_url):
        reason = "news_item_push_feishu_webhook_invalid"
    pacing_warning = (
        PACING_WARNING_TELEGRAM_INTERVAL
        if provider == "telegram" and push.min_interval_seconds < TELEGRAM_MIN_INTERVAL_ADVICE_SECONDS
        else None
    )
    return NewsPushAvailability(
        requested=requested,
        delivery_available=requested and reason is None,
        reason=reason,
        provider=provider,
        pacing_warning=pacing_warning,
        feishu_webhook_url_configured=webhook_configured,
        feishu_signing_secret_configured=bool(push.feishu_signing_secret),
        telegram_bot_token_file_configured=token_file_configured,
        telegram_chat_id_configured=push.telegram_chat_id is not None,
        telegram_proxy_configured=push.telegram_proxy_url is not None,
    )


@dataclass(frozen=True, slots=True)
class NewsModelAvailability:
    """The News model routes a valid configuration describes, secret-free.

    Extraction and the generative judgments run on the `news_triage_model` endpoint (and its fallback);
    cards run on `news_reader_card`, or on the extraction endpoint when no dedicated one is configured.
    `news_judgment_model` is the optional Jev route; `None` means generative judgments.
    """

    extraction_model: str | None
    card_model: str | None
    card_dedicated: bool
    extraction_fallback_model: str | None = None
    card_fallback_model: str | None = None
    card_fallback_dedicated: bool = False
    news_judgment_model: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.extraction_model and self.card_model)


def news_model_availability(settings: Settings) -> NewsModelAvailability:
    direct = bool(settings.llm.api_key and _is_http_base_url(settings.llm.base_url))
    triage = direct and bool(settings.llm.news_triage_model)
    reader = settings.llm.news_reader_card
    reader_ok = triage and reader.configured and _is_http_base_url(reader.base_url)
    fallback = settings.llm.news_triage_fallback
    fallback_ok = triage and fallback.configured and _is_http_base_url(fallback.base_url)
    reader_fallback = settings.llm.news_reader_card_fallback
    reader_fallback_ok = fallback_ok and reader_fallback.configured and _is_http_base_url(reader_fallback.base_url)
    judgment = settings.llm.news_judgment
    return NewsModelAvailability(
        extraction_model=settings.llm.news_triage_model if triage else None,
        card_model=(
            reader.model if reader_ok else settings.llm.news_triage_model if triage and not reader.configured else None
        ),
        card_dedicated=bool(reader_ok),
        extraction_fallback_model=fallback.model if fallback_ok else None,
        card_fallback_model=(
            reader_fallback.model
            if reader_fallback_ok
            else fallback.model
            if fallback_ok and not reader_fallback.configured
            else None
        ),
        card_fallback_dedicated=bool(reader_fallback_ok),
        news_judgment_model=judgment.model if judgment.configured else None,
    )


def _socks_supported() -> bool:
    """Whether this build can speak SOCKS at all; httpx needs `socksio` and raises `ImportError` without it.

    An `ImportError` out of a sender constructor is what #562 §5 row 1 stopped happening: it is not a
    `ValueError`, so it escapes the composition seam and takes reception, triage and the market loop
    down with the process. One capability marked `unavailable` beside a running process is the answer.
    """

    return importlib.util.find_spec("socksio") is not None


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
