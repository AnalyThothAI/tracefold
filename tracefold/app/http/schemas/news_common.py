from __future__ import annotations

from typing import Literal

from pydantic import Field

from tracefold.news import FactKind, MarketType, SourceAuthority

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
        # #706: the EventUpdate path's own conclusions.
        "queued_semantic",
        "semantic_failed",
        "no_update",
        "queued_notification",
        "notification_deferred",
        "not_notified",
        "delivery_ambiguous",
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


class NewsLegacyVerdictData(ExactApiSchema):
    """The reader-facing view of one legacy Triage verdict: history only since #706.

    `news_verdicts` receives no writes. An Event the News Agent processed has no verdict and therefore no
    summary; its reading is `event_update`. Every `*_zh` is server-owned copy; the raw enum stays beside it
    so the browser can map it to a visual tone without owning a vocabulary table. The retired taxonomy axes
    are not summarized here -- the verdict rows keep their stored values for audit.
    """

    final_decision: Literal["push", "escalate", "drop", "throttled"]
    override_rule: str | None = None
    throttled_by: str | None = None
    degraded: bool = False
    error_code: str | None = None
    direction: str | None = None
    fact_kind: FactKind | None = None
    # `null` on a verdict with no editorial sibling at all -- a degraded, OI or liquidation judgment.
    source_authority: SourceAuthority | None = None
    source_authority_zh: str = ""
    scope: str | None = None
    novelty: str | None = None
    evidence_ref: str | None = None
    confidence: float | None = None
    headline_zh: str | None = None
    why_zh: str | None = None
    assets: list[NewsTriageAssetData] = Field(default_factory=list)
    direction_zh: str = ""
    fact_kind_zh: str = ""
    scope_zh: str = ""
    novelty_zh: str = ""
    decision_zh: str = ""


class NewsDeliverySummaryData(ExactApiSchema):
    state: str
    settled_at_ms: int | None = None
    error_code: str | None = None


__all__ = [
    "NewsAssetRefData",
    "NewsDeliverySummaryData",
    "NewsLegacyVerdictData",
    "NewsOutcomeData",
    "NewsSymbolNormalizationData",
    "NewsTriageAssetData",
]
