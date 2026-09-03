from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

import pytest

from tracefold.trading.demo_receipt import (
    DemoReceiptError,
    DemoReceiptObservation,
    verify_binance_demo_receipt,
)
from tracefold.trading.storage.execution_stream import ExecutionRuntimeState

ENTRY = "1" * 64
FLATTEN = "2" * 64
RESUME = "3" * 64


def _state() -> ExecutionRuntimeState:
    return ExecutionRuntimeState(
        account_slot="binance_usdm_primary",
        mode="paper",
        runtime_release="nautilus-1.231.0+oi-v1",
        config_sha256="a" * 64,
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
        runtime_revision="b" * 40,
        image_digest="sha256:" + "c" * 64,
        credential_fingerprint="d" * 64,
        lifecycle_state="running",
        alive=True,
        execution_safe=True,
        entries_armed=False,
        startup_reconciled=True,
        unexpected_exposure=False,
        account_flat=True,
        positions_count=0,
        open_orders_count=0,
        protection_status="not_applicable",
        reconciliation_observed_at_ns=900,
        heartbeat_at_ns=1_000,
        entry_block_reason="entries_paused",
        started_at_ns=800,
        updated_at_ns=1_000,
    )


def _row(
    event: str,
    kind: str,
    at: int,
    *,
    command_id: str | None = None,
    summary: Mapping[str, object] | None = None,
    refs: tuple[str, ...] = (),
) -> DemoReceiptObservation:
    values = dict(summary or {})
    return DemoReceiptObservation(
        event_id=event * 64,
        account_slot="binance_usdm_primary",
        command_id=command_id,
        normalized_kind=kind,
        observed_at_ns=at,
        native_identity_references=refs,
        action=values.get("action") if isinstance(values.get("action"), str) else None,
        disposition=values.get("disposition") if isinstance(values.get("disposition"), str) else None,
        reason=values.get("reason") if isinstance(values.get("reason"), str) else None,
        leg=values.get("leg") if isinstance(values.get("leg"), str) else None,
        status=values.get("status") if isinstance(values.get("status"), str) else None,
        reduce_only=values.get("reduce_only") if isinstance(values.get("reduce_only"), bool) else None,
        explicit_quantity=(
            values.get("explicit_quantity") if isinstance(values.get("explicit_quantity"), str) else None
        ),
        source=values.get("source") if isinstance(values.get("source"), str) else None,
        lifecycle=values.get("lifecycle") if isinstance(values.get("lifecycle"), str) else None,
        runtime_id=values.get("runtime_id") if isinstance(values.get("runtime_id"), str) else None,
        runtime_revision=(values.get("runtime_revision") if isinstance(values.get("runtime_revision"), str) else None),
        image_digest=values.get("image_digest") if isinstance(values.get("image_digest"), str) else None,
        config_sha256=values.get("config_sha256") if isinstance(values.get("config_sha256"), str) else None,
        credential_fingerprint=(
            values.get("credential_fingerprint") if isinstance(values.get("credential_fingerprint"), str) else None
        ),
    )


def _observations() -> list[DemoReceiptObservation]:
    identity = {
        "lifecycle": "started",
        "runtime_id": "11111111-1111-4111-8111-111111111111",
        "runtime_revision": "b" * 40,
        "image_digest": "sha256:" + "c" * 64,
        "config_sha256": "a" * 64,
        "credential_fingerprint": "d" * 64,
    }
    return [
        _row("a", "readiness", 100, summary=identity | {"runtime_id": "old-runtime"}),
        _row(
            "6",
            "control_disposition",
            150,
            command_id=RESUME,
            summary={"action": "resume_entries", "disposition": "accepted"},
        ),
        _row(
            "b",
            "control_disposition",
            200,
            command_id=ENTRY,
            summary={"disposition": "accepted"},
        ),
        _row(
            "c",
            "order",
            300,
            command_id=ENTRY,
            summary={"leg": "entry", "status": "accepted"},
            refs=("entry-client", "entry-venue"),
        ),
        _row(
            "d",
            "fill",
            400,
            command_id=ENTRY,
            summary={"leg": "entry"},
            refs=("entry-venue", "trade-1"),
        ),
        _row(
            "e",
            "protection",
            450,
            command_id=ENTRY,
            summary={"reduce_only": True, "explicit_quantity": "0.01"},
            refs=("stop-client",),
        ),
        _row(
            "f",
            "protection",
            500,
            command_id=ENTRY,
            summary={"status": "accepted"},
            refs=("stop-client", "stop-venue"),
        ),
        _row("7", "readiness", 600, summary=identity),
        _row(
            "8",
            "order",
            700,
            command_id=ENTRY,
            summary={"leg": "exit", "status": "accepted"},
            refs=("exit-client", "exit-venue"),
        ),
        _row(
            "9",
            "control_disposition",
            800,
            command_id=FLATTEN,
            summary={"disposition": "completed", "reason": "binance_account_flat"},
        ),
    ]


