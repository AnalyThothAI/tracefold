from __future__ import annotations

from inspect import signature
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.news.canary import (
    CANARY_ELIGIBILITY_PROFILE,
    CANARY_ELIGIBILITY_PROFILE_SHA,
    CANARY_ROLLING_PROFILE_SHA,
    CANARY_SELECTOR_VERSION,
    apply_canary_control,
    parse_canary_control,
    select_canary_arm,
)
from tracefold.news.storage.root import NewsRepository


def test_canary_assignment_is_deterministic_and_uses_only_pre_call_facts() -> None:
    values = [
        select_canary_arm(
            event_id=f"event-{index}",
            activation_id="a" * 32,
            baseline_bundle_sha="b" * 64,
            candidate_bundle_sha="c" * 64,
            exposure_bps=1_000,
            admission="candidate",
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
        ingest_mode="live",
    )
    assert values[7] == again
    assert {value.arm for value in values} == {"stable", "candidate"}


def test_selector_v2_does_not_accept_queue_priority_as_eligibility_authority() -> None:
    parameters = signature(select_canary_arm).parameters
    assert CANARY_SELECTOR_VERSION == "news_canary_selector_v2"
    assert CANARY_ELIGIBILITY_PROFILE == {
        "live_only": True,
        "excluded_admissions": ["recovery", "listing_deterministic", "telemetry_deterministic"],
    }
    assert "priority" not in parameters
    assert "queue_priority" not in parameters
    selected = select_canary_arm(
        event_id="queue-high-event",
        activation_id="a" * 32,
        baseline_bundle_sha="b" * 64,
        candidate_bundle_sha="c" * 64,
        exposure_bps=10_000,
        admission="candidate",
        ingest_mode="live",
    )
    assert selected.arm == "candidate"
    assert selected.eligibility_reason == "eligible_bucket"


@pytest.mark.parametrize("admission", ["recovery", "listing_deterministic", "telemetry_deterministic"])
def test_canary_excludes_objective_or_replay_admissions_before_hashing(admission: str) -> None:
    common = {
        "event_id": "event",
        "activation_id": "a" * 32,
        "baseline_bundle_sha": "b" * 64,
        "candidate_bundle_sha": "c" * 64,
        "exposure_bps": 10_000,
        "admission": admission,
        "ingest_mode": "live",
    }
    assert admission in CANARY_ELIGIBILITY_PROFILE["excluded_admissions"]
    assert select_canary_arm(**common).eligibility_reason == f"excluded_admission:{admission}"


def test_canary_excludes_non_live_ingest_before_hashing() -> None:
    selected = select_canary_arm(
        event_id="event",
        activation_id="a" * 32,
        baseline_bundle_sha="b" * 64,
        candidate_bundle_sha="c" * 64,
        exposure_bps=10_000,
        admission="candidate",
        ingest_mode="recovery",
    )
    assert selected.eligibility_reason == "excluded_non_live"


def test_canary_control_requires_content_addressed_candidate_and_reason() -> None:
    assert parse_canary_control({"action": "arm", "candidate_sha": "a" * 64}).action == "arm"
    assert parse_canary_control({"action": "status"}).action == "status"
    try:
        parse_canary_control({"action": "trip", "activation_id": "id"})
    except ValueError as exc:
        assert str(exc) == "news_canary_reason_required"
    else:  # pragma: no cover - contract assertion
        raise AssertionError("trip without a reason must fail")


_IDENTITY_DRIFTS = [
    ("selector_version", "news_canary_selector_v1", "selector_version_mismatch"),
    ("eligibility_profile_sha", "0" * 64, "eligibility_profile_hash_mismatch"),
    ("rolling_profile_sha", "0" * 64, "rolling_profile_hash_mismatch"),
]


@pytest.mark.parametrize(("field", "drifted", "reason"), _IDENTITY_DRIFTS)
def test_assignment_trips_an_active_canary_before_selecting_on_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    drifted: str,
    reason: str,
) -> None:
    connection = _AssignmentConnection()
    repository = NewsRepository(connection)
    activation = _activation() | {field: drifted}
    transitions: list[dict[str, Any]] = []
    monkeypatch.setattr(repository, "active_canary", lambda: activation)
    monkeypatch.setattr(
        repository,
        "transition_canary",
        lambda **kwargs: transitions.append(kwargs) or True,
    )

    assignment = repository.assign_agent_arm(
        event_id="event",
        stable_bundle_sha="b" * 64,
        admission="candidate",
        ingest_mode="live",
        now_ms=123,
    )

    assert transitions == [
        {
            "activation_id": "a" * 32,
            "target_state": "tripped",
            "reason": reason,
            "now_ms": 123,
        }
    ]
    assert assignment["arm"] == "stable"
    assert assignment["activation_id"] is None
    assert assignment["selector_version"] == "stable_only_v2"
    assert assignment["eligibility_reason"] == "no_active_canary"


