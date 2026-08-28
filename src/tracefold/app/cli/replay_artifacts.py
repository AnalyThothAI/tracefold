"""Atomic filesystem publication for immutable replay artifacts."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from tracefold.trading.replay import ReplayArtifactV1, canonical_artifact_bytes, verify_replay_artifact_bytes


def publish_replay_artifact(root: Path, artifact: ReplayArtifactV1) -> tuple[Path, str]:
    payload = canonical_artifact_bytes(artifact)
    digest = hashlib.sha256(payload).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / artifact.run_id
    artifact_path = destination / "replay.json"
    if destination.exists():
        verify_replay_artifact(artifact_path, expected_sha256=digest)
        return artifact_path, digest

    temporary = Path(tempfile.mkdtemp(prefix=f".{artifact.run_id}.", dir=root))
    try:
        candidate = temporary / "replay.json"
        candidate.write_bytes(payload)
        verify_replay_artifact(candidate, expected_sha256=digest)
        try:
            os.replace(temporary, destination)
        except OSError:
            if not destination.exists():
                raise
            verify_replay_artifact(artifact_path, expected_sha256=digest)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return artifact_path, digest


def verify_replay_artifact(path: Path, *, expected_sha256: str) -> None:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("replay_artifact_corrupt") from exc
    verify_replay_artifact_bytes(payload, expected_sha256=expected_sha256)


__all__ = ["publish_replay_artifact", "verify_replay_artifact"]
