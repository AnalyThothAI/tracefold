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
    BinanceDemoReceipt,
    DecisionRuntimeV1,
    DemoReceiptError,
    DemoReceiptObservation,
    OperatorCommandError,
    canonical_sha256,
    parse_operator_command,
    prepare_parsed_operator_intent,
    verify_binance_demo_receipt,
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
            decision = trading.decision_runtime() or DecisionRuntimeV1(
                state="FAULTED",
                heartbeat_at_ms=None,
                reason="decision_runtime_missing",
                updated_at_ms=now_ms,
            )
            config = signal_lane_config(settings)
            execution = settings.trading.execution
            execution_status = execution_readiness_projection(
                execution,
                trading.execution_runtime_state(execution.account_slot),
                trading.execution_runtime_control_state(execution.profile_id),
                now_ns=now_ms * 1_000_000,
            )
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
                    runtime_profile_id=None,
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
                    runtime_profile_id=None,
                    action=getattr(args, "action", None),
                    before=None,
                    limit=int(getattr(args, "limit", 20) or 20),
                ),
            }
        if command == "demo-receipt":
            entry_command_id = str(getattr(args, "entry_command_id", "") or "")
            flatten_command_id = str(getattr(args, "flatten_command_id", "") or "")
            if not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (entry_command_id, flatten_command_id)):
                return 2, {"ok": False, "error": "binance_demo_command_identity_invalid"}
            execution = settings.trading.execution
            try:
                observation_rows = trading.console_execution_observations(
                    since_ns=(now_ms - _WINDOW_MS) * 1_000_000,
                    runtime_profile_id=execution.profile_id,
                    normalized_kind=None,
                    before=None,
                    limit=1_000,
                )
                receipt = verify_binance_demo_receipt(
                    state=trading.execution_runtime_state(execution.account_slot),
                    observations=tuple(_demo_observation(row) for row in observation_rows),
                    entry_command_id=entry_command_id,
                    flatten_command_id=flatten_command_id,
                    now_ns=now_ms * 1_000_000,
                )
            except DemoReceiptError as exc:
                return 1, {"ok": False, "error": str(exc)}
            return 0, {"ok": True, "data": _demo_receipt_payload(receipt)}
    return 2, {"ok": False, "error": f"unknown trading command: {command}"}


def _demo_observation(row: dict[str, Any]) -> DemoReceiptObservation:
    summary = row["summary"]
    return DemoReceiptObservation(
        event_id=str(row["event_id"]),
        runtime_profile_id=str(row["runtime_profile_id"]),
        command_id=None if row["command_id"] is None else str(row["command_id"]),
        normalized_kind=str(row["normalized_kind"]),
        observed_at_ns=int(row["observed_at_ns"]),
        native_identity_references=tuple(str(value) for value in row["native_identity_references"]),
        action=_summary_text(summary, "action"),
        disposition=_summary_text(summary, "disposition"),
        reason=_summary_text(summary, "reason"),
        leg=_summary_text(summary, "leg"),
        status=_summary_text(summary, "status"),
        reduce_only=_summary_bool(summary, "reduce_only"),
        explicit_quantity=_summary_text(summary, "explicit_quantity"),
        source=_summary_text(summary, "source"),
        lifecycle=_summary_text(summary, "lifecycle"),
        runtime_id=_summary_text(summary, "runtime_id"),
        runtime_revision=_summary_text(summary, "runtime_revision"),
        image_digest=_summary_text(summary, "image_digest"),
        config_sha256=_summary_text(summary, "config_sha256"),
        credential_fingerprint=_summary_text(summary, "credential_fingerprint"),
    )


def _summary_text(summary: object, key: str) -> str | None:
    if not isinstance(summary, dict):
        raise DemoReceiptError("binance_demo_observation_contract_invalid")
    value = summary.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DemoReceiptError("binance_demo_observation_contract_invalid")
    return value


def _summary_bool(summary: object, key: str) -> bool | None:
    if not isinstance(summary, dict):
        raise DemoReceiptError("binance_demo_observation_contract_invalid")
    value = summary.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise DemoReceiptError("binance_demo_observation_contract_invalid")
    return value


def _demo_receipt_payload(receipt: BinanceDemoReceipt) -> dict[str, Any]:
    return {
        "mode": receipt.mode,
        "runtime_profile_id": receipt.runtime_profile_id,
        "runtime_release": receipt.runtime_release,
        "runtime_id": receipt.runtime_id,
        "runtime_revision": receipt.runtime_revision,
        "image_digest": receipt.image_digest,
        "config_sha256": receipt.config_sha256,
        "credential_fingerprint": receipt.credential_fingerprint,
        "entry_command_id": receipt.entry_command_id,
        "flatten_command_id": receipt.flatten_command_id,
        "runtime_start_event_ids": list(receipt.runtime_start_event_ids),
        "evidence_event_ids": list(receipt.evidence_event_ids),
        "venue_native_references": list(receipt.venue_native_references),
        "authoritative_flat_observed_at_ns": receipt.authoritative_flat_observed_at_ns,
        "truth": receipt.truth,
    }


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
            target_profile_id=settings.trading.execution.profile_id,
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
