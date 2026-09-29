"""Archived #742 reader baseline and current #750 claim-scoped gold replay.

Every fixture row is one claim at its decision stamp: the persisted semantic links and receipts it could
reach (from which `reader_novelty` is recomputed), the receipts behind its messages, and the frozen
`ReaderInput` production asks and caches:
  reader_replay          labelled claims (keep / borderline / demote) with stratum weights
  anchor_labeled         unlinked claims with 16 recalled messages and a blind "already reported the core fact"
                         label (stratified on the native anchor answer; the weights restore the day)
  coverage_labeled       claims with 16 recalled messages and a blind "already fully said" label
  reader_nvidia_buyback  every decision of the seven Nvidia buyback Events (<= 2 pushes expected)
  reader_spacex_starship every decision of the Starship first orbital flight Events

Recorded mode scores archived answers only against archived inputs; it never passes the old contract to a
current judge. `--issue-750-gold` replays candidate selection under the current contract, and `--live`
with that flag asks the configured judge again. No sender, broker or database is constructed.

Reports: incremental importance AUC; a decision table (claims/day estimated with the stratum weights,
precision of keep+borderline, keep recall, pushes by novelty); anchor recall and false-anchor rate against the
core-fact and the fully-said labels; pushes per replayed cluster.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

from tracefold.news.updates.contracts import Claim, EventUpdate
from tracefold.news.updates.reader_judgments import (
    READER_CUTS,
    READER_QUESTIONS_IDENTITY,
    AnchorEvidence,
    ClaimLink,
    ImportanceEvidence,
    LinkedReceipt,
    ReaderBackend,
    ReaderCuts,
    ReaderDecision,
    ReaderInput,
    ReaderJudge,
    ReaderJudgment,
    ReaderNovelty,
    message_id,
    reader_decision,
    reader_novelty,
)
from tracefold.news.updates.receipt_recall import RecallCandidate, query_for_claim, select_for_claim

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/news"
READER_REPLAY = FIXTURES / "reader_replay_2026-09-28.jsonl.gz"
ANCHOR_LABELED = FIXTURES / "anchor_labeled_2026-09-28.jsonl.gz"
COVERAGE_LABELED = FIXTURES / "coverage_labeled_2026-09-28.jsonl.gz"
CLUSTERS = (
    FIXTURES / "reader_nvidia_buyback_2026-09-28.jsonl",
    FIXTURES / "reader_spacex_starship_2026-09-28.jsonl",
)
ISSUE_750_GOLD = FIXTURES / "issue_750_gold_recall.json"
ISSUE_750_SAMPLE = FIXTURES / "issue_750_reader_replay.jsonl.gz"
ISSUE_750_CLUSTERS = FIXTURES / "issue_750_clusters.jsonl.gz"
VERDICTS = ("keep", "borderline", "demote")
CUT_TABLE = (2.0, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9)
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


async def ask(judge: ReaderJudge, rows: Sequence[Mapping[str, Any]], *, seconds: float = 60.0) -> dict[str, Any]:
    from tracefold.news.updates.judgment import Budget

    answers: dict[str, ReaderJudgment] = {}
    failures: Counter[str] = Counter()
    latencies = []
    for row in rows:
        started = perf_counter()
        try:
            judgment = await judge.judge(row["reader"], Budget.start(seconds))
        except Exception as exc:
            failures[type(exc).__name__] += 1
            latencies.append(round((perf_counter() - started) * 1000))
            continue
        latencies.append(round((perf_counter() - started) * 1000))
        if judgment.status == "available":
            answers[row["case_id"]] = judgment
        else:
            failures[str(judgment.error_code)] += 1
    latencies.sort()
    return {
        "answers": answers,
        "failures": dict(failures),
        "latency_ms": {
            "p50": latencies[len(latencies) // 2] if latencies else None,
            "p90": latencies[int(len(latencies) * 0.9)] if latencies else None,
        },
    }


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


def decision_table(
    rows: Sequence[Mapping[str, Any]], answers: Mapping[str, ReaderJudgment], *, cuts: Iterable[float] = CUT_TABLE
) -> list[dict[str, Any]]:
    """Weighted by each row's stratum weight, so a stratified sample estimates one day's population."""

    labelled = [row for row in rows if row["case_id"] in answers and row.get("label")]
    keep_weight = sum(row["weight"] for row in labelled if row["label"]["verdict"] == "keep")
    table = []
    for cut in cuts:
        outcomes = {}
        for row in labelled:
            own = answers[row["case_id"]].cuts
            recut = ReaderCuts(push=cut, key=max(cut, own.key), anchor_none_below=own.anchor_none_below)
            outcomes[row["case_id"]] = decision(row, answers[row["case_id"]], recut).outcome
        pushed = [row for row in labelled if outcomes[row["case_id"]] in PUSHED]
        weight = sum(row["weight"] for row in pushed)
        kept = sum(row["weight"] for row in pushed if row["label"]["verdict"] == "keep")
        border = sum(row["weight"] for row in pushed if row["label"]["verdict"] == "borderline")
        table.append(
            {
                "cut": cut,
                "claims_per_day": round(weight),
                "precision_keep_borderline": round((kept + border) / weight, 3) if weight else None,
                "keep_recall": round(kept / keep_weight, 3) if keep_weight else None,
                "pushed_by_novelty": dict(Counter(row["reader_novelty"].novelty for row in pushed)),
            }
        )
    return table


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
        "decision_table": decision_table(replay, answers),
        "anchor": {"core_fact": anchor_report(anchors, answers), "fully_said": anchor_report(coverage, answers)},
        "clusters": [cluster_report(rows, answers) for rows in clusters],
    }


def load_all() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[list[dict[str, Any]]]]:
    return load(READER_REPLAY), load(ANCHOR_LABELED), load(COVERAGE_LABELED), [load(path) for path in CLUSTERS]


def issue_750_gold() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Replay the frozen candidate union and expose the exact new reader inputs for re-asking."""

    fixture = json.loads(ISSUE_750_GOLD.read_text("utf-8"))
    update = EventUpdate.model_validate(fixture["update"])
    candidates = tuple(
        RecallCandidate(
            intent_id=row["intent_id"],
            body=row["body"],
            payload_sha256=row["payload_sha256"],
            settled_at_ms=row["settled_at_ms"],
            claims=tuple(Claim.model_validate(claim) for claim in row["claims"]),
        )
        for row in fixture["candidates"]
    )
    links = tuple(ClaimLink.model_validate(row) for row in fixture["links"])
    receipts = tuple(LinkedReceipt.model_validate(row) for row in fixture["link_receipts"])
    bodies = {candidate.intent_id: candidate.body for candidate in candidates}
    inputs = []
    report: dict[str, Any] = {"event_id": fixture["event_id"], "as_of_ms": fixture["as_of_ms"], "claims": []}
    for claim in update.claims:
        novelty = reader_novelty(claim.ref, links, receipts)
        selection = select_for_claim(
            query_for_claim(claim),
            novelty,
            candidates,
            as_of_ms=fixture["as_of_ms"],
            route_ranks=fixture["route_ranks"][claim.ref],
        )
        reader = ReaderInput.of(claim, update, [bodies[intent] for intent in selection.intent_ids])
        baseline = fixture["baseline_message_intents"][claim.ref]
        labels = fixture["labels"]
        report["claims"].append(
            {
                "claim_ref": claim.ref,
                "baseline_messages": len(baseline),
                "new_messages": len(selection.intent_ids),
                "baseline_label2_hit": sum(labels.get(intent[7:13]) == 2 for intent in baseline),
                "new_label2_hit": sum(labels.get(intent[7:13]) == 2 for intent in selection.intent_ids),
                "baseline_label0": sum(labels.get(intent[7:13]) == 0 for intent in baseline),
                "new_label0": sum(labels.get(intent[7:13]) == 0 for intent in selection.intent_ids),
                "new_input_digest": reader.digest,
            }
        )
        inputs.append({"case_id": claim.ref, "reader": reader})
    report["labels"] = {
        "direct_foreground": sum(value == 2 for value in fixture["labels"].values()),
        "noise": sum(value == 0 for value in fixture["labels"].values()),
    }
    return report, inputs


