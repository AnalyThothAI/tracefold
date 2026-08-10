from __future__ import annotations

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.market.radar.reducer import reduce_token_radar
from tracefold.market.radar.snapshot_repository import (
    TokenRadarCurrentRepository,
    served_token_radar_snapshot,
)

NOW_MS = 1_800_000_000_000
MINUTE_MS = 60_000


def test_singleton_publish_is_state_idempotent_and_failure_preserves_lkg(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = TokenRadarCurrentRepository(conn)
        reduced = reduce_token_radar(_eligible_rows(), now_ms=NOW_MS)

        with conn.transaction():
            initial_failure_writes = repository.record_failure(
                error_code="token_radar_sample_budget_exceeded",
                evaluation_at_ms=NOW_MS - 1,
            )
        initial_failure = _stored(conn)

        with conn.transaction():
            first = repository.publish(reduced, evaluation_at_ms=NOW_MS)
        published = _stored(conn)

        irrelevant = reduce_token_radar(
            [
                *_eligible_rows(),
                {
                    **_eligible_rows()[0],
                    "target_id": "weak-target",
                    "event_id": "weak-event",
                    "received_at_ms": NOW_MS - 10 * MINUTE_MS,
                },
            ],
            now_ms=NOW_MS,
        )
        assert irrelevant.input_fingerprint != reduced.input_fingerprint
        assert irrelevant.state_fingerprint == reduced.state_fingerprint

        with conn.transaction():
            unchanged = repository.publish(irrelevant, evaluation_at_ms=NOW_MS + 1)
        after_unchanged = _stored(conn)

        with conn.transaction():
            failed_writes = repository.record_failure(
                error_code="token_radar_input_row_overflow",
                evaluation_at_ms=NOW_MS + 2,
            )
        failed = _stored(conn)

        with conn.transaction():
            recovered = repository.publish(irrelevant, evaluation_at_ms=NOW_MS + 3)
        recovered_row = _stored(conn)

    finally:
        conn.close()

    assert initial_failure_writes == 1
    assert initial_failure["latest_attempt_status"] == "failed"
    assert initial_failure["ruleset_version"] is None
    assert served_token_radar_snapshot(initial_failure) == {
        "schema_version": "token_radar_snapshot_v1",
        "evidence_as_of_ms": 0,
        "eligible_total": 0,
        "items": [],
    }
    assert first == {"status": "published", "rows_written": 1}
    assert unchanged == {"status": "unchanged", "rows_written": 0}
    assert after_unchanged == published
    assert failed_writes == 1
    assert failed["latest_attempt_status"] == "failed"
    assert served_token_radar_snapshot(failed) == served_token_radar_snapshot(published)
    assert recovered == {"status": "recovered", "rows_written": 1}
    assert recovered_row["latest_attempt_status"] == "ready"
    assert recovered_row["latest_error_code"] is None
    assert served_token_radar_snapshot(recovered_row) == served_token_radar_snapshot(published)
    assert recovered_row["ruleset_version"] == reduced.ruleset_version
    assert recovered_row["ruleset_fingerprint"] == reduced.ruleset_fingerprint


def _stored(conn) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT ruleset_version, ruleset_fingerprint,
               input_fingerprint, state_fingerprint,
               evidence_as_of_ms, evaluation_at_ms,
               input_rows, input_bytes,
               latest_attempt_status, latest_error_code,
               failure_count, served_payload, updated_at_ms
          FROM token_radar_current
         WHERE singleton_key = true
        """
    ).fetchone()
    conn.commit()
    assert row is not None
    return dict(row)


def _eligible_rows() -> list[dict[str, object]]:
    return [
        {
            "target_type": "Asset",
            "target_id": "asset-1",
            "symbol": "PEPE",
            "chain": "solana",
            "exchange": None,
            "address": "mint-1",
            "resolution_status": "EXACT",
            "event_id": f"event-{index}",
            "received_at_ms": NOW_MS - minutes_ago * MINUTE_MS,
            "author_handle": f"author-{index}",
            "text": f"independent text {index}",
            "signal_price_usd": None,
            "latest_price_usd": None,
            "latest_price_observed_at_ms": None,
        }
        for index, minutes_ago in enumerate((30, 20, 10))
    ]
