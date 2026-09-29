"""Offline replay of the News reader decision (#742) over archived inputs and independent labels.

Every fixture row is one claim at its decision stamp: the persisted semantic links and receipts it could
reach (from which `reader_novelty` is recomputed), the receipts behind its messages, and the frozen
`news_reader_input_v1` input production asked on 2026-09-28:
  reader_replay          labelled claims (keep / borderline / demote) with stratum weights
  anchor_labeled         unlinked claims with 16 recalled messages and a blind "already reported the core fact"
                         label (stratified on the native anchor answer; the weights restore the day)
  coverage_labeled       claims with 16 recalled messages and a blind "already fully said" label
  reader_nvidia_buyback  every decision of the seven Nvidia buyback Events (<= 2 pushes expected)
  reader_spacex_starship every decision of the Starship first orbital flight Events

The script scores the answers recorded in the fixtures for one backend through the current `reader_decision`
and cuts. The inputs are an archived baseline: the current judge reads `news_reader_input_v2` over
claim-scoped recall (#750), so they are never sent to a model. No sender, broker or database is constructed.

Reports: incremental importance AUC; a decision table over push cuts at the backend's key cut and over key cuts
at its push cut (claims and key claims per day estimated with the stratum weights, precision of
keep+borderline, keep recall, pushes by novelty); anchor recall and false-anchor rate against the core-fact and
the fully-said labels; pushes per replayed cluster.
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from tracefold.news.updates.reader_judgments import (
    READER_CUTS,
    AnchorEvidence,
    ClaimLink,
    ImportanceEvidence,
    LinkedReceipt,
    ReaderBackend,
    ReaderCuts,
    ReaderDecision,
    ReaderJudgment,
    ReaderNovelty,
    message_id,
    reader_decision,
    reader_novelty,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/news"
READER_REPLAY = FIXTURES / "reader_replay_2026-09-28.jsonl.gz"
ANCHOR_LABELED = FIXTURES / "anchor_labeled_2026-09-28.jsonl.gz"
COVERAGE_LABELED = FIXTURES / "coverage_labeled_2026-09-28.jsonl.gz"
CLUSTERS = (
    FIXTURES / "reader_nvidia_buyback_2026-09-28.jsonl",
    FIXTURES / "reader_spacex_starship_2026-09-28.jsonl",
)
VERDICTS = ("keep", "borderline", "demote")
CUT_TABLE = (1.6, 1.7, 1.8, 1.9, 2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8)
KEY_TABLE = (2.5, 2.6, 2.7, 2.8, 2.9, 2.95, 2.98, 3.0, 3.05, 3.1, 3.2)
NONE_THRESHOLDS = (0.1, 0.2, 0.3, 0.4, 0.6, 0.8)
PUSHED = ("correction", "key", "push")


def load(path: Path) -> list[dict[str, Any]]:
    text = gzip.decompress(path.read_bytes()).decode("utf-8") if path.suffix == ".gz" else path.read_text("utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not rows:
        raise ValueError("news_reader_eval_cases_required")
    for row in rows:
        # This is an archived v1 baseline, never a current model input.
        if row["reader_input"]["schema_version"] != "news_reader_input_v1":
            raise ValueError("news_reader_eval_baseline_version_unexpected")
        if len(row["message_intents"]) != len(row["reader_input"]["messages"]):
            raise ValueError("news_reader_eval_message_intents_mismatch")
        novelty = reader_novelty(
            row["claim_ref"],
            [ClaimLink.model_validate(link) for link in row["links"]],
            [LinkedReceipt.model_validate(receipt) for receipt in row["receipts"]],
        )
        if novelty != ReaderNovelty.model_validate(row["novelty"]):
            raise ValueError("news_reader_eval_novelty_drift")
        row["reader_novelty"] = novelty
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("news_reader_eval_duplicate_case")
    return rows


def recorded(rows: Iterable[Mapping[str, Any]], backend: ReaderBackend) -> dict[str, ReaderJudgment]:
    answers = {}
    for row in rows:
        answer = (row.get("answers") or {}).get(backend)
        if answer is None:
            continue
        anchor = answer.get("anchor")
        judgment = ReaderJudgment(
            status="available",
            backend=backend,
            identity=f"recorded:{backend}",
            importance=ImportanceEvidence.model_validate(answer["importance"]),
            anchor=None if anchor is None else AnchorEvidence.model_validate(anchor),
        )
        if (anchor is None) != (len(row["message_intents"]) == 0) or (
            anchor is not None and len(anchor["probabilities"]) - 1 != len(row["message_intents"])
        ):
            raise ValueError("news_reader_eval_answer_shape_mismatch")
        answers[row["case_id"]] = judgment
    return answers


def answer_record(judgment: ReaderJudgment) -> dict[str, Any]:
    """The compact, replayable form a fixture stores for one backend's answer."""

    if judgment.importance is None:
        raise ValueError("news_reader_eval_answer_unavailable")
    importance = judgment.importance
    record: dict[str, Any] = {
        "importance": {
            "value": round(importance.value, 4),
            "probabilities": [round(value, 4) for value in importance.probabilities],
            "confidence": round(importance.confidence, 4),
        }
    }
    if judgment.anchor is not None:
        record["anchor"] = {
            "probabilities": {key: round(value, 4) for key, value in judgment.anchor.probabilities.items()},
            "confidence": round(judgment.anchor.confidence, 4),
        }
    return record


