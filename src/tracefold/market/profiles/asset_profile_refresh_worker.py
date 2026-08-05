from __future__ import annotations

import asyncio
import time
from typing import Any

from tracefold.market.profiles.asset_profile_refresh import (
    fetch_asset_profile,
    write_missing_asset_profile,
    write_ready_asset_profile,
)
from tracefold.market.profiles.profile_projection import PROFILE_PROJECTION_VERSION
from tracefold.market.provider_contracts import (
    DexProfileSource,
    DexTokenProfile,
    MarketProviderExpectedError,
)
from tracefold.platform.postgres.projection_frontier import PROFILE_FRONTIER
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

_CLAIM_LEASE_MS = 120_000
_PROVIDER_RETRY_MS = 300_000
_READY_REFRESH_MS = 6 * 60 * 60_000
_MISSING_RETRY_MS = (15 * 60_000, 30 * 60_000, 60 * 60_000, 120 * 60_000)
_STATEMENT_TIMEOUT_SECONDS = 3.0
_PROFILE_PROVIDERS = frozenset({"gmgn_dex_profile", "binance_web3_profile"})


class AssetProfileRefresh:
    def __init__(
        self,
        *,
        db: Any,
        finite_operations: Any,
        runtime_id: str,
        dex_profile_sources: tuple[DexProfileSource, ...] = (),
    ) -> None:
        self.db = db
        self.finite_operations = finite_operations
        self.name = "asset_profile_refresh"
        self.claim_owner = f"asset_profile_refresh:{runtime_id}"
        self.dex_profile_sources = tuple(dex_profile_sources)
        unknown = sorted(
            source.provider for source in self.dex_profile_sources if source.provider not in _PROFILE_PROVIDERS
        )
        if unknown:
            raise ValueError(f"asset_profile_provider_invalid:{','.join(unknown)}")
        self._source_cursor = 0

    async def turn(self, *, now_ms: int | None = None) -> str | bool | None:
        observed_at_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        if not self.dex_profile_sources:
            return False

        try:
            selected = await self._claim_next(observed_at_ms)
        except ResourceAdmissionTimeout:
            return None
        if selected is None:
            return False
        profile_source, claim, _queue = selected

        submitted = False

        def mark_submitted() -> None:
            nonlocal submitted
            submitted = True

        try:
            profile = await self.finite_operations.run(
                "asset_profile_fetch",
                fetch_asset_profile,
                profile_source=profile_source,
                row=claim,
                timeout_seconds=30.0,
                on_submitted=mark_submitted,
            )
        except asyncio.CancelledError:
            if not submitted:
                await asyncio.shield(self._release_prework(claim))
            raise
        except ResourceAdmissionTimeout:
            await self._release_prework(claim)
            return None
        except (MarketProviderExpectedError, ResourceOperationOverrun) as exc:
            provider_error = (
                MarketProviderExpectedError("asset_profile_fetch_timeout")
                if isinstance(exc, ResourceOperationOverrun)
                else exc
            )
            try:
                published = await self.db.run_business(
                    "asset_profile_publish_unavailable",
                    self._publish_provider_failure,
                    claim,
                    provider_error,
                    observed_at_ms,
                    operation_timeout_seconds=3.0,
                )
            except ResourceAdmissionTimeout:
                await self._release_prework(claim)
                return None
            if published is None:
                return None
            return "failed"

        try:
            published = await self.db.run_business(
                "asset_profile_publish",
                self._publish_profile,
                claim,
                profile,
                observed_at_ms,
                operation_timeout_seconds=3.0,
            )
        except ResourceAdmissionTimeout:
            await self._release_prework(claim)
            return None
        if published is None:
            return None
        return "terminal" if int(published["terminal"]) else "processed"

    async def _release_prework(self, claim: dict[str, Any]) -> bool:
        try:
            released = await self.db.run_business(
                "asset_profile_release_prework",
                self._release_prework_sync,
                claim,
                operation_timeout_seconds=0.5,
            )
        except ResourceAdmissionTimeout:
            return False
        return bool(released)

    def _release_prework_sync(self, claim: dict[str, Any]) -> bool:
        with self.db.worker_session(self.name, 0.5) as repos, repos.transaction():
            return bool(repos.asset_profile_refresh_targets.release_prework(claim))

    async def _claim_next(
        self,
        now_ms: int,
    ) -> tuple[DexProfileSource, dict[str, Any], dict[str, int]] | None:
        source_count = len(self.dex_profile_sources)
        for offset in range(source_count):
            index = (self._source_cursor + offset) % source_count
            source = self.dex_profile_sources[index]
            claim, queue = await self.db.run_business(
                "asset_profile_claim",
                self._claim_source,
                source.provider,
                now_ms,
                operation_timeout_seconds=3.0,
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
    ) -> dict[str, Any] | None:
        due_at_ms = int(now_ms) + _PROVIDER_RETRY_MS
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=_STATEMENT_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            released = repos.asset_profile_refresh_targets.release_provider_failure(
                claim,
                due_at_ms=due_at_ms,
                now_ms=now_ms,
            )
            if released == 0:
                return None
            if released != 1:
                raise RuntimeError("asset_profile_provider_failure_claim_stale")
            repos.provider_circuits.open(
                provider=str(claim["provider"]),
                error=str(exc),
                now_ms=now_ms,
                retry_ms=_PROVIDER_RETRY_MS,
            )
        return {
            "rows_written": 0,
            "terminal": 0,
            "next_attempt_at_ms": due_at_ms,
            "target_attempt_consumed": False,
        }

    def _publish_profile(
        self,
        claim: dict[str, Any],
        profile: DexTokenProfile | None,
        now_ms: int,
    ) -> dict[str, Any] | None:
        attempt_count = int(claim["attempt_count"])
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=_STATEMENT_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            if isinstance(profile, DexTokenProfile):
                next_refresh_at_ms = int(now_ms) + _READY_REFRESH_MS
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
            if changed == 0:
                return None
            if changed != 1:
                raise RuntimeError("asset_profile_publish_claim_stale")
            repos.provider_circuits.close(
                provider=str(claim["provider"]),
                now_ms=now_ms,
            )
            if isinstance(profile, DexTokenProfile):
                write_ready_asset_profile(
                    repos=repos,
                    provider=str(claim["provider"]),
                    row=claim,
                    profile=profile,
                    now_ms=now_ms,
                    next_refresh_at_ms=next_refresh_at_ms,
                )
            else:
                write_missing_asset_profile(
                    repos=repos,
                    provider=str(claim["provider"]),
                    row=claim,
                    now_ms=now_ms,
                    next_refresh_at_ms=next_refresh_at_ms,
                )
            _enqueue_profile_current(repos=repos, row=claim, now_ms=now_ms)
        return {
            "rows_written": 1,
            "terminal": int(terminal),
            "next_attempt_at_ms": next_refresh_at_ms,
            "target_attempt_consumed": True,
        }


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


__all__ = ["AssetProfileRefresh"]
