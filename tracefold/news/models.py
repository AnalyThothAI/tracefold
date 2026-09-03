"""News V3 domain models and pinned versions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NEWS_BUS_SCHEMA_VERSION = "news_bus_v1"
EVENT_IDENTITY_VERSION = "news_event_identity_v6"
GATE_POLICY_VERSION = "news_gate_v6"
# v10 (#160) removes queue/provider hints from editorial authority and chooses
# reader actions from the typed trade-relevance contract plus objective guards.
TRIAGE_POLICY_VERSION = "news_triage_policy_v12"
DELIVERY_CARD_VERSION = "news_delivery_card_v11"

Admission = Literal[
    "candidate",
    "listing_deterministic",
    "telemetry_deterministic",
    "liquidation_deterministic",
    "suppressed_pr_template",
    "suppressed_low_signal",
    "unsupported_market_contract",
    "recovery",
]
# The admissions that go on to Triage. `listing_deterministic` is an admitted state, not a suppression: the funnel,
# the outcome vocabulary (the admitted "上币/下币公告" wording) and the re-gate set have always counted it as
# admitted, but the Deduper published only `candidate`, so every exchange listing/delisting frame died between
# the Gate and the queue (#72: 19 events, 0 verdicts, 0 deliveries since launch). One constant, so it cannot
# drift again.
# Admitted means "goes to Triage". `telemetry_deterministic` earns its place there the same way
# `listing_deterministic` does: the frame is judged, just not by a model (#137).
ADMITTED_ADMISSIONS: Final[frozenset[str]] = frozenset(
    {"candidate", "listing_deterministic", "telemetry_deterministic", "liquidation_deterministic"}
)
# How long the Janitor keeps trying to rescue an Event that was created but never reached the Triage queue
# (commit-then-crash, or a publish failure). Measured event -> delivery latency is p50 4.2 s / p95 16.8 s, so this
# is ~100x the p95: it can only fire on a genuinely stranded Event, never on a slow one. Past it the Event is not
# republished — a card the reader would receive half an hour late is worse than no card (#76: one catch-up sent a
# 30.6 h old exchange notice). Code-owned, not policy: it is a relevance floor, not a tuning knob.
OUTBOX_MAX_AGE_MS: Final[int] = 30 * 60_000
Audience = Literal["crypto", "us_equity", "macro", "none"]
AssetClass = Literal["crypto", "equity_or_commodity", "macro", "none"]
EngineType = Literal["news", "meme", "listing", "market", "unknown"]
Decision = Literal["push", "escalate", "drop", "throttled"]
Novelty = Literal["new_fact", "progression", "restatement"]
ReaderReceiptState = Literal["received", "not_received", "unknown"]


@dataclass(frozen=True, slots=True)
class ReaderTradeTarget:
    """One exact venue contract that a delivery adapter may expose as a reader action."""

    ticker: str
    venue: Literal[
        "binance.perp",
        "binance.spot",
        "hl.perp",
        "hl.spot",
        "hl.builder",
        "okx.perp",
        "okx.spot",
        "lighter.perp",
        "lighter.spot",
        "bitget.perp",
        "bitget.spot",
    ]
    venue_symbol: str
    base_symbol: str
    quote_asset: str


ReaderMarketState = Literal["not_due", "pending", "available", "unavailable"]
ReaderMarketDataState = Literal["pending", "ready"]
ReaderMarketScope = Literal["macro", "sector", "single_name"]
ProgressionReviewState = Literal["pending", "confirmed", "rejected", "unavailable"]


@dataclass(frozen=True, slots=True)
class ReaderMarketMovement:
    """Reader-facing news-to-push and trailing-one-hour returns for one displayed ticker."""

    ticker: str
    after_news_bps: int | None
    return_1h_bps: int | None
    change_24h_bps: int | None
    one_hour_state: ReaderMarketState


@dataclass(frozen=True, slots=True)
class ReaderDeliveryPresentation:
    """Ephemeral adapter-only context; never part of the persisted reader card."""

    trade_targets: tuple[ReaderTradeTarget, ...] = ()
    market_movements: tuple[ReaderMarketMovement, ...] = ()
    news_at_ms: int | None = None
    observed_at_ms: int | None = None
    market_data_state: ReaderMarketDataState = "ready"
    market_scope: ReaderMarketScope | None = None
    novelty: Novelty | None = None
    progression_from_headline: str | None = None
    progression_review_state: ProgressionReviewState | None = None
    progression_review_reason: str | None = None
    progression_review_parent_age_minutes: int | None = None
    progression_review_parent_message_id: int | None = None


class ExactNewsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TelegramDeliveryReceipt(ExactNewsModel):
    """Credential-free identity and lifecycle timestamps for one Telegram channel message."""

    provider: Literal["telegram"]
    message_id: int = Field(gt=0, strict=True)
    pushed_at_ms: int = Field(gt=0, strict=True)
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    edited_at_ms: int | None = Field(default=None, gt=0, strict=True)
    deleted_at_ms: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def validate_lifecycle_order(self) -> TelegramDeliveryReceipt:
        if self.edited_at_ms is not None and self.edited_at_ms < self.pushed_at_ms:
            raise ValueError("telegram_delivery_receipt_edit_before_push")
        if self.deleted_at_ms is not None and self.deleted_at_ms < (self.edited_at_ms or self.pushed_at_ms):
            raise ValueError("telegram_delivery_receipt_delete_before_last_mutation")
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class NewsFeedEntry(ExactNewsModel):
    """One canonical provider entry (kept from the OpenNews adapter contract)."""

    guid: str
    link: str | None = None
    title: str | None = None
    description: str = ""
    published_at_ms: int | None = None
    reporting_origin: str = ""


class ReaderReceipt(ExactNewsModel):
    """Delivery truth used by semantic memory and learning.

    Only a settled first delivery in state ``sent`` is known to have reached
    the reader.  A crash after the external send is explicitly unknown; every
    other shape is not received.  This value object intentionally does not
    treat a policy decision or a reservation as delivery evidence.
    """

    state: ReaderReceiptState
    delivery_state: str | None = None
    error_code: str | None = None
    received_at_ms: int | None = None
    rendered_card: dict[str, Any] | None = None

    @classmethod
    def from_delivery(cls, delivery: Mapping[str, Any] | None) -> ReaderReceipt:
        if delivery is None:
            return cls(state="not_received")
        delivery_state = str(delivery.get("state") or "") or None
        error_code = str(delivery.get("error_code") or "") or None
        # A card intentionally removed after authoritative tradability review is not part of the durable reader
        # history. Keeping it as "received" would let an untradeable, deleted issuer suppress a future listing.
        if delivery.get("delete_state") == "deleted":
            return cls(state="not_received", delivery_state=delivery_state, error_code=error_code)
        if delivery_state == "sent":
            return cls(
                state="received",
                delivery_state=delivery_state,
                error_code=error_code,
                received_at_ms=int(delivery["settled_at_ms"]) if delivery.get("settled_at_ms") is not None else None,
                rendered_card=dict(delivery.get("card") or {}),
            )
        if error_code == "ambiguous_after_crash":
            return cls(
                state="unknown",
                delivery_state=delivery_state,
                error_code=error_code,
                rendered_card=dict(delivery.get("card") or {}),
            )
        return cls(state="not_received", delivery_state=delivery_state, error_code=error_code)


class TriageAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=16)
    market_type: str | None = Field(default=None, max_length=16)
    role: Literal["primary", "mentioned"]


class TriageVerdict(BaseModel):
    """Current shared semantic and reader-copy atom.

    ``novelty`` is judged against the told ledger in the status bar (cards the reader already received) and comes
    first in the schema on purpose: the model fills the tool call in property order, and a required field placed
    last was the one it dropped (issue #61 probe: 7/44 hard inputs omitted it). ``restates`` is an integer sentinel
    (-1 = none) rather than ``int | None`` because the anyOf/null shape raised the empty-tool-call rate. Semantic
    taxonomy lives only in ``EditorialEnvelope.taxonomy`` and every action lives only in ``DecisionResult``.
    """

    model_config = ConfigDict(extra="forbid")

    novelty: Novelty = Field(
        description="REQUIRED. new_fact | progression | restatement, judged against <event_status>.told",
    )
    restates: int = Field(
        default=-1, ge=-1, description="index i of the told entry this event restates; -1 unless novelty=restatement"
    )
    assets: list[TriageAsset] = Field(default_factory=list, max_length=8)
    direction: Literal["bullish", "bearish", "neutral", "unclear"]
    scope: Literal["macro", "sector", "single_name"]
    magnitude: int = Field(ge=0, le=3)
    confidence: float = Field(ge=0.0, le=1.0)
    audience: Audience = "none"
    headline_zh: str = Field(min_length=1, max_length=60)
    why_zh: str = Field(default="", max_length=140)


def base_symbol(symbol: str) -> str:
    """The canonical instrument identity used wherever two symbol sets are compared.

    One definition, shared by ``decide()`` and the told-context selector, so retrieval and policy can never
    disagree about whether two cards are about the same instrument.
    """

    return str(symbol or "").upper().replace("XYZ-", "")


def json_ready(value: Any) -> Any:
    """Return a JSON-serializable copy of pydantic/dataclass-free structures."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_ready(v) for v in value]
    return value


__all__ = [
    "ADMITTED_ADMISSIONS",
    "DELIVERY_CARD_VERSION",
    "EVENT_IDENTITY_VERSION",
    "GATE_POLICY_VERSION",
    "NEWS_BUS_SCHEMA_VERSION",
    "OUTBOX_MAX_AGE_MS",
    "TRIAGE_POLICY_VERSION",
    "Admission",
    "AssetClass",
    "Audience",
    "Decision",
    "EngineType",
    "ExactNewsModel",
    "NewsFeedEntry",
    "Novelty",
    "ReaderDeliveryPresentation",
    "ReaderMarketMovement",
    "ReaderMarketState",
    "ReaderReceipt",
    "ReaderReceiptState",
    "ReaderTradeTarget",
    "TriageAsset",
    "TriageVerdict",
    "base_symbol",
    "json_ready",
]
