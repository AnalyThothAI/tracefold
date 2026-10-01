"""Read bounded semantic links against durable sent, ambiguous and in-flight receipt state."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Final, Literal

from pydantic import Field

from ..updates.contracts import Exact, Relation

Novelty = Literal["known", "increment", "development", "in_flight", "unlinked"]
# Relations that say something about what the reader already has. `conflicts` names no order, so it leaves the
# claim to the questions; `unrelated` and `unresolved` are not links.
_FORWARD: Final[dict[Relation, Novelty]] = {
    "equivalent": "known",
    "adds_information": "increment",
    "real_world_change": "development",
    "corrects": "development",
}
# Read from the older claim's side, every such link means the reader holds the same or a later account.
_REVERSE: Final[dict[Relation, Novelty]] = dict.fromkeys(_FORWARD, "known")
_NOVELTY_ORDER: Final[tuple[Novelty, ...]] = ("known", "in_flight", "development", "increment")


class ClaimLink(Exact):
    """One semantic link the semantic layer asserted when it adopted `current_ref` (newer) over `previous_ref`."""

    current_ref: str
    previous_ref: str
    relation: Relation
    asserted_at_ms: int = Field(ge=0)


class LinkedReceipt(Exact):
    """A receipt carrying claims the reader may already hold. An ambiguous send may have reached the reader."""

    intent_id: str
    state: Literal["sent", "ambiguous", "sending"]
    claim_refs: tuple[str, ...]
    settled_at_ms: int | None = None


class ReaderNovelty(Exact):
    novelty: Novelty
    # The receipt the class was read against, when it settled, and the links from the claim to one of its claims.
    intent_id: str | None = None
    settled_at_ms: int | None = None
    path: tuple[ClaimLink, ...] = ()
    # Every delivered receipt a link reaches, strongest first: these lead the claim's messages.
    linked_intents: tuple[str, ...] = ()


def current_links(links: Iterable[ClaimLink]) -> tuple[ClaimLink, ...]:
    """One link per pair of claims: the latest assertion about the pair wins, whichever claim it was made from.

    A pair that became a conflict stops being an increment, and two revisions that each claim to add to the
    other resolve to the later one. A revision that merely omits a pair does not retract it.
    """

    latest: dict[frozenset[str], ClaimLink] = {}
    for link in sorted(links, key=lambda row: (row.asserted_at_ms, row.current_ref, row.previous_ref, row.relation)):
        if link.current_ref != link.previous_ref:
            latest[frozenset((link.current_ref, link.previous_ref))] = link
    return tuple(latest.values())


def reader_novelty(claim_ref: str, links: Iterable[ClaimLink], receipts: Iterable[LinkedReceipt]) -> ReaderNovelty:
    """Classify one claim against what the reader holds, following at most two links.

    A two-link path passes through an `equivalent` claim, so it carries exactly one directed relation. A claim
    a delivered receipt carries itself is known.
    """

    adjacent: dict[str, list[tuple[str, ClaimLink, bool]]] = defaultdict(list)
    for link in current_links(links):
        adjacent[link.current_ref].append((link.previous_ref, link, True))
        adjacent[link.previous_ref].append((link.current_ref, link, False))
    by_claim: dict[str, list[LinkedReceipt]] = defaultdict(list)
    for receipt in receipts:
        for ref in receipt.claim_refs:
            by_claim[ref].append(receipt)

    def reading(link: ClaimLink, forward: bool) -> Novelty | None:
        return (_FORWARD if forward else _REVERSE).get(link.relation)

    paths: list[tuple[Novelty, tuple[ClaimLink, ...], str]] = [("known", (), claim_ref)]
    for middle, first, forward in adjacent[claim_ref]:
        one = reading(first, forward)
        if one is not None:
            paths.append((one, (first,), middle))
        for target, second, onward in adjacent[middle]:
            if target == claim_ref:
                continue
            two = reading(second, onward)
            if one is None or two is None or "equivalent" not in (first.relation, second.relation):
                continue
            paths.append((two if first.relation == "equivalent" else one, (first, second), target))
    found: list[tuple[int, int, int, str, ReaderNovelty]] = []
    for novelty, path, target in paths:
        for receipt in by_claim.get(target, ()):
            label: Novelty = "in_flight" if receipt.state == "sending" else novelty
            found.append(
                (
                    _NOVELTY_ORDER.index(label),
                    len(path),
                    -(receipt.settled_at_ms or 0),
                    receipt.intent_id,
                    ReaderNovelty(
                        novelty=label, intent_id=receipt.intent_id, settled_at_ms=receipt.settled_at_ms, path=path
                    ),
                )
            )
    if not found:
        return ReaderNovelty(novelty="unlinked")
    found.sort(key=lambda row: row[:4])
    delivered = tuple(
        dict.fromkeys(row[4].intent_id for row in found if row[4].novelty != "in_flight" and row[4].intent_id)
    )
    return found[0][4].model_copy(update={"linked_intents": delivered})


Render = Literal["full", "increment", "correction"]
