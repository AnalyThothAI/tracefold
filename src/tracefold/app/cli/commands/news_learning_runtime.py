from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from .news_learning_documents import _read_json_or_yaml

if TYPE_CHECKING:
    from tracefold.news.learning.contracts import CandidateManifest
    from tracefold.news.program.contracts import SemanticJudge


def _load_candidate_bundle(path: str) -> tuple[CandidateManifest | None, dict[str, str]]:
    if not path:
        return None, {}
    from tracefold.news.learning.contracts import CandidateManifest

    document = _read_json_or_yaml(path)
    candidate = CandidateManifest.model_validate(document.get("candidate") or document)
    artifacts = {str(key): str(value) for key, value in dict(document.get("program_artifacts") or {}).items()}
    return candidate, artifacts


def _learning_program_judges(
    conn: Any,
    *,
    settings: Any,
    stable: Any,
    candidate: Any,
    artifact_paths: Mapping[str, str],
    live: bool,
) -> dict[tuple[Literal["stable", "candidate"], str], SemanticJudge]:
    from tracefold.news.program.module import NativeNewsProgram
    from tracefold.news.program.routing import RoutedSemanticJudge

    arm_artifacts = _learning_program_arm_artifacts(
        stable=stable,
        candidate=candidate,
        artifact_paths=artifact_paths,
    )
    if live:
        return {
            (arm_name, arm.bundle_sha): _configured_program_judge(settings, artifact)
            for arm_name, arm, artifact in arm_artifacts
        }
    rows = conn.execute(
        "SELECT DISTINCT ON (execution_contract_sha, predictor_name, route, provider, model, model_sha, "
        "request_sha256) execution_contract_sha, predictor_name, route, provider, model, model_sha, "
        "request_sha256, request, response FROM news_model_recordings WHERE response IS NOT NULL "
        "ORDER BY execution_contract_sha, predictor_name, route, provider, model, model_sha, "
        "request_sha256, created_at_ms DESC"
    ).fetchall()
    judges: dict[tuple[Literal["stable", "candidate"], str], SemanticJudge] = {}
    for arm_name, arm, artifact in arm_artifacts:
        slots = _recorded_program_slots(rows, arm=arm)
        judges[(arm_name, arm.bundle_sha)] = RoutedSemanticJudge(
            NativeNewsProgram(artifact),
            primary=_recorded_route(slots, artifact=artifact, route="primary"),
            fallback=(
                _recorded_route(slots, artifact=artifact, route="fallback")
                if any(key[1] == "fallback" for key in slots)
                else None
            ),
        )
    return judges


