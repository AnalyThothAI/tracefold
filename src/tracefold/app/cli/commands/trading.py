"""`tracefold trading`: Case -> Intent -> Outcome reads plus safety controls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from tracefold.app.repository_session import repositories
from tracefold.app.trading_config import trading_settings_gate
from tracefold.app.workers.wiring.news_to_trading import to_oi_candidate_row
from tracefold.news.oi_signals import METRIC_VERSION as NEWS_OI_METRIC_VERSION
from tracefold.platform.config.loader import load_settings
from tracefold.trading.candidate.blacklist import Blacklist
from tracefold.trading.candidate.gate import CANDIDATE_GATE_VERSION
from tracefold.trading.contracts import canonical_base_symbol
from tracefold.trading.research.oi_replay import replay_oi_facts
from tracefold.trading.strategy.oi_smart_money_momentum import OiSmartMoneyMomentumStrategy

_CONTROL = {"running": "RUNNING", "close-only": "CLOSE_ONLY", "paused": "PAUSED"}
_STATUS_WINDOW_MS = 24 * 3_600_000
_READ_COMMANDS = frozenset({"status", "cases", "show", "replay-oi"})
# One replay read, not the scanner's. The projection's own 256-row ceiling is sized for a 65-minute
# scan window; a seven-day replay is about four hundred rows at the measured rate, and a caller that
# comes back with exactly this many was truncated and says so in `truncated`.
_REPLAY_ROW_LIMIT = 20_000


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


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
            return 0, {
                "ok": True,
                "data": {
                    "enabled": settings.trading.enabled,
                    "control": runtime.get("control", "RUNNING"),
                    "dspy_calls_today": int(runtime.get("dspy_calls_today") or 0),
                    "execution_authority": "nautilus",
                    "execution_environment": "BINANCE_USDM_DEMO",
                    "instrument_id": "SOLUSDT-PERP.BINANCE",
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

        if command == "replay-oi":
            days = int(getattr(args, "days", 7) or 7)
            since_ms = now - days * 86_400_000
            candidates = trading_settings_gate(settings)
            strategy = OiSmartMoneyMomentumStrategy()
            facts = [
                to_oi_candidate_row(row)
                for row in repos.news.trade_candidate_oi_rows(
                    metric_version=NEWS_OI_METRIC_VERSION,
                    after_created_at_ms=since_ms,
                    until_created_at_ms=now,
                    limit=_REPLAY_ROW_LIMIT,
                )
            ]
            try:
                blacklist = Blacklist.from_rows(trading.blacklist_rows())
            except Exception:  # pragma: no cover - a read-only report must not fail on the deny list
                blacklist = Blacklist.unavailable()
            report = replay_oi_facts(
                facts,
                gate=candidates,
                strategy=strategy,
                blacklist=blacklist,
                now_ms=now,
            )
            # One catalogue read per *routable* issuer, not per survivor. A coverage number is only
            # meaningful for a symbol that was actually looked up, and reporting `0` for the rest would
            # read as "no native perp listed" for issuers nobody asked about. The set is bounded by the
            # distinct issuers in the window, and each read is a single indexed lookup.
            for symbol in sorted(report.routable_symbols):
                listed = repos.news.trade_candidate_instrument(base_symbol=symbol, venues=("binance.perp", "hl.perp"))
                report.instrument_coverage[symbol] = len(listed)
            return 0, {
                "ok": True,
                "data": {
                    "window_days": days,
                    "since_ms": since_ms,
                    "until_ms": now,
                    "truncated": len(facts) >= _REPLAY_ROW_LIMIT,
                    "gate_version": CANDIDATE_GATE_VERSION,
                    "gate_config": candidates.snapshot,
                    "gate_config_digest": candidates.digest,
                    "strategy_id": strategy.strategy_id,
                    "strategy_config": strategy.config_snapshot,
                    "strategy_config_digest": strategy.config_digest,
                    # Stated, not implied: this report describes what the rules did, and proposes no
                    # replacement for any of them (#265 §8).
                    "thresholds_are_not_tuned_from_this_report": True,
                    **report.as_dict(),
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

    return 2, {"ok": False, "error": f"unknown trading command: {command}"}


__all__ = ["handle_trading"]
