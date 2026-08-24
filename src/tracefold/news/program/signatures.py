"""The two Predictor output shapes.

`EventSemantics` is what the interpreting Predictor must return; `ReaderCard` is what the writing one must.
They are code-owned schemas, versioned by `factory_id` along with the rest of the graph, and the DSPy
signature objects bound to them live in `dspy_adapter.py` — the only module allowed to import DSPy.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ..models import TriageAsset
from .contracts import TradeRelevanceV1
from .runtime import _ExactModel


class EventSemantics(_ExactModel):
    novelty: Literal["new_fact", "progression", "restatement"]
    restates: int = Field(
        default=-1,
        ge=-1,
        description=(
            "Visible event_status.told index if and only if novelty is restatement; -1 for new_fact or progression."
        ),
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
    assets: tuple[TriageAsset, ...] = Field(default=(), max_length=8)
    direction: Literal["bullish", "bearish", "neutral", "unclear"]
    scope: Literal["macro", "sector", "single_name"]
    magnitude: int = Field(ge=0, le=3)
    confidence: float = Field(ge=0.0, le=1.0)
    audience: Literal["crypto", "us_equity", "macro", "none"] = "none"
    relevance: TradeRelevanceV1


class ReaderCard(_ExactModel):
    headline_zh: str = Field(min_length=1, max_length=60)
    why_zh: str = Field(default="", max_length=140)

    @model_validator(mode="after")
    def _headline_has_content(self) -> ReaderCard:
        if not self.headline_zh.strip():
            raise ValueError("news_program_reader_headline_empty")
        return self
