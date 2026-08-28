"""Carry accepted-corpus cases across an arm change, one verified replay at a time (#300).

An arm identity change invalidates a frozen dataset's recorded-behavior statements, not the human judgment
about evidence: golds and rubric verdicts describe the (evidence -> truth) mapping, which no deploy can
move. This module re-earns the recorded-behavior statements for the *current* arm by replaying it over the
same frozen evidence and carrying forward only the cases where the behavior is verified equivalent on every
axis production reads: the typed verdict, the editorial projection, the card text, and the final action the
frozen policy projection produces. Cases downstream of a diverged delivery are excluded too — their told
ledgers encode a history the current arm would not have produced. Divergent cases are excluded and named;
nothing here weakens a gate, because the product is an ordinary development dataset sealed under the
current cohort.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from ..artifact_identity import canonical_sha
from ..program.contracts import SemanticJudge
from .judge import _SEMANTIC_FIELDS, CardEquivalenceJudge
from .metric import _told_rows
from .objective import DevelopmentEpisode, production_decision

MIGRATION_RECEIPT_SCHEMA = "tracefold.news.corpus_migration_receipt.v1"

# The replay runs the compile route — the production graph on one task endpoint, the same execution scope a
# dataset-bound baseline publishes — not the four-slot production runtime. The receipt names that scope so
# the seal can record it instead of implying a runtime replay that never happened.
REPLAY_SCOPE = "compile_route"

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


def editorial_diffs(recorded: Mapping[str, Any], replayed: Mapping[str, Any]) -> tuple[str, ...]:
    """The editorial projection feeds control classification and scoring, so it gates the carry too."""

    diffs: list[str] = []
    if recorded.get("editorial_origin") != replayed.get("editorial_origin"):
        diffs.append("editorial_origin")
    if _comparable(recorded.get("relevance")) != _comparable(replayed.get("relevance")):
        diffs.append("relevance")
    return tuple(diffs)


def contaminated_case_ids(
    per_case: Sequence[Mapping[str, Any]],
    *,
    told_event_ids_by_case: Mapping[str, Sequence[str]],
    delivered_event_ids_by_case: Mapping[str, str],
) -> dict[str, str]:
    """Cases whose told ledger cites a delivery the current arm was not proven to repeat.

    The exported contexts advanced their told state with the stale arm's recorded deliveries. For carried
    cases that is exactly the history the current arm would have produced; downstream of a diverged
    *delivered* case it is counterfeit, so equivalence measured under it proves nothing.
    """

    diverged_deliveries = {
        delivered_event_ids_by_case[str(entry.get("case_id"))]
        for entry in per_case
        if str(entry.get("verdict")) != "equivalent" and str(entry.get("case_id")) in delivered_event_ids_by_case
    }
    contaminated: dict[str, str] = {}
    for entry in per_case:
        if str(entry.get("verdict")) != "equivalent":
            continue
        case_id = str(entry.get("case_id"))
        cited = set(told_event_ids_by_case.get(case_id) or ()) & diverged_deliveries
        if cited:
            contaminated[case_id] = sorted(cited)[0]
    return contaminated


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
    """One case's verdict-level carry answer: typed fields exactly, text through the equivalence judge."""

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
    program: SemanticJudge,
    judge: CardEquivalenceJudge,
    max_model_cases: int,
    from_dataset_sha: str,
    replay_identity: Mapping[str, Any],
    delivered_event_ids_by_case: Mapping[str, str],
) -> dict[str, Any]:
    """Replay the current arm over every episode and write one content-addressable receipt.

    Sequential on purpose: the task endpoint is the single-slot box that also serves live Triage, and
    `--max-model-cases` is required for the same reason it is on the baseline (#150).
    """

    from ..models import TriageVerdict
    from ..program.contracts import EditorialEnvelope, ScoredJudgment

    if len(episodes) > max_model_cases:
        raise ValueError("news_learning_migration_exceeds_model_case_budget")
    per_case: list[dict[str, Any]] = []
    told_index: dict[str, list[str]] = {}

    async def _replay() -> None:
        for episode in episodes:
            told_index[episode.case_id] = [entry.event_id for entry in episode.context.told.entries]
            recorded = episode.production_judgment
            entry: dict[str, Any] = {"case_id": episode.case_id, "cluster_id": episode.cluster_id}
            if recorded is None:
                entry.update(verdict="error", field_diffs=[], judge_status="no_recorded_judgment")
                per_case.append(entry)
                continue
            try:
                judgment = await program.judge(episode.context)
                replayed_verdict = TriageVerdict.model_validate(judgment.verdict.model_dump(mode="json"))
                replayed_editorial = EditorialEnvelope.model_validate(judgment.editorial.model_dump(mode="json"))
                entry.update(
                    assess_replayed_case(
                        recorded.verdict.model_dump(mode="json"),
                        replayed_verdict.model_dump(mode="json"),
                        judge,
                    )
                )
                if entry["verdict"] == "equivalent":
                    editorial = editorial_diffs(
                        recorded.editorial.model_dump(mode="json"),
                        replayed_editorial.model_dump(mode="json"),
                    )
                    if editorial:
                        entry.update(verdict="divergent", field_diffs=list(editorial))
                if entry["verdict"] == "equivalent":
                    # `verdict.decision` is model intent; the reader-visible action is `decide()` over the
                    # frozen projection, and judge-equivalent-but-not-identical text can still move the
                    # similarity throttles inside it.
                    projection = {
                        **{k: v for k, v in dict(episode.policy_metric).items() if k != "recorded_decision_result"},
                        "told": _told_rows(episode.context),
                    }
                    recorded_final = production_decision(recorded, projection).final
                    replayed_final = production_decision(
                        ScoredJudgment.issue(verdict=replayed_verdict, editorial=replayed_editorial), projection
                    ).final
                    if recorded_final != replayed_final:
                        entry.update(verdict="divergent", field_diffs=["final_action"])
            except Exception as exc:  # one malformed case must cost one error entry, not the run
                entry.update(verdict="error", field_diffs=[], judge_status=f"replay_error:{type(exc).__name__}")
            per_case.append(entry)

    asyncio.run(_replay())
    for case_id, cause in contaminated_case_ids(
        per_case,
        told_event_ids_by_case=told_index,
        delivered_event_ids_by_case=delivered_event_ids_by_case,
    ).items():
        for entry in per_case:
            if entry["case_id"] == case_id:
                entry.update(
                    verdict="divergent", field_diffs=["told_history"], judge_status=f"history_contaminated:{cause}"
                )
    counts = {"equivalent": 0, "divergent": 0, "error": 0}
    for entry in per_case:
        counts[str(entry["verdict"])] += 1
    receipt = {
        "schema": MIGRATION_RECEIPT_SCHEMA,
        "from_dataset_sha": from_dataset_sha,
        "replay_scope": REPLAY_SCOPE,
        "replay_identity": dict(replay_identity),
        "judge": {**judge.identity, **judge.stats},
        "counts": counts,
        "per_case": per_case,
    }
    receipt["receipt_sha256"] = canonical_sha({key: receipt[key] for key in receipt if key != "receipt_sha256"})
    return receipt


__all__ = [
    "MIGRATION_RECEIPT_SCHEMA",
    "REPLAY_SCOPE",
    "assess_replayed_case",
    "contaminated_case_ids",
    "editorial_diffs",
    "run_corpus_migration",
    "verdict_field_diffs",
]
