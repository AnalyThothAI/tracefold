from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
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
from tracefold.platform.postgres.projection_frontier import RADAR_FRONTIER


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


def test_target_feature_dirties_bounded_rank_sets_before_atomic_publication() -> None:
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
        target_claim = service.claim(
            key={
                "target_type": "Asset",
                "target_id": _asset_id(conn),
                "window_key": "1h",
                "venue": "all",
            },
            runtime_id=runtime_id,
            now_ms=FIXED_NOW_MS,
        )
        assert target_claim is not None
        loaded = service.load_target_feature(
            target_claim,
            now_ms=FIXED_NOW_MS,
        )
        target_projection = compute_token_radar_target_projection(loaded)
        target_result = service.publish_target_feature(
            target_claim,
            target_projection=target_projection,
            now_ms=FIXED_NOW_MS,
        )

        assert target_result["projection_status"] == "published"
        assert target_result["rank_frontiers_written"] == 2
        assert conn.execute(
            """
            SELECT count(*) AS count
            FROM token_radar_current_rows
            """
        ).fetchone() == {"count": 0}
        assert conn.execute(
            """
            SELECT target_type, target_id, venue, status
            FROM radar_projection_frontiers
            WHERE target_type = 'RankSet'
              AND target_id = 'token'
              AND window_key = '1h'
            ORDER BY venue
            """,
        ).fetchall() == [
            {
                "target_type": "RankSet",
                "target_id": "token",
                "venue": "all",
                "status": "dirty",
            },
            {
                "target_type": "RankSet",
                "target_id": "token",
                "venue": "eth",
                "status": "dirty",
            },
        ]

        results = [
            _publish_token_rank_set(
                service,
                venue=venue,
                now_ms=FIXED_NOW_MS,
            )
            for venue in ("all", "eth")
        ]
        assert {result["projection_status"] for result in results} == {"published"}
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
        frontiers = conn.execute(
            """
            SELECT target_type, venue, status,
                   first_dirty_at_ms, deadline_at_ms
            FROM radar_projection_frontiers
            WHERE window_key = '1h'
              AND (
                (target_type = 'Asset' AND target_id = %s)
                OR (target_type = 'RankSet' AND target_id = 'token')
              )
            ORDER BY target_type, venue
            """,
            (_asset_id(conn),),
        ).fetchall()
        assert frontiers == [
            {
                "target_type": "Asset",
                "venue": "all",
                "status": "clean",
                "first_dirty_at_ms": None,
                "deadline_at_ms": None,
            },
            {
                "target_type": "RankSet",
                "venue": "all",
                "status": "clean",
                "first_dirty_at_ms": None,
                "deadline_at_ms": None,
            },
            {
                "target_type": "RankSet",
                "venue": "eth",
                "status": "clean",
                "first_dirty_at_ms": None,
                "deadline_at_ms": None,
            },
        ]
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


