"""News V3 domain models and pinned versions."""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

NEWS_BUS_SCHEMA_VERSION = "news_bus_v1"
EVENT_IDENTITY_VERSION = "news_event_identity_v4"
GATE_POLICY_VERSION = "news_gate_v4"
STORYLINE_POLICY_VERSION = "news_storyline_v3"
TRIAGE_PROMPT_VERSION = "news_triage_prompt_v8"
TRIAGE_POLICY_VERSION = "news_triage_policy_v6"
DELIVERY_CARD_VERSION = "news_delivery_card_v9"

Admission = Literal[
    "candidate",
    "listing_deterministic",
    "suppressed_pr_template",
    "suppressed_low_signal",
    "recovery",
]
# The admissions that go on to Triage. `listing_deterministic` is an admitted state, not a suppression: the funnel,
# the outcome vocabulary (the admitted "上币/下币公告" wording) and the re-gate set have always counted it as
# admitted, but the Deduper published only `candidate`, so every exchange listing/delisting frame died between
# the Gate and the queue (#72: 19 events, 0 verdicts, 0 deliveries since launch). One constant, so it cannot
# drift again.
ADMITTED_ADMISSIONS: Final[frozenset[str]] = frozenset({"candidate", "listing_deterministic"})
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
    """Structured output of the Triage call. `decision` is the model's intent only.

    ``novelty`` is judged against the told ledger in the status bar (cards the reader already received) and comes
    first in the schema on purpose: the model fills the tool call in property order, and a required field placed
    last was the one it dropped (issue #61 probe: 7/44 hard inputs omitted it). It stays *required* in the tool
    schema (no default); verdicts stored before v7 are replayed with ``novelty="new_fact"``. ``restates`` is an
    integer sentinel (-1 = none) rather than ``int | None`` because the anyOf/null shape raised the empty-tool-call
    rate.
    """

    model_config = ConfigDict(extra="forbid")

    novelty: Novelty = Field(
        description="REQUIRED. new_fact | progression | restatement, judged against <event_status>.told",
    )
    restates: int = Field(
        default=-1, ge=-1, description="index i of the told entry this event restates; -1 unless novelty=restatement"
    )
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
    audience: Audience = "none"
    headline_zh: str = Field(min_length=1, max_length=60)
    title_zh: str = Field(default="", max_length=160)
    why_zh: str = Field(default="", max_length=140)


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
    "STORYLINE_POLICY_VERSION",
    "TRIAGE_POLICY_VERSION",
    "TRIAGE_PROMPT_VERSION",
    "Admission",
    "AssetClass",
    "Audience",
    "Decision",
    "EngineType",
    "ExactNewsModel",
    "NewsFeedEntry",
    "Novelty",
    "TriageAsset",
    "TriageVerdict",
    "json_ready",
]
