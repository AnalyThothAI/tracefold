"""Offline replay of the News reader judgment (#742) over frozen inputs and independent labels.

Two fixtures, one frozen `ReaderInput` per row (the same projection production asks and caches):
  reader_replay  labelled claims (keep / borderline / demote) with stratum weights, recall at decision time
  coverage       claims with recalled sent messages and an independent label: covering message or none

Recorded mode (default) scores the answers stored in the fixtures for one backend. `--live` asks the
configured reader judge the same inputs: System One when `llm.news_reader_judgment` is configured, else
the generative News route. No sender, broker or database is constructed.

Reports: importance AUC and per-level labels; a cut table (claims/day estimated with the stratum weights,
precision of keep+borderline, keep recall); coverage recall and false-covered rate per P(none) threshold.
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
    IMPORTANCE_LEVELS,
    READER_CUTS,
    CoverageEvidence,
    ImportanceEvidence,
    ReaderBackend,
    ReaderCuts,
    ReaderInput,
    ReaderJudge,
    ReaderJudgment,
    message_id,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/news"
READER_REPLAY = FIXTURES / "reader_replay_2026-09-28.jsonl.gz"
COVERAGE_LABELED = FIXTURES / "coverage_labeled_2026-09-28.jsonl.gz"
VERDICTS = ("keep", "borderline", "demote")
CUT_TABLE = (2.0, 2.2, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.0)
NONE_THRESHOLDS = (0.5, 0.6, 0.7)


def load(path: Path) -> list[dict[str, Any]]:
    text = gzip.decompress(path.read_bytes()).decode("utf-8") if path.suffix == ".gz" else path.read_text("utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not rows:
        raise ValueError("news_reader_eval_cases_required")
    for row in rows:
        # Refuse a fixture the current projection would not ask.
        row["reader"] = ReaderInput.model_validate(row["reader_input"])
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("news_reader_eval_duplicate_case")
    return rows


def recorded(rows: Iterable[Mapping[str, Any]], backend: ReaderBackend) -> dict[str, ReaderJudgment]:
    answers = {}
    for row in rows:
        answer = (row.get("answers") or {}).get(backend)
        if answer is None:
            continue
        coverage = answer.get("coverage")
        judgment = ReaderJudgment(
            status="available",
            backend=backend,
            identity=f"recorded:{backend}",
            importance=ImportanceEvidence.model_validate(answer["importance"]),
            coverage=None if coverage is None else CoverageEvidence.model_validate(coverage),
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
    if judgment.coverage is not None:
        record["coverage"] = {
            "probabilities": {key: round(value, 4) for key, value in judgment.coverage.probabilities.items()},
            "confidence": round(judgment.coverage.confidence, 4),
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


def covered(judgment: ReaderJudgment, none_below: float) -> int | None:
    if judgment.coverage is None:
        return None
    cuts = judgment.cuts
    return judgment.coverage.covering(ReaderCuts(push=cuts.push, key=cuts.key, covered_none_below=none_below))


def importance_report(rows: Sequence[Mapping[str, Any]], answers: Mapping[str, ReaderJudgment]) -> dict[str, Any]:
    labelled = [row for row in rows if row["case_id"] in answers and row.get("label")]
    value = {row["case_id"]: answers[row["case_id"]].importance.value for row in labelled}  # type: ignore[union-attr]
    by_verdict = {
        verdict: [value[row["case_id"]] for row in labelled if row["label"]["verdict"] == verdict]
        for verdict in VERDICTS
    }
    levels: dict[int, Counter[str]] = {index: Counter() for index in range(len(IMPORTANCE_LEVELS))}
    for row in labelled:
        levels[min(len(IMPORTANCE_LEVELS) - 1, int(value[row["case_id"]] + 0.5))][row["label"]["verdict"]] += 1
    return {
        "cases": len(labelled),
        "labels": {verdict: len(values) for verdict, values in by_verdict.items()},
        "auc_keep_borderline_vs_demote": auc(by_verdict["keep"] + by_verdict["borderline"], by_verdict["demote"]),
        "auc_keep_vs_demote": auc(by_verdict["keep"], by_verdict["demote"]),
        "levels": {str(level): dict(counts) for level, counts in levels.items()},
    }


def cut_table(
    rows: Sequence[Mapping[str, Any]],
    answers: Mapping[str, ReaderJudgment],
    *,
    cuts: Iterable[float] = CUT_TABLE,
    none_below: float | None = None,
) -> list[dict[str, Any]]:
    """Weighted by each row's stratum weight, so a stratified sample estimates one day's population.

    With `none_below`, a claim the coverage answer says was already pushed is not counted as pushed: the
    decision the planner makes, rather than the score alone.
    """

    labelled = [row for row in rows if row["case_id"] in answers and row.get("label")]
    keep_weight = sum(row["weight"] for row in labelled if row["label"]["verdict"] == "keep")
    table = []
    for cut in cuts:
        pushed = [
            row
            for row in labelled
            if answers[row["case_id"]].importance.value >= cut  # type: ignore[union-attr]
            and (none_below is None or covered(answers[row["case_id"]], none_below) is None)
        ]
        weight = sum(row["weight"] for row in pushed)
        kept = sum(row["weight"] for row in pushed if row["label"]["verdict"] == "keep")
        border = sum(row["weight"] for row in pushed if row["label"]["verdict"] == "borderline")
        table.append(
            {
                "cut": cut,
                "claims_per_day": round(weight),
                "precision_keep_borderline": round((kept + border) / weight, 3) if weight else None,
                "keep_recall": round(kept / keep_weight, 3) if keep_weight else None,
            }
        )
    return table


def coverage_report(
    rows: Sequence[Mapping[str, Any]],
    answers: Mapping[str, ReaderJudgment],
    *,
    thresholds: Iterable[float] = NONE_THRESHOLDS,
) -> list[dict[str, Any]]:
    judged = [row for row in rows if row["case_id"] in answers and answers[row["case_id"]].coverage is not None]
    report = []
    for threshold in thresholds:
        counts: Counter[str] = Counter()
        for row in judged:
            truth = row["label"]["covered_by"]
            index = covered(answers[row["case_id"]], threshold)
            predicted = None if index is None else message_id(index)
            if truth != "none":
                counts["covered"] += 1
                counts["covered_found"] += predicted is not None
                counts["same_message"] += predicted == truth
            else:
                counts["uncovered"] += 1
                counts["false_covered"] += predicted is not None
        report.append(
            {
                "none_below": threshold,
                "cases": len(judged),
                "covered_recall": round(counts["covered_found"] / counts["covered"], 3) if counts["covered"] else None,
                "false_covered_rate": (
                    round(counts["false_covered"] / counts["uncovered"], 3) if counts["uncovered"] else None
                ),
                "same_message": counts["same_message"],
                "covered": counts["covered"],
                "uncovered": counts["uncovered"],
            }
        )
    return report


def evaluate(
    replay: Sequence[Mapping[str, Any]],
    coverage: Sequence[Mapping[str, Any]],
    replay_answers: Mapping[str, ReaderJudgment],
    coverage_answers: Mapping[str, ReaderJudgment],
    backend: ReaderBackend,
) -> dict[str, Any]:
    cuts = READER_CUTS[backend]
    return {
        "backend": backend,
        "cuts": {"push": cuts.push, "key": cuts.key, "covered_none_below": cuts.covered_none_below},
        "answered": {"reader_replay": len(replay_answers), "coverage": len(coverage_answers)},
        "importance": importance_report(replay, replay_answers),
        "cut_table": cut_table(replay, replay_answers),
        "decision_table": cut_table(replay, replay_answers, none_below=cuts.covered_none_below),
        "coverage": coverage_report(coverage, coverage_answers),
    }


async def _live(replay: list[dict[str, Any]], coverage: list[dict[str, Any]]) -> dict[str, Any]:
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
        backend: ReaderBackend = "native" if connection is not None else "generated"
        replay_run = await ask(judge, replay)
        coverage_run = await ask(judge, coverage)
    finally:
        if connection is not None:
            await connection.aclose()
    report = evaluate(replay, coverage, replay_run["answers"], coverage_run["answers"], backend)
    report["failures"] = {"reader_replay": replay_run["failures"], "coverage": coverage_run["failures"]}
    report["latency_ms"] = {"reader_replay": replay_run["latency_ms"], "coverage": coverage_run["latency_ms"]}
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reader-replay", type=Path, default=READER_REPLAY)
    parser.add_argument("--coverage", type=Path, default=COVERAGE_LABELED)
    parser.add_argument("--backend", choices=("native", "generated"), default="native")
    parser.add_argument("--live", action="store_true", help="ask the configured reader judge (network, cost)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    replay, coverage = load(args.reader_replay), load(args.coverage)
    if args.live:
        report = asyncio.run(_live(replay, coverage))
    else:
        report = evaluate(
            replay, coverage, recorded(replay, args.backend), recorded(coverage, args.backend), args.backend
        )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
