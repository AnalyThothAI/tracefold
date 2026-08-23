"""The frozen recorded-baseline calibration corpus, and the redaction that makes it publishable.

The recorded calibration was reproducible only against the operator's live database, so it stopped being a
calibration the moment the corpus grew — #143 published `0.896373 / n=162` and by 2026-08-23 the same command
answered `0.888426 / n=243` because #148 added 81 reviews. A number that moves when the data moves cannot
prove that metric *wiring* is unchanged, which is the only thing this check exists to prove.

So the corpus is frozen into `tests/fixtures/news_baseline_calibration_v1.json.gz` with every string redacted
except an explicit structural allowlist. `_redact()` maps each remaining value to `redacted:<sha256[:16]>`,
which preserves every comparison the recorded metric performs — all of them are equality — while publishing
no provider headline, body, source handle, card or reviewer prose. Equal strings stay equal, different
strings stay different, and the score is bit-identical to the same run against the live database (asserted in
`tests/news/test_news_baseline_calibration.py`).

**The allowlist is the whole design.** The first version enumerated the *text* keys instead and failed open
in the obvious way: `title_zh` was not on the list, so 60 reader-facing Chinese cards shipped into a public
repository under a docstring promising they had not. A forgotten key has to fail safe. It now does — an
unlisted key is redacted, and the only cost of forgetting one is a score that moves, which the calibration
test catches immediately.

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

# Keys whose values the recorded metric, the cluster grouping or the retrieval receipt compare or count, and
# which carry no prose: rubric labels, enums, symbols, content hashes, opaque identifiers and stable keys.
# Everything not on this list is redacted, including keys that do not exist yet.
_STRUCTURAL_KEYS = frozenset(
    {
        # rubric labels and the reviewer's own answers
        "asset_grounding",
        "direction",
        "factual_fidelity",
        "headline_fidelity",
        "magnitude",
        "timeliness",
        "why_support",
        "why_value",
        "should_push",
        "judgment",
        "first_bad_owner",
        "evidence_refs",
        # verdict enums the metric scores directly
        "novelty",
        "event_type",
        "decision",
        "scope",
        "audience",
        "role",
        "market_type",
        "tier",
        # instruments: `asset_grounding` gold is compared as a symbol set
        "assets",
        "symbol",
        "symbols",
        "grounded_assets",
        "provider_coins",
        # identity and grouping — opaque, and what the case/cluster roots are built from
        "case_id",
        "cluster_id",
        "event_id",
        "review_id",
        "focus_fact_id",
        "duplicate_of",
        "evidence_sha256",
        "storyline_key",
        "stratum",
        "recorded_action",
        # gate facts and the frozen policy
        "admission",
        "asset_class",
        "engine_type",
        "family",
        "priority",
        "strategies",
        "policy_version",
        "policy_sha256",
        "unclear_push_event_types",
    }
)

_REDACTED = re.compile(r"^redacted:[0-9a-f]{16}$")
# Prose as a *shape* rather than as a key name. The guard test scans the shipped bytes for this instead of
# re-running `_redact` and comparing, which was a tautology: unlisted prose is a fixed point of a key-based
# redactor, so the old guard held while 60 Chinese cards sat in the file.
_HAN = re.compile(r"[一-鿿]")
_SENTENCE = re.compile(r"\S+\s+\S+\s+\S+")


def _redact(value: Any, *, key: str = "") -> Any:
    """Idempotent, and safe on a key nobody thought about."""

    if isinstance(value, dict):
        return {name: _redact(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key=key) for item in value]
    if isinstance(value, str) and value and key not in _STRUCTURAL_KEYS and not _REDACTED.match(value):
        return f"redacted:{hashlib.sha256(value.encode()).hexdigest()[:16]}"
    return value


def prose_offenders(payload: Any, *, path: str = "") -> list[tuple[str, str]]:
    """Every string in `payload` that reads like human language. Empty is the only acceptable answer."""

    found: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for name, item in payload.items():
            found.extend(prose_offenders(item, path=f"{path}.{name}"))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            found.extend(prose_offenders(item, path=f"{path}[{index}]"))
    elif (
        isinstance(payload, str)
        and not _REDACTED.match(payload)
        and (_HAN.search(payload) or _SENTENCE.search(payload))
    ):
        found.append((path, payload))
    return found


def load_calibration_corpus() -> dict[str, Any]:
    with gzip.open(CALIBRATION_FIXTURE, "rb") as handle:
        payload: dict[str, Any] = json.loads(handle.read())
    if payload.get("schema") != CALIBRATION_SCHEMA:
        raise ValueError(f"news_baseline_calibration_schema_unknown:{payload.get('schema')}")
    return payload


def write_calibration_corpus(path: Path, payload: dict[str, Any]) -> int:
    """Deterministic *content*: sorted keys and no timestamp, so a regeneration that changed nothing produces
    the same JSON document. The gzip container is not byte-reproducible across zlib builds, so the fixture's
    identity is the decompressed document and never its compressed size."""

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
        "redaction": {
            "structural_keys": sorted(_STRUCTURAL_KEYS),
            "form": "redacted:<sha256[:16]>",
            "rule": "allowlist: every string outside structural_keys is redacted",
            "property": "equality-preserving, not similarity-preserving; valid for --mode recorded only",
        },
        "episodes": [_redact(dict(episode)) for episode in episodes],
    }
    offenders = prose_offenders(payload["episodes"])
    if offenders:  # never write a fixture the guard test would reject
        raise SystemExit(f"news_baseline_calibration_prose_survived:{len(offenders)}:{offenders[0][0]}")
    size = write_calibration_corpus(Path(destination), payload)
    print(json.dumps({"episodes": len(payload["episodes"]), "raw_bytes": size}))


if __name__ == "__main__":  # pragma: no cover
    import sys

    _main(sys.argv[1])
