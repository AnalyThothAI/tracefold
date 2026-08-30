from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.release.canary import (
    CANARY_ELIGIBILITY_PROFILE_SHA,
    CANARY_ROLLING_PROFILE_SHA,
    CANARY_SELECTOR_VERSION,
)
from tracefold.news.release.runtime import (
    CandidateRuntimeFact,
    CandidateRuntimeFailureKind,
    reconcile_canary_startup,
)

MANIFEST_SHA = "a" * 64
COMPILED_BUNDLE_SHA = "b" * 64


class _CanaryRepository:
    def __init__(self, activation: dict[str, Any] | None, *, transition_result: bool = True) -> None:
        self.activation = activation
        self.transition_result = transition_result
        self.transitions: list[dict[str, Any]] = []

    def canary_status(self) -> dict[str, Any]:
        return {"activation": self.activation}

    def transition_canary(self, **kwargs: Any) -> bool:
        self.transitions.append(kwargs)
        return self.transition_result


def _activation(*, state: str = "active", **overrides: Any) -> dict[str, Any]:
    return {
        "activation_id": "c" * 32,
        "state": state,
        "candidate_manifest_sha": MANIFEST_SHA,
        "candidate_bundle_sha": COMPILED_BUNDLE_SHA,
        "selector_version": CANARY_SELECTOR_VERSION,
        "eligibility_profile_sha": CANARY_ELIGIBILITY_PROFILE_SHA,
        "rolling_profile_sha": CANARY_ROLLING_PROFILE_SHA,
        **overrides,
    }


def _fact(
    *,
    compiled_bundle_sha: str = COMPILED_BUNDLE_SHA,
    runnable: bool = False,
    failure_kind: CandidateRuntimeFailureKind = "runtime_unavailable",
) -> CandidateRuntimeFact:
    return CandidateRuntimeFact(
        candidate_manifest_sha=MANIFEST_SHA,
        compiled_bundle_sha=compiled_bundle_sha,
        runnable_bundle_sha=compiled_bundle_sha if runnable else None,
        failure_kind=None if runnable else failure_kind,
    )


@pytest.mark.parametrize(
    ("activation", "facts", "expected_reason", "expected_result", "transition_result"),
    [
        (None, {}, None, False, True),
        (_activation(state="tripped"), {}, None, False, True),
        (_activation(state="closed"), {}, None, False, True),
        (
            _activation(selector_version="news_canary_selector_v1"),
            {},
            "selector_version_mismatch",
            True,
            True,
        ),
        (
            _activation(eligibility_profile_sha="0" * 64),
            {},
            "eligibility_profile_hash_mismatch",
            True,
            True,
        ),
        (
            _activation(rolling_profile_sha="0" * 64),
            {},
            "rolling_profile_hash_mismatch",
            True,
            True,
        ),
        (_activation(), {}, "candidate_manifest_missing_or_invalid", True, True),
        (
            _activation(),
            {MANIFEST_SHA: _fact(compiled_bundle_sha="d" * 64, failure_kind="parent_stale")},
            "candidate_bundle_mismatch",
            True,
            True,
        ),
        (_activation(), {MANIFEST_SHA: _fact(failure_kind="parent_stale")}, "candidate_parent_stale", True, True),
        (
            _activation(),
            {MANIFEST_SHA: _fact(failure_kind="artifact_invalid")},
            "candidate_artifact_invalid",
            True,
            True,
        ),
        (
            _activation(),
            {MANIFEST_SHA: _fact(failure_kind="runtime_invalid")},
            "candidate_runtime_invalid",
            True,
            True,
        ),
        (
            _activation(),
            {MANIFEST_SHA: _fact(failure_kind="runtime_unavailable")},
            "candidate_runtime_unavailable",
            True,
            True,
        ),
        (_activation(state="armed"), {MANIFEST_SHA: _fact(runnable=True)}, None, False, True),
        (_activation(state="active"), {MANIFEST_SHA: _fact(runnable=True)}, None, False, True),
        (
            _activation(),
            {MANIFEST_SHA: _fact(failure_kind="runtime_unavailable")},
            "candidate_runtime_unavailable",
            False,
            False,
        ),
    ],
)
def test_reconcile_canary_startup_parity_matrix(
    activation: dict[str, Any] | None,
    facts: dict[str, CandidateRuntimeFact],
    expected_reason: str | None,
    expected_result: bool,
    transition_result: bool,
) -> None:
    repository = _CanaryRepository(activation, transition_result=transition_result)

    result = reconcile_canary_startup(repository, candidate_facts=facts, now_ms=123)

    assert result is expected_result
    if expected_reason is None:
        assert repository.transitions == []
    else:
        assert repository.transitions == [
            {
                "activation_id": "c" * 32,
                "target_state": "tripped",
                "reason": expected_reason,
                "now_ms": 123,
            }
        ]


def test_candidate_runtime_fact_rejects_contradictory_availability() -> None:
    with pytest.raises(ValueError, match="news_candidate_runtime_fact_failure_required"):
        CandidateRuntimeFact(MANIFEST_SHA, COMPILED_BUNDLE_SHA, None, None)
    with pytest.raises(ValueError, match="news_candidate_runtime_fact_runnable_invalid"):
        CandidateRuntimeFact(MANIFEST_SHA, COMPILED_BUNDLE_SHA, "d" * 64, None)
