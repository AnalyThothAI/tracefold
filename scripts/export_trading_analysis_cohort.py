"""Export one frozen row per Trading Case for the #690/#691 cohort command.

The database supplies identities and receipt refs; the durable analysis archive
supplies frozen source, market, and root-tape content. No market or model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from scripts.trading_analysis_cohort import RuleDecision, _rule_decision
from tracefold.app.analysis_files import AnalysisFiles
from tracefold.trading.engine.evaluation import EVALUATION_VERSION, evaluate_shadow
from tracefold.trading.engine.strategy import ENTRY_WINDOW_MS, MAX_HOLDING_SECONDS, STRATEGY_VERSION
from tracefold.trading.execution_contracts import entry_structure_allows

_BAR_MS = 60_000


def _unevaluable_rule(reason: str) -> dict[str, Any]:
    return {
        "status": "unevaluable",
        "reason": reason,
        "source": "shadow_simulation",
        "evaluation_version": EVALUATION_VERSION,
        "strategy_version": STRATEGY_VERSION,
    }


def _read_ref(files: AnalysisFiles, ref: str | None, missing: list[dict[str, str]], case_id: str, kind: str) -> Any:
    if not ref:
        missing.append({"case_id": case_id, "kind": kind, "reason": "reference_missing"})
        return None
    try:
        return files.read(ref)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        missing.append({"case_id": case_id, "kind": kind, "reason": type(exc).__name__, "ref": ref})
        return None


def _rule_watch_path(
    evidence: dict[str, Any], tape: dict[str, Any], expires_at_ms: int
) -> tuple[list[dict[str, Any]], str]:
    if tape.get("version") != "root_research_tape_v2" or not isinstance(tape.get("closed_bars"), list):
        return [], "missing"
    try:
        b0 = int(evidence["entry_reference"]["closed_at_ms"])
        rows = sorted(
            (bar for bar in tape["closed_bars"] if isinstance(bar, dict) and int(bar["event_at_ms"]) > b0),
            key=lambda bar: int(bar["event_at_ms"]),
        )
        path: list[dict[str, Any]] = []
        expected = b0 + _BAR_MS
        complete = True
        for bar in rows:
            at_ms = int(bar["event_at_ms"])
            if at_ms > expires_at_ms:
                break
            received = int(bar["received_at_ms"])
            close = Decimal(str(bar["close"]))
            if (
                at_ms != expected
                or received < at_ms
                or not bar.get("snapshot_ref")
                or not close.is_finite()
                or close <= 0
            ):
                complete = False
            path.append({"event_at_ms": at_ms, "received_at_ms": received, "close": bar["close"]})
            expected = at_ms + _BAR_MS
        return path, "complete" if complete and expected > expires_at_ms else "partial"
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return [], "invalid"


def _rule_shadow_receipt(
    row: dict[str, Any],
    tape: dict[str, Any],
    decision: RuleDecision,
    *,
    risk_usdt: Decimal | None,
    fee_bps_per_side: Decimal | None,
    max_spread_fraction_of_stop: Decimal | None,
) -> dict[str, Any]:
    if decision.action != "TRADE" or decision.side is None or decision.exit_plan is None:
        return _unevaluable_rule("rule_setup_unavailable")
    if decision.trigger_at_ms is None or decision.visible_at_ms is None or decision.reference_price is None:
        return _unevaluable_rule("rule_clock_unavailable")
    if decision.structure_level is None:
        return _unevaluable_rule("rule_structure_unavailable")
    if risk_usdt is None or fee_bps_per_side is None or max_spread_fraction_of_stop is None:
        return _unevaluable_rule("rule_cost_or_risk_assumption_missing")
    if (
        not all(value.is_finite() for value in (risk_usdt, fee_bps_per_side, max_spread_fraction_of_stop))
        or risk_usdt <= 0
        or fee_bps_per_side < 0
        or max_spread_fraction_of_stop <= 0
    ):
        return _unevaluable_rule("rule_cost_or_risk_assumption_invalid")
    selection = row.get("target_selection") or {}
    instrument = selection.get("instrument") if isinstance(selection, dict) else None
    evidence = row.get("evidence")
    if not isinstance(instrument, dict) or not isinstance(evidence, dict):
        return _unevaluable_rule("rule_instrument_or_evidence_missing")
    native = instrument.get("native_symbol")
    environment = instrument.get("environment")
    mapping = instrument.get("mapping_semantics_digest")
    units = str(instrument.get("units_per_contract"))
    if (
        tape.get("version") != "root_research_tape_v2"
        or tape.get("case_id") != row.get("case_id")
        or tape.get("native_symbol") != native
        or tape.get("environment") != environment
        or tape.get("mapping_semantics_digest") != mapping
        or tape.get("root_accepted_at_ms") != int(row["created_at_ms"])
    ):
        return _unevaluable_rule("root_market_tape_identity_mismatch")
    quotes = tape.get("quotes")
    marks = tape.get("mark_bars")
    funding = tape.get("funding_history")
    if not isinstance(quotes, list) or not isinstance(marks, list):
        return _unevaluable_rule("rule_market_tape_missing")
    eligible = sorted(
        (
            quote
            for quote in quotes
            if isinstance(quote, dict)
            and quote.get("status") == "ok"
            and quote.get("environment") == environment
            and quote.get("native_symbol") == native
            and quote.get("mapping_semantics_digest") == mapping
            and quote.get("units_per_contract") == units
            and quote.get("quote_ref")
            and isinstance(quote.get("received_at_ms"), int)
        ),
        key=lambda quote: int(quote["received_at_ms"]),
    )
    entry = next(
        (
            quote
            for quote in eligible
            if decision.visible_at_ms
            <= int(quote["received_at_ms"])
            < min(int(row["root_expires_at_ms"]), decision.trigger_at_ms + ENTRY_WINDOW_MS)
        ),
        None,
    )
    if entry is None:
        return _unevaluable_rule("rule_entry_quote_missing_or_late")
    try:
        bid, ask = Decimal(str(entry["bid"])), Decimal(str(entry["ask"]))
        executable = ask if decision.side == "long" else bid
        spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000
        drift_bps = abs(executable / decision.reference_price - 1) * 10_000
    except (InvalidOperation, KeyError, TypeError, ZeroDivisionError):
        return _unevaluable_rule("rule_entry_quote_invalid")
    if not all(value.is_finite() for value in (bid, ask, spread_bps, drift_bps)) or bid <= 0 or ask < bid:
        return _unevaluable_rule("rule_entry_quote_invalid")
    if not entry_structure_allows(direction=decision.side, executable=executable, level=decision.structure_level):
        return _unevaluable_rule("rule_entry_structure_lost")
    if drift_bps > 200:
        return _unevaluable_rule("rule_entry_price_outside_envelope")
    if spread_bps > max_spread_fraction_of_stop * decision.exit_plan.stop_distance_bps:
        return _unevaluable_rule("rule_entry_spread_limit")
    market = evidence.get("market")
    rules_frame = market.get("instrument_rules") if isinstance(market, dict) else None
    rules_payload = rules_frame.get("payload") if isinstance(rules_frame, dict) else None
    rules = (
        rules_payload[0]
        if isinstance(rules_payload, (list, tuple))
        and rules_payload
        and isinstance(rules_payload[0], dict)
        and rules_frame.get("status") == "ok"
        and rules_frame.get("unit_definition") == "binance_usdm_contract_rules_v1"
        and isinstance(rules_frame.get("received_at_ms"), int)
        and rules_frame["received_at_ms"] <= int(entry["received_at_ms"])
        and rules_payload[0].get("native_symbol") == native
        and evidence.get("data_environment") == environment
        else None
    )
    research_end_ms = int(row["root_expires_at_ms"]) + MAX_HOLDING_SECONDS * 1_000 + ENTRY_WINDOW_MS
    funding_complete = (
        isinstance(funding, dict)
        and funding.get("status") == "ok"
        and funding.get("snapshot_ref")
        and isinstance(funding.get("scan_received_at_ms"), int)
        and funding["scan_received_at_ms"] >= research_end_ms + 120_000
        and isinstance(funding.get("payload"), list)
    )
    try:
        mark_complete = all(
            isinstance(mark, dict)
            and mark.get("snapshot_ref")
            and isinstance(mark.get("received_at_ms"), int)
            and int(mark["received_at_ms"]) >= int(mark["event_at_ms"])
            for mark in marks
        )
    except (KeyError, TypeError, ValueError):
        mark_complete = False
    fee_assumption = {"kind": "research_fee_assumption_v1", "fee_bps_per_side": str(fee_bps_per_side)}
    fee_ref = hashlib.sha256(json.dumps(fee_assumption, sort_keys=True).encode()).hexdigest()
    try:
        result = evaluate_shadow(
            side=decision.side,
            decision_at_ms=int(entry["received_at_ms"]),
            scheduled_at_ms=int(entry["received_at_ms"]),
            decision_quote=entry,
            planned_quote=entry,
            exit_quotes=tuple(eligible),
            requested_notional_usdt=risk_usdt * 10_000 / Decimal(decision.exit_plan.stop_distance_bps),
            mark_rows=tuple(mark for mark in marks if isinstance(mark, dict)),
            mark_status="ok" if mark_complete else "partial",
            funding_events=tuple(funding["payload"]) if funding_complete else (),
            funding_coverage_complete=bool(funding_complete),
            exit_plan=decision.exit_plan,
            fee_bps_per_side=fee_bps_per_side,
            quote_environment=str(environment),
            target_environment=str(environment),
            instrument_rules=rules,
        )
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return _unevaluable_rule("root_market_tape_invalid")
    result.update(
        {
            "strategy_version": STRATEGY_VERSION,
            "rule_trigger_at_ms": decision.trigger_at_ms,
            "rule_visible_at_ms": decision.visible_at_ms,
            "rule_entry_delay_ms": int(entry["received_at_ms"]) - decision.trigger_at_ms,
            "entry_quote_ref": entry["quote_ref"],
            "decision_quote_ref": entry["quote_ref"],
            "quote_tape_ref": row.get("root_market_tape_ref"),
            "instrument_rules_ref": row.get("evidence_ref"),
            "mark_path_ref": row.get("root_market_tape_ref"),
            "funding_ref": funding.get("snapshot_ref") if isinstance(funding, dict) else None,
            "fee_ref": fee_ref,
            "fee_assumption": fee_assumption,
        }
    )
    return result


def _rule_receipt_archives_complete(
    files: AnalysisFiles,
    row: dict[str, Any],
    tape: dict[str, Any],
    receipt: dict[str, Any],
    missing: list[dict[str, str]],
) -> bool:
    case_id = str(row["case_id"])
    quote_fields = ("received_at_ms", "bid", "ask", "bid_quantity", "ask_quantity")
    for ref in (receipt["entry_quote_ref"], receipt["exit_quote_ref"]):
        snapshot = _read_ref(files, ref, missing, case_id, "rule_quote")
        sample = next(
            (item for item in tape["quotes"] if isinstance(item, dict) and item.get("quote_ref") == ref), None
        )
        payload = snapshot.get("payload") if isinstance(snapshot, dict) else None
        if (
            not isinstance(sample, dict)
            or not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], dict)
            or snapshot.get("status") != "ok"
            or any(payload[0].get(key) != sample.get(key) for key in quote_fields)
            or snapshot.get("native_symbol") != sample.get("native_symbol")
            or snapshot.get("environment") != sample.get("environment")
            or snapshot.get("mapping_semantics_digest") != sample.get("mapping_semantics_digest")
            or snapshot.get("units_per_contract") != sample.get("units_per_contract")
        ):
            missing.append({"case_id": case_id, "kind": "rule_quote", "reason": "snapshot_mismatch", "ref": ref})
            return False
    funding = tape["funding_history"]
    funding_snapshot = _read_ref(files, receipt["funding_ref"], missing, case_id, "rule_funding")
    if (
        not isinstance(funding_snapshot, dict)
        or funding_snapshot.get("status") != "ok"
        or funding_snapshot.get("payload") != funding.get("payload")
    ):
        missing.append(
            {"case_id": case_id, "kind": "rule_funding", "reason": "snapshot_mismatch", "ref": receipt["funding_ref"]}
        )
        return False
    entry_at_ms = int(receipt["entry_at_ms"])
    trigger_at_ms = int(receipt["mark_trigger_at_ms"])
    marks_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mark in tape["mark_bars"]:
        if entry_at_ms < int(mark["event_at_ms"]) <= trigger_at_ms:
            marks_by_ref[str(mark["snapshot_ref"])].append(mark)
    if not marks_by_ref:
        missing.append({"case_id": case_id, "kind": "rule_mark", "reason": "path_missing"})
        return False
    for ref, marks in marks_by_ref.items():
        snapshot = _read_ref(files, ref, missing, case_id, "rule_mark")
        payload = snapshot.get("payload") if isinstance(snapshot, dict) else None
        if not isinstance(payload, list) or not all(
            any(
                isinstance(item, dict)
                and all(item.get(key) == mark.get(key) for key in ("event_at_ms", "high", "low", "close"))
                for item in payload
            )
            for mark in marks
        ):
            missing.append({"case_id": case_id, "kind": "rule_mark", "reason": "snapshot_mismatch", "ref": ref})
            return False
    return True


def export_cases(
    conn: psycopg.Connection[Any],
    files: AnalysisFiles,
    *,
    start_ms: int,
    end_ms: int,
    rule_risk_usdt: Decimal | None = None,
    shadow_fee_bps_per_side: Decimal | None = None,
    max_spread_fraction_of_stop: Decimal | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if start_ms >= end_ms:
        raise ValueError("export_window_invalid")
    cases = conn.execute(
        """
        WITH roots AS (
            SELECT trigger_id FROM trading_cases
             WHERE run_kind='initial' AND created_at_ms >= %s AND created_at_ms < %s
        )
        SELECT c.case_id,c.trigger_id,c.target_asset_id,c.created_at_ms,c.run_kind,
               c.recheck_seq,c.state,c.target_selection,c.mapping_semantics_digest,
               c.root_expires_at_ms,c.evidence_ref,d.action AS decision_action,
               d.policy_version AS decision_policy_version,
               t.kind AS source_kind,t.source_fact_key,t.first_visible_at_ms,
               tape.tape_ref,w.status AS watch_status,
               w.last_observation_status AS watch_last_observation_status,
               w.last_observation_ref AS watch_observation_ref,
               w.child_case_id AS watch_child_case_id
          FROM trading_cases c
          JOIN roots r USING (trigger_id)
          JOIN trading_triggers t USING (trigger_id)
          LEFT JOIN trading_case_decisions d USING (case_id)
          LEFT JOIN trading_root_market_tapes tape ON tape.case_id=c.case_id
          LEFT JOIN trading_watch_observations w ON w.parent_case_id=c.case_id
         ORDER BY c.trigger_id,c.recheck_seq,c.created_at_ms,c.case_id
        """,
        (start_ms, end_ms),
    ).fetchall()
    if not cases:
        raise ValueError("export_window_empty")
    case_ids = [str(row["case_id"]) for row in cases]
    attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in conn.execute(
        "SELECT case_id,claim_attempt,brief_ref,evidence_ref,analysis_status,error_code,"
        "physical_call_count,cost_microusd,known_cost_microusd,unknown_cost_calls "
        "FROM trading_case_attempts WHERE case_id=ANY(%s) ORDER BY case_id,claim_attempt",
        (case_ids,),
    ).fetchall():
        attempts[str(attempt["case_id"])].append(dict(attempt))
    evaluations: dict[str, dict[str, Any]] = defaultdict(dict)
    for evaluation in conn.execute(
        "SELECT case_id,source,status,result FROM trading_case_evaluations "
        "WHERE case_id=ANY(%s) AND source='shadow_simulation'",
        (case_ids,),
    ).fetchall():
        if isinstance(evaluation["result"], dict):
            evaluations[str(evaluation["case_id"])]["dspy"] = evaluation["result"]
    missing: list[dict[str, str]] = []
    exported: list[dict[str, Any]] = []
    complete_paths = simulated_rule_receipts = 0
    for case in cases:
        case_id = str(case["case_id"])
        first_complete = next(
            (item for item in attempts[case_id] if item.get("evidence_ref") and item.get("brief_ref")),
            None,
        )
        evidence_ref = first_complete["evidence_ref"] if first_complete else case["evidence_ref"]
        evidence = _read_ref(files, evidence_ref, missing, case_id, "evidence")
        row: dict[str, Any] = {
            "case_id": case_id,
            "root_trigger_id": str(case["trigger_id"]),
            "source_group_id": f"{case['source_kind']}:{case['source_fact_key']}",
            "asset_id": case["target_asset_id"],
            "source_kind": case["source_kind"],
            "created_at_ms": int(case["created_at_ms"]),
            "run_kind": case["run_kind"],
            "recheck_seq": case["recheck_seq"],
            "state": case["state"],
            "decision_action": case["decision_action"],
            "decision_policy_version": case["decision_policy_version"],
            "watch_status": case["watch_status"],
            "watch_last_observation_status": case["watch_last_observation_status"],
            "watch_observation_ref": case["watch_observation_ref"],
            "watch_child_case_id": case["watch_child_case_id"],
            "mapping_semantics_digest": case["mapping_semantics_digest"],
            "root_expires_at_ms": case["root_expires_at_ms"],
            "target_selection": case["target_selection"],
            "evidence_ref": evidence_ref,
            "evidence": evidence,
            "attempts": attempts[case_id],
            "arm_evaluations": evaluations[case_id],
        }
        if case["run_kind"] == "initial":
            if case["decision_action"] == "WATCH":
                row["watch_observation"] = _read_ref(
                    files, case["watch_observation_ref"], missing, case_id, "watch_observation"
                )
            tape = _read_ref(files, case["tape_ref"], missing, case_id, "root_market_tape")
            row["root_market_tape_ref"] = case["tape_ref"]
            if isinstance(tape, dict) and isinstance(evidence, dict):
                selected = case["target_selection"] or {}
                instrument = selected.get("instrument") if isinstance(selected, dict) else None
                if (
                    isinstance(instrument, dict)
                    and tape.get("case_id") == case_id
                    and tape.get("mapping_semantics_digest") == case["mapping_semantics_digest"]
                    and tape.get("root_accepted_at_ms") == int(case["created_at_ms"])
                    and tape.get("native_symbol") == instrument.get("native_symbol")
                    and tape.get("environment") == instrument.get("environment")
                ):
                    path, status = _rule_watch_path(evidence, tape, int(case["root_expires_at_ms"]))
                else:
                    path, status = [], "identity_mismatch"
                row["rule_watch_bars"] = path
                row["rule_watch_status"] = status
                complete_paths += status == "complete"
            else:
                row["rule_watch_status"] = "missing"
            decision = _rule_decision([row])
            if decision.action == "TRADE":
                rule_receipt = (
                    _rule_shadow_receipt(
                        row,
                        tape,
                        decision,
                        risk_usdt=rule_risk_usdt,
                        fee_bps_per_side=shadow_fee_bps_per_side,
                        max_spread_fraction_of_stop=max_spread_fraction_of_stop,
                    )
                    if isinstance(tape, dict)
                    else _unevaluable_rule("root_market_tape_missing")
                )
                if rule_receipt["status"] == "simulated" and not _rule_receipt_archives_complete(
                    files, row, tape, rule_receipt, missing
                ):
                    rule_receipt = _unevaluable_rule("rule_receipt_archive_incomplete")
                row["arm_evaluations"]["rule"] = rule_receipt
                simulated_rule_receipts += rule_receipt["status"] == "simulated"
        exported.append(row)
    manifest = {
        "export_version": "trading_cohort_export_v1",
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "root_count": sum(row["run_kind"] == "initial" for row in exported),
        "case_count": len(exported),
        "complete_rule_watch_paths": complete_paths,
        "missing_archive_items": missing,
        "source_group_rule": "source_kind:source_fact_key",
        "simulated_rule_receipts": simulated_rule_receipts,
        "rule_evaluation_assumptions": {
            "rule_risk_usdt": None if rule_risk_usdt is None else str(rule_risk_usdt),
            "shadow_fee_bps_per_side": None if shadow_fee_bps_per_side is None else str(shadow_fee_bps_per_side),
            "max_spread_fraction_of_stop": (
                None if max_spread_fraction_of_stop is None else str(max_spread_fraction_of_stop)
            ),
        },
    }
    return exported, manifest


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", help="PostgreSQL DSN; defaults to TRADING_RESEARCH_DSN")
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--start-ms", type=int, required=True)
    parser.add_argument("--end-ms", type=int, required=True)
    parser.add_argument("--rule-risk-usdt", type=Decimal)
    parser.add_argument("--shadow-fee-bps-per-side", type=Decimal)
    parser.add_argument("--max-spread-fraction-of-stop", type=Decimal)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    dsn = args.dsn or os.environ.get("TRADING_RESEARCH_DSN")
    if not dsn:
        parser.error("--dsn or TRADING_RESEARCH_DSN is required")
    if args.output.resolve() == args.manifest.resolve():
        parser.error("output and manifest must be different files")
    with psycopg.connect(dsn, row_factory=dict_row, options="-c default_transaction_read_only=on") as conn:
        rows, manifest = export_cases(
            conn,
            AnalysisFiles(args.archive_root),
            start_ms=args.start_ms,
            end_ms=args.end_ms,
            rule_risk_usdt=args.rule_risk_usdt,
            shadow_fee_bps_per_side=args.shadow_fee_bps_per_side,
            max_spread_fraction_of_stop=args.max_spread_fraction_of_stop,
        )
    data = b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8") for row in rows
    )
    manifest["cases_sha256"] = hashlib.sha256(data).hexdigest()
    _write_private(args.output, data)
    _write_private(args.manifest, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
