"""Assemble immutable adopted content and preserve proposition occurrence identity."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ..entities import CRYPTO_QUOTE_SUFFIXES
from .contracts import (
    Change,
    ChangeKind,
    Claim,
    DraftClaim,
    EventUpdate,
    Evidence,
    EvidenceRelation,
    Extraction,
    FrozenInput,
    Implication,
    KnowledgeGap,
    Phase,
    PriorClaim,
    Quantity,
    RelationDraft,
    content_material,
    content_revision_for,
    current_evidence,
    semantic_state,
)
from .extraction import validate_extraction
from .identity import canonical_json, digest, identity
from .judgment import (
    ContractFault,
)

log = logging.getLogger("tracefold.news")


QuantityKey = tuple[tuple[str, Decimal, str, str], ...]


def _quantity_key(claim: DraftClaim | Claim) -> QuantityKey:
    return tuple(
        sorted(
            (quantity.name.casefold(), Decimal(quantity.value), quantity.unit.casefold(), quantity.period or "")
            for quantity in claim.fields.quantities
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


def different_listing_assets(current: DraftClaim | Claim, previous: Claim) -> bool:
    """Two explicitly quoted crypto listings name different tickers or different same-quote contracts.

    This narrow object mismatch uses literal source spellings, never catalogue aliases or a chosen price
    venue. A ticker versus a pair, missing tags, or a free-text object difference remains unknown.
    """

    symbols = []
    for claim in (current, previous):
        if not re.search(
            r"\b(?:list(?:s|ed|ing)?|delist(?:s|ed|ing)?)\b|上币|上线|挂牌|下架", claim.fields.action, re.I
        ):
            return False
        primary = [asset for asset in claim.fields.assets if asset.role == "primary"]
        if len(primary) != 1 or primary[0].market_type != "crypto":
            return False
        symbol = primary[0].symbol.strip().removeprefix("$")
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,14}", symbol):
            return False
        symbols.append(symbol)
    left, right = symbols
    if left == right:
        return False
    # Pair names of the same explicit quote are comparable; otherwise both must be bare tickers.
    quotes = [next((quote for quote in CRYPTO_QUOTE_SUFFIXES if symbol.endswith(quote)), None) for symbol in symbols]
    if quotes[0] != quotes[1]:
        return False
    for claim, own, other in ((current, left, right), (previous, right, left)):
        text = " ".join(citation.quote for citation in claim.citations)
        if not re.search(rf"(?<![A-Za-z0-9])\$?{re.escape(own)}(?![A-Za-z0-9])", text):
            return False
        if re.search(rf"(?<![A-Za-z0-9])\$?{re.escape(other)}(?![A-Za-z0-9])", text):
            return False
    return True


def proven_mismatches(current: DraftClaim, previous: Claim) -> tuple[str, ...]:
    """Return only differences established by comparable evidence, never prose inequality.

    An empty result means unknown, not proven equivalent. The same result is sent to the relation
    model and used to veto an equivalent answer.
    """

    mismatches: list[str] = []
    if different_listing_assets(current, previous):
        mismatches.append("listing_asset")
    a = current.fields
    b = previous.fields
    if "unknown" not in {a.polarity, b.polarity} and a.polarity != b.polarity:
        mismatches.append("polarity")
    realizations = (
        {"observation", "decision"},
        {"assertion", "demand", "threat", "commitment", "guidance", "forecast", "opinion"},
        {"promotion"},
    )
    left_class = next((index for index, group in enumerate(realizations) if a.mode in group), None)
    right_class = next((index for index, group in enumerate(realizations) if b.mode in group), None)
    if left_class is not None and right_class is not None and left_class != right_class:
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
    # ("Nvidia" and "Nvidia Corp" are one issuer); free text supplies no proven entity identity.
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
    del fields["actor_role"]
    return fields


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


def relation_change(current: DraftClaim, previous: Claim, relation: str) -> ChangeKind | None:
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
        and not proven_mismatches(draft, previous[row.previous_ref].claim)
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
            and not proven_mismatches(draft, previous[row.previous_ref].claim)
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
        relations = [
            RelationDraft(slot=row.slot, previous_ref=row.previous_ref, relation="unrelated")
            if row.relation in {"equivalent", "adds_information", "real_world_change"}
            and different_listing_assets(draft, previous[row.previous_ref].claim)
            else row
            for row in relations_by_slot.get(draft.slot, [])
        ]
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