def _recorded_program_slots(rows: Any, *, arm: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Select and group only recordings issued under this exact Program execution contract."""

    from tracefold.news.artifact_identity import canonical_sha
    from tracefold.news.program.lm import RecordedLM, RuntimeModelIdentity

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        provider = str(row.get("provider") or "")
        model = str(row.get("model") or "")
        model_sha = str(row.get("model_sha") or "")
        if not provider or not model or not model_sha:
            continue
        identity = RuntimeModelIdentity.issue(provider=provider, model=model, model_sha256=model_sha)
        expected_execution_sha = canonical_sha(
            {
                "program_sha256": arm.program_sha256,
                "runtime_model_bindings_sha256": arm.runtime_model_bindings_sha256,
                "envelope_sha256": arm.envelope_sha256,
                "runtime_binding_sha256": identity.binding_sha256,
                "provider": provider,
            }
        )
        if str(row.get("execution_contract_sha") or "") != expected_execution_sha:
            continue
        predictor = str(row.get("predictor_name") or "")
        route = str(row.get("route") or "")
        if predictor not in {"event_semantics", "reader_card"} or route not in {"primary", "fallback"}:
            continue
        terminal = dict(row.get("response") or {})
        if terminal.get("schema") != "tracefold.news.recorded_lm.v1":
            raise ValueError("news_program_recording_schema_unsupported")
        request_sha = str(row.get("request_sha256") or "")
        request = dict(row.get("request") or {})
        request_identity = terminal.get("request_identity")
        if not isinstance(request_identity, Mapping):
            raise ValueError("news_program_recording_request_identity_invalid")
        recorded_binding = str(request_identity.get("model_binding") or "")
        # This is validation only; the slot-level RecordedLM below owns replay.
        RecordedLM(
            {request_sha: terminal},
            model=model,
            runtime_identity=identity,
            model_binding=recorded_binding,
        )
        if terminal.get("request") != request or request.get("model") != model:
            raise ValueError("news_program_recording_request_identity_mismatch")
        key = (predictor, route)
        group = groups.setdefault(key, {"identity": identity, "modes": set(), "recordings": {}})
        if group["identity"] != identity:
            raise ValueError("news_learning_recording_route_ambiguous")
        group["modes"].add(_recorded_request_mode(request))
        group["recordings"][request_sha] = terminal
    return groups


def _recorded_request_mode(request: Mapping[str, Any]) -> str:
    config = request.get("config")
    response_format = config.get("response_format") if isinstance(config, Mapping) else None
    if response_format is None:
        return "prompt_json"
    if response_format == {"type": "json_object"}:
        return "json_object"
    return "json_schema"


def _recorded_route(slots: Mapping[tuple[str, str], dict[str, Any]], *, artifact: Any, route: str) -> Any:
    from tracefold.news.program.lm import (
        AuditedConfiguredLM,
        RecordedLM,
        RuntimeModelIdentity,
        StructuredOutputMode,
    )
    from tracefold.news.program.routing import RouteLMs

    route_groups = [value for key, value in slots.items() if key[1] == route]
    default_identity = (
        route_groups[0]["identity"]
        if route_groups
        else RuntimeModelIdentity.issue(provider="recorded", model=f"recorded/{route}")
    )

    def lm_for(predictor: str) -> AuditedConfiguredLM:
        group = slots.get((predictor, route))
        identity = default_identity if group is None else group["identity"]
        modes = set() if group is None else set(group["modes"])
        mode: StructuredOutputMode
        if "json_schema" in modes:
            mode = "json_schema"
        elif modes == {"json_object"}:
            mode = "json_object"
        elif not modes or modes == {"prompt_json"}:
            mode = "prompt_json" if modes else "json_schema"
        else:
            raise ValueError("news_learning_recording_capability_ambiguous")
        recordings = {} if group is None else group["recordings"]
        binding = getattr(getattr(artifact, predictor).model_bindings, route)
        return AuditedConfiguredLM(
            RecordedLM(
                recordings,
                model=identity.model,
                runtime_identity=identity,
                model_binding=binding,
                structured_output=mode,
            ),
            structured_output=mode,
            runtime_identity=identity,
            predictor=predictor,
            route=route,
            model_binding=binding,
        )

    return RouteLMs(event_semantics=lm_for("event_semantics"), reader_card=lm_for("reader_card"))


def _learning_program_arm_artifacts(
    *,
    stable: Any,
    candidate: Any,
    artifact_paths: Mapping[str, str],
) -> tuple[tuple[Literal["stable", "candidate"], Any, Any], ...]:
    from tracefold.news.program.artifact import (
        ProgramStrategyArtifactCodec,
        load_program_artifact,
        load_stable_program_artifact,
    )

    stable_artifact = load_stable_program_artifact()
    if stable_artifact.program_sha256 != stable.program_sha256:
        raise ValueError("news_learning_stable_program_mismatch")
    candidate_arm = candidate.candidate_arm
    candidate_sha = candidate_arm.program_sha256
    candidate_artifact = stable_artifact
    if candidate_sha != stable_artifact.program_sha256:
        path = artifact_paths.get(candidate_sha)
        candidate_artifact = ProgramStrategyArtifactCodec.load(path) if path else load_program_artifact(candidate_sha)
    if candidate_artifact.program_sha256 != candidate_sha:
        raise ValueError("news_learning_candidate_program_mismatch")
    return (
        ("stable", stable, stable_artifact),
        ("candidate", candidate_arm, candidate_artifact),
    )


def _configured_program_judge(settings: Any, artifact: Any) -> Any:
    from tracefold.app.learning_runtime import compose_news_program_runtime

    program = compose_news_program_runtime(settings).semantic_judge(artifact)
    if program is None:
        raise ValueError("news_learning_live_program_not_configured")
    return program


def _insert_learning_artifact(
    conn: Any,
    *,
    kind: str,
    payload: Mapping[str, Any],
    parent_sha: str | None,
    created_at_ms: int,
) -> str:
    """Register one proposal document. The SQL belongs to News; this is only the call into it."""

    from tracefold.app.repository_session import repositories_for_connection

    return repositories_for_connection(conn).news.append_proposal_artifact(
        kind=kind,
        payload=payload,
        parent_sha=parent_sha,
        created_at_ms=created_at_ms,
    )
