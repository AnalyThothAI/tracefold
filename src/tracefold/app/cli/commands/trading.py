"""`tracefold trading`: the operator surface for the capital lane (#104).

Read-mostly. The three writes it does have are deliberately narrow: the deny-list, the control state,
and an approval bound to an exact frozen payload digest. There is no command that places, amends or
cancels an order — a human deciding to trade is a human editing the mandate, not a CLI flag.

Every command runs as the `serve` role, so nothing here can write a News table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tracefold.app.repositories import repositories
from tracefold.platform.config.settings import load_settings
from tracefold.trading import canonical_base_symbol

_CONTROL = {"running": "RUNNING", "close-only": "CLOSE_ONLY", "paused": "PAUSED"}
_STATUS_WINDOW_MS = 24 * 3_600_000


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def handle_trading(args: Any) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    command = str(getattr(args, "trading_command", "") or "")
    now = _now_ms()

    with repositories(settings, role="serve") as repos:
        trading = repos.trading

        if command == "status":
            runtime = trading.runtime_state() or {}
            counts = trading.status_counts(since_ms=now - _STATUS_WINDOW_MS)
            order = settings.trading.order
            return 0, {
                "ok": True,
                "data": {
                    "enabled": settings.trading.enabled,
                    "mode": settings.trading.mode,
                    "control": runtime.get("control", "RUNNING"),
                    "orders_today": int(runtime.get("orders_today") or 0),
                    "dspy_calls_today": int(runtime.get("dspy_calls_today") or 0),
                    "venues": list(settings.trading.venues.enabled),
                    "worst_case_daily_loss_usd": str(order.worst_case_daily_loss_usd),
                    "funnel_24h": runtime.get("funnel") or {},
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
                        "case_kind": row["case_kind"],
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
            order_row = trading.order_for_case(case_id=str(args.case_id))
            observations = trading.observations(order_id=str(order_row["order_id"])) if order_row is not None else []
            return 0, {"ok": True, "data": {"case": case, "order": order_row, "observations": observations}}

        if command == "blacklist":
            action = str(getattr(args, "blacklist_action", "list") or "list")
            if action == "list":
                return 0, {"ok": True, "data": trading.blacklist_rows()}
            symbol = canonical_base_symbol(getattr(args, "symbol", ""))
            if not symbol:
                return 2, {"ok": False, "error": "symbol_required"}
            if action == "add":
                trading.blacklist_upsert(
                    base_symbol=symbol,
                    reason=str(getattr(args, "reason", "") or "operator"),
                    expires_at_ms=None,
                    now_ms=now,
                )
                repos.conn.commit()
                return 0, {"ok": True, "data": {"base_symbol": symbol, "action": "added"}}
            if action == "remove":
                removed = trading.blacklist_delete(base_symbol=symbol)
                repos.conn.commit()
                return (0 if removed else 1), {
                    "ok": bool(removed),
                    "data": {"base_symbol": symbol, "action": "removed", "rows": removed},
                }
            return 2, {"ok": False, "error": f"unknown blacklist action: {action}"}

        if command == "control":
            control = _CONTROL.get(str(getattr(args, "state", "") or "").lower())
            if control is None:
                return 2, {"ok": False, "error": "control_state_invalid"}
            trading.set_control(control=control, now_ms=now)
            repos.conn.commit()
            return 0, {"ok": True, "data": {"control": control}}

        if command in ("approve", "reject"):
            digest = str(getattr(args, "digest", "") or "")
            if not digest:
                return 2, {"ok": False, "error": "digest_required"}
            order_id = str(args.order_id)
            if command == "approve":
                changed = trading.approve_order(order_id=order_id, payload_sha256=digest, now_ms=now)
            else:
                changed = trading.reject_order(
                    order_id=order_id,
                    payload_sha256=digest,
                    reason=str(getattr(args, "reason", "") or "operator_rejected"),
                    now_ms=now,
                )
            repos.conn.commit()
            # Idempotent by state: a second approve of an already-approved order changes nothing and
            # says so, rather than re-authorising a payload the operator only ever signed once.
            return (0 if changed else 1), {
                "ok": bool(changed),
                "data": {"order_id": order_id, "action": command, "changed": bool(changed)},
            }

    return 2, {"ok": False, "error": f"unknown trading command: {command}"}


__all__ = ["handle_trading"]