def auc(positive: Sequence[float], negative: Sequence[float]) -> float | None:
    if not positive or not negative:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in positive for n in negative)
    return round(wins / (len(positive) * len(negative)), 3)


def decision(row: Mapping[str, Any], judgment: ReaderJudgment, cuts: ReaderCuts | None = None) -> ReaderDecision:
    return reader_decision(
        row["reader_novelty"],
        judgment,
        first_available_at_ms=row["first_available_at_ms"],
        message_intents=row["message_intents"],
        cuts=cuts,
    )


def importance_report(rows: Sequence[Mapping[str, Any]], answers: Mapping[str, ReaderJudgment]) -> dict[str, Any]:
    labelled = [row for row in rows if row["case_id"] in answers and row.get("label")]
    value = {row["case_id"]: answers[row["case_id"]].importance.value for row in labelled}  # type: ignore[union-attr]
    by_verdict = {
        verdict: [value[row["case_id"]] for row in labelled if row["label"]["verdict"] == verdict]
        for verdict in VERDICTS
    }
    return {
        "cases": len(labelled),
        "labels": {verdict: len(values) for verdict, values in by_verdict.items()},
        "auc_keep_borderline_vs_demote": auc(by_verdict["keep"] + by_verdict["borderline"], by_verdict["demote"]),
        "auc_keep_vs_demote": auc(by_verdict["keep"], by_verdict["demote"]),
        "novelty": dict(Counter(row["reader_novelty"].novelty for row in labelled)),
    }


def decision_row(
    rows: Sequence[Mapping[str, Any]], answers: Mapping[str, ReaderJudgment], *, push: float, key: float
) -> dict[str, Any]:
    """One (push, key) pair, weighted by each row's stratum weight, so a stratified sample estimates one day."""

    labelled = [row for row in rows if row["case_id"] in answers and row.get("label")]
    keep_weight = sum(row["weight"] for row in labelled if row["label"]["verdict"] == "keep")
    outcomes = {}
    for row in labelled:
        own = answers[row["case_id"]].cuts
        recut = ReaderCuts(push=push, key=max(push, key), anchor_none_below=own.anchor_none_below)
        outcomes[row["case_id"]] = decision(row, answers[row["case_id"]], recut).outcome
    pushed = [row for row in labelled if outcomes[row["case_id"]] in PUSHED]
    weight = sum(row["weight"] for row in pushed)
    kept = sum(row["weight"] for row in pushed if row["label"]["verdict"] == "keep")
    border = sum(row["weight"] for row in pushed if row["label"]["verdict"] == "borderline")
    return {
        "claims_per_day": round(weight),
        "key_per_day": round(sum(row["weight"] for row in pushed if outcomes[row["case_id"]] == "key")),
        "precision_keep_borderline": round((kept + border) / weight, 3) if weight else None,
        "keep_recall": round(kept / keep_weight, 3) if keep_weight else None,
        "pushed_by_novelty": dict(Counter(row["reader_novelty"].novelty for row in pushed)),
    }


def decision_table(
    rows: Sequence[Mapping[str, Any]],
    answers: Mapping[str, ReaderJudgment],
    cuts: ReaderCuts,
    *,
    push_cuts: Iterable[float] = CUT_TABLE,
    key_cuts: Iterable[float] = KEY_TABLE,
) -> dict[str, list[dict[str, Any]]]:
    """Push cuts at the backend's key cut, and key cuts at its push cut."""

    return {
        "push": [{"cut": cut, **decision_row(rows, answers, push=cut, key=cuts.key)} for cut in push_cuts],
        "key": [{"cut": cut, **decision_row(rows, answers, push=cuts.push, key=cut)} for cut in key_cuts],
    }