def issue_750_sample() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate the frozen SQL candidate pool with the pure selector and load new v2 model inputs."""

    with gzip.open(ISSUE_750_SAMPLE, "rt", encoding="utf-8") as source:
        rows = [json.loads(line) for line in source]
    if not rows or len({row["event_id"] for row in rows}) != len(rows):
        raise ValueError("news_reader_eval_sample_events_not_independent")
    result = []
    for row in rows:
        reader = ReaderInput.model_validate(row["reader_input"])
        candidates = tuple(
            RecallCandidate(
                intent_id=item["intent_id"],
                body=item["body"],
                payload_sha256=item["payload_sha256"],
                settled_at_ms=item["settled_at_ms"],
                claims=tuple(Claim.model_validate(claim) for claim in item["claims"]),
            )
            for item in row["candidates"]
        )
        novelty = reader_novelty(
            row["claim_ref"],
            tuple(ClaimLink.model_validate(link) for link in row["links"]),
            tuple(LinkedReceipt.model_validate(receipt) for receipt in row["link_receipts"]),
        )
        # Keep the adopted identity beside the model input for SQL route replay.
        claim = Claim.model_validate(row["current_claim"])
        selection = select_for_claim(
            query_for_claim(claim),
            novelty,
            candidates,
            as_of_ms=row["as_of_ms"],
            route_ranks=row["route_ranks"],
        )
        if list(selection.intent_ids) != row["message_intents"] or reader.digest != row["input_digest"]:
            raise ValueError(f"news_reader_eval_sample_drift:{row['case_id']}")
        result.append({**row, "reader": reader, "reader_novelty": novelty})
    lengths = sorted(len(row["message_intents"]) for row in result)
    baseline_lengths = sorted(int(row["baseline_messages"]) for row in result)
    report = {
        "cases": len(result),
        "independent_label_verdicts": dict(Counter(row["label"]["verdict"] for row in result)),
        "novelty": dict(Counter(row["reader_novelty"].novelty for row in result)),
        "candidate_pool": {
            "p50": sorted(len(row["candidates"]) for row in result)[len(result) // 2],
            "max": max(len(row["candidates"]) for row in result),
        },
        "messages": {
            "baseline_p50": baseline_lengths[len(result) // 2],
            "new_p50": lengths[len(result) // 2],
            "new_p95": lengths[int(len(result) * 0.95)],
            "new_zero": lengths.count(0),
            "new_sixteen": lengths.count(16),
            "new_body_chars": sum(sum(len(text) for text in row["reader"].messages) for row in result),
        },
    }
    return report, result


def issue_750_sample_answers(
    rows: Sequence[Mapping[str, Any]], run: Mapping[str, Any], backend: ReaderBackend
) -> dict[str, Any]:
    answers = run["answers"]
    scoped = [row for row in rows if row["case_id"] in answers]
    old_values = {
        row["case_id"]: row["baseline_answers"][backend]["importance"]["value"]
        for row in scoped
        if backend in row["baseline_answers"]
    }
    old_rows = [row for row in scoped if row["case_id"] in old_values]
    old_positive = [old_values[row["case_id"]] for row in old_rows if row["label"]["verdict"] != "demote"]
    old_negative = [old_values[row["case_id"]] for row in old_rows if row["label"]["verdict"] == "demote"]
    outcomes = {row["case_id"]: decision(row, answers[row["case_id"]]).outcome for row in scoped}
    return {
        "backend": backend,
        "answered": len(scoped),
        "failures": run["failures"],
        "latency_ms": run["latency_ms"],
        "importance": importance_report(scoped, answers),
        "baseline_importance_auc_keep_borderline_vs_demote_same_cases": auc(old_positive, old_negative),
        "outcomes": dict(Counter(outcomes.values())),
        "pushed_by_label": dict(
            Counter(row["label"]["verdict"] for row in scoped if outcomes[row["case_id"]] in PUSHED)
        ),
        "answer_input_digests": {row["case_id"]: row["input_digest"] for row in scoped},
        "answers": {key: answer_record(value) for key, value in answers.items()},
    }


def merge_issue_750_sample_runs(
    paths: Sequence[Path], backend: ReaderBackend, rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Join disjoint live batches without reusing old-contract answers or repeating model calls."""

    answers: dict[str, ReaderJudgment] = {}
    expected_digests = {row["case_id"]: row["input_digest"] for row in rows}
    failures: Counter[str] = Counter()
    batches = []
    for path in paths:
        result = json.loads(path.read_text("utf-8"))["live"]
        if result["backend"] != backend:
            raise ValueError("news_reader_eval_merge_backend_mismatch")
        for case_id, value in result["answers"].items():
            if case_id in answers:
                raise ValueError("news_reader_eval_merge_duplicate_case")
            if result["answer_input_digests"].get(case_id) != expected_digests.get(case_id):
                raise ValueError(f"news_reader_eval_merge_input_drift:{case_id}")
            answers[case_id] = ReaderJudgment(
                status="available",
                backend=backend,
                identity=f"reasked:{backend}",
                importance=ImportanceEvidence.model_validate(value["importance"]),
                anchor=AnchorEvidence.model_validate(value["anchor"]) if "anchor" in value else None,
            )
        failures.update(result["failures"])
        batches.append({"cases": result["answered"], **result["latency_ms"]})
    return {"answers": answers, "failures": dict(failures), "latency_ms": {"batches": batches}}


