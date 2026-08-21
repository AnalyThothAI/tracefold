"""#118: bounded retention keeps release truth while draining stale learning evidence."""

from __future__ import annotations

import hashlib
import json

import pytest
from psycopg.errors import InsufficientPrivilege, RaiseException

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection

pytestmark = pytest.mark.integration

DAY_MS = 86_400_000


@pytest.fixture(scope="module")
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    yield connection
    connection.close()


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _artifact(
    conn,
    *,
    label: str,
    kind: str,
    payload: dict[str, object],
    created_at_ms: int,
    parent_sha: str | None = None,
) -> str:
    artifact_sha = _sha(f"artifact:{label}")
    conn.execute(
        """
        INSERT INTO news_learning_artifacts(
          artifact_sha, kind, parent_sha, payload, created_by, created_at_ms
        ) VALUES (%s,%s,%s,%s::jsonb,'retention-test',%s)
        """,
        (artifact_sha, kind, parent_sha, json.dumps(payload), created_at_ms),
    )
    return artifact_sha


def _chain(conn, *, label: str, bundle_sha: str, run_sha: str, created_at_ms: int) -> dict[str, str]:
    candidate_sha = _sha(f"candidate:{label}")
    registration_sha = _artifact(
        conn,
        label=f"{label}:registration",
        kind="candidate_registration",
        payload={"candidate_sha": candidate_sha},
        created_at_ms=created_at_ms,
    )
    development_sha = _artifact(
        conn,
        label=f"{label}:development",
        kind="dataset",
        payload={"role": "development"},
        created_at_ms=created_at_ms,
    )
    validation_sha = _artifact(
        conn,
        label=f"{label}:validation",
        kind="dataset",
        payload={"role": "validation", "observation_ref": candidate_sha},
        created_at_ms=created_at_ms,
    )
    proposal_sha = _artifact(
        conn,
        label=f"{label}:proposal",
        kind="proposal",
        payload={"candidate_sha": candidate_sha},
        created_at_ms=created_at_ms,
        parent_sha=development_sha,
    )
    candidate_artifact_sha = _artifact(
        conn,
        label=f"{label}:candidate",
        kind="candidate",
        payload={
            "candidate_sha": candidate_sha,
            "candidate_bundle_sha": bundle_sha,
            "proposal_sha": proposal_sha,
            "manifest": {
                "development_dataset_sha": development_sha,
                "proposal_receipt": {"registration_receipt_sha": registration_sha},
            },
        },
        created_at_ms=created_at_ms,
    )
    observation_sha = _artifact(
        conn,
        label=f"{label}:observation",
        kind="shadow_observation",
        payload={"candidate_sha": candidate_sha, "run_sha": run_sha},
        created_at_ms=created_at_ms,
    )
    report_sha = _artifact(
        conn,
        label=f"{label}:report",
        kind="evaluation_report",
        payload={
            "run_sha": run_sha,
            "candidate_sha": candidate_sha,
            "evidence": {
                "development_dataset_sha": development_sha,
                "validation_dataset_sha": validation_sha,
                "observation_manifest_sha": observation_sha,
            },
        },
        created_at_ms=created_at_ms,
        parent_sha=candidate_sha,
    )
    release_sha = _artifact(
        conn,
        label=f"{label}:release",
        kind="release_evidence",
        payload={
            "run_sha": run_sha,
            "candidate_sha": candidate_sha,
            "report_sha": report_sha,
            "stage": "shadow",
            "gate_outcome": "pass",
        },
        created_at_ms=created_at_ms,
        parent_sha=report_sha,
    )
    return {
        "candidate_sha": candidate_sha,
        "candidate_artifact_sha": candidate_artifact_sha,
        "registration_sha": registration_sha,
        "development_sha": development_sha,
        "validation_sha": validation_sha,
        "proposal_sha": proposal_sha,
        "observation_sha": observation_sha,
        "report_sha": report_sha,
        "release_sha": release_sha,
    }


