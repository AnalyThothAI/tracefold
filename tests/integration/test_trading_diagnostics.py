"""The read-only diagnostic reads the current single-connection schema."""

from __future__ import annotations

from contextlib import closing

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_diagnostic_evidence_reads_current_trade_plan_columns() -> None:
    with closing(connect_postgres_test(read_only=True)) as conn:
        plans, risks = repositories_for_connection(conn).trading.execution_diagnostic_evidence("binance_usdm_primary")

    assert plans == ()
    assert risks == ()
