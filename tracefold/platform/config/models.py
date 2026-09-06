from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.platform.paths import app_home, app_log_path

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
    request: LlmRequestConfig = Field(default_factory=LlmRequestConfig)
    news_reader_card: LlmReaderCardConfig = Field(default_factory=LlmReaderCardConfig)
    news_triage_fallback: LlmFallbackConfig = Field(default_factory=LlmFallbackConfig)
    news_reader_card_fallback: LlmReaderCardFallbackConfig = Field(default_factory=LlmReaderCardFallbackConfig)
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


class NewsPolicySettings(BaseModel):
    """The six operator-owned v12 duplicate/safety/budget knobs used by ``decide()``."""

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
    # #504 D2: at most `storyline_budget_max` delivered cards per final storyline key inside
    # `storyline_budget_window_s`, exempting a corroborated escalate, a direction reversal and the `none`
    # key (#509). Either at 0 disables the budget. A content rule per storyline, not a reader quota.
    storyline_budget_window_s: int = 3600
    storyline_budget_max: int = 2

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsPolicySettings:
        if not 0.0 <= float(self.similarity_max) <= 1.0:
            raise ValueError("news_policy_similarity_max_invalid")
        if int(self.stale_source_max_age_s) < 0:
            raise ValueError("news_policy_stale_source_max_age_s_invalid")
        if int(self.storyline_budget_window_s) < 0:
            raise ValueError("news_policy_storyline_budget_window_s_invalid")
        if int(self.storyline_budget_max) < 0:
            raise ValueError("news_policy_storyline_budget_max_invalid")
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
    """Which wallets the chain tape follows (#572 decision 5).

    Four numbers and no model. Win rate is deliberately absent: over the addresses with five or more
    closes its rank correlation with realized P&L was 0.31, and four of the nine with a win rate above
    0.6 were losing money (#572 §3.2).
    """

    model_config = ConfigDict(extra="forbid")

    min_closed_trades: int = 10
    min_profit_factor: float = 1.2
    top_quality: int = 20
    top_whale_by_open_cost: int = 20

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsChainTapeRosterSettings:
        if not 0 <= self.min_closed_trades <= 10_000:
            raise ValueError("news_chain_tape_roster_min_closed_trades_invalid")
        if not 0.0 <= self.min_profit_factor <= 1_000.0:
            raise ValueError("news_chain_tape_roster_min_profit_factor_invalid")
        # The roster is a topic array on every `eth_getLogs` call. A list that is not bounded here is a
        # request size that is not bounded on a public endpoint.
        if not 0 <= self.top_quality <= 200:
            raise ValueError("news_chain_tape_roster_top_quality_invalid")
        if not 0 <= self.top_whale_by_open_cost <= 200:
            raise ValueError("news_chain_tape_roster_top_whale_invalid")
        if self.top_quality + self.top_whale_by_open_cost <= 0:
            raise ValueError("news_chain_tape_roster_empty")
        return self


class NewsChainTapeRulesSettings(BaseModel):
    """When a followed wallet's movement becomes a card: #572 §6.4's medium tier, as runtime values.

    The numbers were computed from the provider's own seven-day close ledger and project about 25-40
    cards a day, which is the same order as the 50-60 key News cards a reader already gets. They are
    thresholds an operator moves, never a contract: #572 §11 asks for the receipt that says whether the
    projection held, and the receipt is the point of shipping them rather than guessing again.
    """

    model_config = ConfigDict(extra="forbid")

    exit_ratio_bps: int = 3000
    exit_min_position_usd: float = 20_000.0
    exit_cascade_window_s: int = 7200
    exit_cascade_min_usd: float = 5_000.0
    crowding_n: int = 3
    crowding_window_s: int = 900
    crowding_min_usd: float = 1_000.0
    crowding_premium_late_bps: int = 3000
    trigger_max_age_s: int = 600

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsChainTapeRulesSettings:
        if not 0 <= self.exit_ratio_bps < 10_000:
            # Ten thousand would be "sold more than everything", which no sell can clear.
            raise ValueError("news_chain_tape_rules_exit_ratio_invalid")
        if not 0.0 <= self.exit_min_position_usd <= 1e12 or not 0.0 <= self.exit_cascade_min_usd <= 1e12:
            raise ValueError("news_chain_tape_rules_exit_size_invalid")
        if not 0 <= self.exit_cascade_window_s <= 86_400:
            raise ValueError("news_chain_tape_rules_exit_cascade_window_invalid")
        if not 2 <= self.crowding_n <= 40:
            # One wallet is not a crowd, and the roster itself is capped at 40 addresses.
            raise ValueError("news_chain_tape_rules_crowding_n_invalid")
        if not 60 <= self.crowding_window_s <= 86_400:
            raise ValueError("news_chain_tape_rules_crowding_window_invalid")
        if not 0.0 <= self.crowding_min_usd <= 1e12:
            raise ValueError("news_chain_tape_rules_crowding_size_invalid")
        if not 0 <= self.crowding_premium_late_bps <= 1_000_000:
            raise ValueError("news_chain_tape_rules_crowding_premium_invalid")
        # A fill older than this on the host's own receive clock is history, and the 24-hour backfill is
        # exactly that. Zero would mean nothing ever fires; a day would mean the backfill fires.
        if not 1 <= self.trigger_max_age_s <= 3600:
            raise ValueError("news_chain_tape_rules_trigger_age_invalid")
        return self


