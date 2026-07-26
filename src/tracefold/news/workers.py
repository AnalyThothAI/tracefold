from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any, cast

from tracefold.news.models import (
    BriefEvidenceBundle,
    NewsAiPublisher,
    NewsFeedReader,
    NewsPageFetch,
    NewsPageReader,
    NewsPublicationContract,
    NewsSourceDefinition,
    StoryAnalysisEvidence,
    source_definition,
)
from tracefold.news.validation import validate_publication
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
        page_reader: NewsPageReader | None = None,
        name: str = "news_ingest",
        clock_ms: Any | None = None,
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.sources = tuple(source_definition(source) for source in sources)
        self.feed_reader = feed_reader
        self.page_reader = page_reader
        self.clock_ms = clock_ms or _now_ms

    async def run_once(self) -> WorkerResult:
        return await asyncio.to_thread(self.run_once_sync)

    async def on_close(self) -> None:
        await asyncio.to_thread(self.feed_reader.close)
        if self.page_reader is not None:
            await asyncio.to_thread(self.page_reader.close)

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
            page_claims = (
                repos.news.claim_page_enrichment(
                    now_ms=now_ms,
                    limit=int(self.settings.page_enrichment_batch_size),
                    minimum_impact_score=int(self.settings.page_enrichment_minimum_impact_score),
                    extractor_version=self.page_reader.extractor_version,
                    lease_ms=int(self.settings.page_enrichment_lease_ms),
                    max_attempts=int(self.settings.page_enrichment_max_attempts),
                )
                if self.page_reader is not None and bool(self.settings.page_enrichment_enabled)
                else []
            )
        if not claimed and not page_claims:
            return WorkerResult(
                skipped=1,
                notes={"sources_claimed": 0, "page_enrichments_claimed": 0},
            )
        totals = {
            "entries_admitted": 0,
            "duplicate_seen_count": 0,
            "articles_inserted": 0,
            "revisions_inserted": 0,
            "observations_inserted": 0,
        }
        processed = 0
        failed = 0
        for raw_source in claimed:
            source = NewsSourceDefinition.model_validate(
                {field: raw_source[field] for field in NewsSourceDefinition.model_fields}
            )
            started_at_ms = int(self.clock_ms())
            try:
                fetched = self.feed_reader.fetch(
                    source=source,
                    etag=_optional_text(raw_source.get("etag")),
                    last_modified=_optional_text(raw_source.get("last_modified")),
                )
                finished_at_ms = int(self.clock_ms())
                with (
                    self.db.worker_session(
                        self.name,
                        statement_timeout_seconds=self.settings.statement_timeout_seconds,
                    ) as repos,
                    repos.transaction(),
                ):
                    summary = repos.news.record_fetch_success(
                        source=source,
                        entries=fetched.entries,
                        started_at_ms=started_at_ms,
                        finished_at_ms=finished_at_ms,
                        status_code=int(fetched.status_code),
                        etag=fetched.etag,
                        last_modified=fetched.last_modified,
                        not_modified=bool(fetched.not_modified),
                    )
            except Exception as exc:
                failed += 1
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
                        finished_at_ms=int(self.clock_ms()),
                        error=exc,
                        status_code=getattr(getattr(exc, "response", None), "status_code", None),
                    )
                continue
            processed += 1
            for key in totals:
                totals[key] += int(summary[key])
        pages_processed = 0
        pages_failed = 0
        if self.page_reader is not None:
            for claim in page_claims:
                try:
                    page_result = self.page_reader.fetch(url=str(claim["source_url"]))
                except Exception as exc:
                    page_result = NewsPageFetch(
                        status="failed",
                        fetched_at_ms=int(self.clock_ms()),
                        failure_reason=f"{type(exc).__name__}:{exc}",
                        final_url=str(claim["source_url"]),
                    )
                with (
                    self.db.worker_session(
                        self.name,
                        statement_timeout_seconds=self.settings.statement_timeout_seconds,
                    ) as repos,
                    repos.transaction(),
                ):
                    repos.news.complete_page_enrichment(
                        content_snapshot_id=str(claim["content_snapshot_id"]),
                        lease_token=str(claim["lease_token"]),
                        result=page_result,
                        retry_ms=int(self.settings.page_enrichment_retry_ms),
                        now_ms=int(self.clock_ms()),
                    )
                if page_result.status in {"available", "truncated"}:
                    pages_processed += 1
                else:
                    pages_failed += 1
        return WorkerResult(
            processed=processed + pages_processed,
            failed=failed + pages_failed,
            notes={
                "sources_claimed": len(claimed),
                "page_enrichments_claimed": len(page_claims),
                "page_enrichments_available": pages_processed,
                "page_enrichments_unavailable": pages_failed,
                **totals,
            },
        )


