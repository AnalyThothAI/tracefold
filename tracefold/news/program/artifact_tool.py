"""Build the current program image, publish it, and remove obsolete packaged roots."""
from __future__ import annotations

import importlib.resources
import os
import re
import uuid
from pathlib import Path
from typing import Any

from ..artifact_identity import canonical_json
from .artifact import (
    NewsProgramStateV1, _write_exclusive, build_code_owned_program_state,
    decode_program_state, encode_program_state,
)

_IMAGE_NAME = re.compile(r"[0-9a-f]{64}\.json\Z")


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
    destination = root / f"{state.program_sha256}.json"
    if destination.exists():
        if destination.read_text(encoding="utf-8") != document:
            raise ValueError("news_program_artifact_existing_image_mismatch")
        return destination
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_exclusive(temporary, document)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    decode_program_state(destination.read_text(encoding="utf-8"))
    return destination


def regenerate_stable_program_state(*, programs_root: Path | None = None) -> str:
    """No old-schema gate, historical decoder, upgrade, or fallback execution root.

    The new image validates before registry publication. Old packaged images are
    deleted only after that commit point; the Git history retains previous code.
    This build tool does not read or mutate database rows, queues or deployments.
    Cleanup errors propagate without rolling the valid registry back; a rerun
    completes cleanup idempotently. Unrelated JSON and candidate subdirectories
    are not deleted by this root-image cleanup.
    """
    root = programs_root or Path(str(importlib.resources.files("tracefold.news.program"))) / "resources"
    root.mkdir(parents=True, exist_ok=True)
    state = build_code_owned_program_state()
    current = _write_image(root, state)
    _atomic_json(root / "registry.json", {"images": [state.program_sha256], "stable": state.program_sha256})
    for path in root.iterdir():
        if path != current and path.is_file() and _IMAGE_NAME.fullmatch(path.name):
            path.unlink()
    return state.program_sha256


__all__ = ["regenerate_stable_program_state"]
