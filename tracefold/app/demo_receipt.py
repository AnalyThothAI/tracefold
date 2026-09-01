"""Strict read-only Binance Demo closure receipt."""

from __future__ import annotations

from typing import Any

from tracefold.trading.storage.execution_stream import ExecutionRuntimeState


class DemoReceiptError(ValueError):
    pass


def verify_binance_demo_receipt(
    *,
    state: ExecutionRuntimeState | None,
    observations: list[dict[str, Any]],
    entry_command_id: str,
    flatten_command_id: str,
    now_ns: int,
) -> dict[str, Any]:
    if state is None or state.mode != "paper" or not state.ready or not state.account_flat:
        raise DemoReceiptError("binance_demo_current_runtime_not_ready_flat")
    if now_ns - state.heartbeat_at_ns > 5_000_000_000:
        raise DemoReceiptError("binance_demo_current_runtime_stale")
    entry = [row for row in observations if row.get("command_id") == entry_command_id]
    flatten = [row for row in observations if row.get("command_id") == flatten_command_id]
    accepted = _require(
        entry,
        kind="control_disposition",
        predicate=lambda row: row["summary"].get("disposition") == "accepted",
        reason="binance_demo_entry_not_accepted",
    )
    entry_order = _require(
        entry,
        kind="order",
        predicate=lambda row: (
            row["summary"].get("leg") == "entry"
            and row["summary"].get("status") == "accepted"
            and len(row["native_identity_references"]) >= 2
        ),
        reason="binance_demo_entry_order_not_venue_accepted",
    )
    fill = _require(
        entry,
        kind="fill",
        predicate=lambda row: row["summary"].get("leg") == "entry" and len(row["native_identity_references"]) >= 2,
        reason="binance_demo_entry_fill_missing",
    )
    explicit_protection = _require(
        entry,
        kind="protection",
        predicate=lambda row: (
            row["summary"].get("reduce_only") is True and bool(row["summary"].get("explicit_quantity"))
        ),
        reason="binance_demo_explicit_reduce_only_protection_missing",
    )
    protection_accepted = _require(
        entry,
        kind="protection",
        predicate=lambda row: (
            row["summary"].get("status") == "accepted" and len(row["native_identity_references"]) >= 2
        ),
        reason="binance_demo_protection_not_venue_accepted",
    )
    exit_order = _require(
        entry,
        kind="order",
        predicate=lambda row: (
            row["summary"].get("leg") == "exit"
            and row["summary"].get("status") == "accepted"
            and len(row["native_identity_references"]) >= 2
        ),
        reason="binance_demo_exit_not_venue_accepted",
    )
    flat = _require(
        flatten,
        kind="control_disposition",
        predicate=lambda row: (
            row["summary"].get("disposition") == "completed" and row["summary"].get("reason") == "binance_account_flat"
        ),
        reason="binance_demo_flatten_not_completed",
    )
    reconciliations = [
        row
        for row in observations
        if row.get("runtime_profile_id") == state.runtime_profile_id
        and row.get("normalized_kind") == "reconciliation"
        and row.get("summary", {}).get("source") == "binance_private_api"
    ]
    final_reconciliation = _require(
        reconciliations,
        kind="reconciliation",
        predicate=lambda row: (
            row["observed_at_ns"] >= flat["observed_at_ns"] and row["summary"].get("account_flat") is True
        ),
        reason="binance_demo_authoritative_flat_missing",
    )
    starts = sorted(
        (
            row
            for row in observations
            if row.get("runtime_profile_id") == state.runtime_profile_id
            and row.get("normalized_kind") == "readiness"
            and row.get("summary", {}).get("lifecycle") == "started"
        ),
        key=lambda row: row["observed_at_ns"],
    )
    if not any(row["observed_at_ns"] <= accepted["observed_at_ns"] for row in starts) or not any(
        row["observed_at_ns"] > fill["observed_at_ns"] for row in starts
    ):
        raise DemoReceiptError("binance_demo_restart_receipt_missing")
    latest_start = starts[-1]
    expected_identity = {
        "runtime_id": str(state.runtime_id),
        "runtime_revision": state.runtime_revision,
        "image_digest": state.image_digest,
        "config_sha256": state.config_sha256,
        "credential_fingerprint": state.credential_fingerprint,
    }
    if any(latest_start["summary"].get(key) != value for key, value in expected_identity.items()):
        raise DemoReceiptError("binance_demo_runtime_identity_mismatch")
    evidence = (entry_order, fill, explicit_protection, protection_accepted, exit_order, flat, final_reconciliation)
    return {
        "mode": state.mode,
        "runtime_profile_id": state.runtime_profile_id,
        "runtime_release": state.runtime_release,
        **expected_identity,
        "entry_command_id": entry_command_id,
        "flatten_command_id": flatten_command_id,
        "runtime_start_event_ids": [row["event_id"] for row in starts],
        "evidence_event_ids": [row["event_id"] for row in evidence],
        "venue_native_references": sorted(
            {reference for row in evidence for reference in row["native_identity_references"]}
        ),
        "authoritative_flat_observed_at_ns": final_reconciliation["observed_at_ns"],
        "truth": "binance_demo_only_not_live_money",
    }


def _require(
    rows: list[dict[str, Any]],
    *,
    kind: str,
    predicate: Any,
    reason: str,
) -> dict[str, Any]:
    matches = [row for row in rows if row.get("normalized_kind") == kind and predicate(row)]
    if not matches:
        raise DemoReceiptError(reason)
    return max(matches, key=lambda row: (row["observed_at_ns"], row["event_id"]))


__all__ = ["DemoReceiptError", "verify_binance_demo_receipt"]