class NewsStoryProjectWorker(WorkerBase):
    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        telemetry: Any,
        name: str = "news_story_project",
        clock_ms: Any | None = None,
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.clock_ms = clock_ms or _now_ms

    async def run_once(self) -> WorkerResult:
        return await asyncio.to_thread(self.run_once_sync)

    def run_once_sync(self) -> WorkerResult:
        now_ms = int(self.clock_ms())
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=self.settings.statement_timeout_seconds,
            ) as repos,
            repos.transaction(),
        ):
            counts = repos.news.project_pending_revisions(
                now_ms=now_ms,
                limit=int(self.settings.batch_size),
            )
            refreshed = repos.news.refresh_story_presentation(
                now_ms=now_ms,
                limit=int(self.settings.presentation_batch_size),
            )
        return WorkerResult(
            processed=int(counts["processed"]),
            skipped=1 if not counts["processed"] and not refreshed else 0,
            notes={**counts, "presentation_refreshed": refreshed},
        )


class NewsBriefPlanWorker(WorkerBase):
    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        telemetry: Any,
        name: str = "news_brief_plan",
        clock_ms: Any | None = None,
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.clock_ms = clock_ms or _now_ms

    async def run_once(self) -> WorkerResult:
        return await asyncio.to_thread(self.run_once_sync)

    def run_once_sync(self) -> WorkerResult:
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=self.settings.statement_timeout_seconds,
            ) as repos,
            repos.transaction(),
        ):
            result = repos.news.plan_global_brief(
                now_ms=int(self.clock_ms()),
                candidate_limit=int(self.settings.candidate_limit),
                debounce_ms=int(self.settings.debounce_ms),
                critical_debounce_ms=int(self.settings.critical_debounce_ms),
            )
        return WorkerResult(
            processed=1 if result["changed"] else 0,
            skipped=0 if result["changed"] else 1,
            notes=result,
        )


