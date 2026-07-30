from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tracefold.market.profiles.profile_projection import PROFILE_PROJECTION_VERSION
from tracefold.market.profiles.token_image_mirror import mirror_token_image_source
from tracefold.platform.config.settings import TokenImageMirrorWorkerSettings
from tracefold.platform.postgres.projection_frontier import PROFILE_FRONTIER
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult

_CLAIM_LEASE_MS = 120_000
_RETRY_MS = 300_000
_MAX_ATTEMPTS = 3
_STATEMENT_TIMEOUT_SECONDS = 3.0


class TokenImageMirrorWorker(WorkerBase):
    """Mirror one durable image shard with no DB connection over provider I/O."""

    def __init__(
        self,
        *,
        name: str,
        settings: TokenImageMirrorWorkerSettings,
        db: Any,
        telemetry: Any,
        app_home: str | Path,
        http_client: Any | None = None,
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.app_home = Path(app_home)
        self.http_client = http_client

    async def run_once(self, *, now_ms: int | None = None) -> WorkerResult:
        observed_at_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        claimed = await self.require_runtime_resources().run_background_db(
            self._claim_one,
            observed_at_ms,
        )
        if claimed is None:
            return WorkerResult(
                skipped=1,
                notes={
                    "reason": "no_due_token_image_source_targets",
                    "claimed": 0,
                },
            )
        claim, terminal_asset, queue_depth = claimed
        if terminal_asset is not None:
            published = await self.require_runtime_resources().run_background_db(
                self._publish_existing,
                claim,
                observed_at_ms,
            )
            return self._result(
                claim=claim,
                status=str(terminal_asset["status"]),
                queue_depth=queue_depth,
                published=published,
                existing=True,
            )

        async with self.require_provider_governor().acquire(
            host=_provider_host(str(claim["source_url"])),
            lane="image",
        ):
            mirror_result = await self.require_runtime_resources().run_provider_io(
                mirror_token_image_source,
                _source_row_from_claim(claim),
                app_home=self.app_home,
                http_client=self.http_client,
            )
        published = await self.require_runtime_resources().run_background_db(
            self._publish_result,
            claim,
            mirror_result,
            observed_at_ms,
        )
        return self._result(
            claim=claim,
            status=str(mirror_result["status"]),
            queue_depth=queue_depth,
            published=published,
            existing=False,
            error=str(mirror_result.get("error") or "") or None,
        )

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
    ) -> dict[str, int]:
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
    ) -> dict[str, int]:
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
                repos.token_image_assets.mark_ready(
                    source_url=source_url,
                    media_type=str(artifact["media_type"]),
                    file_extension=str(artifact["file_extension"]),
                    content_sha256=str(artifact["content_sha256"]),
                    byte_size=int(artifact["byte_size"]),
                    storage_path=str(artifact["storage_path"]),
                    now_ms=now_ms,
                )
                changed = repos.token_image_source_dirty_targets.mark_done(
                    [claim],
                    now_ms=now_ms,
                )
            elif status == "unsupported":
                repos.token_image_assets.mark_unsupported(
                    source_url=source_url,
                    error=error,
                    now_ms=now_ms,
                )
                changed = repos.token_image_source_dirty_targets.mark_done(
                    [claim],
                    now_ms=now_ms,
                )
            else:
                repos.token_image_assets.mark_error(
                    source_url=source_url,
                    error=error,
                    now_ms=now_ms,
                    retry_ms=_RETRY_MS,
                )
                changed = repos.token_image_source_dirty_targets.mark_error(
                    [claim],
                    error=error,
                    retry_ms=_RETRY_MS,
                    max_attempts=_MAX_ATTEMPTS,
                    worker_name=self.name,
                    now_ms=now_ms,
                )
            if changed != 1:
                raise RuntimeError("token_image_publish_claim_stale")
            if status in {"ready", "unsupported"}:
                _enqueue_profile_current_for_claims(
                    repos=repos,
                    claims=[claim],
                    now_ms=now_ms,
                )
        return {"queue_rows_changed": changed, "asset_rows_changed": 1}

    @staticmethod
    def _result(
        *,
        claim: dict[str, Any],
        status: str,
        queue_depth: int,
        published: dict[str, int],
        existing: bool,
        error: str | None = None,
    ) -> WorkerResult:
        failed = int(status == "error")
        return WorkerResult(
            processed=int(not failed),
            failed=failed,
            notes={
                "claimed": 1,
                "status": status,
                "source_url_hash": str(claim["source_url_hash"]),
                "queue_depth": int(queue_depth),
                "rows_written": sum(published.values()),
                "existing": bool(existing),
                **({"last_error": str(error)[:500]} if error else {}),
            },
        )


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


__all__ = ["TokenImageMirrorWorker"]
