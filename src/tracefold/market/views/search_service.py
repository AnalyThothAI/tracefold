from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, cast

from tracefold.market.views.query_parser import SearchIntent, parse_search_query
from tracefold.market.views.search_aliases import (
    canonical_symbol_for_query,
    expanded_lexical_query,
    fuzzy_canonical_symbol_for_query,
    target_symbols_for_or_query,
)
from tracefold.market.windows import PRODUCT_WINDOW_MS
from tracefold.platform.validation import require_nonnegative_int

RRF_K = 60.0
ROUTE_WEIGHTS = {
    "target": 1.0,
    "handle": 0.85,
    "lexical": 0.65,
    "substring": 0.45,
}
_ROUTE_LIMIT = 500


@dataclass(frozen=True, slots=True)
class SearchPage:
    ok: bool
    query: dict[str, Any]
    items: list[dict[str, Any]] = field(default_factory=list)
    target_candidates: list[dict[str, Any]] = field(default_factory=list)
    page: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class SearchCursorError(Exception):
    pass


class SearchWindowError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _CursorState:
    phase: str
    sort_tuple: tuple[float, int, str] | None = None
    target_after: dict[str, Any] | None = None


class SearchService:
    def __init__(self, *, search_query: Any) -> None:
        self.search_query = search_query

    def search(
        self,
        query: str,
        *,
        limit: int,
        window: str,
        cursor: str | None = None,
        now_ms: int | None = None,
    ) -> SearchPage:
        requested_limit = require_nonnegative_int(limit, error_code="search_limit_required")
        since_ms = _since_ms(window=window, now_ms=now_ms)
        intent = parse_search_query(query)
        query_payload = _query_payload(intent) | {"window": window, "since_ms": since_ms}
        if intent.kind == "empty":
            return SearchPage(
                ok=False,
                query=query_payload,
                page=_fused_page([], requested_limit, has_more=False),
                error="empty_query",
            )
        cursor_state = _decode_cursor(cursor) if cursor else None
        target_intent = _target_intent(intent)
        target_candidates = self._resolve_target_candidates(intent=intent, target_intent=target_intent)
        lexical_query = expanded_lexical_query(intent, target_candidates)
        route_intent = replace(target_intent, lexical_query=lexical_query)
        if _resolved_targets(target_candidates) and (cursor_state is None or cursor_state.phase == "target"):
            return self._target_page(
                query_payload=query_payload | {"lexical_query": lexical_query},
                target_candidates=target_candidates,
                limit=requested_limit,
                cursor_state=cursor_state,
                since_ms=since_ms,
            )
        route_hits = self.search_query.route_hits(
            intent=route_intent,
            target_candidates=target_candidates,
            route_limit=_route_limit(lexical_query=lexical_query, requested_limit=requested_limit),
            since_ms=since_ms,
        )
        items = _items_from_hits(route_hits)
        if cursor_state is not None:
            if cursor_state.phase != "fused" or cursor_state.sort_tuple is None:
                raise SearchCursorError("invalid_cursor")
            items = [item for item in items if _sort_tuple(item) > cursor_state.sort_tuple]
        page_items = items[: requested_limit + 1]
        has_more = len(page_items) > requested_limit
        returned = page_items[:requested_limit]
        return SearchPage(
            ok=True,
            query=query_payload | {"lexical_query": lexical_query},
            items=[_public_item(item) for item in returned],
            target_candidates=target_candidates,
            page=_fused_page(returned, requested_limit, has_more=has_more),
        )

    def _target_page(
        self,
        *,
        query_payload: dict[str, Any],
        target_candidates: list[dict[str, Any]],
        limit: int,
        cursor_state: _CursorState | None,
        since_ms: int,
    ) -> SearchPage:
        after = cursor_state.target_after if cursor_state else None
        hits = self.search_query.target_hits_page(
            target_candidates,
            limit=limit + 1,
            after=after,
            since_ms=since_ms,
        )
        items = _items_from_hits(hits)
        page_items = items[: limit + 1]
        has_more = len(page_items) > limit
        returned = page_items[:limit]
        return SearchPage(
            ok=True,
            query=query_payload,
            items=[_public_item(item) for item in returned],
            target_candidates=target_candidates,
            page=_target_page_payload(returned, limit, has_more=has_more),
        )

    def _resolve_target_candidates(self, *, intent: SearchIntent, target_intent: SearchIntent) -> list[dict[str, Any]]:
        or_symbols = target_symbols_for_or_query(intent.normalized_text)
        if or_symbols:
            candidates = cast(list[dict[str, Any]], self.search_query.resolve_symbols(or_symbols))
            resolved = _resolved_targets(candidates)
            if resolved:
                return _dedupe_candidates(candidates)
        candidates = cast(list[dict[str, Any]], self.search_query.resolve_targets(target_intent))
        if not _resolved_targets(candidates) and intent.kind in {"symbol", "text"}:
            fuzzy_symbol = fuzzy_canonical_symbol_for_query(intent.normalized_text or intent.text)
            if fuzzy_symbol and fuzzy_symbol != target_intent.symbol:
                target_intent = replace(target_intent, kind="symbol", symbol=fuzzy_symbol)
                candidates = cast(list[dict[str, Any]], self.search_query.resolve_targets(target_intent))
        return candidates


def _target_intent(intent: SearchIntent) -> SearchIntent:
    alias_symbol = canonical_symbol_for_query(intent.normalized_text)
    if alias_symbol and intent.kind in {"symbol", "text"}:
        return replace(intent, kind="symbol", symbol=alias_symbol)
    return intent