def _case_and_recording(conn, *, label: str, run_sha: str, created_at_ms: int) -> tuple[str, str]:
    case_id = _sha(f"case:{label}")
    recording_sha = _sha(f"recording:{label}")
    conn.execute(
        """
        INSERT INTO news_learning_cases(
          run_sha, case_id, dataset_sha, dataset_role, evaluation_stage, subject_kind,
          opened_at_ms, evidence_sha256, cluster_id, stratum,
          stable_observation, candidate_observation, comparison, created_at_ms
        ) VALUES (
          %s,%s,%s,'development','offline','pairwise',%s,%s,%s,'retention',
          '{}'::jsonb,'{}'::jsonb,'{}'::jsonb,%s
        )
        """,
        (
            run_sha,
            case_id,
            _sha(f"dataset-ref:{label}"),
            created_at_ms,
            _sha(f"evidence:{label}"),
            label,
            created_at_ms,
        ),
    )
    conn.execute(
        """
        INSERT INTO news_model_recordings(
          recording_sha, run_sha, case_id, arm, trial, request_sha256,
          request, provider, model, model_sha, execution_contract_sha, created_at_ms
        ) VALUES (%s,%s,%s,'stable',1,%s,'{}'::jsonb,'test','test',%s,%s,%s)
        """,
        (
            recording_sha,
            run_sha,
            case_id,
            _sha(f"request:{label}"),
            _sha(f"model:{label}"),
            _sha(f"contract:{label}"),
            created_at_ms,
        ),
    )
    return case_id, recording_sha


