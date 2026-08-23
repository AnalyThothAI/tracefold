from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from .news_learning_documents import _read_json_or_yaml


def _load_candidate_bundle(path: str) -> tuple[Any | None, dict[str, str]]:
    if not path:
        return None, {}
    from tracefold.news import CandidateManifest

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
) -> dict[tuple[str, str], Any]:
    from tracefold.news.agents.semantic_program import (
        DspyNewsSemanticProgram,
        RecordReplayPredictorAdapter,
    )

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
        "SELECT DISTINCT ON (request_sha256) request_sha256, request, response "
        "FROM news_model_recordings WHERE response IS NOT NULL "
        "AND request ? 'program_sha256' AND request ? 'runtime_binding_sha256' "
        "ORDER BY request_sha256, created_at_ms DESC"
    ).fetchall()
    recordings_by_program: dict[str, dict[str, Any]] = {}
    for row in rows:
        recorded_request = dict(row["request"] or {})
        program_sha = str(recorded_request.get("program_sha256") or "")
        if not program_sha:
            raise ValueError("news_learning_recording_program_identity_missing")
        recordings_by_program.setdefault(program_sha, {})[str(row["request_sha256"])] = {
            "request": recorded_request,
            "response": row["response"],
        }
    judges: dict[tuple[str, str], Any] = {}
    for arm_name, arm, artifact in arm_artifacts:
        replay = RecordReplayPredictorAdapter(recordings_by_program.get(arm.program_sha256, {}))
        judges[(arm_name, arm.bundle_sha)] = DspyNewsSemanticProgram(
            artifact,
            primary_adapter=replay,
            fallback_adapter=replay,
        )
    return judges


def _learning_recording_replay_capability(
    conn: Any,
    *,
    stable: Any,
    candidate: Any,
    artifact_paths: Mapping[str, str],
    run_sha: str,
) -> Any:
    from tracefold.news import ReplayArmSpec, load_recording_replay_capability

    arm_artifacts = _learning_program_arm_artifacts(
        stable=stable,
        candidate=candidate,
        artifact_paths=artifact_paths,
    )
    return load_recording_replay_capability(
        conn,
        run_sha=run_sha,
        arms=tuple(
            ReplayArmSpec(arm=arm_name, bundle_sha=arm.bundle_sha, artifact=artifact)
            for arm_name, arm, artifact in arm_artifacts
        ),
    )


def _learning_program_arm_artifacts(
    *,
    stable: Any,
    candidate: Any,
    artifact_paths: Mapping[str, str],
) -> tuple[tuple[Literal["stable", "candidate"], Any, Any], ...]:
    from tracefold.news.agents.semantic_program import (
        ProgramArtifactCodec,
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
        candidate_artifact = ProgramArtifactCodec.load(path) if path else load_program_artifact(candidate_sha)
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
    public = json.loads(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str))
    from tracefold.news import canonical_sha

    artifact_sha = canonical_sha({"kind": kind, "payload": public})
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, %s, %s, %s::jsonb, 'learning_propose', %s) "
        "ON CONFLICT (artifact_sha) DO NOTHING",
        (artifact_sha, kind, parent_sha, json.dumps(public, ensure_ascii=False, sort_keys=True), created_at_ms),
    )
    row = conn.execute(
        "SELECT kind, payload FROM news_learning_artifacts WHERE artifact_sha = %s",
        (artifact_sha,),
    ).fetchone()
    if row is None or str(row["kind"]) != kind or dict(row["payload"] or {}) != public:
        raise ValueError("news_learning_artifact_collision")
    return artifact_sha