class NewsChainTapeSettings(BaseModel):
    """The Robinhood Chain wallet tape (#572 PR-1): read-only, disabled by default, store-only.

    Every value here is a runtime parameter, and none of them is a secret: both providers are public and
    unauthenticated, which is why `tracefold config` prints them as they are.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    poll_interval_s: float = 2.0
    roster_provider_url: str = "https://robinhoodtrenches.com"
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


class TradingCandidateSettings(BaseModel):
    """What may be admitted to the Signal lane at all.

    Every bound here is a universe or timing filter, never sizing and never Alpha. The policy's own
    thresholds are code-owned and frozen onto each Case, so an operator cannot move an Alpha rule
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
    """The Runtime-owned risk gap policy, as operator-owned numbers (#510 E).

    Every value here used to be a literal in `tracefold/app/nautilus/root.py`, which meant the
    `config_sha256` activation fence -- the thing that refuses to reuse a profile whose configuration
    moved -- could not see a risk change at all. They are in the profile digest now, so editing one
    requires a new profile id and a fresh activation, exactly like changing the mode or the account
    slot. None of them is a secret and `tracefold config` prints all of them.

    The stop distance stays a Runtime number: the Nautilus Strategy places and replaces the stop, and
    neither the Case nor the Signal ever carries it.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    risk_fraction_per_trade: Decimal = Decimal("0.01")
    max_risk_per_trade_usd: Decimal = Decimal("10")
    max_total_risk_usd: Decimal = Decimal("25")
    max_positions: int = 1
    max_leverage: int = 1
    max_daily_loss_usd: Decimal = Decimal("25")
    stop_distance_bps: int = 100
    reconciliation_interval_seconds: float = 5.0
    market_stale_after_seconds: float = 5.0

    @model_validator(mode="after")
    def validate_bounds(self) -> TradingExecutionRiskSettings:
        # A single stop-out has to stay a fraction of the day: above five percent of equity one stop
        # is the day, and `max_daily_loss_usd` stops being a limit and becomes a description.
        if not Decimal("0") < self.risk_fraction_per_trade <= Decimal("0.05"):
            raise ValueError("trading_execution_risk_fraction_invalid")
        # Below one dollar of risk every candidate rounds to zero size at the venue's increment, so
        # the Runtime would refuse every Signal with `quantity_below_increment` instead of trading.
        if self.max_risk_per_trade_usd < 1 or self.max_total_risk_usd < 1:
            raise ValueError("trading_execution_risk_limit_invalid")
        # One trade's budget is drawn from the aggregate budget; it cannot exceed it.
        if self.max_risk_per_trade_usd > self.max_total_risk_usd:
            raise ValueError("trading_execution_risk_limit_invalid")
        # This deployment is one operator's single Binance USD-M slot. Ten thousand dollars of
        # simultaneous stop distance is larger than the account the fence exists to protect.
        if self.max_total_risk_usd > 10_000:
            raise ValueError("trading_execution_risk_limit_invalid")
        # Each concurrent position is one more stop this Runtime must keep proven inside one
        # reconciliation period; ten is the most a single private scan can re-prove in that budget.
        if not 1 <= self.max_positions <= 10:
            raise ValueError("trading_execution_max_positions_invalid")
        # Sizing is fixed-risk and only clamps notional to `equity * leverage`, so leverage is a
        # notional ceiling, not a risk input. Twenty keeps a stop-out from reaching liquidation.
        if not 1 <= self.max_leverage <= 20:
            raise ValueError("trading_execution_max_leverage_invalid")
        # A day cannot be allowed to end before its first trade: the day limit is a halt for the
        # whole UTC day, and one trade's risk is the smallest thing it can be asked to survive.
        if self.max_daily_loss_usd < self.max_risk_per_trade_usd or self.max_daily_loss_usd > 10_000:
            raise ValueError("trading_execution_daily_loss_invalid")
        # The same bound `OiInstrumentRoute` enforces: a stop inside one basis point is inside the
        # spread, and one at half the mark is not a stop.
        if not 1 <= self.stop_distance_bps <= 5_000:
            raise ValueError("trading_execution_stop_distance_invalid")
        # `OiRiskLimits` derives the account (2x) and reconciliation (3x) staleness budgets from this
        # one period. Under a second the private REST scan spends the Binance weight budget on itself;
        # over a minute an entry is judged against an account picture up to three minutes old.
        if not 1.0 <= self.reconciliation_interval_seconds <= 60.0:
            raise ValueError("trading_execution_reconciliation_interval_invalid")
        # Quote freshness is a stream fact, not a scan fact, so it is its own number; the same one
        # second floor and one minute ceiling apply for the same reason.
        if not 1.0 <= self.market_stale_after_seconds <= 60.0:
            raise ValueError("trading_execution_market_stale_invalid")
        return self


class TradingExecutionSettings(BaseModel):
    """The one Binance USD-M account slot this deployment executes for.

    `account_slot` plus `mode` is the whole execution identity (#520). There is no separate
    `profile_id`: it existed only to fence a Runtime whose release or config digest had moved, and
    that fence refused 58 restarts in one day without ever refusing a real risk.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    mode: Literal["disabled", "paper", "live"] = "disabled"
    account_slot: str = "binance_usdm_primary"
    credentials: TradingExecutionCredentialsSettings = Field(default_factory=TradingExecutionCredentialsSettings)
    risk: TradingExecutionRiskSettings = Field(default_factory=TradingExecutionRiskSettings)

    @field_validator("account_slot")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}", value) is None:
            raise ValueError("trading_execution_identity_invalid")
        return value


class TradingSettings(BaseModel):
    """Alpha producer plus one cold Binance USD-M Runtime profile (#433)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    candidates: TradingCandidateSettings = Field(default_factory=TradingCandidateSettings)
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