def anchor_truth(row: Mapping[str, Any]) -> str | None:
    """The labelled anchor (`mK` or `none`), or None when the label cannot score the anchor.

    A core-fact label scores every row. A fully-said label implies an anchor where it names a message and no
    anchor only where no message even partly covered the claim; partly covered claims are not scored.
    """

    label = row["label"]
    if "anchor" in label:
        return str(label["anchor"])
    if label["covered_by"] != "none":
        return str(label["covered_by"])
    return None if label["partial"] else "none"


def anchor_report(
    rows: Sequence[Mapping[str, Any]],
    answers: Mapping[str, ReaderJudgment],
    *,
    thresholds: Iterable[float] = NONE_THRESHOLDS,
) -> list[dict[str, Any]]:
    """Anchor recall over labelled anchors and false anchors over labelled none, weighted by stratum weight."""

    judged = [
        row
        for row in rows
        if row["case_id"] in answers and answers[row["case_id"]].anchor is not None and anchor_truth(row) is not None
    ]
    report = []
    for threshold in thresholds:
        counts: Counter[str] = Counter()
        weights: Counter[str] = Counter()
        for row in judged:
            anchor = answers[row["case_id"]].anchor
            index = anchor.anchor(ReaderCuts(push=0, key=0, anchor_none_below=threshold))  # type: ignore[union-attr]
            truth, weight = anchor_truth(row), row.get("weight", 1.0)
            if truth != "none":
                counts["anchors"] += 1
                counts["anchored"] += index is not None
                counts["same_message"] += index is not None and message_id(index) == truth
                weights["anchors"] += weight
                weights["anchored"] += weight * (index is not None)
            else:
                counts["none"] += 1
                counts["false_anchor"] += index is not None
                weights["none"] += weight
                weights["false_anchor"] += weight * (index is not None)
        report.append(
            {
                "none_below": threshold,
                "anchor_recall": round(weights["anchored"] / weights["anchors"], 3) if weights["anchors"] else None,
                "false_anchor_rate": round(weights["false_anchor"] / weights["none"], 3) if weights["none"] else None,
                **counts,
            }
        )
    return report


def cluster_report(rows: Sequence[Mapping[str, Any]], answers: Mapping[str, ReaderJudgment]) -> dict[str, Any]:
    outcomes = {row["case_id"]: decision(row, answers[row["case_id"]]) for row in rows if row["case_id"] in answers}
    return {
        "decisions": len(outcomes),
        "pushed": sorted(case for case, result in outcomes.items() if result.outcome in PUSHED),
        "outcomes": dict(Counter(result.outcome for result in outcomes.values())),
        "novelty": dict(Counter(row["reader_novelty"].novelty for row in rows)),
    }


def evaluate(
    replay: Sequence[Mapping[str, Any]],
    anchors: Sequence[Mapping[str, Any]],
    coverage: Sequence[Mapping[str, Any]],
    clusters: Sequence[Sequence[Mapping[str, Any]]],
    answers: Mapping[str, ReaderJudgment],
    backend: ReaderBackend,
) -> dict[str, Any]:
    cuts = READER_CUTS[backend]
    return {
        "backend": backend,
        "cuts": {"push": cuts.push, "key": cuts.key, "anchor_none_below": cuts.anchor_none_below},
        "answered": {
            "reader_replay": sum(row["case_id"] in answers for row in replay),
            "anchor": sum(row["case_id"] in answers for row in anchors),
            "coverage": sum(row["case_id"] in answers for row in coverage),
        },
        "importance": importance_report(replay, answers),
        "decision_table": decision_table(replay, answers, cuts),
        "anchor": {"core_fact": anchor_report(anchors, answers), "fully_said": anchor_report(coverage, answers)},
        "clusters": [cluster_report(rows, answers) for rows in clusters],
    }


def load_all() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[list[dict[str, Any]]]]:
    return load(READER_REPLAY), load(ANCHOR_LABELED), load(COVERAGE_LABELED), [load(path) for path in CLUSTERS]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=("native", "generated"), default="native")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    replay, anchors, coverage, clusters = load_all()
    everything = [*replay, *anchors, *coverage, *(row for rows in clusters for row in rows)]
    report = evaluate(replay, anchors, coverage, clusters, recorded(everything, args.backend), args.backend)
    report["contract"] = "archived_news_reader_input_v1_baseline"
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
