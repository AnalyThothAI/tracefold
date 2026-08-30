from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.integration.test_news_review_desk import NOW, _open_event
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.wiring import news as workers
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.release import runtime as release_runtime
from tracefold.news.release.canary import (
    CANARY_ELIGIBILITY_PROFILE_SHA,
    CANARY_ROLLING_PROFILE_SHA,
    CANARY_SELECTOR_VERSION,
    apply_canary_control,
    parse_canary_control,
)
from tracefold.news.release.runtime import CandidateRuntimeFact, reconcile_canary_startup
from tracefold.platform.postgres.client import create_pool

pytestmark = pytest.mark.integration


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


def _artifact_sha(kind: str, payload: dict[str, object]) -> str:
    encoded = json.dumps({"kind": kind, "payload": payload}, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _clone_event(conn, source_event_id: str, *, suffix: str, opened_at_ms: int) -> str:
    event_id = hashlib.sha256(f"canary:{suffix}".encode()).hexdigest()
    conn.execute(
        """
        INSERT INTO news_events (
          event_id, leader_item_id, dedupe_family, event_kind, comparison_fingerprint, comparison_title,
          leader_title, opened_at_ms, last_member_at_ms, expires_at_ms, member_count,
          admission, queue_priority, provider_score_max, engine_type, asset_class,
          grounded_assets, watchlist_hits, macro_lexicon, storyline_key, context_line,
          published_at_ms, followup_of, ingest_mode, trace_id, created_at_ms, updated_at_ms,
          focus_fact_id, focus_fact_text, focus_fact_context, focus_fact_method,
          focus_span_start, focus_span_end
        )
        SELECT %s, leader_item_id, dedupe_family, event_kind, comparison_fingerprint || %s,
               comparison_title, leader_title, %s, %s, %s, member_count,
               admission, queue_priority, provider_score_max, engine_type, asset_class,
               grounded_assets, watchlist_hits, macro_lexicon, storyline_key, context_line,
               published_at_ms, followup_of, ingest_mode, trace_id, %s, %s,
               focus_fact_id || %s, focus_fact_text, focus_fact_context, focus_fact_method,
               focus_span_start, focus_span_end
          FROM news_events WHERE event_id = %s
        """,
        (
            event_id,
            suffix,
            opened_at_ms,
            opened_at_ms,
            opened_at_ms + 3_600_000,
            opened_at_ms,
            opened_at_ms,
            suffix,
            source_event_id,
        ),
    )
    return event_id


def test_canary_control_requires_shadow_pass_and_keeps_one_event_arm(conn) -> None:
    candidate_sha = "a" * 64
    candidate_bundle = "b" * 64
    stable_bundle = "c" * 64
    event_id = _open_event(conn)
    release = {
        "candidate_sha": candidate_sha,
        "stage": "shadow",
        "gate_outcome": "pass",
        "report_sha": "d" * 64,
        "run_sha": "e" * 64,
        "trusted_root_sha": "f" * 64,
    }
    artifact_sha = _artifact_sha("release_evidence", release)
    conn.execute(
        """
        INSERT INTO news_learning_artifacts(
          artifact_sha, kind, parent_sha, payload, created_by, created_at_ms
        ) VALUES (%s, 'release_evidence', %s, %s::jsonb, 'test', %s)
        """,
        (artifact_sha, "d" * 64, json.dumps(release), NOW),
    )
    repos = repositories_for_connection(conn)
    with repos.transaction():
        status = apply_canary_control(
            repos,
            parse_canary_control({"action": "arm", "candidate_sha": candidate_sha}),
            stable_bundle_sha=stable_bundle,
            shipped_candidates={candidate_sha: candidate_bundle},
            now_ms=NOW,
        )
    assert status["state"] == "active"
    activation_id = status["activation"]["activation_id"]
    arm_receipt = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE kind = 'deployment_receipt' "
        "AND payload->>'action' = 'canary_arm'"
    ).fetchone()
    assert arm_receipt["payload"]["activation_id"] == activation_id

    with repos.transaction():
        first = repos.news.assign_agent_arm(
            event_id=event_id,
            stable_bundle_sha=stable_bundle,
            admission="candidate",
            ingest_mode="live",
            now_ms=NOW + 1,
        )
        second = repos.news.assign_agent_arm(
            event_id=event_id,
            stable_bundle_sha=stable_bundle,
            admission="candidate",
            ingest_mode="live",
            now_ms=NOW + 2,
        )
    assert first == second
    assert first["activation_id"] == activation_id

    with repos.transaction():
        held = apply_canary_control(
            repos,
            parse_canary_control({"action": "hold", "activation_id": activation_id, "reason": "operator_pause"}),
            stable_bundle_sha=stable_bundle,
            shipped_candidates={candidate_sha: candidate_bundle},
            now_ms=NOW + 3,
        )
    assert held["state"] == "armed"
    held_event = _clone_event(conn, event_id, suffix="during-hold", opened_at_ms=NOW + 4)
    with repos.transaction():
        held_assignment = repos.news.assign_agent_arm(
            event_id=held_event,
            stable_bundle_sha=stable_bundle,
            admission="candidate",
            ingest_mode="live",
            now_ms=NOW + 4,
        )
        resumed = apply_canary_control(
            repos,
            parse_canary_control({"action": "resume", "activation_id": activation_id, "reason": "operator_continue"}),
            stable_bundle_sha=stable_bundle,
            shipped_candidates={candidate_sha: candidate_bundle},
            now_ms=NOW + 5,
        )
    assert held_assignment["arm"] == "stable"
    assert held_assignment["eligibility_reason"] == "no_active_canary"
    assert resumed["state"] == "active"

    with repos.transaction():
        tripped = apply_canary_control(
            repos,
            parse_canary_control({"action": "trip", "activation_id": activation_id, "reason": "manual_safety_hold"}),
            stable_bundle_sha=stable_bundle,
            shipped_candidates={candidate_sha: candidate_bundle},
            now_ms=NOW + 6,
        )
    assert tripped["state"] == "tripped"
    assert tripped["activation"]["revision"] == 4
    assert tripped["activation"]["trip_reason"] == "manual_safety_hold"
    rollback = conn.execute("SELECT payload FROM news_learning_artifacts WHERE kind = 'rollback_receipt'").fetchone()
    assert rollback["payload"]["action"] == "canary_trip"
    assert rollback["payload"]["activation_id"] == activation_id


