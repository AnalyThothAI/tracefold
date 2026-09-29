"""The read-only diagnostic reads the current single-connection schema."""

from __future__ import annotations

from contextlib import closing

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_diagnostic_reads_new_executor_state_and_plans() -> None:
    with closing(connect_postgres_test(read_only=True)) as conn:
        trading = repositories_for_connection(conn).trading
        state = trading.state("binance_usdm_primary")
        control = trading.control("binance_usdm_primary")
        plans = trading.active_plans("binance_usdm_primary")

    assert state is None
    assert control["entries_paused"] is True
    assert plans == []
