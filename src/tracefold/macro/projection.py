from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from tracefold.macro.calculations import (
    CALCULATION_REGISTRY,
    NATURAL_CHANGE_REGISTRY,
    calculate_features,
)
from tracefold.macro.domain import MACRO_MODULE_IDS
from tracefold.macro.history_policy import (
    market_history_limits,
    series_history_limits,
)
from tracefold.macro.module_payloads import (
    build_typed_module_payload,
    schema_version_for_module,
)
from tracefold.macro.registry import datasets_for_module


class MacroProjectionService:
    def __init__(
        self,
        *,
        db: Any,
        settings: Any,
        backfill_worker_enabled: bool,
        worker_name: str = "macro_projection",
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.backfill_worker_enabled = backfill_worker_enabled
        self.worker_name = worker_name
        self.clock_ms = clock_ms or _now_ms

    def rebuild(self, *, now_ms: int | None = None) -> dict[str, Any]:
        now = int(now_ms if now_ms is not None else self.clock_ms())
        all_specs = tuple(spec for module in MACRO_MODULE_IDS for spec in datasets_for_module(module))
        series_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "series")
        market_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "market_observation")
        position_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "market_position")
        settlement_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "market_settlement")
        release_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "release")
        document_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "document")
        projection_version = _projection_version(all_specs)
        with self._session() as repos:
            source_state = _projection_source_state(repos)
            input_fingerprint = _input_fingerprint(
                source_state=source_state,
                projection_version=projection_version,
                backfill_worker_enabled=self.backfill_worker_enabled,
            )
            current_state = repos.macro.projection_state()
            if current_state is not None and str(current_state["input_fingerprint"]) == input_fingerprint:
                return _unchanged_result(source_state)

            series_rows = repos.macro.series_history(history_limits=series_history_limits(series_ids))
            market_rows = repos.macro_market.market_history(history_limits=market_history_limits(market_ids))
            position_rows = repos.macro_market.position_history(dataset_ids=position_ids)
            settlement_rows = repos.macro_market.settlement_history(dataset_ids=settlement_ids)
            release_rows = repos.macro.release_history(dataset_ids=release_ids)
            document_rows = repos.macro.document_history(dataset_ids=document_ids)
            role_rows = repos.macro.fed_official_role_history()
            analysis_rows = repos.macro.document_analysis_history()
            analysis_job_state = repos.macro.document_analysis_job_state()
            target_states = repos.macro.target_states()
            features = calculate_features(series_rows)
            module_payloads = [
                (
                    module_id,
                    build_typed_module_payload(
                        module_id=module_id,
                        now_ms=now,
                        series_rows=series_rows,
                        market_rows=market_rows,
                        position_rows=position_rows,
                        settlement_rows=settlement_rows,
                        release_rows=release_rows,
                        document_rows=document_rows,
                        target_states=target_states,
                        role_rows=role_rows,
                        analysis_rows=analysis_rows,
                        analysis_job_state=analysis_job_state,
                        backfill_worker_enabled=self.backfill_worker_enabled,
                    ),
                )
                for module_id in MACRO_MODULE_IDS
            ]
            source_rows_loaded = {
                "series": len(series_rows),
                "market": len(market_rows),
                "positions": len(position_rows),
                "settlements": len(settlement_rows),
                "releases": len(release_rows),
                "documents": len(document_rows),
                "roles": len(role_rows),
                "analyses": len(analysis_rows),
            }

            feature_writes = 0
            module_writes = 0
            with repos.transaction():
                latest_source_state = _projection_source_state(repos)
                latest_input_fingerprint = _input_fingerprint(
                    source_state=latest_source_state,
                    projection_version=projection_version,
                    backfill_worker_enabled=self.backfill_worker_enabled,
                )
                if latest_input_fingerprint != input_fingerprint:
                    return _stale_snapshot_result(
                        latest_source_state,
                        source_rows_loaded=source_rows_loaded,
                    )
                for feature in features:
                    feature_writes += repos.macro.upsert_feature(
                        feature_id=feature["feature_id"],
                        as_of_date=feature["as_of_date"],
                        formula_version=feature["formula_version"],
                        value_numeric=feature["value_numeric"],
                        unit=feature["unit"],
                        inputs=feature["inputs"],
                        payload_hash=feature["payload_hash"],
                        computed_at_ms=now,
                    )
                for module_id, payload in module_payloads:
                    module_writes += repos.macro.upsert_module_current(
                        module_id=module_id,
                        current_health_state=str(payload["status"]["current_health"]["state"]),
                        history_depth_state=str(payload["status"]["history_depth"]["state"]),
                        fact_cutoff_ms=int(payload["latest_fact_at_ms"]),
                        payload=payload,
                        payload_hash=_payload_hash(payload),
                        updated_at_ms=now,
                    )
                repos.macro.upsert_projection_state(
                    input_fingerprint=input_fingerprint,
                    feature_count=len(features),
                    module_count=len(module_payloads),
                    projected_at_ms=now,
                )
        return {
            "features_computed": len(features),
            "feature_rows_written": feature_writes,
            "modules_computed": len(module_payloads),
            "module_rows_written": module_writes,
            "rows_written": feature_writes + module_writes,
            "projection_status": "rebuilt",
            "source_rows": _source_rows(source_state),
            "candidate_rows": sum(source_rows_loaded.values()),
            "source_rows_loaded": source_rows_loaded,
        }

    def _session(self) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=float(self.settings.statement_timeout_seconds),
        )


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _projection_version(all_specs: tuple[Any, ...]) -> str:
    return _payload_hash(
        {
            "datasets": [asdict(spec) for spec in all_specs],
            "calculations": [asdict(CALCULATION_REGISTRY[key]) for key in sorted(CALCULATION_REGISTRY)],
            "natural_changes": [asdict(NATURAL_CHANGE_REGISTRY[key]) for key in sorted(NATURAL_CHANGE_REGISTRY)],
            "module_schemas": {module_id: schema_version_for_module(module_id) for module_id in MACRO_MODULE_IDS},
            "history_policy": {
                "series": series_history_limits(spec.dataset_id for spec in all_specs if spec.fact_family == "series"),
                "market": market_history_limits(
                    spec.dataset_id for spec in all_specs if spec.fact_family == "market_observation"
                ),
            },
        }
    )