def issue_750_clusters() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with gzip.open(ISSUE_750_CLUSTERS, "rt", encoding="utf-8") as source:
        rows = [json.loads(line) for line in source]
    if len(rows) != 26 or len({row["case_id"] for row in rows}) != 26:
        raise ValueError("news_reader_cluster_cases_missing")
    judged = []
    for row in rows:
        if row.get("deterministic_outcome"):
            continue
        reader = ReaderInput.model_validate(row["reader_input"])
        if reader.digest != row["input_digest"] or len(reader.messages) != len(row["message_intents"]):
            raise ValueError(f"news_reader_cluster_input_drift:{row['case_id']}")
        judged.append({**row, "reader": reader, "reader_novelty": ReaderNovelty.model_validate(row["novelty"])})
    report = {
        "cases": len(rows),
        "judged": len(judged),
        "deterministic": [row["case_id"] for row in rows if row.get("deterministic_outcome")],
        "clusters": dict(Counter(row["cluster"] for row in rows)),
    }
    return report, judged


def issue_750_cluster_answers(
    rows: Sequence[Mapping[str, Any]], answers: Mapping[str, ReaderJudgment], backend: ReaderBackend
) -> dict[str, Any]:
    with gzip.open(ISSUE_750_CLUSTERS, "rt", encoding="utf-8") as source:
        all_rows = [json.loads(line) for line in source]
    by_id = {row["case_id"]: row for row in rows}
    clusters: dict[str, dict[str, Any]] = {}
    for cluster in ("nvidia", "spacex"):
        sequence = [row for row in all_rows if row["cluster"] == cluster]
        outcomes = []
        for row in sequence:
            case_id = row["case_id"]
            if row.get("deterministic_outcome"):
                outcome = row["deterministic_outcome"]
            elif case_id in answers:
                outcome = decision(by_id[case_id], answers[case_id]).outcome
            else:
                outcome = "unanswered"
            outcomes.append({"case_id": case_id, "outcome": outcome})
        clusters[cluster] = {
            "cases": len(sequence),
            "outcomes": outcomes,
            "pushed": [row["case_id"] for row in outcomes if row["outcome"] in PUSHED],
            "unanswered": sum(row["outcome"] == "unanswered" for row in outcomes),
        }
    return {
        "backend": backend,
        "answered": len(answers),
        "clusters": clusters,
        "answer_input_digests": {case_id: by_id[case_id]["input_digest"] for case_id in answers},
        "answers": {key: answer_record(value) for key, value in answers.items()},
    }


