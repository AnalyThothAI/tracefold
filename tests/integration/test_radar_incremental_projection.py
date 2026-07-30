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
from tracefold.app.repositories import repositories_for_connection
from tracefold.market.radar.constants import TOKEN_RADAR_PROJECTION_VERSION, WINDOW_MS
from tracefold.market.radar.projection import (
    RadarProjectionService,
    rebuild_all_token_radar_for_maintenance,
)
from tracefold.market.radar.token_radar_projector import (
    build_token_radar_current_closure,
    compute_token_radar_target_projection,
    rank_token_radar_closure,
)


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


def test_material_event_sync_writes_only_stable_window_edges_and_typed_frontiers() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        _seed_resolved_radar_source(conn)
        conn.commit()
        repos = repositories_for_connection(conn)

        with repos.transaction():
            first = repos.radar_source_edges.sync_event(
                event_id="event-radar-idempotent",
                now_ms=EVENT_MS,
            )
        with repos.transaction():
            second = repos.radar_source_edges.sync_event(
                event_id="event-radar-idempotent",
                now_ms=EVENT_MS,
            )

        edges = [
            dict(row)
            for row in conn.execute(
                """
                SELECT target_type, target_id, window_key, venue, source_kind,
                       source_id, observed_at_ms, expires_at_ms
                FROM radar_source_edges
                ORDER BY window_key
                """
            ).fetchall()
        ]
        frontiers = [
            dict(row)
            for row in conn.execute(
                """
                SELECT target_type, target_id, window_key, venue, status,
                       first_dirty_at_ms, deadline_at_ms, projection_version
                FROM radar_projection_frontiers
                ORDER BY window_key
                """
            ).fetchall()
        ]

        assert first == 4
        assert second == 0
        assert len(edges) == 4
        assert {row["window_key"] for row in edges} == set(WINDOW_MS)
        assert {row["venue"] for row in edges} == {"all"}
        assert {row["source_id"] for row in edges} == {"event-radar-idempotent"}
        assert {row["window_key"]: row["expires_at_ms"] - row["observed_at_ms"] for row in edges} == {
            window: min(7 * window_ms, 48 * 60 * 60 * 1000) for window, window_ms in WINDOW_MS.items()
        }
        assert len(frontiers) == 4
        assert {row["status"] for row in frontiers} == {"dirty"}
        assert {row["window_key"]: row["deadline_at_ms"] - row["first_dirty_at_ms"] for row in frontiers} == {
            "5m": 10_000,
            "1h": 60_000,
            "4h": 60_000,
            "24h": 60_000,
        }
        assert {row["projection_version"] for row in frontiers} == {TOKEN_RADAR_PROJECTION_VERSION}
    finally:
        conn.close()


def test_expiry_removes_only_due_edges_and_redirties_only_affected_shards() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        _seed_resolved_radar_source(conn)
        conn.commit()
        repos = repositories_for_connection(conn)
        with repos.transaction():
            repos.radar_source_edges.sync_event(
                event_id="event-radar-idempotent",
                now_ms=EVENT_MS,
            )
        conn.execute(
            """
            UPDATE radar_projection_frontiers
            SET status = 'clean',
                first_dirty_at_ms = NULL,
                deadline_at_ms = NULL
            """
        )
        conn.commit()

        expiry_ms = EVENT_MS + 7 * WINDOW_MS["5m"]
        with repos.transaction():
            expired = repos.radar_source_edges.expire_due(
                now_ms=expiry_ms,
                limit=100,
            )

        assert expired == 1
        remaining = conn.execute(
            """
            SELECT window_key
            FROM radar_source_edges
            ORDER BY window_key
            """
        ).fetchall()
        assert {str(row["window_key"]) for row in remaining} == {"1h", "4h", "24h"}
        dirty = conn.execute(
            """
            SELECT window_key, first_dirty_at_ms, deadline_at_ms
            FROM radar_projection_frontiers
            WHERE status = 'dirty'
            """
        ).fetchall()
        assert dirty == [
            {
                "window_key": "5m",
                "first_dirty_at_ms": expiry_ms,
                "deadline_at_ms": expiry_ms + 10_000,
            }
        ]
    finally:
        conn.close()


