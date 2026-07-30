from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, cast

from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult

from .brief import brief_fingerprint, validate_and_repair_brief
from .models import (
    EventCategory,
    NewsBriefPublisher,
    NewsBriefStory,
    NewsFeedReader,
    NewsSourceDefinition,
    ThreatLevel,
    source_definition,
)


class NewsPipelineWorker(WorkerBase):
    """The only NewsItem/Story writer."""

    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        telemetry: Any,
        sources: Sequence[NewsSourceDefinition | Any],
        feed_reader: NewsFeedReader,
        name: str = "news_pipeline",
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
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=self.settings.statement_timeout_seconds,
            ) as repos,
            repos.transaction(),
        ):
            repos.news.sync_sources(self.sources, now_ms=now_ms)
            claimed = repos.news.claim_due_sources(
                now_ms=now_ms,
                limit=int(self.settings.batch_size),
            )

        fetched: dict[str, tuple[Any, int]] = {}
        failures: dict[str, tuple[Exception, int]] = {}
        if claimed:
            max_workers = min(int(self.settings.fetch_concurrency), len(claimed))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(
                        self.feed_reader.fetch,
                        source=source_definition(row),
                        etag=_optional_text(row.get("etag")),
                        last_modified=_optional_text(row.get("last_modified")),
                    ): row
                    for row in claimed
                }
                for future in as_completed(futures):
                    row = futures[future]
                    finished_at_ms = int(self.clock_ms())
                    try:
                        fetched[str(row["source_id"])] = (
                            future.result(),
                            finished_at_ms,
                        )
                    except Exception as exc:
                        failures[str(row["source_id"])] = (
                            exc,
                            finished_at_ms,
                        )

        totals = {
            "entries_seen": 0,
            "observations_inserted": 0,
            "items_inserted": 0,
            "items_updated": 0,
        }
        for raw_source in claimed:
            source = source_definition(raw_source)
            started_at_ms = int(raw_source["last_fetch_started_at_ms"] or now_ms)
            if source.source_id in failures:
                error, finished_at_ms = failures[source.source_id]
                with (
                    self.db.worker_session(
                        self.name,
                        statement_timeout_seconds=self.settings.statement_timeout_seconds,
                    ) as repos,
                    repos.transaction(),
                ):
                    repos.news.record_fetch_failure(
                        source_id=source.source_id,
                        started_at_ms=started_at_ms,
                        finished_at_ms=finished_at_ms,
                        error=error,
                        status_code=getattr(
                            error,
                            "status_code",
                            getattr(
                                getattr(error, "response", None),
                                "status_code",
                                None,
                            ),
                        ),
                        fetch_path=getattr(error, "fetch_path", None),
                        direct_error_code=getattr(
                            error,
                            "direct_error_code",
                            None,
                        ),
                    )
                continue
            result, finished_at_ms = fetched[source.source_id]
            with (
                self.db.worker_session(
                    self.name,
                    statement_timeout_seconds=self.settings.statement_timeout_seconds,
                ) as repos,
                repos.transaction(),
            ):
                summary = repos.news.record_fetch_success(
                    source=source,
                    entries=result.entries,
                    started_at_ms=started_at_ms,
                    finished_at_ms=finished_at_ms,
                    status_code=int(result.status_code),
                    fetch_path=str(result.fetch_path),
                    direct_error_code=result.direct_error_code,
                    etag=result.etag,
                    last_modified=result.last_modified,
                    not_modified=bool(result.not_modified),
                    entries_seen=int(result.entries_seen),
                    gate_counts=result.gate_counts,
                )
            for key in totals:
                totals[key] += int(summary[key])

        projection_now_ms = int(self.clock_ms())
        with self.db.worker_session(
            self.name,
            statement_timeout_seconds=self.settings.statement_timeout_seconds,
        ) as repos:
            prepared = repos.news.prepare_story_projection(now_ms=projection_now_ms)
            if prepared.requires_rebuild:
                with repos.transaction():
                    projection = repos.news.rebuild_stories(
                        now_ms=projection_now_ms,
                        prepared=prepared,
                    )
            else:
                projection = repos.news.rebuild_stories(
                    now_ms=projection_now_ms,
                    prepared=prepared,
                )
        return WorkerResult(
            processed=len(fetched),
            skipped=1 if not claimed and projection["story_writes"] == 0 else 0,
            notes={
                "sources_claimed": len(claimed),
                "source_failures": len(failures),
                **totals,
                **projection,
            },
        )


