"""Isolated inference runs and deterministic policy replay over frozen Cases."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, Literal

from tracefold.app.learning_runtime import generative_lm
from tracefold.app.llm import configured_lm_endpoint, llm_is_configured
from tracefold.app.repository_session import async_postgres_connection, repositories
from tracefold.app.trading_assessor import TradingAssessor, load_program, program_sha256
from tracefold.app.trading_evaluation import evaluator_spec, load_calibration, policy_config, policy_manifest
from tracefold.trading.engine.case_view import case_view_from_record
from tracefold.trading.engine.evaluation import EvaluationRun, content_id
from tracefold.trading.engine.forecast import all_policy_decisions, forecast_from_record


async def replay(
    settings: Any,
    *,
    program_file: Path | None,
    since_ms: int,
    until_ms: int,
    mode: Literal["inference", "policies"] = "inference",
    source_run_id: str | None = None,
    tag: str = "default",
) -> dict[str, Any]:
    if since_ms < 0 or until_ms <= since_ms or mode not in ("inference", "policies"):
        raise ValueError("trading_replay_window_or_mode_invalid")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="trading-replay") as pool:

        async def db(fn: Any, *, transaction: bool = False) -> Any:
            def execute() -> Any:
                with repositories(settings, application_name="tracefold_trading_replay") as repos:
                    if transaction:
                        with repos.transaction():
                            return fn(repos.trading)
                    return fn(repos.trading)

            return await asyncio.get_running_loop().run_in_executor(pool, execute)

        cases = await db(lambda storage: storage.frozen_cases(since_ms=since_ms, until_ms=until_ms))
        ids = [case["case_id"] for case in cases]
        assessor = None
        if mode == "inference":
            if program_file is None or not settings.trading.analysis.model_name or not llm_is_configured(settings):
                raise ValueError("trading_replay_program_or_model_unconfigured")
            sha = await asyncio.get_running_loop().run_in_executor(pool, program_sha256, program_file)
            program = await asyncio.get_running_loop().run_in_executor(pool, partial(load_program, program_file, sha))
            spec = evaluator_spec(settings, sha)
            if cases:
                spec = replace(spec, input_contract="|".join(sorted({case["view"]["version"] for case in cases})))
            run = EvaluationRun.replay(
                spec,
                case_ids=ids,
                since_ms=since_ms,
                until_ms=until_ms,
                policy_config=policy_manifest(settings),
                mode=mode,
                tag=tag,
            )
            endpoint = configured_lm_endpoint(settings, model_name=settings.trading.analysis.model_name)
            lm = generative_lm(
                endpoint,
                settings=settings,
                max_tokens=settings.trading.analysis.max_model_output_tokens,
                timeout=settings.trading.analysis.model_timeout_seconds,
                cache=False,
            )
            assessor = TradingAssessor(
                program=program,
                lm=lm,
                timeout_s=settings.trading.analysis.model_timeout_seconds,
                concurrent=settings.trading.analysis.max_model_concurrent_calls,
            )
        else:
            if source_run_id is None:
                raise ValueError("trading_policy_replay_source_required")
            source = await db(lambda storage: storage.evaluation_run(source_run_id))
            if source is None:
                raise ValueError("trading_policy_replay_source_missing")
            manifest = {
                "mode": mode,
                "source_run_id": source_run_id,
                "case_ids": sorted(ids),
                "since_ms": since_ms,
                "until_ms": until_ms,
                "policy_config": policy_manifest(settings),
                "tag": tag,
            }
            if not tag or len(tag) > 128:
                raise ValueError("evaluation_run_manifest_invalid")
            run = EvaluationRun(
                content_id(manifest),
                source["evaluator_id"],
                mode,
                source["evaluator_spec"],
                manifest,
            )
        calibration = load_calibration(settings, evaluator_id=run.evaluator_id)
        # One run owner prevents duplicate provider calls across concurrent replays.
        # Session locking is outside all ledger transactions and releases on cancellation.
        key = int.from_bytes(bytes.fromhex(content_id({"replay_run": run.run_id}))[:8], "big", signed=True)
        async with async_postgres_connection(settings, application_name="tracefold_replay_owner") as owner:
            cursor = await owner.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,))
            ownership = await cursor.fetchone()
            if not ownership["acquired"]:
                raise ValueError("trading_replay_run_busy")
            await db(
                lambda storage: storage.register_evaluation_run(run, now_ms=int(time.time() * 1000)), transaction=True
            )
            succeeded = 0
            failed: dict[str, int] = {}
            for case in cases:
                view = case_view_from_record(dict(case["view"]))
                checkpoint = await db(
                    lambda storage, view=view: storage.assessment_for_run(case_id=view.case_id, run_id=run.run_id)
                )
                if checkpoint is None:
                    started = int(time.time() * 1000)
                    reused = None
                    if assessor is not None:
                        result = await assessor.assess(view)
                        forecast, notes, usage, metadata = (
                            result.forecast,
                            result.notes,
                            result.usage,
                            result.error_metadata,
                        )
                        status = "ok" if result.status == "complete" else result.error_code or "provider"
                    else:
                        prior = await db(
                            lambda storage, view=view: storage.assessment_for_run(
                                case_id=view.case_id, run_id=source_run_id
                            )
                        )
                        forecast = None if prior is None else forecast_from_record(prior["forecast"], prior["drivers"])
                        status = "data_missing" if prior is None else prior["status"]
                        notes = () if prior is None else tuple(prior["notes"])
                        usage = {"input_tokens": 0, "output_tokens": 0}
                        reused = None if prior is None else prior["assessment_id"]
                        metadata = {
                            "observation": "source_missing" if prior is None else "stored_output",
                            "provider_called": False,
                        }
                    ended = int(time.time() * 1000)
                    values = {
                        "case_id": view.case_id,
                        "run_id": run.run_id,
                        "status": status,
                        "forecast": forecast,
                        "notes": notes,
                        "usage": usage,
                        "started_at_ms": started,
                        "ended_at_ms": ended,
                        "error_metadata": metadata,
                        "reused_assessment_id": reused,
                    }
                    assessment = await db(
                        lambda storage, values=values: storage.record_assessment(**values),
                        transaction=True,
                    )
                else:
                    forecast = forecast_from_record(checkpoint["forecast"], checkpoint["drivers"])
                    status, assessment = checkpoint["status"], checkpoint["assessment_id"]
                config = policy_config(view, settings, calibration)
                decisions = all_policy_decisions(view.features, forecast, config)
                await db(
                    lambda storage, assessment=assessment, decisions=decisions, config=config: (
                        storage.record_policy_actions(
                            assessment=assessment,
                            decisions=decisions,
                            policy_config=config.snapshot(),
                            now_ms=int(time.time() * 1000),
                        )
                    ),
                    transaction=True,
                )
                if status == "ok":
                    succeeded += 1
                else:
                    failed[status] = failed.get(status, 0) + 1
            return {
                "run_id": run.run_id,
                "evaluator_id": run.evaluator_id,
                "mode": mode,
                "program_sha": run.evaluator_spec["program_sha"],
                "cases": len(cases),
                "assessed": succeeded,
                "failures": failed,
            }
