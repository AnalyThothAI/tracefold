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
from tracefold.market.radar.constants import (
    TOKEN_RADAR_PROJECTION_VERSION,
    WINDOW_MS,
)
from tracefold.market.radar.maintenance import (
    rebuild_all_token_radar_for_maintenance,
)
from tracefold.market.radar.microbatch import (
    RadarMicroBatchClaim,
    RadarMicroBatchService,
    compute_radar_target_batch,
    hydrate_radar_microbatch,
    rank_radar_microbatch,
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


def test_event_sync_writes_only_stable_target_frontiers() -> None:
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

        rows = conn.execute(
            """
            SELECT target_type, window_key, venue, status,
                   first_dirty_at_ms, deadline_at_ms
            FROM radar_projection_frontiers
            ORDER BY window_key
            """
        ).fetchall()
        assert first == 4
        assert second == 0
        assert len(rows) == 4
        assert {str(row["target_type"]) for row in rows} == {"Asset"}
        assert {str(row["window_key"]) for row in rows} == set(WINDOW_MS)
        assert {str(row["venue"]) for row in rows} == {"all"}
        assert {str(row["status"]) for row in rows} == {"dirty"}
        assert conn.execute(
            """
            SELECT count(*) AS count
            FROM radar_projection_frontiers
            WHERE target_type = 'RankSet'
            """
        ).fetchone() == {"count": 0}
    finally:
        conn.close()


def test_claim_batch_is_one_window_venue_and_capped_at_4() -> None:
    conn = connect_postgres_test()
    try:
        reset_postgres_schema(conn)
        repos = repositories_for_connection(conn)
        with repos.transaction():
            for index in range(40):
                repos.projection_frontiers.mark_dirty(
                    RADAR_FRONTIER,
                    key={
                        "target_type": "Asset",
                        "target_id": f"asset:{index:02d}",
                        "window_key": "1h",
                        "venue": "all",
                    },
                    dirty_at_ms=FIXED_NOW_MS,
                    deadline_at_ms=FIXED_NOW_MS,
                    input_fingerprint=f"sha256:{index:064x}",
                    version=TOKEN_RADAR_PROJECTION_VERSION,
                )

        service = RadarMicroBatchService(db=_SingleConnectionDB(conn))
        claim = service.claim_batch(
            window="1h",
            venue="all",
            runtime_id=str(uuid4()),
            now_ms=FIXED_NOW_MS,
        )

        assert claim is not None
        assert len(claim.targets) == 4
        assert [target.target_id for target in claim.targets] == [f"asset:{index:02d}" for index in range(4)]
        assert conn.execute(
            """
            SELECT count(*) AS count
            FROM radar_projection_frontiers
            WHERE status = 'dirty'
            """
        ).fetchone() == {"count": 36}
    finally:
        conn.close()


def test_running_batch_keeps_claimed_snapshot_and_replays_new_input() -> None:
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
        service = RadarMicroBatchService(db=_SingleConnectionDB(conn))
        claim = service.claim_batch(
            window="1h",
            venue="all",
            runtime_id=str(uuid4()),
            now_ms=FIXED_NOW_MS,
        )
        assert claim is not None
        target = claim.targets[0]

        with repos.transaction():
            repos.projection_frontiers.mark_dirty(
                RADAR_FRONTIER,
                key=target.key(window=claim.window, venue=claim.venue),
                dirty_at_ms=FIXED_NOW_MS + 1,
                deadline_at_ms=FIXED_NOW_MS + 1,
                input_fingerprint="sha256:new-input",
                version=TOKEN_RADAR_PROJECTION_VERSION,
            )
        running = conn.execute(
            """
            SELECT status, input_fingerprint,
                   claimed_input_fingerprint,
                   first_dirty_at_ms, deadline_at_ms
            FROM radar_projection_frontiers
            WHERE target_type = %s
              AND target_id = %s
              AND window_key = '1h'
              AND venue = 'all'
            """,
            (target.target_type, target.target_id),
        ).fetchone()
        assert running == {
            "status": "running",
            "input_fingerprint": "sha256:new-input",
            "claimed_input_fingerprint": target.input_fingerprint,
            "first_dirty_at_ms": min(
                target.first_dirty_at_ms,
                FIXED_NOW_MS + 1,
            ),
            "deadline_at_ms": min(
                target.deadline_at_ms,
                FIXED_NOW_MS + 1,
            ),
        }

        result = _publish_claim(service, claim, now_ms=FIXED_NOW_MS + 2)
        replay = conn.execute(
            """
            SELECT status, input_fingerprint,
                   claimed_input_fingerprint,
                   claimed_projection_version
            FROM radar_projection_frontiers
            WHERE target_type = %s
              AND target_id = %s
              AND window_key = '1h'
              AND venue = 'all'
            """,
            (target.target_type, target.target_id),
        ).fetchone()
        assert result["projection_status"] == "published"
        assert replay == {
            "status": "dirty",
            "input_fingerprint": "sha256:new-input",
            "claimed_input_fingerprint": None,
            "claimed_projection_version": None,
        }
    finally:
        conn.close()


def test_microbatch_publication_is_atomic_and_maintenance_reuses_it() -> None:
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
        service = RadarMicroBatchService(db=_SingleConnectionDB(conn))
        claim = service.claim_batch(
            window="1h",
            venue="all",
            runtime_id=str(uuid4()),
            now_ms=FIXED_NOW_MS,
        )
        assert claim is not None

        first = _publish_claim(service, claim, now_ms=FIXED_NOW_MS)
        assert first["projection_status"] == "published"
        assert first["targets_loaded"] == 1
        assert (
            conn.execute(
                """
            SELECT count(*) AS count
            FROM token_radar_current_rows
            WHERE projection_version = %s
              AND "window" = '1h'
            """,
                (TOKEN_RADAR_PROJECTION_VERSION,),
            ).fetchone()["count"]
            > 0
        )
        assert conn.execute(
            """
            SELECT status
            FROM radar_projection_frontiers
            WHERE target_type = 'Asset'
              AND target_id = %s
              AND window_key = '1h'
              AND venue = 'all'
            """,
            (_asset_id(conn),),
        ).fetchone() == {"status": "clean"}

        rebuilt = rebuild_all_token_radar_for_maintenance(
            db=_SingleConnectionDB(conn),
            now_ms=FIXED_NOW_MS,
        )
        assert rebuilt["projection_status"] == "rebuilt"
        assert rebuilt["events_scanned"] == 1
        assert rebuilt["source_edges_written"] == 4
        assert rebuilt["microbatches_computed"] == 4
        assert rebuilt["targets_computed"] == 4
        assert rebuilt["current_rows"] > 0
        assert conn.execute(
            """
            SELECT count(*) AS count
            FROM radar_projection_frontiers
            WHERE status <> 'clean'
            """
        ).fetchone() == {"count": 0}
    finally:
        conn.close()


def _publish_claim(
    service: RadarMicroBatchService,
    claim: RadarMicroBatchClaim,
    *,
    now_ms: int,
) -> dict[str, Any]:
    loaded = service.load_targets(claim, now_ms=now_ms)
    projections = compute_radar_target_batch(loaded)
    rank_inputs = service.load_rank_inputs(
        claim,
        projections=projections,
        now_ms=now_ms,
    )
    ranked = rank_radar_microbatch(rank_inputs)
    hydrated = service.load_hydration(claim, ranked=ranked)
    return service.publish(
        claim,
        projections=projections,
        ranked=ranked,
        closure=hydrate_radar_microbatch(
            ranked=ranked,
            hydrated_inputs=hydrated,
        ),
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
