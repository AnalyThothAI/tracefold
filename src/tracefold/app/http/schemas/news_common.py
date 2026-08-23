from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ExactApiSchema


class NewsOutcomeData(ExactApiSchema):
    """One human-readable conclusion per Event; ``kind`` is a stable enum, the texts are Chinese reader copy."""

    kind: Literal[
        "held_recovery",
        "held_gate",
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
    symbol: str
    role: str


class NewsAssetRefData(ExactApiSchema):
    """#87: one grounded coin tag, resolved against the #75 instrument universe.

    ``listed`` is the whole point: the provider tags `SPOT` on a Spot Gold headline and `NEAR` on the words
    "near-instant", and until now the console showed those exactly like a real token. ``venue`` is the preferred
    venue when the base trades on several, and is ``None`` when the tag names nothing.
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

    final_decision: str
    override_rule: str | None = None
    throttled_by: str | None = None
    degraded: bool = False
    error_code: str | None = None
    direction: str | None = None
    magnitude: int | None = None
    event_type: str | None = None
    scope: str | None = None
    novelty: str | None = None
    audience: str | None = None
    confidence: float | None = None
    actionable: bool | None = None
    model_decision: str | None = None
    headline_zh: str | None = None
    title_zh: str | None = None
    why_zh: str | None = None
    assets: list[NewsTriageAssetData] = Field(default_factory=list)
    direction_zh: str = ""
    magnitude_zh: str = ""
    event_type_zh: str = ""
    scope_zh: str = ""
    novelty_zh: str = ""
    audience_zh: str = ""
    decision_zh: str = ""
    model_decision_zh: str = ""


class NewsDeliverySummaryData(ExactApiSchema):
    state: str
    settled_at_ms: int | None = None
    error_code: str | None = None


__all__ = [
    "NewsAssetRefData",
    "NewsDeliverySummaryData",
    "NewsOutcomeData",
    "NewsSymbolNormalizationData",
    "NewsTriageAssetData",
    "NewsTriageSummaryData",
]
