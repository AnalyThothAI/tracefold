from __future__ import annotations

import time
from argparse import Namespace
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from tracefold.app.repositories import repositories
from tracefold.app.runtime_capabilities import macro_document_analysis_runtime
from tracefold.integrations.macro_sources import MacroSourceClient
from tracefold.macro import (
    MacroAcquisitionService,
    professional_backfill_policies,
    require_dataset,
)
from tracefold.platform.config.settings import load_settings


def handle_macro(args: Namespace) -> tuple[int, dict[str, Any]]:
    if args.macro_command == "backfill":
        return _handle_backfill(args)
    if args.macro_command == "backfill-professional":
        return _handle_professional_backfill()
    if args.macro_command == "status":
        return _handle_status()
    return 2, {"ok": False, "error": f"unknown macro command: {args.macro_command}"}


def _handle_backfill(args: Namespace) -> tuple[int, dict[str, Any]]:
    try:
        start_date = _parse_date(str(args.start))
        end_date = _parse_date(str(args.end))
        if start_date > end_date or end_date > date.today():
            raise ValueError("macro_backfill_invalid_range")
        spec = require_dataset(str(args.dataset))
        settings = load_settings(require_ws_token=False)
        now_ms = _now_ms()
        with repositories(settings) as repos, repos.transaction():
            if spec.instrument_id is not None:
                repos.macro_market.ensure_instrument(spec, now_ms=now_ms)
            target = repos.macro.enqueue_backfill_target(
                spec,
                start_date=start_date,
                end_date=end_date,
                now_ms=now_ms,
                max_attempts=5,
            )
        execution = _drain_backfills(
            settings,
            target_keys=(str(target["target_key"]),),
        )
    except Exception as exc:
        return 1, _error("macro_backfill_failed", exc)
    return 0, {
        "ok": True,
        "data": {
            "target": _json_ready(target),
            "execution": execution,
        },
    }


def _handle_professional_backfill() -> tuple[int, dict[str, Any]]:
    try:
        through_date = date.today()
        policies = professional_backfill_policies(through_date=through_date)
        settings = load_settings(require_ws_token=False)
        now_ms = _now_ms()
        targets = []
        target_keys = []
        with repositories(settings) as repos, repos.transaction():
            for policy in policies:
                spec = require_dataset(policy.dataset_id)
                if spec.instrument_id is not None:
                    repos.macro_market.ensure_instrument(spec, now_ms=now_ms)
                target = repos.macro.promote_covering_backfill_target(
                    spec,
                    start_date=policy.start_date,
                    end_date=through_date,
                    history_class=policy.history_class,
                    priority=policy.priority,
                    now_ms=now_ms,
                )
                if target is None:
                    target = repos.macro.enqueue_backfill_target(
                        spec,
                        start_date=policy.start_date,
                        end_date=through_date,
                        now_ms=now_ms,
                        max_attempts=5,
                        history_class=policy.history_class,
                        priority=policy.priority,
                    )
                targets.append(
                    {
                        "dataset_id": policy.dataset_id,
                        "history_class": policy.history_class,
                        "priority": policy.priority,
                        "partition_key": target["partition_key"],
                        "status": target["status"],
                    }
                )
                target_keys.append(str(target["target_key"]))
        execution = _drain_backfills(
            settings,
            target_keys=tuple(target_keys),
        )
    except Exception as exc:
        return 1, _error("macro_professional_backfill_failed", exc)
    return 0, {
        "ok": True,
        "data": {
            "through_date": through_date.isoformat(),
            "target_count": len(targets),
            "targets": targets,
            "execution": execution,
        },
    }


def _drain_backfills(
    settings: Any,
    *,
    target_keys: tuple[str, ...],
) -> dict[str, Any]:
    if not target_keys:
        return {
            "attempts": 0,
            "current": 0,
            "failed": 0,
            "results": [],
        }
    source_config = settings.providers.macro_sources
    client = MacroSourceClient(
        user_agent=str(source_config.user_agent),
        fred_enabled=source_config.fred_enabled,
        cboe_enabled=source_config.cboe_enabled,
        cftc_enabled=source_config.cftc_enabled,
        nasdaq_daily_enabled=source_config.nasdaq_daily_enabled,
        yfinance_enabled=source_config.yfinance_enabled,
    )
    service = MacroAcquisitionService(
        db=_CliWorkerDatabase(settings),
        worker_name="macro_backfill",
        clock_kind="backfill",
        source_client=client,
        enabled_adapter_ids=client.enabled_adapter_ids,
        lease_owner="macro_backfill:cli",
        target_keys=target_keys,
    )
    results: list[dict[str, Any]] = []
    try:
        for _ in range(10_000):
            result = service.run_once()
            if result is None:
                break
            results.append(result)
        else:
            raise RuntimeError("macro_backfill_execution_cap_exceeded")
    finally:
        client.close()
    return {
        "attempts": len(results),
        "current": sum(1 for result in results if result["status"] == "current"),
        "failed": sum(1 for result in results if result["status"] in {"failed", "unavailable"}),
        "results": results,
    }


