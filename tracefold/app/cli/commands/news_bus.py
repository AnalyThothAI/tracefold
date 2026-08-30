from __future__ import annotations

import asyncio
from argparse import Namespace
from typing import Any

from tracefold.platform.config.loader import load_settings


def _bus(settings: Any) -> Any:
    from tracefold.integrations.rabbitmq import RabbitMQBus

    url = settings.news.broker.url
    if not url:
        raise ValueError("news_broker_url_missing")
    return RabbitMQBus(
        url=url,
        name_prefix=settings.news.broker.name_prefix,
        connect_timeout_seconds=settings.news.broker.connect_timeout_seconds,
        management_url=settings.news.broker.management_url,
    )


def _handle_bus_check() -> tuple[int, dict[str, Any]]:
    """Declare the topology and report what the broker actually holds, including retry-policy drift."""

    settings = load_settings(require_ws_token=False)

    async def _run() -> dict[str, Any]:
        bus = _bus(settings)
        try:
            await bus.connect()
            declared = await bus.declare_topology()
            queues = await bus.broker_snapshot()
            policies = await bus.effective_policies()
            drift = await bus.topology_drift()
        finally:
            await bus.close()
        return {"declared": declared, "queues": queues, "policies": policies, "drift": drift}

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        return 1, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
    unexpected = result["drift"]["queues"] + result["drift"]["exchanges"]
    policy_ok = all(bool(row.get("policy_ok")) for row in result["queues"].values())
    ok = policy_ok and not unexpected
    return (0 if ok else 1), {"ok": ok, "data": result}


def _handle_bus_policy(args: Namespace) -> tuple[int, dict[str, Any]]:
    """Apply or verify the checked-in RabbitMQ policy document.

    Applying is an explicit deploy/operator step. Nothing in the runtime repairs policy drift: Workers
    verifies and refuses, and this command is the only writer.
    """

    settings = load_settings(require_ws_token=False)

    async def _run() -> dict[str, Any]:
        bus = _bus(settings)
        try:
            await bus.connect()
            applied = await bus.apply_policies() if args.policy_action == "apply" else None
            verified = await bus.verify_policies()
            effective = await bus.effective_policies()
        finally:
            await bus.close()
        return {"applied": applied, "verified": verified, "effective": effective}

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        return 1, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:400]}
    return 0, {"ok": True, "data": result}


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
