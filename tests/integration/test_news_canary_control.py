from __future__ import annotations

import hashlib
import json

import pytest

from tests.integration.test_news_review_desk import NOW, _open_event
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.news import apply_canary_control, parse_canary_control
from tracefold.news.canary import CANARY_ROLLING_PROFILE_SHA

pytestmark = pytest.mark.integration


@pytest.fixture()
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
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
          event_id, leader_item_id, family, comparison_fingerprint, comparison_title,
          leader_title, opened_at_ms, last_member_at_ms, expires_at_ms, member_count,
          admission, priority, provider_score_max, engine_type, asset_class,
          grounded_assets, watchlist_hits, macro_lexicon, storyline_key, context_line,
          published_at_ms, followup_of, ingest_mode, trace_id, created_at_ms, updated_at_ms,
          focus_fact_id, focus_fact_text, focus_fact_context, focus_fact_method,
          focus_span_start, focus_span_end
        )
        SELECT %s, leader_item_id, family, comparison_fingerprint || %s,
               comparison_title, leader_title, %s, %s, %s, member_count,
               admission, priority, provider_score_max, engine_type, asset_class,
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
            priority="normal",
            ingest_mode="live",
            now_ms=NOW + 1,
        )
        second = repos.news.assign_agent_arm(
            event_id=event_id,
            stable_bundle_sha=stable_bundle,
            admission="candidate",
            priority="normal",
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
            priority="normal",
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


def test_runtime_manifest_appends_active_agent_and_rollback_window_receipts(conn) -> None:
    repos = repositories_for_connection(conn)
    first = {
        "manifest_sha": "1" * 64,
        "stable_bundle_sha": "2" * 64,
        "candidate_shas": ("3" * 64,),
        "image_digest": "sha256:first",
        "runtime_revision": "git:first",
        "now_ms": NOW,
    }
    with repos.transaction():
        repos.news.register_agent_runtime_manifest(**first)
        repos.news.register_agent_runtime_manifest(**{**first, "now_ms": NOW + 1})
    assert conn.execute("SELECT count(*) AS n FROM news_agent_runtime_manifests").fetchone()["n"] == 1
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
            selector_version="news_canary_selector_v1",
            exposure_bps=1_000,
            eligibility_profile_sha="5" * 64,
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
                ) VALUES (%s, %s, 'candidate', %s, 'news_canary_selector_v1',
                          'eligible_bucket', %s)
                """,
                (event_id, activation_id, candidate_bundle, first_bucket - 30 * 60_000 + index),
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
            priority="normal",
            ingest_mode="live",
            now_ms=first_bucket + hour + 1,
        )
    assert assignment["arm"] == "stable"
    assert assignment["activation_id"] is None
    assert assignment["eligibility_reason"] == "no_active_canary"