async def _live(
    rows: list[dict[str, Any]], *, backend: ReaderBackend | None = None
) -> tuple[ReaderBackend, dict[str, Any]]:
    from tracefold.app.learning_runtime import compose_news_models, news_reader_judgment_endpoint
    from tracefold.app.news_updates import compose_reader_judge
    from tracefold.platform.config.loader import load_settings

    settings = load_settings(require_ws_token=False)
    models = compose_news_models(settings)
    if models is None:
        raise ValueError("news_reader_eval_models_not_configured")
    judge, connection = compose_reader_judge(
        generated_lm_factory=models.judgment.lms,
        generated_model_identity=models.judgment.identity,
        reader_judgment=None if backend == "generated" else news_reader_judgment_endpoint(settings),
    )
    try:
        return ("native" if connection is not None else "generated"), await ask(judge, rows)
    finally:
        if connection is not None:
            await connection.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=("native", "generated"), default="native")
    parser.add_argument("--live", action="store_true", help="ask the configured reader judge (network, cost)")
    parser.add_argument("--issue-750-gold", action="store_true", help="replay current gold candidates and input")
    parser.add_argument("--issue-750-sample", action="store_true", help="replay 120 independently labelled new inputs")
    parser.add_argument("--issue-750-clusters", action="store_true", help="replay Nvidia and SpaceX event sequences")
    parser.add_argument("--limit", type=int, help="limit live sample calls, preserving deterministic fixture order")
    parser.add_argument("--offset", type=int, default=0, help="start a live sample batch at this row")
    parser.add_argument("--merge-eval", type=Path, action="append", help="merge disjoint new-input live reports")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.issue_750_clusters:
        report, rows = issue_750_clusters()
        if args.live:
            backend, run = asyncio.run(_live(rows, backend=args.backend))
            report["live"] = issue_750_cluster_answers(rows, run["answers"], backend)
            report["live"]["failures"] = run["failures"]
            report["live"]["latency_ms"] = run["latency_ms"]
    elif args.issue_750_sample:
        report, rows = issue_750_sample()
        if args.live:
            end = None if args.limit is None else args.offset + args.limit
            backend, run = asyncio.run(_live(rows[args.offset : end], backend=args.backend))
            report["live"] = issue_750_sample_answers(rows, run, backend)
        elif args.merge_eval:
            report["live"] = issue_750_sample_answers(
                rows, merge_issue_750_sample_runs(args.merge_eval, args.backend, rows), args.backend
            )
    elif args.issue_750_gold:
        report, inputs = issue_750_gold()
        if args.live:
            backend, run = asyncio.run(_live(inputs, backend=args.backend))
            report["backend"] = backend
            report["answers"] = {key: answer_record(value) for key, value in run["answers"].items()}
            report["failures"], report["latency_ms"] = run["failures"], run["latency_ms"]
    else:
        if args.live:
            raise ValueError("news_reader_eval_current_candidate_fixture_required")
        replay, anchors, coverage, clusters = load_all()
        everything = [*replay, *anchors, *coverage, *(row for rows in clusters for row in rows)]
        report = evaluate(replay, anchors, coverage, clusters, recorded(everything, args.backend), args.backend)
        report["contract"] = "archived_news_reader_input_v1_baseline"
    if args.issue_750_gold or args.issue_750_sample or args.issue_750_clusters:
        report["reader_questions_identity"] = READER_QUESTIONS_IDENTITY
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
