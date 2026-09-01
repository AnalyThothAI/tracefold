"""Strict Binance Demo closure meaning over typed durable Trading facts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from .storage.execution_stream import ExecutionRuntimeState


class DemoReceiptError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DemoReceiptObservation:
    event_id: str
    runtime_profile_id: str
    command_id: str | None
    normalized_kind: str
    observed_at_ns: int
    native_identity_references: tuple[str, ...]
    action: str | None = None
    disposition: str | None = None
    reason: str | None = None
    leg: str | None = None
    status: str | None = None
    reduce_only: bool | None = None
    explicit_quantity: str | None = None
    source: str | None = None
    account_flat: bool | None = None
    lifecycle: str | None = None
    runtime_id: str | None = None
    runtime_revision: str | None = None
    image_digest: str | None = None
    config_sha256: str | None = None
    credential_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class BinanceDemoReceipt:
    mode: Literal["paper"]
    runtime_profile_id: str
    runtime_release: str
    runtime_id: str
    runtime_revision: str
    image_digest: str
    config_sha256: str
    credential_fingerprint: str
    entry_command_id: str
    flatten_command_id: str
    runtime_start_event_ids: tuple[str, ...]
    evidence_event_ids: tuple[str, ...]
    venue_native_references: tuple[str, ...]
    authoritative_flat_observed_at_ns: int
    truth: Literal["binance_demo_only_not_live_money"] = "binance_demo_only_not_live_money"


def verify_binance_demo_receipt(
    *,
    state: ExecutionRuntimeState | None,
    observations: Sequence[DemoReceiptObservation],
    entry_command_id: str,
    flatten_command_id: str,
    now_ns: int,
) -> BinanceDemoReceipt:
    if state is None or state.mode != "paper" or not state.ready or not state.account_flat:
        raise DemoReceiptError("binance_demo_current_runtime_not_ready_flat")
    if now_ns - state.heartbeat_at_ns > 5_000_000_000:
        raise DemoReceiptError("binance_demo_current_runtime_stale")
    entry = [row for row in observations if row.command_id == entry_command_id]
    flatten = [row for row in observations if row.command_id == flatten_command_id]
    accepted = _require(
        entry,
        kind="control_disposition",
        predicate=lambda row: row.disposition == "accepted",
        reason="binance_demo_entry_not_accepted",
    )
    entry_order = _require(
        entry,
        kind="order",
        predicate=lambda row: (
            row.leg == "entry" and row.status == "accepted" and len(row.native_identity_references) >= 2
        ),
        reason="binance_demo_entry_order_not_venue_accepted",
    )
    fill = _require(
        entry,
        kind="fill",
        predicate=lambda row: row.leg == "entry" and len(row.native_identity_references) >= 2,
        reason="binance_demo_entry_fill_missing",
    )
    explicit_protection = _require(
        entry,
        kind="protection",
        predicate=lambda row: row.reduce_only is True and bool(row.explicit_quantity),
        reason="binance_demo_explicit_reduce_only_protection_missing",
    )
    accepted_protections = [
        row
        for row in entry
        if row.normalized_kind == "protection" and row.status == "accepted" and len(row.native_identity_references) >= 2
    ]
    protection_accepted = (
        max(accepted_protections, key=lambda row: (row.observed_at_ns, row.event_id))
        if accepted_protections
        else _require(
            observations,
            kind="reconciliation",
            predicate=lambda row: (
                row.runtime_profile_id == state.runtime_profile_id
                and row.source == "binance_private_api"
                and row.observed_at_ns >= explicit_protection.observed_at_ns
                and len(row.native_identity_references) >= 2
                and bool(set(row.native_identity_references) & set(explicit_protection.native_identity_references))
            ),
            reason="binance_demo_protection_not_venue_accepted",
        )
    )
    exit_order = _require(
        entry,
        kind="order",
        predicate=lambda row: (
            row.leg == "exit" and row.status == "accepted" and len(row.native_identity_references) >= 2
        ),
        reason="binance_demo_exit_not_venue_accepted",
    )
    flat = _require(
        flatten,
        kind="control_disposition",
        predicate=lambda row: row.disposition == "completed" and row.reason == "binance_account_flat",
        reason="binance_demo_flatten_not_completed",
    )
    reconciliations = [
        row
        for row in observations
        if row.runtime_profile_id == state.runtime_profile_id
        and row.normalized_kind == "reconciliation"
        and row.source == "binance_private_api"
    ]
    final_reconciliation = _require(
        reconciliations,
        kind="reconciliation",
        predicate=lambda row: row.observed_at_ns >= flat.observed_at_ns and row.account_flat is True,
        reason="binance_demo_authoritative_flat_missing",
    )
    starts = sorted(
        (
            row
            for row in observations
            if row.runtime_profile_id == state.runtime_profile_id
            and row.normalized_kind == "readiness"
            and row.lifecycle == "started"
        ),
        key=lambda row: row.observed_at_ns,
    )
    resume = _require(
        observations,
        kind="control_disposition",
        predicate=lambda row: (
            row.runtime_profile_id == state.runtime_profile_id
            and row.action == "resume_entries"
            and row.disposition == "accepted"
            and row.observed_at_ns <= accepted.observed_at_ns
            and any(start.observed_at_ns <= row.observed_at_ns for start in starts)
        ),
        reason="binance_demo_explicit_resume_missing",
    )
    if not any(row.observed_at_ns <= accepted.observed_at_ns for row in starts) or not any(
        row.observed_at_ns > fill.observed_at_ns for row in starts
    ):
        raise DemoReceiptError("binance_demo_restart_receipt_missing")
    latest_start = starts[-1]
    if (
        latest_start.runtime_id != str(state.runtime_id)
        or latest_start.runtime_revision != state.runtime_revision
        or latest_start.image_digest != state.image_digest
        or latest_start.config_sha256 != state.config_sha256
        or latest_start.credential_fingerprint != state.credential_fingerprint
    ):
        raise DemoReceiptError("binance_demo_runtime_identity_mismatch")
    evidence = (
        resume,
        entry_order,
        fill,
        explicit_protection,
        protection_accepted,
        exit_order,
        flat,
        final_reconciliation,
    )
    return BinanceDemoReceipt(
        mode="paper",
        runtime_profile_id=state.runtime_profile_id,
        runtime_release=state.runtime_release,
        runtime_id=str(state.runtime_id),
        runtime_revision=state.runtime_revision,
        image_digest=state.image_digest,
        config_sha256=state.config_sha256,
        credential_fingerprint=state.credential_fingerprint,
        entry_command_id=entry_command_id,
        flatten_command_id=flatten_command_id,
        runtime_start_event_ids=tuple(row.event_id for row in starts),
        evidence_event_ids=tuple(row.event_id for row in evidence),
        venue_native_references=tuple(
            sorted({reference for row in evidence for reference in row.native_identity_references})
        ),
        authoritative_flat_observed_at_ns=final_reconciliation.observed_at_ns,
    )


def _require(
    rows: Sequence[DemoReceiptObservation],
    *,
    kind: str,
    predicate: Callable[[DemoReceiptObservation], bool],
    reason: str,
) -> DemoReceiptObservation:
    matches = [row for row in rows if row.normalized_kind == kind and predicate(row)]
    if not matches:
        raise DemoReceiptError(reason)
    return max(matches, key=lambda row: (row.observed_at_ns, row.event_id))


__all__ = [
    "BinanceDemoReceipt",
    "DemoReceiptError",
    "DemoReceiptObservation",
    "verify_binance_demo_receipt",
]
