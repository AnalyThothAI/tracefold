from __future__ import annotations

import asyncio
import json
import time
from argparse import Namespace
from collections.abc import Mapping
from typing import Any

from tracefold.platform.config.settings import load_settings


def handle_news(args: Namespace) -> tuple[int, dict[str, Any]]:
    if args.news_command == "bus-check":
        return _handle_bus_check()
    if args.news_command == "control":
        return _handle_control(args)
    if args.news_command == "eval":
        return _handle_eval(args)
    if args.news_command == "replay":
        return _handle_replay(args)
    return 2, {"ok": False, "error": f"unknown news command: {args.news_command}"}


def _bus(settings: Any) -> Any:
    from tracefold.integrations.rabbitmq import RabbitMQBus

    url = settings.news.broker.url
    if not url:
        raise ValueError("news_broker_url_missing")
    return RabbitMQBus(
        url=url,
        name_prefix=settings.news.broker.name_prefix,
        connect_timeout_seconds=settings.news.broker.connect_timeout_seconds,
    )


def _handle_bus_check() -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)

    async def _run() -> dict[str, Any]:
        bus = _bus(settings)
        try:
            await bus.connect()
            declared = await bus.declare_topology()
            depths = await bus.queue_depths()
        finally:
            await bus.close()
        return {"declared": declared, "queues": depths}

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        return 1, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
    return 0, {"ok": True, "data": result}


def _handle_control(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.news.bus import BusMessage, new_trace_id

    settings = load_settings(require_ws_token=False)
    payload = {"action": args.action, "key": args.key or None, "ttl_ms": int(args.ttl_minutes) * 60_000}

    async def _run() -> None:
        bus = _bus(settings)
        try:
            await bus.connect()
            stamp = int(time.time() * 1000)
            await bus.publish_control(
                BusMessage(
                    kind="control",
                    message_id=f"control:{stamp}",
                    routing_key="",
                    payload=payload,
                    trace_id=new_trace_id(),
                    occurred_at_ms=stamp,
                )
            )
        finally:
            await bus.close()

    try:
        asyncio.run(_run())
    except Exception as exc:
        return 1, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
    return 0, {"ok": True, "data": payload}


def _handle_eval(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import repositories
    from tracefold.news.eval.offline import evaluate_recent

    settings = load_settings(require_ws_token=False)
    now_ms = int(time.time() * 1000)
    with repositories(settings) as repos:
        report = evaluate_recent(
            repos, now_ms=now_ms, hours=int(args.hours), policy_version=str(args.policy_version or "") or None
        )
    return 0, {"ok": True, "data": report}


def _handle_replay(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.news.eval.replay import replay_hits

    settings = load_settings(require_ws_token=False)
    with open(args.path, encoding="utf-8") as fh:
        raw = json.load(fh)
    hits: list[Mapping[str, Any]] = []
    if isinstance(raw, Mapping):
        for value in raw.values():
            hits.extend(h for h in value if isinstance(h, Mapping))
    elif isinstance(raw, list):
        hits.extend(h for h in raw if isinstance(h, Mapping))
    report = replay_hits(
        hits,
        strategy_ids=settings.news.opennews_strategy_ids or ("1018", "1352", "1353"),
        watchlist_symbols=settings.news.watchlist_symbols,
    )
    return 0, {"ok": True, "data": report}


__all__ = ["handle_news"]
