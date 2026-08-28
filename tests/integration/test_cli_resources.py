from __future__ import annotations

import asyncio
import io
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tests.contract.test_cli import write_runtime_config
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.cli import main

pytestmark = pytest.mark.integration


@pytest.mark.usefixtures("postgres_dsn")
def test_db_audit_query_audit_and_validate_projections_use_postgres_only() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir)
        write_runtime_config(home, postgres_dsn=_test_postgres_dsn())
        stdout = io.StringIO()
        with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            exit_codes = [
                main(["db", "audit"], stdout=stdout),
                main(["db", "query-audit"], stdout=stdout),
                main(["ops", "validate-projections", "--sample", "5"], stdout=stdout),
            ]

    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert exit_codes == [0, 0, 0], lines
    assert lines[0]["data"]["engine"] == "postgresql"
    assert lines[0]["data"]["news_schema"]["exact"] is True
    assert lines[1]["data"]["analyze"] is False
    assert lines[2]["data"]["mismatch_count"] == 0


@pytest.mark.usefixtures("postgres_dsn")
def test_trading_status_reports_capability_without_claiming_runtime_readiness() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir)
        write_runtime_config(home, postgres_dsn=_test_postgres_dsn())
        stdout = io.StringIO()
        with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            exit_code = main(["trading", "status"], stdout=stdout)

    response = json.loads(stdout.getvalue())
    assert exit_code == 0
    data = response["data"]
    assert data["execution_authority"] == "nautilus"
    assert data["execution_environment"] == "BINANCE_USDM_DEMO"
    assert data["instrument_id"] == "SOLUSDT-PERP.BINANCE"
    assert data["target_notional_usd"] == "10"
    # #211: the stage report is keyed by stage and by nothing else — no symbol, event or order id can
    # enter it — and every stage says how much evidence it rests on.
    assert set(data["stage_latency_ms"]) == {
        "source_observed_to_verdict_persisted",
        "verdict_persisted_to_case_created",
        "case_created_to_case_decided",
        "case_created_to_intent_emitted",
        "intent_emitted_to_entry_fenced",
        "entry_fenced_to_position_opened",
        "position_opened_to_closed_flat",
    }
    assert all(isinstance(stage["n"], int) for stage in data["stage_latency_ms"].values())


def _delete_test_topology(url: str, name_prefix: str) -> None:
    from tracefold.integrations.rabbitmq import RabbitMQBus

    async def run() -> None:
        bus = RabbitMQBus(url=url, name_prefix=name_prefix, connect_timeout_seconds=5.0)
        try:
            await bus.connect()
            await bus.delete_topology()
        finally:
            await bus.close()

    asyncio.run(run())


def test_news_bus_check_reports_topology_or_fails_closed_without_broker(rabbitmq_url: str) -> None:
    amqp_url = rabbitmq_url
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir)
        config_path = write_runtime_config(home)
        stdout = io.StringIO()
        with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            missing_code = main(["news", "bus-check"], stdout=stdout)
        missing_payload = json.loads(stdout.getvalue())
        assert missing_code == 1
        assert missing_payload["detail"].startswith("news_broker_url_missing")

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        name_prefix = f"tf_test_{uuid.uuid4().hex[:8]}"
        payload["news"] = {**(payload.get("news") or {}), "broker": {"url": amqp_url, "name_prefix": name_prefix}}
        config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        stdout = io.StringIO()
        try:
            with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                exit_code = main(["news", "bus-check"], stdout=stdout)
        finally:
            _delete_test_topology(amqp_url, name_prefix)

    response = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert response["ok"] is True
    assert "queues" in response["data"]
