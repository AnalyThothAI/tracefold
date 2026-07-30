from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

from tracefold.macro.calculations import calculate_features
from tracefold.macro.dependencies import (
    MODULE_CALCULATION_DEPENDENCIES,
    MODULE_DATASET_DEPENDENCIES,
    module_input_fingerprint,
    module_projection_version,
)
from tracefold.macro.domain import MACRO_MODULE_IDS, MacroModuleId
from tracefold.macro.history_policy import (
    market_history_limits,
    series_history_limits,
)
from tracefold.macro.module_payloads import build_typed_module_payload
from tracefold.macro.registry import DATASET_REGISTRY, datasets_for_module
from tracefold.platform.postgres.projection_frontier import MACRO_FRONTIER

_INPUT_ROW_CAP = 10_000
_INPUT_BYTE_CAP = 4 * 1024 * 1024
_OUTPUT_BYTE_CAP = 1 * 1024 * 1024
_CLAIM_LEASE_MS = 5_000
_CLAIM_TRANSACTION_TIMEOUT_SECONDS = 0.5
_PUBLISH_TRANSACTION_TIMEOUT_SECONDS = 1.0
_STEADY_STATEMENT_TIMEOUT_SECONDS = 3.0
_MAINTENANCE_STATEMENT_TIMEOUT_SECONDS = 120.0


