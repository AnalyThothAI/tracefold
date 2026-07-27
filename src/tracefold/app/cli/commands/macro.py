from __future__ import annotations

import time
from argparse import Namespace
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from tracefold.app.repositories import repositories
from tracefold.macro import professional_backfill_policies, require_dataset
from tracefold.platform.config.settings import load_settings


def handle_macro(args: Namespace) -> tuple[int, dict[str, Any]]:
    if args.macro_command == "backfill":
        return _handle_backfill(args)
    if args.macro_command == "backfill-professional":
        return _handle_professional_backfill()
    if args.macro_command == "retry-research":
        return _handle_retry_research(args)
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
                max_attempts=int(settings.workers.macro_backfill.max_attempts),
            )
    except Exception as exc:
        return 1, _error("macro_backfill_failed", exc)
    return 0, {"ok": True, "data": _json_ready(target)}


def _handle_professional_backfill() -> tuple[int, dict[str, Any]]:
    try:
        through_date = date.today()
        policies = professional_backfill_policies(through_date=through_date)
        settings = load_settings(require_ws_token=False)
        now_ms = _now_ms()
        targets = []
        with repositories(settings) as repos, repos.transaction():
            for policy in policies:
                spec = require_dataset(policy.dataset_id)
                if spec.instrument_id is not None:
                    repos.macro_market.ensure_instrument(spec, now_ms=now_ms)
                target = repos.macro.enqueue_backfill_target(
                    spec,
                    start_date=policy.start_date,
                    end_date=through_date,
                    now_ms=now_ms,
                    max_attempts=int(settings.workers.macro_backfill.max_attempts),
                )
                targets.append(
                    {
                        "dataset_id": policy.dataset_id,
                        "history_class": policy.history_class,
                        "partition_key": target["partition_key"],
                        "status": target["status"],
                    }
                )
    except Exception as exc:
        return 1, _error("macro_professional_backfill_failed", exc)
    return 0, {
        "ok": True,
        "data": {
            "through_date": through_date.isoformat(),
            "target_count": len(targets),
            "targets": targets,
        },
    }


def _handle_retry_research(args: Namespace) -> tuple[int, dict[str, Any]]:
    try:
        session_date = _parse_date(str(args.session_date))
        settings = load_settings(require_ws_token=False)
        with repositories(settings) as repos, repos.transaction():
            run = repos.macro_research.retry_failed_run(
                session_date=session_date,
                now_ms=_now_ms(),
            )
    except Exception as exc:
        return 1, _error("macro_retry_research_failed", exc)
    return 0, {"ok": True, "data": _json_ready(run)}


def _handle_status() -> tuple[int, dict[str, Any]]:
    try:
        settings = load_settings(require_ws_token=False)
        with repositories(settings) as repos:
            targets = repos.macro.target_states()
            receipts = repos.macro.recent_receipts(limit=20)
            modules = repos.macro.all_modules_current()
            judgment = repos.macro.daily_judgment()
            document_analysis_jobs = repos.macro.document_analysis_job_state()
            research = repos.macro_research.research_state()
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
                    "recent_receipts": receipts,
                    "modules": [
                        {
                            "module_id": row["module_id"],
                            "data_health_state": row["data_health_state"],
                            "fact_cutoff_ms": row["fact_cutoff_ms"],
                            "updated_at_ms": row["updated_at_ms"],
                        }
                        for row in modules
                    ],
                    "daily_judgment": judgment,
                    "document_analysis_jobs": document_analysis_jobs,
                    "research": research,
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