def test_one_target_window_publishes_all_affected_venues_and_frontier_atomically() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        _seed_resolved_radar_source(conn)
        conn.commit()
        repos = repositories_for_connection(conn)
        with repos.transaction():
            repos.radar_source_edges.sync_event(
                event_id="event-radar-idempotent",
                now_ms=EVENT_MS,
            )
        service = RadarProjectionService(db=_SingleConnectionDB(conn))
        runtime_id = str(uuid4())
        claim = service.claim(
            key={
                "target_type": "Asset",
                "target_id": _asset_id(conn),
                "window_key": "1h",
                "venue": "all",
            },
            runtime_id=runtime_id,
            now_ms=FIXED_NOW_MS,
        )
        assert claim is not None

        loaded = service.load_target(claim, now_ms=FIXED_NOW_MS)
        assert conn.info.transaction_status == pq.TransactionStatus.IDLE
        target_projection = compute_token_radar_target_projection(loaded)
        assert conn.info.transaction_status == pq.TransactionStatus.IDLE
        venues = sorted(set(loaded["old_venues"]) | {target_projection["target_venue"]})
        ranked = rank_token_radar_closure(
            {
                **loaded,
                "feature": target_projection["feature"],
                "venues": venues,
                "rank_limit": 100,
            }
        )
        hydrated = service.load_hydration(
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
        result = service.publish(
            claim,
            target_projection=target_projection,
            ranked=ranked,
            closure=closure,
            now_ms=FIXED_NOW_MS,
        )

        assert result["projection_status"] == "published"
        assert set(result["venues"]) == {"all", "eth"}
        rows = conn.execute(
            """
            SELECT venue, target_type_key, identity_id
            FROM token_radar_current_rows
            WHERE projection_version = %s
              AND "window" = '1h'
            ORDER BY venue
            """,
            (TOKEN_RADAR_PROJECTION_VERSION,),
        ).fetchall()
        assert rows == [
            {
                "venue": "all",
                "target_type_key": "Asset",
                "identity_id": _asset_id(conn),
            },
            {
                "venue": "eth",
                "target_type_key": "Asset",
                "identity_id": _asset_id(conn),
            },
        ]
        frontier = conn.execute(
            """
            SELECT status, first_dirty_at_ms, deadline_at_ms
            FROM radar_projection_frontiers
            WHERE target_type = 'Asset'
              AND target_id = %s
              AND window_key = '1h'
              AND venue = 'all'
            """,
            (_asset_id(conn),),
        ).fetchone()
        assert frontier == {
            "status": "clean",
            "first_dirty_at_ms": None,
            "deadline_at_ms": None,
        }
        profile = conn.execute(
            """
            SELECT status
            FROM token_profile_projection_frontiers
            WHERE target_type = 'Asset' AND target_id = %s
            """,
            (_asset_id(conn),),
        ).fetchone()
        assert profile == {"status": "dirty"}
    finally:
        conn.close()


def test_maintenance_rebuild_seeds_edges_and_drains_typed_radar_frontiers() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        _seed_resolved_radar_source(conn)
        conn.commit()

        result = rebuild_all_token_radar_for_maintenance(
            db=_SingleConnectionDB(conn),
            now_ms=FIXED_NOW_MS,
        )

        assert result["projection_status"] == "rebuilt"
        assert result["events_scanned"] == 1
        assert result["source_edges_written"] == 4
        assert result["shards_computed"] == 4
        assert result["current_rows"] > 0
        assert conn.execute(
            """
            SELECT count(*) AS count
            FROM radar_projection_frontiers
            WHERE status <> 'clean'
            """
        ).fetchone() == {"count": 0}
    finally:
        conn.close()


def _asset_id(conn: Any) -> str:
    row = conn.execute(
        """
        SELECT asset_id
        FROM registry_assets
        WHERE address = '0x1111111111111111111111111111111111111111'
        """
    ).fetchone()
    assert row is not None
    return str(row["asset_id"])
