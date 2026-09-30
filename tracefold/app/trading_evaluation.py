"""Compose evaluator and policy identities from configured, secret-free contracts."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from tracefold.app.learning_runtime import configured_endpoint_identity
from tracefold.app.llm import configured_lm_endpoint, llm_is_configured
from tracefold.trading.engine.case_view import VIEW_VERSION, CaseView
from tracefold.trading.engine.evaluation import EvaluatorSpec
from tracefold.trading.engine.features import PROFILE_VERSION, WINDOW_VERSION
from tracefold.trading.engine.forecast import PolicyConfig


def evaluator_spec(settings: Any, program_sha: str) -> EvaluatorSpec:
    analysis = settings.trading.analysis
    endpoint = (
        configured_lm_endpoint(settings, model_name=analysis.model_name)
        if analysis.model_name and llm_is_configured(settings)
        else None
    )
    return EvaluatorSpec(
        program_sha=program_sha,
        model_name=analysis.model_name or "",
        model_revision=analysis.model_revision,
        input_contract=VIEW_VERSION,
        output_contract="forecast_v1",
        feature_contract=PROFILE_VERSION,
        window_contract=WINDOW_VERSION,
        max_output_tokens=analysis.max_model_output_tokens,
        temperature=None if endpoint is None else endpoint.temperature,
        thinking=None,
        request_identity=None if endpoint is None else configured_endpoint_identity(endpoint),
    )


def load_calibration(settings: Any, *, evaluator_id: str | None = None) -> dict[str, Any] | None:
    configured = settings.trading.analysis.calibration
    if configured is None:
        return None
    path = Path(configured.path).expanduser()
    if not path.is_absolute():
        path = settings.app_home / path
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != configured.sha256:
        raise ValueError("calibration_sha_mismatch")
    artifact = json.loads(raw)
    if artifact.get("contract") != "calibration_v1" or not artifact.get("training_manifest"):
        raise ValueError("calibration_contract_invalid")
    if evaluator_id is not None and artifact.get("evaluator_id") != evaluator_id:
        raise ValueError("calibration_evaluator_mismatch")
    groups = artifact.get("groups")
    if not isinstance(groups, dict) or "all" not in groups:
        raise ValueError("calibration_groups_invalid")
    for group in groups.values():
        PolicyConfig(
            100,
            200,
            Decimal(0),
            probability_temperature=Decimal(str(group["temperature"])),
            timeout_long_r=Decimal(str(group["timeout_long_r"])),
            timeout_short_r=Decimal(str(group["timeout_short_r"])),
        )
    return dict(artifact)


def policy_manifest(settings: Any) -> dict[str, Any]:
    analysis = settings.trading.analysis
    return {
        "contract": "policy_v2",
        "min_expected_r": str(analysis.min_expected_r),
        "min_direction_gap_r": str(analysis.min_direction_gap_r),
        "calibrator_version": "identity_v1" if analysis.calibration is None else analysis.calibration.sha256,
    }


def policy_config(view: CaseView, settings: Any, calibration: dict[str, Any] | None) -> PolicyConfig:
    parameters: dict[str, Any] = {}
    if calibration is not None:
        if int(calibration["trained_until_ms"]) > view.decided_at_ms:
            raise ValueError("calibration_point_in_time_leakage")
        group = calibration["groups"].get(view.trigger_kind, calibration["groups"]["all"])
        parameters = {
            "probability_temperature": Decimal(str(group["temperature"])),
            "timeout_long_r": Decimal(str(group["timeout_long_r"])),
            "timeout_short_r": Decimal(str(group["timeout_short_r"])),
        }
    return PolicyConfig(
        view.geometry.stop_bps,
        view.geometry.tp_bps,
        view.half_spread_bps,
        min_expected_r=settings.trading.analysis.min_expected_r,
        min_direction_gap_r=settings.trading.analysis.min_direction_gap_r,
        calibrator_version=policy_manifest(settings)["calibrator_version"],
        **parameters,
    )
