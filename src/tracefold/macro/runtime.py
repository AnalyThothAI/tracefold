from __future__ import annotations

import asyncio
import time
from typing import Any

from tracefold.macro.acquisition import MacroAcquisitionService
from tracefold.macro.domain import FetchBatch, MacroSourceError
from tracefold.platform.resource import ResourceAdmissionTimeout

_FETCH_TIMEOUT_SECONDS = 30.0
_CLAIM_TIMEOUT_SECONDS = 0.5
_PUBLISH_TIMEOUT_SECONDS = 5.0
_MAX_REQUESTS = 4
_MAX_DECODED_BYTES = 25_000_000
_MAX_FACTS = 5_000
_MAX_BATCH_BYTES = 16_777_216


class MacroAcquisition:
    """One native Macro clock with one-target bounded turns."""

    def __init__(
        self,
        *,
        db: Any,
        finite_operations: Any,
        service: MacroAcquisitionService,
    ) -> None:
        self.db = db
        self.finite_operations = finite_operations
        self.service = service

    async def reconcile(self) -> int:
        return int(
            await self.db.run_business(
                "macro_target_reconcile",
                self.service.ensure_targets,
                operation_timeout_seconds=5.0,
            )
        )

    async def turn(self) -> bool | None:
        claim = await self.db.run_business(
            "macro_target_claim",
            self.service.claim_next,
            operation_timeout_seconds=_CLAIM_TIMEOUT_SECONDS,
        )
        if claim is None:
            return False

        submitted = False

        def mark_submitted() -> None:
            nonlocal submitted
            submitted = True

        try:
            batch = await self.finite_operations.run(
                "macro_source_fetch",
                self.service.fetch_claim,
                claim,
                timeout_seconds=_FETCH_TIMEOUT_SECONDS,
                on_submitted=mark_submitted,
            )
        except asyncio.CancelledError:
            if not submitted:
                await asyncio.shield(self._release(claim))
            raise
        except ResourceAdmissionTimeout:
            await self._release(claim)
            return None
        except MacroSourceError as exc:
            published = await self.db.run_business(
                "macro_publish_failure",
                self.service.publish_failure,
                claim,
                exc,
                operation_timeout_seconds=_PUBLISH_TIMEOUT_SECONDS,
            )
            return True if published is not None else None

        try:
            _validate_batch(batch)
        except MacroSourceError as exc:
            published = await self.db.run_business(
                "macro_publish_envelope_failure",
                self.service.publish_failure,
                claim,
                exc,
                operation_timeout_seconds=_PUBLISH_TIMEOUT_SECONDS,
            )
            return True if published is not None else None
        published = await self.db.run_business(
            "macro_publish_success",
            self.service.publish_success,
            claim,
            batch,
            operation_timeout_seconds=_PUBLISH_TIMEOUT_SECONDS,
        )
        return True if published is not None else None

    async def _release(self, claim: Any) -> bool:
        return bool(
            await self.db.run_business(
                "macro_release_prework",
                self.service.release_claim,
                claim,
                operation_timeout_seconds=_CLAIM_TIMEOUT_SECONDS,
            )
        )


def _validate_batch(batch: FetchBatch) -> None:
    if len(batch.facts) > _MAX_FACTS:
        raise MacroSourceError("macro_fetch_fact_limit_exceeded")
    diagnostics = batch.diagnostics
    request_count = int(diagnostics.get("request_count", 1))
    decoded_bytes = int(diagnostics.get("decoded_bytes", 0))
    if request_count > _MAX_REQUESTS:
        raise MacroSourceError("macro_fetch_request_limit_exceeded")
    if decoded_bytes > _MAX_DECODED_BYTES:
        raise MacroSourceError("macro_fetch_byte_limit_exceeded")
    result_bytes = int(diagnostics.get("result_bytes", 0))
    if result_bytes > _MAX_BATCH_BYTES:
        raise MacroSourceError("macro_fetch_result_limit_exceeded")


def now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["MacroAcquisition"]
