"""Run one process-local Analysis harness with a shared market client."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import dspy  # type: ignore[import-untyped]

from tracefold.app.learning_runtime import generative_lm
from tracefold.app.llm import configured_lm_endpoint, llm_is_configured
from tracefold.app.trading_analysis import AnalysisRunner
from tracefold.app.trading_assessor import TradingAssessor, load_program
from tracefold.integrations.marketdata.binance import BinanceMarketData
from tracefold.platform.config.loader import load_settings
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import setup_logging


def _load_configured_program(path_value: str, program_sha: str) -> dspy.Predict:
    configured_path = Path(path_value).expanduser()
    path = (
        configured_path
        if configured_path.is_absolute()
        else (Path(__file__).resolve().parents[3] / "trading" / "programs" / configured_path)
    )
    return load_program(path, program_sha)


async def _run(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signo in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signo, stop.set)
    if not settings.trading.enabled:
        await stop.wait()
        return
    analysis = settings.trading.analysis
    assessor = None
    fault_code = None
    program_sha = "0" * 64
    if analysis.program is None:
        fault_code = "program_unconfigured"
    else:
        program_sha = analysis.program.sha256
        try:
            program = _load_configured_program(analysis.program.path, program_sha)
        except (OSError, ValueError):
            fault_code = "program_invalid"
        else:
            if not analysis.model_name or not llm_is_configured(settings):
                fault_code = "model_unconfigured"
            else:
                endpoint = configured_lm_endpoint(settings, model_name=analysis.model_name)
                lm = generative_lm(
                    endpoint,
                    settings=settings,
                    max_tokens=analysis.max_model_output_tokens,
                    timeout=analysis.model_timeout_seconds,
                )
                assessor = TradingAssessor(
                    program=program,
                    lm=lm,
                    timeout_s=analysis.model_timeout_seconds,
                    concurrent=analysis.max_model_concurrent_calls,
                )
    market = BinanceMarketData(
        max_connections=settings.trading.analysis.market_max_connections,
        max_cached_rows=settings.trading.analysis.market_max_cached_rows,
        weight_soft_limit_1m=settings.trading.analysis.market_weight_soft_limit_1m,
    )
    try:
        runner = AnalysisRunner(
            settings=settings,
            market_data=market,
            execution_catalogue=market.catalogue,
            assessor=assessor,
            program_sha=program_sha,
            raw_root=settings.app_home / "archive" / "trading-cases",
            fault_code=fault_code,
        )
        await runner.run(stop)
    finally:
        await market.aclose()


def handle_analysis(_args: object) -> int:
    settings = load_settings(require_ws_token=False)
    setup_logging(settings.log_file.with_name("analysis.log"))
    asyncio.run(_run(settings))
    return 0
