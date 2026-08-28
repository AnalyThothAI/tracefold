"""Carry accepted-corpus cases across an arm change, one verified replay at a time (#300).

An arm identity change invalidates a frozen dataset for evaluation because every statement the dataset
derives from recorded behavior — targets, owners, delivery truth — was measured on the arm that produced
it. What it does not invalidate is the human judgment about evidence: golds and rubric verdicts describe
the (evidence → truth) mapping, which no deploy can move. This module re-earns the recorded-behavior
statements for the *current* arm by replaying it over the same frozen evidence and carrying forward only
the cases where the behavior is verified equivalent. Divergent cases are excluded and named; nothing here
weakens a gate, because the product is an ordinary development dataset sealed under the current cohort.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import dspy  # type: ignore[import-untyped]

from ..artifact_identity import canonical_sha
from ..program.dspy_adapter import DspyStrictJSONAdapter
from .judge import _SEMANTIC_FIELDS, CardEquivalenceJudge
from .metric import build_compile_example
from .objective import DevelopmentEpisode

MIGRATION_RECEIPT_SCHEMA = "tracefold.news.corpus_migration_receipt.v1"

# The judge's semantic fields decide whether two cards say the same thing; the pipeline fields decide what
# production does with them: `decision` drives delivery, `novelty` and `restates` drive the restatement
# rules (`decide()` reads the pointed-at told entry, so the same judgment against a different entry is a
# different outcome), and `audience` is reader-visible routing. A carried case must hold on all of them,
# or the recorded delivery truth stops describing the current arm.
_PIPELINE_FIELDS = ("decision", "novelty", "restates", "audience")


def verdict_field_diffs(recorded: Mapping[str, Any], replayed: Mapping[str, Any]) -> tuple[str, ...]:
    """Typed fields on which the two verdicts disagree, in a fixed order."""

    return tuple(
        name
        for name in (*_SEMANTIC_FIELDS, *_PIPELINE_FIELDS)
        if _comparable(recorded.get(name)) != _comparable(replayed.get(name))
    )


def _comparable(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return sorted(
            canonical_sha(item if isinstance(item, (str, int, float, bool, type(None))) else dict(item))
            for item in value
        )
    if isinstance(value, Mapping):
        return canonical_sha(dict(value))
    return value


def assess_replayed_case(
    recorded_verdict: Mapping[str, Any],
    replayed_verdict: Mapping[str, Any],
    judge: CardEquivalenceJudge,
) -> dict[str, Any]:
    """One case's carry-forward verdict: typed fields exactly, text through the equivalence judge."""

    diffs = verdict_field_diffs(recorded_verdict, replayed_verdict)
    if diffs:
        return {"verdict": "divergent", "field_diffs": list(diffs), "judge_status": "not_consulted"}
    assessment = judge.equivalence(dict(recorded_verdict), dict(replayed_verdict))
    if assessment.status == "unavailable" or assessment.verdict is None:
        return {"verdict": "error", "field_diffs": [], "judge_status": assessment.status}
    equivalent = (
        assessment.verdict.headline_equivalent
        and assessment.verdict.why_equivalent
        and assessment.verdict.facts_preserved
    )
    return {
        "verdict": "equivalent" if equivalent else "divergent",
        "field_diffs": [] if equivalent else ["card_text"],
        "judge_status": assessment.status,
    }


def run_corpus_migration(
    episodes: Sequence[DevelopmentEpisode],
    *,
    program: dspy.Module,
    lm: dspy.LM,
    judge: CardEquivalenceJudge,
    max_model_cases: int,
    from_dataset_sha: str,
    replay_identity: Mapping[str, Any],
    verdict_extractor: Callable[[Any], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Replay the current arm over every episode and write one content-addressable receipt.

    Sequential on purpose: the task endpoint is the single-slot box that also serves live Triage, and
    `--max-model-cases` is required for the same reason it is on the baseline (#150).
    """

    if len(episodes) > max_model_cases:
        raise ValueError("news_learning_migration_exceeds_model_case_budget")
    per_case: list[dict[str, Any]] = []
    counts = {"equivalent": 0, "divergent": 0, "error": 0}
    with dspy.context(lm=lm, adapter=DspyStrictJSONAdapter(use_native_function_calling=False)):
        for episode in episodes:
            recorded = episode.production_judgment.verdict if episode.production_judgment is not None else None
            entry: dict[str, Any] = {"case_id": episode.case_id, "cluster_id": episode.cluster_id}
            if recorded is None:
                entry.update(verdict="error", field_diffs=[], judge_status="no_recorded_judgment")
            else:
                try:
                    example = build_compile_example(episode)
                    prediction = program(**example.inputs())
                    replayed = (
                        _extract_verdict(prediction) if verdict_extractor is None else verdict_extractor(prediction)
                    )
                except Exception as exc:
                    entry.update(verdict="error", field_diffs=[], judge_status=f"replay_error:{type(exc).__name__}")
                    replayed = None
                if replayed is not None:
                    try:
                        entry.update(assess_replayed_case(recorded.model_dump(mode="json"), dict(replayed), judge))
                    except Exception as exc:  # a malformed historical verdict must cost one case, not the run
                        entry.update(verdict="error", field_diffs=[], judge_status=f"assess_error:{type(exc).__name__}")
                elif "verdict" not in entry:
                    entry.update(verdict="error", field_diffs=[], judge_status="replay_verdict_missing")
            counts[str(entry["verdict"])] += 1
            per_case.append(entry)
    receipt = {
        "schema": MIGRATION_RECEIPT_SCHEMA,
        "from_dataset_sha": from_dataset_sha,
        "replay_identity": dict(replay_identity),
        "judge": {**judge.identity, **judge.stats},
        "counts": counts,
        "per_case": per_case,
    }
    receipt["receipt_sha256"] = canonical_sha({key: receipt[key] for key in receipt if key != "receipt_sha256"})
    return receipt


def _extract_verdict(prediction: Any) -> Mapping[str, Any] | None:
    value = prediction.get("verdict") if hasattr(prediction, "get") else getattr(prediction, "verdict", None)
    if value is None:
        return None
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


__all__ = [
    "MIGRATION_RECEIPT_SCHEMA",
    "assess_replayed_case",
    "run_corpus_migration",
    "verdict_field_diffs",
]