def test_canary_arm_rejects_an_invalid_program_artifact_before_writing_activation(conn, monkeypatch) -> None:
    candidate_sha = "7" * 64
    stable = SimpleNamespace(
        bundle_sha="8" * 64,
        program_version=PROGRAM_VERSION,
        program_sha256="9" * 64,
    )
    stable_artifact = SimpleNamespace(program_sha256=stable.program_sha256)
    # Lineage lives on the candidate's own proposal receipt now, so the candidate has to name this exact
    # stable parent to reach the artifact load at all — otherwise it would be rejected one step earlier
    # and the artifact rejection this test exists for would never be exercised.
    candidate = SimpleNamespace(
        target="program",
        parent_stable_sha=stable.bundle_sha,
        candidate_arm=SimpleNamespace(
            bundle_sha="a" * 64,
            program_version=PROGRAM_VERSION,
            program_sha256="b" * 64,
        ),
        proposal_receipt=SimpleNamespace(
            program_parent_sha256=stable.program_sha256,
            program_candidate_sha256="b" * 64,
        ),
    )
    monkeypatch.setattr(release_runtime, "load_stable_program_artifact", lambda: stable_artifact)

    def reject_artifact(_program_sha256: str):
        raise ValueError("news_program_artifact_hash_mismatch")

    monkeypatch.setattr(release_runtime, "load_program_artifact", reject_artifact)
    shipped = release_runtime.artifact_valid_candidate_bundles(stable, {candidate_sha: candidate})
    assert shipped == {}

    repos = repositories_for_connection(conn)
    with (
        pytest.raises(ValueError, match=r"^news_canary_candidate_not_in_image$"),
        repos.transaction(),
    ):
        apply_canary_control(
            repos,
            parse_canary_control({"action": "arm", "candidate_sha": candidate_sha}),
            stable_bundle_sha=stable.bundle_sha,
            shipped_candidates=shipped,
            now_ms=NOW,
        )

    assert conn.execute("SELECT count(*) AS n FROM news_canary_activations").fetchone()["n"] == 0


