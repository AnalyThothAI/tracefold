from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tracefold.app.cli.commands import db


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        news=SimpleNamespace(
            broker=SimpleNamespace(
                url="amqp://test",
                name_prefix="news",
                connect_timeout_seconds=1.0,
                management_url="http://test",
            )
        )
    )


def _queues(*, consumers: int = 0) -> dict[str, dict[str, object]]:
    return {
        "news.raw": {
            "messages": 0,
            "consumers": consumers,
            "ready": 0,
            "unacked": 0,
            "delayed": 0,
            "dead_letter_pending": 0,
            "policy_ok": True,
            "missing": False,
        }
    }


def _install_bus(monkeypatch: pytest.MonkeyPatch, *, consumers: int = 0) -> None:
    class Bus:
        def __init__(self, **_kwargs) -> None:
            pass

        async def connect(self) -> None:
            pass

        async def verify_policies(self) -> None:
            pass

        async def broker_snapshot(self):
            return _queues(consumers=consumers)

        async def topology_drift(self):
            return {"queues": [], "exchanges": []}

        async def close(self) -> None:
            pass

    monkeypatch.setattr("tracefold.integrations.rabbitmq.RabbitMQBus", Bus)


def test_news_genesis_preflight_binds_live_broker_and_exact_runtime_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_bus(monkeypatch)
    runtime_manifest_sha = "3" * 64
    preflight = {
        "queue_ready": 0,
        "queue_unacked": 0,
        "queue_dead_letter": 0,
        "queue_stale_reference_count": 0,
    }
    monkeypatch.setenv("TRACEFOLD_NEWS_GENESIS_PREFLIGHT_JSON", json.dumps(preflight))
    monkeypatch.setattr(db, "configured_runtime_manifest_sha", lambda _settings: runtime_manifest_sha)

    db._prepare_news_genesis_evidence(_settings())

    observation = asyncio.run(db._observe_drained_news_broker(_settings()))
    assert observation["totals"] == {
        "ready": 0,
        "unacked": 0,
        "dead_letter": 0,
        "stale_reference_count": 0,
    }
    assert db.os.environ[db._GENESIS_BROKER_OBSERVATION_ENV] == db._canonical_sha(observation)
    assert db.os.environ[db._GENESIS_RUNTIME_MANIFEST_ENV] == runtime_manifest_sha


def test_news_genesis_preflight_refuses_attached_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_bus(monkeypatch, consumers=1)

    with pytest.raises(RuntimeError, match=r"news\.raw\.consumers=0"):
        asyncio.run(db._observe_drained_news_broker(_settings()))


def test_fresh_install_is_explicitly_bound_before_any_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_bus(monkeypatch)
    monkeypatch.delenv("TRACEFOLD_NEWS_GENESIS_PREFLIGHT_JSON", raising=False)
    monkeypatch.setattr(db, "configured_runtime_manifest_sha", lambda _settings: "3" * 64)

    db._prepare_news_genesis_evidence(_settings(), fresh_install=True)

    assert db.os.environ[db._GENESIS_FRESH_INSTALL_ENV] == "1"
    assert db.os.environ[db._GENESIS_RUNTIME_MANIFEST_ENV] == "3" * 64
