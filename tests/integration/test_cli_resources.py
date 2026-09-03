from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote

import pytest
import yaml

from tests.contract.test_cli import write_runtime_config
from tests.postgres_test_utils import postgres_migration_test_dsn
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.cli import main

pytestmark = pytest.mark.integration


def test_db_audit_query_audit_and_validate_projections_use_postgres_only(postgres_clone_dsn: str) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir)
        write_runtime_config(home, postgres_dsn=postgres_migration_test_dsn(postgres_clone_dsn))
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


@pytest.mark.usefixtures("postgres_clone_dsn")
def test_trading_status_reports_orthogonal_durable_runtime_facts() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir)
        write_runtime_config(home, postgres_dsn=_test_postgres_dsn())
        stdout = io.StringIO()
        with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            exit_code = main(["trading", "status"], stdout=stdout)

    response = json.loads(stdout.getvalue())
    assert exit_code == 0
    data = response["data"]
    assert set(data["decision"]) == {"last_case_at_ms"}
    assert data["alpha"]["policy_id"] == "source_native_oi_smart_money_long_v4"
    assert len(data["alpha"]["contract_sha256"]) == 64
    assert data["execution"] == {
        "mode": "disabled",
        "account_slot": "binance_usdm_primary",
        "alive": False,
        "execution_safe": False,
        "entries_armed": False,
        "entry_block_reason": "disabled",
        "runtime_release": None,
        "config_sha256": None,
        "runtime_revision": None,
        "image_digest": None,
        "credential_fingerprint": None,
        "lifecycle_state": None,
        "heartbeat_at_ns": None,
        "reconciliation_observed_at_ns": None,
        "reconciliation_age_ms": None,
        "singleton_ready": False,
        "startup_reconciled": False,
        "portfolio_ready": False,
        "control_plane_ready": False,
        "audit_ready": False,
        "day_start_ready": False,
        "entries_paused": True,
        "emergency_halted": False,
        "unexpected_exposure": False,
        "account_flat": False,
        "account_flat_proven": False,
        "positions_count": 0,
        "open_orders_count": 0,
        "protection_status": "unknown",
        "current_account": None,
    }
    assert set(data["counts"]) == {
        "cases_24h",
        "signals_24h",
        "no_trade_24h",
        "blocked_24h",
        "cases_open",
        "signals_unexpired",
    }


def test_trading_issue_records_idempotent_intent_without_interpreting_activation(
    postgres_clone_dsn: str,
) -> None:
    requested_at_ns = time.time_ns()
    command = [
        "trading",
        "issue",
        "/pause planned-maintenance",
        "--request-id",
        "cli-integration-1",
        "--requested-at-ns",
        str(requested_at_ns),
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir)
        write_runtime_config(home, postgres_dsn=postgres_migration_test_dsn(postgres_clone_dsn))
        stdout = io.StringIO()
        with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
            exit_codes = [main(command, stdout=stdout), main(command, stdout=stdout)]

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert exit_codes == [0, 0]
    assert responses[0] == responses[1]
    assert responses[0]["data"] == {
        "command_id": responses[0]["data"]["command_id"],
        "seq": 1,
        "requested_at_ns": requested_at_ns,
        "disposition": "awaiting_runtime",
        "reason": None,
        "truth": "intent_recorded_not_order_or_fill",
    }


def _management_url(url: str) -> str:
    from urllib.parse import urlsplit

    return os.environ.get(
        "TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL",
        f"http://{urlsplit(url).hostname or '127.0.0.1'}:15672",
    ).rstrip("/")


def _test_bus(url: str, name_prefix: str) -> Any:
    """The bus these CLI tests reach the broker with, addressed exactly like the command under test."""

    from tracefold.integrations.rabbitmq import RabbitMQBus

    return RabbitMQBus(
        url=url,
        name_prefix=name_prefix,
        connect_timeout_seconds=5.0,
        management_url=_management_url(url),
    )


def _delete_test_topology(url: str, name_prefix: str) -> None:
    async def run() -> None:
        bus = _test_bus(url, name_prefix)
        try:
            await bus.connect()
            await bus.delete_topology()
        finally:
            await bus.close()

    asyncio.run(run())


def _settle_effective_policy(url: str, name_prefix: str) -> None:
    """The production attach sequence: declare the topology, then wait out the statistics interval."""

    from tracefold.integrations.rabbitmq import POLICY_EFFECTIVE_TIMEOUT_SECONDS

    async def run() -> None:
        bus = _test_bus(url, name_prefix)
        try:
            await bus.connect()
            await bus.verify_policies(settle_timeout_seconds=POLICY_EFFECTIVE_TIMEOUT_SECONDS)
        finally:
            await bus.close()

    asyncio.run(run())


