from __future__ import annotations

import asyncio
import time
from typing import Any

from tracefold.market.profiles.asset_profile_refresh import (
    fetch_asset_profile,
    write_error_asset_profile,
    write_missing_asset_profile,
    write_ready_asset_profile,
    write_unsupported_asset_profile,
)
from tracefold.market.provider_contracts import (
    DexProfileSource,
    DexProviderTemporarilyUnavailable,
    DexTokenProfile,
)
from tracefold.platform.config.settings import AssetProfileRefreshWorkerSettings
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


class AssetProfileRefreshWorker(WorkerBase):
    def __init__(
        self,
        *,
        name: str,
        settings: AssetProfileRefreshWorkerSettings,
        db: Any,
        telemetry: Any,
        dex_profile_sources: tuple[DexProfileSource, ...] = (),
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.dex_profile_sources = tuple(dex_profile_sources)

    async def run_once(self, *, now_ms: int | None = None) -> WorkerResult:
        observed_at_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        result = await asyncio.to_thread(self._refresh_once, observed_at_ms)
        return WorkerResult(
            processed=int(result.get("ready") or 0) + int(result.get("missing") or 0),
            failed=int(result.get("error") or 0) + int(result.get("provider_blocked") or 0),
            skipped=int(result.get("skipped") or 0),
            notes={
                "claimed": int(result.get("claimed") or 0),
                "queue_depth": int(result.get("queue_depth") or 0),
                "oldest_due_age_ms": int(result.get("oldest_due_age_ms") or 0),
                "source_rows_scanned": int(result.get("source_rows_scanned") or 0),
                "targets_loaded": int(result.get("targets_loaded") or 0),
                "rows_written": int(result.get("rows_written") or 0),
                "terminal": int(result.get("terminal") or 0),
                "result": result,
            },
        )

    def _refresh_once(self, now_ms: int) -> dict[str, Any]:
        result: dict[str, Any] = {
            "providers": [source.provider for source in self.dex_profile_sources],
            "selected": 0,
            "claimed": 0,
            "queue_depth": 0,
            "oldest_due_age_ms": 0,
            "queue_hot": 0,
            "queue_warm": 0,
            "queue_cold": 0,
            "queue_terminal": 0,
            "source_rows_scanned": 0,
            "targets_loaded": 0,
            "rows_written": 0,
            "ready": 0,
            "missing": 0,
            "error": 0,
            "provider_blocked": 0,
            "terminal": 0,
            "skipped": 0,
            "sources": {},
            "started_at_ms": int(now_ms),
            "finished_at_ms": int(now_ms),
        }
        if not self.dex_profile_sources:
            result["skipped"] = 1
            return result
        for profile_source in self.dex_profile_sources:
            source_result = self._refresh_source_once(profile_source=profile_source, now_ms=now_ms)
            result["sources"][profile_source.provider] = source_result
            for key in (
                "selected",
                "claimed",
                "queue_depth",
                "source_rows_scanned",
                "targets_loaded",
                "rows_written",
                "ready",
                "missing",
                "error",
                "provider_blocked",
                "terminal",
                "skipped",
            ):
                result[key] += int(source_result.get(key) or 0)
            queue = dict(source_result.get("queue") or {})
            result["oldest_due_age_ms"] = max(
                int(result["oldest_due_age_ms"]),
                int(queue.get("oldest_due_age_ms") or 0),
            )
            for queue_key, health_key in (
                ("queue_hot", "hot"),
                ("queue_warm", "warm"),
                ("queue_cold", "cold"),
                ("queue_terminal", "terminal"),
            ):
                result[queue_key] += int(queue.get(health_key) or 0)
        return result

    def _refresh_source_once(self, *, profile_source: DexProfileSource, now_ms: int) -> dict[str, Any]:
        source_result: dict[str, Any] = {
            "provider": profile_source.provider,
            "selected": 0,
            "claimed": 0,
            "queue_depth": 0,
            "source_rows_scanned": 0,
            "targets_loaded": 0,
            "rows_written": 0,
            "ready": 0,
            "missing": 0,
            "error": 0,
            "provider_blocked": 0,
            "terminal": 0,
            "skipped": 0,
            "started_at_ms": int(now_ms),
            "finished_at_ms": int(now_ms),
        }
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=self.settings.statement_timeout_seconds,
            ) as repos,
            repos.transaction(),
        ):
            rows = repos.asset_profile_refresh_targets.claim_due(
                provider=profile_source.provider,
                now_ms=now_ms,
                limit=self.settings.batch_size,
                lease_owner=self.name,
                lease_ms=self.settings.lease_ms,
            )
            queue_health = repos.asset_profile_refresh_targets.queue_health(
                provider=profile_source.provider,
                now_ms=now_ms,
            )
            source_result["queue_depth"] = queue_health["due"]
            source_result["queue"] = queue_health
        source_result["selected"] = len(rows)
        source_result["claimed"] = len(rows)
        source_result["targets_loaded"] = len(rows)
        if not rows:
            source_result["skipped"] = 1
            source_result["reason"] = "no_due_asset_profile_refresh_targets"
            return source_result
        ready_refresh_ms = self.settings.ready_refresh_ms
        missing_refresh_ms = self.settings.missing_refresh_ms
        error_refresh_ms = self.settings.error_refresh_ms
        backoff_cap_ms = self.settings.retry_backoff_cap_ms
        for row in rows:
            try:
                profile = fetch_asset_profile(profile_source=profile_source, row=row)
            except DexProviderTemporarilyUnavailable as exc:
                source_result["provider_blocked"] = 1
                source_result["last_error"] = str(exc)[:500]
                self._reschedule_claims(
                    [item for item in rows if int(item.get("due_at_ms") or 0) <= int(now_ms)],
                    due_at_ms=now_ms + self.settings.provider_retry_ms,
                    now_ms=now_ms,
                    reason="provider_blocked",
                    reset_attempts=True,
                )
                break
            except Exception as exc:
                terminal_reason = _terminal_error_reason(
                    exc,
                    attempt_count=int(row["attempt_count"]),
                    max_attempts=self.settings.error_max_attempts,
                )
                retry_delay_ms = _retry_delay_ms(
                    base_ms=error_refresh_ms,
                    attempt_count=int(row["attempt_count"]),
                    cap_ms=backoff_cap_ms,
                )
                next_refresh_at_ms = now_ms + retry_delay_ms
                with (
                    self.db.worker_session(
                        self.name,
                        statement_timeout_seconds=self.settings.statement_timeout_seconds,
                    ) as repos,
                    repos.transaction(),
                ):
                    if terminal_reason == "profile_unsupported":
                        write_unsupported_asset_profile(
                            repos=repos,
                            provider=profile_source.provider,
                            row=row,
                            exc=exc,
                            now_ms=now_ms,
                        )
                    else:
                        write_error_asset_profile(
                            repos=repos,
                            provider=profile_source.provider,
                            row=row,
                            exc=exc,
                            now_ms=now_ms,
                            next_refresh_at_ms=next_refresh_at_ms,
                        )
                    if terminal_reason is not None:
                        repos.asset_profile_refresh_targets.mark_terminal(
                            [row],
                            reason=terminal_reason,
                            now_ms=now_ms,
                        )
                    else:
                        repos.asset_profile_refresh_targets.reschedule(
                            [row],
                            due_at_ms=next_refresh_at_ms,
                            now_ms=now_ms,
                            reason="profile_error_written",
                        )
                    _enqueue_profile_current(repos=repos, row=row, now_ms=now_ms)
                source_result["rows_written"] += 1
                source_result["error"] += 1
                source_result["terminal"] += int(terminal_reason is not None)
                continue
            with (
                self.db.worker_session(
                    self.name,
                    statement_timeout_seconds=self.settings.statement_timeout_seconds,
                ) as repos,
                repos.transaction(),
            ):
                if isinstance(profile, DexTokenProfile):
                    next_refresh_at_ms = now_ms + ready_refresh_ms
                    write_ready_asset_profile(
                        repos=repos,
                        provider=profile_source.provider,
                        row=row,
                        profile=profile,
                        now_ms=now_ms,
                        next_refresh_at_ms=next_refresh_at_ms,
                    )
                    repos.asset_profile_refresh_targets.reschedule(
                        [row],
                        due_at_ms=next_refresh_at_ms,
                        now_ms=now_ms,
                        reason="profile_ready_written",
                        reset_attempts=True,
                    )
                    source_result["ready"] += 1
                else:
                    terminal = int(row["attempt_count"]) >= self.settings.missing_max_attempts
                    retry_delay_ms = _retry_delay_ms(
                        base_ms=missing_refresh_ms,
                        attempt_count=int(row["attempt_count"]),
                        cap_ms=backoff_cap_ms,
                    )
                    next_refresh_at_ms = now_ms + retry_delay_ms
                    write_missing_asset_profile(
                        repos=repos,
                        provider=profile_source.provider,
                        row=row,
                        now_ms=now_ms,
                        next_refresh_at_ms=next_refresh_at_ms,
                    )
                    if terminal:
                        repos.asset_profile_refresh_targets.mark_terminal(
                            [row],
                            reason="profile_missing_after_max_attempts",
                            now_ms=now_ms,
                        )
                    else:
                        repos.asset_profile_refresh_targets.reschedule(
                            [row],
                            due_at_ms=next_refresh_at_ms,
                            now_ms=now_ms,
                            reason="profile_missing_written",
                        )
                    source_result["missing"] += 1
                    source_result["terminal"] += int(terminal)
                _enqueue_profile_current(repos=repos, row=row, now_ms=now_ms)
                source_result["rows_written"] += 1
        return source_result

    def _reschedule_claims(
        self,
        claims: list[dict[str, Any]],
        *,
        due_at_ms: int,
        now_ms: int,
        reason: str,
        reset_attempts: bool = False,
    ) -> None:
        if not claims:
            return
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=self.settings.statement_timeout_seconds,
            ) as repos,
            repos.transaction(),
        ):
            repos.asset_profile_refresh_targets.reschedule(
                claims,
                due_at_ms=due_at_ms,
                now_ms=now_ms,
                reason=reason,
                reset_attempts=reset_attempts,
            )


