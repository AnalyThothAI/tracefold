"""`tracefold trading`: Case -> Intent -> Outcome reads plus safety controls."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from tracefold.app.repository_session import repositories
from tracefold.platform.config.loader import load_settings
from tracefold.trading import (
    CapitalRuntimeV1,
    DailyRiskPolicyV1,
    DecisionRuntimeV1,
    OperatorArmReceiptV1,
    ProductionPromotionGrantRevocationV1,
    ProductionPromotionGrantV1,
    VenueBindingRuntimeV1,
)
from tracefold.trading.contracts import canonical_base_symbol

_CONTROL = {"close-only": "CLOSE_ONLY", "paused": "PAUSED"}
_STATUS_WINDOW_MS = 24 * 3_600_000
_READ_COMMANDS = frozenset({"status", "cases", "show"})


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def handle_trading(args: Any) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)
    command = str(getattr(args, "trading_command", "") or "")
    now = _now_ms()
    if command == "replay-oi":
        from .trading_replay import handle_oi_replay

        return handle_oi_replay(settings, args, now_ms=now)
    if command == "evidence":
        from .trading_evidence import handle_trading_evidence

        return handle_trading_evidence(settings, args, now_ms=now)
    listing_only = command == "blacklist" and str(getattr(args, "blacklist_action", "list") or "list") == "list"
    role: Literal["serve", "workers"] = "serve" if (command in _READ_COMMANDS or listing_only) else "workers"

    with repositories(settings, role=role) as repos:
        trading = repos.trading

        if command == "status":
            decision = trading.decision_runtime() or DecisionRuntimeV1(
                state="FAULTED",
                heartbeat_at_ms=None,
                reason="decision_runtime_missing",
                updated_at_ms=now,
            )
            capital = trading.capital_runtime() or CapitalRuntimeV1(
                control="PAUSED", blacklist_revision=0, arm_epoch=1, updated_at_ms=now
            )
            return 0, {
                "ok": True,
                "data": {
                    "decision": {
                        "state": decision.state,
                        "heartbeat_at_ms": decision.heartbeat_at_ms,
                        "reason": decision.reason,
                    },
                    "capital": {
                        "control": capital.control,
                        "blacklist_revision": capital.blacklist_revision,
                        "arm_epoch": capital.arm_epoch,
                    },
                    "bindings": [_binding_runtime(binding) for binding in trading.binding_runtime_rows(now_ms=now)],
                    "target_notional_usd": str(settings.trading.order.fixed_notional_usd),
                    # #211: where the 24 h of work actually spent its time, stage by stage. `n` per
                    # stage says how much evidence each number rests on.
                    "stage_latency_ms": trading.stage_latency_ms(since_ms=now - _STATUS_WINDOW_MS),
                    # The durable admission ledger and the durable Case/Intent aggregates. Every
                    # number here survives a restart and a UTC midnight, which is what the retired
                    # in-memory funnel could not do (#331).
                    **trading.candidate_admission_report(now_ms=now),
                    **trading.runtime_summary(since_ms=now - _STATUS_WINDOW_MS, now_ms=now),
                    "cases_by_state_24h": trading.case_counts(since_ms=now - _STATUS_WINDOW_MS),
                    "case_reasons_24h": trading.case_reason_counts(since_ms=now - _STATUS_WINDOW_MS),
                    "capital_reasons_24h": trading.case_capital_reason_counts(since_ms=now - _STATUS_WINDOW_MS),
                    "intents_24h": trading.intent_counts(since_ms=now - _STATUS_WINDOW_MS),
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
                        "policy_decision": row["policy_decision"],
                        "policy_reason": row["policy_reason"],
                        "capital_disposition": row["capital_disposition"],
                        "capital_reason": row["capital_reason"],
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

        if command == "authority":
            action = str(getattr(args, "authority_command", "") or "")
            if action == "activate":
                arms = [str(value) for value in (getattr(args, "arm", None) or [])]
                with repos.transaction():
                    activated = trading.activate_operator_arms(arms, now_ms=now)
                return (0 if activated else 1), {
                    "ok": bool(activated),
                    "data": {"activated": bool(activated), "arm_receipt_sha256s": sorted(arms)},
                }
            document = _authority_document(str(getattr(args, "file", "") or ""))
            with repos.transaction():
                if action == "risk-policy-install":
                    policy = DailyRiskPolicyV1.model_validate(document)
                    inserted = trading.append_daily_risk_policy(policy, created_at_ms=now)
                    digest = policy.risk_policy_sha256
                elif action == "grant-install":
                    grant = ProductionPromotionGrantV1.model_validate(document)
                    inserted = trading.append_production_promotion_grant(grant, created_at_ms=now)
                    digest = grant.grant_sha256
                elif action == "grant-revoke":
                    revocation = ProductionPromotionGrantRevocationV1.model_validate(document)
                    inserted = trading.revoke_production_promotion_grant(revocation, created_at_ms=now)
                    digest = revocation.revocation_sha256
                elif action == "arm-install":
                    arm = OperatorArmReceiptV1.model_validate(document)
                    inserted = trading.append_operator_arm_receipt(arm, created_at_ms=now)
                    digest = arm.arm_receipt_sha256
                else:
                    return 2, {"ok": False, "error": f"unknown authority action: {action}"}
            return 0, {"ok": True, "data": {"action": action, "sha256": digest, "inserted": inserted}}

    return 2, {"ok": False, "error": f"unknown trading command: {command}"}


def _binding_runtime(binding: VenueBindingRuntimeV1) -> dict[str, Any]:
    return {
        "binding": binding.binding,
        "credential_state": binding.credential_state,
        "credential_fingerprint": binding.credential_fingerprint,
        "runtime_state": binding.runtime_state,
        "account_state": binding.account_state,
        "account_generation": binding.account_generation,
        "catalog_state": binding.catalog_state,
        "catalog_snapshot_sha256": binding.catalog_snapshot_sha256,
        "catalog_captured_at_ms": binding.catalog_captured_at_ms,
        "capability_state": binding.capability_state,
        "capability_snapshot_sha256": binding.capability_snapshot_sha256,
        "capability_compiled_at_ms": binding.capability_compiled_at_ms,
        "capability_compile_error": binding.capability_compile_error,
        "execution_binding_sha256": binding.execution_binding_sha256,
        "active_arm_receipt_sha256": binding.active_arm_receipt_sha256,
        "heartbeat_at_ms": binding.heartbeat_at_ms,
        "reason": binding.reason,
        "updated_at_ms": binding.updated_at_ms,
    }


def _authority_document(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser()
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("trading_authority_document_invalid")
    return document


__all__ = ["handle_trading"]
