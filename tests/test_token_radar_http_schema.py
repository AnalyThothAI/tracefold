from __future__ import annotations

import math
from copy import deepcopy

import pytest
from pydantic import ValidationError

from tracefold.app.http.schemas import TokenRadarData


def test_token_radar_schema_accepts_exact_v4_packet_with_independent_market_clocks() -> None:
    packet = _v4_packet()

    validated = TokenRadarData.model_validate(packet)

    assert validated.model_dump(mode="json") == packet


@pytest.mark.parametrize(
    ("state", "stale_reason"),
    [
        ("current", "source_unavailable"),
        ("stale", None),
        ("unavailable", "projection_failed"),
    ],
)
def test_token_radar_schema_rejects_incoherent_public_state(
    state: str,
    stale_reason: str | None,
) -> None:
    packet = _v4_packet()
    packet["state"] = state
    packet["stale_reason"] = stale_reason

    with pytest.raises(ValidationError, match="token_radar_state_reason_invalid"):
        TokenRadarData.model_validate(packet)


def test_token_radar_schema_rejects_unavailable_state_with_business_payload() -> None:
    packet = _v4_packet()
    packet["state"] = "unavailable"

    with pytest.raises(ValidationError, match="token_radar_unavailable_payload_invalid"):
        TokenRadarData.model_validate(packet)


def test_token_radar_schema_rejects_unavailable_state_with_change_clock() -> None:
    packet = _v4_packet()
    packet.update(
        {
            "state": "unavailable",
            "state_changed_at_ms": 1,
            "social_evidence_as_of_ms": 0,
            "eligible_total": 0,
            "items": [],
        }
    )

    with pytest.raises(ValidationError, match="token_radar_unavailable_state_clock_invalid"):
        TokenRadarData.model_validate(packet)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_usd", None),
        ("price_observed_at_ms", None),
        ("market_cap_usd", None),
        ("market_cap_observed_at_ms", None),
    ],
)
def test_token_radar_schema_rejects_market_value_without_its_own_clock(
    field: str,
    value: object,
) -> None:
    packet = _v4_packet()
    packet["items"][0]["market"][field] = value

    with pytest.raises(ValidationError, match="token_radar_market_clock_invalid"):
        TokenRadarData.model_validate(packet)


def test_token_radar_schema_rejects_price_change_before_trigger_source_time() -> None:
    packet = _v4_packet()
    packet["items"][0]["market"]["price_observed_at_ms"] = 79

    with pytest.raises(ValidationError, match="token_radar_price_change_clock_invalid"):
        TokenRadarData.model_validate(packet)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trigger_source_event_at_ms", 91),
        ("qualified_at_ms", 101),
    ],
)
def test_token_radar_schema_rejects_incoherent_causal_times(field: str, value: int) -> None:
    packet = _v4_packet()
    packet["items"][0][field] = value

    with pytest.raises(ValidationError, match="token_radar_causal_time_invalid"):
        TokenRadarData.model_validate(packet)


def test_token_radar_schema_rejects_duplicate_target_keys() -> None:
    packet = _v4_packet()
    packet["eligible_total"] = 2
    packet["items"].append(deepcopy(packet["items"][0]))

    with pytest.raises(ValidationError, match="token_radar_target_duplicate"):
        TokenRadarData.model_validate(packet)