def test_demo_receipt_requires_entry_restart_exit_and_authoritative_flat() -> None:
    receipt = verify_binance_demo_receipt(
        state=_state(),
        observations=_observations(),
        entry_command_id=ENTRY,
        flatten_command_id=FLATTEN,
        now_ns=1_000,
    )

    assert receipt.truth == "binance_demo_only_not_live_money"
    assert receipt.runtime_start_event_ids == ("a" * 64, "7" * 64)
    assert {"entry-venue", "trade-1", "stop-venue", "exit-venue"} <= set(receipt.venue_native_references)


def test_demo_receipt_accepts_private_reconciliation_as_protection_venue_receipt() -> None:
    observations = [row for row in _observations() if row.event_id != "f" * 64]
    observations.append(
        _row(
            "5",
            "reconciliation",
            550,
            summary={"source": "binance_private_api"},
            refs=("stop-client", "stop-venue"),
        )
    )

    receipt = verify_binance_demo_receipt(
        state=_state(),
        observations=observations,
        entry_command_id=ENTRY,
        flatten_command_id=FLATTEN,
        now_ns=1_000,
    )

    assert "stop-venue" in receipt.venue_native_references


def test_demo_receipt_reads_the_flat_proof_from_the_projection_not_a_heartbeat_observation() -> None:
    """Steady reconciliation stopped appending a row per cycle in #510 PR-1.

    The account being flat, and the clock that proved it, live in `trading_execution_runtime_state`,
    which the private reconciliation refreshes every loop. Requiring a later `reconciliation`
    observation would now make the receipt unobtainable on a flat, unchanging account.
    """

    receipt = verify_binance_demo_receipt(
        state=_state(),
        observations=_observations(),
        entry_command_id=ENTRY,
        flatten_command_id=FLATTEN,
        now_ns=1_000,
    )
    assert receipt.authoritative_flat_observed_at_ns == 900
    assert all(not event_id.startswith("0") for event_id in receipt.evidence_event_ids)

    with pytest.raises(DemoReceiptError, match="binance_demo_authoritative_flat_missing"):
        verify_binance_demo_receipt(
            state=replace(_state(), reconciliation_observed_at_ns=799),
            observations=_observations(),
            entry_command_id=ENTRY,
            flatten_command_id=FLATTEN,
            now_ns=1_000,
        )


def test_demo_receipt_rejects_flat_without_a_post_fill_restart() -> None:
    observations = [row for row in _observations() if row.event_id != "7" * 64]

    with pytest.raises(DemoReceiptError, match="binance_demo_restart_receipt_missing"):
        verify_binance_demo_receipt(
            state=_state(),
            observations=observations,
            entry_command_id=ENTRY,
            flatten_command_id=FLATTEN,
            now_ns=1_000,
        )


def test_demo_receipt_rejects_entry_without_an_explicit_resume() -> None:
    observations = [row for row in _observations() if row.command_id != RESUME]

    with pytest.raises(DemoReceiptError, match="binance_demo_explicit_resume_missing"):
        verify_binance_demo_receipt(
            state=_state(),
            observations=observations,
            entry_command_id=ENTRY,
            flatten_command_id=FLATTEN,
            now_ns=1_000,
        )
