from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from tracefold.app.http.schemas import TokenRadarData


def test_token_radar_schema_rejects_incoherent_market_and_selection_states() -> None:
    packet = _packet()

    confirmed_without_change = _packet()
    confirmed_without_change["items"][0]["market"] = {
        "status": "confirmed",
        "price_change_since_signal": None,
    }
    with pytest.raises(ValidationError, match="token_radar_confirmed_market_change_required"):
        TokenRadarData.model_validate(confirmed_without_change)

    unavailable_with_change = _packet()
    unavailable_with_change["items"][0]["market"] = {
        "status": "unavailable",
        "price_change_since_signal": 0.1,
    }
    with pytest.raises(ValidationError, match="token_radar_unavailable_market_change_forbidden"):
        TokenRadarData.model_validate(unavailable_with_change)

    nonfinite = _packet()
    nonfinite["items"][0]["market"] = {
        "status": "confirmed",
        "price_change_since_signal": math.inf,
    }
    with pytest.raises(ValidationError, match="token_radar_confirmed_market_change_required"):
        TokenRadarData.model_validate(nonfinite)

    packet["eligible_total"] = 0
    with pytest.raises(ValidationError, match="token_radar_eligible_total_invalid"):
        TokenRadarData.model_validate(packet)

    unsupported_counter = _packet()
    unsupported_counter["items"][0]["counter_evidence"] = "unknown_reason"
    with pytest.raises(ValidationError):
        TokenRadarData.model_validate(unsupported_counter)


def _packet() -> dict[str, object]:
    return {
        "schema_version": "token_radar_snapshot_v1",
        "evidence_as_of_ms": 1,
        "eligible_total": 1,
        "items": [
            {
                "target": {
                    "target_type": "Asset",
                    "target_id": "asset:test",
                    "symbol": "TEST",
                    "chain": "eip155:1",
                    "exchange": None,
                    "address": "0xtest",
                },
                "trigger_event_id": "event-1",
                "triggered_at_ms": 1,
                "why_now": {
                    "current_mentions": 3,
                    "prior_mentions": 1,
                    "mention_delta": 2,
                },
                "evidence": {
                    "new_independent_author_count": 3,
                    "independent_text_count": 3,
                    "time_to_nth_author_ms": 1,
                    "duplicate_share": 0.0,
                },
                "market": {
                    "status": "unavailable",
                    "price_change_since_signal": None,
                },
                "counter_evidence": "market_confirmation_unavailable",
            }
        ],
    }
