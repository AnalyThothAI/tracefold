"""Atomic content-addressed files for the Production V3 evidence clock."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

EvidenceArtifactKind = Literal["capture", "drain", "corpus", "candidate", "future-result"]


def canonical_evidence_bytes(artifact: BaseModel) -> bytes:
    return json.dumps(
        artifact.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def publish_evidence_artifact(
    root: Path,
    *,
    kind: EvidenceArtifactKind,
    artifact: BaseModel,
) -> tuple[Path, str]:
    payload = canonical_evidence_bytes(artifact)
    digest = hashlib.sha256(payload).hexdigest()
    destination = root / kind / f"{digest}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        verify_evidence_artifact(destination, expected_sha256=digest)
        return destination, digest

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        verify_evidence_artifact(temporary, expected_sha256=digest)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            verify_evidence_artifact(destination, expected_sha256=digest)
    finally:
        temporary.unlink(missing_ok=True)
    return destination, digest


def verify_evidence_artifact(path: Path, *, expected_sha256: str) -> bytes:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("trading_evidence_artifact_corrupt") from exc
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeError("trading_evidence_artifact_corrupt")
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("trading_evidence_artifact_corrupt") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("trading_evidence_artifact_corrupt")
    return payload


def load_evidence_artifact[ArtifactT: BaseModel](
    path: Path,
    model: type[ArtifactT],
    *,
    expected_sha256: str | None = None,
) -> ArtifactT:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("trading_evidence_artifact_corrupt") from exc
    if expected_sha256 is not None and hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeError("trading_evidence_artifact_corrupt")
    try:
        artifact = model.model_validate_json(payload)
    except ValueError as exc:
        raise RuntimeError("trading_evidence_artifact_corrupt") from exc
    if hashlib.sha256(canonical_evidence_bytes(artifact)).hexdigest() != hashlib.sha256(payload).hexdigest():
        raise RuntimeError("trading_evidence_artifact_not_canonical")
    return artifact


__all__ = [
    "EvidenceArtifactKind",
    "canonical_evidence_bytes",
    "load_evidence_artifact",
    "publish_evidence_artifact",
    "verify_evidence_artifact",
]
