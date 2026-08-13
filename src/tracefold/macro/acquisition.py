from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as datetime_time
from typing import Any

from tracefold.macro.dependencies import (
    DATASET_MODULE_DEPENDENCIES,
    MODULE_DATASET_DEPENDENCIES,
    module_input_fingerprint,
    module_projection_version,
)
from tracefold.macro.domain import (
    DatasetSpec,
    DocumentFact,
    FetchBatch,
    MacroSourceClientProtocol,
    MacroSourceError,
    MacroSourceUnavailable,
    ReleaseFact,
    SeriesFact,
)
from tracefold.macro.fed_roles import derive_fomc_role_facts
from tracefold.macro.registry import datasets_for_clock, require_dataset
from tracefold.market import MarketObservationFact, MarketPositionFact, MarketSettlementFact
from tracefold.platform.postgres.projection_frontier import MACRO_FRONTIER

_MACRO_DEADLINE_MS = 60_000
_ACQUISITION_POLICY = {
    "intraday_market": (300.0, 32, 300_000),
    "daily_settlement": (21_600.0, 32, 900_000),
    "scheduled_release": (3_600.0, 4, 900_000),
    "official_state": (10_800.0, 4, 900_000),
    "official_document": (3_600.0, 2, 900_000),
    "backfill": (5.0, 1, 900_000),
}
_LEASE_MS = 45_000
_MAX_ATTEMPTS = 5
_CLAIM_TIMEOUT_SECONDS = 0.5
_PUBLISH_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class MacroAcquisitionClaim:
    target: dict[str, Any]
    spec: DatasetSpec
    started_at_ms: int