def _enqueue_profile_current(*, repos: Any, row: dict[str, Any], now_ms: int) -> None:
    source_watermark_ms = _required_source_watermark_ms(row, error="asset_profile_refresh_source_watermark_required")
    repos.token_profile_current_dirty_targets.enqueue_targets(
        [
            {
                "target_type": "Asset",
                "target_id": str(row["target_id"]),
                "source_watermark_ms": source_watermark_ms,
                "priority": 40,
            }
        ],
        reason="asset_profile_refresh_changed",
        now_ms=now_ms,
    )


def _required_source_watermark_ms(row: dict[str, Any], *, error: str) -> int:
    try:
        value = row["source_watermark_ms"]
    except KeyError as exc:
        raise RuntimeError(error) from exc
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(error)
    if value <= 0:
        raise RuntimeError(error)
    return int(value)


def _retry_delay_ms(*, base_ms: int, attempt_count: int, cap_ms: int) -> int:
    exponent = max(0, int(attempt_count) - 1)
    multiplier = int(2**exponent)
    return min(int(cap_ms), int(base_ms) * multiplier)


def _terminal_error_reason(
    exc: Exception,
    *,
    attempt_count: int,
    max_attempts: int,
) -> str | None:
    error = str(exc).strip().lower()
    if error.startswith("unsupported_") or "unsupported chain" in error:
        return "profile_unsupported"
    if int(attempt_count) >= int(max_attempts):
        return "profile_error_after_max_attempts"
    return None
