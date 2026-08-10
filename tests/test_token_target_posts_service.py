from __future__ import annotations

import pytest

from tracefold.market.views.token_target_posts_service import (
    TokenTargetPostsQueryError,
    TokenTargetPostsService,
)


def test_event_id_mode_uses_exact_target_bound_lookup_without_pagination() -> None:
    targets = _Targets()

    result = TokenTargetPostsService(targets=targets).target_posts(
        target_type="Asset",
        target_id="asset:test",
        event_id="trigger-event",
        window="5m",
        post_range="all_history",
        limit=50,
    )

    assert targets.exact_calls == [
        {
            "target_type": "Asset",
            "target_id": "asset:test",
            "event_ids": ["trigger-event"],
            "limit": 1,
        }
    ]
    assert result["returned_count"] == 1
    assert result["total_count"] == 1
    assert result["has_more"] is False
    assert result["next_cursor"] is None
    assert result["items"][0]["event_id"] == "trigger-event"


def test_event_id_mode_rejects_cursor_instead_of_silently_ignoring_it() -> None:
    with pytest.raises(TokenTargetPostsQueryError, match="event_id_cursor_incompatible"):
        TokenTargetPostsService(targets=_Targets()).target_posts(
            target_type="Asset",
            target_id="asset:test",
            event_id="trigger-event",
            window="5m",
            post_range="all_history",
            limit=50,
            cursor="cursor",
        )


class _Targets:
    def __init__(self) -> None:
        self.exact_calls: list[dict[str, object]] = []

    def timeline_rows_for_event_ids(self, **kwargs):
        self.exact_calls.append(kwargs)
        return [
            {
                "event_id": "trigger-event",
                "tweet_id": "tweet-1",
                "target_type": "Asset",
                "target_id": "asset:test",
                "symbol": "TEST",
                "author_handle": "alice",
                "text": "trigger evidence",
                "received_at_ms": 1,
                "attribution_status": "EXACT",
                "confidence": 1.0,
            }
        ]
