"""Regenerate the sole executable stable image without destroying its history."""

from __future__ import annotations

import importlib.resources
import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..artifact_identity import canonical_json, canonical_sha
from .artifact import (
    NewsProgramStateV1,
    _write_exclusive,
    build_code_owned_program_state,
    decode_program_state,
    encode_program_state,
)

# Historical images are verified as documents, never loaded through today's graph.
# A future schema cut must explicitly retain the identity format it can read here.
_HISTORICAL_SCHEMAS = frozenset({"news_program_state_v1"})


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


def _verify_previous_image(root: Path, identity: str) -> None:
    """Check the original hash without applying current Predictor/schema validation.

    In the v1 identity format, only each Predictor's ``lm`` entry is excluded.
    Do not reissue the image with new defaults: that would authenticate different
    bytes and could silently replace a previously deployed program's identity.
    """

    if len(identity) != 64 or any(char not in "0123456789abcdef" for char in identity):
        raise ValueError("news_program_previous_image_identity_invalid")
    previous = _read_json_object(root / f"{identity}.json")
    if (
        previous.get("program_sha256") != identity
        or previous.get("schema_version") not in _HISTORICAL_SCHEMAS
    ):
        raise ValueError("news_program_previous_image_identity_invalid")
    try:
        state = previous["state"]
        if not isinstance(state, Mapping) or any(not isinstance(value, Mapping) for value in state.values()):
            raise ValueError("news_program_previous_image_identity_invalid")
        material = {
            "schema_version": previous["schema_version"],
            "evidence_input_version": previous["evidence_input_version"],
            "dspy_version": previous["dspy_version"],
            "predictors": previous["predictors"],
            "state": {
                name: {key: value for key, value in document.items() if key != "lm"}
                for name, document in state.items()
            },
        }
        digest = canonical_sha(material)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("news_program_previous_image_identity_invalid") from exc
    if digest != identity:
        raise ValueError("news_program_previous_image_identity_invalid")


def _write_image(root: Path, state: NewsProgramStateV1) -> Path:
    document = encode_program_state(state)
    decode_program_state(document)
    image = root / f"{state.program_sha256}.json"
    if image.exists():
        if image.read_text(encoding="utf-8") != document:
            raise ValueError("news_program_artifact_existing_image_mismatch")
        return image
    temporary = image.with_name(f".{image.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_exclusive(temporary, document)
        os.replace(temporary, image)
    finally:
        temporary.unlink(missing_ok=True)
    decode_program_state(image.read_text(encoding="utf-8"))
    return image


def regenerate_stable_program_state(*, programs_root: Path | None = None) -> str:
    """Validate both sides before atomically publishing the new executable root.

    A failed build can leave an unregistered content-addressed image, but cannot
    retire the working registry. Old images remain readable historical documents;
    omission from ``images`` keeps them out of the runtime's executable registry.
    """

    root = programs_root or Path(str(importlib.resources.files("tracefold.news.program"))) / "resources"
    registry_path = root / "registry.json"
    registry = _read_json_object(registry_path)
    if set(registry) != {"stable", "images"} or not isinstance(registry["images"], list):
        raise ValueError("news_program_registry_schema_invalid")
    old_sha = str(registry["stable"])
    if [str(value) for value in registry["images"]] != [old_sha]:
        raise ValueError("news_program_regenerate_with_candidates_forbidden")

    _verify_previous_image(root, old_sha)
    state = build_code_owned_program_state()
    _write_image(root, state)
    # All fallible image reads, validation and construction precede this commit
    # point. Never restore an old registry after publishing a verified new one.
    _atomic_json(registry_path, {"images": [state.program_sha256], "stable": state.program_sha256})
    return state.program_sha256


__all__ = ["regenerate_stable_program_state"]
