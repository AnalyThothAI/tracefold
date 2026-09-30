"""Ground quotes in the frozen reading scope and validate extraction references."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Final, Protocol

from .contracts import (
    Citation,
    DiscardedClaim,
    Evidence,
    Extraction,
    FrozenInput,
)
from .judgment import (
    ContractFault,
)
from .projection import reading_views

log = logging.getLogger("tracefold.news")


class ClaimExtractor(Protocol):
    identity: str

    async def extract(self, source: FrozenInput) -> Extraction:
        """Open claim extraction. The caller bounds the call with its own asyncio.timeout."""
        ...


def validate_extraction(source: FrozenInput, extraction: Extraction) -> None:
    evidence = {item.ref: item for item in source.evidence}
    visible = {view.evidence_ref: view.spans for view in reading_views(source)}
    prior = {row.claim.ref for row in source.prior}
    targets = {row.ref for row in source.read_targets}
    for claim in extraction.claims:
        for citation in claim.citations:
            item = evidence.get(citation.evidence_ref)
            if item is None or citation.quote not in item.text:
                raise ContractFault("news_citation_not_in_frozen_source")
            if not any(citation.quote in span.text for span in visible[item.ref]):
                raise ContractFault("news_citation_not_in_visible_source")
    for relation in extraction.relations:
        if relation.previous_ref not in prior:
            raise ContractFault("news_relation_previous_not_supplied")
    for support in extraction.supports:
        if support.evidence_ref not in evidence:
            raise ContractFault("news_support_evidence_not_supplied")
    for resolution in extraction.resolved_questions:
        if resolution.question_ref not in source.open_questions:
            raise ContractFault("news_question_not_supplied")
        for citation in resolution.citations:
            item = evidence.get(citation.evidence_ref)
            if item is None or citation.quote not in item.text:
                raise ContractFault("news_resolution_not_grounded")
            if not any(citation.quote in span.text for span in visible[item.ref]):
                raise ContractFault("news_resolution_not_in_visible_source")
    for gap in extraction.open_questions:
        if gap.target_ref is not None and gap.target_ref not in targets:
            raise ContractFault("news_read_target_not_supplied")


# Quotation and emphasis marks a model wraps around a quote (`*HEADLINE*`, `"..."`); never part of what it cites.
_QUOTE_MARKS: Final = "*_`\"'“”‘’「」『』«»…"


def locate_quote(quote: str, texts: Sequence[str]) -> str | None:
    """The exact source text a generated quote names, letting it differ only in letter case, whitespace and the
    quotation or emphasis marks around it.

    Returns the span as it appears in the source, never the generated spelling, so a stored quote is always a
    verbatim substring of its evidence.
    """

    found = _locate(quote, texts)
    unmarked = quote.strip().strip(_QUOTE_MARKS)
    if found is None and unmarked and unmarked != quote:
        found = _locate(unmarked, texts)
    return found


def _locate(quote: str, texts: Sequence[str]) -> str | None:
    for text in texts:
        if quote in text:
            return quote
    wanted = "".join(quote.split()).casefold()
    if not wanted:
        return None
    for text in texts:
        folded: list[str] = []
        positions: list[int] = []
        for index, char in enumerate(text):
            if char.isspace():
                continue
            for piece in char.casefold():
                folded.append(piece)
                positions.append(index)
        found = "".join(folded).find(wanted)
        if found >= 0:
            return text[positions[found] : positions[found + len(wanted) - 1] + 1]
    return None


def _grounded(
    citations: Sequence[Citation], evidence: Mapping[str, Evidence], visible: Mapping[str, tuple[str, ...]]
) -> tuple[tuple[Citation, ...], str | None]:
    grounded = []
    for citation in citations:
        item = evidence.get(citation.evidence_ref)
        if item is None:
            return (), "news_citation_not_in_frozen_source"
        quote = locate_quote(citation.quote, visible[item.ref])
        if quote is None:
            outside = locate_quote(citation.quote, (item.text,)) is None
            return (), "news_citation_not_in_frozen_source" if outside else "news_citation_not_in_visible_source"
        grounded.append(Citation(evidence_ref=item.ref, quote=quote))
    return tuple(grounded), None


def ground_extraction(source: FrozenInput, extraction: Extraction) -> Extraction:
    """Keep every claim whose quotes name visible source text, each quote replaced by that exact text.

    A claim with a quote that names no visible text is discarded by slot with its reason, together with the
    hints that referred to it; its siblings are kept. A question resolution needs the same grounding, and one
    that has none leaves its question open.
    """

    evidence = {item.ref: item for item in source.evidence}
    visible = {view.evidence_ref: tuple(span.text for span in view.spans) for view in reading_views(source)}
    claims = []
    discarded = list(extraction.discarded_claims)
    for claim in extraction.claims:
        citations, code = _grounded(claim.citations, evidence, visible)
        if code is None:
            claims.append(claim.model_copy(update={"citations": citations}))
        else:
            discarded.append(DiscardedClaim(slot=claim.slot, code=code))
            log.warning("news_extraction_claim_discarded", extra={"slot": claim.slot, "error_code": code})
    resolutions = []
    for resolution in extraction.resolved_questions:
        citations, code = _grounded(resolution.citations, evidence, visible)
        if code is None:
            resolutions.append(resolution.model_copy(update={"citations": citations}))
        else:
            log.warning("news_extraction_hint_discarded", extra={"hint": "QuestionResolution", "error_code": code})
    kept = {claim.slot for claim in claims}
    return replace_extraction(
        extraction,
        claims=tuple(claims),
        resolved_questions=tuple(resolutions),
        relations=tuple(row for row in extraction.relations if row.slot in kept),
        supports=tuple(row for row in extraction.supports if row.slot in kept),
        implications=tuple(row for row in extraction.implications if set(row.slots) <= kept),
        open_questions=tuple(row for row in extraction.open_questions if set(row.slots) <= kept),
        discarded_claims=tuple(discarded),
    )


def replace_extraction(extraction: Extraction, **values: object) -> Extraction:
    """A validated copy: model_copy alone would skip the slot invariants."""

    return Extraction.model_validate({**dict(extraction), **values})
