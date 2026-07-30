from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import urlsplit

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


class NewsIngestWorker(WorkerBase):
    """Source claim/fetch/persist only; Story projection is an EDF candidate."""

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
        resources = self.require_runtime_resources()
        now_ms = int(self.clock_ms())
        claimed = await resources.run_background_db(
            self._claim_sources,
            now_ms,
        )
        totals = _empty_ingest_totals()
        fetched = 0
        failures = 0
        for raw_source in claimed:
            source = source_definition(raw_source)
            started_at_ms = int(raw_source["last_fetch_started_at_ms"] or now_ms)
            try:
                async with self.require_provider_governor().acquire(host=_source_provider_host(source)):
                    result = await resources.run_provider_io(
                        self.feed_reader.fetch,
                        source=source,
                        etag=_optional_text(raw_source.get("etag")),
                        last_modified=_optional_text(raw_source.get("last_modified")),
                    )
            except Exception as exc:
                failures += 1
                await resources.run_background_db(
                    self._record_fetch_failure,
                    source,
                    started_at_ms,
                    int(self.clock_ms()),
                    exc,
                )
                continue
            fetched += 1
            summary = await resources.run_background_db(
                self._record_fetch_success,
                source,
                started_at_ms,
                int(self.clock_ms()),
                result,
            )
            for key in totals:
                totals[key] += int(summary[key])
        return WorkerResult(
            processed=fetched,
            skipped=1 if not claimed else 0,
            notes={
                "sources_claimed": len(claimed),
                "source_failures": failures,
                **totals,
            },
        )

    async def on_close(self) -> None:
        await self.require_runtime_resources().run_provider_cleanup(self.feed_reader.close)

    def _claim_sources(self, now_ms: int) -> list[dict[str, Any]]:
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
        return [dict(row) for row in claimed]

    def _record_fetch_failure(
        self,
        source: NewsSourceDefinition,
        started_at_ms: int,
        finished_at_ms: int,
        error: Exception,
    ) -> None:
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

    def _record_fetch_success(
        self,
        source: NewsSourceDefinition,
        started_at_ms: int,
        finished_at_ms: int,
        result: Any,
    ) -> dict[str, Any]:
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=self.settings.statement_timeout_seconds,
            ) as repos,
            repos.transaction(),
        ):
            return dict(
                repos.news.record_fetch_success(
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
        resources = self.require_runtime_resources()
        prepared = await resources.run_background_db(self.prepare_run_sync)
        if "result" in prepared:
            return cast(WorkerResult, prepared["result"])
        try:
            generated = await resources.run_model(
                self.generate_sync,
                prepared["stories"],
            )
            return cast(
                WorkerResult,
                await resources.run_background_db(
                    self.publish_prepared_sync,
                    prepared,
                    generated,
                ),
            )
        except Exception as exc:
            return cast(
                WorkerResult,
                await resources.run_background_db(
                    self.fail_prepared_sync,
                    prepared,
                    exc,
                ),
            )

    async def on_close(self) -> None:
        await self.require_runtime_resources().run_model_cleanup(self.publisher.close)

    def run_once_sync(self) -> WorkerResult:
        prepared = self.prepare_run_sync()
        if "result" in prepared:
            return cast(WorkerResult, prepared["result"])
        try:
            generated = self.generate_sync(prepared["stories"])
            return self.publish_prepared_sync(prepared, generated)
        except Exception as exc:
            return self.fail_prepared_sync(prepared, exc)

    def prepare_run_sync(self) -> dict[str, Any]:
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
            return {
                "result": WorkerResult(
                    skipped=1,
                    notes={
                        "reason": "insufficient_material",
                        "story_count": len(candidates),
                        "source_count": source_count,
                        "model_calls": 0,
                    },
                )
            }

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
                lease_owner=self.claim_owner,
            )
        if claim is None:
            return {
                "result": WorkerResult(
                    skipped=1,
                    notes={"reason": "fingerprint_not_due"},
                )
            }

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
        return {
            "claim": dict(claim),
            "fingerprint": fingerprint,
            "candidates": candidates,
            "stories": stories,
            "source_count": source_count,
        }

    def generate_sync(
        self,
        stories: list[NewsBriefStory],
    ) -> tuple[Any, dict[str, Any]]:
        draft = self.publisher.publish(stories)
        repaired, validation, _ = validate_and_repair_brief(draft, stories)
        return repaired, dict(validation)

    def publish_prepared_sync(
        self,
        prepared: dict[str, Any],
        generated: tuple[Any, dict[str, Any]],
    ) -> WorkerResult:
        repaired, validation = generated
        claim = prepared["claim"]
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
                fingerprint=prepared["fingerprint"],
                stories=prepared["candidates"],
                draft=repaired,
                validation=validation,
                now_ms=int(self.clock_ms()),
            )
        return WorkerResult(
            processed=1,
            notes={
                "publication_id": publication_id,
                "story_count": len(prepared["stories"]),
                "source_count": prepared["source_count"],
            },
        )

    def fail_prepared_sync(
        self,
        prepared: dict[str, Any],
        error: Exception,
    ) -> WorkerResult:
        claim = prepared["claim"]
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
                error=error,
                now_ms=int(self.clock_ms()),
            )
        return WorkerResult(
            failed=1,
            notes={
                "reason": type(error).__name__,
                "last_known_good_preserved": True,
            },
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _source_provider_host(source: NewsSourceDefinition) -> str:
    return (urlsplit(str(source.feed_url)).hostname or str(source.source_id)).lower()


def _empty_ingest_totals() -> dict[str, int]:
    return {
        "entries_seen": 0,
        "observations_inserted": 0,
        "items_inserted": 0,
        "items_updated": 0,
        "projection_frontiers_written": 0,
    }


__all__ = ["NewsIngestWorker", "NewsWorldBriefWorker"]
