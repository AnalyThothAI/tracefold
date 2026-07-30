from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from types import MappingProxyType

from tracefold.macro.calculations import (
    CALCULATION_REGISTRY,
    NATURAL_CHANGE_REGISTRY,
)
from tracefold.macro.domain import MACRO_MODULE_IDS, MacroModuleId
from tracefold.macro.module_payloads import schema_version_for_module
from tracefold.macro.registry import DATASET_REGISTRY, datasets_for_module


def _module_calculation_ids(module_id: MacroModuleId) -> tuple[str, ...]:
    return tuple(
        sorted(
            feature_id for feature_id, calculation in CALCULATION_REGISTRY.items() if calculation.module_id == module_id
        )
    )


def _module_dataset_ids(module_id: MacroModuleId) -> tuple[str, ...]:
    dataset_ids = {spec.dataset_id for spec in datasets_for_module(module_id)}
    for feature_id in _module_calculation_ids(module_id):
        dataset_ids.update(CALCULATION_REGISTRY[feature_id].input_dataset_ids)
    return tuple(sorted(dataset_ids))


MODULE_CALCULATION_DEPENDENCIES = MappingProxyType(
    {module_id: _module_calculation_ids(module_id) for module_id in MACRO_MODULE_IDS}
)
MODULE_DATASET_DEPENDENCIES = MappingProxyType(
    {module_id: _module_dataset_ids(module_id) for module_id in MACRO_MODULE_IDS}
)
DATASET_MODULE_DEPENDENCIES = MappingProxyType(
    {
        dataset_id: tuple(
            module_id for module_id in MACRO_MODULE_IDS if dataset_id in MODULE_DATASET_DEPENDENCIES[module_id]
        )
        for dataset_id in sorted(DATASET_REGISTRY)
    }
)


def module_projection_version(module_id: MacroModuleId) -> str:
    dataset_ids = MODULE_DATASET_DEPENDENCIES[module_id]
    calculation_ids = MODULE_CALCULATION_DEPENDENCIES[module_id]
    payload = {
        "module_id": module_id,
        "module_schema": schema_version_for_module(module_id),
        "datasets": [asdict(DATASET_REGISTRY[dataset_id]) for dataset_id in dataset_ids],
        "calculations": [asdict(CALCULATION_REGISTRY[feature_id]) for feature_id in calculation_ids],
        "natural_changes": [
            asdict(NATURAL_CHANGE_REGISTRY[dataset_id])
            for dataset_id in dataset_ids
            if dataset_id in NATURAL_CHANGE_REGISTRY
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def module_input_fingerprint(
    module_id: MacroModuleId,
    dataset_states: list[dict[str, object]],
) -> str:
    by_dataset = {str(row["dataset_id"]): dict(row) for row in dataset_states}
    payload = {
        "module_id": module_id,
        "projection_version": module_projection_version(module_id),
        "datasets": [
            {
                "dataset_id": dataset_id,
                "material_fingerprint": str(by_dataset.get(dataset_id, {}).get("material_fingerprint") or "missing"),
                "acquisition_status": str(by_dataset.get(dataset_id, {}).get("acquisition_status") or "missing"),
                "source_frontier_ms": int(str(by_dataset.get(dataset_id, {}).get("source_frontier_ms") or 0)),
            }
            for dataset_id in MODULE_DATASET_DEPENDENCIES[module_id]
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


__all__ = [
    "DATASET_MODULE_DEPENDENCIES",
    "MODULE_CALCULATION_DEPENDENCIES",
    "MODULE_DATASET_DEPENDENCIES",
    "module_input_fingerprint",
    "module_projection_version",
]
