from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

import pytest

from tracefold.app.demo_receipt import DemoReceiptError, verify_binance_demo_receipt
from tracefold.trading.storage.execution_stream import ExecutionRuntimeState

ENTRY = "1" * 64
FLATTEN = "2" * 64


def _state() -> ExecutionRuntimeState:
    return ExecutionRuntimeState(
        account_slot="binance_usdm_primary",
        runtime_profile_id="demo-v1",
        mode="paper",
        runtime_release="nautilus-1.231.0+oi-v1",
        config_sha256="a" * 64,
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
        runtime_revision="b" * 40,
        image_digest="sha256:" + "c" * 64,
        credential_fingerprint="d" * 64,
        lifecycle_state="running",
        ready=True,
        singleton_ready=True,
        credential_ready=True,
        activation_ready=True,
        startup_reconciled=True,
        portfolio_ready=True,
        audit_ready=True,
        unexpected_exposure=False,
        account_flat=True,
        reconciliation_observed_at_ns=900,
        heartbeat_at_ns=1_000,
        unavailable_reason=None,
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
) -> dict[str, object]:
    return {
        "event_id": event * 64,
        "runtime_profile_id": "demo-v1",
        "command_id": command_id,
        "normalized_kind": kind,
        "observed_at_ns": at,
        "summary": dict(summary or {}),
        "native_identity_references": refs,
    }


def _observations() -> list[dict[str, object]]:
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
        _row(
            "0",
            "reconciliation",
            900,
            summary={"source": "binance_private_api", "account_flat": True},
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

    assert receipt["truth"] == "binance_demo_only_not_live_money"
    assert receipt["runtime_start_event_ids"] == ["a" * 64, "7" * 64]
    assert {"entry-venue", "trade-1", "stop-venue", "exit-venue"} <= set(receipt["venue_native_references"])


def test_demo_receipt_rejects_flat_without_a_post_fill_restart() -> None:
    observations = [row for row in _observations() if row["event_id"] != "7" * 64]

    with pytest.raises(DemoReceiptError, match="binance_demo_restart_receipt_missing"):
        verify_binance_demo_receipt(
            state=_state(),
            observations=observations,
            entry_command_id=ENTRY,
            flatten_command_id=FLATTEN,
            now_ns=1_000,
        )
