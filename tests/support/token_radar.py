from __future__ import annotations

from typing import Any

from tracefold.market.radar.reducer import enrich_token_radar, reduce_token_radar
from tracefold.market.radar.snapshot_repository import TokenRadarCurrentRepository


def run_token_radar_current(conn: Any, *, now_ms: int) -> dict[str, Any]:
    """Run the fixed-period reducer/publisher synchronously in integration tests."""

    repository = TokenRadarCurrentRepository(conn)
    rows = repository.load_material_inputs(now_ms=now_ms)
    reduced = reduce_token_radar(rows, now_ms=now_ms)
    reduced = enrich_token_radar(
        reduced,
        repository.load_presentation_facts(
            [
                (str(item["target"]["target_type"]), str(item["target"]["target_id"]))
                for item in reduced.snapshot["items"]
            ],
            now_ms=now_ms,
        ),
        now_ms=now_ms,
    )
    conn.commit()
    with conn.transaction():
        return repository.publish(reduced, evaluation_at_ms=now_ms)


__all__ = ["run_token_radar_current"]
