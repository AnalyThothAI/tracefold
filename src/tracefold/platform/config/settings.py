from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from tracefold.platform.paths import app_home, app_log_path, config_path

OPENNEWS_STRATEGY_ID_LIMIT = 32


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


class LlmFallbackConfig(BaseModel):
    """A second direct endpoint used only when the primary Triage call fails (issue #65); all-or-nothing."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: str | None = None
    base_url: str | None = None
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
    def require_complete_configuration(self) -> LlmFallbackConfig:
        configured = (self.api_key, self.base_url, self.model)
        if any(configured) and not all(configured):
            raise ValueError("llm_fallback_configuration_incomplete")
        return self

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: str | None = None
    base_url: str | None = None
    news_triage_model: str | None = None
    news_triage_fallback: LlmFallbackConfig = Field(default_factory=LlmFallbackConfig)

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

    deadline_seconds: float = 6.0
    concurrency: int = 4
    circuit_failures: int = 3
    circuit_open_seconds: float = 60.0

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsTriageSettings:
        if not 0.5 <= self.deadline_seconds <= 30:
            raise ValueError("news_triage_deadline_invalid")
        if not 1 <= self.concurrency <= 32:
            raise ValueError("news_triage_concurrency_invalid")
        return self


class NewsPolicySettings(BaseModel):
    """Operator-owned decide() thresholds and switches (see tracefold.news.DecidePolicy)."""

    model_config = ConfigDict(extra="forbid")

    escalate_magnitude: int = 3
    min_push_magnitude: int = 1
    min_watchlist_magnitude: int = 1
    unclear_push_min_magnitude: int = 2
    unclear_push_event_types: tuple[str, ...] = (
        "product",
        "listing",
        "delisting",
        "regulation",
        "hack",
        "exploit",
        "partnership",
        "filing",
    )
    restatement_drop: bool = True
    # This is duplicate evidence, not a reader quota. Zero disables the
    # deterministic similarity check.
    similarity_max: float = 0.25
    # #77: the Gate's AMQP priority no longer decides the ⚡ header. Set true to restore the pre-v4 behaviour.
    high_priority_escalates: bool = False

    @field_validator("unclear_push_event_types", mode="before")
    @classmethod
    def parse_event_types(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            raise ValueError("news_policy_event_types_invalid")
        return tuple(str(v).strip() for v in value if str(v).strip())

    @model_validator(mode="after")
    def validate_bounds(self) -> NewsPolicySettings:
        bounded = ("escalate_magnitude", "min_push_magnitude", "min_watchlist_magnitude", "unclear_push_min_magnitude")
        for name in bounded:
            if not 0 <= int(getattr(self, name)) <= 3:
                raise ValueError(f"news_policy_{name}_invalid")
        if not 0.0 <= float(self.similarity_max) <= 1.0:
            raise ValueError("news_policy_similarity_max_invalid")
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
    opennews_strategy_ids: tuple[str, ...] = ()
    broker: NewsBrokerSettings = Field(default_factory=NewsBrokerSettings)
    triage: NewsTriageSettings = Field(default_factory=NewsTriageSettings)
    push: NewsPushSettings = Field(default_factory=NewsPushSettings)
    policy: NewsPolicySettings = Field(default_factory=NewsPolicySettings)
    retention: NewsRetentionSettings = Field(default_factory=NewsRetentionSettings)
    gate: NewsGateSettings = Field(default_factory=NewsGateSettings)
    venues: NewsVenuesSettings = Field(default_factory=NewsVenuesSettings)
    watchlist: tuple[NewsWatchlistEntry, ...] = ()

    @field_validator("opennews_token", mode="before")
    @classmethod
    def parse_opennews_token(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("opennews_strategy_ids", mode="before")
    @classmethod
    def parse_opennews_strategy_ids(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            raise ValueError("opennews_strategy_ids_invalid")
        if len(value) > OPENNEWS_STRATEGY_ID_LIMIT:
            raise ValueError("opennews_strategy_ids_too_many")
        normalized: list[str] = []
        for raw in value:
            if not isinstance(raw, str):
                raise ValueError("opennews_strategy_id_invalid")
            strategy_id = raw.strip()
            if not strategy_id or "\x00" in strategy_id or len(strategy_id) > 128:
                raise ValueError("opennews_strategy_id_invalid")
            try:
                strategy_id.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("opennews_strategy_id_invalid") from exc
            normalized.append(strategy_id)
        if len(set(normalized)) != len(normalized):
            raise ValueError("opennews_strategy_ids_duplicate")
        return tuple(sorted(normalized))

    @field_validator("watchlist", mode="before")
    @classmethod
    def parse_watchlist(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            raise ValueError("news_watchlist_invalid")
        return tuple(value)

    @model_validator(mode="after")
    def validate_opennews_strategy_configuration(self) -> NewsSettings:
        if self.enabled and self.opennews_token and not self.opennews_strategy_ids:
            raise ValueError("opennews_strategy_ids_required")
        return self

    @property
    def watchlist_symbols(self) -> frozenset[str]:
        return frozenset(entry.symbol for entry in self.watchlist)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    _config_dir: Path = PrivateAttr(default_factory=app_home)

    ws_token: str | None = None
    api: ApiConfig = Field(default_factory=ApiConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    news: NewsSettings = Field(default_factory=NewsSettings)

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
    triage_fallback_model: str | None = None


def news_model_availability(settings: Settings) -> NewsModelAvailability:
    direct = bool(settings.llm.api_key and _is_http_base_url(settings.llm.base_url))
    triage = direct and bool(settings.llm.news_triage_model)
    fallback = settings.llm.news_triage_fallback
    fallback_ok = triage and fallback.configured and _is_http_base_url(fallback.base_url)
    return NewsModelAvailability(
        triage_configured=triage,
        triage_model=settings.llm.news_triage_model if triage else None,
        triage_fallback_model=fallback.model if fallback_ok else None,
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


def load_settings(*, require_ws_token: bool = True) -> Settings:
    path = config_path()
    if not path.exists():
        raise FileNotFoundError(f"config.yaml not found at {path}; run `tracefold init` first")
    data = _load_yaml_mapping(path)
    settings = Settings(**dict(data))
    settings.set_config_dir(path.parent)
    if require_ws_token and not settings.ws_token:
        raise ValueError("ws_token is required in config.yaml")
    return settings


def write_default_config(*, force: bool = False) -> Path:
    home = app_home()
    path = config_path(home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    for directory_name in ("logs", "cache"):
        directory = home / directory_name
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    if force or not path.exists():
        path.write_text(default_config_yaml(), encoding="utf-8")
    path.chmod(0o600)
    return path


def default_config_yaml() -> str:
    token = secrets.token_urlsafe(32)
    return f"""# Tracefold
