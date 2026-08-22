"""Latest-only liquidation shadow read model against real PostgreSQL (#144)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.news.liquidation import (
    LIQUIDATION_MODEL_VERSION,
    LIQUIDATION_RANGE,
    LIQUIDATION_TARGETS,
    LiquidationZone,
    ProviderLiquidationSnapshot,
)

pytestmark = pytest.mark.integration

NOW = 1_800_000_000_000


@pytest.fixture(scope="module")
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _clean(conn):
    conn.execute("DELETE FROM news_liquidation_snapshots")
    conn.commit()


def _snapshot(*, freshness: str, received_at_ms: int, error_class: str | None = None) -> ProviderLiquidationSnapshot:
    return ProviderLiquidationSnapshot(
        target=LIQUIDATION_TARGETS[0],
        provider="coinglass_web",
        contract="undocumented_public_web_http",
        authenticated=False,
        completeness="unknown",
        model_version=LIQUIDATION_MODEL_VERSION,
        range_key=LIQUIDATION_RANGE,
        zones=(
            LiquidationZone(
                price=Decimal("65000"),
                size=Decimal("123"),
                raw_side=1,
                model_level=3,
                model_level2="h1",
                begin_at_ms=NOW - 1_000,
                x=1,
            ),
        ),
        source_at_ms=NOW - 500,
        received_at_ms=received_at_ms,
        freshness=freshness,
        degraded=freshness != "fresh",
        error_class=error_class,
        payload_sha256="a" * 64,
        raw_level_count=1,
        raw_price_count=1,
    )


def test_snapshot_identity_is_latest_only_and_failure_keeps_the_last_good_zones(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.liquidation.store_snapshot(_snapshot(freshness="fresh", received_at_ms=NOW))
        repos.liquidation.store_snapshot(
            _snapshot(freshness="unavailable", received_at_ms=NOW + 60_000, error_class="upstream_timeout")
        )

    rows = conn.execute("SELECT * FROM news_liquidation_snapshots").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["zones"][0]["price"] == "65000"
    assert row["last_success_at_ms"] == NOW
    assert row["last_attempt_at_ms"] == NOW + 60_000
    assert row["freshness"] == "unavailable" and row["error_class"] == "upstream_timeout"
    status = repos.liquidation.status(now_ms=NOW + 60_000)
    assert status["shadow"] is True and status["fresh"] == 0 and status["degraded"] == 1
    assert status["snapshots"][0]["zone_count"] == 1


def test_due_planner_is_stable_bounded_and_oldest_first(conn) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.liquidation.store_snapshot(_snapshot(freshness="fresh", received_at_ms=NOW))

    due = repos.liquidation.due_targets(
        LIQUIDATION_TARGETS,
        provider="coinglass_web",
        model_version=LIQUIDATION_MODEL_VERSION,
        range_key=LIQUIDATION_RANGE,
        due_before_ms=NOW + 1,
        limit=2,
    )

    assert due == list(LIQUIDATION_TARGETS[1:3])
