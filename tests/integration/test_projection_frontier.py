from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from tests.postgres_test_utils import connect_postgres_test, prepare_postgres_database
from tracefold.app.cli.commands.queue_ops import QUEUE_RETRY_TRANSITIONS
from tracefold.platform.postgres.projection_frontier import (
    PROFILE_FRONTIER,
    ProjectionFrontierRepository,
)
from tracefold.platform.postgres.queue_terminal import resolve_terminal_event


def test_typed_frontier_coalesces_recovers_and_quarantines_by_input_version():
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    key = {"target_type": "Asset", "target_id": "asset:test:frontier"}
    runtime_id = str(uuid4())
    repo = ProjectionFrontierRepository(conn)
    try:
        with conn.transaction():
            assert (
                repo.mark_dirty(
                    PROFILE_FRONTIER,
                    key=key,
                    dirty_at_ms=1_000,
                    deadline_at_ms=31_000,
                    input_fingerprint="sha256:input-a",
                    version="profile-v1",
                )
                == 1
            )
            assert (
                repo.mark_dirty(
                    PROFILE_FRONTIER,
                    key=key,
                    dirty_at_ms=2_000,
                    deadline_at_ms=32_000,
                    input_fingerprint="sha256:input-a",
                    version="profile-v1",
                )
                == 1
            )
            assert (
                repo.mark_dirty(
                    PROFILE_FRONTIER,
                    key=key,
                    dirty_at_ms=3_000,
                    deadline_at_ms=33_000,
                    input_fingerprint="sha256:input-a2",
                    version="profile-v1",
                )
                == 1
            )

        due = repo.next_due(PROFILE_FRONTIER, now_ms=1_000)
        assert due is not None
        assert due["first_dirty_at_ms"] == 1_000
        assert due["deadline_at_ms"] == 31_000
        assert due["input_fingerprint"] == "sha256:input-a2"

        with conn.transaction():
            claim = repo.claim(
                PROFILE_FRONTIER,
                key=key,
                runtime_id=runtime_id,
                now_ms=31_000,
                lease_ms=5_000,
            )
        assert claim is not None
        assert str(claim["claimed_by"]) == runtime_id

        with conn.transaction():
            assert repo.release_stale(
                PROFILE_FRONTIER,
                key=key,
                runtime_id=runtime_id,
                now_ms=31_100,
            )
        row = _frontier_row(conn, key)
        assert row["attempt_count"] == 0
        assert row["status"] == "dirty"
        conn.commit()

        for attempt in range(1, 4):
            with conn.transaction():
                claim = repo.claim(
                    PROFILE_FRONTIER,
                    key=key,
                    runtime_id=runtime_id,
                    now_ms=40_000 * attempt,
                    lease_ms=5_000,
                )
                assert claim is not None
                failed = repo.fail_deterministic(
                    PROFILE_FRONTIER,
                    key=key,
                    runtime_id=runtime_id,
                    error_code="compute_timeout",
                    now_ms=40_000 * attempt,
                )
                assert failed is not None
                assert failed["attempt_count"] == attempt

        row = _frontier_row(conn, key)
        assert row["status"] == "quarantined"
        terminal = conn.execute(
            """
            SELECT final_status, attempt_count, payload_hash
            FROM queue_terminal_events
            WHERE owner_key = 'profile_projection'
              AND source_table = 'token_profile_projection_frontiers'
              AND operator_action IS NULL
            """
        ).fetchone()
        assert terminal == {
            "final_status": "quarantined",
            "attempt_count": 3,
            "payload_hash": "sha256:input-a2",
        }
        conn.commit()

        terminal_id = str(
            conn.execute(
                """
                SELECT terminal_id
                FROM queue_terminal_events
                WHERE owner_key = 'profile_projection'
                  AND source_table = 'token_profile_projection_frontiers'
                  AND operator_action IS NULL
                """
            ).fetchone()["terminal_id"]
        )
        transition = QUEUE_RETRY_TRANSITIONS[("profile_projection", "token_profile_projection_frontiers")]
        with conn.transaction():
            resolved = resolve_terminal_event(
                conn,
                terminal_id=terminal_id,
                action="retry",
                reason="fixed projection output envelope",
                now_ms=160_000,
                retry_transitions={
                    (
                        "profile_projection",
                        "token_profile_projection_frontiers",
                    ): lambda event, *, now_ms, reason: transition(
                        SimpleNamespace(projection_frontiers=repo),
                        event,
                        now_ms=now_ms,
                        reason=reason,
                    )
                },
            )
        assert resolved["operator_action"] == "retry"
        assert resolved["transition"]["requeued"] == 1
        row = _frontier_row(conn, key)
        assert row["status"] == "dirty"
        assert row["attempt_count"] == 0
        assert row["first_dirty_at_ms"] == 1_000
        assert row["deadline_at_ms"] == 31_000
        assert row["last_error_code"] is None

        with conn.transaction():
            retried_claim = repo.claim(
                PROFILE_FRONTIER,
                key=key,
                runtime_id=runtime_id,
                now_ms=160_000,
                lease_ms=5_000,
            )
            assert retried_claim is not None
            assert repo.complete(
                PROFILE_FRONTIER,
                key=key,
                runtime_id=runtime_id,
                input_fingerprint="sha256:input-a2",
                version="profile-v1",
                now_ms=160_100,
            )

        with conn.transaction():
            repo.mark_dirty(
                PROFILE_FRONTIER,
                key=key,
                dirty_at_ms=200_000,
                deadline_at_ms=230_000,
                input_fingerprint="sha256:input-b",
                version="profile-v1",
            )
        row = _frontier_row(conn, key)
        assert row["status"] == "dirty"
        assert row["attempt_count"] == 0
        assert row["first_dirty_at_ms"] == 200_000
        assert row["last_error_code"] is None
    finally:
        conn.execute(
            """
            DELETE FROM queue_terminal_events
            WHERE owner_key = 'profile_projection'
              AND source_table = 'token_profile_projection_frontiers'
            """
        )
        conn.execute(
            """
            DELETE FROM token_profile_projection_frontiers
            WHERE target_type = %s AND target_id = %s
            """,
            (key["target_type"], key["target_id"]),
        )
        conn.commit()
        conn.close()


