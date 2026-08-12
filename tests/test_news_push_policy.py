import pytest

from tracefold.news import evaluate_news_push_eligibility


def _evidence(
    *,
    score: object = 88,
    symbols: tuple[str, ...] = ("BTC",),
    published_at_ms: int = 1_000,
    eligibility_observed_at_ms: object = 1_000,
) -> dict[str, object]:
    return {
        "provider_score": score,
        "published_at_ms": published_at_ms,
        "eligibility_observed_at_ms": eligibility_observed_at_ms,
        "provider_metadata": {"coins": [{"symbol": symbol, "market_type": "spot"} for symbol in symbols]},
    }


def test_news_push_policy_admits_fresh_post_baseline_evidence() -> None:
    eligibility = evaluate_news_push_eligibility(
        _evidence(),
        enabled=True,
        baseline_at_ms=500,
        now_ms=1_100,
    )

    assert eligibility.eligible is True
    assert eligibility.ineligible_reason is None


def test_news_push_policy_uses_local_evidence_clock_to_fence_provider_clock_skew() -> None:
    evidence = _evidence(published_at_ms=10_000)
    evidence["eligibility_observed_at_ms"] = 500

    eligibility = evaluate_news_push_eligibility(
        evidence,
        enabled=True,
        baseline_at_ms=500,
        now_ms=1_100,
    )

    assert eligibility.eligible is False
    assert eligibility.ineligible_reason == "baseline"


@pytest.mark.parametrize(
    ("evidence", "enabled", "baseline_at_ms", "now_ms", "reason"),
    (
        (_evidence(score=None), True, 500, 1_100, "score_threshold"),
        (_evidence(score=70), True, 500, 1_100, "score_threshold"),
        (_evidence(symbols=()), True, 500, 1_100, "no_asset"),
        (_evidence(symbols=("CL", "XYZ-CL")), True, 500, 1_100, "cl_family_only"),
        (_evidence(), False, 500, 1_100, "disabled"),
        (_evidence(), True, None, 1_100, "baseline"),
        (_evidence(published_at_ms=500), True, 500, 1_100, "baseline"),
        (_evidence(eligibility_observed_at_ms=None), True, 500, 1_100, "baseline"),
        (
            _evidence(published_at_ms=1_000),
            True,
            500,
            1_000 + 15 * 60 * 1_000 + 1,
            "stale",
        ),
    ),
)
def test_news_push_policy_returns_one_reason_for_ineligible_evidence(
    evidence: dict[str, object],
    enabled: bool,
    baseline_at_ms: int | None,
    now_ms: int,
    reason: str,
) -> None:
    eligibility = evaluate_news_push_eligibility(
        evidence,
        enabled=enabled,
        baseline_at_ms=baseline_at_ms,
        now_ms=now_ms,
    )

    assert eligibility.eligible is False
    assert eligibility.ineligible_reason == reason


def test_news_push_policy_keeps_mixed_cl_assets_and_exact_freshness_boundary() -> None:
    published_at_ms = 1_000

    eligibility = evaluate_news_push_eligibility(
        _evidence(symbols=("CL", "BTC"), published_at_ms=published_at_ms),
        enabled=True,
        baseline_at_ms=500,
        now_ms=published_at_ms + 15 * 60 * 1_000,
    )

    assert eligibility.eligible is True
    assert eligibility.ineligible_reason is None