@pytest.mark.parametrize(("field", "drifted", "reason"), _IDENTITY_DRIFTS)
def test_resume_trips_a_held_canary_on_identity_drift(field: str, drifted: str, reason: str) -> None:
    activation = _activation() | {field: drifted}
    news = _ResumeNews(activation)

    status = apply_canary_control(
        SimpleNamespace(news=news),
        parse_canary_control({"action": "resume", "activation_id": "a" * 32, "reason": "operator_resume"}),
        stable_bundle_sha="b" * 64,
        shipped_candidates={"c" * 64: "d" * 64},
        now_ms=456,
    )

    assert news.transitions == [
        {
            "activation_id": "a" * 32,
            "target_state": "tripped",
            "reason": reason,
            "now_ms": 456,
        }
    ]
    assert status["state"] == "tripped"
    assert status["activation"]["trip_reason"] == reason


@pytest.mark.parametrize(("field", "drifted", "reason"), _IDENTITY_DRIFTS)
def test_rolling_evaluation_trips_before_reading_candidate_outcomes_on_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    drifted: str,
    reason: str,
) -> None:
    activation = _activation() | {field: drifted, "state": "active"}
    connection = _SingleRowConnection(activation)
    repository = NewsRepository(connection)
    transitions: list[dict[str, Any]] = []
    monkeypatch.setattr(
        repository,
        "transition_canary",
        lambda **kwargs: transitions.append(kwargs) or True,
    )

    result = repository.evaluate_canary_rolling_slo(activation_id="a" * 32, now_ms=789)

    assert result == {"evaluated": True, "tripped": True, "reason": reason}
    assert transitions == [
        {
            "activation_id": "a" * 32,
            "target_state": "tripped",
            "reason": reason,
            "now_ms": 789,
        }
    ]
    assert connection.calls == ["SELECT * FROM news_canary_activations WHERE activation_id = %s FOR UPDATE"]


def _activation() -> dict[str, Any]:
    return {
        "activation_id": "a" * 32,
        "state": "armed",
        "baseline_bundle_sha": "b" * 64,
        "candidate_manifest_sha": "c" * 64,
        "candidate_bundle_sha": "d" * 64,
        "selector_version": CANARY_SELECTOR_VERSION,
        "eligibility_profile_sha": CANARY_ELIGIBILITY_PROFILE_SHA,
        "rolling_profile_sha": CANARY_ROLLING_PROFILE_SHA,
        "exposure_bps": 1_000,
        "rolling_last_bucket_ms": None,
        "rolling_breach_windows": 0,
        "revision": 1,
    }


class _Cursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _AssignmentConnection:
    def __init__(self) -> None:
        self.assignment: dict[str, Any] | None = None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Cursor:
        normalized = " ".join(sql.split())
        if normalized == "SELECT * FROM news_agent_assignments WHERE event_id = %s":
            return _Cursor(self.assignment)
        if normalized.startswith("INSERT INTO news_agent_assignments"):
            (
                event_id,
                activation_id,
                arm,
                bundle_sha,
                selector_version,
                eligibility_reason,
                assigned_at_ms,
            ) = params
            self.assignment = {
                "event_id": event_id,
                "activation_id": activation_id,
                "arm": arm,
                "bundle_sha": bundle_sha,
                "selector_version": selector_version,
                "eligibility_reason": eligibility_reason,
                "assigned_at_ms": assigned_at_ms,
            }
            return _Cursor(None)
        raise AssertionError(normalized)


class _SingleRowConnection:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.calls: list[str] = []

    def execute(self, sql: str, _params: tuple[Any, ...] = ()) -> _Cursor:
        normalized = " ".join(sql.split())
        self.calls.append(normalized)
        return _Cursor(self.row)


class _ResumeNews:
    def __init__(self, activation: dict[str, Any]) -> None:
        self.activation = activation
        self.state = "armed"
        self.transitions: list[dict[str, Any]] = []

    def canary_status(self) -> dict[str, Any]:
        return {"state": self.state, "activation": self.activation}

    def transition_canary(self, **kwargs: Any) -> bool:
        self.transitions.append(kwargs)
        self.state = str(kwargs["target_state"])
        self.activation["state"] = self.state
        self.activation["trip_reason"] = kwargs["reason"]
        return True
