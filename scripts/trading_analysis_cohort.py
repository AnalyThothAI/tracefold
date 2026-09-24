"""Recompute the #690/#691 Case funnel and leak-free research coverage from an export.

The input is one JSON object per Case, exported from frozen Trading records. This
script never fetches today's market data or reruns a model. Missing counterfactual
quotes and costs remain unavailable; they cannot become zero-return observations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tracefold.app.trading_analyst import _WireAssessment
from tracefold.trading.engine.contracts import AgentAssessment
from tracefold.trading.engine.strategy import build_oi_price_candidates

ARMS = ("simple_rule", "recorded_predict", "simplified_predict")


def _rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"case_row_invalid:{number}")
        rows.append(row)
    return rows


def _simple_rule(snapshot: Any, asset_id: str) -> str | None:
    if not isinstance(snapshot, dict):
        return None
    source = snapshot.get("source_fact")
    market = snapshot.get("market")
    if not isinstance(source, dict) or not isinstance(market, dict):
        return None
    perp, oi = market.get("perp_bars"), market.get("open_interest")
    if not isinstance(perp, dict) or not isinstance(oi, dict):
        return None
    if perp.get("status") != "ok":
        return None
    try:
        candidates = build_oi_price_candidates(
            asset_id=asset_id,
            instrument_semantics_digest="0" * 64,
            source_fact=source,
            perp_rows=tuple(perp["payload"]),
            market_oi_available=oi.get("status") == "ok",
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if any(candidate.entry_ready for candidate in candidates):
        return "TRADE"
    if any(candidate.watch_eligible for candidate in candidates):
        return "WATCH"
    return "NO_TRADE"


def _split(asset_id: str | None, created_at_ms: int, cutoff_ms: int) -> str:
    if not asset_id:
        return "unassigned"
    # Asset groups cannot occur in both partitions. Time additionally fences
    # every development Case before every held-out Case.
    held_asset = int(hashlib.sha256(asset_id.encode()).hexdigest()[:8], 16) % 10 >= 7
    if held_asset and created_at_ms >= cutoff_ms:
        return "holdout"
    if not held_asset and created_at_ms < cutoff_ms:
        return "development"
    return "outside_split"


def _arm_summary(rows: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    decisions = Counter()
    evaluated: list[tuple[int, str, Decimal]] = []
    unknown_cost = 0
    known_cost = 0
    total_cost = 0
    for row in rows:
        action = (
            _simple_rule(row.get("evidence"), str(row.get("asset_id") or ""))
            if arm == "simple_rule"
            else row.get("decision_action")
            if arm == "recorded_predict"
            else row.get("simplified_action")
        )
        decisions[str(action or "unavailable")] += 1
        if arm == "recorded_predict" and row.get("model_attempted") is True:
            cost = row.get("model_cost_microusd")
            if cost is None:
                unknown_cost += 1
            else:
                known_cost += 1
                total_cost += int(cost)
        evaluation = (row.get("arm_evaluations") or {}).get(arm)
        if action != "TRADE" or not isinstance(evaluation, dict):
            continue
        if evaluation.get("status") != "simulated" or evaluation.get("net_bps") is None:
            continue
        try:
            net = Decimal(str(evaluation["net_bps"]))
        except (InvalidOperation, TypeError):
            continue
        if net.is_finite():
            evaluated.append((int(row["created_at_ms"]), str(row["case_id"]), net))
    evaluated.sort()
    running = peak = max_drawdown = Decimal(0)
    for _, _, net in evaluated:
        running += net
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
    return {
        "decisions": dict(sorted(decisions.items())),
        "net_evaluable": len(evaluated),
        "net_unknown": decisions["TRADE"] - len(evaluated),
        "mean_net_bps": str(sum((item[2] for item in evaluated), Decimal(0)) / len(evaluated)) if evaluated else None,
        "trade_sequence_max_drawdown_bps": str(max_drawdown) if evaluated else None,
        "account_drawdown": None,
        "model_cost_known_cases": known_cost,
        "model_cost_unknown_cases": unknown_cost,
        "model_cost_known_microusd": total_cost if known_cost else None,
    }


def _invalid_output_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    first = Counter()
    all_errors = Counter()
    valid_wire = 0
    for row in rows:
        raw = row.get("raw_output")
        try:
            if isinstance(raw, str):
                raw = json.loads(raw)
            wire = _WireAssessment.model_validate(raw)
            AgentAssessment.model_validate_json(wire.model_dump_json())
        except (ValidationError, ValueError, TypeError) as exc:
            if isinstance(exc, ValidationError):
                errors = [
                    f"{'.'.join(map(str, item['loc']))}:{item['type']}" for item in exc.errors(include_input=False)
                ]
            else:
                errors = [type(exc).__name__]
            first[errors[0]] += 1
            all_errors.update(errors)
        else:
            valid_wire += 1
    return {
        "inputs": len(rows),
        "wire_valid_v2": valid_wire,
        "first_errors": dict(sorted(first.items())),
        "all_errors": dict(sorted(all_errors.items())),
        "contextual_decision_validity": None,
    }


def evaluate(
    cases: list[dict[str, Any]],
    *,
    expected_roots: int,
    cutoff_ms: int,
    invalid_outputs: list[dict[str, Any]],
    expected_invalid: int,
) -> dict[str, Any]:
    ids = [str(row["case_id"]) for row in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_case_id")
    roots = defaultdict(list)
    for row in cases:
        if row.get("run_kind") not in ("initial", "recheck"):
            raise ValueError("historical_run_kind_excluded")
        roots[str(row["root_trigger_id"])].append(row)
    if len(roots) != expected_roots:
        raise ValueError(f"root_denominator_mismatch:{len(roots)}:{expected_roots}")
    if len(invalid_outputs) != expected_invalid:
        raise ValueError(f"invalid_output_denominator_mismatch:{len(invalid_outputs)}:{expected_invalid}")
    initial = []
    for root_cases in roots.values():
        first = [row for row in root_cases if row["run_kind"] == "initial"]
        if len(first) != 1:
            raise ValueError("root_initial_case_count_invalid")
        initial.append(first[0])
        assets = {row.get("asset_id") for row in root_cases}
        if len(assets) != 1:
            raise ValueError("root_asset_changed")
    split = {
        root_id: _split(
            next(iter({row.get("asset_id") for row in root_cases})),
            int(min(row["created_at_ms"] for row in root_cases)),
            cutoff_ms,
        )
        for root_id, root_cases in roots.items()
    }
    return {
        "protocol_version": "trading_cohort_v1",
        "denominator": {"root_triggers": len(roots), "cases": len(cases)},
        "funnel": {
            "initial_excluded": sum(row.get("state") == "EXCLUDED" for row in initial),
            "initial_decisions": sum(bool(row.get("decision_action")) for row in initial),
            "initial_failures": sum(
                row.get("state") != "EXCLUDED" and not row.get("decision_action") for row in initial
            ),
            "recheck_cases": sum(row["run_kind"] == "recheck" for row in cases),
            "recheck_decisions": sum(
                row["run_kind"] == "recheck" and bool(row.get("decision_action")) for row in cases
            ),
            "recheck_failures": sum(row["run_kind"] == "recheck" and not row.get("decision_action") for row in cases),
            "watch_satisfied": sum(row.get("watch_status") == "satisfied" for row in cases),
        },
        "split_roots": dict(sorted(Counter(split.values()).items())),
        "arms": {
            partition: {
                arm: _arm_summary(
                    [row for row in cases if split[str(row["root_trigger_id"])] == partition],
                    arm,
                )
                for arm in ARMS
            }
            for partition in ("development", "holdout")
        },
        "historical_invalid_outputs": _invalid_output_summary(invalid_outputs),
        "release_conclusion": "remain_shadow_until_complete_holdout_and_paper_receipts",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--invalid-outputs", type=Path, required=True)
    parser.add_argument("--cutoff-ms", type=int, required=True)
    parser.add_argument("--expected-roots", type=int, default=531)
    parser.add_argument("--expected-invalid", type=int, default=22)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(
        _rows(args.cases),
        expected_roots=args.expected_roots,
        cutoff_ms=args.cutoff_ms,
        invalid_outputs=_rows(args.invalid_outputs),
        expected_invalid=args.expected_invalid,
    )
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
