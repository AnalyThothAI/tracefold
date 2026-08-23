"""The frozen recorded-baseline calibration corpus, and the redaction that makes it publishable.

The recorded calibration was reproducible only against the operator's live database, so it stopped being a
calibration the moment the corpus grew — #143 published `0.896373 / n=162` and by 2026-08-23 the same command
answered `0.888426 / n=243` because #148 added 81 reviews. A number that moves when the data moves cannot
prove that metric *wiring* is unchanged, which is the only thing this check exists to prove.

So the corpus is frozen into `tests/fixtures/news_baseline_calibration_v1.json.gz`, and the free text is
redacted on the way in. `_redact()` maps each distinct string to `redacted:<sha256[:16]>`, which preserves
every comparison the recorded metric performs — all of them are equality — while publishing no provider
headline, body, or reviewer prose. Equal strings stay equal, different strings stay different, and the score
is bit-identical to the same run against the live database (asserted in
`tests/news/test_news_baseline_calibration.py`).

The redaction is deliberately *not* similarity-preserving: `decide()`'s character-bigram duplicate check
would read different neighbours out of redacted headlines. This fixture is therefore valid for
`--mode recorded` only, and the calibration test pins that.

Regenerate (requires the operator's database and `~/.tracefold/config.yaml`):

    uv run python -m tests.support.baseline_calibration tests/fixtures/news_baseline_calibration_v1.json.gz
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any

CALIBRATION_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_baseline_calibration_v1.json.gz"
CALIBRATION_SCHEMA = "tracefold.news.baseline_calibration_corpus.v1"

# Every key in the corpus whose value is prose written by a news provider, by the Program, or by a reviewer.
# Enumerated rather than inferred: a heuristic that guessed wrong would silently publish the text it was
# added to withhold.
_TEXT_KEYS = frozenset(
    {
        "title",  # context.evidence, policy_metric.storyline
        "raw_first_line",
        "content",
        "comparison_title",
        "headline_zh",  # production_verdict, told entries, seen ledger
        "why_zh",
        "note",  # accepted_review
        "expected_correction",
    }
)


_REDACTED = re.compile(r"^redacted:[0-9a-f]{16}$")


def _redact(value: Any, *, key: str = "") -> Any:
    """Idempotent by construction, so re-running it over the fixture is the check that nothing leaked."""

    if isinstance(value, dict):
        return {name: _redact(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key=key) for item in value]
    if isinstance(value, str) and key in _TEXT_KEYS and value and not _REDACTED.match(value):
        return f"redacted:{hashlib.sha256(value.encode()).hexdigest()[:16]}"
    return value


def load_calibration_corpus() -> dict[str, Any]:
    with gzip.open(CALIBRATION_FIXTURE, "rb") as handle:
        payload: dict[str, Any] = json.loads(handle.read())
    if payload.get("schema") != CALIBRATION_SCHEMA:
        raise ValueError(f"news_baseline_calibration_schema_unknown:{payload.get('schema')}")
    return payload


def write_calibration_corpus(path: Path, payload: dict[str, Any]) -> int:
    """Deterministic bytes: sorted keys, no timestamp, so a regeneration that changed nothing is an empty diff."""

    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as handle:
        handle.write(blob)
    return len(blob)


def _main(destination: str) -> None:  # pragma: no cover - operator tool, needs the live database
    from tracefold.app.learning_runtime import active_arm_manifest
    from tracefold.app.repositories import postgres_connection
    from tracefold.news import CandidateEvaluator, ClosedWindow
    from tracefold.news.agents.semantic_program import load_stable_program_artifact
    from tracefold.platform.config.settings import load_settings

    settings = load_settings()
    stable = active_arm_manifest(settings)
    artifact = load_stable_program_artifact()
    window = ClosedWindow(from_ms=1_786_000_000_000, to_ms=1_787_460_000_000)
    with postgres_connection(settings, role="serve") as conn:
        evaluator = CandidateEvaluator(conn, stable=stable, judges={})
        episodes = evaluator.baseline_episodes(window, cohort=False, limit=5000)

    payload = {
        "schema": CALIBRATION_SCHEMA,
        "captured_window": {"from_ms": window.from_ms, "to_ms": window.to_ms},
        "program_sha256": artifact.program_sha256,
        "policy_sha256": stable.policy_sha256,
        "redaction": {
            "keys": sorted(_TEXT_KEYS),
            "form": "redacted:<sha256[:16]>",
            "property": "equality-preserving, not similarity-preserving; valid for --mode recorded only",
        },
        "episodes": [_redact(dict(episode)) for episode in episodes],
    }
    size = write_calibration_corpus(Path(destination), payload)
    print(json.dumps({"episodes": len(payload["episodes"]), "raw_bytes": size}))


if __name__ == "__main__":  # pragma: no cover
    import sys

    _main(sys.argv[1])
