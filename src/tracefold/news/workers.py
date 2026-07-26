from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any, cast

from tracefold.news.models import (
    NewsFeedReader,
    NewsSourceDefinition,
    NewsStoryAnalyzer,
    source_definition,
)
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


class NewsIngestWorker(WorkerBase):
    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        telemetry: Any,
        sources: Sequence[NewsSourceDefinition | Any],
        feed_reader: NewsFeedReader,
        name: str = "news_ingest",
        clock_ms: Any | None = None,
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.sources = tuple(source_definition(source) for source in sources)
        self.feed_reader = feed_reader
        self.clock_ms = clock_ms or _now_ms

    async def run_once(self) -> WorkerResult:
        return await asyncio.to_thread(self.run_once_sync)

    async def on_close(self) -> None:
        await asyncio.to_thread(self.feed_reader.close)

    def run_once_sync(self) -> WorkerResult:
        now_ms = int(self.clock_ms())
        claimed = self._claim(now_ms=now_ms)
        if not claimed:
            refreshed = self._refresh_story_time_state(now_ms=now_ms)
            return WorkerResult(skipped=1, notes={"sources_claimed": 0, "stories_refreshed": refreshed})
        processed = 0
        failed = 0
        articles_inserted = 0
        articles_changed = 0
        stories_created = 0
        memberships_created = 0
        for raw_source in claimed:
            source = NewsSourceDefinition.model_validate(
                {
                    field: raw_source[field]
                    for field in NewsSourceDefinition.model_fields
                }
            )
            try:
                result = self.feed_reader.fetch(
                    source=source,
                    etag=_optional_text(raw_source.get("etag")),
                    last_modified=_optional_text(raw_source.get("last_modified")),
                )
                summary = self._record_success(
                    source=source,
                    result=result,
                    now_ms=int(self.clock_ms()),
                )
            except Exception as exc:
                failed += 1
                self._record_failure(source_id=source.source_id, now_ms=int(self.clock_ms()), error=exc)
                continue
            processed += 1
            articles_inserted += summary["articles_inserted"]
            articles_changed += summary["articles_changed"]
            stories_created += summary["stories_created"]
            memberships_created += summary["memberships_created"]
        refreshed = self._refresh_story_time_state(now_ms=int(self.clock_ms()))
        return WorkerResult(
            processed=processed,
            failed=failed,
            notes={
                "sources_claimed": len(claimed),
                "articles_inserted": articles_inserted,
                "articles_changed": articles_changed,
                "stories_created": stories_created,
                "memberships_created": memberships_created,
                "stories_refreshed": refreshed,
            },
        )

    def _claim(self, *, now_ms: int) -> list[dict[str, Any]]:
        with self.db.worker_session(
            self.name,
            statement_timeout_seconds=self.settings.statement_timeout_seconds,
        ) as repos, repos.transaction():
            repos.news.sync_sources(self.sources, now_ms=now_ms)
            return cast(
                list[dict[str, Any]],
                repos.news.claim_due_sources(now_ms=now_ms, limit=int(self.settings.batch_size)),
            )

    def _record_success(self, *, source: NewsSourceDefinition, result: Any, now_ms: int) -> dict[str, int]:
        with self.db.worker_session(
            self.name,
            statement_timeout_seconds=self.settings.statement_timeout_seconds,
        ) as repos, repos.transaction():
            return cast(
                dict[str, int],
                repos.news.record_fetch_success(
                    source=source,
                    entries=result.entries,
                    now_ms=now_ms,
                    status_code=int(result.status_code),
                    etag=result.etag,
                    last_modified=result.last_modified,
                    not_modified=bool(result.not_modified),
                ),
            )

    def _record_failure(self, *, source_id: str, now_ms: int, error: Exception) -> None:
        with self.db.worker_session(
            self.name,
            statement_timeout_seconds=self.settings.statement_timeout_seconds,
        ) as repos, repos.transaction():
            repos.news.record_fetch_failure(
                source_id=source_id,
                now_ms=now_ms,
                error=f"{type(error).__name__}:{error}",
                status_code=getattr(getattr(error, "response", None), "status_code", None),
            )

    def _refresh_story_time_state(self, *, now_ms: int) -> int:
        with self.db.worker_session(
            self.name,
            statement_timeout_seconds=self.settings.statement_timeout_seconds,
        ) as repos, repos.transaction():
            return cast(int, repos.news.refresh_story_time_state(now_ms=now_ms))


class NewsAnalysisWorker(WorkerBase):
    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        telemetry: Any,
        analyzer: NewsStoryAnalyzer,
        model_name: str | None = None,
        name: str = "news_analysis",
        clock_ms: Any | None = None,
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.analyzer = analyzer
        self.model_name = str(model_name or settings.model).strip()
        if not self.model_name:
            raise ValueError("news_analysis_model_name_required")
        self.clock_ms = clock_ms or _now_ms

    async def run_once(self) -> WorkerResult:
        claimed = await asyncio.to_thread(self._claim)
        if not claimed:
            return WorkerResult(skipped=1, notes={"stories_claimed": 0})
        processed = 0
        failed = 0
        analysis_ids: list[str] = []
        for analysis_key, evidence in claimed:
            try:
                result = await self.analyzer.analyze(evidence)
                analysis_id = await asyncio.to_thread(
                    self._complete,
                    analysis_key,
                    evidence,
                    result.draft,
                    result.receipt,
                )
            except Exception as exc:
                failed += 1
                await asyncio.to_thread(self._fail, analysis_key, exc)
                continue
            processed += 1
            analysis_ids.append(analysis_id)
        return WorkerResult(
            processed=processed,
            failed=failed,
            notes={
                "stories_claimed": len(claimed),
                "analysis_ids": analysis_ids,
                "model": self.model_name,
            },
        )

    def _claim(self) -> list[tuple[str, Any]]:
        with self.db.worker_session(
            self.name,
            statement_timeout_seconds=self.settings.statement_timeout_seconds,
        ) as repos, repos.transaction():
            return cast(
                list[tuple[str, Any]],
                repos.news.claim_analysis_evidence(
                    model=self.model_name,
                    now_ms=int(self.clock_ms()),
                    limit=int(self.settings.batch_size),
                    lease_ms=int(self.settings.lease_ms),
                    max_attempts=int(self.settings.max_attempts),
                ),
            )

    def _complete(
        self,
        analysis_key: str,
        evidence: Any,
        draft: Any,
        receipt: dict[str, object],
    ) -> str:
        with self.db.worker_session(
            self.name,
            statement_timeout_seconds=self.settings.statement_timeout_seconds,
        ) as repos, repos.transaction():
            return cast(
                str,
                repos.news.complete_analysis(
                    analysis_key=analysis_key,
                    evidence=evidence,
                    model=self.model_name,
                    draft=draft,
                    published_at_ms=int(self.clock_ms()),
                    receipt=receipt,
                ),
            )

    def _fail(self, analysis_key: str, error: Exception) -> None:
        with self.db.worker_session(
            self.name,
            statement_timeout_seconds=self.settings.statement_timeout_seconds,
        ) as repos, repos.transaction():
            repos.news.fail_analysis(
                analysis_key=analysis_key,
                now_ms=int(self.clock_ms()),
                error=f"{type(error).__name__}:{error}",
                retry_ms=int(self.settings.retry_ms),
            )


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["NewsAnalysisWorker", "NewsIngestWorker"]
