"""The frozen recorded-baseline calibration corpora, and the redaction that makes them publishable.

The recorded calibration was reproducible only against the operator's live database, so it stopped being a
calibration the moment the corpus grew — #143 published `0.896373 / n=162` and by 2026-08-23 the same command
answered `0.888426 / n=243` because #148 added 81 reviews. A number that moves when the data moves cannot
prove that metric *wiring* is unchanged, which is the only thing this check exists to prove.

The pre-#160 corpus remains frozen at `tests/fixtures/news_baseline_calibration_v1.json.gz`. It is historical
audit evidence only: its `production_verdict`, `recorded_action`, policy-v8 and Gate `priority` fields are
deliberately rejected by the current hard-cut contracts. Rewriting it would erase the evidence of what the
old ruler measured. The active typed corpus is the separate
`tests/fixtures/news_baseline_calibration_v2.json`, with `production_judgment`, the complete persisted
`recorded_decision_result`, policy v10 and `queue_priority`.

Every string is redacted except an explicit structural allowlist. `_redact()` maps each remaining value to
`redacted:<sha256[:16]>`, which preserves every equality comparison the recorded metric performs while
publishing no provider headline, body, source handle, card or reviewer prose. Equal strings stay equal and
different strings stay different.

**The allowlist is the whole design.** The first version enumerated the *text* keys instead and failed open
in the obvious way: `title_zh` was not on the list, so 60 reader-facing Chinese cards shipped into a public
repository under a docstring promising they had not. A forgotten key has to fail safe. It now does — an
unlisted key is redacted, and the only cost of forgetting one is a score that moves, which the calibration
test catches immediately.

The redaction is deliberately *not* similarity-preserving: `decide()`'s character-bigram duplicate check
would read different neighbours out of redacted headlines. This fixture is therefore valid for
`--mode recorded` only, and the calibration test pins that.

Regenerate v2 (requires the operator's database and `~/.tracefold/config.yaml`):

    uv run python -m tests.support.baseline_calibration tests/fixtures/news_baseline_calibration_v2.json
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
HISTORICAL_CALIBRATION_FIXTURE = _FIXTURE_DIR / "news_baseline_calibration_v1.json.gz"
HISTORICAL_CALIBRATION_SCHEMA = "tracefold.news.baseline_calibration_corpus.v1"
CALIBRATION_FIXTURE = _FIXTURE_DIR / "news_baseline_calibration_v2.json"
CALIBRATION_SCHEMA = "tracefold.news.baseline_calibration_corpus.v2"

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
        "trade_impact_breadth",
        "trade_tradability",
        "trade_surprise",
        "trade_development_delta",
        "trade_channels",
        "trade_affected_markets",
        "reader_value",
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
        "editorial_contract_version",
        "editorial_origin",
        "editorial_sha256",
        "verdict_sha256",
        "scored_judgment_sha256",
        # complete persisted DecisionResult projection
        "final",
        "override_rule",
        "throttled_by",
        "rule_baseline",
        "watchlist_hits",
        "seen_scope",
        # gate facts and the frozen policy
        "admission",
        "asset_class",
        "engine_type",
        "family",
        "queue_priority",
        "strategies",
        "policy_version",
        "policy_sha256",
        "unclear_push_event_types",
        # typed editorial relevance enums and bounded code sets
        "impact_breadth",
        "tradability",
        "surprise",
        "development_delta",
        "channels",
        "affected_markets",
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


def redact_calibration_episode(episode: dict[str, Any]) -> dict[str, Any]:
    """Redact one current episode and reissue identities over the redacted bytes.

    `ScoredJudgment` hashes its verdict. Redacting reader copy after issuing the
    judgment would therefore make the public fixture correctly private but
    contract-invalid. The public projection is a new, internally consistent
    judgment over equality-preserving redacted text; the historical v1 fixture
    is never passed through this function.
    """

    from tracefold.news.models import TriageVerdict
    from tracefold.news.semantic_contract import EditorialEnvelope, ScoredJudgment

    redacted = _redact(episode)
    raw = redacted.get("production_judgment")
    if isinstance(raw, dict):
        redacted["production_judgment"] = ScoredJudgment.issue(
            verdict=TriageVerdict.model_validate(raw["verdict"]),
            editorial=EditorialEnvelope.model_validate(raw["editorial"]),
        ).model_dump(mode="json")
    return redacted


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


def _load_fixture(path: Path, schema: str) -> dict[str, Any]:
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as handle:
            raw = handle.read()
    else:
        raw = path.read_bytes()
    payload: dict[str, Any] = json.loads(raw)
    if payload.get("schema") != schema:
        raise ValueError(f"news_baseline_calibration_schema_unknown:{payload.get('schema')}")
    return payload


def load_calibration_corpus() -> dict[str, Any]:
    """Load the current typed, policy-v10 calibration corpus."""

    return _load_fixture(CALIBRATION_FIXTURE, CALIBRATION_SCHEMA)


def load_historical_calibration_corpus() -> dict[str, Any]:
    """Load the immutable pre-#160 audit corpus without projecting it into current models."""

    return _load_fixture(HISTORICAL_CALIBRATION_FIXTURE, HISTORICAL_CALIBRATION_SCHEMA)


def write_calibration_corpus(path: Path, payload: dict[str, Any]) -> int:
    """Deterministic *content*: sorted keys and no timestamp, so a regeneration that changed nothing produces
    the same JSON document. The gzip container is not byte-reproducible across zlib builds, so the fixture's
    identity is the decompressed document and never its compressed size."""

    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if path.suffix == ".gz":
        with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as handle:
            handle.write(blob)
    else:
        path.write_bytes(blob)
    return len(blob)


def _main(destination: str) -> None:  # pragma: no cover - operator tool, needs the live database
    from tracefold.app.learning_runtime import active_arm_manifest
    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.agents.semantic_program import load_stable_program_artifact
    from tracefold.news.candidate_evaluator import CandidateEvaluator, ClosedWindow
    from tracefold.platform.config.loader import load_settings

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
        "episodes": [redact_calibration_episode(dict(episode)) for episode in episodes],
    }
    offenders = prose_offenders(payload["episodes"])
    if offenders:  # never write a fixture the guard test would reject
        raise SystemExit(f"news_baseline_calibration_prose_survived:{len(offenders)}:{offenders[0][0]}")
    size = write_calibration_corpus(Path(destination), payload)
    print(json.dumps({"episodes": len(payload["episodes"]), "raw_bytes": size}))


if __name__ == "__main__":  # pragma: no cover
    import sys

    _main(sys.argv[1])
