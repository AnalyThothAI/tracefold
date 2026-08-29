"""The two native DSPy Signatures and their exact News output contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

import dspy  # type: ignore[import-untyped]
from pydantic import Field, PrivateAttr, model_validator

from ..models import TriageAsset
from ..taxonomy import ModelTaxonomyV1
from .contracts import TradeRelevanceV1
from .runtime import _ExactModel


class EventSemantics(_ExactModel):
    _raw_channels: tuple[str, ...] | None = PrivateAttr(default=None)
    _raw_affected_markets: tuple[str, ...] | None = PrivateAttr(default=None)

    novelty: Literal["new_fact", "progression", "restatement"]
    restates: int = Field(
        default=-1,
        ge=-1,
        description=(
            "Visible event_status.told index if and only if novelty is restatement; -1 for new_fact or progression."
        ),
    )
    assets: tuple[TriageAsset, ...] = Field(default=(), max_length=8)
    direction: Literal["bullish", "bearish", "neutral", "unclear"]
    scope: Literal["macro", "sector", "single_name"]
    magnitude: int = Field(ge=0, le=3)
    confidence: float = Field(ge=0.0, le=1.0)
    audience: Literal["crypto", "us_equity", "macro", "none"] = "none"
    taxonomy: ModelTaxonomyV1
    relevance: TradeRelevanceV1

    @model_validator(mode="wrap")
    @classmethod
    def _retain_pre_normalization_code_order(cls, value: Any, handler: Any) -> EventSemantics:
        semantics = cast(EventSemantics, handler(value))
        if not isinstance(value, Mapping) or not isinstance(value.get("relevance"), Mapping):
            return semantics
        relevance = value["relevance"]
        channels = relevance.get("channels")
        markets = relevance.get("affected_markets")
        if isinstance(channels, (list, tuple)) and all(isinstance(item, str) for item in channels):
            semantics._raw_channels = tuple(channels)
        if isinstance(markets, (list, tuple)) and all(isinstance(item, str) for item in markets):
            semantics._raw_affected_markets = tuple(markets)
        return semantics

    def raw_relevance_codes(self, field: Literal["channels", "affected_markets"]) -> tuple[str, ...] | None:
        return self._raw_channels if field == "channels" else self._raw_affected_markets


class ReaderCard(_ExactModel):
    headline_zh: str = Field(min_length=1, max_length=60)
    why_zh: str = Field(default="", max_length=140)

    @model_validator(mode="after")
    def _headline_has_content(self) -> ReaderCard:
        if not self.headline_zh.strip():
            raise ValueError("news_program_reader_headline_empty")
        return self


class EventSemanticsSignature(dspy.Signature):  # type: ignore[misc]
    """Interpret one bounded Event against the selected reader-history ledger."""

    evidence_json: str = dspy.InputField(
        desc="Canonical bounded Event, gate, and event_status JSON inside Tracefold's untrusted-data delimiters."
    )
    semantics: EventSemantics = dspy.OutputField(desc="The exact typed semantic interpretation of this Event.")


class ReaderCardSignature(dspy.Signature):  # type: ignore[misc]
    """Write factual reader copy from bounded Event evidence and accepted semantics."""

    evidence_json: str = dspy.InputField(
        desc="Canonical bounded Event and gate JSON inside Tracefold's untrusted-data delimiters; no told ledger."
    )
    semantics_json: str = dspy.InputField(desc="Canonical ReaderCardSemanticView JSON from EventSemantics.")
    card: ReaderCard = dspy.OutputField(desc="The exact typed Chinese reader card.")
