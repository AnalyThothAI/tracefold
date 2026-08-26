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
from tracefold.app.workers.wiring.news_to_trading import to_oi_candidate_row
from tracefold.news.oi_signals import METRIC_VERSION as NEWS_OI_METRIC_VERSION
from tracefold.platform.config.loader import load_settings
from tracefold.platform.config.secret_file import secret_file_configured
from tracefold.trading.candidate.blacklist import Blacklist
from tracefold.trading.candidate.eligibility import EligibilityPolicy
from tracefold.trading.candidate.gate import CANDIDATE_GATE_VERSION, GateConfig
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


def trading_settings_gate(settings: Any) -> GateConfig:
    """The Candidate Gate's configuration as the running lane would build it.

    Assembled from the same settings the Workers wiring reads, so a replay cannot describe a floor the
    scanner is not applying — the digest in the report is the digest the ledger's rows are filed under.
    """

    candidates = settings.trading.candidates
    return GateConfig.from_policy(
        EligibilityPolicy(
            max_age_ms=candidates.max_age_seconds * 1000,
            max_rank_in_window=candidates.max_rank_in_window,
            min_oi_value_usd=candidates.min_oi_value_usd,
            symbol_cooldown_ms=candidates.symbol_cooldown_seconds * 1000,
        ),
        venue_priority=settings.trading.venues.enabled,
    )


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
