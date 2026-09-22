"""The three native DSPy Signatures and their exact News output contracts."""

from __future__ import annotations

import unicodedata
from typing import Literal

import dspy  # type: ignore[import-untyped]
from pydantic import Field, model_validator

from ..models import FactKind, TriageAsset
from ..taxonomy import ModelTaxonomyV1
from .runtime import _ExactModel


class EventSemantics(_ExactModel):
    """What the model observed in one bounded Event, and nothing about what the reader should get.

    #675 §1 deleted the other half. `TradeRelevanceV1`'s seven codes and `magnitude` were the model
    answering a policy question -- seven days of 8950 judgments collapsed them into one bit, and the
    seed had to teach a threshold ("at least 5% on the day") to make them answerable at all. Both the
    threshold and the answer belong in `triage_rules.decide()`, where they can be replayed, versioned
    and argued with. What survives is what a reader of the text can check: which instruments it names,
    whether it is new against the ledger it was shown, which way it reads, how wide its surface is, and
    `fact_kind` -- what kind of new thing the text states.
    """

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
    fact_kind: FactKind = Field(
        description=(
            "REQUIRED. What kind of new thing this text states, read off the text itself: state_change | "
            "new_quantity | level_crossed | period_record | quantified_flow | official_measure | statement | "
            "recap | schedule | promotion."
        )
    )
    evidence_ref: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "REQUIRED. The ref_id of the current_evidence or related_evidence span that states the fact_kind."
        ),
    )
    confidence: float = Field(ge=0.0, le=1.0)


# #522 D4: the 9 h receipt after the #504 deploy shipped three cards whose `why_zh` was empty and one
# whose whole value was ":", and two whose headline was the untranslated English original. `max_length`
# and a non-empty headline were the only checks, so a card the reader cannot use passed the contract and
# reached the push. These two predicates are the difference between "the field is present" and "the field
# says something in Chinese"; a failure goes through the JSON adapter's existing one format retry.
def _carries_substance(value: str) -> bool:
    """True when something survives stripping whitespace, punctuation, symbols and control characters.

    Category-based rather than a character list because the copy is Chinese: an ASCII colon and its
    full-width form are both `Po`, and a hand-written blacklist would have to enumerate every full-width
    variant to say the same thing.
    """

    return any(unicodedata.category(character)[0] not in {"C", "P", "S", "Z"} for character in value)


def _carries_han(value: str) -> bool:
    """True when at least one character is Han: the reader card is Chinese copy, not a passthrough."""

    return any(
        unicodedata.category(character) == "Lo" and unicodedata.name(character, "").startswith("CJK")
        for character in value
    )


class ReaderCard(_ExactModel):
    source_refs: tuple[str, ...] = Field(default=(), max_length=12)
    headline_zh: str = Field(min_length=1, max_length=60)
    why_zh: str = Field(default="", min_length=1, max_length=140)

    @model_validator(mode="after")
    def _reader_text_is_deliverable(self) -> ReaderCard:
        if not self.headline_zh.strip():
            raise ValueError("news_program_reader_headline_empty")
        if not _carries_han(self.headline_zh):
            raise ValueError("news_program_reader_headline_not_chinese")
        if not _carries_substance(self.why_zh):
            raise ValueError("news_program_reader_why_empty")
        return self


class EventSemanticsSignature(dspy.Signature):  # type: ignore[misc]
    """Interpret one bounded Event against the selected reader-history ledger."""

    evidence_json: str = dspy.InputField(
        desc="Delimited current_evidence, related_evidence, Event preview, gate and event_status JSON."
    )
    semantics: EventSemantics = dspy.OutputField(desc="The exact typed semantic interpretation of this Event.")


class EventTaxonomySignature(dspy.Signature):  # type: ignore[misc]
    """Classify one bounded Event under news_taxonomy_v1 from its evidence alone."""

    evidence_json: str = dspy.InputField(desc="Delimited current_evidence and Event preview/gate JSON; no told ledger.")
    taxonomy: ModelTaxonomyV1 = dspy.OutputField(desc="The exact typed four-axis taxonomy of this Event.")


class ReaderCardSignature(dspy.Signature):  # type: ignore[misc]
    """Write factual reader copy from bounded Event evidence and accepted semantics."""

    evidence_json: str = dspy.InputField(
        desc="Delimited current_evidence, related_evidence and Event preview/gate JSON; no told ledger."
    )
    semantics_json: str = dspy.InputField(desc="Canonical ReaderCardSemanticView JSON from EventSemantics.")
    card: ReaderCard = dspy.OutputField(desc="The exact typed Chinese reader card.")
