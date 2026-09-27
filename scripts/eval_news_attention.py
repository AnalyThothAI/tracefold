"""Offline comparison of frozen News attention inputs; network use requires --live-model.

Input: {"cases": [{"case_id", "input_snapshot", "recorded_decisions", "labels"}]}
`recorded_decisions` are stored claim_decisions; other preselected refs are ignored.
Labels map candidate refs to should_push, should_hold, or uncertain.
No sender or database is constructed by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from tracefold.news.updates.contracts import EventUpdate
from tracefold.news.updates.dspy_backend import DspyAttentionAssessor


def _cases(path: Path) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("news_attention_eval_cases_required")
    result = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("input_snapshot"), dict):
            raise ValueError("news_attention_eval_case_invalid")
        snapshot = case["input_snapshot"]
        update = EventUpdate.model_validate(snapshot["update"])
        refs = [row["ref"] for row in snapshot["candidate"]["claims"]]
        if len(refs) != len(set(refs)) or not set(refs) <= {claim.ref for claim in update.claims}:
            raise ValueError("news_attention_eval_candidate_refs_invalid")
        result.append(case)
    return result


def _recorded(case: dict[str, Any]) -> list[dict[str, str]]:
    rows = case.get("recorded_decisions")
    if not isinstance(rows, list):
        raise ValueError("news_attention_eval_recording_required")
    refs = {row["ref"] for row in case["input_snapshot"]["candidate"]["claims"]}
    decisions = [
        {
            "claim_ref": str(row["claim_ref"]),
            "disposition": "feed_only"
            if row["decision"] == "not_notified"
            else "key"
            if row.get("reason") == "editor_key"
            else "notify",
        }
        for row in rows
        if row["claim_ref"] in refs
    ]
    if {row["claim_ref"] for row in decisions} != refs or len(decisions) != len(refs):
        raise ValueError("news_attention_eval_recorded_refs_invalid")
    return decisions


async def _live(case: dict[str, Any], assessor: DspyAttentionAssessor) -> list[dict[str, str | None]]:
    snapshot = case["input_snapshot"]
    update = EventUpdate.model_validate(snapshot["update"])
    refs = {row["ref"] for row in snapshot["candidate"]["claims"]}
    claims = tuple(claim for claim in update.claims if claim.ref in refs)
    sources = {item.ref: item.source for item in update.evidence}
    response = await assessor.assess(
        claims,
        sources=sources,
        watch_symbols=tuple(snapshot["candidate"].get("watch_symbols") or ()),
    )
    return [row.model_dump(mode="json") for row in response.decisions]


def _usage(lms: tuple[Any, ...]) -> dict[str, int | None]:
    prompt = completion = 0
    known = False
    for lm in lms:
        for record in getattr(lm, "history", ()):
            usage = record.get("usage") if isinstance(record, dict) else None
            if not isinstance(usage, dict):
                continue
            known = True
            prompt += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return {"input_tokens": prompt if known else None, "output_tokens": completion if known else None}


async def evaluate(path: Path, *, live_model: str | None = None) -> dict[str, Any]:
    cases = _cases(path)
    assessor = None
    lms: tuple[Any, ...] = ()
    if live_model is not None:
        from tracefold.app.learning_runtime import compose_news_models
        from tracefold.platform.config.loader import load_settings

        models = compose_news_models(load_settings(require_ws_token=False))
        if models is None or str(models.card.primary.model_name) != live_model:
            raise ValueError("news_attention_eval_model_must_match_configured_card_route")
        lms = models.card.lms()
        assessor = DspyAttentionAssessor(lambda: lms, model_identity=models.card.identity)
    results = []
    for case in cases:
        before = sum(len(getattr(lm, "history", ())) for lm in lms)
        started = perf_counter()
        failure = None
        try:
            decisions = (
                _recorded(case) if assessor is None else await asyncio.wait_for(_live(case, assessor), timeout=20.0)
            )
        except Exception as exc:
            if assessor is None:
                raise
            decisions = []
            failure = type(exc).__name__
        elapsed_ms = round((perf_counter() - started) * 1000)
        observed_calls = sum(len(getattr(lm, "history", ())) for lm in lms) - before
        physical_calls = observed_calls if assessor is not None and observed_calls else None
        labels = case.get("labels") or {}
        if not isinstance(labels, dict) or set(labels.values()) - {"should_push", "should_hold", "uncertain"}:
            raise ValueError("news_attention_eval_labels_invalid")
        results.append(
            {
                "case_id": case.get("case_id"),
                "event_id": case["input_snapshot"]["update"]["event_id"],
                "decisions": decisions,
                "labels": labels,
                "physical_calls": physical_calls,
                "elapsed_ms": elapsed_ms if assessor is not None else None,
                "recording_origin": case.get("recording_origin") if assessor is None else None,
                "error_type": failure,
                "useful_held": sum(
                    1
                    for row in decisions
                    if labels.get(row["claim_ref"]) == "should_push" and row["disposition"] == "feed_only"
                ),
                "noise_notified": sum(
                    1
                    for row in decisions
                    if labels.get(row["claim_ref"]) == "should_hold" and row["disposition"] != "feed_only"
                ),
            }
        )
    return {
        "schema": "news_attention_eval_v1",
        "mode": "live" if assessor is not None else "recorded",
        "model": live_model,
        "assessment_identity": None if assessor is None else assessor.identity,
        "cases": results,
        "totals": {
            "cases": len(results),
            "physical_calls": (
                sum(row["physical_calls"] for row in results)
                if assessor is not None and all(row["physical_calls"] is not None for row in results)
                else None
            ),
            "elapsed_ms": sum(row["elapsed_ms"] for row in results) if assessor is not None else None,
            "useful_held": sum(row["useful_held"] for row in results),
            "noise_notified": sum(row["noise_notified"] for row in results),
            "technical_failures": sum(row["error_type"] is not None for row in results),
            "usage": _usage(lms),
            "cost_usd": None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--live-model", help="explicitly use the configured card route primary model")
    args = parser.parse_args()
    report = asyncio.run(evaluate(args.input, live_model=args.live_model))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