class MacroAcquisitionService:
    """Coordinate target claims and atomic fact/cursor commits."""

    def __init__(
        self,
        *,
        db: Any,
        worker_name: str,
        clock_kind: str,
        source_client: MacroSourceClientProtocol,
        enabled_adapter_ids: frozenset[str],
        lease_owner: str | None = None,
        clock_ms: Callable[[], int] | None = None,
        target_keys: tuple[str, ...] = (),
        document_analysis_model_name: str | None = None,
        document_analysis_prompt_version: str | None = None,
        document_analysis_max_attempts: int = 3,
        document_analysis_fomc_lookback_days: int = 550,
        document_analysis_speech_lookback_days: int = 120,
    ) -> None:
        self.db = db
        self.worker_name = worker_name
        self.lease_owner = str(lease_owner or worker_name)
        self.clock_kind = clock_kind
        if clock_kind not in _ACQUISITION_POLICY:
            raise ValueError(f"macro_acquisition_clock_invalid:{clock_kind}")
        self.retry_ms = _ACQUISITION_POLICY[clock_kind][2]
        self.source_client = source_client
        self.enabled_adapter_ids = frozenset(enabled_adapter_ids)
        self.clock_ms = clock_ms or _now_ms
        self.target_keys = tuple(sorted(set(target_keys)))
        self.document_analysis_model_name = str(document_analysis_model_name or "").strip() or None
        self.document_analysis_prompt_version = str(document_analysis_prompt_version or "").strip() or None
        self.document_analysis_max_attempts = int(document_analysis_max_attempts)
        self.document_analysis_fomc_lookback_days = int(document_analysis_fomc_lookback_days)
        self.document_analysis_speech_lookback_days = int(document_analysis_speech_lookback_days)
        if (self.document_analysis_model_name is None) != (self.document_analysis_prompt_version is None):
            raise ValueError("macro_document_analysis_job_identity_incomplete")

    def ensure_targets(self, *, now_ms: int | None = None) -> int:
        now = int(now_ms if now_ms is not None else self.clock_ms())
        written = 0
        with self._session() as repos, repos.transaction():
            for spec in datasets_for_clock(self.clock_kind):
                written += repos.macro.ensure_target(
                    spec,
                    now_ms=now,
                    max_attempts=_MAX_ATTEMPTS,
                    reactivate_unavailable=(spec.adapter_id in self.enabled_adapter_ids),
                )
                if spec.instrument_id is not None:
                    written += repos.macro_market.ensure_instrument(spec, now_ms=now)
                if repos.macro.ensure_dataset_projection_state(
                    dataset_id=spec.dataset_id,
                    updated_at_ms=now,
                ):
                    _dirty_dependent_modules(
                        repos,
                        dataset_id=spec.dataset_id,
                        dirty_at_ms=now,
                    )
        return written

    def run_once(self, *, now_ms: int | None = None) -> dict[str, Any] | None:
        claim = self.claim_next(now_ms=now_ms)
        if claim is None:
            return None
        try:
            batch = self.fetch_claim(claim)
        except Exception as exc:
            return self.publish_failure(claim, exc)
        return self.publish_success(claim, batch)

    def claim_next(
        self,
        *,
        now_ms: int | None = None,
    ) -> MacroAcquisitionClaim | None:
        started_at_ms = int(now_ms if now_ms is not None else self.clock_ms())
        with self._session(timeout_seconds=_CLAIM_TIMEOUT_SECONDS) as repos, repos.transaction():
            target = repos.macro.claim_target(
                clock_kind=self.clock_kind,
                lease_owner=self.lease_owner,
                lease_ms=_LEASE_MS,
                now_ms=started_at_ms,
                target_keys=self.target_keys,
            )
        if target is None:
            return None
        return MacroAcquisitionClaim(
            target=dict(target),
            spec=require_dataset(str(target["dataset_id"])),
            started_at_ms=started_at_ms,
        )

    def fetch_claim(self, claim: MacroAcquisitionClaim) -> FetchBatch:
        """Provider-only phase. The claim owns no database connection."""

        return self.source_client.fetch(
            claim.spec,
            partition_key=str(claim.target["partition_key"]),
            cursor=dict(claim.target["cursor_json"] or {}),
            now_ms=claim.started_at_ms,
        )

    def release_claim(self, claim: MacroAcquisitionClaim) -> bool:
        with self._session(timeout_seconds=_CLAIM_TIMEOUT_SECONDS) as repos, repos.transaction():
            return bool(
                repos.macro.release_target_claim(
                    target_key=str(claim.target["target_key"]),
                    lease_owner=self.lease_owner,
                    previous_status=str(claim.target["previous_status"]),
                    claimed_attempt_count=int(claim.target["attempt_count"]),
                )
            )

    def publish_failure(
        self,
        claim: MacroAcquisitionClaim,
        error: Exception,
    ) -> dict[str, Any] | None:
        completed_at_ms = int(self.clock_ms())
        unavailable = isinstance(error, MacroSourceUnavailable)
        error_code = str(error) if isinstance(error, MacroSourceError) else type(error).__name__
        with self._session() as repos, repos.transaction():
            target_status = repos.macro.fail_target(
                target=claim.target,
                lease_owner=self.lease_owner,
                error_code=error_code,
                next_due_at_ms=completed_at_ms + self.retry_ms,
                completed_at_ms=completed_at_ms,
                unavailable=unavailable,
            )
            if target_status is None:
                return None
            _mark_dataset_state_and_modules(
                repos,
                dataset_id=claim.spec.dataset_id,
                acquisition_status=("unavailable" if unavailable else "failed"),
                material_fingerprint=_stable_hash(
                    {
                        "dataset_id": claim.spec.dataset_id,
                        "status": ("unavailable" if unavailable else "failed"),
                        "error_code": error_code,
                        "target_status": target_status,
                    }
                ),
                source_frontier_ms=_current_dataset_frontier(
                    repos,
                    dataset_id=claim.spec.dataset_id,
                ),
                updated_at_ms=completed_at_ms,
                material_changed=False,
            )
        return {
            "dataset_id": claim.spec.dataset_id,
            "status": "unavailable" if unavailable else "failed",
            "rows_seen": 0,
            "rows_inserted": 0,
            "error_code": error_code,
        }

    def publish_success(
        self,
        claim: MacroAcquisitionClaim,
        batch: FetchBatch,
    ) -> dict[str, Any] | None:
        completed_at_ms = int(self.clock_ms())
        target = claim.target
        spec = claim.spec
        completed_cursor = dict(batch.cursor)
        if self.clock_kind == "backfill":
            target_cursor = target.get("cursor_json")
            if isinstance(target_cursor, dict):
                for key in ("history_class",):
                    if key in target_cursor:
                        completed_cursor[key] = target_cursor[key]
        unfinished = batch.completion == "continuation"
        backfill_complete = not unfinished and (
            self.clock_kind != "backfill"
            or _backfill_complete(
                completed_cursor,
                has_facts=bool(batch.facts),
            )
        )
        target_status = (
            "backfilling"
            if unfinished
            else "current"
            if backfill_complete and (bool(batch.facts) or self.clock_kind == "backfill")
            else "delayed"
            if not batch.facts and self.clock_kind != "backfill"
            else "backfilling"
        )
        next_due_at_ms = (
            completed_at_ms
            if unfinished
            else completed_at_ms + (spec.refresh_seconds * 1_000 if batch.facts else self.retry_ms)
            if self.clock_kind != "backfill"
            else (253_402_300_799_000 if backfill_complete else completed_at_ms)
        )
        inserted = 0
        inserted_document_ids: list[str] = []
        with self._session() as repos, repos.transaction():
            completed = repos.macro.complete_target(
                target_key=str(target["target_key"]),
                lease_owner=self.lease_owner,
                cursor=completed_cursor,
                next_due_at_ms=next_due_at_ms,
                completed_at_ms=completed_at_ms,
                status=target_status,
            )
            if not completed:
                return None
            for fact in batch.facts:
                if isinstance(fact, SeriesFact):
                    inserted += repos.macro.insert_series_fact(fact)
                elif isinstance(fact, ReleaseFact):
                    inserted += repos.macro.insert_release_fact(fact)
                elif isinstance(fact, DocumentFact):
                    document_inserted = repos.macro.insert_document(fact)
                    inserted += document_inserted
                    if document_inserted:
                        inserted_document_ids.append(fact.document_id)
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
            if inserted_document_ids and self.document_analysis_model_name is not None:
                inserted += repos.macro.ensure_document_analysis_jobs(
                    model_name=self.document_analysis_model_name,
                    prompt_version=str(self.document_analysis_prompt_version),
                    max_attempts=self.document_analysis_max_attempts,
                    now_ms=completed_at_ms,
                    fomc_lookback_days=self.document_analysis_fomc_lookback_days,
                    speech_lookback_days=self.document_analysis_speech_lookback_days,
                    document_ids=tuple(sorted(set(inserted_document_ids))),
                )
            _mark_dataset_state_and_modules(
                repos,
                dataset_id=spec.dataset_id,
                acquisition_status=target_status,
                material_fingerprint=_stable_hash(
                    {
                        "dataset_id": spec.dataset_id,
                        "response_hash": batch.response_hash,
                        "status": target_status,
                        "cursor": completed_cursor,
                    }
                ),
                source_frontier_ms=(
                    max((int(fact.received_at_ms) for fact in batch.facts), default=0)
                    if inserted
                    else _current_dataset_frontier(
                        repos,
                        dataset_id=spec.dataset_id,
                    )
                ),
                updated_at_ms=completed_at_ms,
                material_changed=bool(inserted),
            )
        return {
            "dataset_id": spec.dataset_id,
            "status": target_status,
            "rows_seen": len(batch.facts),
            "rows_inserted": inserted,
        }

    def _session(self, *, timeout_seconds: float = _PUBLISH_TIMEOUT_SECONDS) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=timeout_seconds,
            transaction_timeout_seconds=timeout_seconds,
        )


