from __future__ import annotations

from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.trading.evidence_verification import (
    FixedWindowAcceptanceV1,
    NautilusRuntimeStartV1,
    ProductionRollbackReceiptV1,
)
from tracefold.trading.storage.root import TradingRepository

pytestmark = pytest.mark.integration

START = 1_900_000_000_000
END = START + 7 * 86_400_000


@pytest.fixture
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _window() -> FixedWindowAcceptanceV1:
    return FixedWindowAcceptanceV1(
        start_ms=START,
        end_ms=END,
        drain_cutoff_ms=END + 1,
        gate_version="candidate_gate_v1",
        gate_config_digest="a" * 64,
        minimum_source_count=1,
        minimum_case_count=1,
        minimum_intent_count=1,
        minimum_closed_flat_count=1,
    )


def test_empty_fixed_window_snapshot_cannot_manufacture_operational_activity(conn: Any) -> None:
    snapshot = TradingRepository(conn).fixed_window_verification_snapshot(_window())

    assert snapshot["counts"]["source_count"] == 0
    assert snapshot["counts"]["case_count"] == 0
    assert snapshot["counts"]["intent_count"] == 0
    assert snapshot["counts"]["closed_flat_count"] == 0
    assert snapshot["by_binding"] == []


def test_rollback_snapshot_requires_real_flat_bindings_and_revoked_grants(conn: Any) -> None:
    unsigned: dict[str, object] = {
        "rollback_version": "production_v3_rollback_receipt_v1",
        "release_candidate_sha256": "1" * 64,
        "bindings": ["BINANCE_USDM"],
        "grant_sha256s": ["2" * 64],
        "rolled_back_at_ms": START,
        "rolled_back_by": "operator",
        "statement": "ALL_ENABLED_VENUES_FLAT_GRANTS_REVOKED_CAPITAL_PAUSED_NO_TERMINAL_INTENT_REVIVAL",
    }
    from tracefold.trading.contracts import canonical_sha256

    receipt = ProductionRollbackReceiptV1.model_validate(unsigned | {"receipt_sha256": canonical_sha256(unsigned)})
    snapshot = TradingRepository(conn).rollback_verification_snapshot(receipt)

    assert snapshot["control"] == "PAUSED"
    assert snapshot["active_intent_count"] == 0
    assert snapshot["active_risk_count"] == 0
    assert snapshot["grants"] == []


def test_release_snapshot_reads_append_only_nautilus_process_generations(conn: Any) -> None:
    repos = TradingRepository(conn)
    starts = tuple(
        NautilusRuntimeStartV1(
            runtime_id=f"00000000-0000-0000-0000-{index:012d}",
            runtime_revision="1" * 40,
            image_digest="tracefold@sha256:" + "2" * 64,
            nautilus_version="1.231.0",
            nautilus_source_git_commit="3" * 40,
            nautilus_wheel_identity="linux@sha256:" + "4" * 64,
            started_at_ms=START + index,
        )
        for index in (1, 2)
    )
    for start in starts:
        assert repos.append_nautilus_runtime_start(start)

    snapshot = repos.release_verification_snapshot(
        evidence_receipts=(),
        promotion_grants=(),
        risk_policies=(),
        canary_intents=("5" * 64,),
        restart_runtime_ids=(str(starts[0].runtime_id), str(starts[1].runtime_id)),
    )

    assert [row["runtime_id"] for row in snapshot["runtime_starts"]] == sorted(
        str(start.runtime_id) for start in starts
    )
    assert snapshot["canaries"] == []