def _wait_until_the_broken_policy_is_readable(url: str, name_prefix: str) -> None:
    """Wait for a deleted policy to show up as a mismatch in the per-queue view a one-shot check reads.

    The broker stops applying a deleted policy at once, but publishes the queue's new effective policy on
    its own statistics interval — so for a moment the contract is already gone and still reads intact.
    Production has the same window; the command is fail-closed on what is observable, not clairvoyant.
    """

    from tracefold.integrations.rabbitmq import BrokerPolicyMismatch

    async def run() -> None:
        bus = _test_bus(url, name_prefix)
        try:
            await bus.connect()
            deadline = asyncio.get_running_loop().time() + 30.0
            while asyncio.get_running_loop().time() < deadline:
                try:
                    await bus.verify_policies()
                except BrokerPolicyMismatch:
                    return
                await asyncio.sleep(0.5)
            raise AssertionError("the deleted policy never became readable as a mismatch")
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
        payload["news"] = {
            **(payload.get("news") or {}),
            "broker": {
                "url": amqp_url,
                "name_prefix": name_prefix,
                "management_url": _management_url(amqp_url),
            },
        }
        config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        try:
            with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                # #400: a topology whose retry policy has never been applied is not healthy, so
                # bus-check fails closed until the operator applies the checked-in document.
                drifted = io.StringIO()
                drift_code = main(["news", "bus-check"], stdout=drifted)
                drift_payload = json.loads(drifted.getvalue())
                assert drift_code == 1 and drift_payload["ok"] is False
                assert not all(row["policy_ok"] for row in drift_payload["data"]["queues"].values())

                applied = io.StringIO()
                assert main(["news", "bus-policy", "apply"], stdout=applied) == 0
                assert json.loads(applied.getvalue())["ok"] is True

                # The standalone verify action proves the documents without touching the topology.
                verified = io.StringIO()
                assert main(["news", "bus-policy", "verify"], stdout=verified) == 0
                verify_payload = json.loads(verified.getvalue())
                assert verify_payload["ok"] is True
                assert verify_payload["data"]["applied"] is None
                assert verify_payload["data"]["verified"]["verified"]

                # bus-check reads the effective per-queue policy, which the broker publishes on its own
                # statistics interval after the document is in place. Wait with the same bounded settle
                # Workers runs before consuming; bus-check itself stays a one-shot snapshot.
                _settle_effective_policy(amqp_url, name_prefix)

                stdout = io.StringIO()
                exit_code = main(["news", "bus-check"], stdout=stdout)
        finally:
            _delete_test_topology(amqp_url, name_prefix)

    response = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert response["ok"] is True
    assert "queues" in response["data"]
    assert response["data"]["drift"] == {"queues": [], "exchanges": []}
    assert all(row["policy_ok"] for row in response["data"]["queues"].values())


def _dlq_depth(url: str, name_prefix: str) -> int:
    from tracefold.news.bus import Q_DEAD

    async def run() -> int:
        bus = _test_bus(url, name_prefix)
        try:
            await bus.connect()
            return int((await bus.queue_depths())[bus.queue_name(Q_DEAD)]["messages"])
        finally:
            await bus.close()

    return asyncio.run(run())


