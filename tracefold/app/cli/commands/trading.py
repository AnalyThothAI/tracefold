"""``tracefold trading`` read projections and one bounded operator-intent ingress."""

from __future__ import annotations

import os
import re
import socket
import time
from datetime import UTC, datetime
from typing import Any

from tracefold.app.execution_status import execution_readiness_projection
from tracefold.app.operator_control import persist_operator_intent
from tracefold.app.repository_session import repositories
from tracefold.app.trading_config import signal_lane_config
from tracefold.platform.config.loader import load_settings
from tracefold.trading import (
    OperatorCommandError,
    parse_operator_command,
    prepare_parsed_operator_intent,
)

_WINDOW_MS = 24 * 3_600_000
_MAX_FUTURE_SKEW_NS = 30 * 1_000_000_000
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$")


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def handle_trading(args: Any) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    command = str(getattr(args, "trading_command", "") or "")
    now_ms = _now_ms()
    if command == "issue":
        return _issue_operator_intent(args, settings=settings)
    with repositories(settings) as repos:
        trading = repos.trading
        if command == "status":
            last_case_at_ms = trading.latest_case_created_at_ms()
            config = signal_lane_config(settings)
            execution = settings.trading.execution
            execution_status = execution_readiness_projection(
                execution,
                trading.execution_runtime_state(execution.account_slot),
                trading.execution_runtime_control_state(execution.account_slot),
                now_ns=now_ms * 1_000_000,
            )
            return 0, {
                "ok": True,
                "data": {
                    "decision": {"last_case_at_ms": last_case_at_ms},
                    "alpha": {
                        "policy_id": config.policy.policy_id,
                        "policy_version": config.policy.policy_version,
                        "config_digest": config.policy.config_digest,
                    },
                    "execution": execution_status,
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
                    account_slot=None,
                    normalized_kind=None,
                    before=None,
                    limit=int(getattr(args, "limit", 20) or 20),
                ),
            }
        if command == "commands":
            return 0, {
                "ok": True,
                "data": trading.console_operator_intents(
                    since_ns=(now_ms - _WINDOW_MS) * 1_000_000,
                    account_slot=None,
                    action=getattr(args, "action", None),
                    before=None,
                    limit=int(getattr(args, "limit", 20) or 20),
                ),
            }
    return 2, {"ok": False, "error": f"unknown trading command: {command}"}


def _issue_operator_intent(args: Any, *, settings: Any) -> tuple[int, dict[str, Any]]:
    request_id = str(getattr(args, "request_id", "") or "")
    if _REQUEST_ID.fullmatch(request_id) is None:
        return 2, {"ok": False, "error": "operator_command_request_id_invalid"}
    try:
        parsed = parse_operator_command(str(getattr(args, "text", "") or ""))
        requested_at_ns = int(getattr(args, "requested_at_ns", 0) or 0)
        now_ns = time.time_ns()
        local_uid = os.getuid()
        prepared = prepare_parsed_operator_intent(
            parsed,
            source=_cli_request_source(local_uid=local_uid, hostname=socket.gethostname()),
            source_command_id=request_id,
            account_slot=settings.trading.execution.account_slot,
            operator_identity=f"local-cli:{local_uid}",
            authentication_identity=f"local-os-uid:{local_uid}",
            requested_at_ns=requested_at_ns,
        )
        if requested_at_ns > now_ns + _MAX_FUTURE_SKEW_NS:
            raise OperatorCommandError("operator_command_clock_invalid")
        if prepared.value.expires_at_ns <= now_ns:
            raise OperatorCommandError("operator_command_expired")
    except OperatorCommandError as exc:
        return 2, {"ok": False, "error": exc.code}
    with repositories(settings, application_name="tracefold_trading_control_cli") as repos, repos.transaction():
        receipt = persist_operator_intent(repos.trading, prepared)
    return 0, {
        "ok": True,
        "data": {
            "command_id": receipt.command_id,
            "seq": receipt.seq,
            "requested_at_ns": requested_at_ns,
            "disposition": receipt.disposition,
            "reason": receipt.reason,
            "truth": "intent_recorded_not_order_or_fill",
        },
    }


def _cli_request_source(*, local_uid: int, hostname: str) -> str:
    """Namespace caller-supplied request IDs by the stable local caller and host."""

    normalized_host = hostname.strip().lower()
    if local_uid < 0 or not normalized_host or "\x00" in normalized_host:
        raise OperatorCommandError("operator_command_caller_identity_invalid")
    return f"cli:uid:{local_uid}:host:{normalized_host}"


__all__ = ["handle_trading"]
