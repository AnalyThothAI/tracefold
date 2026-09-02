from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tracefold.news.artifact_identity import canonical_json, canonical_sha

_DATASET_SHA = "70206b87f8cc12d7585323d875e2fd5abb7a60142d94902191c20efd77d12b54"
_EPISODE_ROOT = "0468b6bf7bbc2246e7a197333ec5e1cf2fc7b922d69fb5fddb598856cf0c0212"
_CLUSTERS_FILE_SHA = "799fb51535e18c535d5ee94a8af9c26a1a3648ce03e39f25c2e6ce0e53b7a73f"
_ROOT = Path(__file__).parents[2] / "datasets" / "news" / "gold" / _DATASET_SHA
_SECRET_KEY = re.compile(r"(^|_)(api_?key|password|secret|token|cookie|authorization)($|_)", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{20,}")


def _walk(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            assert not _SECRET_KEY.search(str(key))
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, str):
        yield value


def test_published_gold_corpus_identity_and_secret_boundary() -> None:
    manifest_text = (_ROOT / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest_text == canonical_json(manifest) + "\n"
    assert canonical_sha({"kind": "dataset", "payload": manifest}) == _DATASET_SHA
    assert manifest["counts"]["case_n"] == 432
    assert manifest["counts"]["contract_cluster_receipt"]["cluster_n"] == 420

    cluster_bytes = (_ROOT / "clusters.jsonl").read_bytes()
    assert hashlib.sha256(cluster_bytes).hexdigest() == _CLUSTERS_FILE_SHA
    rows = [json.loads(line) for line in cluster_bytes.decode().splitlines()]
    assert len(rows) == 420
    assert len({row["cluster_id"] for row in rows}) == 420

    episodes: list[dict[str, Any]] = []
    for row in rows:
        assert set(row) == {"cases", "cluster_id"}
        assert all(case["cluster_id"] == row["cluster_id"] for case in row["cases"])
        episodes.extend(row["cases"])
    assert len(episodes) == 432
    assert len({episode["case_id"] for episode in episodes}) == 432
    episodes.sort(key=lambda episode: (episode["context"]["now_ms"], episode["case_id"]))
    assert canonical_sha(episodes) == _EPISODE_ROOT
    assert not any(_SECRET_VALUE.search(value) for value in _walk([manifest, *rows]))
