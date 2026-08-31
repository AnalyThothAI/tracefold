"""Read-only ``tracefold trading`` projections for the Signal boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tracefold.app.repository_session import repositories
from tracefold.app.trading_config import signal_lane_config
from tracefold.platform.config.loader import load_settings
from tracefold.trading import DecisionRuntimeV1, canonical_sha256

_WINDOW_MS = 24 * 3_600_000


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def handle_trading(args: Any) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    command = str(getattr(args, "trading_command", "") or "")
    now_ms = _now_ms()
    with repositories(settings) as repos:
        trading = repos.trading
        if command == "status":
            decision = trading.decision_runtime() or DecisionRuntimeV1(
                state="FAULTED",
                heartbeat_at_ms=None,
                reason="decision_runtime_missing",
                updated_at_ms=now_ms,
            )
            config = signal_lane_config(settings)
            execution = settings.trading.execution
            return 0, {
                "ok": True,
                "data": {
                    "decision": {
                        "state": decision.state,
                        "heartbeat_at_ms": decision.heartbeat_at_ms,
                        "reason": decision.reason,
                    },
                    "alpha": {
                        "policy_id": config.policy.policy_id,
                        "policy_version": config.policy.policy_version,
                        "config_digest": config.policy.config_digest,
                        "contract_sha256": canonical_sha256(
                            {
                                "policy_id": config.policy.policy_id,
                                "policy_version": config.policy.policy_version,
                                "policy_config": config.policy.config_snapshot,
                            }
                        ),
                    },
                    "execution": {
                        "mode": execution.mode,
                        "profile_id": execution.profile_id,
                        "account_slot": execution.account_slot,
                        "ready": False,
                        "reason": (
                            "disabled" if execution.mode == "disabled" else "activation_not_available_before_433e"
                        ),
                    },
                    "counts": trading.runtime_summary(since_ms=now_ms - _WINDOW_MS, now_ms=now_ms),
                },
            }
        if command == "cases":
            return 0, {
                "ok": True,
                "data": trading.cases(
                    state=getattr(args, "state", None),
                    limit=int(getattr(args, "limit", 20) or 20),
                ),
            }
        if command == "signals":
            return 0, {
                "ok": True,
                "data": trading.console_signals(
                    since_ns=(now_ms - _WINDOW_MS) * 1_000_000,
                    market_key=None,
                    before=None,
                    limit=int(getattr(args, "limit", 20) or 20),
                ),
            }
        if command == "observations":
            return 0, {
                "ok": True,
                "data": trading.console_execution_observations(
                    since_ns=(now_ms - _WINDOW_MS) * 1_000_000,
                    runtime_profile_id=None,
                    normalized_kind=None,
                    before=None,
                    limit=int(getattr(args, "limit", 20) or 20),
                ),
            }
    return 2, {"ok": False, "error": f"unknown trading command: {command}"}


__all__ = ["handle_trading"]
