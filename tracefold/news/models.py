"""News V3 domain models and pinned versions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .market_review.instruments import INSTRUMENT_CLASSES, InstrumentClass

NEWS_BUS_SCHEMA_VERSION = "news_bus_v1"
EVENT_IDENTITY_VERSION = "news_event_identity_v6"
# v12 (#706): a News card is one EventUpdate intent's frozen Chinese copy -- its headline and one line per
# selected claim -- plus code-owned facts: the selected claims' primary assets and quotes, the key marker
# and the change label (新增/更新/更正). No model direction, novelty or fact kind, and no progression review.
DELIVERY_CARD_VERSION = "news_delivery_card_v12"

# What the editorial Gate can decide about one Event.
Admission = Literal[
    "candidate",
    "listing_deterministic",
    "recovery",
]
# The admissions that go on to Triage. `listing_deterministic` is an admitted state, not a suppression: the funnel,
# the outcome vocabulary (the admitted "上币/下币公告" wording) and the re-gate set have always counted it as
# admitted, but the Deduper published only `candidate`, so every exchange listing/delisting frame died between
# the Gate and the queue (#72: 19 events, 0 verdicts, 0 deliveries since launch). One constant, so it cannot
# drift again.
ADMITTED_ADMISSIONS: Final[frozenset[str]] = frozenset({"candidate", "listing_deterministic"})
AssetClass = Literal["crypto", "equity_or_commodity", "macro", "none"]
EngineType = Literal["news", "meme", "listing", "market", "unknown"]
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
    market_data_state: ReaderMarketDataState = "ready"


class ExactNewsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TelegramDeliveryReceipt(ExactNewsModel):
    """Credential-free identity and lifecycle timestamps for one Telegram channel message."""

    provider: Literal["telegram"]
    message_id: int = Field(gt=0, strict=True)
    pushed_at_ms: int = Field(gt=0, strict=True)
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    edited_at_ms: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def validate_lifecycle_order(self) -> TelegramDeliveryReceipt:
        if self.edited_at_ms is not None and self.edited_at_ms < self.pushed_at_ms:
            raise ValueError("telegram_delivery_receipt_edit_before_push")
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


# The one market vocabulary News compares assets under (#651 §6.2). It is the instrument-class
# vocabulary `news_market_instruments` already stores, `unknown` included, because a model asset, a
# reviewer's gold asset, a catalogue candidate and a quote target all have to be the same kind of thing
# for "same instrument" to be one question with one answer.
MarketType = InstrumentClass
MARKET_TYPES: Final[frozenset[str]] = INSTRUMENT_CLASSES


def market_type_of(value: Any) -> MarketType:
    """The vocabulary value a stored or supplied market position carries, or ``unknown``.

    One normalizer, shared by the typed contracts and by every projection that reads verdict JSONB
    directly. Historical ``forex`` explicitly names ``fx``; ``fund`` does not establish the underlying
    market and remains ``unknown``. Other values outside the vocabulary — a free string, ``null``, a
    misspelling — are ``unknown``: the code does not know, and saying so is the only honest answer.
    """

    text = str(value or "").strip().lower()
    text = {"forex": "fx", "fund": "unknown"}.get(text, text)
    return cast(MarketType, text) if text in MARKET_TYPES else "unknown"


@dataclass(frozen=True, slots=True)
class MarketAsset:
    """A canonical symbol and its market, which together are one comparable instrument identity."""

    symbol: str
    market_type: MarketType = "unknown"

    @classmethod
    def of(cls, value: Any) -> MarketAsset:
        """One asset from a typed model, a stored JSONB object, or a bare symbol string."""

        if isinstance(value, Mapping):
            return cls(base_symbol(str(value.get("symbol") or "")), market_type_of(value.get("market_type")))
        symbol = getattr(value, "symbol", None)
        if symbol is not None:
            return cls(base_symbol(str(symbol)), market_type_of(getattr(value, "market_type", None)))
        return cls(base_symbol(str(value or "")), "unknown")

    @property
    def key(self) -> str:
        """The stable token two sides agree on: typed when the market is known, bare when it is not."""

        return self.symbol if self.market_type == "unknown" else f"{self.market_type}:{self.symbol}"


def same_market_asset(left: MarketAsset, right: MarketAsset) -> bool:
    """Whether two assets name one instrument, under the honest rule of #651 §6.2.

    Equal canonical symbols are necessary. Beyond that, two *known* and different markets are a real
    contradiction — `SEI/crypto` is not `SEI/equity` — and anything else cannot claim to be different:
    `unknown` is what every asset written before #651 carries, and treating it as a mismatch would
    silently drop the whole delivered history out of every overlap it is the evidence for.
    """

    if not left.symbol or left.symbol != right.symbol:
        return False
    return "unknown" in {left.market_type, right.market_type} or left.market_type == right.market_type


def base_symbol(symbol: str) -> str:
    """The canonical instrument identity used wherever two symbol sets are compared."""

    return str(symbol or "").upper().replace("XYZ-", "")


__all__ = [
    "ADMITTED_ADMISSIONS",
    "DELIVERY_CARD_VERSION",
    "EVENT_IDENTITY_VERSION",
    "MARKET_TYPES",
    "NEWS_BUS_SCHEMA_VERSION",
    "Admission",
    "AssetClass",
    "EngineType",
    "ExactNewsModel",
    "MarketAsset",
    "MarketType",
    "NewsFeedEntry",
    "ReaderDeliveryPresentation",
    "ReaderMarketMovement",
    "ReaderMarketState",
    "ReaderReceipt",
    "ReaderReceiptState",
    "ReaderTradeTarget",
    "base_symbol",
    "market_type_of",
    "same_market_asset",
]
