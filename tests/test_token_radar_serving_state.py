from __future__ import annotations

import pytest

from tracefold.market.radar.snapshot_repository import TokenRadarCurrentRepository

BUSINESS_PAYLOAD = {
    "schema_version": "token_radar_snapshot_v4",
    "social_evidence_as_of_ms": 1_800_000_000_000,
    "eligible_total": 0,
    "items": [],
}


@pytest.mark.parametrize(
    ("row", "expected_state", "expected_reason"),
    [
        (
            {
                "state_fingerprint": None,
                "latest_attempt_status": "never",
                "latest_error_code": None,
                "state_changed_at_ms": 0,
            },
            "unavailable",
            None,
        ),
        (
            {
                "state_fingerprint": "sha256:" + "a" * 64,
                "latest_attempt_status": "ready",
                "latest_error_code": None,
                "state_changed_at_ms": 1_800_000_000_001,
            },
            "current",
            None,
        ),
        (
            {
                "state_fingerprint": "sha256:" + "a" * 64,
                "latest_attempt_status": "failed",
                "latest_error_code": "token_radar_source_unavailable",
                "state_changed_at_ms": 1_800_000_000_002,
            },
            "stale",
            "source_unavailable",
        ),
        (
            {
                "state_fingerprint": "sha256:" + "a" * 64,
                "latest_attempt_status": "failed",
                "latest_error_code": "token_radar_input_row_overflow",
                "state_changed_at_ms": 1_800_000_000_003,
            },
            "stale",
            "projection_failed",
        ),
    ],
)
def test_served_snapshot_constructs_minimal_public_state_from_singleton(
    row: dict[str, object],
    expected_state: str,
    expected_reason: str | None,
) -> None:
    connection = _Connection({**row, "served_payload": BUSINESS_PAYLOAD})

    snapshot = TokenRadarCurrentRepository(connection).served_snapshot()

    assert snapshot == {
        "schema_version": "token_radar_snapshot_v4",
        "state": expected_state,
        "stale_reason": expected_reason,
        "state_changed_at_ms": row["state_changed_at_ms"],
        "social_evidence_as_of_ms": 0 if expected_state == "unavailable" else 1_800_000_000_000,
        "eligible_total": 0,
        "items": [],
    }


class _Cursor:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def fetchone(self) -> dict[str, object]:
        return self.row


class _Connection:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def execute(self, _sql: str) -> _Cursor:
        return _Cursor(self.row)
