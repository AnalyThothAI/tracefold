"""The two Predictor output shapes, and their frozen signature identities.

`EventSemantics` is what the interpreting Predictor must return; `ReaderCard` is what the writing one
must. They sit below both `artifact.py` and `graph.py` because the artifact validates stored demos
against them while the graph validates live answers against them — a shared floor, not a layer.

These are Pydantic models, not `dspy.Signature` classes. The DSPy signature objects bound to them live
in `dspy_adapter.py`, which is the only module allowed to import DSPy.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, model_validator

from ..artifact_identity import canonical_sha
from ..models import TriageAsset
from .contracts import ReaderCardSemanticView, TradeRelevanceV1
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


EVENT_SEMANTICS_SIGNATURE_SHA256: Final[str] = canonical_sha(
    {
        "signature": "EventSemantics.v2",
        "inputs": {"evidence_json": "delimited canonical ModelVisibleSemanticsInput"},
        "outputs": {"semantics": EventSemantics.model_json_schema()},
    }
)

READER_CARD_SIGNATURE_SHA256: Final[str] = canonical_sha(
    {
        "signature": "ReaderCard.v2",
        "inputs": {
            "evidence_json": "delimited canonical ModelVisibleCardInput",
            "semantics_json": ReaderCardSemanticView.model_json_schema(),
        },
        "outputs": {"card": ReaderCard.model_json_schema()},
    }
)
