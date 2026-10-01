"""Exact-input judgment cache adapter over caller-owned News database operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from ..clock import clock_ms
from ..updates.judgment import Answer

if TYPE_CHECKING:
    from ..pipeline.runtime import NewsDatabasePort


class PgJudgmentCache:
    def __init__(self, db: NewsDatabasePort, *, clock: Callable[[], int] = clock_ms) -> None:
        self.db = db
        self.clock = clock

    async def get_many(self, keys: tuple[str, ...]) -> dict[str, Answer]:
        if not keys:
            return {}
        rows = await self.db.read(
            "news_judgment_cache_get", lambda repos: repos.news.judgment_cache.judgment_cache_answers(keys)
        )
        return {key: Answer.model_validate(answer) for key, answer in rows.items()}

    async def put_many(self, answers: Mapping[str, Answer]) -> None:
        if not answers:
            return
        documents = {key: answer.model_dump_json() for key, answer in answers.items()}
        now_ms = self.clock()
        await self.db.tx(
            "news_judgment_cache_put",
            lambda repos: repos.news.judgment_cache.put_judgment_cache_answers(answers=documents, now_ms=now_ms),
        )