def test_frontier_completion_is_input_and_version_cas():
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    key = {"target_type": "Asset", "target_id": "asset:test:frontier-cas"}
    runtime_id = str(uuid4())
    repo = ProjectionFrontierRepository(conn)
    try:
        with conn.transaction():
            repo.mark_dirty(
                PROFILE_FRONTIER,
                key=key,
                dirty_at_ms=1_000,
                deadline_at_ms=31_000,
                input_fingerprint="sha256:input-a",
                version="profile-v1",
            )
            assert repo.claim(
                PROFILE_FRONTIER,
                key=key,
                runtime_id=runtime_id,
                now_ms=31_000,
                lease_ms=5_000,
            )
        with conn.transaction():
            assert not repo.complete(
                PROFILE_FRONTIER,
                key=key,
                runtime_id=runtime_id,
                input_fingerprint="sha256:wrong",
                version="profile-v1",
                now_ms=31_100,
            )
            assert repo.release_stale(
                PROFILE_FRONTIER,
                key=key,
                runtime_id=runtime_id,
                now_ms=31_100,
            )
        assert _frontier_row(conn, key)["attempt_count"] == 0
    finally:
        conn.execute(
            """
            DELETE FROM token_profile_projection_frontiers
            WHERE target_type = %s AND target_id = %s
            """,
            (key["target_type"], key["target_id"]),
        )
        conn.commit()
        conn.close()


def test_dirty_frontier_uses_next_attempt_only_as_eligibility_not_as_deadline():
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    key = {
        "target_type": "Asset",
        "target_id": "asset:test:scheduled-frontier",
    }
    repo = ProjectionFrontierRepository(conn)
    try:
        with conn.transaction():
            repo.mark_dirty(
                PROFILE_FRONTIER,
                key=key,
                dirty_at_ms=1_000,
                deadline_at_ms=31_000,
                eligible_at_ms=5_000,
                input_fingerprint="sha256:scheduled",
                version="profile-v1",
            )
            repo.mark_dirty(
                PROFILE_FRONTIER,
                key=key,
                dirty_at_ms=2_000,
                deadline_at_ms=32_000,
                eligible_at_ms=7_000,
                input_fingerprint="sha256:scheduled-new-input",
                version="profile-v1",
            )

        assert repo.next_due(PROFILE_FRONTIER, now_ms=4_999) is None
        due = repo.next_due(PROFILE_FRONTIER, now_ms=5_000)
        assert due is not None
        assert due["deadline_at_ms"] == 31_000
        assert due["next_attempt_at_ms"] == 5_000
        assert due["input_fingerprint"] == "sha256:scheduled-new-input"

        with conn.transaction():
            repo.mark_dirty(
                PROFILE_FRONTIER,
                key=key,
                dirty_at_ms=3_000,
                deadline_at_ms=33_000,
                input_fingerprint="sha256:material-input",
                version="profile-v1",
            )
        immediate = repo.next_due(PROFILE_FRONTIER, now_ms=3_000)
        assert immediate is not None
        assert immediate["next_attempt_at_ms"] is None
    finally:
        conn.execute(
            """
            DELETE FROM token_profile_projection_frontiers
            WHERE target_type = %s AND target_id = %s
            """,
            (key["target_type"], key["target_id"]),
        )
        conn.commit()
        conn.close()


def _frontier_row(conn, key):
    row = conn.execute(
        """
        SELECT *
        FROM token_profile_projection_frontiers
        WHERE target_type = %s AND target_id = %s
        """,
        (key["target_type"], key["target_id"]),
    ).fetchone()
    assert row is not None
    return row
