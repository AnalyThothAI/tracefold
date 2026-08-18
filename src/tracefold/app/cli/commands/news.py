from __future__ import annotations

import asyncio
import json
import time
from argparse import Namespace
from collections.abc import Mapping
from typing import Any

from tracefold.platform.config.settings import load_settings

LABEL_VERSION = "news_label_v1"


def handle_news(args: Namespace) -> tuple[int, dict[str, Any]]:
    if args.news_command == "bus-check":
        return _handle_bus_check()
    if args.news_command == "control":
        return _handle_control(args)
    if args.news_command == "label":
        return _handle_label(args)
    if args.news_command == "eval":
        return _handle_eval(args)
    if args.news_command == "replay-decisions":
        return _handle_replay_decisions(args)
    if args.news_command == "replay":
        return _handle_replay(args)
    if args.news_command == "dlq":
        return _handle_dlq(args)
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
    """Consumers read news_control_state on every message; the CLI writes it directly (no broker hop)."""

    from tracefold.app.repositories import repositories
    from tracefold.news.control import apply_control, parse_control

    settings = load_settings(require_ws_token=False)
    payload = {"action": args.action, "key": args.key or None, "ttl_ms": int(args.ttl_minutes) * 60_000}
    try:
        command = parse_control(payload)
    except ValueError as exc:
        return 1, {"ok": False, "error": str(exc)}
    stamp = int(time.time() * 1000)
    with repositories(settings) as repos, repos.transaction():
        state = repos.news.read_control(now_ms=stamp)
        new_state = apply_control(state, command, now_ms=stamp)
        repos.news.write_control(paused=new_state["paused"], mutes=new_state["mutes"], now_ms=stamp)
    return 0, {"ok": True, "data": {"command": payload, "control": new_state}}


def _handle_label(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import repositories

    settings = load_settings(require_ws_token=False)
    stamp = int(time.time() * 1000)
    label = {"label": args.label, "note": str(args.note or "")[:200]}
    with repositories(settings) as repos, repos.transaction():
        if repos.news.event_card(args.event_id) is None:
            return 1, {"ok": False, "error": "news_event_not_found"}
        inserted = repos.news.insert_label(
            event_id=args.event_id, label_version=LABEL_VERSION, source="human", label=label, now_ms=stamp
        )
    return 0, {"ok": True, "data": {"event_id": args.event_id, "inserted": bool(inserted), "label": label}}


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


def _handle_replay_decisions(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import repositories
    from tracefold.news.eval.offline import replay_decisions
    from tracefold.news.triage_rules import DecidePolicy

    settings = load_settings(require_ws_token=False)
    now_ms = int(time.time() * 1000)
    policy = DecidePolicy(
        escalate_magnitude=int(args.escalate_magnitude),
        min_push_magnitude=int(args.min_push_magnitude),
        min_watchlist_magnitude=int(args.min_watchlist_magnitude),
    )
    with repositories(settings) as repos:
        report = replay_decisions(
            repos,
            now_ms=now_ms,
            hours=int(args.hours),
            watchlist_symbols=settings.news.watchlist_symbols,
            policy=policy,
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


def _handle_dlq(args: Namespace) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)

    async def _run() -> dict[str, Any]:
        bus = _bus(settings)
        try:
            await bus.connect()
            if args.dlq_action == "inspect":
                return {"messages": await bus.dead_letters(limit=int(args.limit))}
            if args.dlq_action == "replay":
                return {"replayed": await bus.replay_dead_letters(limit=int(args.limit))}
            return {"purged": await bus.purge_dead_letters()}
        finally:
            await bus.close()

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        return 1, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
    return 0, {"ok": True, "data": result}


__all__ = ["handle_news"]
