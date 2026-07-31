from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from typing import Any, cast
from uuid import uuid4

from tracefold.platform.model_candidate import ModelCandidate
from tracefold.platform.resource import CpuTaskTimeout, ResourceAdmissionTimeout

from .brief import brief_fingerprint, validate_and_repair_brief
from .models import (
    EventCategory,
    NewsBriefExpectedError,
    NewsBriefPublisher,
    NewsBriefStory,
    NewsFeedExpectedError,
    NewsFeedFetch,
    NewsFeedReader,
    NewsSourceDefinition,
    ThreatLevel,
    source_definition,
)

_SOURCE_LEASE_MS = 45_000
_NEWS_OPERATION_TIMEOUT_SECONDS = 30.0
_NEWS_HTTP_OUTER_TIMEOUT_SECONDS = 28.0
_NEWS_PARSE_SERVICE_TIMEOUT_SECONDS = 2.0
_NEWS_PARSE_OUTER_TIMEOUT_SECONDS = 2.5
_BRIEF_MODEL_TIMEOUT_SECONDS = 60.0
_BRIEF_MAX_ATTEMPTS = 3


class NewsAcquisition:
    """One bounded durable News source turn."""

    def __init__(
        self,
        *,
        db: Any,
        finite_operations: Any,
        cpu: Any,
        sources: Sequence[NewsSourceDefinition | Any],
        feed_reader: NewsFeedReader,
        feed_parser: Callable[[Any], NewsFeedFetch],
    ) -> None:
        self.db = db
        self.finite_operations = finite_operations
        self.cpu = cpu
        self.sources = tuple(source_definition(source) for source in sources)
        self.feed_reader = feed_reader
        self.feed_parser = feed_parser

    async def reconcile(self) -> None:
        await self.db.run_business(
            "news_source_reconcile",
            self._reconcile_sync,
            operation_timeout_seconds=3.0,
        )

    async def turn(self) -> bool | None:
        now_ms = _now_ms()
        claim_token = str(uuid4())
        claimed = await self.db.run_business(
            "news_source_claim",
            self._claim_sync,
            now_ms,
            claim_token,
            operation_timeout_seconds=0.5,
        )
        if claimed is None:
            return False
        source = source_definition(claimed)
        started_at_ms = int(claimed["last_fetch_started_at_ms"])
        submitted = False

        def mark_submitted() -> None:
            nonlocal submitted
            submitted = True

        operation_started = asyncio.get_running_loop().time()
        try:
            wire = await self.finite_operations.run(
                "news_source_fetch",
                self.feed_reader.fetch_wire,
                source=source,
                etag=_optional_text(claimed.get("etag")),
                last_modified=_optional_text(claimed.get("last_modified")),
                timeout_seconds=_NEWS_HTTP_OUTER_TIMEOUT_SECONDS,
                on_submitted=mark_submitted,
            )
            submitted = False
            remaining = _NEWS_OPERATION_TIMEOUT_SECONDS - (asyncio.get_running_loop().time() - operation_started)
            if remaining < _NEWS_PARSE_OUTER_TIMEOUT_SECONDS:
                raise _news_parse_expected_error("news_rss_total_timeout")
            fetched = await self.cpu.run(
                "news_source_parse",
                self.feed_parser,
                wire,
                service_timeout_seconds=_NEWS_PARSE_SERVICE_TIMEOUT_SECONDS,
                operation_timeout_seconds=_NEWS_PARSE_OUTER_TIMEOUT_SECONDS,
                on_submitted=mark_submitted,
            )
            submitted = False
        except asyncio.CancelledError:
            if not submitted:
                await asyncio.shield(
                    self.db.run_business(
                        "news_source_release_cancelled_prework",
                        self._release_sync,
                        source.source_id,
                        claim_token,
                        operation_timeout_seconds=0.5,
                    )
                )
            raise
        except ResourceAdmissionTimeout:
            await self.db.run_business(
                "news_source_release_prework",
                self._release_sync,
                source.source_id,
                claim_token,
                operation_timeout_seconds=0.5,
            )
            return None
        except NewsFeedExpectedError as exc:
            published = await self.db.run_business(
                "news_source_publish_failure",
                self._failure_sync,
                source,
                started_at_ms,
                claim_token,
                exc,
                operation_timeout_seconds=3.0,
            )
            return True if published else None
        except CpuTaskTimeout:
            error = _news_parse_expected_error("news_rss_parse_cpu_limit_exceeded")
            published = await self.db.run_business(
                "news_source_publish_parse_timeout",
                self._failure_sync,
                source,
                started_at_ms,
                claim_token,
                error,
                operation_timeout_seconds=3.0,
            )
            return True if published else None
        published = await self.db.run_business(
            "news_source_publish_success",
            self._success_sync,
            source,
            started_at_ms,
            claim_token,
            fetched,
            operation_timeout_seconds=3.0,
        )
        return True if published else None

    async def close(self) -> None:
        await self.finite_operations.run(
            "news_source_client_close",
            self.feed_reader.close,
            timeout_seconds=5.0,
            allow_shutdown=True,
        )

    def _reconcile_sync(self) -> None:
        with self.db.worker_session("news_source_reconcile", 3.0) as repos, repos.transaction():
            repos.news.sync_sources(self.sources, now_ms=_now_ms())

    def _claim_sync(self, now_ms: int, claim_token: str) -> dict[str, Any] | None:
        with self.db.worker_session("news_source_claim", 0.5) as repos, repos.transaction():
            return cast(
                dict[str, Any] | None,
                repos.news.claim_due_source(
                    now_ms=now_ms,
                    claim_token=claim_token,
                    lease_ms=_SOURCE_LEASE_MS,
                ),
            )

    def _release_sync(self, source_id: str, claim_token: str) -> bool:
        with self.db.worker_session("news_source_release", 0.5) as repos, repos.transaction():
            return bool(
                repos.news.release_source_claim(
                    source_id=source_id,
                    claim_token=claim_token,
                )
            )

    def _failure_sync(
        self,
        source: NewsSourceDefinition,
        started_at_ms: int,
        claim_token: str,
        error: NewsFeedExpectedError,
    ) -> bool:
        finished_at_ms = _now_ms()
        with self.db.worker_session("news_source_publish_failure", 3.0) as repos, repos.transaction():
            return bool(
                repos.news.record_fetch_failure(
                    source_id=source.source_id,
                    started_at_ms=started_at_ms,
                    finished_at_ms=finished_at_ms,
                    error=error,
                    status_code=getattr(error, "status_code", None),
                    fetch_path=getattr(error, "fetch_path", None),
                    direct_error_code=getattr(error, "direct_error_code", None),
                    claim_token=claim_token,
                )
            )

    def _success_sync(
        self,
        source: NewsSourceDefinition,
        started_at_ms: int,
        claim_token: str,
        result: Any,
    ) -> bool:
        with self.db.worker_session("news_source_publish_success", 3.0) as repos, repos.transaction():
            published = repos.news.record_fetch_success(
                source=source,
                entries=result.entries,
                started_at_ms=started_at_ms,
                finished_at_ms=_now_ms(),
                status_code=int(result.status_code),
                fetch_path=str(result.fetch_path),
                direct_error_code=result.direct_error_code,
                etag=result.etag,
                last_modified=result.last_modified,
                not_modified=bool(result.not_modified),
                entries_seen=int(result.entries_seen),
                gate_counts=result.gate_counts,
                claim_token=claim_token,
            )
        return published is not None


