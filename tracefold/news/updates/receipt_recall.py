"""Claim-scoped recall of exact sent receipt bodies.

Retrieval evidence chooses context, never establishes that a fact was already reported. The
semantic graph alone supplies novelty; the reader compares the selected bodies themselves.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from ..events.grounding import COMMODITY_CONTEXT
from ..market_review.instruments import ALIAS_SEEDS, resolve_base_symbol
from .contracts import Claim
from .identity import digest
from .reader_judgments import READER_INPUT_VERSION, READER_MESSAGES_MAX, LinkedReceipt, ReaderNovelty

RECALL_POLICY: Final = "claim_receipts_v1"
RECALL_WINDOW_MS: Final = 48 * 60 * 60 * 1000
LINKED_RECEIPT_WINDOW_MS: Final = 48 * 60 * 60 * 1000
ROUTE_CANDIDATES_MAX: Final = 32
RRF_K: Final = 60
LEXICAL_SHARED_MIN: Final = 2
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
_HAN = re.compile(r"[\u3400-\u9fff]+")
# English function words only: PostgreSQL's `english` text-search stop words of three or more letters, which
# the SQL lexical route already ignores, plus the modals `would`/`could`. A domain word such as "market" or
# "price" is content: whether it is shared evidence is the route's question, not this list's.
_STOP: Final = frozenset(
    {
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "and",
        "any",
        "are",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "can",
        "could",
        "does",
        "did",
        "doing",
        "don",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "into",
        "its",
        "itself",
        "just",
        "more",
        "most",
        "myself",
        "nor",
        "not",
        "now",
        "off",
        "once",
        "only",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "too",
        "under",
        "until",
        "very",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
    }
)
# Subjects and objects that name no one in particular; equal text on them is no shared identity.
_GENERIC_ENTITY: Final = frozenset({"market", "markets", "data", "government", "company", "people"})
# Asset spellings resolve through the owners that already ground them: ticker aliases from the instrument
# catalogue's seeds (`XAU`/`XAUT` -> `GOLD`, `XAG` -> `SILVER`, `WTI` -> `CL`), and a commodity written as a word
# in any language through the Gate's grounding table, whose keys are catalogue commodities. The alias map is
# closed over hops so the SQL route applies it with one lookup.
ASSET_ALIASES: Final[Mapping[str, str]] = {alias: resolve_base_symbol(alias) for alias in ALIAS_SEEDS}
_COMMODITY_NAMES: Final = tuple((resolve_base_symbol(tag), pattern) for tag, pattern in COMMODITY_CONTEXT.items())
# Leading `$` (cashtags) and surrounding whitespace carry no identity. The SQL route strips the same edges.
_SYMBOL_EDGES: Final = re.compile(r"^[\s$]+|\s+$")


def asset_symbols(symbol: str, market_type: str) -> frozenset[str]:
    """Every canonical symbol one claim asset names; two assets are the same when these sets meet.

    `$OKLO` is `OKLO`, `xyz:GOLD` and `XAU` are `GOLD`, and a commodity named in words (`spot gold`,
    `国际现货黄金`, `现货白银`) is the catalogue commodity the grounding table recognises in it. Anything
    else keeps its own normalized spelling, so an unknown name still matches the same name exactly.
    """

    text = _SYMBOL_EDGES.sub("", symbol)
    names = {resolve_base_symbol(text)}
    if market_type == "commodity":
        names.update(base for base, pattern in _COMMODITY_NAMES if pattern.search(text))
    return frozenset(name for name in names if name)


def commodity_name_patterns(symbol: str) -> tuple[str, ...]:
    """The grounding patterns that name commodity `symbol`, spelled for PostgreSQL's `~*` (`\\y`, not `\\b`)."""

    return tuple(
        sorted({pattern.pattern.replace(r"\b", r"\y") for base, pattern in _COMMODITY_NAMES if base == symbol})
    )


@dataclass(frozen=True, slots=True)
class ClaimRecallQuery:
    ref: str
    subject: str
    object: str
    primary_assets: frozenset[tuple[str, str]]
    mentioned_assets: frozenset[tuple[str, str]]
    known_identity: frozenset[tuple[str, str]]
    words: frozenset[str]
    han_bigrams: frozenset[str]


