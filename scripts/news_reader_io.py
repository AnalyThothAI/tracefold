"""Canonical local JSONL inputs, including gzip exports, for offline News reader tools."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tracefold.news.updates.identity import canonical_json


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("news_reader_jsonl_object_required")
    return rows


def jsonl_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(f"{canonical_json(row)}\n" for row in rows).encode("utf-8")


def dataset_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    """Digest canonical uncompressed records, independent of gzip headers."""
    return hashlib.sha256(jsonl_bytes(rows)).hexdigest()


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> str:
    raw = jsonl_bytes(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        path.chmod(0o600)
        stream.write(gzip.compress(raw, mtime=0) if path.suffix == ".gz" else raw)
    return hashlib.sha256(raw).hexdigest()
