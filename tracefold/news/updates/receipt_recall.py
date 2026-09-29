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
# The only rule for lexical evidence: a shared term counts when at most this share of the 48 h window's sent
# receipts carry it (never fewer than one receipt); there is no word list. Function words are in nearly every
# receipt. On the 2026-09-29 window (1,321 receipts) 1 % is 13 receipts: it drops the words that filled
# unrelated claims' lists (reported 7.6 %, prices 6.4 %, market 4.4 %, trading 4.2 %, week 3.0 %) and the
# high-volume topics the asset route already carries (bitcoin 2.2 %, gold 2.0 %, eth 1.8 %), while specific
# terms (hyperliquid, insider 0.3 %; oversight, probe 0.2 %) still count.
LEXICAL_DF_MAX: Final = 0.01
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
# The SQL route extracts the same words from the same text, so both sides count the same terms.
WORD_PATTERN: Final = _WORD.pattern
_HAN = re.compile(r"[\u3400-\u9fff]+")
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
class RouteEvidence:
    """What the SQL routes found for one receipt: its rank in each bounded route and the qualifying query terms
    it shares (empty unless the lexical route matched)."""

    structure_rank: int | None = None
    lexical_rank: int | None = None
    lexical_terms: tuple[str, ...] = ()


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
    return frozenset(match.group().lower() for match in _WORD.finditer(text))


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


def lexical_df_cap(window: int) -> float:
    """The most receipts of a window of `window` receipts that may carry a term for it to count as evidence."""

    return max(1.0, LEXICAL_DF_MAX * window)


def _view(candidate: RecallCandidate) -> str:
    # Only the sent body and the claims actually carried by this receipt are searchable. A source
    # leader/head may mention an unpushed sibling and must not lend that fact to the body.
    return " ".join((candidate.body, *(claim.statement for claim in candidate.claims)))


def lexical_evidence(
    query: ClaimRecallQuery, window: tuple[RecallCandidate, ...]
) -> dict[str, tuple[int, tuple[str, ...]]]:
    """Per receipt of `window`, how many qualifying query terms it shares and which.

    A term qualifies when no more than `lexical_df_cap(len(window))` receipts of the window carry it. A receipt
    is evidence when it shares at least `LEXICAL_SHARED_MIN` qualifying English words or Han bigrams; the count
    is the larger of the two, the terms are those of each language that reached the minimum. The SQL route
    computes the same over the materialized 48 h window; a pure caller passes its own pool as the window.
    """

    views = {
        candidate.intent_id: (_words(text), _han_bigrams(text)) for candidate in window for text in (_view(candidate),)
    }
    cap = lexical_df_cap(len(window))
    rare_words = {word for word in query.words if sum(word in words for words, _ in views.values()) <= cap}
    rare_han = {pair for pair in query.han_bigrams if sum(pair in han for _, han in views.values()) <= cap}
    evidence: dict[str, tuple[int, tuple[str, ...]]] = {}
    for intent, (words, han) in views.items():
        kinds = [shared for shared in (rare_words & words, rare_han & han) if len(shared) >= LEXICAL_SHARED_MIN]
        if kinds:
            evidence[intent] = (max(len(shared) for shared in kinds), tuple(sorted(set().union(*kinds))))
    return evidence


def select_for_claim(
    query: ClaimRecallQuery,
    novelty: ReaderNovelty,
    candidates: tuple[RecallCandidate, ...],
    *,
    as_of_ms: int,
    routes: Mapping[str, RouteEvidence] | None = None,
) -> ClaimSelection:
    """Keep valid semantic representatives, then fuse bounded structural and lexical routes.

    `routes` is what the SQL found over the whole 48 h window. Without it the candidates within the window are
    the window: document frequency, both routes and their ranks are computed over them.
    """

    by_id = {candidate.intent_id: candidate for candidate in candidates if candidate.settled_at_ms < as_of_ms}
    linked = tuple(intent for intent in novelty.linked_intents if intent in by_id)
    ordinary = tuple(
        candidate for candidate in by_id.values() if as_of_ms - RECALL_WINDOW_MS <= candidate.settled_at_ms < as_of_ms
    )
    structure = {candidate.intent_id: _structure(query, candidate) for candidate in ordinary}
    lexical: dict[str, int]
    ranked_routes: tuple[tuple[str, tuple[tuple[RecallCandidate, int], ...]], ...]
    if routes is None:
        evidence = lexical_evidence(query, ordinary)
        lexical = {intent: len(terms) for intent, (_, terms) in evidence.items()}
        structural_rank = sorted(
            (candidate for candidate in ordinary if structure[candidate.intent_id]),
            key=lambda candidate: (-candidate.settled_at_ms, candidate.intent_id),
        )[:ROUTE_CANDIDATES_MAX]
        lexical_rank = sorted(
            (candidate for candidate in ordinary if candidate.intent_id in evidence),
            key=lambda candidate: (-evidence[candidate.intent_id][0], -candidate.settled_at_ms, candidate.intent_id),
        )[:ROUTE_CANDIDATES_MAX]
        ranked_routes = (
            ("structure", tuple((candidate, rank) for rank, candidate in enumerate(structural_rank, start=1))),
            ("lexical", tuple((candidate, rank) for rank, candidate in enumerate(lexical_rank, start=1))),
        )
    else:
        # SQL has already bounded each route, ranked it the same way and returned the qualifying terms.
        lexical = {intent: len(hit.lexical_terms) for intent, hit in routes.items()}
        ranked_routes = tuple(
            (
                route,
                tuple(
                    sorted(
                        (
                            (candidate, rank)
                            for candidate in ordinary
                            if (hit := routes.get(candidate.intent_id)) is not None
                            and (rank := (hit.structure_rank, hit.lexical_rank)[index]) is not None
                            and rank <= ROUTE_CANDIDATES_MAX
                            and (route == "structure" or lexical[candidate.intent_id] >= LEXICAL_SHARED_MIN)
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
