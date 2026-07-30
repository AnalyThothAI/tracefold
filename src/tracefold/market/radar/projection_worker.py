from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from loguru import logger

from tracefold.market.radar.constants import (
    TOKEN_RADAR_DEFAULT_VENUE,
    TOKEN_RADAR_PROJECTION_VERSION,
)
from tracefold.market.radar.token_radar_projector import (
    TokenRadarProjector,
    prune_token_radar_private_cache,
)
from tracefold.market.radar.token_radar_publisher import TokenRadarPublisher
from tracefold.platform.config.settings import TokenRadarProjectionWorkerSettings
from tracefold.platform.validation import require_positive_int
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


class TokenRadarProjectionWorker(WorkerBase):
    def __init__(
        self,
        *,
        name: str,
        settings: TokenRadarProjectionWorkerSettings,
        db: Any,
        telemetry: Any,
    ) -> None:
        if db is None:
            raise RuntimeError("token_radar_projection_db_required")
        super().__init__(
            name=name,
            settings=settings,
            db=db,
            telemetry=telemetry,
        )
        self.windows = tuple(str(window).strip().lower() for window in settings.windows)
        self.venues = tuple(str(venue).strip().lower() for venue in settings.venues)
        hot_windows = tuple(str(window).strip().lower() for window in settings.hot_windows)
        self.hot_windows = tuple(window for window in hot_windows if window in self.windows)
        self.limit = settings.batch_size
        self.lease_ms = settings.lease_ms
        self.retry_ms = settings.retry_ms
        self.max_attempts = settings.max_attempts
        self.private_cache_retention_ms = settings.private_cache_retention_ms
        self.hot_interval_ms = int(self.interval_seconds * 1000)
        self.cold_interval_ms = int(settings.cold_interval_seconds * 1000)
        self._cursor = 0

    async def run_once(self, *, now_ms: int | None = None) -> WorkerResult:
        result = await asyncio.to_thread(self.rebuild_once, now_ms=now_ms)
        failed = sum(1 for item in result["windows"].values() if str(item.get("status") or "") == "failed")
        skipped = 1 if str(result.get("status") or "") == "idle" else 0
        processed = 0 if skipped else max(0, len(result["windows"]) - failed)
        return WorkerResult(
            processed=processed,
            failed=failed,
            skipped=skipped,
            notes={
                "computed_at_ms": result["computed_at_ms"],
                "rows_written": result["rows_written"],
                "source_rows": result["source_rows"],
                "hydrated_rows": result.get("hydrated_rows", 0),
                "window": result.get("window"),
                "status": result.get("status"),
                "reason": result.get("reason"),
                "claimed": result.get("claimed"),
                "merged_rank_set_triggers": result.get("merged_rank_set_triggers"),
                "catch_up_enqueued": result.get("catch_up_enqueued"),
                "private_cache_retention": result.get("private_cache_retention"),
                "windows": result["windows"],
            },
        )

    def rebuild_once(
        self,
        *,
        now_ms: int | None = None,
        windows: tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        computed_at_ms = int(now_ms if now_ms is not None else _now_ms())
        self.last_error = None
        original_windows = self.windows
        original_venues = self.venues
        original_hot_windows = self.hot_windows
        original_limit = self.limit
        if windows is not None:
            self.windows = tuple(windows)
            self.hot_windows = tuple(window for window in self.hot_windows if window in self.windows)
        if limit is not None:
            self.limit = require_positive_int(
                limit,
                error_code="token_radar_projection_limit_required",
            )
        try:
            return self._rebuild_once(computed_at_ms=computed_at_ms)
        finally:
            self.windows = original_windows
            self.venues = original_venues
            self.hot_windows = original_hot_windows
            self.limit = original_limit

    def _rebuild_once(self, *, computed_at_ms: int) -> dict[str, Any]:
        try:
            with self._worker_session() as repos:
                publication_work_items = self._next_work_items(
                    publication_state=_latest_publication_state_from_repos(
                        repos,
                        windows=self.windows,
                        venues=self.venues,
                    ),
                    computed_at_ms=computed_at_ms,
                )[0]
                with repos.transaction():
                    target_claims = repos.token_radar_dirty_targets.claim_due(
                        limit=self.limit,
                        lease_ms=self.lease_ms,
                        now_ms=computed_at_ms,
                        lease_owner=self.name,
                    )
            has_claims = bool(target_claims)
            if publication_work_items or has_claims:
                score_work_items = _dedupe_work_items(
                    _configured_score_work_items(windows=self.windows) if has_claims else publication_work_items
                )
                publish_work_items = _dedupe_work_items(publication_work_items)
                with self._worker_session() as repos:
                    projector = TokenRadarProjector(repos=repos)
                    publisher = TokenRadarPublisher(repos=repos, projector=projector)
                    metadata_work_items = score_work_items or publish_work_items
                    rebuild_kwargs: dict[str, Any] = {
                        "windows": tuple(dict.fromkeys(window for window, _venue in metadata_work_items)),
                        "work_items": _service_work_items(publish_work_items),
                        "now_ms": computed_at_ms,
                        "limit": self.limit,
                        "rank_limit": self.limit,
                        "lease_ms": self.lease_ms,
                        "retry_ms": self.retry_ms,
                        "max_attempts": self.max_attempts,
                        "lease_owner": self.name,
                        "claimed_targets": tuple(dict(claim) for claim in target_claims),
                    }
                    if has_claims:
                        rebuild_kwargs["score_work_items"] = _service_work_items(score_work_items)
                    result = publisher.rebuild_dirty_targets(**rebuild_kwargs)
            else:
                result = _idle_result(computed_at_ms=computed_at_ms)
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception(f"token radar dirty target projection failed: error={exc}")
            result = {
                "computed_at_ms": computed_at_ms,
                "rows_written": 0,
                "source_rows": 0,
                "status": "failed",
                "error": str(exc),
                "claimed": 0,
                "catch_up_enqueued": 0,
                "windows": {},
            }

        result = self._finalize_result(result=result, computed_at_ms=computed_at_ms)
        self._attach_private_cache_retention(result=result, computed_at_ms=computed_at_ms)

        if str(result.get("status") or "") == "failed":
            self.last_error = self.last_error or str(result.get("error") or "token radar projection failed")

        return result

    def _finalize_result(self, *, result: dict[str, Any], computed_at_ms: int) -> dict[str, Any]:
        result.setdefault("computed_at_ms", computed_at_ms)
        result.setdefault("rows_written", 0)
        result.setdefault("source_rows", 0)
        result.setdefault("hydrated_rows", 0)
        result.setdefault("windows", {})
        result.setdefault("claimed", 0)
        result.setdefault("catch_up_enqueued", 0)
        result["window"] = self.windows[0] if self.windows else None
        result["venue"] = self.venues[0] if self.venues else None
        return result

    def _attach_private_cache_retention(self, *, result: dict[str, Any], computed_at_ms: int) -> None:
        try:
            result["private_cache_retention"] = self._prune_private_cache(computed_at_ms=computed_at_ms)
            if (
                str(result["private_cache_retention"].get("status") or "") == "ready"
                and str(result.get("status") or "") == "idle"
            ):
                result["status"] = "ready"
                result["reason"] = "private_cache_retention"
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception(f"token radar private cache retention failed: error={exc}")
            result["private_cache_retention"] = {"status": "failed", "error": str(exc)}
            if str(result.get("status") or "") == "idle":
                result["status"] = "failed"
                result["error"] = str(exc)

    def _prune_private_cache(self, *, computed_at_ms: int) -> dict[str, Any]:
        with self._worker_session() as repos:
            return prune_token_radar_private_cache(
                repos=repos,
                windows=self.windows,
                now_ms=computed_at_ms,
                retention_ms=self.private_cache_retention_ms,
                limit=self.limit,
            )

    def _next_work_items(
        self,
        *,
        publication_state: dict[tuple[str, str], dict[str, Any]],
        computed_at_ms: int,
    ) -> tuple[list[tuple[str, str]], tuple[str, str] | None]:
        hot_items = self._hot_work_items(
            publication_state=publication_state,
            computed_at_ms=computed_at_ms,
        )
        background_item = self._next_background_window(
            publication_state=publication_state,
            computed_at_ms=computed_at_ms,
        )
        work_items = list(hot_items)
        if background_item is not None and background_item not in work_items:
            work_items.append(background_item)
        work_items = _dedupe_work_items(work_items)
        if not work_items:
            return [], None
        return work_items, background_item or work_items[-1]

    def _next_background_window(
        self,
        *,
        publication_state: dict[tuple[str, str], dict[str, Any]],
        computed_at_ms: int,
    ) -> tuple[str, str] | None:
        work_items = [
            (window, venue) for window in self.windows if window not in self.hot_windows for venue in self.venues
        ]
        if not work_items:
            return None
        for _ in range(len(work_items)):
            item = work_items[self._cursor % len(work_items)]
            self._cursor += 1
            if _publication_due(
                publication_state.get(item),
                computed_at_ms=computed_at_ms,
                interval_ms=self.cold_interval_ms,
                failed_retry_ms=self.cold_interval_ms,
            ):
                return item
        return None

    def _hot_work_items(
        self,
        *,
        publication_state: dict[tuple[str, str], dict[str, Any]],
        computed_at_ms: int,
    ) -> list[tuple[str, str]]:
        due: list[tuple[str, str]] = []
        for window in self.hot_windows:
            for venue in self.venues:
                item = (window, venue)
                if _publication_due(
                    publication_state.get(item),
                    computed_at_ms=computed_at_ms,
                    interval_ms=self.hot_interval_ms,
                    failed_retry_ms=self.cold_interval_ms,
                ):
                    due.append(item)
        return due

    @contextmanager
    def _worker_session(self) -> Iterator[Any]:
        with self.db.worker_session(
            self.name,
            statement_timeout_seconds=self.settings.statement_timeout_seconds,
        ) as repos:
            yield repos


def _now_ms() -> int:
    return int(time.time() * 1000)


def _idle_result(*, computed_at_ms: int) -> dict[str, Any]:
    return {
        "computed_at_ms": computed_at_ms,
        "rows_written": 0,
        "source_rows": 0,
        "hydrated_rows": 0,
        "status": "idle",
        "reason": "no_due_work_items",
        "claimed": 0,
        "catch_up_enqueued": 0,
        "windows": {},
    }


def _publication_due(
    state: dict[str, Any] | None,
    *,
    computed_at_ms: int,
    interval_ms: int,
    failed_retry_ms: int,
) -> bool:
    if not state:
        return True

    status = str(state.get("latest_attempt_status") or "")
    published_at_ms = _state_ms(state, "last_published_at_ms", "current_published_at_ms")

    if status == "ready":
        if published_at_ms is None:
            return True
        return _elapsed_due(
            computed_at_ms=computed_at_ms,
            since_ms=published_at_ms,
            interval_ms=interval_ms,
        )

    if status == "failed":
        if published_at_ms is not None or state.get("current_generation_id") is not None:
            retry_since_ms = (
                _state_ms(
                    state,
                    "latest_attempt_finished_at_ms",
                    "updated_at_ms",
                    "latest_attempt_started_at_ms",
                )
                or published_at_ms
            )
            return _elapsed_due(
                computed_at_ms=computed_at_ms,
                since_ms=retry_since_ms,
                interval_ms=failed_retry_ms,
            )
        failed_at_ms = _state_ms(
            state,
            "latest_attempt_finished_at_ms",
            "updated_at_ms",
            "latest_attempt_started_at_ms",
        )
        return failed_at_ms is None or _elapsed_due(
            computed_at_ms=computed_at_ms,
            since_ms=failed_at_ms,
            interval_ms=interval_ms,
        )

    if published_at_ms is None:
        return True
    return _elapsed_due(
        computed_at_ms=computed_at_ms,
        since_ms=published_at_ms,
        interval_ms=interval_ms,
    )


def _elapsed_due(*, computed_at_ms: int, since_ms: int | None, interval_ms: int) -> bool:
    if since_ms is None:
        return True
    return computed_at_ms - int(since_ms) >= interval_ms


def _state_ms(state: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = state.get(key)
        if value is not None:
            return int(value)
    return None


def _dedupe_work_items(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _configured_score_work_items(
    *,
    windows: tuple[str, ...],
) -> list[tuple[str, str]]:
    return [(window, TOKEN_RADAR_DEFAULT_VENUE) for window in windows]


def _service_work_items(items: list[tuple[str, str]]) -> tuple[tuple[str, ...], ...]:
    return tuple(items)


def _latest_publication_state_from_repos(
    repos: Any,
    *,
    windows: tuple[str, ...],
    venues: tuple[str, ...],
) -> dict[tuple[str, str], dict[str, Any]]:
    return cast(
        dict[tuple[str, str], dict[str, Any]],
        repos.token_radar.latest_publication_state(
            projection_version=TOKEN_RADAR_PROJECTION_VERSION,
            windows=windows,
            venues=venues,
        ),
    )
