"""Atomic content-addressed files for recorded analysis input and output."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

_MAX_ARTIFACT_BYTES = 4_194_304


class AnalysisFiles:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, value: Any) -> str:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
        if len(data) > _MAX_ARTIFACT_BYTES:
            raise ValueError("analysis_file_oversized")
        digest = hashlib.sha256(data).hexdigest()
        target = self.root / digest[:2] / f"{digest}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("analysis_file_digest_mismatch")
            return digest
        descriptor, name = tempfile.mkstemp(prefix=".analysis-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return digest

    def read(self, ref: str) -> Any:
        if len(ref) != 64 or any(char not in "0123456789abcdef" for char in ref):
            raise ValueError("analysis_ref_invalid")
        data = (self.root / ref[:2] / f"{ref}.json").read_bytes()
        if len(data) > _MAX_ARTIFACT_BYTES:
            raise ValueError("analysis_file_oversized")
        if hashlib.sha256(data).hexdigest() != ref:
            raise ValueError("analysis_file_digest_mismatch")
        return json.loads(data)