class MacroShardOversized(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MacroModuleClaim:
    module_id: MacroModuleId
    runtime_id: str
    input_fingerprint: str
    projection_version: str
    deadline_at_ms: int


class MacroProjectionService:
    """Claim/load/publish host for one bounded Macro module shard."""

    def __init__(
        self,
        *,
        db: Any,
        settings: Any,
        backfill_worker_enabled: bool,
        worker_name: str = "steady_projection_coordinator",
    ) -> None:
        if backfill_worker_enabled:
            raise ValueError("macro_steady_projection_backfill_forbidden")
        self.db = db
        self.settings = settings
        self.worker_name = worker_name

    def next_due_module(self, *, now_ms: int) -> dict[str, Any] | None:
        with self._session() as repos:
            return cast(
                dict[str, Any] | None,
                repos.projection_frontiers.next_due(
                    MACRO_FRONTIER,
                    now_ms=now_ms,
                ),
            )

    def prepare_maintenance_frontiers(self, *, now_ms: int) -> int:
        """One-shot hard-cut rebuild preparation; never called by steady runtime."""

        with self._session() as repos, repos.transaction():
            repos.conn.execute("DELETE FROM macro_module_frontiers")
            material_rows = [
                *repos.macro.maintenance_dataset_fact_states(),
                *repos.macro_market.maintenance_dataset_fact_states(),
            ]
            material_by_dataset = {str(row["dataset_id"]): dict(row) for row in material_rows}
            target_by_dataset = {
                str(row["dataset_id"]): dict(row)
                for row in repos.macro.target_states()
                if str(row.get("partition_key") or "") == "latest"
            }
            state_writes = 0
            for dataset_id in sorted(DATASET_REGISTRY):
                material = material_by_dataset.get(dataset_id)
                target = target_by_dataset.get(dataset_id)
                state_writes += int(
                    repos.macro.upsert_dataset_projection_state(
                        dataset_id=dataset_id,
                        material_fingerprint=_stable_hash(
                            {
                                "dataset_id": dataset_id,
                                "row_count": int(material["row_count"]) if material else 0,
                                "max_fact_hash": str(material["max_fact_hash"]) if material else None,
                            }
                        ),
                        acquisition_status=str(
                            target.get("status")
                            if target is not None
                            else "current"
                            if material is not None
                            else "uninitialized"
                        ),
                        source_frontier_ms=(int(material["source_frontier_ms"] or 0) if material is not None else 0),
                        updated_at_ms=now_ms,
                    )
                )
            for module_id in MACRO_MODULE_IDS:
                states = repos.macro.dataset_projection_states(
                    dataset_ids=MODULE_DATASET_DEPENDENCIES[module_id],
                )
                repos.projection_frontiers.mark_dirty(
                    MACRO_FRONTIER,
                    key={"module_id": module_id},
                    dirty_at_ms=now_ms,
                    deadline_at_ms=now_ms,
                    input_fingerprint=module_input_fingerprint(module_id, states),
                    version=module_projection_version(module_id),
                    extra_insert={
                        "source_frontier_ms": max(
                            (int(row["source_frontier_ms"]) for row in states),
                            default=0,
                        )
                    },
                )
        return state_writes

    def claim_module(
        self,
        *,
        module_id: str,
        runtime_id: str,
        now_ms: int,
    ) -> MacroModuleClaim | None:
        parsed_module_id = _module_id(module_id)
        with self._session(
            transaction_timeout_seconds=_CLAIM_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            row = repos.projection_frontiers.claim(
                MACRO_FRONTIER,
                key={"module_id": parsed_module_id},
                runtime_id=runtime_id,
                now_ms=now_ms,
                lease_ms=_CLAIM_LEASE_MS,
            )
        if row is None:
            return None
        return MacroModuleClaim(
            module_id=parsed_module_id,
            runtime_id=str(UUID(str(runtime_id))),
            input_fingerprint=str(row["input_fingerprint"]),
            projection_version=str(row["projection_version"]),
            deadline_at_ms=int(row["deadline_at_ms"]),
        )

    def load_module(self, claim: MacroModuleClaim, *, now_ms: int) -> dict[str, Any]:
        dataset_ids = MODULE_DATASET_DEPENDENCIES[claim.module_id]
        specs = tuple(DATASET_REGISTRY[dataset_id] for dataset_id in dataset_ids)
        series_ids = tuple(spec.dataset_id for spec in specs if spec.fact_family == "series")
        market_ids = tuple(spec.dataset_id for spec in specs if spec.fact_family == "market_observation")
        market_limits = {
            dataset_id: (
                36
                if DATASET_REGISTRY[dataset_id].frequency == "intraday"
                else market_history_limits((dataset_id,))[dataset_id]
            )
            for dataset_id in market_ids
        }
        position_ids = tuple(spec.dataset_id for spec in specs if spec.fact_family == "market_position")
        settlement_ids = tuple(spec.dataset_id for spec in specs if spec.fact_family == "market_settlement")
        release_ids = tuple(spec.dataset_id for spec in specs if spec.fact_family == "release")
        document_ids = tuple(spec.dataset_id for spec in specs if spec.fact_family == "document")
        module_dataset_ids = tuple(spec.dataset_id for spec in datasets_for_module(claim.module_id))
        with self._session() as repos:
            dataset_states = repos.macro.dataset_projection_states(dataset_ids=dataset_ids)
            current_fingerprint = module_input_fingerprint(claim.module_id, dataset_states)
            if current_fingerprint != claim.input_fingerprint:
                return {
                    "status": "stale_snapshot",
                    "module_id": claim.module_id,
                    "current_input_fingerprint": current_fingerprint,
                }
            document_rows = (
                repos.macro.document_projection_history(
                    dataset_ids=document_ids,
                    row_cap=_INPUT_ROW_CAP,
                )
                if claim.module_id == "rates_fed"
                else repos.macro.document_history(
                    dataset_ids=document_ids,
                    row_cap=_INPUT_ROW_CAP,
                )
            )
            role_rows = (
                repos.macro.fed_official_role_projection_history(
                    effective_from=min(
                        (row["effective_date"] for row in document_rows),
                        default=None,
                    ),
                    row_cap=_INPUT_ROW_CAP,
                )
                if claim.module_id == "rates_fed"
                else []
            )
            analysis_rows = (
                repos.macro.document_analysis_projection_history(
                    document_ids=tuple(
                        str(row["document_id"])
                        for row in document_rows
                    ),
                    row_cap=_INPUT_ROW_CAP,
                )
                if claim.module_id == "rates_fed"
                else []
            )
            payload = {
                "status": "loaded",
                "module_id": claim.module_id,
                "now_ms": int(now_ms),
                "input_fingerprint": claim.input_fingerprint,
                "projection_version": claim.projection_version,
                "series_rows": repos.macro.series_projection_history(
                    history_limits=series_history_limits(series_ids),
                    row_cap=_INPUT_ROW_CAP,
                ),
                "market_rows": repos.macro_market.market_projection_history(
                    history_limits=market_limits,
                    row_cap=_INPUT_ROW_CAP,
                ),
                "position_rows": repos.macro_market.position_history(
                    dataset_ids=position_ids,
                    limit_per_contract=1,
                    row_cap=_INPUT_ROW_CAP,
                ),
                "settlement_rows": repos.macro_market.settlement_history(
                    dataset_ids=settlement_ids,
                    row_cap=_INPUT_ROW_CAP,
                ),
                "release_rows": repos.macro.release_history(
                    dataset_ids=release_ids,
                    row_cap=_INPUT_ROW_CAP,
                ),
                "document_rows": document_rows,
                "role_rows": role_rows,
                "analysis_rows": analysis_rows,
                "analysis_job_state": (
                    repos.macro.document_analysis_job_state()
                    if claim.module_id == "rates_fed"
                    else {"total": 0, "open": 0, "failed": 0, "completed": 0}
                ),
                "target_states": repos.macro.target_states(dataset_ids=module_dataset_ids),
            }
        _require_bounded_input(payload)
        return payload

    def publish_module(
        self,
        claim: MacroModuleClaim,
        output: dict[str, Any],
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        if output.get("module_id") != claim.module_id:
            raise ValueError("macro_projection_output_module_mismatch")
        features = [dict(feature) for feature in output["features"]]
        module_payload = dict(output["module_payload"])
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            states = repos.macro.dataset_projection_states(
                dataset_ids=MODULE_DATASET_DEPENDENCIES[claim.module_id],
            )
            current_fingerprint = module_input_fingerprint(claim.module_id, states)
            current_version = module_projection_version(claim.module_id)
            if current_fingerprint != claim.input_fingerprint or current_version != claim.projection_version:
                repos.projection_frontiers.release_stale(
                    MACRO_FRONTIER,
                    key={"module_id": claim.module_id},
                    runtime_id=claim.runtime_id,
                    now_ms=now_ms,
                )
                return {
                    "projection_status": "stale_snapshot",
                    "module_id": claim.module_id,
                    "rows_written": 0,
                }
            feature_writes = 0
            for feature in features:
                feature_writes += repos.macro.upsert_feature(
                    feature_id=feature["feature_id"],
                    as_of_date=feature["as_of_date"],
                    formula_version=feature["formula_version"],
                    value_numeric=feature["value_numeric"],
                    unit=feature["unit"],
                    inputs=feature["inputs"],
                    payload_hash=feature["payload_hash"],
                    computed_at_ms=now_ms,
                )
            module_writes = repos.macro.upsert_module_current(
                module_id=claim.module_id,
                current_health_state=str(module_payload["status"]["current_health"]["state"]),
                history_depth_state=str(module_payload["status"]["history_depth"]["state"]),
                fact_cutoff_ms=int(module_payload["latest_fact_at_ms"]),
                payload=module_payload,
                payload_hash=_payload_hash(module_payload),
                updated_at_ms=now_ms,
            )
            if not repos.projection_frontiers.complete(
                MACRO_FRONTIER,
                key={"module_id": claim.module_id},
                runtime_id=claim.runtime_id,
                input_fingerprint=claim.input_fingerprint,
                version=claim.projection_version,
                now_ms=now_ms,
            ):
                raise RuntimeError("macro_projection_publish_frontier_cas_failed")
        return {
            "projection_status": "published",
            "module_id": claim.module_id,
            "features_computed": len(features),
            "feature_rows_written": feature_writes,
            "module_rows_written": module_writes,
            "rows_written": feature_writes + module_writes,
            "candidate_rows": int(output["candidate_rows"]),
        }

    def release_stale(self, claim: MacroModuleClaim, *, now_ms: int) -> bool:
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            return bool(
                repos.projection_frontiers.release_stale(
                    MACRO_FRONTIER,
                    key={"module_id": claim.module_id},
                    runtime_id=claim.runtime_id,
                    now_ms=now_ms,
                )
            )

    def fail_deterministic(
        self,
        claim: MacroModuleClaim,
        *,
        error_code: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            return cast(
                dict[str, Any] | None,
                repos.projection_frontiers.fail_deterministic(
                    MACRO_FRONTIER,
                    key={"module_id": claim.module_id},
                    runtime_id=claim.runtime_id,
                    error_code=error_code,
                    now_ms=now_ms,
                ),
            )

    def fail_transient(
        self,
        claim: MacroModuleClaim,
        *,
        error_code: str,
        now_ms: int,
    ) -> bool:
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            return bool(
                repos.projection_frontiers.fail_transient(
                    MACRO_FRONTIER,
                    key={"module_id": claim.module_id},
                    runtime_id=claim.runtime_id,
                    error_code=error_code,
                    now_ms=now_ms,
                )
            )

    def _session(
        self,
        *,
        transaction_timeout_seconds: float | None = None,
    ) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=(
                _MAINTENANCE_STATEMENT_TIMEOUT_SECONDS
                if self.worker_name == "macro_maintenance_rebuild"
                else _STEADY_STATEMENT_TIMEOUT_SECONDS
            ),
            transaction_timeout_seconds=transaction_timeout_seconds,
        )


def compute_macro_module_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Pure spawn-safe compute function; no I/O or mutable process state."""

    module_id = _module_id(payload["module_id"])
    series_rows = [dict(row) for row in payload["series_rows"]]
    calculation_ids = set(MODULE_CALCULATION_DEPENDENCIES[module_id])
    features = [feature for feature in calculate_features(series_rows) if str(feature["feature_id"]) in calculation_ids]
    module_payload = build_typed_module_payload(
        module_id=module_id,
        now_ms=int(payload["now_ms"]),
        series_rows=series_rows,
        market_rows=[dict(row) for row in payload["market_rows"]],
        position_rows=[dict(row) for row in payload["position_rows"]],
        settlement_rows=[dict(row) for row in payload["settlement_rows"]],
        release_rows=[dict(row) for row in payload["release_rows"]],
        document_rows=[dict(row) for row in payload["document_rows"]],
        target_states=[dict(row) for row in payload["target_states"]],
        role_rows=[dict(row) for row in payload["role_rows"]],
        analysis_rows=[dict(row) for row in payload["analysis_rows"]],
        analysis_job_state=dict(payload["analysis_job_state"]),
        backfill_worker_enabled=False,
    )
    output = {
        "module_id": module_id,
        "features": features,
        "module_payload": module_payload,
        "candidate_rows": _input_row_count(payload),
    }
    _require_bounded_output(output)
    return output


def rebuild_all_macro_modules_for_maintenance(
    *,
    db: Any,
    settings: Any,
    now_ms: int,
) -> dict[str, Any]:
    """Explicit cutover/backfill operation; absent from steady worker topology."""

    service = MacroProjectionService(
        db=db,
        settings=settings,
        backfill_worker_enabled=False,
        worker_name="macro_maintenance_rebuild",
    )
    state_writes = service.prepare_maintenance_frontiers(now_ms=now_ms)
    runtime_id = str(uuid4())
    results: list[dict[str, Any]] = []
    for module_id in MACRO_MODULE_IDS:
        claim = service.claim_module(
            module_id=module_id,
            runtime_id=runtime_id,
            now_ms=now_ms,
        )
        if claim is None:
            raise RuntimeError(f"macro_maintenance_claim_missing:{module_id}")
        loaded = service.load_module(claim, now_ms=now_ms)
        if loaded["status"] != "loaded":
            raise RuntimeError(f"macro_maintenance_stale_load:{module_id}")
        output = compute_macro_module_projection(loaded)
        result = service.publish_module(claim, output, now_ms=now_ms)
        if result["projection_status"] != "published":
            raise RuntimeError(f"macro_maintenance_publish_failed:{module_id}")
        results.append(result)
    return {
        "projection_status": "rebuilt",
        "dataset_state_writes": state_writes,
        "modules_computed": len(results),
        "features_computed": sum(int(row["features_computed"]) for row in results),
        "feature_rows_written": sum(int(row["feature_rows_written"]) for row in results),
        "module_rows_written": sum(int(row["module_rows_written"]) for row in results),
        "rows_written": sum(int(row["rows_written"]) for row in results),
        "candidate_rows": sum(int(row["candidate_rows"]) for row in results),
        "modules": results,
    }


def _require_bounded_input(payload: dict[str, Any]) -> None:
    row_count = _input_row_count(payload)
    if row_count > _INPUT_ROW_CAP:
        raise MacroShardOversized(
            f"macro_shard_oversized:rows={row_count}:cap={_INPUT_ROW_CAP}:module={payload['module_id']}"
        )
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    if len(encoded) > _INPUT_BYTE_CAP:
        raise MacroShardOversized(
            f"macro_shard_oversized:bytes={len(encoded)}:cap={_INPUT_BYTE_CAP}:module={payload['module_id']}"
        )


def _input_row_count(payload: dict[str, Any]) -> int:
    return sum(
        len(payload.get(field) or ())
        for field in (
            "series_rows",
            "market_rows",
            "position_rows",
            "settlement_rows",
            "release_rows",
            "document_rows",
            "role_rows",
            "analysis_rows",
            "target_states",
        )
    )


def _require_bounded_output(output: dict[str, Any]) -> None:
    encoded = json.dumps(
        output,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    if len(encoded) > _OUTPUT_BYTE_CAP:
        raise MacroShardOversized(
            f"macro_shard_oversized:output_bytes={len(encoded)}:cap={_OUTPUT_BYTE_CAP}:module={output['module_id']}"
        )


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _module_id(value: object) -> MacroModuleId:
    normalized = str(value)
    if normalized not in MACRO_MODULE_IDS:
        raise ValueError(f"macro_module_unknown:{normalized}")
    return cast(MacroModuleId, normalized)


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = [
    "MacroModuleClaim",
    "MacroProjectionService",
    "MacroShardOversized",
    "compute_macro_module_projection",
    "rebuild_all_macro_modules_for_maintenance",
]