def _projection_source_state(repos: Any) -> dict[str, Any]:
    source_state = repos.macro.projection_source_state()
    return {
        **source_state,
        "fact_tables": sorted(
            [
                *source_state["fact_tables"],
                *repos.macro_market.projection_source_state(),
            ],
            key=lambda row: str(row["source_name"]),
        ),
    }


def _input_fingerprint(
    *,
    source_state: dict[str, Any],
    projection_version: str,
    backfill_worker_enabled: bool,
) -> str:
    return _stable_hash(
        {
            "source_state": source_state,
            "projection_version": projection_version,
            "backfill_worker_enabled": backfill_worker_enabled,
        }
    )


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _source_rows(source_state: dict[str, Any]) -> int:
    return sum(int(row["row_count"]) for row in source_state["fact_tables"])


def _unchanged_result(source_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "features_computed": 0,
        "feature_rows_written": 0,
        "modules_computed": 0,
        "module_rows_written": 0,
        "rows_written": 0,
        "projection_status": "unchanged_input",
        "source_rows": _source_rows(source_state),
        "candidate_rows": 0,
        "source_rows_loaded": {},
    }


def _stale_snapshot_result(
    source_state: dict[str, Any],
    *,
    source_rows_loaded: dict[str, int],
) -> dict[str, Any]:
    return {
        "features_computed": 0,
        "feature_rows_written": 0,
        "modules_computed": 0,
        "module_rows_written": 0,
        "rows_written": 0,
        "projection_status": "stale_snapshot",
        "source_rows": _source_rows(source_state),
        "candidate_rows": sum(source_rows_loaded.values()),
        "source_rows_loaded": source_rows_loaded,
    }


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["MacroProjectionService", "build_typed_module_payload"]