def test_canary_resume_trips_a_held_candidate_no_longer_carried_by_the_image(conn) -> None:
    activation_id = "c" * 32
    stable_bundle = "d" * 64
    candidate_sha = "e" * 64
    candidate_bundle = "f" * 64
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.arm_canary(
            activation_id=activation_id,
            baseline_bundle_sha=stable_bundle,
            candidate_manifest_sha=candidate_sha,
            candidate_bundle_sha=candidate_bundle,
            selector_version=CANARY_SELECTOR_VERSION,
            exposure_bps=1_000,
            eligibility_profile_sha=CANARY_ELIGIBILITY_PROFILE_SHA,
            rolling_profile_sha=CANARY_ROLLING_PROFILE_SHA,
            now_ms=NOW,
        )
        repos.news.transition_canary(
            activation_id=activation_id,
            target_state="armed",
            reason="operator_hold",
            now_ms=NOW + 1,
        )

    with repos.transaction():
        status = apply_canary_control(
            repos,
            parse_canary_control({"action": "resume", "activation_id": activation_id, "reason": "operator_resume"}),
            stable_bundle_sha=stable_bundle,
            shipped_candidates={},
            now_ms=NOW + 2,
        )

    assert status["state"] == "tripped"
    assert status["activation"]["trip_reason"] == "candidate_artifact_missing"
    assert conn.execute("SELECT count(*) AS n FROM news_canary_activations WHERE state = 'active'").fetchone()["n"] == 0
    receipt = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE kind = 'rollback_receipt' "
        "AND payload->>'activation_id' = %s",
        (activation_id,),
    ).fetchone()
    assert receipt["payload"]["action"] == "canary_trip"
    assert receipt["payload"]["reason"] == "candidate_artifact_missing"


def test_worker_startup_persists_unrunnable_candidate_trip_before_consumption(conn) -> None:
    activation_id = "1" * 32
    stable_bundle = "2" * 64
    candidate_sha = "3" * 64
    candidate_bundle = "4" * 64
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.arm_canary(
            activation_id=activation_id,
            baseline_bundle_sha=stable_bundle,
            candidate_manifest_sha=candidate_sha,
            candidate_bundle_sha=candidate_bundle,
            selector_version=CANARY_SELECTOR_VERSION,
            exposure_bps=1_000,
            eligibility_profile_sha=CANARY_ELIGIBILITY_PROFILE_SHA,
            rolling_profile_sha=CANARY_ROLLING_PROFILE_SHA,
            now_ms=NOW,
        )
        repos.news.transition_canary(
            activation_id=activation_id,
            target_state="armed",
            reason="operator_hold",
            now_ms=NOW + 1,
        )

    pool = create_pool(
        _test_postgres_dsn(),
        min_size=0,
        max_size=1,
        connect_timeout_seconds=5.0,
        application_name="tracefold_canary_startup_test",
        statement_timeout_seconds=5.0,
    )
    pool.wait(timeout=5.0)
    database = WorkerDatabase(worker_pool=pool, telemetry=None)
    try:
        facts = {
            candidate_sha: CandidateRuntimeFact(
                candidate_manifest_sha=candidate_sha,
                compiled_bundle_sha=candidate_bundle,
                runnable_bundle_sha=None,
                failure_kind="artifact_invalid",
            )
        }
        assert workers._reconcile_news_canary_startup(
            database,
            facts,
        )
        assert not workers._reconcile_news_canary_startup(
            database,
            facts,
        )
    finally:
        database.close_executors()
        pool.close()

    activation = conn.execute(
        "SELECT baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha, state, revision, trip_reason "
        "FROM news_canary_activations WHERE activation_id = %s",
        (activation_id,),
    ).fetchone()
    assert activation == {
        "baseline_bundle_sha": stable_bundle,
        "candidate_manifest_sha": candidate_sha,
        "candidate_bundle_sha": candidate_bundle,
        "state": "tripped",
        "revision": 3,
        "trip_reason": "candidate_artifact_invalid",
    }
    receipts = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE kind = 'rollback_receipt' "
        "AND payload->>'activation_id' = %s",
        (activation_id,),
    ).fetchall()
    assert len(receipts) == 1
    assert receipts[0]["payload"] == {
        "action": "canary_trip",
        "activation_id": activation_id,
        "baseline_bundle_sha": stable_bundle,
        "candidate_manifest_sha": candidate_sha,
        "candidate_bundle_sha": candidate_bundle,
        "reason": "candidate_artifact_invalid",
        "transitioned_at_ms": receipts[0]["payload"]["transitioned_at_ms"],
        "previous_revision": 2,
        "new_revision": 3,
    }


