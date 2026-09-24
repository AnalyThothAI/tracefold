"""Root-level rule/DSPy comparison from frozen, contemporary analysis exports.

This command never fetches current market data or invents execution prices.
Missing quote, mark, funding or cost receipts remain unknown in the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from tracefold.trading.engine.contracts import ExitPlan
from tracefold.trading.engine.strategy import (
    BAR_MS,
    ENTRY_WINDOW_MS,
    STRATEGY_VERSION,
    build_event_price_candidates,
    range_cross_side,
)

ARMS = ("rule", "dspy")
PURGE_MS = 4 * 3_600_000


def _rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"case_row_invalid:{number}")
            rows.append(row)
    return rows


def _split(created_at_ms: int, cutoff_ms: int) -> str:
    if created_at_ms + PURGE_MS <= cutoff_ms:
        return "development"
    if created_at_ms >= cutoff_ms:
        return "holdout"
    return "purged"


@dataclass(frozen=True)
class RuleDecision:
    action: Literal["TRADE", "NO_TRADE"] | None
    side: Literal["long", "short"] | None = None
    trigger_at_ms: int | None = None
    visible_at_ms: int | None = None
    reference_price: Decimal | None = None
    structure_level: Decimal | None = None
    exit_plan: ExitPlan | None = None


def _rule_decision(root_cases: list[dict[str, Any]]) -> RuleDecision:
    initial = next(row for row in root_cases if row["run_kind"] == "initial")
    snapshot = initial.get("evidence")
    if not isinstance(snapshot, dict):
        return RuleDecision(None)
    market = snapshot.get("market") or {}
    perp = market.get("perp_bars") or {}
    if perp.get("status") != "ok":
        return RuleDecision(None)
    try:
        candidates = build_event_price_candidates(
            asset_id=str(initial["asset_id"]),
            instrument_semantics_digest=str(initial["mapping_semantics_digest"]),
            source_fact=snapshot["source_fact"],
            source_first_visible_at_ms=int(snapshot["source_first_visible_at_ms"]),
            perp_rows=tuple(perp["payload"]),
        )
        expires_at = int(initial["root_expires_at_ms"])
        ready = next((candidate for candidate in candidates if candidate.entry_ready), None)
        if ready is not None:
            trigger_at_ms = ready.entry_observed_at_ms
            visible_at_ms = int(perp["payload"][-1]["received_at_ms"])
            if visible_at_ms < trigger_at_ms:
                return RuleDecision(None)
            if visible_at_ms >= min(expires_at, trigger_at_ms + ENTRY_WINDOW_MS):
                return RuleDecision("NO_TRADE")
            return RuleDecision(
                "TRADE",
                side=ready.side,
                trigger_at_ms=trigger_at_ms,
                visible_at_ms=visible_at_ms,
                reference_price=ready.entry_observed,
                structure_level=ready.entry_level,
                exit_plan=ready.exit_plan,
            )
        if not all(candidate.watch_eligible for candidate in candidates):
            return RuleDecision("NO_TRADE")
        # The rule arm must not borrow a child Case that only exists because DSPy
        # chose WATCH. Its own post-setup bar path is frozen in the root export.
        path = initial.get("rule_watch_bars")
        if not isinstance(path, list):
            return RuleDecision(None)
        upper = candidates[0].entry_level
        lower = candidates[1].entry_level
        previous = candidates[0].entry_observed
        expected_at = candidates[0].entry_observed_at_ms + BAR_MS
        for bar in path:
            at_ms = int(bar["event_at_ms"])
            received_at_ms = int(bar["received_at_ms"])
            close = Decimal(str(bar["close"]))
            if at_ms > expires_at:
                break
            if at_ms != expected_at or received_at_ms < at_ms or not close.is_finite() or close <= 0:
                return RuleDecision(None)
            side = range_cross_side(previous_close=previous, close=close, upper=upper, lower=lower)
            if side is not None:
                # First crossing consumes the root, including when the receipt
                # arrives too late for the immutable entry window.
                if received_at_ms >= min(expires_at, at_ms + ENTRY_WINDOW_MS):
                    return RuleDecision("NO_TRADE")
                return RuleDecision(
                    "TRADE",
                    side=side,
                    trigger_at_ms=at_ms,
                    visible_at_ms=received_at_ms,
                    reference_price=close,
                    structure_level=upper if side == "long" else lower,
                    exit_plan=candidates[0].exit_plan,
                )
            previous, expected_at = close, at_ms + BAR_MS
        if initial.get("rule_watch_status") == "complete" and expected_at > expires_at:
            return RuleDecision("NO_TRADE")
        return RuleDecision(None)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return RuleDecision(None)


def _rule_action(root_cases: list[dict[str, Any]]) -> str | None:
    return _rule_decision(root_cases).action


def _dspy_action(root_cases: list[dict[str, Any]]) -> str | None:
    latest = max(root_cases, key=lambda row: (int(row.get("recheck_seq") or 0), int(row["created_at_ms"])))
    return latest.get("decision_action")


def _model_cost(root_cases: list[dict[str, Any]]) -> tuple[int, int]:
    known = unknown = 0
    for row in root_cases:
        attempts = row.get("attempts")
        if not isinstance(attempts, list):
            attempts = [row] if row.get("model_attempted") else []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            cost = attempt.get("cost_microusd", attempt.get("model_cost_microusd"))
            if cost is None:
                known += int(attempt.get("known_cost_microusd") or 0)
                unknown += (
                    int(attempt.get("unknown_cost_calls") or 0)
                    if "unknown_cost_calls" in attempt
                    else int(bool(attempt.get("physical_call_count", 1)))
                )
            else:
                known += int(cost)
    return known, unknown


def _account_drawdown(initial_equity: Decimal, trades: list[dict[str, Any]]) -> Decimal | None:
    """Use simultaneous liquidation marks for every open position at every sample."""
    if not trades:
        return None
    timestamps: set[int] = set()
    marks_by_trade: list[dict[int, Decimal]] = []
    for trade in trades:
        entry_at, exit_at = trade["entry_at_ms"], trade["exit_at_ms"]
        timestamps.update((entry_at, exit_at))
        raw = trade["equity_marks"]
        if not isinstance(raw, list):
            return None
        marks: dict[int, Decimal] = {}
        try:
            for item in raw:
                at_ms = int(item["at_ms"])
                net_bps = Decimal(str(item["liquidation_net_bps"]))
                if not entry_at <= at_ms < exit_at or at_ms in marks or not net_bps.is_finite():
                    return None
                marks[at_ms] = net_bps
                timestamps.add(at_ms)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return None
        if entry_at not in marks:
            return None
        marks_by_trade.append(marks)
    peak = initial_equity
    maximum = Decimal(0)
    for at_ms in sorted(timestamps):
        equity = initial_equity
        for trade, marks in zip(trades, marks_by_trade, strict=True):
            if trade["exit_at_ms"] <= at_ms:
                equity += trade["realized_usdt"]
            elif trade["entry_at_ms"] <= at_ms:
                if at_ms not in marks:
                    return None
                equity += trade["notional_usdt"] * marks[at_ms] / 10_000
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _arm_summary(
    roots: list[list[dict[str, Any]]],
    arm: str,
    *,
    initial_equity: Decimal,
    max_positions: int,
    max_notional_fraction: Decimal,
    risk_fraction: Decimal,
) -> dict[str, Any]:
    decisions = Counter()
    entries: list[tuple[int, int, str, dict[str, Any]]] = []
    unknown_net = 0
    known_model_cost = unknown_model_calls = 0
    for root_cases in roots:
        initial = next(row for row in root_cases if row["run_kind"] == "initial")
        action = _rule_action(root_cases) if arm == "rule" else _dspy_action(root_cases)
        decisions[str(action or "unavailable")] += 1
        if arm == "dspy":
            known, unknown = _model_cost(root_cases)
            known_model_cost += known
            unknown_model_calls += unknown
        if action != "TRADE":
            continue
        trade_case = (
            initial
            if arm == "rule"
            else max(
                (row for row in root_cases if row["run_kind"] in ("initial", "conditional")),
                key=lambda row: (int(row.get("recheck_seq") or 0), int(row["created_at_ms"])),
            )
        )
        receipt = (trade_case.get("arm_evaluations") or {}).get(arm)
        if not isinstance(receipt, dict) or receipt.get("status") != "simulated":
            unknown_net += 1
            continue
        try:
            if receipt.get("strategy_version") != STRATEGY_VERSION:
                raise ValueError("strategy_version_mismatch")
            entry_at = int(receipt["entry_at_ms"])
            exit_at = int(receipt["exit_at_ms"])
            net_bps = Decimal(str(receipt["net_bps"]))
            components = receipt["net_components_bps"]
            gross = Decimal(str(components["gross"]))
            entry_cost = Decimal(str(components["entry_cost"]))
            exit_cost = Decimal(str(components["exit_cost"]))
            fees = Decimal(str(components["fees"]))
            funding = Decimal(str(components["funding_cashflow"]))
            if not all(value.is_finite() for value in (gross, entry_cost, exit_cost, fees, funding)):
                raise ValueError("receipt_component_nonfinite")
            if min(entry_cost, exit_cost, fees) < 0 or net_bps != gross - entry_cost - exit_cost - fees + funding:
                raise ValueError("receipt_net_mismatch")
            if (
                any(
                    not receipt.get(ref)
                    for ref in (
                        "entry_quote_ref",
                        "exit_quote_ref",
                        "instrument_rules_ref",
                        "mark_path_ref",
                        "funding_ref",
                        "fee_ref",
                    )
                )
                or int(receipt["order_latency_ms"]) < 0
            ):
                raise ValueError("receipt_provenance_incomplete")
            requested_notional = Decimal(str(receipt["requested_notional_usdt"]))
            simulated_notional = Decimal(str(receipt["simulated_notional_usdt"]))
            quantity = Decimal(str(receipt["quantity_base"]))
            entry_price = Decimal(str(receipt["entry_price"]))
            market_step = Decimal(str(receipt["market_step_size"]))
            stop_bps = Decimal(str(receipt["stop_bps"]))
            if (
                exit_at <= entry_at
                or not net_bps.is_finite()
                or not requested_notional.is_finite()
                or requested_notional <= 0
                or not all(value.is_finite() for value in (simulated_notional, quantity, entry_price, market_step))
                or min(simulated_notional, quantity, entry_price, market_step) <= 0
                or simulated_notional > requested_notional
                or simulated_notional != quantity * entry_price
                or quantity % market_step != 0
                or not stop_bps.is_finite()
                or stop_bps <= 0
            ):
                raise ValueError("receipt_invalid")
        except (KeyError, TypeError, InvalidOperation, ValueError):
            unknown_net += 1
            continue
        entries.append(
            (
                entry_at,
                exit_at,
                str(initial["root_trigger_id"]),
                {
                    "net_bps": net_bps,
                    "simulated_notional": simulated_notional,
                    "stop_bps": stop_bps,
                    "equity_marks": receipt.get("equity_marks"),
                },
            )
        )
    entries.sort(key=lambda item: (item[0], item[2]))
    equity = initial_equity
    peak = equity
    max_drawdown = Decimal(0)
    active: list[tuple[int, Decimal, Decimal, Decimal]] = []
    accepted = capital_rejected = 0
    accepted_trades: list[dict[str, Any]] = []
    for entry_at, exit_at, _, receipt in entries:
        active.sort(key=lambda item: item[0])
        while active and active[0][0] <= entry_at:
            _, _, _, realized = active.pop(0)
            equity += realized
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        if len(active) >= max_positions:
            capital_rejected += 1
            continue
        capital_available = equity * max_notional_fraction - sum(item[1] for item in active)
        risk_available = equity * risk_fraction - sum(item[1] * item[2] / 10_000 for item in active)
        notional = receipt["simulated_notional"]
        if notional > capital_available or notional * receipt["stop_bps"] / 10_000 > risk_available:
            capital_rejected += 1
            continue
        realized = notional * receipt["net_bps"] / 10_000
        active.append((exit_at, notional, receipt["stop_bps"], realized))
        accepted += 1
        accepted_trades.append(
            {
                "entry_at_ms": entry_at,
                "exit_at_ms": exit_at,
                "notional_usdt": notional,
                "realized_usdt": realized,
                "equity_marks": receipt["equity_marks"],
            }
        )
    for _, _, _, realized in sorted(active):
        equity += realized
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    account_drawdown = _account_drawdown(initial_equity, accepted_trades)
    return {
        "decisions": dict(sorted(decisions.items())),
        "net_evaluable": accepted,
        "net_unknown": unknown_net,
        "capital_rejected": capital_rejected,
        "ending_equity_usdt": str(equity) if accepted else None,
        "closed_equity_drawdown_usdt": str(max_drawdown) if accepted else None,
        "account_drawdown_usdt": str(account_drawdown) if account_drawdown is not None else None,
        "unrealized_marks_complete": account_drawdown is not None,
        "model_cost_known_microusd": known_model_cost,
        "model_cost_unknown_calls": unknown_model_calls,
    }


def _legacy_output_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    parsed = 0
    missing = Counter()
    for row in rows:
        raw = row.get("raw_output")
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            missing["json_unparseable"] += 1
            continue
        if not isinstance(value, dict):
            missing["object_missing"] += 1
            continue
        parsed += 1
        for field in ("action", "public_rationale", "candidate_assessments"):
            if field not in value:
                missing[field] += 1
    return {
        "inputs": len(rows),
        "legacy_json_objects": parsed,
        "missing_legacy_fields": dict(sorted(missing.items())),
        "new_program_replayed": False,
    }


def evaluate(
    cases: list[dict[str, Any]],
    *,
    expected_roots: int,
    cutoff_ms: int,
    invalid_outputs: list[dict[str, Any]],
    expected_invalid: int,
    initial_equity_usdt: Decimal = Decimal("1000"),
    max_positions: int = 1,
    max_notional_fraction: Decimal = Decimal("0.1"),
    risk_fraction: Decimal = Decimal("0.01"),
) -> dict[str, Any]:
    ids = [str(row["case_id"]) for row in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_case_id")
    roots: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in cases:
        if row.get("run_kind") not in ("initial", "conditional", "recheck"):
            raise ValueError("historical_run_kind_excluded")
        roots[str(row["root_trigger_id"])].append(row)
    if len(roots) != expected_roots:
        raise ValueError(f"root_denominator_mismatch:{len(roots)}:{expected_roots}")
    if len(invalid_outputs) != expected_invalid:
        raise ValueError(f"invalid_output_denominator_mismatch:{len(invalid_outputs)}:{expected_invalid}")
    if (
        initial_equity_usdt <= 0
        or max_positions <= 0
        or not 0 < max_notional_fraction <= 1
        or not 0 < risk_fraction <= 1
    ):
        raise ValueError("capital_config_invalid")
    initial: dict[str, dict[str, Any]] = {}
    group_splits: dict[str, set[str]] = defaultdict(set)
    for root_id, root_cases in roots.items():
        first = [row for row in root_cases if row["run_kind"] == "initial"]
        if len(first) != 1 or len({row.get("asset_id") for row in root_cases}) != 1:
            raise ValueError("root_identity_invalid")
        initial[root_id] = first[0]
        group = str(first[0].get("source_group_id") or root_id)
        group_splits[group].add(_split(int(first[0]["created_at_ms"]), cutoff_ms))
    partitions = {}
    for root_id, row in initial.items():
        group = str(row.get("source_group_id") or root_id)
        possibilities = group_splits[group]
        partitions[root_id] = next(iter(possibilities)) if len(possibilities) == 1 else "cross_split_excluded"
    return {
        "protocol_version": "trading_cohort_v2",
        "strategy_version": STRATEGY_VERSION,
        "denominator": {"root_triggers": len(roots), "cases": len(cases)},
        "funnel": {
            "initial_excluded": sum(row.get("state") == "EXCLUDED" for row in initial.values()),
            "initial_decisions": sum(bool(row.get("decision_action")) for row in initial.values()),
            "initial_failures": sum(
                row.get("state") != "EXCLUDED" and not row.get("decision_action") for row in initial.values()
            ),
            "conditional_cases": sum(row["run_kind"] == "conditional" for row in cases),
            "historical_rechecks": sum(row["run_kind"] == "recheck" for row in cases),
        },
        "split_roots": dict(sorted(Counter(partitions.values()).items())),
        "strata": {
            partition: dict(
                sorted(
                    Counter(
                        f"{row.get('source_kind', 'unknown')}:{row.get('asset_id', 'unknown')}"
                        for root_id, row in initial.items()
                        if partitions[root_id] == partition
                    ).items()
                )
            )
            for partition in ("development", "holdout")
        },
        "arms": {
            partition: {
                arm: _arm_summary(
                    [root_cases for root_id, root_cases in roots.items() if partitions[root_id] == partition],
                    arm,
                    initial_equity=initial_equity_usdt,
                    max_positions=max_positions,
                    max_notional_fraction=max_notional_fraction,
                    risk_fraction=risk_fraction,
                )
                for arm in ARMS
            }
            for partition in ("development", "holdout")
        },
        "historical_invalid_outputs": _legacy_output_summary(invalid_outputs),
        "research_conclusion": "evidence_insufficient_without_complete_contemporary_receipts",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--cases-manifest", type=Path)
    parser.add_argument("--invalid-outputs", type=Path, required=True)
    parser.add_argument("--cutoff-ms", type=int, required=True)
    parser.add_argument("--expected-roots", type=int, default=531)
    parser.add_argument("--expected-invalid", type=int, default=22)
    parser.add_argument("--initial-equity-usdt", type=Decimal, default=Decimal("1000"))
    parser.add_argument("--max-positions", type=int, default=1)
    parser.add_argument("--max-notional-fraction", type=Decimal, default=Decimal("0.1"))
    parser.add_argument("--risk-fraction", type=Decimal, default=Decimal("0.01"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases_digest = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    invalid_digest = hashlib.sha256(args.invalid_outputs.read_bytes()).hexdigest()
    manifest_digest = None
    if args.cases_manifest is not None:
        raw_manifest = args.cases_manifest.read_bytes()
        manifest_digest = hashlib.sha256(raw_manifest).hexdigest()
        manifest = json.loads(raw_manifest)
        if not isinstance(manifest, dict) or manifest.get("cases_sha256") != cases_digest:
            raise ValueError("case_export_manifest_mismatch")
    report = evaluate(
        _rows(args.cases),
        expected_roots=args.expected_roots,
        cutoff_ms=args.cutoff_ms,
        invalid_outputs=_rows(args.invalid_outputs),
        expected_invalid=args.expected_invalid,
        initial_equity_usdt=args.initial_equity_usdt,
        max_positions=args.max_positions,
        max_notional_fraction=args.max_notional_fraction,
        risk_fraction=args.risk_fraction,
    )
    report["research_manifest"] = {
        "cases_sha256": cases_digest,
        "invalid_outputs_sha256": invalid_digest,
        "cases_manifest_sha256": manifest_digest,
        "cutoff_ms": args.cutoff_ms,
        "initial_equity_usdt": str(args.initial_equity_usdt),
        "max_positions": args.max_positions,
        "max_notional_fraction": str(args.max_notional_fraction),
        "risk_fraction": str(args.risk_fraction),
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
