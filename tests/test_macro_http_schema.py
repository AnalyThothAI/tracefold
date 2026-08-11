from __future__ import annotations

import pytest
from pydantic import ValidationError

from tracefold.app.http import schemas


def test_macro_http_contract_rejects_unknown_module_identity() -> None:
    with pytest.raises(ValidationError):
        schemas.MacroModuleUnavailableData.model_validate(
            {
                "schema_version": "macro_module_unavailable_v1",
                "module_id": "retired_macro_module",
                "label": "retired",
                "availability": "unavailable",
                "reason": {
                    "code": "macro_module_not_materialized",
                    "message": "missing",
                    "impact": "blocked",
                    "affected_dataset_ids": [],
                    "retryable": True,
                    "recovery": "automatic",
                    "next_action": "retry",
                    "next_check_at_ms": None,
                },
                "href": "/macro/retired",
            }
        )


def test_macro_http_contract_rejects_unknown_source_role_and_health_state() -> None:
    with pytest.raises(ValidationError):
        schemas.MacroDatasetStateData.model_validate(
            {
                "dataset_id": "fred.example",
                "concept_id": "example",
                "source_role": "fallback",
                "required_for_current": True,
                "required_for_history": False,
                "label": "Example",
                "current_health": "current",
                "history_depth": "not_required",
                "market_state": "not_applicable",
                "source_state": "healthy",
                "current_reason": _reason(),
                "history_reason": _reason(),
                "critical": True,
                "trust_tier": "official",
                "source_url": "https://example.test",
                "latest_reference": None,
                "latest_received_at_ms": None,
                "last_market_at_ms": None,
                "next_open_ms": None,
                "health_group": "example",
            }
        )

    with pytest.raises(ValidationError):
        schemas.MacroNextCheckpointData.model_validate(
            {
                "dataset_id": "fred.example",
                "label": "Example",
                "current_health": "warming",
                "history_depth": "not_required",
                "reason": None,
                "next_check_at_ms": None,
            }
        )


def test_macro_http_contract_keeps_auction_rate_definitions_separate() -> None:
    result = schemas.MacroTreasuryAuctionResultData.model_validate(
        {
            "auction_id": "2026-08-10:13-WEEK:912797QJ2",
            "cusip": "912797QJ2",
            "security_term": "13-Week Bill",
            "auction_date": "2026-08-10",
            "scheduled_at_ms": None,
            "published_at_ms": 1_786_329_000_000,
            "received_at_ms": 1_786_329_010_000,
            "source_url": "https://fiscaldata.treasury.gov/",
            "bid_to_cover_ratio": 2.8,
            "high_yield_pct": None,
            "high_discount_rate_pct": 3.735,
            "high_investment_rate_pct": 3.823,
            "offering_amount_usd": 78_000_000_000.0,
            "indirect_award_share_pct": 61.0,
            "direct_award_share_pct": 19.0,
            "primary_dealer_award_share_pct": 20.0,
        }
    )

    assert result.high_discount_rate_pct == 3.735
    assert result.high_investment_rate_pct == 3.823
    assert result.high_yield_pct is None


@pytest.mark.parametrize(
    "seasonal_adjustment",
    [
        "seasonally_adjusted",
        "not_seasonally_adjusted",
        "seasonally_adjusted_annual_rate",
        "unknown",
    ],
)
def test_macro_http_contract_exposes_official_release_adjustment(
    seasonal_adjustment: str,
) -> None:
    observation = schemas.MacroReleaseObservationData.model_validate(
        {
            "reference_period": "2026-07",
            "seasonal_adjustment": seasonal_adjustment,
            "scheduled_at_ms": None,
            "actual_value": 1.0,
            "estimate_value": None,
            "prior_value": None,
            "revised_prior_value": None,
            "surprise": None,
            "revision": None,
            "unit": "percent",
            "published_at_ms": 1_786_329_000_000,
            "received_at_ms": 1_786_329_010_000,
            "source_url": "https://example.test/release",
        }
    )

    assert observation.seasonal_adjustment == seasonal_adjustment


def test_macro_http_contract_owns_correlation_windows_and_derivation() -> None:
    contract = schemas.MacroCorrelationContractData.model_validate(
        {
            "default_window": "90_daily_returns",
            "supported_windows": [
                "30_daily_returns",
                "90_daily_returns",
                "252_daily_returns",
            ],
            "minimum_common_observations": 20,
            "presentation_derivation": "undirected_pairs_mirrored_with_unit_diagonal",
        }
    )
    fact = schemas.MacroCorrelationData.model_validate(
        {
            "left": "SPY",
            "right": "QQQ",
            "correlation": 0.91,
            "sample_count": 90,
            "window": "90_daily_returns",
        }
    )

    assert contract.default_window == "90_daily_returns"
    assert fact.window in contract.supported_windows


def _reason() -> dict[str, object]:
    return {
        "code": "current",
        "message": "current",
        "impact": "none",
        "affected_dataset_ids": [],
        "retryable": False,
        "recovery": "none",
        "next_action": None,
        "next_check_at_ms": None,
    }
