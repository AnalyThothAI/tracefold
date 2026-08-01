from __future__ import annotations

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
        sort: str = "importance",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(category or "").strip().lower() or None
        normalized_sort = str(sort or "").strip().lower()
        if normalized_sort not in {"importance", "latest"}:
            raise ValueError("news_feed_sort_invalid")
        return self._repository.list_feed(
            category=normalized,
            level=str(level or "").strip().lower() or None,
            source_id=str(source_id or "").strip() or None,
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
    ) -> dict[str, Any]:
        return self._repository.health_snapshot(
            now_ms=now_ms,
            push_enabled=push_enabled,
            feishu_webhook_url_configured=feishu_webhook_url_configured,
            feishu_signing_secret_configured=feishu_signing_secret_configured,
        )


__all__ = ["NewsInterface"]