class NewsWorldBriefWorker(WorkerBase):
    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        telemetry: Any,
        publisher: NewsBriefPublisher,
        name: str = "news_world_brief",
        clock_ms: Any | None = None,
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.publisher = publisher
        self.clock_ms = clock_ms or _now_ms

    async def run_once(self) -> WorkerResult:
        return await asyncio.to_thread(self.run_once_sync)

    async def on_close(self) -> None:
        await asyncio.to_thread(self.publisher.close)

    def run_once_sync(self) -> WorkerResult:
        with self.db.worker_session(
            self.name,
            statement_timeout_seconds=self.settings.statement_timeout_seconds,
        ) as repos:
            candidates = repos.news.brief_candidates()
        fingerprint = brief_fingerprint(candidates)
        source_count = len({str(candidate["representative_source_id"]) for candidate in candidates})
        now_ms = int(self.clock_ms())
        if len(candidates) < 3 or source_count < 2:
            with (
                self.db.worker_session(
                    self.name,
                    statement_timeout_seconds=self.settings.statement_timeout_seconds,
                ) as repos,
                repos.transaction(),
            ):
                repos.news.record_brief_insufficient(
                    fingerprint=fingerprint,
                    story_count=len(candidates),
                    source_count=source_count,
                    now_ms=now_ms,
                )
            return WorkerResult(
                skipped=1,
                notes={
                    "reason": "insufficient_material",
                    "story_count": len(candidates),
                    "source_count": source_count,
                    "model_calls": 0,
                },
            )

        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=self.settings.statement_timeout_seconds,
            ) as repos,
            repos.transaction(),
        ):
            claim = repos.news.claim_brief_run(
                fingerprint=fingerprint,
                story_count=len(candidates),
                source_count=source_count,
                now_ms=now_ms,
                max_attempts=int(self.settings.max_attempts),
            )
        if claim is None:
            return WorkerResult(
                skipped=1,
                notes={"reason": "fingerprint_not_due"},
            )

        stories = [
            NewsBriefStory(
                story_id=str(row["story_id"]),
                title=str(row["representative_title"]),
                source=str(row["representative_source_name"]),
                url=str(row["representative_url"]),
                source_count=int(row["source_count"]),
                importance_score=int(row["importance_score"]),
                level=cast(ThreatLevel, str(row["level"])),
                category=cast(EventCategory, str(row["category"])),
            )
            for row in candidates
        ]
        try:
            draft = self.publisher.publish(stories)
            repaired, validation, _ = validate_and_repair_brief(draft, stories)
            with (
                self.db.worker_session(
                    self.name,
                    statement_timeout_seconds=self.settings.statement_timeout_seconds,
                ) as repos,
                repos.transaction(),
            ):
                publication_id = repos.news.publish_brief(
                    run_id=claim["run_id"],
                    lease_owner=claim["lease_owner"],
                    fingerprint=fingerprint,
                    stories=candidates,
                    draft=repaired,
                    validation=validation,
                    now_ms=int(self.clock_ms()),
                )
        except Exception as exc:
            with (
                self.db.worker_session(
                    self.name,
                    statement_timeout_seconds=self.settings.statement_timeout_seconds,
                ) as repos,
                repos.transaction(),
            ):
                repos.news.fail_brief_run(
                    run_id=claim["run_id"],
                    lease_owner=claim["lease_owner"],
                    error=exc,
                    now_ms=int(self.clock_ms()),
                )
            return WorkerResult(
                failed=1,
                notes={
                    "reason": type(exc).__name__,
                    "last_known_good_preserved": True,
                },
            )
        return WorkerResult(
            processed=1,
            notes={
                "publication_id": publication_id,
                "story_count": len(stories),
                "source_count": source_count,
            },
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


__all__ = ["NewsPipelineWorker", "NewsWorldBriefWorker"]
