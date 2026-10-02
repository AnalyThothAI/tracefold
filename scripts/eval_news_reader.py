"""Offline current-v3 News reader evaluation, including independent cut fitting.

Archived public inputs are physically converted to the sole current contract; old
scores remain arithmetic baselines. Fresh #791 model reasks and blind labels are
required to calibrate a new rubric. No sender, broker, store or cache is constructed.
Reports cover expected/P(3)+P(4)/P(4) AUC, push/held/tail grids, Event volumes,
official story recall, anchor quality and sequential cluster counts.
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt, ReaderNovelty, reader_novelty
from tracefold.news.notifications.policy import (
    READER_CUTS,
    ReaderCuts,
    ReaderDecision,
    anchor_index,
    cuts_for,
    reader_decision,
)
from tracefold.news.notifications.reader import (
    AnchorEvidence,
    ImportanceEvidence,
    ReaderBackend,
    ReaderInput,
    ReaderJudgment,
    message_id,
)
from tracefold.news.updates.contracts import ClaimFields
from tracefold.news.updates.identity import digest

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
HELD_TABLE = (2.7, 2.8, 2.9, 2.98, 3.0, 3.05, 3.1)
TAIL_TABLE = (0.02, 0.03, 0.05, 0.08, 0.1, 0.15)
OFFICIAL_ROLES = frozenset(
    {
        "head_of_state_or_government",
        "central_bank_policymaker",
        "economic_policy_official",
        "foreign_or_defense_official",
    }
)
NONE_THRESHOLDS = (0.1, 0.2, 0.3, 0.4, 0.6, 0.8)
PUSHED = ("correction", "key", "push")


def load(path: Path) -> list[dict[str, Any]]:
    text = gzip.decompress(path.read_bytes()).decode("utf-8") if path.suffix == ".gz" else path.read_text("utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not rows:
        raise ValueError("news_reader_eval_cases_required")
    for row in rows:
        ReaderInput.model_validate(row["reader_input"])
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
        row.setdefault("weight", 1.0)
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
        claim_fields=ClaimFields.model_validate(row["reader_input"]["claim"]["fields"]),
    )


def importance_report(rows: Sequence[Mapping[str, Any]], answers: Mapping[str, ReaderJudgment]) -> dict[str, Any]:
    labelled = [row for row in rows if row["case_id"] in answers and row.get("label")]
    measures = {
        "expected": lambda evidence: evidence.value,
        "tail_3_4": lambda evidence: sum(evidence.probabilities[3:]),
        "tail_4": lambda evidence: evidence.probabilities[4],
    }
    reports = {}
    for name, score in measures.items():

        def compare(subset: Sequence[Mapping[str, Any]], positive: set[str], score: Any = score) -> float | None:
            positive_values, negative_values = [], []
            for row in subset:
                evidence = answers[row["case_id"]].importance
                if evidence is None:
                    continue
                if row["label"]["verdict"] in positive:
                    positive_values.append(score(evidence))
                elif row["label"]["verdict"] == "demote":
                    negative_values.append(score(evidence))
            return auc(positive_values, negative_values)

        reports[name] = {
            "auc_keep_borderline_vs_demote": compare(labelled, {"keep", "borderline"}),
            "auc_keep_vs_demote": compare(labelled, {"keep"}),
            "official_auc_keep_vs_demote": compare([row for row in labelled if is_official(row)], {"keep"}),
        }
    return {
        "cases": len(labelled),
        "labels": dict(Counter(row["label"]["verdict"] for row in labelled)),
        **reports["expected"],
        "statistics": reports,
        "novelty": dict(Counter(row["reader_novelty"].novelty for row in labelled)),
    }


def is_official(row: Mapping[str, Any]) -> bool:
    return row["reader_input"]["claim"]["fields"].get("actor_role") in OFFICIAL_ROLES


def decision_row(
    rows: Sequence[Mapping[str, Any]],
    answers: Mapping[str, ReaderJudgment],
    *,
    push: float,
    held: float,
    key_tail: float,
) -> dict[str, Any]:
    """One cut triple; event volume collapses sibling claims, stratum weights estimate one day."""
    labelled = [row for row in rows if row["case_id"] in answers and row.get("label")]
    outcomes = {}
    for row in labelled:
        own = cuts_for(answers[row["case_id"]])
        recut = ReaderCuts(push=push, held=held, key_tail=key_tail, anchor_none_below=own.anchor_none_below)
        outcomes[row["case_id"]] = decision(row, answers[row["case_id"]], recut).outcome
    pushed = [row for row in labelled if outcomes[row["case_id"]] in PUSHED]
    keys = [row for row in pushed if outcomes[row["case_id"]] == "key"]
    weight = sum(row["weight"] for row in pushed)
    kept = sum(row["weight"] for row in pushed if row["label"]["verdict"] == "keep")
    border = sum(row["weight"] for row in pushed if row["label"]["verdict"] == "borderline")
    keep_weight = sum(row["weight"] for row in labelled if row["label"]["verdict"] == "keep")
    official_keep = [row for row in labelled if is_official(row) and row["label"]["verdict"] == "keep"]
    official_demote = [row for row in labelled if is_official(row) and row["label"]["verdict"] == "demote"]

    def rate(subset: Sequence[Mapping[str, Any]]) -> float | None:
        total = sum(row["weight"] for row in subset)
        return (
            round(sum(row["weight"] for row in subset if outcomes[row["case_id"]] in PUSHED) / total, 3)
            if total
            else None
        )

    events: dict[str, float] = {}
    key_events: dict[str, float] = {}
    for row in pushed:
        event = str(row["event_id"])
        events[event] = max(events.get(event, 0), row["weight"])
        if outcomes[row["case_id"]] == "key":
            key_events[event] = max(key_events.get(event, 0), row["weight"])
    key_weight = sum(row["weight"] for row in keys)
    return {
        "claims_per_day": round(weight),
        "key_per_day": round(key_weight),
        "events_per_day": round(sum(events.values())),
        "key_events_per_day": round(sum(key_events.values())),
        "precision_keep_borderline": round((kept + border) / weight, 3) if weight else None,
        "keep_recall": round(kept / keep_weight, 3) if keep_weight else None,
        "official_keep_recall": rate(official_keep),
        "official_demote_push_rate": rate(official_demote),
        "official_keep_stories_pushed": len(
            {
                row["label"].get("story_id", row["event_id"])
                for row in official_keep
                if outcomes[row["case_id"]] in PUSHED
            }
        ),
        "key_precision": round(sum(row["weight"] for row in keys if row["label"].get("key", False)) / key_weight, 3)
        if key_weight
        else None,
        "key_demote_cases": [row["case_id"] for row in keys if row["label"]["verdict"] == "demote"],
        "pushed_by_novelty": dict(Counter(row["reader_novelty"].novelty for row in pushed)),
        "story_push_counts": dict(Counter(row["label"].get("story_id", row["event_id"]) for row in pushed)),
    }


def decision_table(
    rows: Sequence[Mapping[str, Any]],
    answers: Mapping[str, ReaderJudgment],
    cuts: ReaderCuts,
    *,
    push_cuts: Iterable[float] = CUT_TABLE,
    held_cuts: Iterable[float] = HELD_TABLE,
    tail_cuts: Iterable[float] = TAIL_TABLE,
) -> dict[str, list[dict[str, Any]]]:
    return {
        "push": [
            {"cut": cut, **decision_row(rows, answers, push=cut, held=cuts.held, key_tail=cuts.key_tail)}
            for cut in push_cuts
        ],
        "held": [
            {"cut": cut, **decision_row(rows, answers, push=cuts.push, held=cut, key_tail=cuts.key_tail)}
            for cut in held_cuts
        ],
        "key_tail": [
            {"cut": cut, **decision_row(rows, answers, push=cuts.push, held=cuts.held, key_tail=cut)}
            for cut in tail_cuts
        ],
    }


def fit_cuts(rows: Sequence[Mapping[str, Any]], answers: Mapping[str, ReaderJudgment]) -> dict[str, Any]:
    """Fit native and generated independently; refuse a passing claim when the labelled gates fail."""
    table = []
    for push in (2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6):
        for held in (2.7, 2.8, 2.9, 3.0, 3.1):
            for tail in TAIL_TABLE:
                metrics = decision_row(rows, answers, push=push, held=held, key_tail=tail)
                table.append({"push": push, "held": held, "key_tail": tail, **metrics})
    official_auc = importance_report(rows, answers)["official_auc_keep_vs_demote"]
    passing = [
        row
        for row in table
        if official_auc is not None
        and official_auc >= 0.75
        and row["official_keep_recall"] is not None
        and row["official_keep_recall"] >= 0.70
        and row["official_demote_push_rate"] is not None
        and row["official_demote_push_rate"] <= 0.15
        and row["keep_recall"] is not None
        and row["keep_recall"] >= 0.70
        and row["precision_keep_borderline"] is not None
        and row["precision_keep_borderline"] >= 0.52
        and row["key_precision"] is not None
        and row["key_precision"] >= 0.5
        and not row["key_demote_cases"]
        and 300 <= row["events_per_day"] <= 500
    ]
    chosen = (
        max(passing, key=lambda row: (row["keep_recall"], row["precision_keep_borderline"], -row["events_per_day"]))
        if passing
        else None
    )
    return {
        "dataset_sha256": digest([{k: v for k, v in row.items() if k != "reader_novelty"} for row in rows]),
        "rows": len(rows),
        "relabels": sum("relabel" in row.get("label", {}) for row in rows),
        "official_auc_keep_vs_demote": official_auc,
        "selected": chosen,
        "passing_triples": len(passing),
        "table": table,
        "limitations": [
            "A6 and A8 require sequential cluster reasks and independent repeat answers; "
            "aggregate fitting does not prove those gates."
        ],
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
            index = anchor_index(anchor, ReaderCuts(push=0, held=0, key_tail=1, anchor_none_below=threshold))  # type: ignore[arg-type]
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
        "cuts": asdict(cuts),
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
    parser.add_argument("--input", type=Path, help="Current v3 labelled JSONL, or an archived v1/v2 recording.")
    parser.add_argument("--fit", action="store_true", help="Fit cut triples on the supplied labelled dataset.")
    args = parser.parse_args()
    replay, anchors, coverage, clusters = (load(args.input), [], [], []) if args.input else load_all()
    everything = [*replay, *anchors, *coverage, *(row for rows in clusters for row in rows)]
    report = evaluate(replay, anchors, coverage, clusters, recorded(everything, args.backend), args.backend)
    report["contract"] = sorted({row["reader_input"]["schema_version"] for row in everything})
    if args.fit:
        report["calibration"] = fit_cuts(replay, recorded(replay, args.backend))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
