from __future__ import annotations

from typing import Any

from tracefold.platform.validation import require_positive_int

from .search_service import SearchService
from .token_case_service import TokenCaseService


class SearchInspectService:
    def __init__(
        self,
        *,
        search_query: Any,
        targets: Any,
        market_candles: Any | None = None,
    ) -> None:
        self.search_query = search_query
        self.targets = targets
        self.market_candles = market_candles

    def inspect(
        self,
        q: str,
        *,
        window: str,
        limit: int,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        parsed_limit = require_positive_int(limit, error_code="search_inspect_limit_required")
        search_page = SearchService(search_query=self.search_query).search(
            q,
            limit=parsed_limit,
            window=window,
            now_ms=now_ms,
        )
        candidates = list(search_page.target_candidates)
        resolved = [candidate for candidate in candidates if str(candidate.get("status") or "") == "resolved"]
        selected = resolved[0] if len(resolved) == 1 else None
        result_kind = _result_kind(search_ok=search_page.ok, selected=selected, candidates=candidates)
        payload: dict[str, Any] = {
            "query": {
                "q": q.strip(),
                "normalized_q": str(
                    search_page.query.get("normalized_text") or search_page.query.get("text") or q
                ).strip(),
                "window": window,
                "result_kind": result_kind,
            },
            "resolver": {
                "target_candidates": candidates,
                "selected_target": selected,
                "reasons": _resolver_reasons(result_kind=result_kind, candidates=candidates),
            },
            "token_result": None,
            "topic_result": None,
            "ambiguous_result": None,
        }
        if result_kind == "empty_result":
            return payload
        if result_kind == "token_result" and selected:
            payload["token_result"] = self._token_result(
                selected=selected,
                window=window,
                now_ms=now_ms,
                limit=parsed_limit,
            )
            return payload
        topic_result = self._topic_result(query=q.strip(), items=search_page.items)
        if result_kind == "ambiguous_result":
            payload["ambiguous_result"] = {
                "candidates": candidates,
                "summary": topic_result["summary"],
                "items": topic_result["items"],
            }
            return payload
        payload["topic_result"] = topic_result
        return payload

    def _token_result(
        self,
        *,
        selected: dict[str, Any],
        window: str,
        now_ms: int | None,
        limit: int,
    ) -> dict[str, Any]:
        target_type = str(selected["target_type"])
        target_id = str(selected["target_id"])
        return TokenCaseService(
            targets=self.targets,
            market_candles=self.market_candles,
        ).dossier(
            target_type=target_type,
            target_id=target_id,
            window=window,
            posts_limit=min(limit, 50),
            now_ms=now_ms,
        )

    def _topic_result(self, *, query: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "summary": _topic_summary(items),
            "items": items,
        }


def _result_kind(
    *,
    search_ok: bool,
    selected: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
) -> str:
    if not search_ok:
        return "empty_result"
    if selected is not None:
        return "token_result"
    if candidates:
        return "ambiguous_result"
    return "topic_result"


def _resolver_reasons(*, result_kind: str, candidates: list[dict[str, Any]]) -> list[str]:
    if result_kind == "token_result":
        return ["one_resolved_target", "route_backed_result"]
    if result_kind == "ambiguous_result":
        return [f"{len(candidates)}_candidate_targets", "no_auto_selection"]
    if result_kind == "topic_result":
        return ["no_unique_target", "keyword_corpus_result"]
    return ["empty_query"]


def _topic_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    authors = {
        str(_dict(item.get("event")).get("author_handle") or "")
        for item in items
        if _dict(item.get("event")).get("author_handle")
    }
    return {"posts": len(items), "authors": len(authors)}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
