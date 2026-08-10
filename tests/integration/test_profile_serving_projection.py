from __future__ import annotations

from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.market.profiles.profile_projection import _load_profile_snapshot

NOW_MS = 1_800_000_000_000


def test_profile_serving_is_owned_by_stable_registry_identity_not_radar_or_recency() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            asset = repos.registry.upsert_chain_asset(
                chain_id="eip155:1",
                address="0x1111111111111111111111111111111111111111",
                observed_at_ms=1,
            )

        with repository_session_for_connection(conn) as repos:
            serving = _load_profile_snapshot(
                repos,
                target_type="Asset",
                target_id=str(asset["asset_id"]),
                now_ms=NOW_MS,
            )
            missing = _load_profile_snapshot(
                repos,
                target_type="Asset",
                target_id="asset:missing",
                now_ms=NOW_MS,
            )
    finally:
        conn.close()

    assert serving["serving"] is True
    assert missing["serving"] is False


def test_missing_profile_backfill_reads_registry_and_identity_without_radar_rows() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            asset = repos.registry.upsert_chain_asset(
                chain_id="eip155:1",
                address="0x2222222222222222222222222222222222222222",
                observed_at_ms=NOW_MS,
            )
            repos.identity_evidence.upsert_identity_evidence(
                asset_id=str(asset["asset_id"]),
                evidence_kind="manual_identity_repair",
                provider="manual",
                lookup_mode="manual",
                chain_id=str(asset["chain_id"]),
                address=str(asset["address"]),
                symbol="TEST",
                name="Test Token",
                decimals=18,
                confidence="manual",
                observed_at_ms=NOW_MS,
            )
            repos.identity_evidence.recompute_current_identity(
                str(asset["asset_id"]),
                now_ms=NOW_MS,
            )
            result = repos.asset_profile_refresh_targets.enqueue_missing_identity_assets_for_ops(
                provider="gmgn_dex_profile",
                now_ms=NOW_MS,
                limit=10,
            )
            queued = conn.execute(
                """
                SELECT target_id, dirty_reason
                FROM asset_profile_refresh_targets
                WHERE provider = 'gmgn_dex_profile'
                """
            ).fetchall()
    finally:
        conn.close()

    assert result == {"targets": 1, "source_rows_scanned": 1}
    assert [dict(row) for row in queued] == [
        {
            "target_id": str(asset["asset_id"]),
            "dirty_reason": "identity_asset_backfill",
        }
    ]
