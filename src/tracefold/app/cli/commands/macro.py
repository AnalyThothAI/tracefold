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
        timeout_seconds=float(source_config.request_timeout_seconds),
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
            targets = repos.macro.target_states()
            modules = repos.macro.all_modules_current()
            document_analysis_jobs = repos.macro.document_analysis_job_state()
    except Exception as exc:
        return 1, _error("macro_status_unavailable", exc)
    status_counts: dict[str, int] = {}
    for target in targets:
        status = str(target["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
    return (
        0,
        {
            "ok": True,
            "data": _json_ready(
                {
                    "dataset_target_count": len(targets),
                    "target_status_counts": status_counts,
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