ws_token: "{token}"

api:
  host: "0.0.0.0"
  port: 8765

storage:
  postgres:
    serve_dsn: "postgresql://tracefold_serve@postgres:5432/tracefold"
    workers_dsn: "postgresql://tracefold_workers@postgres:5432/tracefold"
    migrate_dsn: "postgresql://tracefold_migrate@postgres:5432/tracefold"
    serve_password_file: "postgres_serve_password"
    workers_password_file: "postgres_workers_password"
    migrate_password_file: "postgres_migrate_password"
    connect_timeout_seconds: 5

llm:
  api_key:
  base_url:
  news_triage_model:
  news_triage_fallback:
    api_key:
    base_url:
    model:

news:
  enabled: true
  opennews_token:
  opennews_strategy_ids: []
  broker:
    url: "amqp://tracefold:tracefold@rabbitmq:5672/"
    name_prefix: ""
  triage:
    deadline_seconds: 6.0
    concurrency: 4
  push:
    enabled: false
    feishu_webhook_url:
    feishu_signing_secret:
    min_interval_seconds: 0.6
  policy:
    escalate_magnitude: 3
    min_push_magnitude: 1
    min_watchlist_magnitude: 1
    unclear_push_min_magnitude: 2
    unclear_push_event_types: [product, listing, delisting, regulation, hack, exploit, partnership, filing]
    restatement_drop: true
    similarity_max: 0.25
    high_priority_escalates: false
  retention:
    raw_days: 30
    judged_days: 365
  gate:
    suppress_low_signal: false
  venues:
    enabled: true
    binance: true
    hyperliquid: true
    us_reference: true
    snapshot_period_hours: 6.0
  watchlist: []
"""


def _load_yaml_mapping(path: Path) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a mapping at {path}")
    return data
