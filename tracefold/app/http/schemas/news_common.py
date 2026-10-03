from __future__ import annotations

from typing import Literal

from pydantic import Field

from tracefold.news.models import MarketType

from .common import ExactApiSchema


class NewsOutcomeData(ExactApiSchema):
    """One human-readable conclusion per Event; ``kind`` is a stable enum, the texts are Chinese reader copy."""

    kind: Literal[
        "held_recovery",
        "held_gate",
        "pending_delivery",
        "delivered",
        "delivery_failed",
        # #706: the EventUpdate path's own conclusions.
        "queued_semantic",
        "semantic_failed",
        "no_update",
        "queued_notification",
        "notification_deferred",
        "notification_failed",
        "not_notified",
        "delivery_ambiguous",
        "duplicate",
    ]
    text_zh: str
    reason_zh: str = ""
    reason_at_ms: int | None = None
    reason_before_time_zh: str = ""
    reason_after_time_zh: str = ""
    group: Literal["pushed", "held", "pending"]


class NewsAssetRefData(ExactApiSchema):
    """A current primary asset, or a source asset before adoption, resolved in its stated market."""

    symbol: str
    market_type: MarketType
    base_symbol: str
    venue: str | None = None
    venue_symbol: str | None = None
    listed: bool = False
    resolution_state: Literal["resolved", "reference_only", "unresolved_market", "unlisted"]


class NewsSymbolNormalizationData(ExactApiSchema):
    """#87: the several names one issuer trades under, collapsed to one stable storyline identity."""

    base_symbol: str
    aliases: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class NewsDeliverySummaryData(ExactApiSchema):
    state: str
    settled_at_ms: int | None = None
    error_code: str | None = None
    content_revision: str | None = None
    payload_sha256: str | None = None


__all__ = [
    "NewsAssetRefData",
    "NewsDeliverySummaryData",
    "NewsOutcomeData",
    "NewsSymbolNormalizationData",
]
