from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

from tracefold.macro.calculations import calculate_features
from tracefold.macro.domain import MACRO_MODULE_IDS
from tracefold.macro.module_payloads import build_typed_module_payload
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
        with self._session() as repos, repos.transaction():
            series_rows = repos.macro.series_history(dataset_ids=series_ids, limit_per_dataset=10_000)
            market_rows = repos.macro_market.market_history(dataset_ids=market_ids, limit_per_dataset=5_000)
            position_rows = repos.macro_market.position_history(dataset_ids=position_ids)
            settlement_rows = repos.macro_market.settlement_history(dataset_ids=settlement_ids)
            release_rows = repos.macro.release_history(dataset_ids=release_ids)
            document_rows = repos.macro.document_history(dataset_ids=document_ids)
            role_rows = repos.macro.fed_official_role_history()
            analysis_rows = repos.macro.document_analysis_history()
            analysis_job_state = repos.macro.document_analysis_job_state()
            target_states = repos.macro.target_states()
            features = calculate_features(series_rows)
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
                    computed_at_ms=now,
                )
            module_writes = 0
            for module_id in MACRO_MODULE_IDS:
                payload = build_typed_module_payload(
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
                )
                module_writes += repos.macro.upsert_module_current(
                    module_id=module_id,
                    current_health_state=str(payload["status"]["current_health"]["state"]),
                    history_depth_state=str(payload["status"]["history_depth"]["state"]),
                    fact_cutoff_ms=int(payload["latest_fact_at_ms"]),
                    payload=payload,
                    payload_hash=_payload_hash(payload),
                    updated_at_ms=now,
                )
        return {
            "features_computed": len(features),
            "feature_rows_written": feature_writes,
            "modules_computed": len(MACRO_MODULE_IDS),
            "module_rows_written": module_writes,
        }

    def _session(self) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=float(self.settings.statement_timeout_seconds),
        )


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["MacroProjectionService", "build_typed_module_payload"]
