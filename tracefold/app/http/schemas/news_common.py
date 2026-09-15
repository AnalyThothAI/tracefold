from __future__ import annotations

from typing import Literal

from pydantic import Field

from tracefold.news import IPTCCodebookSha, MarketType, NewsTaxonomyV1, SourceAuthority, TradeRelevanceV1

from .common import ExactApiSchema


class NewsOutcomeData(ExactApiSchema):
    """One human-readable conclusion per Event; ``kind`` is a stable enum, the texts are Chinese reader copy."""

    kind: Literal[
        "held_recovery",
        "held_gate",
        "expired_triage_handoff",
        "expired_delivery_handoff",
        "queued_publish",
        "queued_triage",
        "dropped",
        "throttled",
        "degraded_dropped",
        "pending_delivery",
        "delivered",
        "delivery_failed",
    ]
    text_zh: str
    reason_zh: str = ""
    group: Literal["pushed", "held", "pending"]


class NewsTriageAssetData(ExactApiSchema):
    """One typed asset of a Triage verdict (#651 §6.2).

    ``market_type`` is the catalogue's instrument-class vocabulary, never a free string: the browser has
    to be able to tell `SEI` the token from `SEI` the listed insurer, and so does anything reading this
    API. Verdicts written before #651 carry `null` or a provider-tag word; the projection normalizes
    those to `unknown` rather than publishing a market nobody established.
    """

    symbol: str = Field(min_length=1, max_length=16)
    market_type: MarketType
    role: Literal["primary", "mentioned"]


class NewsTradeRelevanceData(TradeRelevanceV1):
    """The current typed market-relevance judgment; no free-form compatibility payload crosses HTTP."""


class NewsTaxonomyData(NewsTaxonomyV1):
    """The four model-owned classification axes and the codebook they were labelled against.

    Source authority left this object in #651: it is code-owned, computed from the evidence, and present
    on a judgment whose taxonomy call failed and which therefore has no taxonomy at all. It is published
    beside this one, on the editorial and the Triage summary.
    """

    taxonomy_version: Literal["news_taxonomy_v1"]
    codebook_sha256: IPTCCodebookSha
    subject_labels_zh: list[str] = Field(default_factory=list, max_length=3)
    event_family_zh: str
    change_state_zh: str
    assertion_status_zh: str


class NewsAssetRefData(ExactApiSchema):
    """One durable Event asset, resolved against the #75 instrument universe (#87/#287).

    The ledger contains Gate-grounded provider tags and deterministic-judge primaries. ``listed`` keeps a tag
    such as `SPOT` from looking like a real token; ``venue`` is preferred when the base trades on several and
    is ``None`` when the symbol names nothing in the instrument universe.
    """

    symbol: str
    base_symbol: str
    venue: str | None = None
    listed: bool = False


class NewsSymbolNormalizationData(ExactApiSchema):
    """#87: the several names one issuer trades under, collapsed to one stable storyline identity."""

    base_symbol: str
    aliases: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class NewsTriageSummaryData(ExactApiSchema):
    """The reader-facing view of one Triage verdict. Every `*_zh` is server-owned copy; the raw enum stays
    beside it so the browser can map it to a visual tone without owning a vocabulary table."""

    final_decision: Literal["push", "escalate", "drop", "throttled"]
    override_rule: str | None = None
    throttled_by: str | None = None
    degraded: bool = False
    error_code: str | None = None
    direction: str | None = None
    magnitude: int | None = None
    taxonomy: NewsTaxonomyData | None = None
    # `null` on a verdict with no editorial sibling at all -- a degraded, OI or liquidation judgment.
    # `unavailable` on a model judgment whose taxonomy Predictor failed while the other two answered:
    # the card is real, the classification is missing, and `taxonomy_error_code` says why (#651 §5.3).
    taxonomy_status: Literal["available", "unavailable"] | None = None
    taxonomy_error_code: str | None = None
    source_authority: SourceAuthority | None = None
    source_authority_zh: str = ""
    relevance: NewsTradeRelevanceData | None = None
    scope: str | None = None
    novelty: str | None = None
    audience: str | None = None
    confidence: float | None = None
    headline_zh: str | None = None
    why_zh: str | None = None
    assets: list[NewsTriageAssetData] = Field(default_factory=list)
    direction_zh: str = ""
    magnitude_zh: str = ""
    scope_zh: str = ""
    novelty_zh: str = ""
    audience_zh: str = ""
    decision_zh: str = ""


class NewsDeliverySummaryData(ExactApiSchema):
    state: str
    settled_at_ms: int | None = None
    error_code: str | None = None


__all__ = [
    "NewsAssetRefData",
    "NewsDeliverySummaryData",
    "NewsOutcomeData",
    "NewsSymbolNormalizationData",
    "NewsTaxonomyData",
    "NewsTradeRelevanceData",
    "NewsTriageAssetData",
    "NewsTriageSummaryData",
]