def _since_ms(*, window: str, now_ms: int | None) -> int:
    resolved_now_ms = int(now_ms or time.time() * 1000)
    try:
        window_ms = PRODUCT_WINDOW_MS[window]
    except KeyError as exc:
        raise SearchWindowError(window) from exc
    return resolved_now_ms - window_ms


def _resolved_targets(target_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [candidate for candidate in target_candidates if str(candidate.get("status") or "") == "resolved"]


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for candidate in candidates:
        key = (str(candidate.get("target_type") or ""), str(candidate.get("target_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _route_limit(*, lexical_query: str, requested_limit: int) -> int:
    if _substring_like_query(lexical_query):
        return max(requested_limit + 1, min(_ROUTE_LIMIT, 50))
    return max(requested_limit + 1, _ROUTE_LIMIT)


def _substring_like_query(query: str) -> bool:
    normalized = query.strip()
    return 4 <= len(normalized) <= 32 and normalized.replace("_", "").isalnum() and normalized.isascii()


def _items_from_hits(route_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_event: dict[str, list[dict[str, Any]]] = {}
    for hit in route_hits:
        by_event.setdefault(str(hit["event_id"]), []).append(hit)
    items = [_item_from_event_hits(hits) for hits in by_event.values()]
    return sorted(items, key=_sort_tuple)


def _item_from_event_hits(hits: list[dict[str, Any]]) -> dict[str, Any]:
    first = hits[0]
    route_scores: dict[str, float] = {}
    reasons: list[str] = []
    target = None
    for hit in hits:
        route = str(hit["route"])
        route_scores[route] = max(route_scores.get(route, 0.0), float(hit.get("route_score") or 0.0))
        reasons.extend(str(reason) for reason in hit.get("match_reasons") or [])
        if target is None and route == "target":
            target = hit.get("target")
    score = _fused_score(hits)
    return {
        "event": first["event"],
        "match_type": _match_type(hits),
        "score": score,
        "match_reasons": list(dict.fromkeys(reasons)),
        "target": target,
        "route_scores": route_scores,
        "_target_cursor": _target_cursor(hits),
        "_sort": {
            "rank_score": score,
            "received_at_ms": int(first.get("received_at_ms") or 0),
            "event_id": str(first["event_id"]),
        },
    }


def _target_cursor(hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    for hit in hits:
        if str(hit.get("route")) == "target":
            return {
                "status_rank": int(hit.get("target_status_rank") or 0),
                "received_at_ms": int(hit.get("received_at_ms") or 0),
                "event_id": str(hit["event_id"]),
            }
    return None


def _fused_score(route_hits: list[dict[str, Any]]) -> float:
    return sum(ROUTE_WEIGHTS[str(hit["route"])] / (RRF_K + max(1, int(hit["route_rank"]))) for hit in route_hits)


def _match_type(hits: list[dict[str, Any]]) -> str:
    routes = {str(hit["route"]) for hit in hits}
    if "target" in routes:
        return "target"
    if "handle" in routes:
        return "handle"
    if "lexical" in routes:
        return "lexical"
    return "substring"


def _sort_tuple(item: dict[str, Any]) -> tuple[float, int, str]:
    sort = item["_sort"]
    return (-float(sort["rank_score"]), -int(sort["received_at_ms"]), str(sort["event_id"]))


def _target_page_payload(items: list[dict[str, Any]], limit: int, *, has_more: bool) -> dict[str, Any]:
    next_cursor = _encode_target_cursor(items[-1]) if has_more and items else None
    return {
        "returned_count": min(len(items), limit),
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def _fused_page(items: list[dict[str, Any]], limit: int, *, has_more: bool) -> dict[str, Any]:
    next_cursor = _encode_fused_cursor(items[-1]) if has_more and items else None
    return {
        "returned_count": min(len(items), limit),
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in {"_sort", "_target_cursor"}}


def _encode_fused_cursor(item: dict[str, Any]) -> str:
    sort = item["_sort"]
    return f"fused:{float(sort['rank_score']):.17g}:{int(sort['received_at_ms'])}:{sort['event_id']}"


def _encode_target_cursor(item: dict[str, Any]) -> str:
    cursor = item.get("_target_cursor")
    if not cursor:
        raise SearchCursorError("invalid_cursor")
    return f"target:{int(cursor['status_rank'])}:{int(cursor['received_at_ms'])}:{cursor['event_id']}"


def _decode_cursor(cursor: str | None) -> _CursorState:
    if not cursor:
        raise SearchCursorError("invalid_cursor")
    try:
        phase, rest = cursor.split(":", 1)
        if phase == "target":
            status_rank_raw, received_raw, event_id = rest.split(":", 2)
            return _CursorState(
                phase="target",
                target_after={
                    "status_rank": int(status_rank_raw),
                    "received_at_ms": int(received_raw),
                    "event_id": event_id,
                },
            )
        if phase != "fused":
            raise ValueError("unsupported cursor phase")
        score_raw, received_raw, event_id = rest.split(":", 2)
        return _CursorState(
            phase="fused",
            sort_tuple=(-float(score_raw), -int(received_raw), event_id),
        )
    except (TypeError, ValueError) as exc:
        raise SearchCursorError("invalid_cursor") from exc


def _query_payload(intent: SearchIntent) -> dict[str, Any]:
    return {key: value for key, value in asdict(intent).items() if value not in {None, ""}}