class NewsBriefCandidate:
    """Native News Brief candidate for the serial model arbiter."""

    def __init__(
        self,
        *,
        db: Any,
        model_adapter: Any,
        publisher: NewsBriefPublisher,
        runtime_id: str,
        stable_order: int = 20,
    ) -> None:
        self.db = db
        self.model_adapter = model_adapter
        self.publisher = publisher
        self.lease_owner = f"news_brief:{runtime_id}"
        self.stable_order = stable_order

    async def peek(self, *, now_ms: int) -> ModelCandidate | None:
        row = await self.db.run_business(
            "news_brief_peek",
            self._peek_sync,
            now_ms,
            operation_timeout_seconds=0.5,
        )
        if row is None:
            return None
        due_at_ms = int(row.get("next_due_at_ms") or row.get("pending_due_at_ms") or now_ms)
        return ModelCandidate(
            kind="news_brief",
            target_key=str(row["target_fingerprint"]),
            due_at_ms=due_at_ms,
            stable_order=self.stable_order,
        )

    async def execute(self, candidate: ModelCandidate) -> bool:
        prepared = await self.db.run_business(
            "news_brief_prepare",
            self._prepare_sync,
            candidate.target_key,
            operation_timeout_seconds=3.0,
        )
        if prepared is None:
            return False
        if bool(prepared.get("completed_without_model")):
            return True
        claim = prepared["claim"]
        model_submitted = False
        claim_lost = False

        def mark_model_submitted() -> None:
            nonlocal model_submitted
            model_submitted = True

        async def start_attempt_after_submit() -> None:
            nonlocal claim_lost
            started = await self.db.run_business(
                "news_brief_start_model",
                self._start_model_sync,
                claim,
                operation_timeout_seconds=0.5,
            )
            if not started:
                claim_lost = True

        try:
            generated = await self.model_adapter.run(
                "news_brief_inference",
                self._generate_sync,
                prepared["stories"],
                timeout_seconds=_BRIEF_MODEL_TIMEOUT_SECONDS,
                after_submit=start_attempt_after_submit,
                on_submitted=mark_model_submitted,
            )
        except asyncio.CancelledError:
            if not model_submitted:
                await asyncio.shield(self._release_prework(claim))
            raise
        except ResourceAdmissionTimeout:
            if model_submitted:
                raise
            await self._release_prework(claim)
            return False
        except NewsBriefExpectedError as exc:
            failed = await self.db.run_business(
                "news_brief_publish_failure",
                self._fail_sync,
                claim,
                exc,
                operation_timeout_seconds=3.0,
            )
            return failed is not None
        if claim_lost:
            return False
        published = await self.db.run_business(
            "news_brief_publish",
            self._publish_sync,
            prepared,
            generated,
            operation_timeout_seconds=3.0,
        )
        return published is not None

    async def _release_prework(self, claim: dict[str, Any]) -> bool:
        return bool(
            await self.db.run_business(
                "news_brief_release_prework",
                self._release_prework_sync,
                claim,
                operation_timeout_seconds=0.5,
            )
        )

    async def close(self) -> None:
        await self.model_adapter.run(
            "news_brief_client_close",
            self.publisher.close,
            timeout_seconds=5.0,
            allow_shutdown=True,
        )

    def _peek_sync(self, now_ms: int) -> dict[str, Any] | None:
        with self.db.worker_session("news_brief_peek", 0.5) as repos:
            return cast(
                dict[str, Any] | None,
                repos.news.peek_brief_candidate(now_ms=now_ms),
            )

    def _prepare_sync(self, target_fingerprint: str) -> dict[str, Any] | None:
        now_ms = _now_ms()
        with self.db.worker_session("news_brief_prepare", 3.0) as repos:
            candidates = repos.news.brief_candidates()
        fingerprint = brief_fingerprint(candidates)
        if fingerprint != target_fingerprint:
            return None
        source_count = len({str(row["representative_source_id"]) for row in candidates})
        if len(candidates) < 3 or source_count < 2:
            with self.db.worker_session("news_brief_insufficient", 3.0) as repos, repos.transaction():
                repos.news.record_brief_insufficient(
                    fingerprint=fingerprint,
                    story_count=len(candidates),
                    source_count=source_count,
                    now_ms=now_ms,
                )
            return {"completed_without_model": True}
        with self.db.worker_session("news_brief_claim", 0.5) as repos, repos.transaction():
            claim = repos.news.claim_brief_run(
                fingerprint=fingerprint,
                story_count=len(candidates),
                source_count=source_count,
                now_ms=now_ms,
                max_attempts=_BRIEF_MAX_ATTEMPTS,
                lease_owner=self.lease_owner,
            )
        if claim is None:
            return None
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
        }

    def _start_model_sync(self, claim: dict[str, Any]) -> bool:
        with self.db.worker_session("news_brief_start_model", 0.5) as repos, repos.transaction():
            return bool(
                repos.news.start_brief_model(
                    run_id=str(claim["run_id"]),
                    lease_owner=str(claim["lease_owner"]),
                    now_ms=_now_ms(),
                    max_attempts=_BRIEF_MAX_ATTEMPTS,
                )
            )

    def _release_prework_sync(self, claim: dict[str, Any]) -> bool:
        with self.db.worker_session("news_brief_release_prework", 0.5) as repos, repos.transaction():
            return bool(
                repos.news.release_brief_claim(
                    run_id=str(claim["run_id"]),
                    lease_owner=str(claim["lease_owner"]),
                    due_at_ms=int(claim["release_due_at_ms"]),
                    now_ms=_now_ms(),
                )
            )

    def _generate_sync(self, stories: list[NewsBriefStory]) -> tuple[Any, dict[str, Any]]:
        draft = self.publisher.publish(stories)
        repaired, validation, _ = validate_and_repair_brief(draft, stories)
        return repaired, dict(validation)

    def _publish_sync(
        self,
        prepared: dict[str, Any],
        generated: tuple[Any, dict[str, Any]],
    ) -> str | None:
        repaired, validation = generated
        claim = prepared["claim"]
        with self.db.worker_session("news_brief_publish", 3.0) as repos, repos.transaction():
            return cast(
                str | None,
                repos.news.publish_brief(
                    run_id=str(claim["run_id"]),
                    lease_owner=str(claim["lease_owner"]),
                    fingerprint=str(prepared["fingerprint"]),
                    stories=prepared["candidates"],
                    draft=repaired,
                    validation=validation,
                    now_ms=_now_ms(),
                ),
            )

    def _fail_sync(self, claim: dict[str, Any], error: NewsBriefExpectedError) -> str | None:
        with self.db.worker_session("news_brief_publish_failure", 3.0) as repos, repos.transaction():
            return cast(
                str | None,
                repos.news.fail_brief_run(
                    run_id=str(claim["run_id"]),
                    lease_owner=str(claim["lease_owner"]),
                    error=error,
                    now_ms=_now_ms(),
                    max_attempts=_BRIEF_MAX_ATTEMPTS,
                ),
            )


def _now_ms() -> int:
    return int(time.time() * 1_000)


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _news_parse_expected_error(code: str) -> NewsFeedExpectedError:
    return NewsFeedExpectedError(str(code))


__all__ = ["NewsAcquisition", "NewsBriefCandidate"]
