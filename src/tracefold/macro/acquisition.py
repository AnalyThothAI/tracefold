from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from datetime import time as datetime_time
from typing import Any

from tracefold.macro.domain import (
    DocumentFact,
    MacroSourceClientProtocol,
    MacroSourceUnavailable,
    ReleaseFact,
    SeriesFact,
)
from tracefold.macro.fed_roles import derive_fomc_role_facts
from tracefold.macro.registry import datasets_for_clock, require_dataset
from tracefold.market import MarketObservationFact, MarketPositionFact, MarketSettlementFact


class MacroAcquisitionService:
    """Coordinate target claims and atomic fact/cursor/receipt commits."""

    def __init__(
        self,
        *,
        db: Any,
        worker_name: str,
        clock_kind: str,
        settings: Any,
        source_client: MacroSourceClientProtocol,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.db = db
        self.worker_name = worker_name
        self.clock_kind = clock_kind
        self.settings = settings
        self.source_client = source_client
        self.clock_ms = clock_ms or _now_ms

    def ensure_targets(self, *, now_ms: int | None = None) -> int:
        now = int(now_ms if now_ms is not None else self.clock_ms())
        written = 0
        with self._session() as repos, repos.transaction():
            for spec in datasets_for_clock(self.clock_kind):
                written += repos.macro.ensure_target(
                    spec,
                    now_ms=now,
                    max_attempts=int(self.settings.max_attempts),
                )
                if spec.instrument_id is not None:
                    written += repos.macro_market.ensure_instrument(spec, now_ms=now)
        return written

    def run_once(self, *, now_ms: int | None = None) -> dict[str, Any] | None:
        started_at_ms = int(now_ms if now_ms is not None else self.clock_ms())
        with self._session() as repos, repos.transaction():
            target = repos.macro.claim_target(
                clock_kind=self.clock_kind,
                lease_owner=self.worker_name,
                lease_ms=int(self.settings.lease_ms),
                now_ms=started_at_ms,
            )
        if target is None:
            return None

        spec = require_dataset(str(target["dataset_id"]))
        try:
            batch = self.source_client.fetch(
                spec,
                partition_key=str(target["partition_key"]),
                cursor=dict(target["cursor_json"] or {}),
                now_ms=started_at_ms,
            )
        except Exception as exc:
            completed_at_ms = int(self.clock_ms())
            unavailable = isinstance(exc, MacroSourceUnavailable)
            receipt_id = _receipt_id(target, started_at_ms, completed_at_ms, "failed")
            error_code = str(exc) if unavailable else type(exc).__name__
            with self._session() as repos, repos.transaction():
                repos.macro.record_receipt(
                    target=target,
                    receipt_id=receipt_id,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    status="failed",
                    http_status=None,
                    rows_seen=0,
                    rows_inserted=0,
                    response_hash=None,
                    error_code=error_code,
                    error_message=_safe_error(exc),
                    diagnostics={"adapter_id": spec.adapter_id},
                )
                completed = repos.macro.fail_target(
                    target=target,
                    lease_owner=self.worker_name,
                    receipt_id=receipt_id,
                    error_code=error_code,
                    next_due_at_ms=completed_at_ms + int(self.settings.retry_ms),
                    completed_at_ms=completed_at_ms,
                    unavailable=unavailable,
                )
                if not completed:
                    raise RuntimeError("macro_acquisition_stale_claim") from exc
            return {
                "dataset_id": spec.dataset_id,
                "status": "unavailable" if unavailable else "failed",
                "rows_seen": 0,
                "rows_inserted": 0,
                "error_code": error_code,
            }

        completed_at_ms = int(self.clock_ms())
        receipt_id = _receipt_id(target, started_at_ms, completed_at_ms, batch.response_hash)
        inserted = 0
        with self._session() as repos, repos.transaction():
            for fact in batch.facts:
                if isinstance(fact, SeriesFact):
                    inserted += repos.macro.insert_series_fact(fact)
                elif isinstance(fact, ReleaseFact):
                    inserted += repos.macro.insert_release_fact(fact)
                elif isinstance(fact, DocumentFact):
                    inserted += repos.macro.insert_document(fact)
                    for role_fact in derive_fomc_role_facts(fact):
                        inserted += repos.macro.insert_fed_official_role_fact(role_fact)
                elif isinstance(fact, MarketObservationFact):
                    inserted += repos.macro_market.insert_observation(fact)
                elif isinstance(fact, MarketPositionFact):
                    inserted += repos.macro_market.insert_position(fact)
                elif isinstance(fact, MarketSettlementFact):
                    inserted += repos.macro_market.insert_settlement(fact)
                else:
                    raise TypeError(f"unknown_macro_fact:{type(fact).__name__}")
            receipt_status = "ok" if batch.facts else "empty"
            repos.macro.record_receipt(
                target=target,
                receipt_id=receipt_id,
                started_at_ms=started_at_ms,
                completed_at_ms=completed_at_ms,
                status=receipt_status,
                http_status=batch.http_status,
                rows_seen=len(batch.facts),
                rows_inserted=inserted,
                response_hash=batch.response_hash,
                error_code=None,
                error_message=None,
                diagnostics=batch.diagnostics,
            )
            backfill_complete = self.clock_kind != "backfill" or _backfill_complete(
                batch.cursor,
                has_facts=bool(batch.facts),
            )
            target_status = (
                "current"
                if backfill_complete and (bool(batch.facts) or self.clock_kind == "backfill")
                else "delayed"
                if not batch.facts and self.clock_kind != "backfill"
                else "backfilling"
            )
            completed = repos.macro.complete_target(
                target_key=str(target["target_key"]),
                lease_owner=self.worker_name,
                receipt_id=receipt_id,
                cursor=batch.cursor,
                next_due_at_ms=(
                    completed_at_ms + (spec.refresh_seconds * 1_000 if batch.facts else int(self.settings.retry_ms))
                    if self.clock_kind != "backfill"
                    else (253_402_300_799_000 if backfill_complete else completed_at_ms)
                ),
                completed_at_ms=completed_at_ms,
                status=target_status,
            )
            if not completed:
                raise RuntimeError("macro_acquisition_stale_claim")
        return {
            "dataset_id": spec.dataset_id,
            "status": target_status,
            "rows_seen": len(batch.facts),
            "rows_inserted": inserted,
        }

    def _session(self) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=float(self.settings.statement_timeout_seconds),
        )


def _receipt_id(
    target: dict[str, Any],
    started_at_ms: int,
    completed_at_ms: int,
    result_key: str,
) -> str:
    identity = f"{target['target_key']}|{started_at_ms}|{completed_at_ms}|{target['attempt_count']}|{result_key}"
    return "macrorcpt_" + hashlib.sha256(identity.encode()).hexdigest()


def _safe_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text[:500] or type(exc).__name__


def _backfill_complete(cursor: dict[str, Any], *, has_facts: bool) -> bool:
    if "backfill_complete" in cursor:
        return bool(cursor["backfill_complete"])
    if not has_facts:
        return True
    end_date = str(cursor.get("end_date") or "")
    if not end_date:
        return True
    reference_date = str(cursor.get("reference_date") or "")
    if reference_date:
        return reference_date >= end_date
    observed_at_ms = cursor.get("observed_at_ms")
    if observed_at_ms is None:
        return True
    end_at_ms = int(
        datetime.combine(
            date.fromisoformat(end_date),
            datetime_time.max,
            tzinfo=UTC,
        ).timestamp()
        * 1_000
    )
    return int(observed_at_ms) >= end_at_ms


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["MacroAcquisitionService"]