class _StartupRepositoryProbe:
    def __init__(self, repository, *, lose_cas: bool, now_ms: int) -> None:
        self._repository = repository
        self._lose_cas = lose_cas
        self._now_ms = now_ms
        self.transitions: list[dict[str, object]] = []

    def canary_status(self):
        status = self._repository.canary_status()
        activation = status.get("activation")
        if self._lose_cas and activation is not None:
            assert self._repository.transition_canary(
                activation_id=str(activation["activation_id"]),
                target_state="closed",
                reason="concurrent_operator_close",
                now_ms=self._now_ms - 1,
            )
        return status

    def transition_canary(self, **kwargs):
        self.transitions.append(dict(kwargs))
        return self._repository.transition_canary(**kwargs)


_STARTUP_DURABLE_CASES = [
    ("no_activation", None, {}, "missing", None, None, False),
    ("terminal_tripped", "tripped", {}, "missing", None, None, False),
    ("terminal_closed", "closed", {}, "missing", None, None, False),
    (
        "selector_version_mismatch",
        "active",
        {"selector_version": "news_canary_selector_v1"},
        "missing",
        None,
        "selector_version_mismatch",
        False,
    ),
    (
        "eligibility_profile_hash_mismatch",
        "active",
        {"eligibility_profile_sha": "0" * 64},
        "missing",
        None,
        "eligibility_profile_hash_mismatch",
        False,
    ),
    (
        "rolling_profile_hash_mismatch",
        "active",
        {"rolling_profile_sha": "0" * 64},
        "missing",
        None,
        "rolling_profile_hash_mismatch",
        False,
    ),
    (
        "candidate_manifest_missing_or_invalid",
        "active",
        {},
        "missing",
        None,
        "candidate_manifest_missing_or_invalid",
        False,
    ),
    (
        "candidate_bundle_mismatch",
        "active",
        {},
        "mismatch",
        "parent_stale",
        "candidate_bundle_mismatch",
        False,
    ),
    ("candidate_parent_stale", "active", {}, "failed", "parent_stale", "candidate_parent_stale", False),
    (
        "candidate_artifact_invalid",
        "active",
        {},
        "failed",
        "artifact_invalid",
        "candidate_artifact_invalid",
        False,
    ),
    (
        "candidate_runtime_invalid",
        "active",
        {},
        "failed",
        "runtime_invalid",
        "candidate_runtime_invalid",
        False,
    ),
    (
        "candidate_runtime_unavailable",
        "active",
        {},
        "failed",
        "runtime_unavailable",
        "candidate_runtime_unavailable",
        False,
    ),
    ("runnable_armed", "armed", {}, "runnable", None, None, False),
    ("runnable_active", "active", {}, "runnable", None, None, False),
    (
        "repository_cas_false",
        "active",
        {},
        "failed",
        "runtime_unavailable",
        "candidate_runtime_unavailable",
        True,
    ),
]


