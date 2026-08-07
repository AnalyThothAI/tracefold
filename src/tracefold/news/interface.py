from __future__ import annotations

from math import isfinite
from typing import Any

from .repository import NewsRepository


class NewsInterface:
    """The sole package-external News product seam."""

    def __init__(self, repository: NewsRepository) -> None:
        self._repository = repository

    def get_feed(
        self,
        *,
        category: str | None = None,
        level: str | None = None,
        source_id: str | None = None,
        reporting_origin: str | None = None,
        provider_score_gt: float | None = None,
        q: str | None = None,
        sort: str = "importance",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(category or "").strip().lower() or None
        normalized_query = " ".join(str(q or "").split()).lower() or None
        normalized_reporting_origin = str(reporting_origin or "").strip().lower() or None
        normalized_sort = str(sort or "").strip().lower()
        if normalized_sort not in {"importance", "latest"}:
            raise ValueError("news_feed_sort_invalid")
        if normalized_query is not None and len(normalized_query) > 200:
            raise ValueError("news_feed_query_invalid")
        if normalized_reporting_origin is not None and len(normalized_reporting_origin) > 128:
            raise ValueError("news_feed_reporting_origin_invalid")
        normalized_provider_score_gt = None
        if provider_score_gt is not None:
            if isinstance(provider_score_gt, bool):
                raise ValueError("news_feed_provider_score_gt_invalid")
            try:
                normalized_provider_score_gt = float(provider_score_gt)
            except (TypeError, ValueError) as exc:
                raise ValueError("news_feed_provider_score_gt_invalid") from exc
            if not isfinite(normalized_provider_score_gt):
                raise ValueError("news_feed_provider_score_gt_invalid")
        return self._repository.list_feed(
            category=normalized,
            level=str(level or "").strip().lower() or None,
            source_id=str(source_id or "").strip() or None,
            reporting_origin=normalized_reporting_origin,
            provider_score_gt=normalized_provider_score_gt,
            q=normalized_query,
            sort=normalized_sort,
            limit=limit,
            cursor=str(cursor or "").strip() or None,
        )

    def get_story(
        self,
        *,
        story_id: str,
        members_limit: int = 100,
        members_cursor: str | None = None,
    ) -> dict[str, Any] | None:
        normalized = str(story_id or "").strip()
        if not normalized:
            raise ValueError("news_story_id_required")
        return self._repository.get_story(
            story_id=normalized,
            members_limit=members_limit,
            members_cursor=str(members_cursor or "").strip() or None,
        )

    def get_world_brief(self, *, now_ms: int) -> dict[str, Any]:
        return self._repository.get_brief(now_ms=now_ms)

    def get_sources(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._repository.list_sources(
            limit=limit,
            cursor=str(cursor or "").strip() or None,
        )

    def health(
        self,
        *,
        now_ms: int,
        push_enabled: bool = False,
        feishu_webhook_url_configured: bool = False,
        feishu_signing_secret_configured: bool = False,
        workers_state: str | None = None,
        workers_reason: str | None = None,
    ) -> dict[str, Any]:
        return self._repository.health_snapshot(
            now_ms=now_ms,
            push_enabled=push_enabled,
            feishu_webhook_url_configured=feishu_webhook_url_configured,
            feishu_signing_secret_configured=feishu_signing_secret_configured,
            workers_state=workers_state,
            workers_reason=workers_reason,
        )


__all__ = ["NewsInterface"]
