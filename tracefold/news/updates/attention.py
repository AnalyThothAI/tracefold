"""One bounded editorial selection over already adopted and delivery-eligible claims."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .contracts import Claim, Exact, Source
from .identity import digest, identity

BRIEF = files("tracefold.news.updates").joinpath("editorial_brief.txt").read_text(encoding="utf-8")
BRIEF_IDENTITY = identity("editorial_brief", BRIEF)
Disposition = Literal["notify", "key", "feed_only"]


class AttentionDecision(Exact):
    claim_ref: str
    disposition: Disposition
    reason_zh: str | None = Field(default=None, max_length=240)


class AttentionAssessment(Exact):
    decisions: tuple[AttentionDecision, ...]

    @model_validator(mode="after")
    def unique(self) -> AttentionAssessment:
        refs = [row.claim_ref for row in self.decisions]
        if len(refs) != len(set(refs)):
            raise ValueError("news_attention_duplicate_claim_ref")
        return self


class AttentionAssessor(Protocol):
    identity: str

    async def assess(
        self, claims: tuple[Claim, ...], *, sources: Mapping[str, Source], watch_symbols: tuple[str, ...]
    ) -> AttentionAssessment: ...


def assessment_input(
    claims: tuple[Claim, ...], *, sources: Mapping[str, Source], watch_symbols: tuple[str, ...]
) -> dict[str, object]:
    """A replayable, bounded input slice; cited source provenance is retained."""

    return {
        "brief_identity": BRIEF_IDENTITY,
        "watch_symbols": sorted(set(watch_symbols)),
        "claims": [
            {
                "ref": claim.ref,
                "statement": claim.statement,
                "fields": claim.fields.model_dump(mode="json"),
                "topics": claim.topics,
                "citations": [
                    {"ref": citation.evidence_ref, "quote": citation.quote, "source": sources[citation.evidence_ref]}
                    for citation in claim.citations
                ],
            }
            for claim in claims
        ],
    }


def input_digest(value: object) -> str:
    return digest(value)
