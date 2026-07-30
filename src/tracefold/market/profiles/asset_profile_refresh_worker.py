from __future__ import annotations

import time
from typing import Any

from tracefold.market.profiles.asset_profile_refresh import (
    fetch_asset_profile,
    write_error_asset_profile,
    write_missing_asset_profile,
    write_ready_asset_profile,
    write_unsupported_asset_profile,
)
from tracefold.market.profiles.profile_projection import PROFILE_PROJECTION_VERSION
from tracefold.market.provider_contracts import (
    DexProfileSource,
    DexProviderTemporarilyUnavailable,
    DexTokenProfile,
)
from tracefold.platform.config.settings import AssetProfileRefreshWorkerSettings
from tracefold.platform.postgres.projection_frontier import PROFILE_FRONTIER
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult

_CLAIM_LEASE_MS = 120_000
_PROVIDER_RETRY_MS = 300_000
_READY_REFRESH_MS = 6 * 60 * 60_000
_MISSING_RETRY_MS = (15 * 60_000, 30 * 60_000, 60 * 60_000, 120 * 60_000)
_ERROR_RETRY_BASE_MS = 15 * 60_000
_ERROR_RETRY_CAP_MS = 24 * 60 * 60_000
_ERROR_MAX_ATTEMPTS = 5
_STATEMENT_TIMEOUT_SECONDS = 3.0
_PROVIDER_LANES = {
    "gmgn_dex_profile": "profile_gmgn",
    "binance_web3_profile": "profile_binance",
}