def acquisition_loop_policy(clock_kind: str) -> tuple[float, int]:
    try:
        interval_seconds, batch_size, retry_ms = _ACQUISITION_POLICY[str(clock_kind)]
    except KeyError as exc:
        raise ValueError(f"macro_acquisition_clock_invalid:{clock_kind}") from exc
    return min(interval_seconds, retry_ms / 1_000), batch_size


def _mark_dataset_state_and_modules(
    repos: Any,
    *,
    dataset_id: str,
    acquisition_status: str,
    material_fingerprint: str,
    source_frontier_ms: int,
    updated_at_ms: int,
    material_changed: bool = True,
) -> None:
    changed = repos.macro.upsert_dataset_projection_state(
        dataset_id=dataset_id,
        material_fingerprint=material_fingerprint,
        acquisition_status=acquisition_status,
        source_frontier_ms=source_frontier_ms,
        updated_at_ms=updated_at_ms,
        material_changed=material_changed,
    )
    if changed:
        _dirty_dependent_modules(
            repos,
            dataset_id=dataset_id,
            dirty_at_ms=updated_at_ms,
        )


def _dirty_dependent_modules(
    repos: Any,
    *,
    dataset_id: str,
    dirty_at_ms: int,
) -> None:
    for module_id in DATASET_MODULE_DEPENDENCIES.get(dataset_id, ()):
        states = repos.macro.dataset_projection_states(
            dataset_ids=MODULE_DATASET_DEPENDENCIES[module_id],
        )
        repos.projection_frontiers.mark_dirty(
            MACRO_FRONTIER,
            key={"module_id": module_id},
            dirty_at_ms=dirty_at_ms,
            deadline_at_ms=dirty_at_ms + _MACRO_DEADLINE_MS,
            input_fingerprint=module_input_fingerprint(module_id, states),
            version=module_projection_version(module_id),
            extra_insert={"source_frontier_ms": _max_source_frontier(states)},
        )


def _current_dataset_frontier(repos: Any, *, dataset_id: str) -> int:
    states = repos.macro.dataset_projection_states(dataset_ids=(dataset_id,))
    return max((int(row["source_frontier_ms"]) for row in states), default=0)


def _max_source_frontier(states: list[dict[str, Any]]) -> int:
    return max((int(row["source_frontier_ms"]) for row in states), default=0)


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


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


__all__ = [
    "MacroAcquisitionClaim",
    "MacroAcquisitionService",
    "acquisition_loop_policy",
]
