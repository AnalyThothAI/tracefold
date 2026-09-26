"""Normalize grounded claims once; adopt content independently of reader copy."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from .contracts import (
    Change, Claim, DraftClaim, EvidenceRelation, EventUpdate, Extraction, FrozenInput,
    Implication, IdentityHint, KnowledgeGap, PriorClaim, RelationDraft, SupportDraft,
)
from .identity import canonical_json, digest, identity
from .judgment import Budget, ContractFault, NewsJudgments, ProviderUnavailable, Question
from .topics import CODEBOOK, project_topics


class ClaimExtractor(Protocol):
    identity: str

    async def extract(self, source: FrozenInput, *, extract_only: bool, timeout: float) -> Extraction: ...


def _quantity_key(claim: DraftClaim | Claim) -> tuple[tuple[str, Decimal, str, str | None], ...]:
    return tuple(sorted((q.name.casefold(), Decimal(q.value), q.unit.casefold(), q.period or "") for q in claim.fields.quantities))


def _known_identity(current: DraftClaim, hints: tuple[IdentityHint, ...]) -> tuple[IdentityHint, ...]:
    return tuple(h for h in hints if any(c.evidence_ref == h.evidence_ref and h.surface in c.quote for c in current.citations))


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
    a, b = current.fields, previous.fields
    if a.polarity != "unknown" and b.polarity != "unknown" and a.polarity != b.polarity:
        return False
    if a.mode != "unknown" and b.mode != "unknown" and a.mode != b.mode:
        return False
    if a.phase not in {None, "unknown"} and b.phase not in {None, "unknown"} and a.phase != b.phase:
        return False
    # Only compare normalized code values, not language-dependent period prose.
    qa = {(q.name.casefold(), q.unit.casefold(), q.period): Decimal(q.value) for q in a.quantities}
    qb = {(q.name.casefold(), q.unit.casefold(), q.period): Decimal(q.value) for q in b.quantities}
    if any(qa[key] != qb[key] for key in qa.keys() & qb.keys()):
        return False
    return True


def _claim_material(draft: DraftClaim) -> dict[str, Any]:
    fields = draft.fields.model_dump(mode="json")
    fields["quantities"] = sorted(
        ({**q.model_dump(mode="json"), "value": format(Decimal(q.value).normalize(), "f")} for q in draft.fields.quantities),
        key=canonical_json,
    )
    fields["conditions"] = sorted(set(draft.fields.conditions))
    fields["assets"] = sorted(fields["assets"], key=canonical_json)
    # Statement is presentation; it never changes the stable identity on its own.
    return fields


def validate_extraction(source: FrozenInput, extraction: Extraction) -> None:
    evidence = {e.ref: e for e in source.evidence}
    prior = {p.claim.ref for p in source.prior}
    targets = {r.ref for r in source.read_targets}
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


def _default_change(current: DraftClaim, previous: Claim, relation: str) -> str | None:
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
    return "new_fact" if relation == "adds_information" else None


class SemanticAnalyzer:
    def __init__(self, extractor: ClaimExtractor, judgments: NewsJudgments, *, topics: tuple[tuple[str, str], ...] = CODEBOOK) -> None:
        self.extractor, self.judgments, self.topics = extractor, judgments, topics
        self.identity = identity("semantic", "event_update_v1", extractor.identity, judgments.identity)

    async def extract(self, source: FrozenInput, budget: Budget) -> Extraction:
        result = await self.extractor.extract(source, extract_only=self.judgments.native is not None, timeout=budget.remaining())
        validate_extraction(source, result)
        return result

    async def understand(self, source: FrozenInput, extracted: Extraction, budget: Budget,
                         *, rebase_only: bool = False) -> Extraction:
        validate_extraction(source, extracted)
        result = extracted
        if self.judgments.native is not None and not rebase_only:
            mode_items = tuple(Question(item_id=c.slot, payload_json=canonical_json({"claim": c, "evidence": source.evidence})) for c in result.claims)
            modes = await self.judgments.judge("mode", mode_items, budget)
            phases = await self.judgments.judge("phase", mode_items, budget)
            mode_values, phase_values = {a.item_id: a for a in modes}, {a.item_id: a for a in phases}
            updated = []
            for claim in result.claims:
                mode, phase = mode_values[claim.slot], phase_values[claim.slot]
                if mode.status == "unavailable":
                    raise ProviderUnavailable("news_required_claim_mode_unavailable")
                values = claim.fields.model_dump(mode="json")
                values["mode"] = mode.value
                values["phase"] = None if phase.value == "not_applicable" else phase.value or "unknown"
                updated.append(DraftClaim.model_validate({**claim.model_dump(mode="json"), "fields": values}))
            result = Extraction.model_validate({**result.model_dump(mode="json"), "claims": [c.model_dump(mode="json") for c in updated]})

        # Current/prior candidates are already bounded by retrieval. No global
        # pair search; no title-only key for relation cache reuse.
        if self.judgments.native is not None and not rebase_only:
            topic_questions = tuple(Question(item_id=code, payload_json=canonical_json({"topic": label, "claims": result.claims})) for code, label in self.topics)
            topic_answers = await self.judgments.judge("topic", topic_questions, budget)
            result = Extraction.model_validate({**result.model_dump(mode="json"), "topics": project_topics(topic_answers, self.topics)})
        known = {(r.slot, r.previous_ref): r for r in result.relations}
        questions = []
        pairs: dict[str, tuple[DraftClaim, PriorClaim]] = {}
        for claim in result.claims:
            for prior in source.prior:
                key = (claim.slot, prior.claim.ref)
                if key in known:
                    continue
                item_id = identity("pair", claim.slot, prior.claim.ref)
                pairs[item_id] = claim, prior
                questions.append(Question(item_id=item_id, payload_json=canonical_json({
                    "current": claim, "previous": prior.claim,
                    "previous_content_revision": prior.content_revision,
                    "known_numeric_modal_mismatch": not equivalent_is_possible(claim, prior.claim, source.identity_hints),
                })))
        answers = await self.judgments.judge("relation", tuple(questions), budget)
        for answer in answers:
            claim, prior = pairs[answer.item_id]
            value = str(answer.value or "unresolved")
            known[(claim.slot, prior.claim.ref)] = RelationDraft.model_validate({
                "slot": claim.slot, "previous_ref": prior.claim.ref, "relation": value,
                "change_kind": _default_change(claim, prior.claim, value),
            })
        # One source/claim comparison, reused later. Native mode does not ask the
        # generator to validate successful Jev results a second time.
        supports = {(r.slot, r.evidence_ref): r for r in result.supports}
        support_items = []
        support_pairs: dict[str, tuple[str, str]] = {}
        ev = {item.ref: item for item in source.evidence}
        for claim in result.claims:
            for ref in dict.fromkeys(c.evidence_ref for c in claim.citations):
                if (claim.slot, ref) in supports:
                    continue
                key = identity("support", claim.slot, ref)
                support_pairs[key] = claim.slot, ref
                support_items.append(Question(item_id=key, payload_json=canonical_json({"claim": claim, "evidence": ev[ref]})))
        for answer in await self.judgments.judge("support", tuple(support_items), budget):
            slot, ref = support_pairs[answer.item_id]
            supports[(slot, ref)] = SupportDraft.model_validate({"slot": slot, "evidence_ref": ref, "relation": answer.value or "unresolved"})
        return Extraction.model_validate({**result.model_dump(mode="json"),
            "relations": [r.model_dump(mode="json") for r in known.values()],
            "supports": [r.model_dump(mode="json") for r in supports.values()],
        })


def assemble_update(source: FrozenInput, extraction: Extraction, head: EventUpdate | None,
                    *, adopted_at_ms: int) -> EventUpdate | None:
    """Return new substantive content or None; no reader/history/card input."""
    validate_extraction(source, extraction)
    if head is not None and source.event_id != head.event_id:
        raise ContractFault("news_head_event_mismatch")
    previous = {p.claim.ref: p for p in source.prior}
    if head is not None:
        previous.update({c.ref: PriorClaim(event_id=head.event_id, content_revision=head.content_revision, claim=c) for c in head.claims})
    evidence = {} if head is None else {e.ref: e for e in head.evidence}
    evidence.update({e.ref: e for e in source.evidence})
    claims = {} if head is None else {c.ref: c for c in head.claims}
    links = {} if head is None else {(r.claim_ref, r.evidence_ref): r for r in head.evidence_relations}
    retired = set() if head is None else set(head.retired_claim_refs)
    changes: list[Change] = []
    slot_refs: dict[str, str] = {}
    known_links: dict[str, list[RelationDraft]] = {}
    for relation in extraction.relations:
        known_links.setdefault(relation.slot, []).append(relation)
    for draft in extraction.claims:
        equivalent = [r for r in known_links.get(draft.slot, ()) if r.relation == "equivalent" and equivalent_is_possible(draft, previous[r.previous_ref].claim, source.identity_hints)]
        if any(r.relation == "equivalent" and not equivalent_is_possible(draft, previous[r.previous_ref].claim, source.identity_hints) for r in known_links.get(draft.slot, ())):
            # A contradicted equivalent is unresolved, never coverage proof. The
            # remaining additions/changes still remain available for adoption.
            equivalent = []
        # Prefer this Event's claim to an equivalent claim recalled from elsewhere.
        equivalent.sort(key=lambda r: (previous[r.previous_ref].event_id != source.event_id, r.previous_ref))
        material_relations = tuple(r for r in known_links.get(draft.slot, ()) if r.change_kind is not None)
        if material_relations:
            equivalent = []
        same = previous[equivalent[0].previous_ref] if equivalent else None
        material = _claim_material(draft)
        # A new real-world reversal can return to a previously seen numeric state.
        # Anchor this occurrence to its explicit predecessor and source, rather
        # than reusing an earlier same-shaped claim and silently losing the action.
        if material_relations:
            material["occurrence"] = {
                "previous": sorted(r.previous_ref for r in material_relations),
                "citations": sorted((c.evidence_ref, c.quote) for c in draft.citations),
            }
        ref = same.claim.ref if same is not None and same.event_id == source.event_id else identity("cl", source.event_id, same.claim.ref if same else material)
        slot_refs[draft.slot] = ref
        if ref not in claims:
            first = same.claim.first_available_at_ms if same else min(evidence[c.evidence_ref].source.first_available_at_ms for c in draft.citations)
            claims[ref] = Claim(ref=ref, statement=draft.statement, fields=same.claim.fields if same else draft.fields,
                citations=draft.citations, first_available_at_ms=first, known_identity=_known_identity(draft, source.identity_hints))
            if same is not None:
                changes.append(Change(kind="restatement", current_ref=ref, previous_ref=same.claim.ref,
                    previous_content_ref=identity("update", same.event_id, same.content_revision)))
            else:
                meaningful = [r for r in known_links.get(draft.slot, ()) if r.change_kind is not None]
                if not meaningful:
                    changes.append(Change(kind="new_fact", current_ref=ref))
                for relation in meaningful:
                    prior = previous[relation.previous_ref]
                    if relation.relation == "corrects" and relation.previous_ref in claims:
                        retired.add(relation.previous_ref)
                    kinds = {relation.change_kind}
                    if relation.relation == "real_world_change":
                        if draft.fields.phase != prior.claim.fields.phase:
                            kinds.add("phase_change")
                        if _quantity_key(draft) != _quantity_key(prior.claim):
                            kinds.add("parameter_change")
                    for kind in sorted(kinds):
                        changes.append(Change.model_validate({"kind": kind, "current_ref": ref,
                            "previous_ref": relation.previous_ref, "previous_content_ref": identity("update", prior.event_id, prior.content_revision)}))
        # Quote existence is not semantic support. Unresolved is explicit until
        # the single backend supplied a source relationship.
        support = {r.evidence_ref: r.relation for r in extraction.supports if r.slot == draft.slot}
        for citation in draft.citations:
            key = (ref, citation.evidence_ref)
            relationship = support.get(citation.evidence_ref, "unresolved")
            # A transient unavailable answer cannot degrade an adopted relationship.
            if key in links and relationship == "unresolved":
                continue
            new = EvidenceRelation(claim_ref=ref, evidence_ref=citation.evidence_ref, relation=relationship)
            old = links.get(key)
            if old == new:
                continue
            links[key] = new
            if head is not None and ref in {c.ref for c in head.claims}:
                changes.append(Change(kind="evidence_change", current_ref=ref, previous_ref=ref, previous_content_ref=head.ref))
    material = {
        "event_id": source.event_id,
        "claims": sorted(claims),
        "retired_claim_refs": sorted(retired),
        "evidence_relations": sorted((r.model_dump(mode="json") for r in links.values()), key=lambda x: (x["claim_ref"], x["evidence_ref"], x["relation"])),
    }
    revision = digest(material)
    if head is not None and revision == head.content_revision:
        return None
    return EventUpdate(
        event_id=source.event_id, input_revision=source.revision, content_revision=revision,
        previous_content_revision=None if head is None else head.content_revision, adopted_at_ms=adopted_at_ms,
        topics=tuple(sorted(set(extraction.topics))), claims=tuple(claims.values()), evidence=tuple(evidence.values()),
        evidence_relations=tuple(links.values()), retired_claim_refs=tuple(sorted(retired)), changes=tuple(dict.fromkeys(changes)),
        implications=tuple(Implication(claim_refs=tuple(slot_refs[s] for s in i.slots), channel=i.channel,
            explanation=i.explanation, conditions=i.conditions, origin=i.origin) for i in extraction.implications),
        open_questions=tuple(KnowledgeGap(question=g.question, claim_refs=tuple(slot_refs[s] for s in g.slots), target_ref=g.target_ref) for g in extraction.open_questions),
    )