@pytest.mark.parametrize(
    ("qualified_at_ms", "target_id"),
    [
        (95, "asset:z"),
        (90, "asset:alpha"),
    ],
)
def test_token_radar_schema_rejects_non_server_ordered_items(
    qualified_at_ms: int,
    target_id: str,
) -> None:
    packet = _v4_packet()
    second = deepcopy(packet["items"][0])
    second["target"]["target_id"] = target_id
    second["qualified_at_ms"] = qualified_at_ms
    packet["eligible_total"] = 2
    packet["items"].append(second)

    with pytest.raises(ValidationError, match="token_radar_server_order_invalid"):
        TokenRadarData.model_validate(packet)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_id", ""),
        ("target_id", "   "),
        ("symbol", ""),
        ("symbol", "   "),
        ("name", ""),
        ("name", "   "),
        ("trigger_event_id", ""),
        ("trigger_event_id", "   "),
    ],
)
def test_token_radar_schema_rejects_blank_public_identifiers(field: str, value: str) -> None:
    packet = _v4_packet()
    item = packet["items"][0]
    if field == "trigger_event_id":
        item[field] = value
    else:
        item["target"][field] = value

    with pytest.raises(ValidationError):
        TokenRadarData.model_validate(packet)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_usd", 0),
        ("price_usd", math.inf),
        ("market_cap_usd", -1),
        ("market_cap_usd", math.nan),
        ("price_change_since_signal", math.inf),
    ],
)
def test_token_radar_schema_rejects_nonpositive_or_nonfinite_market_metrics(
    field: str,
    value: float,
) -> None:
    packet = _v4_packet()
    packet["items"][0]["market"][field] = value

    with pytest.raises(ValidationError):
        TokenRadarData.model_validate(packet)


def test_token_radar_schema_rejects_remote_logo() -> None:
    remote_logo = _v4_packet()
    remote_logo["items"][0]["target"]["logo_url"] = "https://remote.example/token.png"

    with pytest.raises(ValidationError, match="token_radar_logo_url_invalid"):
        TokenRadarData.model_validate(remote_logo)


def test_token_radar_schema_rejects_incoherent_counts() -> None:
    invalid_selection = _v4_packet()
    invalid_selection["eligible_total"] = 0
    with pytest.raises(ValidationError, match="token_radar_eligible_total_invalid"):
        TokenRadarData.model_validate(invalid_selection)

    underfilled_selection = _v4_packet()
    underfilled_selection["eligible_total"] = 2
    with pytest.raises(ValidationError, match="token_radar_eligible_total_invalid"):
        TokenRadarData.model_validate(underfilled_selection)

    invalid_delta = _v4_packet()
    invalid_delta["items"][0]["why_now"]["mention_delta"] = 1
    with pytest.raises(ValidationError, match="token_radar_mention_delta_invalid"):
        TokenRadarData.model_validate(invalid_delta)


def test_token_radar_schema_rejects_removed_v2_fields() -> None:
    old_market_status = _v4_packet()
    old_market_status["items"][0]["market"]["status"] = "confirmed"
    with pytest.raises(ValidationError):
        TokenRadarData.model_validate(old_market_status)

    old_counter = _v4_packet()
    old_counter["items"][0]["counter_evidence"] = "market_confirmation_unavailable"
    with pytest.raises(ValidationError):
        TokenRadarData.model_validate(old_counter)


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
    packet = _v4_packet()
    packet["items"][0]["target"] = target

    with pytest.raises(ValidationError, match="token_radar_target_identity_invalid"):
        TokenRadarData.model_validate(packet)


def _v4_packet() -> dict[str, object]:
    return {
        "schema_version": "token_radar_snapshot_v4",
        "state": "current",
        "stale_reason": None,
        "state_changed_at_ms": 120,
        "social_evidence_as_of_ms": 100,
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
                "trigger_source_event_at_ms": 80,
                "qualified_at_ms": 90,
                "why_now": {
                    "current_mentions": 3,
                    "prior_mentions": 1,
                    "mention_delta": 2,
                },
                "evidence": {
                    "independent_author_count": 3,
                    "independent_text_count": 3,
                    "time_to_nth_author_ms": 1,
                    "duplicate_share": 0.0,
                },
                "market": {
                    "price_usd": 1.0,
                    "price_observed_at_ms": 110,
                    "price_change_since_signal": 0.1,
                    "market_cap_usd": 1_000_000.0,
                    "market_cap_observed_at_ms": 105,
                },
            }
        ],
    }
