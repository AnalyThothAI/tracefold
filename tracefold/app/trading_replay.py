"""Run a candidate DSPy program only on previously frozen Trading CaseViews."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

from tracefold.app.learning_runtime import generative_lm
from tracefold.app.llm import configured_lm_endpoint, llm_is_configured
from tracefold.app.repository_session import repositories
from tracefold.app.trading_assessor import TradingAssessor, load_program, program_sha256
from tracefold.trading.engine.case_view import case_view_from_record
from tracefold.trading.engine.forecast import PolicyConfig, all_policy_decisions


async def replay(
    settings: Any,
    *,
    program_file: Path,
    since_ms: int,
    until_ms: int,
) -> dict[str, Any]:
    if since_ms < 0 or until_ms <= since_ms:
        raise ValueError("trading_replay_window_invalid")
    if not settings.trading.analysis.model_name or not llm_is_configured(settings):
        raise ValueError("trading_replay_model_unconfigured")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="trading-replay") as pool:
        program_sha = await asyncio.get_running_loop().run_in_executor(pool, program_sha256, program_file)
        program = await asyncio.get_running_loop().run_in_executor(
            pool, partial(load_program, program_file, program_sha)
        )
        endpoint = configured_lm_endpoint(settings, model_name=settings.trading.analysis.model_name)
        lm = generative_lm(
            endpoint,
            max_tokens=settings.trading.analysis.max_model_output_tokens,
            timeout=settings.trading.analysis.model_timeout_seconds,
            cache=True,
        )
        assessor = TradingAssessor(
            program=program,
            lm=lm,
            timeout_s=settings.trading.analysis.model_timeout_seconds,
            concurrent=settings.trading.analysis.max_model_concurrent_calls,
        )

        def read_cases() -> list[dict[str, Any]]:
            with repositories(settings, application_name="tracefold_trading_replay") as repos:
                return repos.trading.frozen_cases(since_ms=since_ms, until_ms=until_ms)

        cases = await asyncio.get_running_loop().run_in_executor(pool, read_cases)
        succeeded = 0
        failed: dict[str, int] = {}
        for case in cases:
            view = case_view_from_record(dict(case["view"]))
            started = int(time.time() * 1_000)
            result = await assessor.assess(view)
            ended = int(time.time() * 1_000)
            status = "ok" if result.status == "complete" else result.error_code or "provider"
            decisions = all_policy_decisions(
                view.features,
                result.forecast,
                PolicyConfig(view.geometry.stop_bps, view.geometry.tp_bps, view.half_spread_bps),
            )

            def record(
                view: Any = view,
                result: Any = result,
                status: str = status,
                started: int = started,
                ended: int = ended,
                decisions: Any = decisions,
            ) -> None:
                with repositories(settings, application_name="tracefold_trading_replay") as repos, repos.transaction():
                    repos.trading.record_assessment(
                        case_id=view.case_id,
                        program_sha=program_sha,
                        route=settings.trading.analysis.model_name,
                        status=status,
                        forecast=result.forecast,
                        notes=result.notes,
                        usage=result.usage,
                        started_at_ms=started,
                        ended_at_ms=ended,
                    )
                    repos.trading.record_policy_actions(
                        case_id=view.case_id,
                        program_sha=program_sha,
                        decisions=decisions,
                        now_ms=ended,
                    )

            await asyncio.get_running_loop().run_in_executor(pool, record)
            if status == "ok":
                succeeded += 1
            else:
                failed[status] = failed.get(status, 0) + 1
        return {"program_sha": program_sha, "cases": len(cases), "assessed": succeeded, "failures": failed}
