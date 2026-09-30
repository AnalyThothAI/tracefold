"""Content identities for prediction contracts and explicit evaluation runs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal


def content_id(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluatorSpec:
    program_sha: str
    model_name: str
    model_revision: str | None
    input_contract: str
    output_contract: str
    max_output_tokens: int
    temperature: float | None = 0
    thinking: bool | None = False
    request_identity: str | None = None
    feature_contract: str | None = None
    window_contract: str | None = None

    @property
    def evaluator_id(self) -> str:
        return content_id(asdict(self))


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    run_id: str
    evaluator_id: str
    kind: Literal["online", "inference", "policies", "legacy"]
    evaluator_spec: dict[str, Any]
    manifest: dict[str, Any]

    @classmethod
    def online(cls, spec: EvaluatorSpec) -> EvaluationRun:
        manifest = {"mode": "online", "evaluator_id": spec.evaluator_id}
        return cls(content_id(manifest), spec.evaluator_id, "online", asdict(spec), manifest)

    @classmethod
    def replay(
        cls,
        spec: EvaluatorSpec,
        *,
        case_ids: list[str],
        since_ms: int,
        until_ms: int,
        policy_config: dict[str, Any],
        mode: Literal["inference", "policies"],
        source_run_id: str | None = None,
        tag: str = "default",
    ) -> EvaluationRun:
        if not tag or len(tag) > 128 or since_ms < 0 or until_ms <= since_ms:
            raise ValueError("evaluation_run_manifest_invalid")
        manifest = {
            "mode": mode,
            "evaluator_id": spec.evaluator_id,
            "case_ids": sorted(case_ids),
            "since_ms": since_ms,
            "until_ms": until_ms,
            "policy_config": policy_config,
            "source_run_id": source_run_id,
            "tag": tag,
        }
        return cls(content_id(manifest), spec.evaluator_id, mode, asdict(spec), manifest)


def assessment_id(case_id: str, run_id: str) -> str:
    return content_id(("assessment_v2", case_id, run_id))


def action_id(assessment: str, policy_id: str, policy_version: str) -> str:
    return content_id(("action_v2", assessment, policy_id, policy_version))
