from __future__ import annotations

from typing import Literal

from pydantic import Field

from tracefold.news import IPTCCodebookSha, NewsTaxonomyV1, TradeRelevanceV1

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
    symbol: str = Field(min_length=1, max_length=16)
    market_type: str | None = Field(default=None, max_length=16)
    role: Literal["primary", "mentioned"]


class NewsTradeRelevanceData(TradeRelevanceV1):
    """The current typed market-relevance judgment; no free-form compatibility payload crosses HTTP."""


class NewsTaxonomyData(NewsTaxonomyV1):
    taxonomy_version: Literal["news_taxonomy_v1"]
    codebook_sha256: IPTCCodebookSha
    subject_labels_zh: list[str] = Field(default_factory=list, max_length=3)
    event_family_zh: str
    change_state_zh: str
    source_authority_zh: str
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