def _register_runtime(conn, *, label: str, stable_bundle_sha: str, registered_at_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO news_agent_runtime_manifests(
          manifest_sha, stable_bundle_sha, candidate_shas, image_digest,
          runtime_revision, registered_at_ms
        ) VALUES (%s,%s,'[]'::jsonb,%s,%s,%s)
        """,
        (
            _sha(f"runtime:{label}"),
            stable_bundle_sha,
            f"sha256:{_sha(f'image:{label}')}",
            label,
            registered_at_ms,
        ),
    )


def test_retention_bounds_age_and_pins_current_previous_stable_chains(conn) -> None:
    now_ms = int(conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint n").fetchone()["n"])
    old = now_ms - 366 * DAY_MS
    current_bundle, previous_bundle, stale_bundle, canary_bundle = (
        _sha("bundle:current"),
        _sha("bundle:previous"),
        _sha("bundle:stale"),
        _sha("bundle:canary"),
    )
    current = _chain(conn, label="current", bundle_sha=current_bundle, run_sha=_sha("run:current"), created_at_ms=old)
    previous = _chain(
        conn, label="previous", bundle_sha=previous_bundle, run_sha=_sha("run:previous"), created_at_ms=old
    )
    stale = _chain(conn, label="stale", bundle_sha=stale_bundle, run_sha=_sha("run:stale"), created_at_ms=old)
    canary = _chain(conn, label="canary", bundle_sha=canary_bundle, run_sha=_sha("run:canary"), created_at_ms=old)
    for label in ("current", "previous", "stale", "canary"):
        _case_and_recording(conn, label=label, run_sha=_sha(f"run:{label}"), created_at_ms=old)

    # Two restarts of the current bundle must not displace the true previous stable.
    _register_runtime(conn, label="previous", stable_bundle_sha=previous_bundle, registered_at_ms=now_ms - 3)
    _register_runtime(conn, label="current-1", stable_bundle_sha=current_bundle, registered_at_ms=now_ms - 2)
    _register_runtime(conn, label="current-2", stable_bundle_sha=current_bundle, registered_at_ms=now_ms - 1)
    conn.execute(
        """
        INSERT INTO news_canary_activations(
          activation_id, baseline_bundle_sha, candidate_manifest_sha, candidate_bundle_sha,
          selector_version, exposure_bps, eligibility_profile_sha, rolling_profile_sha,
          state, created_at_ms, activated_at_ms
        ) VALUES (%s,%s,%s,%s,'retention-v1',1000,%s,%s,'active',%s,%s)
        """,
        (
            _sha("activation:canary")[:32],
            current_bundle,
            canary["candidate_sha"],
            canary_bundle,
            _sha("eligibility:canary"),
            _sha("rolling:canary"),
            now_ms - 2,
            now_ms - 1,
        ),
    )
    rollback_sha = _artifact(
        conn,
        label="stale:rollback",
        kind="rollback_receipt",
        payload={"candidate_manifest_sha": stale["candidate_sha"], "action": "canary_trip"},
        created_at_ms=old,
        parent_sha=stale["candidate_sha"],
    )
    conn.commit()

    repos = repositories_for_connection(conn)
    results = []
    for _ in range(12):
        with repos.transaction():
            results.append(repos.news.purge_learning_retention(batch_size=1))
        if all(results[-1][field] == 0 for field in ("eligible_recordings", "eligible_cases", "eligible_artifacts")):
            break

    # Batch=1 is a hard per-table ceiling; capped eligible counts expose backlog without a full count.
    assert all(result["deleted_recordings"] <= 1 for result in results)
    assert all(result["deleted_cases"] <= 1 for result in results)
    assert all(result["deleted_artifacts"] <= 1 for result in results)
    assert max(result["eligible_artifacts"] for result in results) <= 2

    remaining_runs = {
        str(row["run_sha"]) for row in conn.execute("SELECT DISTINCT run_sha FROM news_model_recordings").fetchall()
    }
    assert _sha("run:current") in remaining_runs
    assert _sha("run:previous") in remaining_runs
    assert _sha("run:canary") in remaining_runs
    assert _sha("run:stale") not in remaining_runs
    for chain in (current, previous, canary):
        assert conn.execute(
            "SELECT 1 FROM news_learning_artifacts WHERE artifact_sha = %s", (chain["validation_sha"],)
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM news_learning_artifacts WHERE artifact_sha = %s", (chain["observation_sha"],)
        ).fetchone()
    assert (
        conn.execute(
            "SELECT 1 FROM news_learning_artifacts WHERE artifact_sha = %s", (stale["release_sha"],)
        ).fetchone()
        is None
    )
    # Rollback/deployment receipts are permanent audit truth even after the stale candidate chain ages out.
    assert conn.execute("SELECT 1 FROM news_learning_artifacts WHERE artifact_sha = %s", (rollback_sha,)).fetchone()
    state = conn.execute("SELECT * FROM news_learning_retention_state WHERE singleton").fetchone()
    assert state["last_run_at_ms"] is not None and state["last_error_code"] is None
    conn.commit()


def test_unreferenced_recordings_use_90_days_and_referenced_runs_use_365(conn) -> None:
    now_ms = int(conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint n").fetchone()["n"])
    unreferenced_keep = _sha("run:unreferenced-89")
    unreferenced_delete = _sha("run:unreferenced-91")
    referenced_keep = _sha("run:referenced-364")
    _case_and_recording(conn, label="unreferenced-89", run_sha=unreferenced_keep, created_at_ms=now_ms - 89 * DAY_MS)
    _case_and_recording(conn, label="unreferenced-91", run_sha=unreferenced_delete, created_at_ms=now_ms - 91 * DAY_MS)
    _case_and_recording(conn, label="referenced-364", run_sha=referenced_keep, created_at_ms=now_ms - 364 * DAY_MS)
    _artifact(
        conn,
        label="referenced-364:report",
        kind="evaluation_report",
        payload={"run_sha": referenced_keep, "candidate_sha": _sha("candidate:referenced-364")},
        created_at_ms=now_ms - 364 * DAY_MS,
    )
    conn.commit()

    with repositories_for_connection(conn).transaction():
        result = repositories_for_connection(conn).news.purge_learning_retention(batch_size=100)

    runs = {
        str(row["run_sha"]) for row in conn.execute("SELECT DISTINCT run_sha FROM news_model_recordings").fetchall()
    }
    assert unreferenced_keep in runs
    assert unreferenced_delete not in runs
    assert referenced_keep in runs
    assert result["deleted_recordings"] >= 1 and result["deleted_cases"] >= 1
    conn.commit()


def test_only_workers_can_execute_the_retention_function(conn) -> None:
    function_def = conn.execute(
        "SELECT pg_get_functiondef('purge_news_learning_retention(integer)'::regprocedure) AS definition"
    ).fetchone()["definition"]
    assert "p_batch > 1000" in function_def
    assert "LIMIT p_batch" in function_def
    assert "ORDER BY created_at_ms ASC LIMIT 1" in function_def

    conn.execute("SET ROLE tracefold_workers")
    try:
        with pytest.raises(InsufficientPrivilege):
            conn.execute("DELETE FROM news_model_recordings WHERE false")
        row = conn.execute("SELECT purge_news_learning_retention(1) AS result").fetchone()
        assert isinstance(row["result"], dict)
        with pytest.raises(RaiseException, match="news_learning_retention_batch_invalid"):
            conn.execute("SELECT purge_news_learning_retention(1001)")
    finally:
        conn.execute("RESET ROLE")
        conn.rollback()

    for role in ("tracefold_serve", "tracefold_review"):
        conn.execute(f"SET ROLE {role}")
        try:
            with pytest.raises(InsufficientPrivilege):
                conn.execute("SELECT purge_news_learning_retention(1)")
        finally:
            conn.execute("RESET ROLE")
            conn.rollback()
