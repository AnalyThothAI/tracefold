"""Trusted host-side Program compiler adapter.

This module owns restricted patch application, the hash-only diff and the artifact writer.  It deliberately
does not import the optimizer module, GEPA, or any provider client; the CLI can perform all privileged
post-container work without loading untrusted compiler code.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from ...program.artifact import (
    ProgramStrategyArtifactCodec,
    ProgramStrategyArtifactV1,
    ProgramStrategyPatchV1,
    load_stable_program_artifact,
)
from ...program.artifact import apply_program_patch as _apply_program_patch
from ...program.runtime import PROGRAM_FACTORY_ID, PROGRAM_SCHEMA_VERSION, PredictorName
from .security import (
    METRIC_JUDGE_MAX_TOKENS,
    METRIC_JUDGE_TIMEOUT_SECONDS,
    REFLECTION_MAX_TOKENS,
    REFLECTION_TIMEOUT_SECONDS,
)

LEARNING_EPOCH: Literal["program_v7"] = "program_v7"
# The reflection endpoint's budget, declared on the trusted seam because the CLI may not import the optimizer
# module. GEPA's reflection call reads a minibatch of failures and emits a whole replacement instruction, so it
# is nothing like a Program route: DSPy documents 32k output tokens for it, while the task route's ceiling here
# is 1,200 — below even what one advisory instruction itself accepts.


def apply_trusted_program_patch(
    parent: ProgramStrategyArtifactV1,
    patch: ProgramStrategyPatchV1,
) -> ProgramStrategyArtifactV1:
    """Apply the sole optimizer write-set against the exact active stable root."""

    return _apply_program_patch(parent, patch)


def reapply_exact_candidate(
    parent: ProgramStrategyArtifactV1,
    patch: ProgramStrategyPatchV1,
    candidate: ProgramStrategyArtifactV1,
) -> ProgramStrategyArtifactV1:
    """Rebuild a loaded candidate from its parent and patch, and require the exact same bytes."""

    rebuilt = _apply_program_patch(parent, patch)
    if rebuilt != candidate:
        raise ValueError("news_learning_program_trusted_reapply_mismatch")
    return rebuilt


def load_exact_stable_program() -> ProgramStrategyArtifactV1:
    parent = load_stable_program_artifact()
    if parent.schema_version != PROGRAM_SCHEMA_VERSION or parent.factory_id != PROGRAM_FACTORY_ID:
        raise ValueError("news_program_compile_parent_must_be_exact_stable_root")
    return parent


def load_program_artifact(path: str | None = None) -> ProgramStrategyArtifactV1:
    return ProgramStrategyArtifactCodec.load(path)


def parse_program_patch(payload: Mapping[str, Any]) -> ProgramStrategyPatchV1:
    return ProgramStrategyPatchV1.model_validate(payload)


def program_machine_diff(
    parent: ProgramStrategyArtifactV1,
    candidate: ProgramStrategyArtifactV1,
) -> dict[str, Any]:
    """Prove the compile changed nothing but the advisory instructions, and say which ones.

    The returned mapping is the persisted receipt: `factory_id` is the whole immutable surface, and the two
    Program roots already commit to the instruction bytes. `changed_predictors` is returned beside it for
    the operator and is deliberately not part of the receipt — nothing that reads a stored candidate holds
    the instructions, so it could never be checked there.
    """

    if candidate.factory_id != parent.factory_id or candidate.schema_version != parent.schema_version:
        raise ValueError("news_program_compile_machine_diff_immutable_change")
    if not changed_predictors(parent, candidate):
        raise ValueError("news_program_compile_machine_diff_empty")
    return {
        "schema_version": "tracefold.news.program_machine_diff.v4",
        "factory_id": parent.factory_id,
        "parent_program_sha256": parent.program_sha256,
        "candidate_program_sha256": candidate.program_sha256,
    }


def changed_predictors(
    parent: ProgramStrategyArtifactV1,
    candidate: ProgramStrategyArtifactV1,
) -> tuple[PredictorName, ...]:
    """Which advisory instructions a candidate rewrote, for the operator-facing proposal document."""

    predictors: tuple[PredictorName, ...] = ("event_semantics", "reader_card")
    return tuple(
        predictor
        for predictor in predictors
        if parent.instruction_for(predictor) != candidate.instruction_for(predictor)
    )


def write_program_candidate_artifact(artifact: ProgramStrategyArtifactV1, *, artifact_root: Path) -> str:
    """Persist one already trusted/applied artifact document atomically."""

    requested_root = Path(artifact_root)
    if ".." in requested_root.parts or requested_root.is_symlink():
        raise ValueError("news_program_compile_artifact_root_invalid")
    requested_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root = requested_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("news_program_compile_artifact_root_invalid") from exc
    if not root.is_dir() or requested_root.absolute().resolve() != root:
        raise ValueError("news_program_compile_artifact_root_invalid")
    document = ProgramStrategyArtifactCodec.encode(artifact)
    destination = root / f"{artifact.program_sha256}.json"
    if destination.exists():
        if ProgramStrategyArtifactCodec.load(str(destination)) != artifact:
            raise ValueError("news_program_compile_artifact_collision")
        return str(destination)
    temporary = root / f".{artifact.program_sha256}.{uuid.uuid4().hex}.tmp"
    try:
        _write_exclusive(temporary, document)
        os.rename(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if ProgramStrategyArtifactCodec.load(str(destination)) != artifact:
        raise ValueError("news_program_compile_artifact_write_verification_failed")
    return str(destination)


def _write_exclusive(path: Path, document: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        encoded = document.encode("utf-8")
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "METRIC_JUDGE_MAX_TOKENS",
    "METRIC_JUDGE_TIMEOUT_SECONDS",
    "REFLECTION_MAX_TOKENS",
    "REFLECTION_TIMEOUT_SECONDS",
    "ProgramStrategyPatchV1",
    "apply_trusted_program_patch",
    "changed_predictors",
    "load_exact_stable_program",
    "load_program_artifact",
    "parse_program_patch",
    "program_machine_diff",
    "reapply_exact_candidate",
    "write_program_candidate_artifact",
]
