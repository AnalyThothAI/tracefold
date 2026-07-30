from __future__ import annotations

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.market.profiles.asset_profile_refresh_worker import _retry_delay_ms

NOW_MS = 1_779_000_000_000


def test_profile_retry_delay_is_exponential_and_bounded() -> None:
    assert _retry_delay_ms(base_ms=900_000, attempt_count=1, cap_ms=86_400_000) == 900_000
    assert _retry_delay_ms(base_ms=900_000, attempt_count=3, cap_ms=86_400_000) == 3_600_000
    assert _retry_delay_ms(base_ms=900_000, attempt_count=20, cap_ms=86_400_000) == 86_400_000


def test_hot_profile_target_claims_before_older_cold_target() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        common = {
            "provider": "gmgn_dex_profile",
            "target_type": "Asset",
            "chain_id": "sol",
            "source_watermark_ms": NOW_MS,
        }
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.asset_profile_refresh_targets.enqueue_targets(
                [
                    {
                        **common,
                        "target_id": "cold",
                        "address": "cold-address",
                        "heat_tier": "cold",
                        "due_at_ms": NOW_MS - 60_000,
                    },
                    {
                        **common,
                        "target_id": "hot",
                        "address": "hot-address",
                        "heat_tier": "hot",
                        "due_at_ms": NOW_MS,
                    },
                ],
                reason="queue-policy",
                now_ms=NOW_MS,
            )
            [claimed] = repos.asset_profile_refresh_targets.claim_due(
                provider="gmgn_dex_profile",
                now_ms=NOW_MS,
                limit=1,
                lease_owner="test-worker",
                lease_ms=60_000,
            )
            assert claimed["target_id"] == "hot"
            assert claimed["priority"] == 20
    finally:
        conn.close()


def test_terminal_profile_target_only_reactivates_for_new_evidence() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        target = {
            "provider": "gmgn_dex_profile",
            "target_type": "Asset",
            "target_id": "asset-1",
            "chain_id": "sol",
            "address": "address-1",
            "symbol": "ONE",
            "source_watermark_ms": NOW_MS,
            "priority": 20,
            "heat_tier": "hot",
            "payload_hash": "sha256:evidence-v1",
        }
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.asset_profile_refresh_targets.enqueue_targets(
                [target],
                reason="token_radar_entered",
                now_ms=NOW_MS,
            )
            [claim] = repos.asset_profile_refresh_targets.claim_due(
                provider="gmgn_dex_profile",
                now_ms=NOW_MS,
                limit=1,
                lease_owner="test-worker",
                lease_ms=60_000,
            )
            assert claim["heat_tier"] == "hot"
            assert (
                repos.asset_profile_refresh_targets.mark_terminal(
                    [claim],
                    reason="profile_missing_after_max_attempts",
                    now_ms=NOW_MS,
                )
                == 1
            )
            terminal = conn.execute(
                """
                SELECT final_reason, operator_action
                FROM worker_queue_terminal_events
                WHERE worker_name = 'asset_profile_refresh'
                  AND source_table = 'asset_profile_refresh_targets'
                """
            ).fetchone()
            assert terminal == {
                "final_reason": "profile_missing_after_max_attempts",
                "operator_action": None,
            }

        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.asset_profile_refresh_targets.enqueue_targets(
                [target],
                reason="token_radar_rank_changed",
                now_ms=NOW_MS + 1,
            )
            assert (
                repos.asset_profile_refresh_targets.claim_due(
                    provider="gmgn_dex_profile",
                    now_ms=NOW_MS + 1,
                    limit=1,
                    lease_owner="test-worker",
                    lease_ms=60_000,
                )
                == []
            )

        changed = {
            **target,
            "source_watermark_ms": NOW_MS + 2,
            "payload_hash": "sha256:evidence-v2",
        }
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.asset_profile_refresh_targets.enqueue_targets(
                [changed],
                reason="token_radar_source_watermark_changed",
                now_ms=NOW_MS + 2,
            )
            [reactivated] = repos.asset_profile_refresh_targets.claim_due(
                provider="gmgn_dex_profile",
                now_ms=NOW_MS + 2,
                limit=1,
                lease_owner="test-worker",
                lease_ms=60_000,
            )
            assert reactivated["attempt_count"] == 1
            assert reactivated["terminal_reason"] is None
            audit = conn.execute(
                """
                SELECT operator_action, operator_reason
                FROM worker_queue_terminal_events
                WHERE worker_name = 'asset_profile_refresh'
                  AND source_table = 'asset_profile_refresh_targets'
                """
            ).fetchone()
            assert audit == {
                "operator_action": "retry",
                "operator_reason": "reactivated_by_new_evidence",
            }
    finally:
        conn.close()
