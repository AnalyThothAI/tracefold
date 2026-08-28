"""`tracefold trading`: Case -> Intent -> Outcome reads plus safety controls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal

from tracefold.app.repository_session import repositories
from tracefold.platform.config.loader import load_settings
from tracefold.platform.runtime_identity import runtime_identity
from tracefold.trading.capabilities import build_execution_capability_snapshot
from tracefold.trading.contracts import canonical_base_symbol

_CONTROL = {"running": "RUNNING", "close-only": "CLOSE_ONLY", "paused": "PAUSED"}
_STATUS_WINDOW_MS = 24 * 3_600_000
_READ_COMMANDS = frozenset({"status", "cases", "show", "replay-oi"})


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def handle_trading(args: Any) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    command = str(getattr(args, "trading_command", "") or "")
    now = _now_ms()
    if command == "refresh-capabilities":
        return _refresh_capabilities(settings, now_ms=now)
    if command == "replay-oi":
        from .trading_replay import handle_oi_replay

        return handle_oi_replay(settings, args, now_ms=now)
    listing_only = command == "blacklist" and str(getattr(args, "blacklist_action", "list") or "list") == "list"
    role: Literal["serve", "workers"] = "serve" if (command in _READ_COMMANDS or listing_only) else "workers"

    with repositories(settings, role=role) as repos:
        trading = repos.trading

        if command == "status":
            runtime = trading.runtime_state() or {}
            counts = trading.status_counts(
                since_ms=now - _STATUS_WINDOW_MS,
                now_ms=now,
                day_key=runtime.get("day_key"),
            )
            return 0, {
                "ok": True,
                "data": {
                    "enabled": settings.trading.enabled,
                    "control": runtime.get("control", "RUNNING"),
                    "dspy_calls_today": int(runtime.get("dspy_calls_today") or 0),
                    "execution_authority": "nautilus",
                    "execution_environment": "BINANCE_USDM_DEMO",
                    "active_capability_snapshot_sha256": runtime.get("active_capability_snapshot_sha256"),
                    "active_capability_included_count": int(runtime.get("active_capability_included_count") or 0),
                    "blacklist_revision": int(runtime.get("blacklist_revision") or 0),
                    "target_notional_usd": str(settings.trading.order.fixed_notional_usd),
                    "nautilus_heartbeat_at_ms": runtime.get("nautilus_heartbeat_at_ms"),
                    "nautilus_ready": bool(runtime.get("nautilus_ready")),
                    "nautilus_readiness_reason": runtime.get("nautilus_readiness_reason"),
                    "nautilus_unexpected_exposure": bool(runtime.get("nautilus_unexpected_exposure")),
                    "funnel_today": runtime.get("funnel") or {},
                    # #211: where the 24 h of work actually spent its time, stage by stage, read off
                    # the same rows the funnel counts. `n` per stage says how much evidence each
                    # number rests on.
                    "stage_latency_ms": trading.stage_latency_ms(since_ms=now - _STATUS_WINDOW_MS),
                    # #264: the durable admission ledger. `funnel_24h` above is the day's in-memory
                    # document and is overwritten at UTC midnight; this survives it, and is the only
                    # part of this report a lane with zero cases and zero intents can still answer from.
                    **trading.candidate_admission_report(now_ms=now),
                    **counts,
                },
            }

        if command == "cases":
            state = getattr(args, "state", None)
            rows = trading.cases(state=state, limit=int(getattr(args, "limit", 20) or 20))
            return 0, {
                "ok": True,
                "data": [
                    {
                        "case_id": row["case_id"],
                        "underlying_key": row["underlying_key"],
                        "trigger_kind": row["trigger_kind"],
                        "strategy_id": row["strategy_id"],
                        "strategy_version": row["strategy_version"],
                        "state": row["state"],
                        "regime": row["regime"],
                        "policy_decision": row["policy_decision"],
                        "policy_reason": row["policy_reason"],
                        "created_at_ms": row["created_at_ms"],
                    }
                    for row in rows
                ],
            }

        if command == "show":
            case = trading.case(case_id=str(args.case_id))
            if case is None:
                return 1, {"ok": False, "error": "case_not_found"}
            linked = trading.intent_for_case(case_id=str(args.case_id))
            intent = None if linked is None else linked[0].model_dump(mode="json")
            outcome = None if linked is None else linked[1].model_dump(mode="json")
            return 0, {"ok": True, "data": {"case": case, "intent": intent, "outcome": outcome}}

        if command == "blacklist":
            action = str(getattr(args, "blacklist_action", "list") or "list")
            if action == "list":
                return 0, {"ok": True, "data": trading.blacklist_rows()}
            symbol = canonical_base_symbol(getattr(args, "symbol", ""))
            if not symbol:
                return 2, {"ok": False, "error": "symbol_required"}
            if action == "add":
                with repos.transaction():
                    trading.blacklist_upsert(
                        base_symbol=symbol,
                        reason=str(getattr(args, "reason", "") or "operator"),
                        expires_at_ms=None,
                        now_ms=now,
                    )
                return 0, {"ok": True, "data": {"base_symbol": symbol, "action": "added"}}
            if action == "remove":
                with repos.transaction():
                    removed = trading.blacklist_delete(base_symbol=symbol, now_ms=now)
                return (0 if removed else 1), {
                    "ok": bool(removed),
                    "data": {"base_symbol": symbol, "action": "removed", "rows": removed},
                }
            return 2, {"ok": False, "error": f"unknown blacklist action: {action}"}

        if command == "control":
            control = _CONTROL.get(str(getattr(args, "state", "") or "").lower())
            if control is None:
                return 2, {"ok": False, "error": "control_state_invalid"}
            with repos.transaction():
                trading.set_control(control=control, now_ms=now)
            return 0, {"ok": True, "data": {"control": control}}

    return 2, {"ok": False, "error": f"unknown trading command: {command}"}


def _refresh_capabilities(settings: Any, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    from tracefold.app.workers.wiring.news_to_trading import news_execution_instruments
    from tracefold.integrations.nautilus import (
        installed_nautilus_wheel_identity,
        load_binance_usdm_demo_capabilities,
    )

    with repositories(settings, role="workers") as repos, repos.transaction():
        repos.trading.set_control(control="PAUSED", now_ms=now_ms)
        news_rows = news_execution_instruments(repos)
    try:
        provider_rows = asyncio.run(load_binance_usdm_demo_capabilities())
    except Exception:
        return 1, {"ok": False, "error": "execution_capability_provider_load_failed"}
    identity = runtime_identity()
    try:
        snapshot = build_execution_capability_snapshot(
            news_rows=news_rows,
            provider_rows=provider_rows,
            app_revision=identity.runtime_revision,
            app_image_digest=identity.image_digest,
            nautilus_wheel_identity=installed_nautilus_wheel_identity(),
        )
    except (RuntimeError, ValueError):
        return 1, {"ok": False, "error": "execution_capability_snapshot_invalid"}
    try:
        with repositories(settings, role="workers") as repos, repos.transaction():
            activated = repos.trading.append_and_activate_execution_capability_snapshot(
                snapshot,
                created_at_ms=now_ms,
            )
            if not activated:
                raise RuntimeError("execution_capability_activation_blocked")
    except RuntimeError as exc:
        return 1, {"ok": False, "error": str(exc)}
    return 0, {
        "ok": True,
        "data": {
            "snapshot_sha256": snapshot.snapshot_sha256,
            "included_count": len(snapshot.included),
            "excluded_count": len(snapshot.excluded),
        },
    }


__all__ = ["handle_trading"]
