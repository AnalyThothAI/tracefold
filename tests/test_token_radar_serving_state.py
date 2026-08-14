from __future__ import annotations

import pytest

from tracefold.market.radar.snapshot_repository import TokenRadarCurrentRepository

BUSINESS_PAYLOAD = {
    "schema_version": "token_radar_snapshot_v5",
    "social_evidence_as_of_ms": 1_800_000_000_000,
    "eligible_total": 0,
    "items": [],
}


def test_served_snapshot_returns_the_complete_v5_business_payload() -> None:
    snapshot = TokenRadarCurrentRepository(_Connection(BUSINESS_PAYLOAD)).served_snapshot()

    assert snapshot == BUSINESS_PAYLOAD


def test_served_snapshot_rejects_a_retired_v4_payload() -> None:
    payload = {**BUSINESS_PAYLOAD, "schema_version": "token_radar_snapshot_v4"}

    with pytest.raises(RuntimeError, match="token_radar_current_schema_invalid"):
        TokenRadarCurrentRepository(_Connection(payload)).served_snapshot()


class _Cursor:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def fetchone(self) -> dict[str, object]:
        return self.row


class _Connection:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def execute(self, _sql: str) -> _Cursor:
        return _Cursor({"served_payload": self.payload})
