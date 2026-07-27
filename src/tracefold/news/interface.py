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
        sort: str = "importance",
    ) -> dict[str, Any]:
        normalized = str(category or "").strip().lower() or None
        normalized_sort = str(sort or "").strip().lower()
        if normalized_sort not in {"importance", "latest"}:
            raise ValueError("news_feed_sort_invalid")
        return self._repository.list_feed(category=normalized, sort=normalized_sort)

    def get_story(self, *, story_id: str) -> dict[str, Any] | None:
        normalized = str(story_id or "").strip()
        if not normalized:
            raise ValueError("news_story_id_required")
        return self._repository.get_story(story_id=normalized)

    def get_world_brief(self, *, now_ms: int) -> dict[str, Any]:
        return self._repository.get_brief(now_ms=now_ms)

    def get_sources(self) -> dict[str, Any]:
        return self._repository.list_sources()

    def health(self, *, now_ms: int) -> dict[str, Any]:
        return self._repository.health_snapshot(now_ms=now_ms)


__all__ = ["NewsInterface"]
