"""The two Predictor output shapes.

`EventSemantics` is what the interpreting Predictor must return; `ReaderCard` is what the writing one must,
together with the bounded fields each is shown and the envelope key its answer arrives under. Since #306
Phase 3 the output models are also what the provider is constrained by: `transport.response_format` builds
the request's `json_schema` from `model_json_schema()`, so the schema the code validates against and the
schema the model is handed cannot drift.

All four values are code, not artifact state, and `identity.compute_execution_identity` hashes the request
they compose. They live here rather than in `graph.py` so that identity can be computed without importing
the executor.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, Field, model_validator

from ..models import TriageAsset
from .contracts import TradeRelevanceV1
from .runtime import PredictorName, _ExactModel


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


# The bounded fields each Predictor is shown, in the fixed order the transport renders them.
PREDICTOR_INPUT_FIELDS: Final[dict[PredictorName, tuple[str, ...]]] = {
    "event_semantics": ("evidence_json",),
    "reader_card": ("evidence_json", "semantics_json"),
}

# The single envelope key each Predictor answers under, and the model that key is validated against.
PREDICTOR_OUTPUT: Final[dict[PredictorName, tuple[str, type[BaseModel]]]] = {
    "event_semantics": ("semantics", EventSemantics),
    "reader_card": ("card", ReaderCard),
}
