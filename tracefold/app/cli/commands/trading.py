"""``tracefold trading`` reads one Trading ledger and records bounded operator intent."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import time
from datetime import UTC, datetime
from http.client import HTTPConnection
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tracefold.app.analysis_status import analysis_status_projection
from tracefold.app.execution_status import execution_readiness_projection
from tracefold.app.operator_control import persist_operator_intent
from tracefold.app.repository_session import repositories
from tracefold.app.trading_replay import replay
from tracefold.platform.config.loader import load_settings
from tracefold.trading.operator_control import (
    OperatorCommandError,
    parse_operator_command,
    prepare_parsed_operator_intent,
)

_WINDOW_MS = 24 * 3_600_000
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$")


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _window_clock(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("trading_window_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000)


def handle_trading(args: Any) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    command = str(getattr(args, "trading_command", "") or "")
    now_ms = _now_ms()
    if command == "issue":
        return _issue_operator_intent(args, settings=settings)
    if command == "diagnose":
        return _diagnose(args, settings=settings)
    if command == "replay":
        try:
            result = asyncio.run(
                replay(
                    settings,
                    program_file=Path(args.program),
                    since_ms=_window_clock(args.since),
                    until_ms=_window_clock(args.until),
                )
            )
        except (ValueError, OSError) as exc:
            return 2, {"ok": False, "error": str(exc)}
        return 0, {"ok": True, "data": result}
    with repositories(settings) as repos:
        trading = repos.trading
        if command == "status":
            last_case_at_ms = trading.latest_case_created_at_ms()
            execution = settings.trading.execution
            analysis_runtime = trading.analysis_runtime(
                execution.account_slot,
            )
            execution_status = execution_readiness_projection(
                execution,
                trading.state(execution.account_slot),
                trading.control(execution.account_slot),
                now_ns=now_ms * 1_000_000,
            )
            # The same dict `GET /api/trading/status` publishes. One projection, so an operator who
            # reads the CLI and an operator who reads the desk cannot be told two different things
            # about the same instant (#537 PR-4, PR-5).
            return 0, {
                "ok": True,
                "data": {
                    "decision": analysis_status_projection(
                        settings,
                        analysis_runtime,
                        now_ms=now_ms,
                        last_case_at_ms=last_case_at_ms,
                    ),
                    "execution": execution_status,
                },
            }
        if command == "cases":
            state = getattr(args, "state", None)
            return 0, {
                "ok": True,
                "data": trading.analysis_cases(
                    since_ms=now_ms - _WINDOW_MS,
                    state=state,
                    limit=int(getattr(args, "limit", 20) or 20),
                ),
            }
        if command == "scoreboard":
            try:
                since_ms, until_ms = _window_clock(args.since), _window_clock(args.until)
                result = trading.scoreboard(since_ms=since_ms, until_ms=until_ms, program_sha=args.program)
            except ValueError as exc:
                return 2, {"ok": False, "error": str(exc)}
            return 0, {"ok": True, "data": result}
        if command == "signals":
            return 0, {
                "ok": True,
                "data": trading.signal_ledger(
                    since_ns=(now_ms - _WINDOW_MS) * 1_000_000,
                    limit=int(getattr(args, "limit", 20) or 20),
                ),
            }
        if command == "fills":
            return 0, {
                "ok": True,
                "data": trading.fill_ledger(
                    since_ns=(now_ms - _WINDOW_MS) * 1_000_000,
                    limit=int(getattr(args, "limit", 20) or 20),
                ),
            }
        if command == "commands":
            return 0, {
                "ok": True,
                "data": trading.console_operator_intents(
                    since_ns=(now_ms - _WINDOW_MS) * 1_000_000,
                    action=getattr(args, "action", None),
                    limit=int(getattr(args, "limit", 20) or 20),
                ),
            }
    return 2, {"ok": False, "error": f"unknown trading command: {command}"}


def _diagnose(args: Any, *, settings: Any) -> tuple[int, dict[str, Any]]:
    """Sequential read-only samples; each source keeps its own clock and failure."""
    from tracefold.platform.postgres.migrations import database_migration_version, latest_migration_version

    started_at_ns = time.time_ns()
    execution = settings.trading.execution
    result: dict[str, Any] = {
        "scope": {"account_slot": execution.account_slot},
        "caller_identity": {
            "image_digest": os.environ.get("TRACEFOLD_IMAGE_DIGEST") or None,
            "runtime_revision": os.environ.get("TRACEFOLD_RUNTIME_REVISION") or None,
            "image_migration_head": latest_migration_version(),
        },
        "started_at_ns": started_at_ns,
    }
    db_started = time.time_ns()
    try:
        with repositories(settings, application_name="tracefold_trading_diagnose") as repos:
            with repos.transaction():
                repos.conn.execute("SET TRANSACTION READ ONLY")
                repos.conn.execute("SET LOCAL statement_timeout = '3s'")
                db_head = database_migration_version(repos.conn)
                state = repos.trading.state(execution.account_slot)
                control = repos.trading.control(execution.account_slot)
                plans = repos.trading.active_plans(execution.account_slot)
            read_at_ns = time.time_ns()
            result["database"] = {
                "started_at_ns": db_started,
                "completed_at_ns": read_at_ns,
                "migration_head": db_head,
                "heartbeat_at_ns": None if state is None else state["heartbeat_at_ns"],
                "projection": execution_readiness_projection(execution, state, control, now_ns=read_at_ns),
                "open_plans": list(plans[:1000]),
                "open_plans_truncated": len(plans) > 1000,
            }
    except Exception as exc:
        result["database"] = {
            "started_at_ns": db_started,
            "completed_at_ns": time.time_ns(),
            "error": type(exc).__name__,
        }
    status_url = getattr(args, "status_url", None)
    if status_url:
        result["http_status"] = _diagnostic_http(str(status_url), token=settings.ws_token)
    result["completed_at_ns"] = time.time_ns()
    db = result["database"]
    result["summary"] = {
        "database_ok": "error" not in db,
        "http_ok": result.get("http_status", {}).get("status_code") == 200 if status_url else None,
        "evidence_is_sequential": True,
    }
    return 0, {"ok": True, "data": result}


def _diagnostic_http(url: str, *, token: str | None) -> dict[str, Any]:
    """A bounded GET; never put a token in the URL or output."""
    started_at_ns = time.time_ns()
    parts = urlsplit(url)
    allowed_hosts = {"localhost", "127.0.0.1", "serve"}
    if parts.scheme != "http" or parts.hostname not in allowed_hosts:
        return {"started_at_ns": started_at_ns, "error": "diagnostic_url_not_local"}
    if parts.username or parts.password or parts.query or parts.fragment:
        return {"started_at_ns": started_at_ns, "error": "diagnostic_url_invalid"}
    expected_path = "/api/trading/status"
    if parts.path != expected_path:
        return {"started_at_ns": started_at_ns, "error": "diagnostic_url_path_invalid"}
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    connection: HTTPConnection | None = None
    try:
        connection = HTTPConnection(parts.hostname, parts.port, timeout=3)
        connection.request("GET", parts.path, headers=headers)
        response = connection.getresponse()
        body = response.read(512_001)
        if len(body) > 512_000:
            raise ValueError("diagnostic_response_too_large")
        payload = json.loads(body)
        return {
            "started_at_ns": started_at_ns,
            "completed_at_ns": time.time_ns(),
            "status_code": response.status,
            "body": payload,
        }
    except Exception as exc:
        return {
            "started_at_ns": started_at_ns,
            "completed_at_ns": time.time_ns(),
            "error": type(exc).__name__,
        }
    finally:
        if connection is not None:
            connection.close()


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
            now_ns=now_ns,
        )
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
