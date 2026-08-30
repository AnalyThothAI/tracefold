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
        # No AMQP connection and no topology declaration. A RabbitMQ policy is matched by queue name
        # pattern and applies whenever a queue appears, so the retry contract can — and during the #400
        # cutover must — be put in place while the queues on the broker still have their old shape and
        # would refuse to be redeclared.
        bus = _bus(settings)
        applied = await bus.apply_policies() if args.policy_action == "apply" else None
        # The policy document, not the per-queue effect: this command runs before any consumer has
        # declared the topology, and on a fresh broker there is nothing for a policy to be effective on
        # yet. `news bus-check` is where the effective per-queue policy is read back.
        verified = await bus.verify_policy_documents()
        return {"applied": applied, "verified": verified}

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        return 1, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:400]}
    return 0, {"ok": True, "data": result}


def _handle_dlq(args: Namespace) -> tuple[int, dict[str, Any]]:
    """Inspect, replay or purge `news.dead`.

    `inspect` reads and requeues, `purge` is the one explicitly destructive command, and `replay` is the
    only one that writes back into the pipeline — so it is the only one that has to prove the broker is
    running the checked-in contract before it touches a message.
    """

    settings = load_settings(require_ws_token=False)

    async def _run() -> dict[str, Any]:
        bus = _bus(settings)
        try:
            await bus.connect()
            if args.dlq_action == "inspect":
                return {"messages": await bus.dead_letters(limit=int(args.limit))}
            if args.dlq_action == "purge":
                return {"purged": await bus.purge_dead_letters()}
            # Replaying into a broker whose retry contract or topology is not the checked-in one
            # republishes evidence into a lane that will mishandle it: no delay, the quorum default
            # delivery limit, at-most-once dead lettering — and the next failure is terminal. Both
            # questions already have an answer here, the same one Workers and `bus-check` read. A
            # mismatch, an unknown (the management API raises rather than guessing) or any unexpected
            # name refuses before the first `basic.get`.
            await bus.verify_policies()
            drift = await bus.topology_drift()
            if drift["queues"] or drift["exchanges"]:
                return {"replayed": 0, "drift": drift}
            return {"replayed": await bus.replay_dead_letters(limit=int(args.limit))}
        finally:
            await bus.close()

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        return 1, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
    refused = bool(result.get("drift"))
    return (1 if refused else 0), {"ok": not refused, "data": result}