def test_running_rank_set_coalesces_new_target_input_without_losing_it() -> None:
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
        target_key = {
            "target_type": "Asset",
            "target_id": _asset_id(conn),
            "window_key": "1h",
            "venue": "all",
        }
        target_claim = service.claim(
            key=target_key,
            runtime_id=str(uuid4()),
            now_ms=FIXED_NOW_MS,
        )
        assert target_claim is not None
        loaded = service.load_target_feature(
            target_claim,
            now_ms=FIXED_NOW_MS,
        )
        first_projection = compute_token_radar_target_projection(loaded)
        service.publish_target_feature(
            target_claim,
            target_projection=first_projection,
            now_ms=FIXED_NOW_MS,
        )

        rank_claim = service.claim(
            key={
                "target_type": "RankSet",
                "target_id": "token",
                "window_key": "1h",
                "venue": "all",
            },
            runtime_id=str(uuid4()),
            now_ms=FIXED_NOW_MS,
        )
        assert rank_claim is not None
        old_rank_loaded = service.load_rank_set(
            rank_claim,
            now_ms=FIXED_NOW_MS,
        )

        with repos.transaction():
            repos.projection_frontiers.mark_dirty(
                RADAR_FRONTIER,
                key=target_key,
                dirty_at_ms=FIXED_NOW_MS + 1,
                deadline_at_ms=FIXED_NOW_MS + 60_001,
                input_fingerprint="sha256:new-target-input",
                version=TOKEN_RADAR_PROJECTION_VERSION,
            )
        second_target_claim = service.claim(
            key=target_key,
            runtime_id=str(uuid4()),
            now_ms=FIXED_NOW_MS + 1,
        )
        assert second_target_claim is not None
        second_projection = deepcopy(first_projection)
        assert isinstance(second_projection["projected"], dict)
        second_projection["projected"]["source_event_ids_json"] = [
            "event-radar-idempotent",
            "event-radar-new-input",
        ]
        second_result = service.publish_target_feature(
            second_target_claim,
            target_projection=second_projection,
            now_ms=FIXED_NOW_MS + 1,
        )
        assert second_result["projection_status"] == "published"
        pending = conn.execute(
            """
            SELECT status, input_fingerprint,
                   pending_first_dirty_at_ms,
                   pending_deadline_at_ms,
                   pending_input_fingerprint
            FROM radar_projection_frontiers
            WHERE target_type = 'RankSet'
              AND target_id = 'token'
              AND window_key = '1h'
              AND venue = 'all'
            """
        ).fetchone()
        assert pending is not None
        assert pending["status"] == "running"
        assert pending["input_fingerprint"] == rank_claim.input_fingerprint
        assert pending["pending_first_dirty_at_ms"] == FIXED_NOW_MS + 1
        assert pending["pending_deadline_at_ms"] == FIXED_NOW_MS + 60_001
        assert pending["pending_input_fingerprint"] is not None

        _publish_claimed_token_rank_set(
            service,
            claim=rank_claim,
            loaded=old_rank_loaded,
            now_ms=FIXED_NOW_MS + 2,
        )
        promoted = conn.execute(
            """
            SELECT status, first_dirty_at_ms, deadline_at_ms,
                   pending_input_fingerprint
            FROM radar_projection_frontiers
            WHERE target_type = 'RankSet'
              AND target_id = 'token'
              AND window_key = '1h'
              AND venue = 'all'
            """
        ).fetchone()
        assert promoted == {
            "status": "dirty",
            "first_dirty_at_ms": FIXED_NOW_MS + 1,
            "deadline_at_ms": FIXED_NOW_MS + 60_001,
            "pending_input_fingerprint": None,
        }

        final_result = _publish_token_rank_set(
            service,
            venue="all",
            now_ms=FIXED_NOW_MS + 2,
        )
        assert final_result["frontier_status"] == "clean"
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
        assert result["shards_computed"] == 10
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


def _publish_token_rank_set(
    service: RadarProjectionService,
    *,
    venue: str,
    now_ms: int,
) -> dict[str, Any]:
    claim = service.claim(
        key={
            "target_type": "RankSet",
            "target_id": "token",
            "window_key": "1h",
            "venue": venue,
        },
        runtime_id=str(uuid4()),
        now_ms=now_ms,
    )
    assert claim is not None
    loaded = service.load_rank_set(claim, now_ms=now_ms)
    return _publish_claimed_token_rank_set(
        service,
        claim=claim,
        loaded=loaded,
        now_ms=now_ms,
    )


def _publish_claimed_token_rank_set(
    service: RadarProjectionService,
    *,
    claim: Any,
    loaded: dict[str, Any],
    now_ms: int,
) -> dict[str, Any]:
    ranked = rank_token_radar_closure(
        {
            **loaded,
            "feature": None,
            "venues": [claim.venue],
            "rank_limit": 100,
        }
    )
    hydrated = service.load_hydration(
        claim,
        target_projection={},
        ranked=ranked,
    )
    closure = build_token_radar_current_closure(
        {
            "feature": None,
            "selected_by_venue": ranked["selected_by_venue"],
            "hydrated_inputs": hydrated,
        }
    )
    return service.publish_token_rank_set(
        claim,
        ranked=ranked,
        closure=closure,
        now_ms=now_ms,
    )


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
