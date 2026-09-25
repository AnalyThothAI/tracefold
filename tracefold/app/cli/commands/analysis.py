"""Run one process-local Analysis harness with a shared market client."""

from __future__ import annotations

import asyncio
import signal

from tracefold.app.llm import configured_lm_endpoint, llm_is_configured
from tracefold.app.system_one import SystemOneConnection
from tracefold.app.trading_analysis import AnalysisRunner
from tracefold.app.trading_analyst import TradeAnalyst
from tracefold.integrations.marketdata.binance import BinanceMarketData
from tracefold.platform.config.loader import load_settings
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import setup_logging


async def _run(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signo in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signo, stop.set)
    if not settings.trading.enabled:
        await stop.wait()
        return
    model = settings.trading.analysis.model_name or settings.llm.news_triage_model
    analyst = None
    if model and llm_is_configured(settings):
        endpoint = configured_lm_endpoint(settings, model_name=model)
        analyst = TradeAnalyst(
            endpoint,
            timeout_seconds=settings.trading.analysis.model_timeout_seconds,
            max_input_bytes=settings.trading.analysis.max_model_input_bytes,
            max_output_tokens=settings.trading.analysis.max_model_output_tokens,
            max_concurrent_calls=settings.trading.analysis.max_model_concurrent_calls,
            cost_budget_microusd=settings.trading.analysis.model_cost_budget_microusd,
            input_price_ceiling_usd_per_million=(settings.trading.analysis.model_input_price_ceiling_usd_per_million),
            output_price_ceiling_usd_per_million=(settings.trading.analysis.model_output_price_ceiling_usd_per_million),
        )
    market = BinanceMarketData(
        max_connections=settings.trading.analysis.market_max_connections,
        max_cached_rows=settings.trading.analysis.market_max_cached_rows,
        weight_soft_limit_1m=settings.trading.analysis.market_weight_soft_limit_1m,
    )
    semantic_route = settings.llm.trading_semantics
    semantics = None
    if semantic_route.configured:
        if semantic_route.base_url is None or semantic_route.api_key is None or semantic_route.model is None:
            raise ValueError("trading_semantic_route_incomplete")
        semantics = SystemOneConnection(
            base_url=semantic_route.base_url,
            api_key=semantic_route.api_key,
            model=semantic_route.model,
            timeout_seconds=settings.trading.analysis.model_timeout_seconds,
        )
    try:
        runner = AnalysisRunner(
            settings=settings,
            market_data=market,
            analyst=analyst,
            files_root=settings.app_home / "archive" / "trading-analysis",
            max_active_cases=settings.trading.analysis.max_active_cases,
            semantics=semantics,
        )
        await runner.run(stop)
    finally:
        await market.aclose()
        if analyst is not None:
            await analyst.aclose()
        if semantics is not None:
            await semantics.aclose()


def handle_analysis(_args: object) -> int:
    settings = load_settings(require_ws_token=False)
    setup_logging(settings.log_file.with_name("analysis.log"))
    asyncio.run(_run(settings))
    return 0