@pytest.mark.parametrize(
    ("case", "initial_state", "activation_overrides", "fact_mode", "failure_kind", "expected_reason", "lose_cas"),
    _STARTUP_DURABLE_CASES,
    ids=[case[0] for case in _STARTUP_DURABLE_CASES],
)
def test_canary_startup_durable_parity_matrix(
    conn,
    case,
    initial_state,
    activation_overrides,
    fact_mode,
    failure_kind,
    expected_reason,
    lose_cas,
) -> None:
    activation_id = hashlib.sha256(f"{case}:activation".encode()).hexdigest()[:32]
    stable_bundle = hashlib.sha256(f"{case}:stable".encode()).hexdigest()
    candidate_sha = hashlib.sha256(f"{case}:candidate".encode()).hexdigest()
    candidate_bundle = hashlib.sha256(f"{case}:bundle".encode()).hexdigest()
    repos = repositories_for_connection(conn)
    if initial_state is not None:
        with repos.transaction():
            repos.news.arm_canary(
                activation_id=activation_id,
                baseline_bundle_sha=stable_bundle,
                candidate_manifest_sha=candidate_sha,
                candidate_bundle_sha=candidate_bundle,
                selector_version=activation_overrides.get("selector_version", CANARY_SELECTOR_VERSION),
                exposure_bps=1_000,
                eligibility_profile_sha=activation_overrides.get(
                    "eligibility_profile_sha", CANARY_ELIGIBILITY_PROFILE_SHA
                ),
                rolling_profile_sha=activation_overrides.get("rolling_profile_sha", CANARY_ROLLING_PROFILE_SHA),
                now_ms=NOW,
            )
            if initial_state != "active":
                repos.news.transition_canary(
                    activation_id=activation_id,
                    target_state=initial_state,
                    reason=f"setup_{initial_state}",
                    now_ms=NOW + 1,
                )

    facts = {}
    if fact_mode != "missing":
        compiled_bundle = (
            hashlib.sha256(f"{case}:other-bundle".encode()).hexdigest() if fact_mode == "mismatch" else candidate_bundle
        )
        facts[candidate_sha] = CandidateRuntimeFact(
            candidate_manifest_sha=candidate_sha,
            compiled_bundle_sha=compiled_bundle,
            runnable_bundle_sha=compiled_bundle if fact_mode == "runnable" else None,
            failure_kind=None if fact_mode == "runnable" else failure_kind,
        )

    before = _canary_durable_projection(conn, activation_id)
    probe = _StartupRepositoryProbe(repos.news, lose_cas=lose_cas, now_ms=NOW + 3)
    with repos.transaction():
        result = reconcile_canary_startup(probe, candidate_facts=facts, now_ms=NOW + 3)
    after = _canary_durable_projection(conn, activation_id)

    assert result is bool(expected_reason is not None and not lose_cas)
    assert len(probe.transitions) == (1 if expected_reason is not None else 0)
    if expected_reason is not None:
        assert probe.transitions[0] == {
            "activation_id": activation_id,
            "target_state": "tripped",
            "reason": expected_reason,
            "now_ms": NOW + 3,
        }
    if initial_state is None:
        assert before is None
        assert after is None
    elif expected_reason is None:
        assert after == before
    elif lose_cas:
        assert after == {
            **before,
            "state": "closed",
            "revision": before["revision"] + 1,
            "trip_reason": "concurrent_operator_close",
            "deployment_receipts": before["deployment_receipts"] + 1,
        }
    else:
        assert after == {
            **before,
            "state": "tripped",
            "revision": before["revision"] + 1,
            "trip_reason": expected_reason,
            "rollback_receipts": before["rollback_receipts"] + 1,
        }
        receipt = conn.execute(
            "SELECT payload FROM news_learning_artifacts WHERE kind = 'rollback_receipt' "
            "AND payload->>'activation_id' = %s ORDER BY created_at_ms DESC LIMIT 1",
            (activation_id,),
        ).fetchone()["payload"]
        assert receipt["reason"] == expected_reason
        assert receipt["previous_revision"] == before["revision"]
        assert receipt["new_revision"] == after["revision"]