class _CliWorkerDatabase:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def worker_session(self, *_args: Any, **_kwargs: Any) -> Any:
        return repositories(self.settings)


def _handle_status() -> tuple[int, dict[str, Any]]:
    try:
        settings = load_settings(require_ws_token=False)
        with repositories(settings) as repos:
            targets = repos.macro.all_acquisition_target_states()
            modules = repos.macro.all_modules_current()
            document_analysis_jobs = repos.macro.document_analysis_job_state()
    except Exception as exc:
        return 1, _error("macro_status_unavailable", exc)
    return (
        0,
        {
            "ok": True,
            "data": _json_ready(
                {
                    "acquisition": _summarize_acquisition_targets(
                        targets,
                        now_ms=_now_ms(),
                    ),
                    "modules": [
                        {
                            "module_id": row["module_id"],
                            "current_health_state": row["current_health_state"],
                            "history_depth_state": row["history_depth_state"],
                            "fact_cutoff_ms": row["fact_cutoff_ms"],
                            "updated_at_ms": row["updated_at_ms"],
                        }
                        for row in modules
                    ],
                    "document_analysis_runtime": macro_document_analysis_runtime(settings),
                    "document_analysis_jobs": document_analysis_jobs,
                }
            ),
        },
    )


def _summarize_acquisition_targets(
    targets: Sequence[Mapping[str, Any]],
    *,
    now_ms: int,
) -> dict[str, Any]:
    steady = [row for row in targets if str(row["clock_kind"]) != "backfill"]
    maintenance = [row for row in targets if str(row["clock_kind"]) == "backfill"]
    claimable = {"pending", "current", "delayed", "backfilling"}
    expired_claims = [
        row for row in steady if str(row["status"]) == "claimed" and int(row.get("leased_until_ms") or 0) <= int(now_ms)
    ]
    active_claims = [
        row for row in steady if str(row["status"]) == "claimed" and int(row.get("leased_until_ms") or 0) > int(now_ms)
    ]
    due = [row for row in steady if str(row["status"]) in claimable and int(row["next_due_at_ms"]) <= int(now_ms)]
    scheduled = [row for row in steady if str(row["status"]) in claimable and int(row["next_due_at_ms"]) > int(now_ms)]
    maintenance_claims = [row for row in maintenance if str(row["status"]) == "claimed"]
    expired_maintenance = [row for row in maintenance_claims if int(row.get("leased_until_ms") or 0) <= int(now_ms)]
    return {
        "steady": {
            "target_count": len(steady),
            "actionable_due_count": len(due) + len(expired_claims),
            "oldest_actionable_due_at_ms": min(
                [int(row["next_due_at_ms"]) for row in due]
                + [int(row.get("leased_until_ms") or 0) for row in expired_claims],
                default=None,
            ),
            "scheduled_future_count": len(scheduled),
            "in_progress_count": len(active_claims),
            "expired_claim_count": len(expired_claims),
            "oldest_expired_claim_at_ms": min(
                (int(row.get("leased_until_ms") or 0) for row in expired_claims),
                default=None,
            ),
            "status_counts": _count_target_values(steady, "status"),
            "error_code_counts": _count_target_values(
                steady,
                "last_error_code",
                omit_empty=True,
            ),
        },
        "maintenance": {
            "target_count": len(maintenance),
            "in_progress_count": len(maintenance_claims) - len(expired_maintenance),
            "expired_claim_count": len(expired_maintenance),
            "oldest_expired_claim_at_ms": min(
                (int(row.get("leased_until_ms") or 0) for row in expired_maintenance),
                default=None,
            ),
            "status_counts": _count_target_values(maintenance, "status"),
            "error_code_counts": _count_target_values(
                maintenance,
                "last_error_code",
                omit_empty=True,
            ),
        },
    }


def _count_target_values(
    targets: Sequence[Mapping[str, Any]],
    key: str,
    *,
    omit_empty: bool = False,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for target in targets:
        raw = target.get(key)
        if omit_empty and raw in {None, ""}:
            continue
        value = str(raw)
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def _error(error: str, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "detail": str(exc)[:200] or type(exc).__name__,
    }


def _json_ready(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_ready(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["handle_macro"]
