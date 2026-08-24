"""Regenerate the sole packaged stable Program strategy artifact.

The binary has exactly one executable factory. Cross-generation rollback images are deployment artifacts
built outside this registry; this tool never creates a second runtime-loadable profile.
"""

from __future__ import annotations

import argparse
import importlib.resources
import json
import os
from pathlib import Path
from typing import Any

from ..artifact_identity import canonical_json
from ..program.artifact import (
    ProgramStrategyArtifactCodec,
    ProgramStrategyArtifactV1,
    build_code_owned_program_artifact,
)
from ..program.runtime import PROGRAM_SCHEMA_VERSION


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"news_program_duplicate_key:{key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"news_program_json_nonfinite:{value}")


def _read_canonical_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"news_program_identity_file_invalid:{path.name}")
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"news_program_json_invalid:{path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"news_program_json_object_required:{path.name}")
    canonical = canonical_json(value)
    if text not in {canonical, canonical + "\n"}:
        raise ValueError(f"news_program_json_noncanonical:{path.name}")
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
        if image.is_symlink() or not image.is_file():
            raise ValueError("news_program_artifact_path_invalid")
        if image.read_text(encoding="utf-8") != document:
            raise ValueError("news_program_artifact_existing_image_mismatch")
        return image
    temporary = image.with_name(f".{image.name}.tmp")
    temporary.write_text(document, encoding="utf-8")
    os.replace(temporary, image)
    ProgramStrategyArtifactCodec.decode(image.read_text(encoding="utf-8"))
    return image


def regenerate_stable_program_artifact(*, programs_root: Path | None = None) -> str:
    """Atomically replace the one-entry registry with the reviewed root."""

    # Resolved from the owning package, not from this module's own location: the registry lives with the
    # Program (`news/program/resources`), while this tool lives with the learning plane, and PR8 moved
    # both. A `Path(__file__).parent / "programs"` here silently pointed at a directory that no longer
    # existed — the same failure mode the compile source seal hit in PR8-A.
    root = (programs_root or Path(str(importlib.resources.files("tracefold.news.program"))) / "resources").resolve()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("news_program_registry_path_invalid")
    registry_path = root / "registry.json"
    registry = _read_canonical_object(registry_path)
    if set(registry) != {"stable", "images"} or not isinstance(registry["images"], list):
        raise ValueError("news_program_registry_schema_invalid")
    old_sha = str(registry["stable"])
    if [str(value) for value in registry["images"]] != [old_sha]:
        raise ValueError("news_program_regenerate_with_candidates_forbidden")

    artifact = build_code_owned_program_artifact()
    new_image = _write_image(root, artifact)
    _atomic_json(registry_path, {"images": [artifact.program_sha256], "stable": artifact.program_sha256})
    registered = _read_canonical_object(registry_path)
    if registered != {"images": [artifact.program_sha256], "stable": artifact.program_sha256}:
        _atomic_json(registry_path, registry)
        raise ValueError("news_program_registry_switch_failed")
    ProgramStrategyArtifactCodec.decode(new_image.read_text(encoding="utf-8"))

    if old_sha != artifact.program_sha256:
        old_image = root / f"{old_sha}.json"
        if old_image.is_symlink() or not old_image.is_file():
            raise ValueError("news_program_previous_image_invalid")
        previous = _read_canonical_object(old_image)
        if previous.get("program_sha256") != old_sha or previous.get("schema_version") != PROGRAM_SCHEMA_VERSION:
            raise ValueError("news_program_previous_image_identity_invalid")
        old_image.unlink()
    return artifact.program_sha256


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args(argv)
    print(regenerate_stable_program_artifact())


if __name__ == "__main__":
    main()


__all__ = ["regenerate_stable_program_artifact"]