def _canary_durable_projection(conn, activation_id):
    activation = conn.execute(
        "SELECT state, revision, trip_reason FROM news_canary_activations WHERE activation_id = %s",
        (activation_id,),
    ).fetchone()
    if activation is None:
        return None
    receipts = conn.execute(
        "SELECT kind, count(*) AS n FROM news_learning_artifacts WHERE payload->>'activation_id' = %s GROUP BY kind",
        (activation_id,),
    ).fetchall()
    counts = {str(row["kind"]): int(row["n"]) for row in receipts}
    return {
        **dict(activation),
        "deployment_receipts": counts.get("deployment_receipt", 0),
        "rollback_receipts": counts.get("rollback_receipt", 0),
    }


def test_canary_startup_transition_rolls_back_with_its_transaction(conn) -> None:
    activation_id = "5" * 32
    stable_bundle = "6" * 64
    candidate_sha = "7" * 64
    candidate_bundle = "8" * 64
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.arm_canary(
            activation_id=activation_id,
            baseline_bundle_sha=stable_bundle,
            candidate_manifest_sha=candidate_sha,
            candidate_bundle_sha=candidate_bundle,
            selector_version=CANARY_SELECTOR_VERSION,
            exposure_bps=1_000,
            eligibility_profile_sha=CANARY_ELIGIBILITY_PROFILE_SHA,
            rolling_profile_sha=CANARY_ROLLING_PROFILE_SHA,
            now_ms=NOW,
        )
        repos.news.transition_canary(
            activation_id=activation_id,
            target_state="armed",
            reason="operator_hold",
            now_ms=NOW + 1,
        )

    facts = {
        candidate_sha: CandidateRuntimeFact(
            candidate_manifest_sha=candidate_sha,
            compiled_bundle_sha=candidate_bundle,
            runnable_bundle_sha=None,
            failure_kind="runtime_unavailable",
        )
    }
    with pytest.raises(RuntimeError, match="startup_after_transition_failure"), repos.transaction():
        assert reconcile_canary_startup(repos.news, candidate_facts=facts, now_ms=NOW + 2)
        raise RuntimeError("startup_after_transition_failure")

    activation = conn.execute(
        "SELECT state, revision, trip_reason FROM news_canary_activations WHERE activation_id = %s",
        (activation_id,),
    ).fetchone()
    assert activation == {"state": "armed", "revision": 2, "trip_reason": None}
    receipt_count = conn.execute(
        "SELECT count(*) AS n FROM news_learning_artifacts WHERE kind = 'rollback_receipt' "
        "AND payload->>'activation_id' = %s AND payload->>'reason' = 'candidate_runtime_unavailable'",
        (activation_id,),
    ).fetchone()
    assert receipt_count["n"] == 0


def test_existing_candidate_assignment_revalidates_profile_before_retry(conn) -> None:
    event_id = _open_event(conn)
    activation_id = "6" * 32
    stable_bundle = "7" * 64
    candidate_bundle = "8" * 64
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.arm_canary(
            activation_id=activation_id,
            baseline_bundle_sha=stable_bundle,
            candidate_manifest_sha="9" * 64,
            candidate_bundle_sha=candidate_bundle,
            selector_version=CANARY_SELECTOR_VERSION,
            exposure_bps=1_000,
            eligibility_profile_sha=CANARY_ELIGIBILITY_PROFILE_SHA,
            rolling_profile_sha=CANARY_ROLLING_PROFILE_SHA,
            now_ms=NOW,
        )
        conn.execute(
            """
            INSERT INTO news_agent_assignments(
              event_id, activation_id, arm, bundle_sha, selector_version,
              eligibility_reason, assigned_at_ms
            ) VALUES (%s, %s, 'candidate', %s, %s, 'eligible_bucket', %s)
            """,
            (event_id, activation_id, candidate_bundle, CANARY_SELECTOR_VERSION, NOW + 1),
        )
        conn.execute(
            "UPDATE news_canary_activations SET eligibility_profile_sha = %s WHERE activation_id = %s",
            ("f" * 64, activation_id),
        )

    with repos.transaction():
        assignment = repos.news.assign_agent_arm(
            event_id=event_id,
            stable_bundle_sha=stable_bundle,
            admission="candidate",
            ingest_mode="live",
            now_ms=NOW + 2,
        )

    assert assignment["arm"] == "candidate"
    assert assignment["bundle_sha"] == candidate_bundle
    assert assignment["validation_error"] == "eligibility_profile_hash_mismatch"
    activation = conn.execute(
        "SELECT state, trip_reason FROM news_canary_activations WHERE activation_id = %s",
        (activation_id,),
    ).fetchone()
    assert activation == {"state": "tripped", "trip_reason": "eligibility_profile_hash_mismatch"}