def _publish_a_dead_letter(url: str, name_prefix: str) -> None:
    """One replayable dead letter, published through the DLX the way a terminal settlement arrives."""

    import aio_pika

    from tracefold.integrations.rabbitmq import topology
    from tracefold.news.bus import BusMessage, new_trace_id

    async def run() -> None:
        bus = _test_bus(url, name_prefix)
        message = BusMessage(
            kind="event",
            message_id="dlq:preflight",
            routing_key="event.general.normal",
            payload={"probe": 1},
            trace_id=new_trace_id(),
            occurred_at_ms=1,
        )
        try:
            await bus.connect()
            connection = await aio_pika.connect_robust(url, timeout=5)
            channel = await connection.channel(publisher_confirms=True)
            try:
                dlx = await channel.get_exchange(topology(name_prefix).dlx)
                await dlx.publish(
                    aio_pika.Message(message.body(), delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                    routing_key=message.routing_key,
                )
            finally:
                await channel.close()
                await connection.close()
        finally:
            await bus.close()

    asyncio.run(run())


def _declare_unexpected_queue(url: str, name: str) -> None:
    import aio_pika

    async def run() -> None:
        connection = await aio_pika.connect_robust(url, timeout=5)
        channel = await connection.channel()
        try:
            await channel.declare_queue(name, durable=True, arguments={"x-queue-type": "quorum"})
        finally:
            await channel.close()
            await connection.close()

    asyncio.run(run())


def _delete_queue(url: str, name: str) -> None:
    import aio_pika

    async def run() -> None:
        connection = await aio_pika.connect_robust(url, timeout=5)
        channel = await connection.channel()
        try:
            await channel.queue_delete(name, if_unused=False, if_empty=False)
        finally:
            await channel.close()
            await connection.close()

    asyncio.run(run())


def test_dlq_replay_refuses_before_reading_a_message_when_the_broker_contract_is_not_the_checked_in_one(
    rabbitmq_url: str,
) -> None:
    """Replay is the one DLQ action that writes back into the pipeline, so it proves the contract first.

    Republishing into a lane whose policy is gone means no delay, the quorum default delivery limit and
    at-most-once dead lettering — the next failure destroys the message this command exists to save. An
    unexpected queue is the same class of unknown: nobody has said what it holds or what it would do.
    """

    amqp_url = rabbitmq_url
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir)
        config_path = write_runtime_config(home)
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        name_prefix = f"tf_test_{uuid.uuid4().hex[:8]}"
        payload["news"] = {
            **(payload.get("news") or {}),
            "broker": {
                "url": amqp_url,
                "name_prefix": name_prefix,
                "management_url": _management_url(amqp_url),
            },
        }

        def _write_config(management_url: str) -> None:
            payload["news"] = {
                **payload["news"],
                "broker": {**payload["news"]["broker"], **{"management_url": management_url}},
            }
            config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

        _write_config(_management_url(amqp_url))
        try:
            with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                assert main(["news", "bus-policy", "apply"], stdout=io.StringIO()) == 0
                assert main(["news", "bus-check"], stdout=io.StringIO()) in (0, 1)  # declares the topology
                _settle_effective_policy(amqp_url, name_prefix)
                _publish_a_dead_letter(amqp_url, name_prefix)
                assert _dlq_depth(amqp_url, name_prefix) == 1

                # A replayable dead letter and an intact contract: the command does its job.
                replayed = io.StringIO()
                assert main(["news", "dlq", "replay"], stdout=replayed) == 0
                assert json.loads(replayed.getvalue())["data"]["replayed"] == 1
                assert _dlq_depth(amqp_url, name_prefix) == 0

                _publish_a_dead_letter(amqp_url, name_prefix)
                assert _dlq_depth(amqp_url, name_prefix) == 1

                # Unknown is not permission: with the management API unreachable, nothing can say the
                # retry contract still holds, so the replay refuses rather than assuming it does.
                _write_config("http://127.0.0.1:1")
                unknown = io.StringIO()
                unknown_code = main(["news", "dlq", "replay"], stdout=unknown)
                unknown_payload = json.loads(unknown.getvalue())
                assert unknown_code == 1 and unknown_payload["ok"] is False
                assert unknown_payload["error"] == "BrokerUnavailable"
                assert _dlq_depth(amqp_url, name_prefix) == 1
                _write_config(_management_url(amqp_url))

                # An unexpected queue under this prefix is state nobody has accounted for.
                _declare_unexpected_queue(amqp_url, f"{name_prefix}.news.deep")
                try:
                    drifted = io.StringIO()
                    drift_code = main(["news", "dlq", "replay"], stdout=drifted)
                    drift_payload = json.loads(drifted.getvalue())
                    assert drift_code == 1 and drift_payload["ok"] is False
                    assert drift_payload["data"]["drift"]["queues"] == [f"{name_prefix}.news.deep"]
                    assert drift_payload["data"]["replayed"] == 0
                    assert _dlq_depth(amqp_url, name_prefix) == 1
                finally:
                    _delete_queue(amqp_url, f"{name_prefix}.news.deep")

                _delete_one_policy(amqp_url, name_prefix)
                _wait_until_the_broken_policy_is_readable(amqp_url, name_prefix)
                mismatched = io.StringIO()
                mismatch_code = main(["news", "dlq", "replay"], stdout=mismatched)
                mismatch_payload = json.loads(mismatched.getvalue())
                assert mismatch_code == 1 and mismatch_payload["ok"] is False
                assert mismatch_payload["error"] == "BrokerPolicyMismatch"
                assert _dlq_depth(amqp_url, name_prefix) == 1

                # Inspecting is still allowed while the contract is broken: it reads and requeues.
                inspected = io.StringIO()
                assert main(["news", "dlq", "inspect"], stdout=inspected) == 0
                assert len(json.loads(inspected.getvalue())["data"]["messages"]) == 1
        finally:
            _delete_test_topology(amqp_url, name_prefix)


def _delete_one_policy(url: str, name_prefix: str) -> None:
    from urllib.parse import quote, urlsplit

    from tracefold.news import broker_policy

    parsed = urlsplit(url)
    name = broker_policy.policies(name_prefix=name_prefix)[1].name
    request = urllib.request.Request(  # noqa: S310 - test-only HTTP management endpoint
        f"{_management_url(url)}/api/policies/%2F/{quote(name, safe='')}",
        method="DELETE",
        headers={
            "Authorization": "Basic "
            + base64.b64encode(
                f"{unquote(parsed.username or 'guest')}:{unquote(parsed.password or 'guest')}".encode()
            ).decode("ascii")
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5):
        return
