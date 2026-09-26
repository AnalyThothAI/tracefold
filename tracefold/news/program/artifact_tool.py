"""Regenerate the sole packaged stable Program state image.

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
    NewsProgramStateV1,
    _write_exclusive,
    build_code_owned_program_state,
    decode_program_state,
    encode_program_state,
)
from .artifact_history import historical_program_document


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"news_program_json_invalid:{path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"news_program_json_object_required:{path.name}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_exclusive(temporary, canonical_json(value) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_image(root: Path, state: NewsProgramStateV1) -> Path:
    document = encode_program_state(state)
    decode_program_state(document)
    image = root / f"{state.program_sha256}.json"
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
    decode_program_state(image.read_text(encoding="utf-8"))
    return image


def regenerate_stable_program_state(*, programs_root: Path | None = None) -> str:
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

    # Historical identity is checked before any write. A schema cut must not
    # validate the previous graph with today's Signatures after switching stable.
    if len(old_sha) != 64 or any(char not in "0123456789abcdef" for char in old_sha):
        raise ValueError("news_program_registry_sha_invalid")
    old_image = root / f"{old_sha}.json"
    historical_program_document(old_image.read_text(encoding="utf-8"), expected_sha=old_sha)

    state = build_code_owned_program_state()
    _write_image(root, state)  # Complete current schema and identity verification before publication.
    _atomic_json(registry_path, {"images": [state.program_sha256], "stable": state.program_sha256})
    # Keep unregistered old images as historical evidence. Only registry members
    # are executable, so retention does not add a second runtime profile.
    return state.program_sha256


__all__ = ["regenerate_stable_program_state"]
