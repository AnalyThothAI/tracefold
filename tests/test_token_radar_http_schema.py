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
        "price_usd": 1.0,
        "market_cap_usd": 1_000_000.0,
        "observed_at_ms": 1,
    }
    with pytest.raises(ValidationError, match="token_radar_confirmed_market_change_required"):
        TokenRadarData.model_validate(confirmed_without_change)

    unavailable_with_change = _packet()
    unavailable_with_change["items"][0]["market"] = {
        "status": "unavailable",
        "price_change_since_signal": 0.1,
        "price_usd": 1.0,
        "market_cap_usd": 1_000_000.0,
        "observed_at_ms": 1,
    }
    with pytest.raises(ValidationError, match="token_radar_unavailable_market_change_forbidden"):
        TokenRadarData.model_validate(unavailable_with_change)

    nonfinite = _packet()
    nonfinite["items"][0]["market"] = {
        "status": "confirmed",
        "price_change_since_signal": math.inf,
        "price_usd": 1.0,
        "market_cap_usd": 1_000_000.0,
        "observed_at_ms": 1,
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_usd", 0),
        ("price_usd", math.inf),
        ("market_cap_usd", -1),
        ("market_cap_usd", math.nan),
    ],
)
def test_token_radar_schema_rejects_nonpositive_or_nonfinite_current_metrics(
    field: str,
    value: float,
) -> None:
    packet = _packet()
    packet["items"][0]["market"][field] = value

    with pytest.raises(ValidationError):
        TokenRadarData.model_validate(packet)


def test_token_radar_schema_rejects_remote_logo_and_market_timestamp_without_metrics() -> None:
    remote_logo = _packet()
    remote_logo["items"][0]["target"]["logo_url"] = "https://remote.example/token.png"
    with pytest.raises(ValidationError):
        TokenRadarData.model_validate(remote_logo)

    timestamp_without_metrics = _packet()
    timestamp_without_metrics["items"][0]["market"] = {
        "status": "unavailable",
        "price_change_since_signal": None,
        "price_usd": None,
        "market_cap_usd": None,
        "observed_at_ms": 1,
    }
    with pytest.raises(ValidationError, match="token_radar_market_observation_without_metrics"):
        TokenRadarData.model_validate(timestamp_without_metrics)

    missing_required_market_field = _packet()
    del missing_required_market_field["items"][0]["market"]["price_usd"]
    with pytest.raises(ValidationError):
        TokenRadarData.model_validate(missing_required_market_field)


@pytest.mark.parametrize(
    "target",
    [
        {
            "target_type": "Asset",
            "target_id": "asset:test",
            "symbol": "TEST",
            "name": None,
            "logo_url": None,
            "chain": "eip155:1",
            "exchange": "binance",
            "address": "0xtest",
        },
        {
            "target_type": "Asset",
            "target_id": "asset:test",
            "symbol": "TEST",
            "name": None,
            "logo_url": None,
            "chain": "eip155:1",
            "exchange": None,
            "address": None,
        },
        {
            "target_type": "CexToken",
            "target_id": "cex:test",
            "symbol": "TEST",
            "name": None,
            "logo_url": None,
            "chain": "eip155:1",
            "exchange": "binance",
            "address": None,
        },
        {
            "target_type": "CexToken",
            "target_id": "cex:test",
            "symbol": "TEST",
            "name": None,
            "logo_url": None,
            "chain": None,
            "exchange": None,
            "address": None,
        },
    ],
)
def test_token_radar_schema_rejects_ambiguous_target_identity(target: dict[str, object]) -> None:
    packet = _packet()
    packet["items"][0]["target"] = target

    with pytest.raises(ValidationError, match="token_radar_target_identity_invalid"):
        TokenRadarData.model_validate(packet)


def _packet() -> dict[str, object]:
    return {
        "schema_version": "token_radar_snapshot_v2",
        "evidence_as_of_ms": 1,
        "eligible_total": 1,
        "items": [
            {
                "target": {
                    "target_type": "Asset",
                    "target_id": "asset:test",
                    "symbol": "TEST",
                    "name": "Test Token",
                    "logo_url": f"/api/token-images/{'a' * 64}",
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
                    "price_usd": 1.0,
                    "market_cap_usd": 1_000_000.0,
                    "observed_at_ms": 1,
                },
                "counter_evidence": "market_confirmation_unavailable",
            }
        ],
    }
