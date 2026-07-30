from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from psycopg import pq

from tests.integration.test_token_radar_idempotency import (
    EVENT_MS,
    FIXED_NOW_MS,
    _seed_resolved_radar_source,
)
from tests.postgres_test_utils import (
    connect_postgres_test,
    repository_session_for_connection,
    reset_postgres_schema,
)
from tracefold.market.profiles.profile_projection import (
    ProfileProjectionService,
    _fingerprint,
    _serialized_size,
    compute_profile_current_projection,
    rebuild_all_profiles_for_maintenance,
)
from tracefold.market.profiles.token_profile_current_projection import (
    project_token_profile_current,
)
from tracefold.market.radar.projection import RadarProjectionService
from tracefold.market.radar.token_radar_projector import (
    build_token_radar_current_closure,
    compute_token_radar_target_projection,
    rank_token_radar_closure,
)
from tracefold.platform.postgres.projection_frontier import PROFILE_FRONTIER

NOW_MS = 1_800_000_000_000
TARGET_ID = "asset:eip155:1:erc20:0x1111111111111111111111111111111111111111"


class _SingleConnectionDB:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def worker_session(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> Iterator[Any]:
        try:
            with repository_session_for_connection(self.conn) as repos:
                yield repos
        finally:
            if self.conn.info.transaction_status != pq.TransactionStatus.IDLE:
                self.conn.rollback()


def test_profile_snapshot_fingerprint_accepts_stable_composite_source_keys() -> None:
    first = {
        ("gmgn", "https://example.com/a.png"): {"status": "ready"},
        ("okx", "https://example.com/b.png"): {"status": "pending"},
    }
    second = dict(reversed(tuple(first.items())))

    assert _fingerprint(first) == _fingerprint(second)
    assert _serialized_size(first) == _serialized_size(second)


def test_outside_serving_profile_shard_deletes_current_and_recovery_state() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.token_profiles.upsert_current(
                project_token_profile_current(
                    target={"target_type": "Asset", "target_id": TARGET_ID},
                    gmgn_openapi=None,
                    binance_web3=None,
                    gmgn_stream=None,
                    okx_dex=None,
                    computed_at_ms=NOW_MS - 1,
                    image_states_by_source_key={},
                )
            )
            conn.execute(
                """
                INSERT INTO asset_profile_refresh_targets(
                  provider, target_type, target_id, chain_id, address, symbol,
                  dirty_reason, payload_hash, source_watermark_ms, heat_tier,
                  priority, due_at_ms, leased_until_ms, lease_owner,
                  attempt_count, last_error, terminal_reason,
                  first_dirty_at_ms, updated_at_ms
                )
                VALUES (
                  'gmgn_dex_profile', 'Asset', %s, 'eip155:1',
                  '0x1111111111111111111111111111111111111111', 'TEST',
                  'legacy', 'legacy', 1, 'cold', 100, %s,
                  NULL, NULL, 0, NULL, NULL, %s, %s
                )
                """,
                (TARGET_ID, NOW_MS, NOW_MS, NOW_MS),
            )
            conn.execute(
                """
                INSERT INTO token_image_source_dirty_targets(
                  source_url_hash, source_url, source_provider, source_kind,
                  target_type, target_id, raw_ref_json, dirty_reason,
                  payload_hash, source_watermark_ms, priority, due_at_ms,
                  leased_until_ms, lease_owner, attempt_count, last_error,
                  first_dirty_at_ms, updated_at_ms
                )
                VALUES (
                  'hash', 'https://example.com/logo.png', 'fixture', 'fixture',
                  'Asset', %s, '{}', 'legacy', 'legacy', 1, 100, %s,
                  NULL, NULL, 0, NULL, %s, %s
                )
                """,
                (TARGET_ID, NOW_MS, NOW_MS, NOW_MS),
            )
            repos.projection_frontiers.mark_dirty(
                PROFILE_FRONTIER,
                key={"target_type": "Asset", "target_id": TARGET_ID},
                dirty_at_ms=NOW_MS,
                deadline_at_ms=NOW_MS + 30_000,
                input_fingerprint="sha256:outside-serving",
                version="token-profile-current-serving-v1",
            )

        service = ProfileProjectionService(db=_SingleConnectionDB(conn))
        claim = service.claim(
            target_type="Asset",
            target_id=TARGET_ID,
            runtime_id=str(uuid4()),
            now_ms=NOW_MS + 30_000,
        )
        assert claim is not None
        loaded = service.load_target(claim, now_ms=NOW_MS + 30_000)
        assert loaded["serving"] is False
        output = compute_profile_current_projection(loaded)
        result = service.publish(
            claim,
            loaded=loaded,
            output=output,
            now_ms=NOW_MS + 30_001,
        )

        assert result == {
            "projection_status": "deleted_outside_serving",
            "rows_written": 3,
            "target_type": "Asset",
            "target_id": TARGET_ID,
        }
        assert (
            conn.execute(
                """
                SELECT
                  (SELECT count(*) FROM token_profile_current
                    WHERE target_type = 'Asset' AND target_id = %s)
                  + (SELECT count(*) FROM asset_profile_refresh_targets
                    WHERE target_type = 'Asset' AND target_id = %s)
                  + (SELECT count(*) FROM token_image_source_dirty_targets
                    WHERE target_type = 'Asset' AND target_id = %s)
                  + (SELECT count(*) FROM token_profile_projection_frontiers
                    WHERE target_type = 'Asset' AND target_id = %s)
                  AS count
                """,
                (TARGET_ID, TARGET_ID, TARGET_ID, TARGET_ID),
            ).fetchone()["count"]
            == 0
        )
    finally:
        conn.close()


def test_serving_profile_projection_is_one_target_and_activates_only_provider_recovery() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        _seed_resolved_radar_source(conn)
        conn.commit()
        target_id = _seed_radar_current(conn)
        service = ProfileProjectionService(db=_SingleConnectionDB(conn))
        claim = service.claim(
            target_type="Asset",
            target_id=target_id,
            runtime_id=str(uuid4()),
            now_ms=FIXED_NOW_MS + 30_000,
        )
        assert claim is not None

        loaded = service.load_target(
            claim,
            now_ms=FIXED_NOW_MS + 30_000,
        )
        assert loaded["serving"] is True
        output = compute_profile_current_projection(loaded)
        assert output["operation"] == "upsert"
        result = service.publish(
            claim,
            loaded=loaded,
            output=output,
            now_ms=FIXED_NOW_MS + 30_001,
        )

        assert result["projection_status"] == "published"
        profile = conn.execute(
            """
            SELECT target_type, target_id, status
            FROM token_profile_current
            WHERE target_type = 'Asset' AND target_id = %s
            """,
            (target_id,),
        ).fetchone()
        assert profile == {
            "target_type": "Asset",
            "target_id": target_id,
            "status": "missing",
        }
        providers = conn.execute(
            """
            SELECT provider, heat_tier, due_at_ms
            FROM asset_profile_refresh_targets
            WHERE target_type = 'Asset' AND target_id = %s
            ORDER BY provider
            """,
            (target_id,),
        ).fetchall()
        assert providers == [
            {
                "provider": "binance_web3_profile",
                "heat_tier": "hot",
                "due_at_ms": FIXED_NOW_MS + 30_001,
            },
            {
                "provider": "gmgn_dex_profile",
                "heat_tier": "hot",
                "due_at_ms": FIXED_NOW_MS + 30_001,
            },
        ]
        frontier = conn.execute(
            """
            SELECT status, first_dirty_at_ms, deadline_at_ms
            FROM token_profile_projection_frontiers
            WHERE target_type = 'Asset' AND target_id = %s
            """,
            (target_id,),
        ).fetchone()
        assert frontier == {
            "status": "clean",
            "first_dirty_at_ms": None,
            "deadline_at_ms": None,
        }
    finally:
        conn.close()


def test_profile_maintenance_sweeps_history_and_rebuilds_only_serving_set(
    tmp_path,
) -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        _seed_resolved_radar_source(conn)
        conn.commit()
        target_id = _seed_radar_current(conn)
        outside_target = "asset:eip155:1:erc20:0x2222222222222222222222222222222222222222"
        with repository_session_for_connection(conn) as repos, repos.transaction():
            repos.token_profiles.upsert_current(
                project_token_profile_current(
                    target={
                        "target_type": "Asset",
                        "target_id": outside_target,
                    },
                    gmgn_openapi=None,
                    binance_web3=None,
                    gmgn_stream=None,
                    okx_dex=None,
                    computed_at_ms=NOW_MS - 1,
                    image_states_by_source_key={},
                )
            )

        result = rebuild_all_profiles_for_maintenance(
            db=_SingleConnectionDB(conn),
            app_home=tmp_path,
            now_ms=NOW_MS,
        )

        assert result["projection_status"] == "rebuilt"
        assert result["serving_targets"] == 1
        assert result["cleanup"]["profile_current"] == 1
        assert conn.execute(
            """
            SELECT target_id
            FROM token_profile_current
            ORDER BY target_id
            """
        ).fetchall() == [{"target_id": target_id}]
    finally:
        conn.close()


def _seed_radar_current(conn: Any) -> str:
    target_id = str(
        conn.execute(
            """
            SELECT asset_id
            FROM registry_assets
            WHERE address = '0x1111111111111111111111111111111111111111'
            """
        ).fetchone()["asset_id"]
    )
    with repository_session_for_connection(conn) as repos, repos.transaction():
        repos.radar_source_edges.sync_event(
            event_id="event-radar-idempotent",
            now_ms=EVENT_MS,
        )
    radar = RadarProjectionService(db=_SingleConnectionDB(conn))
    claim = radar.claim(
        key={
            "target_type": "Asset",
            "target_id": target_id,
            "window_key": "1h",
            "venue": "all",
        },
        runtime_id=str(uuid4()),
        now_ms=FIXED_NOW_MS,
    )
    assert claim is not None
    loaded = radar.load_target(claim, now_ms=FIXED_NOW_MS)
    target_projection = compute_token_radar_target_projection(loaded)
    ranked = rank_token_radar_closure(
        {
            **loaded,
            "feature": target_projection["feature"],
            "venues": [claim.venue],
            "rank_limit": 100,
        }
    )
    hydrated = radar.load_hydration(
        claim,
        target_projection=target_projection,
        ranked=ranked,
    )
    closure = build_token_radar_current_closure(
        {
            "feature": target_projection["feature"],
            "selected_by_venue": ranked["selected_by_venue"],
            "hydrated_inputs": hydrated,
        }
    )
    result = radar.publish(
        claim,
        target_projection=target_projection,
        ranked=ranked,
        closure=closure,
        now_ms=FIXED_NOW_MS,
    )
    assert result["projection_status"] == "published"
    return target_id