@dataclass(frozen=True, slots=True)
class RecallCandidate:
    intent_id: str
    payload_sha256: str
    body: str
    settled_at_ms: int
    claims: tuple[Claim, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimSelection:
    intent_ids: tuple[str, ...]
    # These reasons are diagnostic and do not imply fact coverage.
    reasons: tuple[tuple[str, tuple[str, ...]], ...]


def _asset_pairs(claim: Claim, role: str) -> frozenset[tuple[str, str]]:
    return frozenset(
        (symbol, asset.market_type)
        for asset in claim.fields.assets
        if asset.role == role
        for symbol in asset_symbols(asset.symbol, asset.market_type)
    )


def _words(text: str) -> frozenset[str]:
    return frozenset(word for match in _WORD.finditer(text) if (word := match.group().lower()) not in _STOP)


def _han_bigrams(text: str) -> frozenset[str]:
    return frozenset(segment[index : index + 2] for segment in _HAN.findall(text) for index in range(len(segment) - 1))


def query_for_claim(claim: Claim) -> ClaimRecallQuery:
    fields = claim.fields
    text = " ".join((claim.statement, fields.subject, fields.action, fields.object))
    return ClaimRecallQuery(
        ref=claim.ref,
        subject="" if fields.subject.strip().casefold() in _GENERIC_ENTITY else fields.subject.strip().casefold(),
        object="" if fields.object.strip().casefold() in _GENERIC_ENTITY else fields.object.strip().casefold(),
        primary_assets=_asset_pairs(claim, "primary"),
        mentioned_assets=_asset_pairs(claim, "mentioned"),
        known_identity=frozenset(
            (hint.key, hint.value.strip().casefold())
            for hint in claim.known_identity
            if hint.key in {"subject_id", "object_id"} and hint.value.strip()
        ),
        words=_words(text),
        han_bigrams=_han_bigrams(text),
    )


def _structure(query: ClaimRecallQuery, candidate: RecallCandidate) -> tuple[str, ...]:
    reasons: set[str] = set()
    for claim in candidate.claims:
        fields = claim.fields
        primary = _asset_pairs(claim, "primary")
        mentioned = _asset_pairs(claim, "mentioned")
        if query.primary_assets & primary:
            reasons.update(f"primary_asset:{market}:{symbol}" for symbol, market in query.primary_assets & primary)
        elif query.primary_assets & mentioned or query.mentioned_assets & primary:
            reasons.add("asset_role_cross")
        for hint in claim.known_identity:
            if (hint.key, hint.value.strip().casefold()) in query.known_identity:
                reasons.add(f"identity:{hint.key}:{hint.value.strip().casefold()}")
        if (
            query.subject
            and query.subject not in _GENERIC_ENTITY
            and query.subject == fields.subject.strip().casefold()
        ):
            reasons.add(f"subject:{query.subject}")
        if query.object and query.object not in _GENERIC_ENTITY and query.object == fields.object.strip().casefold():
            reasons.add(f"object:{query.object}")
    return tuple(sorted(reasons))


def _lexical(query: ClaimRecallQuery, candidate: RecallCandidate) -> int:
    # Only the sent body and the claims actually carried by this receipt are searchable. A source
    # leader/head may mention an unpushed sibling and must not lend that fact to the body.
    view = " ".join((candidate.body, *(claim.statement for claim in candidate.claims)))
    english = len(query.words & _words(view))
    chinese = len(query.han_bigrams & _han_bigrams(view))
    return max(english, chinese) if max(english, chinese) >= LEXICAL_SHARED_MIN else 0


def select_for_claim(
    query: ClaimRecallQuery,
    novelty: ReaderNovelty,
    candidates: tuple[RecallCandidate, ...],
    *,
    as_of_ms: int,
    route_ranks: Mapping[str, tuple[int | None, int | None]] | None = None,
) -> ClaimSelection:
    """Keep valid semantic representatives, then fuse bounded structural and lexical routes."""

    by_id = {candidate.intent_id: candidate for candidate in candidates if candidate.settled_at_ms < as_of_ms}
    linked = tuple(intent for intent in novelty.linked_intents if intent in by_id)
    ordinary = tuple(
        candidate for candidate in by_id.values() if as_of_ms - RECALL_WINDOW_MS <= candidate.settled_at_ms < as_of_ms
    )
    structure = {candidate.intent_id: _structure(query, candidate) for candidate in ordinary}
    lexical = {candidate.intent_id: _lexical(query, candidate) for candidate in ordinary}
    ranked_routes: tuple[tuple[str, tuple[tuple[RecallCandidate, int], ...]], ...]
    if route_ranks is None:
        # Pure fixture evaluation uses the same bounded routes without a PostgreSQL rank.
        structural_rank = sorted(
            (candidate for candidate in ordinary if structure[candidate.intent_id]),
            key=lambda candidate: (-candidate.settled_at_ms, candidate.intent_id),
        )[:ROUTE_CANDIDATES_MAX]
        lexical_rank = sorted(
            (candidate for candidate in ordinary if lexical[candidate.intent_id]),
            key=lambda candidate: (-lexical[candidate.intent_id], -candidate.settled_at_ms, candidate.intent_id),
        )[:ROUTE_CANDIDATES_MAX]
        ranked_routes = (
            ("structure", tuple((candidate, rank) for rank, candidate in enumerate(structural_rank, start=1))),
            ("lexical", tuple((candidate, rank) for rank, candidate in enumerate(lexical_rank, start=1))),
        )
    else:
        # SQL has already bounded each route and ranked lexical matches with ts_rank_cd.
        ranked_routes = tuple(
            (
                route,
                tuple(
                    sorted(
                        (
                            (candidate, rank)
                            for candidate in ordinary
                            if (ranks := route_ranks.get(candidate.intent_id)) is not None
                            and (rank := ranks[index]) is not None
                            and rank <= ROUTE_CANDIDATES_MAX
                            and (route == "structure" or lexical[candidate.intent_id] > 0)
                        ),
                        key=lambda pair: pair[1],
                    )
                ),
            )
            for index, route in enumerate(("structure", "lexical"))
        )
    scores: dict[str, float] = {}
    reasons: dict[str, set[str]] = {}
    for route, ranked in ranked_routes:
        for candidate, rank in ranked:
            scores[candidate.intent_id] = scores.get(candidate.intent_id, 0.0) + 1 / (RRF_K + rank)
            details = (
                structure[candidate.intent_id]
                if route == "structure"
                else (f"lexical_shared:{lexical[candidate.intent_id]}",)
            )
            reasons.setdefault(candidate.intent_id, set()).update((route, *details))
    ranked_ids = sorted(
        scores,
        key=lambda intent: (-scores[intent], -by_id[intent].settled_at_ms, intent),
    )
    selected = tuple(dict.fromkeys((*linked, *ranked_ids)))[:READER_MESSAGES_MAX]
    return ClaimSelection(
        intent_ids=selected,
        reasons=tuple((intent, tuple(sorted(reasons.get(intent, {"semantic"})))) for intent in selected),
    )


def reader_context_revision(
    update_ref: str,
    selections: dict[str, ClaimSelection],
    novelties: dict[str, ReaderNovelty],
    candidates: tuple[RecallCandidate, ...],
    linked_receipts: tuple[LinkedReceipt, ...],
    *,
    blocked: tuple[str, ...],
    ambiguous: tuple[str, ...],
    invalidated: tuple[str, ...],
    watch_symbols: tuple[str, ...],
    protected_listing: tuple[str, ...] = (),
) -> str:
    """Hash exactly the claim-scoped input and state facts used to make a reader decision."""

    by_id = {row.intent_id: row for row in candidates}
    states = {row.intent_id: row.state for row in linked_receipts}
    material = {
        "policy": RECALL_POLICY,
        "input_version": READER_INPUT_VERSION,
        "update_ref": update_ref,
        "claims": [
            {
                "ref": ref,
                "novelty": novelties[ref].model_dump(mode="json"),
                "messages": [(intent, by_id[intent].payload_sha256) for intent in selections[ref].intent_ids],
                "linked_states": sorted((intent, states.get(intent)) for intent in novelties[ref].linked_intents),
            }
            for ref in sorted(selections)
        ],
        "blocked": sorted(blocked),
        "ambiguous": sorted(ambiguous),
        "invalidated": sorted(invalidated),
        "watch": sorted(watch_symbols),
        "protected_listing": sorted(protected_listing),
    }
    return f"reader_v3:{digest(material)}"
