"""Regenerate the sole packaged stable Program strategy artifact.

The binary has exactly one executable factory and no second runtime-loadable
profile.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import uuid
from pathlib import Path
from typing import Any

from ..artifact_identity import canonical_json
from .artifact import (
    ProgramStrategyArtifactCodec,
    ProgramStrategyArtifactV1,
    _write_exclusive,
    build_code_owned_program_artifact,
)
from .runtime import PROGRAM_SCHEMA_VERSION


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"news_program_json_invalid:{path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"news_program_json_object_required:{path.name}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _write_image(root: Path, artifact: ProgramStrategyArtifactV1) -> Path:
    document = ProgramStrategyArtifactCodec.encode(artifact)
    ProgramStrategyArtifactCodec.decode(document)
    image = root / f"{artifact.program_sha256}.json"
    if image.exists():
        if image.read_text(encoding="utf-8") != document:
            raise ValueError("news_program_artifact_existing_image_mismatch")
        return image
    # Exclusive and unique, then unwound on any failure. #319 dropped the no-follow half; exclusive
    # creation stays because two tool runs writing one image must collide loudly rather than interleave.
    temporary = image.with_name(f".{image.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_exclusive(temporary, document)
        os.replace(temporary, image)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    ProgramStrategyArtifactCodec.decode(image.read_text(encoding="utf-8"))
    return image


def regenerate_stable_program_artifact(*, programs_root: Path | None = None) -> str:
    """Atomically replace the one-entry registry with the reviewed root."""

    # Resolved from the owning package, not from this module's own location: the registry lives with the
    # Program (`news/program/resources`), while this tool lives with the learning plane, and PR8 moved
    # both. A `Path(__file__).parent / "programs"` here silently pointed at a directory that no longer
    # existed — the same failure mode the compile source seal hit in PR8-A.
    root = programs_root or Path(str(importlib.resources.files("tracefold.news.program"))) / "resources"
    registry_path = root / "registry.json"
    registry = _read_json_object(registry_path)
    if set(registry) != {"stable", "images"} or not isinstance(registry["images"], list):
        raise ValueError("news_program_registry_schema_invalid")
    old_sha = str(registry["stable"])
    if [str(value) for value in registry["images"]] != [old_sha]:
        raise ValueError("news_program_regenerate_with_candidates_forbidden")

    artifact = build_code_owned_program_artifact()
    new_image = _write_image(root, artifact)
    _atomic_json(registry_path, {"images": [artifact.program_sha256], "stable": artifact.program_sha256})
    registered = _read_json_object(registry_path)
    if registered != {"images": [artifact.program_sha256], "stable": artifact.program_sha256}:
        _atomic_json(registry_path, registry)
        raise ValueError("news_program_registry_switch_failed")
    ProgramStrategyArtifactCodec.decode(new_image.read_text(encoding="utf-8"))

    if old_sha != artifact.program_sha256:
        old_image = root / f"{old_sha}.json"
        previous = _read_json_object(old_image)
        if previous.get("program_sha256") != old_sha or previous.get("schema_version") != PROGRAM_SCHEMA_VERSION:
            raise ValueError("news_program_previous_image_identity_invalid")
        old_image.unlink()
    return artifact.program_sha256


__all__ = ["regenerate_stable_program_artifact"]
