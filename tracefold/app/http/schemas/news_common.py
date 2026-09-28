from __future__ import annotations

from typing import Literal

from pydantic import Field

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
        "notification_exhausted",
        "not_notified",
        "delivery_ambiguous",
    ]
    text_zh: str
    reason_zh: str = ""
    group: Literal["pushed", "held", "pending"]


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
