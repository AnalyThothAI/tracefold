"""Content-addressed judgment cache and bounded checkpoint retention.

Commands use the caller's existing transaction; no external I/O or independent commit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

JUDGMENT_CACHE_RETENTION_MS: Final = 14 * 24 * 3_600_000


PURGE_BATCH_MAX: Final = 1_000


class JudgmentCacheStorage:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def judgment_cache_answers(self, cache_keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        """One statement for a whole question set's cached answers."""

        rows = self.conn.execute(
            "SELECT cache_key, answer FROM news_judgment_cache WHERE cache_key = ANY(%s)", (list(cache_keys),)
        ).fetchall()
        return {str(row["cache_key"]): dict(row["answer"]) for row in rows}

    def put_judgment_cache_answers(self, *, answers: Mapping[str, str], now_ms: int) -> int:
        """One statement per answered batch; the first stored answer for a key wins."""

        cursor = self.conn.execute(
            """
            INSERT INTO news_judgment_cache (cache_key, answer, created_at_ms)
            SELECT cache_key, answer::jsonb, %s FROM unnest(%s::text[], %s::text[]) AS a(cache_key, answer)
            ON CONFLICT (cache_key) DO NOTHING
            """,
            (int(now_ms), list(answers), list(answers.values())),
        )
        return int(cursor.rowcount or 0)

    def purge_semantic_caches(self, *, now_ms: int, limit: int = PURGE_BATCH_MAX) -> int:
        """Janitor retention: judgment answers and stage checkpoints older than 14 days, bounded."""

        cutoff = int(now_ms) - JUDGMENT_CACHE_RETENTION_MS
        bounded = max(1, min(int(limit), PURGE_BATCH_MAX))
        answers = self.conn.execute(
            """
            DELETE FROM news_judgment_cache WHERE cache_key IN (
              SELECT cache_key FROM news_judgment_cache WHERE created_at_ms < %s
               ORDER BY created_at_ms, cache_key LIMIT %s
            )
            """,
            (cutoff, bounded),
        )
        checkpoints = self.conn.execute(
            """
            DELETE FROM news_semantic_checkpoints WHERE (work_id, stage) IN (
              SELECT work_id, stage FROM news_semantic_checkpoints WHERE created_at_ms < %s
               ORDER BY created_at_ms, work_id, stage LIMIT %s
            )
            """,
            (cutoff, bounded),
        )
        return int(answers.rowcount or 0) + int(checkpoints.rowcount or 0)