def test_runtime_manifest_appends_active_agent_and_rollback_window_receipts(conn) -> None:
    repos = repositories_for_connection(conn)
    first = {
        "manifest_sha": "1" * 64,
        "stable_bundle_sha": "2" * 64,
        "envelope_sha256": "4" * 64,
        "artifact_schema_version": "news_program_strategy_artifact_v1",
        "program_version": PROGRAM_VERSION,
        "program_sha256": "5" * 64,
        "candidate_shas": ("3" * 64,),
        "image_digest": "sha256:first",
        "runtime_revision": "git:first",
        "now_ms": NOW,
    }
    with repos.transaction():
        repos.news.register_agent_runtime_manifest(**first)
        repos.news.register_agent_runtime_manifest(**{**first, "now_ms": NOW + 1})
    assert conn.execute("SELECT count(*) AS n FROM news_agent_runtime_manifests").fetchone()["n"] == 1
    assert (
        conn.execute("SELECT count(*) AS n FROM news_learning_artifacts WHERE kind = 'active_agent'").fetchone()["n"]
        == 1
    )
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM news_learning_artifacts WHERE kind = 'deployment_receipt' "
            "AND payload->>'action' = 'runtime_deploy'"
        ).fetchone()["n"]
        == 1
    )
    active = conn.execute("SELECT payload FROM news_learning_artifacts WHERE kind = 'active_agent'").fetchone()[
        "payload"
    ]
    assert active["stable_sha"] == "2" * 64
    deployment = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE kind = 'deployment_receipt' "
        "AND payload->>'action' = 'runtime_deploy'"
    ).fetchone()["payload"]
    assert deployment["previous_image_digest"] is None
    assert deployment["rollback_available_until_ms"] == NOW + 24 * 3_600_000

    with repos.transaction():
        repos.news.register_agent_runtime_manifest(
            manifest_sha="4" * 64,
            stable_bundle_sha="5" * 64,
            envelope_sha256="6" * 64,
            artifact_schema_version="news_program_strategy_artifact_v1",
            program_version=PROGRAM_VERSION,
            program_sha256="7" * 64,
            candidate_shas=(),
            image_digest="sha256:second",
            runtime_revision="git:second",
            now_ms=NOW + 10,
        )
    latest = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE kind = 'deployment_receipt' "
        "AND payload->>'action' = 'runtime_deploy' ORDER BY created_at_ms DESC LIMIT 1"
    ).fetchone()["payload"]
    assert latest["previous_stable_sha"] == "2" * 64
    assert latest["previous_image_digest"] == "sha256:first"

    with repos.transaction():
        repos.news.register_agent_runtime_manifest(**{**first, "now_ms": NOW + 20})

    assert conn.execute("SELECT count(*) AS n FROM news_agent_runtime_manifests").fetchone()["n"] == 2
    active_rows = conn.execute(
        "SELECT artifact_sha, parent_sha, payload FROM news_learning_artifacts "
        "WHERE kind = 'active_agent' ORDER BY created_at_ms"
    ).fetchall()
    assert len(active_rows) == 3
    assert active_rows[-1]["parent_sha"] == active_rows[-2]["artifact_sha"]
    assert active_rows[-1]["payload"]["runtime_manifest_sha"] == first["manifest_sha"]
    assert active_rows[-1]["payload"]["image_digest"] == first["image_digest"]
    assert active_rows[-1]["payload"]["registered_at_ms"] == NOW + 20
    deployment_rows = conn.execute(
        "SELECT parent_sha, payload FROM news_learning_artifacts WHERE kind = 'deployment_receipt' "
        "AND payload->>'action' = 'runtime_deploy' ORDER BY created_at_ms"
    ).fetchall()
    assert len(deployment_rows) == 3
    rollback_row = deployment_rows[-1]
    rollback = rollback_row["payload"]
    assert rollback_row["parent_sha"] == active_rows[-1]["artifact_sha"]
    assert rollback["active_agent_sha"] == active_rows[-1]["artifact_sha"]
    assert rollback["image_digest"] == "sha256:first"
    assert rollback["stable_sha"] == "2" * 64
    assert rollback["previous_image_digest"] == "sha256:second"
    assert rollback["previous_stable_sha"] == "5" * 64


