"""Normalize grounded claims once; adopt content independently of reader copy."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Protocol

from .contracts import (
    Change,
    ChangeKind,
    Citation,
    Claim,
    DiscardedClaim,
    DraftClaim,
    EventUpdate,
    Evidence,
    EvidenceRelation,
    Extraction,
    FrozenInput,
    IdentityHint,
    Implication,
    KnowledgeGap,
    Phase,
    PriorClaim,
    Quantity,
    RelationDraft,
    SupportDraft,
    content_material,
    content_revision_for,
    current_evidence,
    semantic_state,
)
from .identity import canonical_json, digest, identity
from .judgment import (
    MAX_QUESTIONS_PER_REQUEST,
    Answer,
    Budget,
    ContractFault,
    NewsJudgments,
    ProviderUnavailable,
    Question,
)
from .projection import reading_views
from .topics import CODEBOOK

log = logging.getLogger("tracefold.news")


class ClaimExtractor(Protocol):
    identity: str

    async def extract(self, source: FrozenInput) -> Extraction:
        """Open claim extraction. The caller bounds the call with its own asyncio.timeout."""
        ...


QuantityKey = tuple[tuple[str, Decimal, str, str], ...]


def _quantity_key(claim: DraftClaim | Claim) -> QuantityKey:
    return tuple(
        sorted(
            (quantity.name.casefold(), Decimal(quantity.value), quantity.unit.casefold(), quantity.period or "")
            for quantity in claim.fields.quantities
        )
    )


def _known_identity(current: DraftClaim, hints: tuple[IdentityHint, ...]) -> tuple[IdentityHint, ...]:
    return tuple(
        hint
        for hint in hints
        if any(
            citation.evidence_ref == hint.evidence_ref and hint.surface in citation.quote
            for citation in current.citations
        )
    )


def _calendar_quarter(value: str | None) -> tuple[int, int] | None:
    """Only an explicit calendar quarter has a comparable period identity."""

    match = re.fullmatch(r"\s*(?:Q([1-4])\s*([12]\d{3})|([12]\d{3})\s*Q([1-4]))\s*", value or "", re.I)
    if match is None:
        return None
    return (int(match.group(2)), int(match.group(1))) if match.group(1) else (int(match.group(3)), int(match.group(4)))


def _absolute_time(value: str | None) -> tuple[str, str] | None:
    """A date or timezone-aware timestamp of explicit precision, never a relative phrase."""

    if value is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return "date", datetime.fromisoformat(value).date().isoformat()
        except ValueError:
            return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})", value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return "second", parsed.astimezone(UTC).isoformat()


def _comparable_quantities(quantities: tuple[Quantity, ...]) -> dict[tuple[str, str, tuple[int, int] | None], Decimal]:
    """Values keyed by metric, unit and calendar quarter; a quantity over an unparsed period is not comparable."""

    return {
        (q.name.strip().casefold(), q.unit.strip().casefold(), _calendar_quarter(q.period)): Decimal(q.value)
        for q in quantities
        if q.period is None or _calendar_quarter(q.period) is not None
    }


def proven_mismatches(current: DraftClaim, previous: Claim, hints: tuple[IdentityHint, ...] = ()) -> tuple[str, ...]:
    """Return only differences established by comparable evidence, never prose inequality.

    An empty result means unknown, not proven equivalent. The same result is sent to the relation
    model and used to veto an equivalent answer.
    """

    mismatches: list[str] = []
    current_facts: dict[str, set[str]] = {}
    previous_facts: dict[str, set[str]] = {}
    for hint in _known_identity(current, hints):
        if hint.key not in {"subject_id", "object_id"}:
            continue
        current_facts.setdefault(hint.key, set()).add(hint.value)
    for hint in previous.known_identity:
        if hint.key not in {"subject_id", "object_id"}:
            continue
        previous_facts.setdefault(hint.key, set()).add(hint.value)
    mismatches.extend(
        key
        for key in current_facts.keys() & previous_facts.keys()
        if len(current_facts[key]) == len(previous_facts[key]) == 1 and current_facts[key] != previous_facts[key]
    )
    a = current.fields
    b = previous.fields
    if "unknown" not in {a.polarity, b.polarity} and a.polarity != b.polarity:
        mismatches.append("polarity")
    if "unknown" not in {a.mode, b.mode} and a.mode != b.mode:
        mismatches.append("mode")
    if a.phase not in {None, "unknown"} and b.phase not in {None, "unknown"} and a.phase != b.phase:
        mismatches.append("phase")
    a_quarter = _calendar_quarter(a.statistical_period)
    b_quarter = _calendar_quarter(b.statistical_period)
    if a_quarter is not None and b_quarter is not None and a_quarter != b_quarter:
        mismatches.append("statistical_period")
    for field in ("occurred_at", "effective_at"):
        left = _absolute_time(getattr(a, field))
        right = _absolute_time(getattr(b, field))
        if left is not None and right is not None and left[0] == right[0] and left != right:
            mismatches.append(field)
    # Two numbers are comparable only for the same metric and unit over an aligned period: no period on
    # either side, or one explicit calendar quarter. Who the numbers belong to is not read from free text
    # ("Nvidia" and "Nvidia Corp" are one issuer); a proven entity difference is `subject_id`/`object_id`.
    if (a.statistical_period is None and b.statistical_period is None) or (
        a_quarter is not None and a_quarter == b_quarter
    ):
        qa = _comparable_quantities(a.quantities)
        qb = _comparable_quantities(b.quantities)
        if any(qa[key] != qb[key] for key in qa.keys() & qb.keys()):
            mismatches.append("quantity")
    return tuple(sorted(set(mismatches)))


def _claim_material(draft: DraftClaim) -> dict[str, Any]:
    fields = draft.fields.model_dump(mode="json")
    fields["quantities"] = sorted(
        (
            {**quantity.model_dump(mode="json"), "value": format(Decimal(quantity.value).normalize(), "f")}
            for quantity in draft.fields.quantities
        ),
        key=canonical_json,
    )
    fields["conditions"] = sorted(set(draft.fields.conditions))
    fields["assets"] = sorted(fields["assets"], key=canonical_json)
    # Statement is presentation and content_kind is the notification policy's reading; neither changes the
    # stable identity of a proposition on its own.
    del fields["content_kind"]
    return fields


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
    return _replace(
        extraction,
        claims=tuple(claims),
        resolved_questions=tuple(resolutions),
        relations=tuple(row for row in extraction.relations if row.slot in kept),
        supports=tuple(row for row in extraction.supports if row.slot in kept),
        implications=tuple(row for row in extraction.implications if set(row.slots) <= kept),
        open_questions=tuple(row for row in extraction.open_questions if set(row.slots) <= kept),
        discarded_claims=tuple(discarded),
    )


def _phase(value: Phase | None) -> Phase | None:
    # A missing phase and an unestablished one say the same thing: nothing.
    return None if value == "unknown" else value


def _realization_changes(current: DraftClaim, previous: Claim) -> set[ChangeKind]:
    """What a real-world change altered: a phase both readings establish, or a structured quantity."""

    kinds: set[ChangeKind] = set()
    before, after = _phase(previous.fields.phase), _phase(current.fields.phase)
    if before is not None and after is not None and before != after:
        kinds.add("phase_change")
    if _quantity_key(current) != _quantity_key(previous):
        kinds.add("parameter_change")
    return kinds


def _default_change(current: DraftClaim, previous: Claim, relation: str) -> ChangeKind | None:
    if relation == "corrects":
        return "correction"
    if relation == "conflicts":
        return "conflict"
    if relation == "real_world_change":
        kinds = _realization_changes(current, previous)
        if "phase_change" in kinds:
            return "phase_change"
        if "parameter_change" in kinds:
            return "parameter_change"
        return "scope_change"
    if relation == "adds_information":
        return "new_fact"
    return None


def _require_available(answers: tuple[Answer, ...], code: str, *, final_attempt: bool) -> None:
    """A provider failure is retried while attempts remain; content uncertainty (`unresolved`) never is."""

    if not final_attempt and any(answer.status == "unavailable" for answer in answers):
        raise ProviderUnavailable(code)


def _replace(extraction: Extraction, **values: object) -> Extraction:
    """A validated copy: model_copy alone would skip the slot invariants."""

    return Extraction.model_validate({**dict(extraction), **values})


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

        result = _replace(extracted, relations=())
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
        return _replace(extraction, claims=tuple(claims))

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
                        "change_kind": _default_change(claim, prior.claim, value),
                    }
                )
            )
        return _replace(extraction, relations=tuple(relations))

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
        return _replace(extraction, supports=tuple(supports.values()))


def _equivalent_prior(
    draft: DraftClaim,
    relations: list[RelationDraft],
    occurrence_previous: set[str],
    previous: dict[str, PriorClaim],
    source: FrozenInput,
    evidence: dict[str, Evidence],
) -> PriorClaim | None:
    # Reject a contradicted equivalent pair, not other independently valid
    # pairs. A numerical mismatch with an older claim cannot veto the latest.
    equivalent = [
        row
        for row in relations
        if row.relation == "equivalent"
        and not proven_mismatches(draft, previous[row.previous_ref].claim, source.identity_hints)
        # Only a real-world transition can prevent reuse. A conflict or correction
        # with another Claim changes the relationship, not this proposition's identity.
        # Repeating B after A keeps B's antecedents; A -> B -> A has a new predecessor.
        and occurrence_previous <= set(previous[row.previous_ref].claim.antecedent_refs)
    ]
    if not equivalent and not occurrence_previous:
        # Two feeds can carry the same complete headline while the relation model
        # calls its second reading unrelated. This is narrower than deduplicating
        # statements: both complete cited source texts must match, the reading
        # must be identical, and the new structured quantities cannot add facts.
        repeated_source = [
            row
            for row in relations
            if row.relation == "unrelated"
            and previous[row.previous_ref].event_id == source.event_id
            and draft.statement == previous[row.previous_ref].claim.statement
            and not proven_mismatches(draft, previous[row.previous_ref].claim, source.identity_hints)
            and set(_quantity_key(draft)) <= set(_quantity_key(previous[row.previous_ref].claim))
            and any(
                current.quote
                == earlier.quote
                == evidence[current.evidence_ref].text
                == evidence[earlier.evidence_ref].text
                for current in draft.citations
                for earlier in previous[row.previous_ref].claim.citations
                if current.evidence_ref in evidence and earlier.evidence_ref in evidence
            )
        ]
        if len(repeated_source) == 1:
            return previous[repeated_source[0].previous_ref]
    if not equivalent:
        return None
    equivalent.sort(key=lambda row: (previous[row.previous_ref].event_id != source.event_id, row.previous_ref))
    return previous[equivalent[0].previous_ref]


def _unsettled_priors(relations: list[RelationDraft], source: FrozenInput) -> tuple[str, ...]:
    """Supplied priors this claim's relation to was not established.

    An unresolved or unavailable answer, a missing relation, and an `equivalent` answer the code refuted
    all leave the comparison open. Only `unrelated` settles it without a change.
    """

    by_prior = {row.previous_ref: row for row in relations}
    unsettled = []
    for prior in source.prior:
        relation = by_prior.get(prior.claim.ref)
        if relation is None or relation.relation in {"unresolved", "equivalent"}:
            unsettled.append(prior.claim.ref)
    return tuple(unsettled)


def _content_ref(prior: PriorClaim) -> str:
    return identity("update", prior.event_id, prior.content_revision)


def _occurrence_changes(
    draft: DraftClaim,
    ref: str,
    relations: list[RelationDraft],
    material_relations: tuple[RelationDraft, ...],
    previous: dict[str, PriorClaim],
    source: FrozenInput,
) -> list[Change]:
    """Changes introduced by a new occurrence that is not a restatement.

    A conflict annotates the new claim; it does not stand in for it. A claim that only conflicts with earlier
    claims is still new content (a catalyst), and its conflicts are published beside it.
    """

    changes: list[Change] = []
    if all(relation.relation == "conflicts" for relation in material_relations):
        unsettled = _unsettled_priors(relations, source)
        if not unsettled:
            changes.append(Change(kind="new_fact", current_ref=ref))
        # An unresolved comparison cannot manufacture a catalyst. The claim is adopted and can reach a
        # reader, but it is not published as new until a relation is established.
        changes.extend(
            Change(
                kind="possible_new",
                current_ref=ref,
                previous_ref=prior_ref,
                previous_content_ref=_content_ref(previous[prior_ref]),
                relation="unresolved",
            )
            for prior_ref in unsettled
        )
    for relation in material_relations:
        prior = previous[relation.previous_ref]
        kinds: set[ChangeKind] = set()
        if relation.change_kind is not None:
            kinds.add(relation.change_kind)
        if relation.relation == "real_world_change":
            kinds |= _realization_changes(draft, prior.claim)
        changes.extend(
            Change(
                kind=kind,
                current_ref=ref,
                previous_ref=relation.previous_ref,
                previous_content_ref=_content_ref(prior),
                relation=relation.relation,
            )
            for kind in sorted(kinds)
        )
    return changes


def _in_order(
    draft: DraftClaim,
    relations: list[RelationDraft],
    same: PriorClaim | None,
    previous: Mapping[str, PriorClaim],
    source: FrozenInput,
    evidence: Mapping[str, Evidence],
) -> list[RelationDraft]:
    """Refuse a correction or real-world change of a claim that was available before this one was reported.

    A late-arriving older report cannot retire or supersede a newer claim; the comparison stays unresolved.
    This claim's time is its earliest cited source (its publication time when the provider gave one), or,
    when it restates one of this Event's own claims, that claim's first availability.
    """

    reported = [
        min(value for value in (item.source.published_at_ms, item.source.first_available_at_ms) if value is not None)
        for item in (evidence[citation.evidence_ref] for citation in draft.citations)
    ]
    if same is not None and same.event_id == source.event_id:
        reported.append(same.claim.first_available_at_ms)
    reported_at = min(reported)
    ordered = []
    for row in relations:
        if row.relation not in {"corrects", "real_world_change"} or (
            previous[row.previous_ref].claim.first_available_at_ms <= reported_at
        ):
            ordered.append(row)
            continue
        log.info(
            "news_relation_out_of_order",
            extra={"event_id": source.event_id, "relation": row.relation, "previous_ref": row.previous_ref},
        )
        ordered.append(RelationDraft(slot=row.slot, previous_ref=row.previous_ref, relation="unresolved"))
    return ordered


def assemble_update(
    source: FrozenInput,
    extraction: Extraction,
    head: EventUpdate | None,
    *,
    adopted_at_ms: int,
) -> EventUpdate | None:
    """Return new substantive content or None; no reader/history/card input."""
    validate_extraction(source, extraction)
    if head is not None and source.event_id != head.event_id:
        raise ContractFault("news_head_event_mismatch")
    previous = {row.claim.ref: row for row in source.prior}
    evidence = {item.ref: item for item in source.evidence}
    claims: dict[str, Claim] = {}
    links: dict[tuple[str, str], EvidenceRelation] = {}
    retired: set[str] = set()
    head_refs: set[str] = set()
    if head is not None:
        for claim in head.claims:
            previous[claim.ref] = PriorClaim(
                event_id=head.event_id, content_revision=head.content_revision, claim=claim
            )
        evidence = {**{item.ref: item for item in head.evidence}, **evidence}
        claims = {claim.ref: claim for claim in head.claims}
        links = {(row.claim_ref, row.evidence_ref): row for row in head.evidence_relations}
        retired = set(head.retired_claim_refs)
        head_refs = set(claims)
    changes: list[Change] = []
    slot_refs: dict[str, str] = {}
    relations_by_slot: dict[str, list[RelationDraft]] = {}
    for relation in extraction.relations:
        relations_by_slot.setdefault(relation.slot, []).append(relation)
    established = {(row.current_ref, row.previous_ref, row.relation) for row in source.established_relations}
    for draft in extraction.claims:
        relations = relations_by_slot.get(draft.slot, [])
        occurrence_previous = {row.previous_ref for row in relations if row.relation == "real_world_change"}
        same = _equivalent_prior(draft, relations, occurrence_previous, previous, source, evidence)
        ordered = _in_order(draft, relations, same, previous, source, evidence)
        if ordered != relations:
            relations = ordered
            occurrence_previous = {row.previous_ref for row in relations if row.relation == "real_world_change"}
            same = _equivalent_prior(draft, relations, occurrence_previous, previous, source, evidence)
        material_relations = tuple(row for row in relations if row.change_kind is not None)
        material = _claim_material(draft)
        # A genuine new proposition may have incomplete structured fields, and a
        # reversal can return to a previously seen state. Without an equivalent
        # prior, anchor material changes to their predecessor and source.
        if same is None and material_relations:
            material["occurrence"] = {
                "previous": sorted(row.previous_ref for row in material_relations),
                "citations": sorted((citation.evidence_ref, citation.quote) for citation in draft.citations),
            }
        if same is not None and same.event_id == source.event_id:
            ref = same.claim.ref
        else:
            ref = identity("cl", source.event_id, same.claim.ref if same is not None else material)
        slot_refs[draft.slot] = ref
        if ref not in claims:
            if same is not None:
                first = same.claim.first_available_at_ms
                antecedents = same.claim.antecedent_refs
            else:
                first = min(evidence[row.evidence_ref].source.first_available_at_ms for row in draft.citations)
                ancestry = set(occurrence_previous)
                for previous_ref in occurrence_previous:
                    ancestry.update(previous[previous_ref].claim.antecedent_refs)
                antecedents = tuple(sorted(ancestry))
            claims[ref] = Claim(
                ref=ref,
                statement=draft.statement,
                fields=same.claim.fields if same is not None else draft.fields,
                citations=draft.citations,
                first_available_at_ms=first,
                topics=draft.topics,
                known_identity=_known_identity(draft, source.identity_hints),
                antecedent_refs=antecedents,
            )
            if same is not None:
                changes.append(
                    Change(
                        kind="restatement",
                        current_ref=ref,
                        previous_ref=same.claim.ref,
                        previous_content_ref=_content_ref(same),
                        relation="equivalent",
                    )
                )
            else:
                changes.extend(_occurrence_changes(draft, ref, relations, material_relations, previous, source))
        # Relationship deltas do not depend on whether this occurrence already existed.
        # An equivalent Claim can acquire a cross-Event conflict or correction while
        # retaining its ref and its first available time -- once: a later revision that
        # restates it again does not publish the same conflict or correction again.
        for relation in material_relations:
            if relation.relation == "corrects" and relation.previous_ref in claims:
                retired.add(relation.previous_ref)
            if (
                ref in head_refs
                and relation.relation != "real_world_change"
                and (ref, relation.previous_ref, relation.relation) not in established
            ):
                prior = previous[relation.previous_ref]
                changes.append(
                    Change(
                        kind="evidence_change" if relation.relation == "adds_information" else relation.change_kind,
                        current_ref=ref,
                        previous_ref=relation.previous_ref,
                        previous_content_ref=_content_ref(prior),
                        relation=relation.relation,
                    )
                )
        if draft.topics:
            claims[ref] = claims[ref].model_copy(update={"topics": tuple(sorted(set(draft.topics)))})
        changes.extend(_link_evidence(draft, ref, extraction, links, head, head_refs))
    # Replacing a source version changes its support even when the new body yields no claim.
    # Keep the historical relationship; the unjudged current version is explicitly unresolved.
    current_sources = current_evidence(evidence.values())
    previous_sources = {} if head is None else current_evidence(head.evidence, identity_context=evidence.values())
    for key, latest in current_sources.items():
        old_source = previous_sources.get(key)
        if not latest.source.record_id or old_source is None or old_source.ref == latest.ref:
            continue
        invalidated = retired | {change.previous_ref for change in changes if change.relation == "real_world_change"}
        affected = {
            link.claim_ref
            for link in links.values()
            if link.evidence_ref == old_source.ref and link.claim_ref not in invalidated
        }
        for claim_ref in affected:
            pair = (claim_ref, latest.ref)
            if pair not in links:
                links[pair] = EvidenceRelation(claim_ref=claim_ref, evidence_ref=latest.ref, relation="unresolved")
                changes.append(
                    Change(
                        kind="evidence_change",
                        current_ref=claim_ref,
                        previous_ref=claim_ref,
                        previous_content_ref=head.ref if head else None,
                    )
                )
    # History is immutable; the new document contains the still-current annotations.
    # Omission means no operation. Resolution requires an explicit, grounded reference.
    superseded = (set() if head is None else set(head.superseded_claim_refs)) | {
        change.previous_ref
        for change in changes
        if change.previous_ref in claims and change.relation == "real_world_change"
    }
    inactive = retired | superseded
    implications = {
        (tuple(sorted(row.claim_refs)), row.channel, row.origin): row
        for row in (() if head is None else head.implications)
        if not set(row.claim_refs) & inactive
    }
    for row in extraction.implications:
        value = Implication(
            claim_refs=tuple(slot_refs[slot] for slot in row.slots),
            channel=row.channel,
            explanation=row.explanation,
            conditions=row.conditions,
            origin=row.origin,
        )
        implications[(tuple(sorted(value.claim_refs)), value.channel, value.origin)] = value
    resolved = {row.question_ref for row in extraction.resolved_questions}
    questions = {
        row.ref: row
        for row in (() if head is None else head.open_questions)
        if row.ref not in resolved and not set(row.claim_refs) & inactive
    }
    for question in extraction.open_questions:
        gap = KnowledgeGap(
            question=question.question,
            claim_refs=tuple(slot_refs[slot] for slot in question.slots),
            target_ref=question.target_ref,
        )
        if gap.ref not in resolved:
            questions[gap.ref] = gap
    content_sha = digest(
        content_material(
            source.event_id,
            claims,
            retired,
            links.values(),
            state=semantic_state(
                claims.values(),
                implications.values(),
                questions.values(),
                evidence.values(),
                links.values(),
                superseded,
            ),
        )
    )
    if head is None and not claims:
        # A first read that asserts nothing is not an Event version: there is nothing for a reader to see and
        # nothing to supersede. The work settles, and a later member can still open the first version.
        return None
    if head is not None and content_sha == head.content_sha:
        return None
    previous_revision = None if head is None else head.content_revision
    return EventUpdate(
        event_id=source.event_id,
        input_revision=source.revision,
        content_sha=content_sha,
        content_revision=content_revision_for(content_sha, previous_revision),
        previous_content_revision=previous_revision,
        adopted_at_ms=adopted_at_ms,
        topics=tuple(
            sorted({topic for claim in claims.values() if claim.ref not in inactive for topic in claim.topics})
        ),
        claims=tuple(claims.values()),
        evidence=tuple(evidence.values()),
        evidence_relations=tuple(links.values()),
        retired_claim_refs=tuple(sorted(retired)),
        superseded_claim_refs=tuple(sorted(superseded)),
        changes=tuple(dict.fromkeys(changes)),
        implications=tuple(implications.values()),
        open_questions=tuple(questions.values()),
    )


def _link_evidence(
    draft: DraftClaim,
    ref: str,
    extraction: Extraction,
    links: dict[tuple[str, str], EvidenceRelation],
    head: EventUpdate | None,
    head_refs: set[str],
) -> list[Change]:
    """Record this claim's source relationships; return evidence changes to adopted claims."""

    # Quote existence is not semantic support. Unresolved is explicit until
    # the single backend supplied a source relationship.
    cited = {citation.evidence_ref for citation in draft.citations}
    support = {row.evidence_ref: row.relation for row in extraction.supports if row.slot == draft.slot}
    # Preserve every validated source relationship, including refutations
    # outside the claim's quote list. A citation without a supplied judgment
    # remains unresolved; a source relationship does not invent a new quote.
    # Material that does not address the claim is no relationship at all: arriving
    # beside the claim's sources must not make a new business revision.
    evidence_refs = dict.fromkeys(
        [*cited, *(ref for ref, relation in support.items() if ref in cited or relation != "not_addressed")]
    )
    changes: list[Change] = []
    for evidence_ref in evidence_refs:
        key = (ref, evidence_ref)
        relationship = support.get(evidence_ref, "unresolved")
        # A transient unavailable answer cannot degrade an adopted relationship.
        if key in links and relationship == "unresolved":
            continue
        new = EvidenceRelation(claim_ref=ref, evidence_ref=evidence_ref, relation=relationship)
        if links.get(key) == new:
            continue
        links[key] = new
        if head is not None and ref in head_refs:
            changes.append(
                Change(kind="evidence_change", current_ref=ref, previous_ref=ref, previous_content_ref=head.ref)
            )
    return changes
