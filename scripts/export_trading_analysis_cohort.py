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

from tracefold.app.analysis_files import AnalysisFiles

_BAR_MS = 60_000


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
    if tape.get("version") != "root_research_tape_v1" or not isinstance(tape.get("closed_bars"), list):
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


def export_cases(
    conn: psycopg.Connection[Any], files: AnalysisFiles, *, start_ms: int, end_ms: int
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
               t.kind AS source_kind,t.source_fact_key,t.first_visible_at_ms,
               tape.tape_ref
          FROM trading_cases c
          JOIN roots r USING (trigger_id)
          JOIN trading_triggers t USING (trigger_id)
          LEFT JOIN trading_case_decisions d USING (case_id)
          LEFT JOIN trading_root_market_tapes tape ON tape.case_id=c.case_id
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
    complete_paths = 0
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
            "mapping_semantics_digest": case["mapping_semantics_digest"],
            "root_expires_at_ms": case["root_expires_at_ms"],
            "target_selection": case["target_selection"],
            "evidence_ref": evidence_ref,
            "evidence": evidence,
            "attempts": attempts[case_id],
            "arm_evaluations": evaluations[case_id],
        }
        if case["run_kind"] == "initial":
            tape = _read_ref(files, case["tape_ref"], missing, case_id, "root_market_tape")
            row["root_market_tape_ref"] = case["tape_ref"]
            if isinstance(tape, dict) and isinstance(evidence, dict):
                if (
                    tape.get("case_id") == case_id
                    and tape.get("mapping_semantics_digest") == case["mapping_semantics_digest"]
                ):
                    path, status = _rule_watch_path(evidence, tape, int(case["root_expires_at_ms"]))
                else:
                    path, status = [], "identity_mismatch"
                row["rule_watch_bars"] = path
                row["rule_watch_status"] = status
                complete_paths += status == "complete"
            else:
                row["rule_watch_status"] = "missing"
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
        "rule_arm_net_receipts": "not_collected_by_export",
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
            conn, AnalysisFiles(args.archive_root), start_ms=args.start_ms, end_ms=args.end_ms
        )
    data = b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8") for row in rows
    )
    manifest["cases_sha256"] = hashlib.sha256(data).hexdigest()
    _write_private(args.output, data)
    _write_private(args.manifest, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