def test_rolling_canary_slo_survives_repository_restart_and_fails_closed(conn) -> None:
    source_event_id = _open_event(conn)
    activation_id = "1" * 32
    stable_bundle = "2" * 64
    candidate_bundle = "3" * 64
    repos = repositories_for_connection(conn)
    hour = 3_600_000
    first_bucket = (NOW // hour + 1) * hour
    with repos.transaction():
        repos.news.arm_canary(
            activation_id=activation_id,
            baseline_bundle_sha=stable_bundle,
            candidate_manifest_sha="4" * 64,
            candidate_bundle_sha=candidate_bundle,
            selector_version=CANARY_SELECTOR_VERSION,
            exposure_bps=1_000,
            eligibility_profile_sha=CANARY_ELIGIBILITY_PROFILE_SHA,
            rolling_profile_sha=CANARY_ROLLING_PROFILE_SHA,
            now_ms=first_bucket - hour,
        )
        for index in range(8):
            event_id = _clone_event(
                conn,
                source_event_id,
                suffix=f"missing-verdict-{index}",
                opened_at_ms=first_bucket - 30 * 60_000 + index,
            )
            conn.execute(
                """
                INSERT INTO news_agent_assignments(
                  event_id, activation_id, arm, bundle_sha, selector_version,
                  eligibility_reason, assigned_at_ms
                ) VALUES (%s, %s, 'candidate', %s, %s,
                          'eligible_bucket', %s)
                """,
                (
                    event_id,
                    activation_id,
                    candidate_bundle,
                    CANARY_SELECTOR_VERSION,
                    first_bucket - 30 * 60_000 + index,
                ),
            )
        first = repos.news.evaluate_canary_rolling_slo(activation_id=activation_id, now_ms=first_bucket)
    assert first == {
        "evaluated": True,
        "bucket_ms": first_bucket,
        "candidate_n": 8,
        "bad_n": 8,
        "breached": True,
        "breach_windows": 1,
        "tripped": False,
    }

    # A fresh repository object represents a worker restart.  The second
    # breach comes from the durable counter, not process memory.
    restarted = repositories_for_connection(conn)
    with restarted.transaction():
        second = restarted.news.evaluate_canary_rolling_slo(
            activation_id=activation_id,
            now_ms=first_bucket + hour,
        )
    assert second["breach_windows"] == 2
    assert second["tripped"] is True
    activation = conn.execute(
        "SELECT state, trip_reason, rolling_breach_windows FROM news_canary_activations WHERE activation_id = %s",
        (activation_id,),
    ).fetchone()
    assert activation == {
        "state": "tripped",
        "trip_reason": "candidate_rolling_error_slo_trip",
        "rolling_breach_windows": 2,
    }

    next_event = _clone_event(
        conn,
        source_event_id,
        suffix="after-trip",
        opened_at_ms=first_bucket + hour + 1,
    )
    with restarted.transaction():
        assignment = restarted.news.assign_agent_arm(
            event_id=next_event,
            stable_bundle_sha=stable_bundle,
            admission="candidate",
            ingest_mode="live",
            now_ms=first_bucket + hour + 1,
        )
    assert assignment["arm"] == "stable"
    assert assignment["activation_id"] is None
    assert assignment["eligibility_reason"] == "no_active_canary"
