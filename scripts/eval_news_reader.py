"""Offline replay of the News reader decision (#742) over frozen inputs and independent labels.

Every fixture row is one claim at its decision stamp: the persisted semantic links and receipts it could
reach (from which `reader_novelty` is recomputed), the receipts behind its messages, and the frozen
`ReaderInput` production asks and caches:
  reader_replay          labelled claims (keep / borderline / demote) with stratum weights
  coverage_labeled       claims with 16 recalled messages and an independent "already fully said" label
  reader_nvidia_buyback  every decision of the seven Nvidia buyback Events (<= 2 pushes expected)
  reader_spacex_starship every decision of the Starship first orbital flight Events

Recorded mode (default) scores the answers stored in the fixtures for one backend. `--live` asks the
configured reader judge the same inputs: System One when `llm.news_reader_judgment` is configured, else
the generative News route. No sender, broker or database is constructed.

Reports: incremental importance AUC; a decision table (claims/day estimated with the stratum weights,
precision of keep+borderline, keep recall, pushes by novelty); anchor recall and false-anchor rate; pushes
per replayed cluster.
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

from tracefold.news.updates.reader_judgments import (
    READER_CUTS,
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

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/news"
READER_REPLAY = FIXTURES / "reader_replay_2026-09-28.jsonl.gz"
COVERAGE_LABELED = FIXTURES / "coverage_labeled_2026-09-28.jsonl.gz"
CLUSTERS = (
    FIXTURES / "reader_nvidia_buyback_2026-09-28.jsonl",
    FIXTURES / "reader_spacex_starship_2026-09-28.jsonl",
)
VERDICTS = ("keep", "borderline", "demote")
CUT_TABLE = (2.0, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9)
NONE_THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7)
PUSHED = ("correction", "key", "push")


def load(path: Path) -> list[dict[str, Any]]:
    text = gzip.decompress(path.read_bytes()).decode("utf-8") if path.suffix == ".gz" else path.read_text("utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not rows:
        raise ValueError("news_reader_eval_cases_required")
    for row in rows:
        # Refuse a fixture the current projection and novelty rules would not reproduce.
        row["reader"] = ReaderInput.model_validate(row["reader_input"])
        if len(row["message_intents"]) != len(row["reader"].messages):
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
        if not judgment.matches(row["reader"]):
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
        judgment = await judge.judge(row["reader"], Budget.start(seconds))
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


def anchor_report(
    rows: Sequence[Mapping[str, Any]],
    answers: Mapping[str, ReaderJudgment],
    *,
    thresholds: Iterable[float] = NONE_THRESHOLDS,
) -> list[dict[str, Any]]:
    """Against "already fully said" labels: a fully covered claim has an anchor (recall), and a claim no message
    even partly covered has none (false anchors). Partly covered claims are not scored."""

    judged = [row for row in rows if row["case_id"] in answers and answers[row["case_id"]].anchor is not None]
    report = []
    for threshold in thresholds:
        counts: Counter[str] = Counter()
        for row in judged:
            anchor = answers[row["case_id"]].anchor
            index = anchor.anchor(ReaderCuts(push=0, key=0, anchor_none_below=threshold))  # type: ignore[union-attr]
            truth = row["label"]["covered_by"]
            if truth != "none":
                counts["covered"] += 1
                counts["anchored"] += index is not None
                counts["same_message"] += index is not None and message_id(index) == truth
            elif not row["label"]["partial"]:
                counts["unrelated"] += 1
                counts["false_anchor"] += index is not None
        report.append(
            {
                "none_below": threshold,
                "anchor_recall": round(counts["anchored"] / counts["covered"], 3) if counts["covered"] else None,
                "false_anchor_rate": round(counts["false_anchor"] / counts["unrelated"], 3)
                if counts["unrelated"]
                else None,
                "same_message": counts["same_message"],
                "covered": counts["covered"],
                "unrelated": counts["unrelated"],
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
            "coverage": sum(row["case_id"] in answers for row in coverage),
        },
        "importance": importance_report(replay, answers),
        "decision_table": decision_table(replay, answers),
        "anchor": anchor_report(coverage, answers),
        "clusters": [cluster_report(rows, answers) for rows in clusters],
    }


def load_all() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[list[dict[str, Any]]]]:
    return load(READER_REPLAY), load(COVERAGE_LABELED), [load(path) for path in CLUSTERS]


async def _live(rows: list[dict[str, Any]]) -> tuple[ReaderBackend, dict[str, Any]]:
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
        reader_judgment=news_reader_judgment_endpoint(settings),
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
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    replay, coverage, clusters = load_all()
    everything = [*replay, *coverage, *(row for rows in clusters for row in rows)]
    if args.live:
        backend, run = asyncio.run(_live(everything))
        report = evaluate(replay, coverage, clusters, run["answers"], backend)
        report["failures"], report["latency_ms"] = run["failures"], run["latency_ms"]
    else:
        report = evaluate(replay, coverage, clusters, recorded(everything, args.backend), args.backend)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
