from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from tracefold.market.profiles.profile_projection import PROFILE_PROJECTION_VERSION
from tracefold.market.profiles.token_image_mirror import mirror_token_image_source
from tracefold.platform.postgres.projection_frontier import PROFILE_FRONTIER
from tracefold.platform.resource import ResourceAdmissionTimeout

_CLAIM_LEASE_MS = 120_000
_RETRY_MS = 300_000
_MAX_ATTEMPTS = 3
_STATEMENT_TIMEOUT_SECONDS = 3.0


class TokenImageMirror:
    """Mirror one durable image shard with no DB connection over provider I/O."""

    def __init__(
        self,
        *,
        db: Any,
        app_home: str | Path,
        finite_operations: Any,
        runtime_id: str,
        http_client: Any | None = None,
    ) -> None:
        self.db = db
        self.finite_operations = finite_operations
        self.name = "token_image_mirror"
        self.claim_owner = f"token_image_mirror:{runtime_id}"
        self.app_home = Path(app_home)
        self.http_client = http_client

    async def turn(self, *, now_ms: int | None = None) -> str | bool | None:
        observed_at_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        try:
            claimed = await self.db.run_business(
                "token_image_claim",
                self._claim_one,
                observed_at_ms,
                operation_timeout_seconds=3.0,
            )
        except ResourceAdmissionTimeout:
            return None
        if claimed is None:
            return False
        claim, terminal_asset, _queue_depth = claimed
        if terminal_asset is not None:
            submitted = False

            def mark_existing_submitted() -> None:
                nonlocal submitted
                submitted = True

            try:
                published = await self.db.run_business(
                    "token_image_publish_existing",
                    self._publish_existing,
                    claim,
                    observed_at_ms,
                    operation_timeout_seconds=3.0,
                    on_submitted=mark_existing_submitted,
                )
            except asyncio.CancelledError:
                if not submitted:
                    await asyncio.shield(self._release_prework(claim))
                raise
            except ResourceAdmissionTimeout:
                await self._release_prework(claim)
                return None
            if published is None:
                return None
            return "processed"

        submitted = False

        def mark_submitted() -> None:
            nonlocal submitted
            submitted = True

        try:
            mirror_result = await self.finite_operations.run(
                "token_image_fetch",
                mirror_token_image_source,
                _source_row_from_claim(claim),
                app_home=self.app_home,
                http_client=self.http_client,
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
        try:
            published = await self.db.run_business(
                "token_image_publish",
                self._publish_result,
                claim,
                mirror_result,
                observed_at_ms,
                operation_timeout_seconds=3.0,
            )
        except ResourceAdmissionTimeout:
            await self._release_prework(claim)
            return None
        if published is None:
            return None
        status = str(mirror_result.get("status") or "")
        if status == "error":
            return "terminal" if int(claim["attempt_count"]) >= _MAX_ATTEMPTS else "failed"
        return "processed"

    async def _release_prework(self, claim: dict[str, Any]) -> bool:
        try:
            released = await self.db.run_business(
                "token_image_release_prework",
                self._release_prework_sync,
                claim,
                operation_timeout_seconds=0.5,
            )
        except ResourceAdmissionTimeout:
            return False
        return bool(released)

    def _release_prework_sync(self, claim: dict[str, Any]) -> bool:
        with self.db.worker_session(self.name, 0.5) as repos, repos.transaction():
            return bool(repos.token_image_source_dirty_targets.release_prework(claim))

    def _claim_one(
        self,
        now_ms: int,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, int] | None:
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=_STATEMENT_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            rows = repos.token_image_source_dirty_targets.claim_due(
                now_ms=now_ms,
                limit=1,
                lease_owner=self.claim_owner,
                lease_ms=_CLAIM_LEASE_MS,
            )
            if not rows:
                return None
            claim = rows[0]
            terminal_asset = repos.token_image_assets.terminal_by_source_urls([str(claim["source_url"])]).get(
                str(claim["source_url"])
            )
            if terminal_asset is None:
                repos.token_image_assets.upsert_pending_sources(
                    [_source_row_from_claim(claim)],
                    now_ms=now_ms,
                )
            queue_depth = repos.token_image_source_dirty_targets.queue_depth(
                now_ms=now_ms,
            )
        return claim, terminal_asset, queue_depth

    def _publish_existing(
        self,
        claim: dict[str, Any],
        now_ms: int,
    ) -> dict[str, int] | None:
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=_STATEMENT_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            changed = repos.token_image_source_dirty_targets.mark_done(
                [claim],
                now_ms=now_ms,
            )
            if changed == 0:
                return None
            if changed != 1:
                raise RuntimeError("token_image_existing_claim_stale")
            _enqueue_profile_current_for_claims(
                repos=repos,
                claims=[claim],
                now_ms=now_ms,
            )
        return {"queue_rows_changed": changed, "asset_rows_changed": 0}

    def _publish_result(
        self,
        claim: dict[str, Any],
        mirror_result: dict[str, Any],
        now_ms: int,
    ) -> dict[str, int] | None:
        status = str(mirror_result.get("status") or "")
        source_url = str(claim["source_url"])
        error = str(mirror_result.get("error") or "token_image_mirror_failed")
        with (
            self.db.worker_session(
                self.name,
                statement_timeout_seconds=_STATEMENT_TIMEOUT_SECONDS,
            ) as repos,
            repos.transaction(),
        ):
            if status == "ready":
                artifact = dict(mirror_result.get("asset") or {})
                changed = repos.token_image_source_dirty_targets.mark_done(
                    [claim],
                    now_ms=now_ms,
                )
            elif status == "unsupported":
                changed = repos.token_image_source_dirty_targets.mark_done(
                    [claim],
                    now_ms=now_ms,
                )
            else:
                changed = repos.token_image_source_dirty_targets.mark_error(
                    [claim],
                    error=error,
                    retry_ms=_RETRY_MS,
                    max_attempts=_MAX_ATTEMPTS,
                    owner_key=self.name,
                    now_ms=now_ms,
                )
            if changed == 0:
                return None
            if changed != 1:
                raise RuntimeError("token_image_publish_claim_stale")
            if status == "ready":
                repos.token_image_assets.mark_ready(
                    source_url=source_url,
                    media_type=str(artifact["media_type"]),
                    file_extension=str(artifact["file_extension"]),
                    content_sha256=str(artifact["content_sha256"]),
                    byte_size=int(artifact["byte_size"]),
                    storage_path=str(artifact["storage_path"]),
                    now_ms=now_ms,
                )
            elif status == "unsupported":
                repos.token_image_assets.mark_unsupported(
                    source_url=source_url,
                    error=error,
                    now_ms=now_ms,
                )
            else:
                repos.token_image_assets.mark_error(
                    source_url=source_url,
                    error=error,
                    now_ms=now_ms,
                    retry_ms=_RETRY_MS,
                )
            if status in {"ready", "unsupported"}:
                _enqueue_profile_current_for_claims(
                    repos=repos,
                    claims=[claim],
                    now_ms=now_ms,
                )
        return {"queue_rows_changed": changed, "asset_rows_changed": 1}


def _source_row_from_claim(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_url": str(claim.get("source_url") or ""),
        "source_provider": str(claim.get("source_provider") or ""),
        "source_kind": str(claim.get("source_kind") or ""),
        "raw_ref_json": claim.get("raw_ref_json") or {},
    }


def _provider_host(source_url: str) -> str:
    try:
        return source_url.split("/", maxsplit=3)[2].lower()
    except IndexError as exc:
        raise ValueError("token_image_provider_host_required") from exc


def _enqueue_profile_current_for_claims(
    *,
    repos: Any,
    claims: list[dict[str, Any]],
    now_ms: int,
) -> None:
    for claim in claims:
        target_type = str(claim.get("target_type") or "").strip()
        target_id = str(claim.get("target_id") or "").strip()
        if not target_type or not target_id:
            continue
        _required_claim_source_watermark_ms(claim)
        repos.projection_frontiers.mark_dirty(
            PROFILE_FRONTIER,
            key={
                "target_type": target_type,
                "target_id": target_id,
            },
            dirty_at_ms=int(now_ms),
            deadline_at_ms=int(now_ms) + 30_000,
            input_fingerprint=f"token-image:{claim['payload_hash']}:{int(now_ms)}",
            version=PROFILE_PROJECTION_VERSION,
        )


def _required_claim_source_watermark_ms(claim: dict[str, Any]) -> int:
    try:
        value = claim["source_watermark_ms"]
    except KeyError as exc:
        raise RuntimeError("token_image_mirror_profile_dirty_source_watermark_required") from exc
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("token_image_mirror_profile_dirty_source_watermark_required")
    if value <= 0:
        raise RuntimeError("token_image_mirror_profile_dirty_source_watermark_required")
    return int(value)


__all__ = ["TokenImageMirror"]
