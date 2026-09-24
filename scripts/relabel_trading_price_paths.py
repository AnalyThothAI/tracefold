"""Append verified endpoint v2 labels beside settled historical v1 labels.

Legacy paths archived selected endpoint values and clocks, not full bar paths.
The pinned v1 adapter accepted only closed bars from Binance mainnet. This
command validates those archived endpoints without refetching today's history.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from typing import Any

from tracefold.app.analysis_files import AnalysisFiles
from tracefold.app.repository_session import RepositorySession, repositories
from tracefold.platform.config.loader import load_settings

_BAR_MS = 60_000
_V1_ADAPTER_ORIGIN = "f1ef42091"


def _missing(reason: str) -> dict[str, Any]:
    return {"status": "missing", "reason": reason, "historical_quality": "unverifiable"}


def _audit_v1_path(row: dict[str, Any], path: Any) -> dict[str, Any]:
    """Recompute only the gross endpoint label proven by the v1 archive."""
    if row["old_status"] != "ok":
        return _missing("historical_v1_missing")
    if not isinstance(path, dict):
        return _missing("historical_v1_archive_missing")
    try:
        anchor = int(row["source_observed_at_ms"]) if row["axis"] == "source" else int(row["decided_at_ms"]) + _BAR_MS
        horizon = int(row["horizon_seconds"])
        start_at = ((anchor + _BAR_MS - 1) // _BAR_MS) * _BAR_MS
        end_at = ((anchor + horizon * 1_000 + _BAR_MS - 1) // _BAR_MS) * _BAR_MS
        if (
            path.get("case_id") != str(row["case_id"])
            or path.get("axis") != row["axis"]
            or int(path["horizon_seconds"]) != horizon
            or path.get("version") != "price_path_v1"
            or path.get("label_version") != "price_path_v1"
            or path.get("status") != "ok"
        ):
            return _missing("historical_v1_identity_mismatch")
        if (
            int(path["axis_anchor_ms"]) != anchor
            or int(path["target_ms"]) != anchor + horizon * 1_000
            or int(path["start_close_at_ms"]) != start_at
            or int(path["end_close_at_ms"]) != end_at
            or start_at >= end_at
        ):
            return _missing("historical_endpoint_mismatch")
        first = Decimal(str(path["start_price"]))
        last = Decimal(str(path["end_price"]))
        recorded = Decimal(str(path["return_bps"]))
        settled = Decimal(str(row["old_return_bps"]))
        if not all(value.is_finite() for value in (first, last, recorded, settled)) or first <= 0 or last <= 0:
            return _missing("historical_price_invalid")
        calculated = (last / first - 1) * 10_000
        if abs(recorded - calculated) > Decimal("0.0000001") or abs(settled - calculated) > Decimal("0.0000001"):
            return _missing("historical_arithmetic_mismatch")
        instrument = row["target_selection"]["instrument"]
        receipts = path["request_receipts"]
        if (
            path.get("source_identity") != "binance_public_v1"
            or path.get("source_version") != "binance_public_v1"
            or path.get("unit_definition") != "native_quote_v1"
            or path.get("measure") != "underlying_close_to_close_gross"
            or path.get("execution_claim") is not False
            or path.get("costs_included") is not False
            or path.get("market_status") not in ("ok", "partial")
            or int(path["received_at_ms"]) < end_at
            or not isinstance(receipts, list)
            or not receipts
            or any(not isinstance(receipt, dict) for receipt in receipts)
            or {receipt["native_symbol"] for receipt in receipts} != {instrument["native_symbol"]}
            or any(receipt.get("endpoint") not in ("cache", "/fapi/v1/klines") for receipt in receipts)
        ):
            return _missing("historical_market_identity_unverified")
    except (InvalidOperation, KeyError, TypeError, ValueError, ZeroDivisionError):
        return _missing("historical_v1_archive_invalid")
    return {
        "status": "ok",
        "version": "price_path_v2",
        "axis_anchor_ms": anchor,
        "target_ms": anchor + horizon * 1_000,
        "start_close_at_ms": start_at,
        "end_close_at_ms": end_at,
        "start_price": str(first),
        "end_price": str(last),
        "return_bps": str(calculated),
        "measure": "underlying_close_to_close_gross",
        "execution_claim": False,
        "costs_included": False,
        "historical_quality": "verified_endpoint_only",
        "full_path_quality": "unknown",
        "data_environment": "live",
        "data_environment_provenance": f"pinned_binance_public_v1_adapter_{_V1_ADAPTER_ORIGIN}",
        "source_identity": "binance_public_v1",
        "unit_definition": "native_quote_v1",
        "received_at_ms": int(path["received_at_ms"]),
    }


def _pending_corrections(repos: RepositorySession, *, limit: int) -> list[dict[str, Any]]:
    rows = repos.conn.execute(
        """
        SELECT old.case_id,old.axis,old.horizon_seconds,old.status AS old_status,
               old.return_bps AS old_return_bps,old.path_ref AS old_path_ref,
               c.source_observed_at_ms,c.decided_at_ms,c.target_selection
          FROM trading_case_outcomes old
          JOIN trading_case_outcomes newer
            ON newer.case_id=old.case_id AND newer.axis=old.axis
           AND newer.horizon_seconds=old.horizon_seconds
          JOIN trading_cases c ON c.case_id=old.case_id
         WHERE old.label_version='price_path_v1'
           AND newer.label_version='price_path_v2' AND newer.status='pending'
         ORDER BY old.case_id,old.axis,old.horizon_seconds
         LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def relabel(*, batch_size: int, max_batches: int) -> dict[str, Any]:
    settings = load_settings(require_ws_token=False)
    files = AnalysisFiles(settings.app_home / "archive" / "trading-analysis")
    queued = processed = 0
    reasons: Counter[str] = Counter()
    verified = unverifiable = 0
    with repositories(settings, application_name="tracefold_v1_path_audit") as repos:
        for _ in range(max_batches):
            with repos.transaction():
                queued += repos.trading.queue_price_path_v2_corrections(limit=batch_size)
            rows = _pending_corrections(repos, limit=batch_size)
            if not rows:
                break
            for row in rows:
                old_path = None
                if row["old_path_ref"]:
                    with suppress(OSError, ValueError, TypeError, json.JSONDecodeError):
                        old_path = files.read(row["old_path_ref"])
                audit = _audit_v1_path(row, old_path)
                now_ms = int(time.time() * 1_000)
                correction = {
                    **audit,
                    "case_id": str(row["case_id"]),
                    "axis": row["axis"],
                    "horizon_seconds": row["horizon_seconds"],
                    "label_version": "price_path_v2",
                    "source_label_version": "price_path_v1",
                    "source_path_ref": row["old_path_ref"],
                    "labeled_at_ms": now_ms,
                }
                ref = files.write(correction)
                with repos.transaction():
                    settled = repos.trading.settle_analysis_outcome(
                        case_id=str(row["case_id"]),
                        axis=row["axis"],
                        horizon_seconds=row["horizon_seconds"],
                        label_version="price_path_v2",
                        status=audit["status"],
                        return_bps=audit.get("return_bps"),
                        path_ref=ref,
                        now_ms=now_ms,
                    )
                if settled:
                    processed += 1
                    if audit["status"] == "ok":
                        verified += 1
                    else:
                        unverifiable += 1
                        reasons[audit["reason"]] += 1
        pending = repos.conn.execute(
            """
            SELECT count(*) FROM trading_case_outcomes old
              JOIN trading_case_outcomes newer
                ON newer.case_id=old.case_id AND newer.axis=old.axis
               AND newer.horizon_seconds=old.horizon_seconds
             WHERE old.label_version='price_path_v1'
               AND newer.label_version='price_path_v2' AND newer.status='pending'
            """
        ).fetchone()[0]
    return {
        "queued": queued,
        "processed": processed,
        "pending": int(pending),
        "verified_endpoint_labels": verified,
        "unverifiable": unverifiable,
        "unverifiable_reasons": dict(sorted(reasons.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-batches", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 128 or not 1 <= args.max_batches <= 10_000:
        parser.error("batch bounds invalid")
    print(json.dumps(relabel(batch_size=args.batch_size, max_batches=args.max_batches), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
