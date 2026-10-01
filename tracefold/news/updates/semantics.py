"""Complete finite semantic judgments over one grounded extraction."""

from __future__ import annotations

import asyncio

from .assembly import proven_mismatches, relation_change
from .contracts import (
    DraftClaim,
    Extraction,
    FrozenInput,
    PriorClaim,
    RelationDraft,
    SupportDraft,
)
from .extraction import ClaimExtractor, ground_extraction, replace_extraction, validate_extraction
from .identity import canonical_json, identity
from .judgment import (
    MAX_QUESTIONS_PER_REQUEST,
    Answer,
    Budget,
    ContractFault,
    NewsJudgments,
    ProviderUnavailable,
    Question,
)
from .topics import CODEBOOK


def _require_available(answers: tuple[Answer, ...], code: str, *, final_attempt: bool) -> None:
    """A provider failure is retried while attempts remain; content uncertainty (`unresolved`) never is."""

    if not final_attempt and any(answer.status == "unavailable" for answer in answers):
        raise ProviderUnavailable(code)


class SemanticAnalyzer:
    def __init__(
        self,
        extractor: ClaimExtractor,
        judgments: NewsJudgments,
        *,
        topics: tuple[tuple[str, str], ...] = CODEBOOK,
    ) -> None:
        self.extractor = extractor
        self.judgments = judgments
        self.topics = topics
        # The whole codebook is one native request; refuse a codebook that cannot be one.
        if len(topics) > MAX_QUESTIONS_PER_REQUEST:
            raise ValueError("news_topic_codebook_too_large")
        self.identity = identity("semantic", "event_understanding_v2", extractor.identity, judgments.identity, topics)

    async def extract(self, source: FrozenInput, budget: Budget) -> Extraction:
        """Extract and ground claims one by one. Only material whose every claim was unusable fails."""

        async with asyncio.timeout(budget.remaining()):
            result = await self.extractor.extract(source)
        result = ground_extraction(source, result)
        if not result.claims and result.discarded_claims:
            raise ContractFault(result.discarded_claims[0].code)
        validate_extraction(source, result)
        return result

    async def understand(
        self,
        source: FrozenInput,
        extracted: Extraction,
        budget: Budget,
        *,
        rebase_only: bool = False,
        final_attempt: bool = True,
    ) -> Extraction:
        """Complete the narrow judgments of one extraction.

        A relation or source answer the provider could not give is an unresolved comparison only on
        the final attempt of a revision. Earlier attempts raise ProviderUnavailable so the worker
        retries; successful answers are already cached and are not asked again. Relations are always
        derived for the priors supplied now, so an understanding made against other priors is re-derived.
        """

        result = replace_extraction(extracted, relations=())
        validate_extraction(source, result)
        if not rebase_only:
            result = await self._clarify_modes(source, result, budget)
        result = await self._relations(source, result, budget, final_attempt=final_attempt)
        return await self._supports(source, result, budget, final_attempt=final_attempt)

    async def _clarify_modes(self, source: FrozenInput, extraction: Extraction, budget: Budget) -> Extraction:
        """One cached clarification belongs to understanding, never to reader selection."""
        evidence = {item.ref: item for item in source.evidence}
        pending = tuple(
            Question(
                item_id=claim.slot,
                payload_json=canonical_json(
                    {
                        "claim": claim,
                        "evidence": [evidence[citation.evidence_ref] for citation in claim.citations],
                    }
                ),
            )
            for claim in extraction.claims
            if claim.fields.mode == "unknown"
        )
        if not pending:
            return extraction
        answers = {row.item_id: row for row in await self.judgments.reask("mode", pending, budget)}
        claims = []
        for claim in extraction.claims:
            answer = answers.get(claim.slot)
            if answer is not None and answer.status == "available":
                values = claim.model_dump(mode="json")
                values["fields"]["mode"] = answer.value
                claims.append(DraftClaim.model_validate(values))
            else:
                claims.append(claim)
        # Unknown/unavailable is settled content uncertainty, not a new retry lifecycle.
        return replace_extraction(extraction, claims=tuple(claims))

    async def _relations(
        self, source: FrozenInput, extraction: Extraction, budget: Budget, *, final_attempt: bool
    ) -> Extraction:
        """Judge every new claim against every supplied current prior; no model outside the judge decides one.

        Candidates are already bounded by retrieval (current claims only); no global pair search and no
        title-only key for relation cache reuse. Every supplied pair ends with a relation.
        """

        questions = []
        pairs: dict[str, tuple[DraftClaim, PriorClaim]] = {}
        for claim in extraction.claims:
            for prior in source.prior:
                item_id = identity("pair", claim.slot, prior.claim.ref)
                pairs[item_id] = (claim, prior)
                payload = {
                    "current": claim,
                    "previous": prior.claim,
                    "proven_mismatches": proven_mismatches(claim, prior.claim, source.identity_hints),
                }
                questions.append(Question(item_id=item_id, payload_json=canonical_json(payload)))
        if not questions:
            return extraction
        answers = await self.judgments.judge("relation", tuple(questions), budget)
        _require_available(answers, "news_relation_unavailable", final_attempt=final_attempt)
        relations = []
        for answer in answers:
            claim, prior = pairs[answer.item_id]
            # An unavailable answer is an unresolved relation, never a manufactured one.
            value = str(answer.value or "unresolved")
            relations.append(
                RelationDraft.model_validate(
                    {
                        "slot": claim.slot,
                        "previous_ref": prior.claim.ref,
                        "relation": value,
                        "change_kind": relation_change(claim, prior.claim, value),
                    }
                )
            )
        return replace_extraction(extraction, relations=tuple(relations))

    async def _supports(
        self, source: FrozenInput, extraction: Extraction, budget: Budget, *, final_attempt: bool
    ) -> Extraction:
        # One source/claim comparison, reused later. Native mode does not ask the
        # generator to validate successful Jev results a second time.
        supports = {(row.slot, row.evidence_ref): row for row in extraction.supports}
        items = []
        pairs: dict[str, tuple[str, str]] = {}
        evidence = {item.ref: item for item in source.evidence}
        for claim in extraction.claims:
            # A source can refute a claim without being that claim's quoted
            # provenance. Compare missing pairs from the current frozen material,
            # never every source accumulated in the Event's adopted history.
            for ref, item in evidence.items():
                if (claim.slot, ref) in supports:
                    continue
                key = identity("support", claim.slot, ref)
                pairs[key] = (claim.slot, ref)
                items.append(Question(item_id=key, payload_json=canonical_json({"claim": claim, "evidence": item})))
        if not items:
            return extraction
        answers = await self.judgments.judge("support", tuple(items), budget)
        _require_available(answers, "news_support_unavailable", final_attempt=final_attempt)
        for answer in answers:
            slot, ref = pairs[answer.item_id]
            supports[(slot, ref)] = SupportDraft.model_validate(
                {"slot": slot, "evidence_ref": ref, "relation": answer.value or "unresolved"}
            )
        return replace_extraction(extraction, supports=tuple(supports.values()))
