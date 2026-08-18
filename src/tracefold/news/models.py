"""News V3 domain models and pinned versions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NEWS_BUS_SCHEMA_VERSION = "news_bus_v1"
EVENT_IDENTITY_VERSION = "news_event_identity_v3"
GATE_POLICY_VERSION = "news_gate_v3"
STORYLINE_POLICY_VERSION = "news_storyline_v1"
TRIAGE_PROMPT_VERSION = "news_triage_prompt_v2"
TRIAGE_POLICY_VERSION = "news_triage_policy_v1"
ANALYST_PROMPT_VERSION = "news_analyst_prompt_v3"
ANALYST_POLICY_VERSION = "news_analyst_policy_v3"
DELIVERY_CARD_VERSION = "news_delivery_card_v5"

Admission = Literal[
    "candidate",
    "listing_deterministic",
    "suppressed_ungrounded",
    "suppressed_ungrounded_meme",
    "suppressed_meme_low",
    "suppressed_pr_template",
    "suppressed_low_signal",
    "recovery",
]
AssetClass = Literal["crypto", "equity_or_commodity", "macro", "none"]
EngineType = Literal["news", "meme", "listing", "market", "unknown"]
Decision = Literal["push", "escalate", "drop", "throttled"]


class ExactNewsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NewsFeedEntry(ExactNewsModel):
    """One canonical provider entry (kept from the OpenNews adapter contract)."""

    guid: str
    link: str | None = None
    title: str | None = None
    description: str = ""
    published_at_ms: int | None = None
    reporting_origin: str = ""


class TriageAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=16)
    market_type: str | None = Field(default=None, max_length=16)
    role: Literal["primary", "mentioned"]


class TriageVerdict(BaseModel):
    """Structured output of the Triage call. `decision` is the model's intent only."""

    model_config = ConfigDict(extra="forbid")

    event_type: Literal[
        "listing",
        "delisting",
        "filing",
        "regulation",
        "hack",
        "exploit",
        "partnership",
        "funding",
        "macro",
        "rates",
        "oi_spike",
        "liquidation",
        "whale",
        "earnings",
        "product",
        "rumor",
        "noise",
    ]
    assets: list[TriageAsset] = Field(default_factory=list, max_length=8)
    direction: Literal["bullish", "bearish", "neutral", "unclear"]
    scope: Literal["macro", "sector", "single_name"]
    magnitude: int = Field(ge=0, le=3)
    actionable: bool
    confidence: float = Field(ge=0.0, le=1.0)
    decision: Literal["push", "drop", "escalate"]
    headline_zh: str = Field(min_length=1, max_length=60)
    title_zh: str = Field(default="", max_length=160)
    rationale: str = Field(default="", max_length=160)


class AnalystVerdict(BaseModel):
    """Structured output of the Analyst deep agent."""

    model_config = ConfigDict(extra="forbid")

    agrees_with_triage: bool
    revised_direction: Literal["bullish", "bearish", "neutral", "unclear"]
    revised_magnitude: int = Field(ge=0, le=3)
    novelty_assessment: Literal["new", "followup", "rehash"]
    context_evidence: list[str] = Field(default_factory=list, max_length=8)
    thesis_zh: str = Field(min_length=1, max_length=800)
    risks_zh: str = Field(default="", max_length=400)
    follow_up_needed: bool
    confidence: float = Field(ge=0.0, le=1.0)


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
    "ANALYST_POLICY_VERSION",
    "ANALYST_PROMPT_VERSION",
    "DELIVERY_CARD_VERSION",
    "EVENT_IDENTITY_VERSION",
    "GATE_POLICY_VERSION",
    "NEWS_BUS_SCHEMA_VERSION",
    "STORYLINE_POLICY_VERSION",
    "TRIAGE_POLICY_VERSION",
    "TRIAGE_PROMPT_VERSION",
    "Admission",
    "AnalystVerdict",
    "AssetClass",
    "Decision",
    "EngineType",
    "ExactNewsModel",
    "NewsFeedEntry",
    "TriageAsset",
    "TriageVerdict",
    "json_ready",
]