class NewsAiPublishWorker(WorkerBase):
    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        telemetry: Any,
        publisher: NewsAiPublisher,
        brief_contract: NewsPublicationContract,
        story_contract: NewsPublicationContract,
        name: str = "news_ai_publish",
        clock_ms: Any | None = None,
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.publisher = publisher
        self.brief_contract = brief_contract
        self.story_contract = story_contract
        self.clock_ms = clock_ms or _now_ms

    async def run_once(self) -> WorkerResult:
        claimed = await asyncio.to_thread(self._claim)
        if not claimed:
            return WorkerResult(skipped=1, notes={"publications_claimed": 0})
        processed = 0
        failed = 0
        publication_ids: list[str] = []
        for publication_kind, attempt_key, lease_token, evidence in claimed:
            repair_count = 0
            try:
                result = await self._generate(publication_kind, evidence)
                draft, errors = validate_publication(
                    publication_kind=publication_kind,  # type: ignore[arg-type]
                    payload=result.payload,
                    evidence=evidence,
                )
                if errors:
                    repair_count = 1
                    repaired = await self.publisher.repair(
                        publication_kind=publication_kind,  # type: ignore[arg-type]
                        evidence=evidence,
                        validation_errors=errors[:20],
                    )
                    result = repaired
                    draft, errors = validate_publication(
                        publication_kind=publication_kind,  # type: ignore[arg-type]
                        payload=result.payload,
                        evidence=evidence,
                    )
                if draft is None or errors:
                    await asyncio.to_thread(
                        self._fail,
                        attempt_key,
                        lease_token,
                        "publication_validation_failed",
                        errors,
                        True,
                        repair_count,
                    )
                    failed += 1
                    continue
                publication_id = await asyncio.to_thread(
                    self._complete,
                    publication_kind,
                    attempt_key,
                    lease_token,
                    evidence,
                    draft.model_dump(mode="json"),
                    result.receipt,
                    repair_count,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    self._fail,
                    attempt_key,
                    lease_token,
                    f"{type(exc).__name__}:{exc}",
                    (),
                    False,
                    repair_count,
                )
                failed += 1
                continue
            processed += 1
            publication_ids.append(publication_id)
        return WorkerResult(
            processed=processed,
            failed=failed,
            notes={
                "publications_claimed": len(claimed),
                "publication_ids": publication_ids,
                "model": self.brief_contract.model,
            },
        )

    def _claim(
        self,
    ) -> list[tuple[str, str, str, BriefEvidenceBundle | StoryAnalysisEvidence]]:
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=self.settings.statement_timeout_seconds,
            ) as repos,
            repos.transaction(),
        ):
            return cast(
                list[
                    tuple[
                        str,
                        str,
                        str,
                        BriefEvidenceBundle | StoryAnalysisEvidence,
                    ]
                ],
                repos.news.claim_ai_work(
                    brief_contract=self.brief_contract,
                    story_contract=self.story_contract,
                    now_ms=int(self.clock_ms()),
                    limit=int(self.settings.batch_size),
                    lease_ms=int(self.settings.lease_ms),
                    max_attempts=int(self.settings.max_attempts),
                ),
            )

    async def _generate(
        self,
        publication_kind: str,
        evidence: BriefEvidenceBundle | StoryAnalysisEvidence,
    ) -> Any:
        if publication_kind == "brief":
            if not isinstance(evidence, BriefEvidenceBundle):
                raise ValueError("news_brief_evidence_required")
            return await self.publisher.synthesize_brief(evidence)
        if not isinstance(evidence, StoryAnalysisEvidence):
            raise ValueError("news_story_analysis_evidence_required")
        return await self.publisher.analyze_story(evidence)

    def _complete(
        self,
        publication_kind: str,
        attempt_key: str,
        lease_token: str,
        evidence: BriefEvidenceBundle | StoryAnalysisEvidence,
        payload: dict[str, Any],
        receipt: dict[str, Any],
        repair_count: int,
    ) -> str:
        evidence_references = _evidence_references(payload)
        contract = self.brief_contract if publication_kind == "brief" else self.story_contract
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=self.settings.statement_timeout_seconds,
            ) as repos,
            repos.transaction(),
        ):
            return cast(
                str,
                repos.news.complete_ai_publication(
                    publication_kind=publication_kind,
                    attempt_key=attempt_key,
                    lease_token=lease_token,
                    evidence=evidence,
                    contract=contract,
                    payload=payload,
                    evidence_references=evidence_references,
                    receipt=receipt,
                    published_at_ms=int(self.clock_ms()),
                    repair_count=repair_count,
                ),
            )

    def _fail(
        self,
        attempt_key: str,
        lease_token: str,
        error: str,
        validation_errors: Sequence[str],
        terminal: bool,
        repair_count: int,
    ) -> None:
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=self.settings.statement_timeout_seconds,
            ) as repos,
            repos.transaction(),
        ):
            repos.news.fail_ai_attempt(
                attempt_key=attempt_key,
                lease_token=lease_token,
                now_ms=int(self.clock_ms()),
                error=error,
                validation_errors=validation_errors,
                retry_ms=int(self.settings.retry_ms),
                terminal=terminal,
                repair_count=repair_count,
            )


def _evidence_references(payload: dict[str, Any]) -> list[str]:
    refs: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "evidence_references" and isinstance(nested, list | tuple):
                    refs.extend(str(item) for item in nested if str(item).strip())
                else:
                    visit(nested)
        elif isinstance(value, list | tuple):
            for nested in value:
                visit(nested)

    visit(payload)
    return list(dict.fromkeys(refs))


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "NewsAiPublishWorker",
    "NewsBriefPlanWorker",
    "NewsIngestWorker",
    "NewsStoryProjectWorker",
]
