from __future__ import annotations

from typing import Any

import dspy  # type: ignore[import-untyped]
from loguru import logger

from tracefold.app.learning_runtime import active_arm_manifest
from tracefold.app.llm import configured_lm_endpoint
from tracefold.app.trading_config import trading_config_from_settings
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.app.workers.wiring.news_to_trading import news_trade_candidates, news_trade_instruments
from tracefold.integrations.venues import fetch_binance_candles, fetch_hyperliquid_candles
from tracefold.news.learning.contracts import epoch_id_for_bundle
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.trading.contracts import Bar as TradingBar
from tracefold.trading.decision.program import DEFAULT_DEADLINE_SECONDS as TRADING_DECISION_DEADLINE_SECONDS
from tracefold.trading.decision.program import DEFAULT_MAX_TOKENS as TRADING_DECISION_MAX_TOKENS
from tracefold.trading.decision.program import TradingDecisionProgram
from tracefold.trading.pipeline.root import TradingPipeline
from tracefold.trading.pipeline.root import build_pipeline as build_trading_pipeline


def _wire_trading_pipeline(
    *,
    settings: Settings,
    db: WorkerDatabase,
    telemetry: TelemetryRegistry | None = None,
) -> TradingPipeline | None:
    """#104. Disabled by default; a disabled Trading context constructs no program and no adapter.

    The runners share Event Reaction's one-slot heavy admission rather than the four News lane slots,
    for the same reason #88 gave: a trading backlog must not compete with the Deduper, Triage and the
    Deliverer for the lane they were budgeted.
    """

    trading = settings.trading
    if not trading.enabled:
        return None

    try:
        program = None
        if settings.llm.trading_decision_model and settings.llm.api_key and settings.llm.base_url:
            endpoint = configured_lm_endpoint(settings, model_name=settings.llm.trading_decision_model)
            program = TradingDecisionProgram(
                lm=dspy.LM(
                    endpoint.model_name,
                    api_key=endpoint.api_key,
                    api_base=endpoint.api_base,
                    temperature=0,
                    max_tokens=TRADING_DECISION_MAX_TOKENS,
                    timeout=TRADING_DECISION_DEADLINE_SECONDS,
                    cache=False,
                    num_retries=0,
                    **dict(endpoint.model_kwargs or {}),
                ),
                model_name=endpoint.model_name,
                deadline_seconds=TRADING_DECISION_DEADLINE_SECONDS,
            )

        return build_trading_pipeline(
            db=WorkerTradingDatabase(db),
            config=trading_config_from_settings(settings),
            bars=_trading_bar_fetcher(settings),
            candidate_projection=news_trade_candidates,
            instrument_projection=news_trade_instruments,
            # The one place that may tell Trading which News generation is running (#314). Trading holds
            # no News literal and reads no News table; this seam derives the label from the same stable
            # arm the News workers appoint, so the two cannot drift.
            news_generation=epoch_id_for_bundle(active_arm_manifest(settings).bundle_sha),
            program=program,
            telemetry=telemetry,
        )
    except Exception:
        logger.exception("trading pipeline wiring failed; Trading stays disabled for this process")
        return None


def _trading_bar_fetcher(settings: Any) -> Any:
    """Exchange id -> native perp candles, reusing the venue adapters the reaction plane already owns.

    Only the two native perp venues are wired. HIP-3 builder markets (`hl.xyz`) are excluded in V1, so
    there is deliberately no path here that could price one.
    """

    venue_for = {"binance": "binance.perp", "hyperliquid": "hl.perp"}
    enabled = {"binance", "hyperliquid"}

    def factory(exchange_id: str) -> Any | None:
        venue = venue_for.get(str(exchange_id))
        if venue is None or str(exchange_id) not in enabled:
            return None

        async def fetch(provider_symbol: str, start_ms: int, end_ms: int) -> Any:
            if venue.startswith("binance."):
                candles = await fetch_binance_candles(provider_symbol, venue=venue, start_ms=start_ms, end_ms=end_ms)
            else:
                candles = await fetch_hyperliquid_candles(
                    provider_symbol, venue=venue, start_ms=start_ms, end_ms=end_ms
                )
            return tuple(TradingBar(open_at_ms=c.open_at_ms, close_at_ms=c.close_at_ms, close=c.close) for c in candles)

        return fetch

    return factory
