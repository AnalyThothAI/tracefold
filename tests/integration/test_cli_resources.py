from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tests.contract.test_cli import write_runtime_config
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.cli import main

pytestmark = pytest.mark.integration


@pytest.mark.usefixtures("postgres_dsn")
def test_db_audit_query_audit_and_validate_projections_use_postgres_only() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir)
        write_runtime_config(home, postgres_dsn=_test_postgres_dsn())
        conn = connect_postgres_test(read_only=False)
        try:
            migrate(conn)
        finally:
            conn.close()
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


def _amqp_reachable(url: str) -> bool:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 5672), timeout=1.0):
            return True
    except OSError:
        return False


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


def test_news_bus_check_reports_topology_or_fails_closed_without_broker() -> None:
    amqp_url = os.environ.get("TRACEFOLD_TEST_AMQP_URL", "amqp://tracefold:tracefold@127.0.0.1:5672/")
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir)
        config_path = write_runtime_config(home)
        stdout = io.StringIO()
        with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            missing_code = main(["news", "bus-check"], stdout=stdout)
        missing_payload = json.loads(stdout.getvalue())
        assert missing_code == 1
        assert missing_payload["detail"].startswith("news_broker_url_missing")

        if not _amqp_reachable(amqp_url):
            pytest.fail("integration RabbitMQ is not reachable")
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
