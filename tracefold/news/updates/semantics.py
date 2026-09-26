"""Normalize grounded claims once; adopt content independently of reader copy."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Protocol

from .contracts import (
    Change,
    ChangeKind,
    Claim,
    DraftClaim,
    EventUpdate,
    EvidenceRelation,
    Extraction,
    FrozenInput,
    IdentityHint,
    Implication,
    KnowledgeGap,
    PriorClaim,
    RelationDraft,
    SupportDraft,
    content_material,
    content_revision_for,
)
from .identity import canonical_json, digest, identity
from .judgment import (
    CLAIM_READING_TASKS,
    MAX_QUESTIONS_PER_REQUEST,
    Answer,
    Budget,
    ContractFault,
    NewsJudgments,
    ProviderUnavailable,
    Question,
    Task,
)
from .topics import CODEBOOK, project_topics


class ClaimExtractor(Protocol):
    identity: str

    async def extract(self, source: FrozenInput, *, extract_only: bool) -> Extraction:
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


def equivalent_is_possible(current: DraftClaim, previous: Claim, hints: tuple[IdentityHint, ...] = ()) -> bool:
    """Refuse demonstrable numeric/modal mismatches; do not guess entity aliases.

    Missing fields are not evidence of equality or conflict. Free-text subject
    translations cannot be compared by lowercasing; the relation backend owns that.
    """
    current_facts: dict[str, set[str]] = {}
    previous_facts: dict[str, set[str]] = {}
    for hint in _known_identity(current, hints):
        current_facts.setdefault(hint.key, set()).add(hint.value)
    for hint in previous.known_identity:
        previous_facts.setdefault(hint.key, set()).add(hint.value)
    if any(current_facts[key] != previous_facts[key] for key in current_facts.keys() & previous_facts.keys()):
        return False
    a = current.fields
    b = previous.fields
    if "unknown" not in {a.polarity, b.polarity} and a.polarity != b.polarity:
        return False
    if "unknown" not in {a.mode, b.mode} and a.mode != b.mode:
        return False
    if a.phase not in {None, "unknown"} and b.phase not in {None, "unknown"} and a.phase != b.phase:
        return False
    # Only compare normalized code values, not language-dependent period prose.
    qa = {(q.name.casefold(), q.unit.casefold(), q.period): Decimal(q.value) for q in a.quantities}
    qb = {(q.name.casefold(), q.unit.casefold(), q.period): Decimal(q.value) for q in b.quantities}
    return all(qa[key] == qb[key] for key in qa.keys() & qb.keys())


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
    prior = {row.claim.ref for row in source.prior}
    targets = {row.ref for row in source.read_targets}
    for claim in extraction.claims:
        for citation in claim.citations:
            item = evidence.get(citation.evidence_ref)
            if item is None or citation.quote not in item.text:
                raise ContractFault("news_citation_not_in_frozen_source")
    for relation in extraction.relations:
        if relation.previous_ref not in prior:
            raise ContractFault("news_relation_previous_not_supplied")
    for support in extraction.supports:
        if support.evidence_ref not in evidence:
            raise ContractFault("news_support_evidence_not_supplied")
    for gap in extraction.open_questions:
        if gap.target_ref is not None and gap.target_ref not in targets:
            raise ContractFault("news_read_target_not_supplied")


def _default_change(current: DraftClaim, previous: Claim, relation: str) -> ChangeKind | None:
    if relation == "corrects":
        return "correction"
    if relation == "conflicts":
        return "conflict"
    if relation == "real_world_change":
        if current.fields.phase != previous.fields.phase:
            return "phase_change"
        if _quantity_key(current) != _quantity_key(previous):
            return "parameter_change"
        return "scope_change"
    if relation == "adds_information":
        return "new_fact"
    return None


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
        self.identity = identity("semantic", "event_update_v1", extractor.identity, judgments.identity, topics)

    async def extract(self, source: FrozenInput, budget: Budget) -> Extraction:
        async with asyncio.timeout(budget.remaining()):
            result = await self.extractor.extract(source, extract_only=self.judgments.native is not None)
        validate_extraction(source, result)
        return result

    async def understand(
        self,
        source: FrozenInput,
        extracted: Extraction,
        budget: Budget,
        *,
        rebase_only: bool = False,
    ) -> Extraction:
        validate_extraction(source, extracted)
        result = extracted
        if self.judgments.native is not None and not rebase_only:
            result = await self._claim_readings(source, result, budget)
            result = await self._topics(result, budget)
        result = await self._relations(source, result, budget)
        return await self._supports(source, result, budget)

    async def _claim_readings(self, source: FrozenInput, extraction: Extraction, budget: Budget) -> Extraction:
        """The native backend owns mode, phase and content kind for each claim."""

        # The frozen evidence is shared context; each item carries only its own claim.
        context = canonical_json({"evidence": source.evidence})
        items = tuple(
            Question(item_id=claim.slot, payload_json=canonical_json({"claim": claim})) for claim in extraction.claims
        )
        readings: dict[Task, dict[str, Answer]] = {}
        for task in CLAIM_READING_TASKS:
            answers = await self.judgments.judge(task, items, budget, context_json=context)
            readings[task] = {answer.item_id: answer for answer in answers}
        updated = []
        for claim in extraction.claims:
            mode = readings["mode"][claim.slot]
            phase = readings["phase"][claim.slot]
            content_kind = readings["content_kind"][claim.slot]
            if mode.status == "unavailable":
                raise ProviderUnavailable("news_required_claim_mode_unavailable")
            values = claim.fields.model_dump(mode="json")
            values["mode"] = mode.value
            if phase.value == "not_applicable":
                values["phase"] = None
            else:
                values["phase"] = phase.value or "unknown"
            # An unavailable content reading keeps the extractor's own; it never blocks adoption.
            if content_kind.status == "available":
                values["content_kind"] = content_kind.value
            updated.append(DraftClaim.model_validate({**claim.model_dump(mode="json"), "fields": values}))
        return _replace(extraction, claims=tuple(updated))

    async def _topics(self, extraction: Extraction, budget: Budget) -> Extraction:
        # One request for the whole codebook; the claims are supplied once as shared context.
        context = canonical_json({"claims": extraction.claims})
        items = tuple(
            Question(item_id=code, payload_json=canonical_json({"topic": label})) for code, label in self.topics
        )
        answers = await self.judgments.judge("topic", items, budget, context_json=context)
        return _replace(extraction, topics=project_topics(answers, self.topics))

    async def _relations(self, source: FrozenInput, extraction: Extraction, budget: Budget) -> Extraction:
        # Current/prior candidates are already bounded by retrieval. No global
        # pair search; no title-only key for relation cache reuse.
        known = {(row.slot, row.previous_ref): row for row in extraction.relations}
        questions = []
        pairs: dict[str, tuple[DraftClaim, PriorClaim]] = {}
        for claim in extraction.claims:
            for prior in source.prior:
                if (claim.slot, prior.claim.ref) in known:
                    continue
                item_id = identity("pair", claim.slot, prior.claim.ref)
                pairs[item_id] = (claim, prior)
                payload = {
                    "current": claim,
                    "previous": prior.claim,
                    "previous_content_revision": prior.content_revision,
                    "known_numeric_modal_mismatch": not equivalent_is_possible(
                        claim, prior.claim, source.identity_hints
                    ),
                }
                questions.append(Question(item_id=item_id, payload_json=canonical_json(payload)))
        if not questions:
            return extraction
        answers = await self.judgments.judge("relation", tuple(questions), budget)
        for answer in answers:
            claim, prior = pairs[answer.item_id]
            # An unavailable answer is an unresolved relation, never a manufactured one.
            value = str(answer.value or "unresolved")
            known[(claim.slot, prior.claim.ref)] = RelationDraft.model_validate(
                {
                    "slot": claim.slot,
                    "previous_ref": prior.claim.ref,
                    "relation": value,
                    "change_kind": _default_change(claim, prior.claim, value),
                }
            )
        return _replace(extraction, relations=tuple(known.values()))

    async def _supports(self, source: FrozenInput, extraction: Extraction, budget: Budget) -> Extraction:
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
        for answer in await self.judgments.judge("support", tuple(items), budget):
            slot, ref = pairs[answer.item_id]
            supports[(slot, ref)] = SupportDraft.model_validate(
                {"slot": slot, "evidence_ref": ref, "relation": answer.value or "unresolved"}
            )
        return _replace(extraction, supports=tuple(supports.values()))


def _equivalent_prior(
    draft: DraftClaim,
    relations: list[RelationDraft],
    material_previous: set[str],
    previous: dict[str, PriorClaim],
    source: FrozenInput,
) -> PriorClaim | None:
    # Reject a contradicted equivalent pair, not other independently valid
    # pairs. A numerical mismatch with an older claim cannot veto the latest.
    equivalent = [
        row
        for row in relations
        if row.relation == "equivalent"
        and equivalent_is_possible(draft, previous[row.previous_ref].claim, source.identity_hints)
        # Repeating B can correctly be both equivalent to B and a change from A.
        # Reuse B only when ALL reported changes are already its antecedents. A
        # real reversal back to A still has a new predecessor B and remains new.
        and material_previous <= set(previous[row.previous_ref].claim.antecedent_refs)
    ]
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
    """Changes introduced by a new occurrence that is not a restatement."""

    if not material_relations:
        unsettled = _unsettled_priors(relations, source)
        if not unsettled:
            return [Change(kind="new_fact", current_ref=ref)]
        # An unresolved comparison cannot manufacture a catalyst. The claim is adopted and can reach a
        # reader, but it is not published as new until a relation is established.
        return [
            Change(
                kind="possible_new",
                current_ref=ref,
                previous_ref=prior_ref,
                previous_content_ref=_content_ref(previous[prior_ref]),
                relation="unresolved",
            )
            for prior_ref in unsettled
        ]
    changes: list[Change] = []
    for relation in material_relations:
        prior = previous[relation.previous_ref]
        kinds: set[ChangeKind] = set()
        if relation.change_kind is not None:
            kinds.add(relation.change_kind)
        if relation.relation == "real_world_change":
            if draft.fields.phase != prior.claim.fields.phase:
                kinds.add("phase_change")
            if _quantity_key(draft) != _quantity_key(prior.claim):
                kinds.add("parameter_change")
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
    for draft in extraction.claims:
        relations = relations_by_slot.get(draft.slot, [])
        material_relations = tuple(row for row in relations if row.change_kind is not None)
        material_previous = {row.previous_ref for row in material_relations}
        same = _equivalent_prior(draft, relations, material_previous, previous, source)
        material = _claim_material(draft)
        # A new real-world reversal can return to a previously seen numeric state.
        # Anchor this occurrence to its explicit predecessor and source, rather
        # than reusing an earlier same-shaped claim and silently losing the action.
        if material_relations:
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
                ancestry = set(material_previous)
                for previous_ref in material_previous:
                    ancestry.update(previous[previous_ref].claim.antecedent_refs)
                antecedents = tuple(sorted(ancestry))
            claims[ref] = Claim(
                ref=ref,
                statement=draft.statement,
                fields=same.claim.fields if same is not None else draft.fields,
                citations=draft.citations,
                first_available_at_ms=first,
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
                for relation in material_relations:
                    if relation.relation == "corrects" and relation.previous_ref in claims:
                        retired.add(relation.previous_ref)
                changes.extend(_occurrence_changes(draft, ref, relations, material_relations, previous, source))
        changes.extend(_link_evidence(draft, ref, extraction, links, head, head_refs))
    content_sha = digest(content_material(source.event_id, claims, retired, links.values()))
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
        topics=tuple(sorted(set(extraction.topics))),
        claims=tuple(claims.values()),
        evidence=tuple(evidence.values()),
        evidence_relations=tuple(links.values()),
        retired_claim_refs=tuple(sorted(retired)),
        changes=tuple(dict.fromkeys(changes)),
        implications=tuple(
            Implication(
                claim_refs=tuple(slot_refs[slot] for slot in row.slots),
                channel=row.channel,
                explanation=row.explanation,
                conditions=row.conditions,
                origin=row.origin,
            )
            for row in extraction.implications
        ),
        open_questions=tuple(
            KnowledgeGap(
                question=row.question,
                claim_refs=tuple(slot_refs[slot] for slot in row.slots),
                target_ref=row.target_ref,
            )
            for row in extraction.open_questions
        ),
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
    support = {row.evidence_ref: row.relation for row in extraction.supports if row.slot == draft.slot}
    # Preserve every validated source relationship, including refutations
    # outside the claim's quote list. A citation without a supplied judgment
    # remains unresolved; a source relationship does not invent a new quote.
    evidence_refs = dict.fromkeys([*(citation.evidence_ref for citation in draft.citations), *support])
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