class AssetProfileRefreshWorker(WorkerBase):
    """One host for two durable provider lanes with no connection held over I/O."""

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
        unknown = sorted(
            source.provider for source in self.dex_profile_sources if source.provider not in _PROVIDER_LANES
        )
        if unknown:
            raise ValueError(f"asset_profile_provider_invalid:{','.join(unknown)}")
        self._source_cursor = 0

    async def run_once(self, *, now_ms: int | None = None) -> WorkerResult:
        observed_at_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        if not self.dex_profile_sources:
            return WorkerResult(
                skipped=1,
                notes={"reason": "no_asset_profile_sources", "claimed": 0},
            )

        selected = await self._claim_next(observed_at_ms)
        if selected is None:
            return WorkerResult(
                skipped=1,
                notes={"reason": "no_due_asset_profile_refresh_targets", "claimed": 0},
            )
        profile_source, claim, queue = selected

        try:
            async with self.require_provider_governor().acquire(
                host=profile_source.provider,
                lane=_PROVIDER_LANES[profile_source.provider],
            ):
                profile = await self.require_runtime_resources().run_provider_io(
                    fetch_asset_profile,
                    profile_source=profile_source,
                    row=claim,
                )
        except DexProviderTemporarilyUnavailable as exc:
            publish = await self.require_runtime_resources().run_background_db(
                self._publish_provider_failure,
                claim,
                exc,
                observed_at_ms,
            )
            return self._result(
                status="provider_blocked",
                queue=queue,
                publish=publish,
                error=str(exc),
            )
        except Exception as exc:
            publish = await self.require_runtime_resources().run_background_db(
                self._publish_target_error,
                claim,
                exc,
                observed_at_ms,
            )
            return self._result(
                status="error",
                queue=queue,
                publish=publish,
                error=str(exc),
            )

        publish = await self.require_runtime_resources().run_background_db(
            self._publish_profile,
            claim,
            profile,
            observed_at_ms,
        )
        return self._result(
            status="ready" if isinstance(profile, DexTokenProfile) else "missing",
            queue=queue,
            publish=publish,
        )

    async def _claim_next(
        self,
        now_ms: int,
    ) -> tuple[DexProfileSource, dict[str, Any], dict[str, int]] | None:
        source_count = len(self.dex_profile_sources)
        resources = self.require_runtime_resources()
        for offset in range(source_count):
            index = (self._source_cursor + offset) % source_count
            source = self.dex_profile_sources[index]
            claim, queue = await resources.run_background_db(
                self._claim_source,
                source.provider,
                now_ms,
            )
            if claim is None:
                continue
            self._source_cursor = (index + 1) % source_count
            return source, claim, queue
        self._source_cursor = (self._source_cursor + 1) % source_count
        return None

    def _claim_source(
        self,
        provider: str,
        now_ms: int,
    ) -> tuple[dict[str, Any] | None, dict[str, int]]:
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=_STATEMENT_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            if not repos.provider_circuits.can_attempt(provider=provider, now_ms=now_ms):
                return None, {"due": 0, "oldest_due_age_ms": 0}
            rows = repos.asset_profile_refresh_targets.claim_due(
                provider=provider,
                now_ms=now_ms,
                limit=1,
                lease_owner=self.claim_owner,
                lease_ms=_CLAIM_LEASE_MS,
            )
            queue = repos.asset_profile_refresh_targets.queue_health(
                provider=provider,
                now_ms=now_ms,
            )
        return (rows[0] if rows else None), queue

    def _publish_provider_failure(
        self,
        claim: dict[str, Any],
        exc: Exception,
        now_ms: int,
    ) -> dict[str, Any]:
        due_at_ms = int(now_ms) + _PROVIDER_RETRY_MS
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=_STATEMENT_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            repos.provider_circuits.open(
                provider=str(claim["provider"]),
                error=str(exc),
                now_ms=now_ms,
                retry_ms=_PROVIDER_RETRY_MS,
            )
            released = repos.asset_profile_refresh_targets.release_provider_failure(
                claim,
                due_at_ms=due_at_ms,
                now_ms=now_ms,
            )
            if released != 1:
                raise RuntimeError("asset_profile_provider_failure_claim_stale")
        return {
            "rows_written": 0,
            "terminal": 0,
            "next_attempt_at_ms": due_at_ms,
            "target_attempt_consumed": False,
        }

    def _publish_target_error(
        self,
        claim: dict[str, Any],
        exc: Exception,
        now_ms: int,
    ) -> dict[str, Any]:
        attempt_count = int(claim["attempt_count"])
        terminal_reason = _terminal_error_reason(
            exc,
            attempt_count=attempt_count,
            max_attempts=_ERROR_MAX_ATTEMPTS,
        )
        retry_delay_ms = _retry_delay_ms(
            base_ms=_ERROR_RETRY_BASE_MS,
            attempt_count=attempt_count,
            cap_ms=_ERROR_RETRY_CAP_MS,
        )
        next_refresh_at_ms = int(now_ms) + retry_delay_ms
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=_STATEMENT_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            repos.provider_circuits.close(
                provider=str(claim["provider"]),
                now_ms=now_ms,
            )
            if terminal_reason == "profile_unsupported":
                write_unsupported_asset_profile(
                    repos=repos,
                    provider=str(claim["provider"]),
                    row=claim,
                    exc=exc,
                    now_ms=now_ms,
                )
            else:
                write_error_asset_profile(
                    repos=repos,
                    provider=str(claim["provider"]),
                    row=claim,
                    exc=exc,
                    now_ms=now_ms,
                    next_refresh_at_ms=next_refresh_at_ms,
                )
            if terminal_reason is not None:
                changed = repos.asset_profile_refresh_targets.mark_terminal(
                    [claim],
                    reason=terminal_reason,
                    now_ms=now_ms,
                )
            else:
                changed = repos.asset_profile_refresh_targets.reschedule(
                    [claim],
                    due_at_ms=next_refresh_at_ms,
                    now_ms=now_ms,
                    reason="profile_error_written",
                )
            if changed != 1:
                raise RuntimeError("asset_profile_target_error_claim_stale")
            _enqueue_profile_current(repos=repos, row=claim, now_ms=now_ms)
        return {
            "rows_written": 1,
            "terminal": int(terminal_reason is not None),
            "next_attempt_at_ms": next_refresh_at_ms,
            "target_attempt_consumed": True,
        }

    def _publish_profile(
        self,
        claim: dict[str, Any],
        profile: DexTokenProfile | None,
        now_ms: int,
    ) -> dict[str, Any]:
        attempt_count = int(claim["attempt_count"])
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=_STATEMENT_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            repos.provider_circuits.close(
                provider=str(claim["provider"]),
                now_ms=now_ms,
            )
            if isinstance(profile, DexTokenProfile):
                next_refresh_at_ms = int(now_ms) + _READY_REFRESH_MS
                write_ready_asset_profile(
                    repos=repos,
                    provider=str(claim["provider"]),
                    row=claim,
                    profile=profile,
                    now_ms=now_ms,
                    next_refresh_at_ms=next_refresh_at_ms,
                )
                changed = repos.asset_profile_refresh_targets.reschedule(
                    [claim],
                    due_at_ms=next_refresh_at_ms,
                    now_ms=now_ms,
                    reason="profile_ready_written",
                    reset_attempts=True,
                )
                terminal = False
            else:
                retry_delay_ms = _missing_retry_delay_ms(attempt_count)
                next_refresh_at_ms = int(now_ms) + retry_delay_ms
                write_missing_asset_profile(
                    repos=repos,
                    provider=str(claim["provider"]),
                    row=claim,
                    now_ms=now_ms,
                    next_refresh_at_ms=next_refresh_at_ms,
                )
                terminal = attempt_count >= len(_MISSING_RETRY_MS)
                if terminal:
                    changed = repos.asset_profile_refresh_targets.mark_terminal(
                        [claim],
                        reason="profile_missing_after_max_attempts",
                        now_ms=now_ms,
                    )
                else:
                    changed = repos.asset_profile_refresh_targets.reschedule(
                        [claim],
                        due_at_ms=next_refresh_at_ms,
                        now_ms=now_ms,
                        reason="profile_missing_written",
                    )
            if changed != 1:
                raise RuntimeError("asset_profile_publish_claim_stale")
            _enqueue_profile_current(repos=repos, row=claim, now_ms=now_ms)
        return {
            "rows_written": 1,
            "terminal": int(terminal),
            "next_attempt_at_ms": next_refresh_at_ms,
            "target_attempt_consumed": True,
        }

    @staticmethod
    def _result(
        *,
        status: str,
        queue: dict[str, int],
        publish: dict[str, Any],
        error: str | None = None,
    ) -> WorkerResult:
        failed = int(status in {"error", "provider_blocked"})
        processed = int(status in {"ready", "missing"})
        return WorkerResult(
            processed=processed,
            failed=failed,
            notes={
                "claimed": 1,
                "status": status,
                "queue_depth": int(queue.get("due") or 0),
                "oldest_due_age_ms": int(queue.get("oldest_due_age_ms") or 0),
                "rows_written": int(publish.get("rows_written") or 0),
                "terminal": int(publish.get("terminal") or 0),
                "target_attempt_consumed": bool(publish.get("target_attempt_consumed")),
                "next_attempt_at_ms": publish.get("next_attempt_at_ms"),
                **({"last_error": str(error)[:500]} if error else {}),
            },
        )


def _enqueue_profile_current(*, repos: Any, row: dict[str, Any], now_ms: int) -> None:
    _required_source_watermark_ms(
        row,
        error="asset_profile_refresh_source_watermark_required",
    )
    repos.projection_frontiers.mark_dirty(
        PROFILE_FRONTIER,
        key={
            "target_type": "Asset",
            "target_id": str(row["target_id"]),
        },
        dirty_at_ms=int(now_ms),
        deadline_at_ms=int(now_ms) + 30_000,
        input_fingerprint=(f"asset-profile:{row['provider']}:{row['payload_hash']}:{int(now_ms)}"),
        version=PROFILE_PROJECTION_VERSION,
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


def _missing_retry_delay_ms(attempt_count: int) -> int:
    index = min(max(1, int(attempt_count)), len(_MISSING_RETRY_MS)) - 1
    return _MISSING_RETRY_MS[index]


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


__all__ = ["AssetProfileRefreshWorker"]
