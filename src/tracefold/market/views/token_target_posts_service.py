from __future__ import annotations

import time
from typing import Any

from tracefold.market.windows import PRODUCT_WINDOW_MS
from tracefold.platform.validation import require_nonnegative_int

from .token_target_cursor import TokenTargetCursorError, decode_target_cursor, encode_target_cursor
from .token_target_post_serializer import token_target_post_payload
from .token_target_stage_builder import build_token_target_stages


class TokenTargetPostsCursorError(Exception):
    pass


class TokenTargetPostsRangeError(Exception):
    pass


class TokenTargetPostsQueryError(Exception):
    pass


class TokenTargetPostsWindowError(ValueError):
    pass


class TokenTargetPostsService:
    def __init__(self, *, targets: Any) -> None:
        self.targets = targets

    def target_posts(
        self,
        *,
        target_type: str,
        target_id: str,
        window: str,
        post_range: str,
        limit: int,
        cursor: str | None = None,
        event_id: str | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        row_limit = require_nonnegative_int(limit, error_code="token_target_posts_limit_required")
        if post_range not in {"current_window", "since_ignition", "all_history"}:
            raise TokenTargetPostsRangeError(post_range)
        requested_event_id = str(event_id or "").strip()
        if requested_event_id:
            if cursor:
                raise TokenTargetPostsQueryError("event_id_cursor_incompatible")
            rows = self.targets.timeline_rows_for_event_ids(
                target_type=target_type,
                target_id=target_id,
                event_ids=[requested_event_id],
                limit=1,
            )
            return _response(
                target_type=target_type,
                target_id=target_id,
                window=window,
                post_range=post_range,
                page_rows=rows,
                has_more=False,
                next_cursor=None,
            )
        try:
            timeline_cursor = decode_target_cursor(cursor)
        except TokenTargetCursorError as exc:
            raise TokenTargetPostsCursorError(cursor) from exc
        resolved_now_ms = int(now_ms or time.time() * 1000)
        window_ms = _window_ms(window)
        since_ms = 0 if post_range in {"since_ignition", "all_history"} else resolved_now_ms - window_ms
        rows = self.targets.timeline_rows(
            target_type=target_type,
            target_id=target_id,
            since_ms=since_ms,
            limit=row_limit + 1,
            cursor=timeline_cursor,
        )
        page_rows = rows[:row_limit]
        has_more = len(rows) > len(page_rows)
        next_cursor = encode_target_cursor(page_rows[-1]) if has_more and page_rows else None
        return _response(
            target_type=target_type,
            target_id=target_id,
            window=window,
            post_range=post_range,
            page_rows=page_rows,
            has_more=has_more,
            next_cursor=next_cursor,
        )


def _response(
    *,
    target_type: str,
    target_id: str,
    window: str,
    post_range: str,
    page_rows: list[dict[str, Any]],
    has_more: bool,
    next_cursor: str | None,
) -> dict[str, Any]:
    stage_build = build_token_target_stages(page_rows)
    return {
        "query": {
            "target_type": target_type,
            "target_id": target_id,
            "window": window,
            "range": post_range,
        },
        "score_window": {"window": window},
        "total_count": len(page_rows) + (1 if has_more else 0),
        "returned_count": len(page_rows),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "items": [
            token_target_post_payload(
                row,
                stage=stage_build.annotations.get(str(row.get("event_id") or "")),
            )
            for row in page_rows
        ],
    }


def _window_ms(window: str) -> int:
    try:
        return PRODUCT_WINDOW_MS[window]
    except KeyError as exc:
        raise TokenTargetPostsWindowError(window) from exc
