from __future__ import annotations

from tracefold.news.canary import parse_canary_control, select_canary_arm


def test_canary_assignment_is_deterministic_and_uses_only_pre_call_facts() -> None:
    values = [
        select_canary_arm(
            event_id=f"event-{index}",
            activation_id="a" * 32,
            baseline_bundle_sha="b" * 64,
            candidate_bundle_sha="c" * 64,
            exposure_bps=1_000,
            admission="candidate",
            priority="normal",
            ingest_mode="live",
        )
        for index in range(100)
    ]
    again = select_canary_arm(
        event_id="event-7",
        activation_id="a" * 32,
        baseline_bundle_sha="b" * 64,
        candidate_bundle_sha="c" * 64,
        exposure_bps=1_000,
        admission="candidate",
        priority="normal",
        ingest_mode="live",
    )
    assert values[7] == again
    assert {value.arm for value in values} == {"stable", "candidate"}


def test_canary_excludes_high_risk_or_non_live_events_before_hashing() -> None:
    common = {
        "event_id": "event",
        "activation_id": "a" * 32,
        "baseline_bundle_sha": "b" * 64,
        "candidate_bundle_sha": "c" * 64,
        "exposure_bps": 10_000,
        "admission": "candidate",
        "priority": "normal",
        "ingest_mode": "live",
    }
    assert select_canary_arm(**{**common, "priority": "high"}).eligibility_reason == "excluded_priority:high"
    assert (
        select_canary_arm(**{**common, "admission": "listing_deterministic"}).eligibility_reason
        == "excluded_admission:listing_deterministic"
    )
    assert select_canary_arm(**{**common, "ingest_mode": "recovery"}).eligibility_reason == "excluded_non_live"


def test_canary_control_requires_content_addressed_candidate_and_reason() -> None:
    assert parse_canary_control({"action": "arm", "candidate_sha": "a" * 64}).action == "arm"
    assert parse_canary_control({"action": "status"}).action == "status"
    try:
        parse_canary_control({"action": "trip", "activation_id": "id"})
    except ValueError as exc:
        assert str(exc) == "news_canary_reason_required"
    else:  # pragma: no cover - contract assertion
        raise AssertionError("trip without a reason must fail")
