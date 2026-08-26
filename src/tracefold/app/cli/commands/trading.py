"""`tracefold trading`: the operator surface for the capital lane (#104).

Read-mostly. The three writes it does have are deliberately narrow: the deny-list, the control state,
and an approval bound to an exact frozen payload digest. There is no command that places, amends or
cancels an order — a human deciding to trade is a human editing the mandate, not a CLI flag.

Reads run as the read-only `serve` role. The three operator mutations run as `workers`, because
`tracefold_serve` is the HTTP-facing role, it carries `default_transaction_read_only = on`, and the
deny-list is a safety control the internet-facing role must not be able to rewrite or erase. Neither
role can write a News table from here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from tracefold.app.repository_session import repositories
from tracefold.platform.config.loader import load_settings
from tracefold.platform.config.secret_file import secret_file_configured
from tracefold.trading.contracts import canonical_base_symbol

_CONTROL = {"running": "RUNNING", "close-only": "CLOSE_ONLY", "paused": "PAUSED"}
_STATUS_WINDOW_MS = 24 * 3_600_000
_READ_COMMANDS = frozenset({"status", "cases", "show"})


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _execution_capability(settings: Any) -> dict[str, Any]:
    trading = settings.trading
    if not trading.enabled:
        return {
            "execution_backend": "disabled",
            "execution_configured": False,
            "live_mode_supported": False,
            "live_ready": False,
            "live_readiness": "not_applicable",
        }
    if trading.mode == "paper":
        return {
            "execution_backend": "paper",
            "execution_configured": True,
            "live_mode_supported": False,
            "live_ready": False,
            "live_readiness": "not_applicable",
        }
    token_file = settings.trading_opentrade_token_file()
    token_configured = secret_file_configured(token_file)
    return {
        "execution_backend": "opentrade_reviewed",
        "execution_configured": bool(trading.opentrade.base_url and token_configured),
        # This read-only CLI cannot infer a separate Workers process's startup/canary result.
        "live_mode_supported": True,
        "live_ready": False,
        "live_readiness": "not_proven",
    }


def handle_trading(args: Any) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    command = str(getattr(args, "trading_command", "") or "")
    now = _now_ms()
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
                    "live_symbol": settings.trading.live_symbol,
                    "nominal_daily_stop_loss_usd": str(order.nominal_daily_stop_loss_usd),
                    **_execution_capability(settings),
                    "funnel_24h": runtime.get("funnel") or {},
                    # #211: where the 24 h of work actually spent its time, stage by stage, read off
                    # the same rows the funnel counts. `n` per stage says how much evidence each
                    # number rests on.
                    "stage_latency_ms": trading.stage_latency_ms(since_ms=now - _STATUS_WINDOW_MS),
                    # #264: the durable admission ledger. `funnel_24h` above is the day's in-memory
                    # document and is overwritten at UTC midnight; this survives it, and is the only
                    # part of this report a lane with zero cases and zero orders can still answer from.
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

        if command == "resolve":
            # Five reconcile paths escalate to MANUAL_REVIEW_REQUIRED and the state sits inside the
            # active-underlying index, so without a drain two unresolved orders halt the lane with no
            # remedy. The operator states what they confirmed at the venue; nothing here calls a
            # provider, and `--open` hands the order back to the reconciler with its exit re-armed.
            changed = trading.resolve_manual_review(
                order_id=str(args.order_id),
                outcome=str(args.outcome),
                reason=str(getattr(args, "reason", "") or "operator_checked_venue"),
                remote_order_id=str(getattr(args, "remote_order_id", "") or "").strip() or None,
                now_ms=now,
            )
            repos.conn.commit()
            return (0 if changed else 1), {
                "ok": bool(changed),
                "data": {"order_id": str(args.order_id), "outcome": str(args.outcome), "changed": bool(changed)},
            }

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
