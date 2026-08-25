from __future__ import annotations

from typing import Any, cast

import dspy  # type: ignore[import-untyped]
from loguru import logger

from tracefold.app.llm import configured_lm_endpoint
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.app.workers.wiring.news_to_trading import news_trade_candidates, news_trade_instruments
from tracefold.integrations.opentrade import OpenTradeAdapter
from tracefold.integrations.venues import fetch_binance_candles, fetch_hyperliquid_candles
from tracefold.news.oi_signals import METRIC_VERSION as NEWS_OI_METRIC_VERSION
from tracefold.platform.config.models import Settings
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.platform.observability import TelemetryRegistry
from tracefold.trading.candidate.eligibility import EligibilityPolicy
from tracefold.trading.contracts import Bar as TradingBar
from tracefold.trading.contracts import LiveExchangeId
from tracefold.trading.decision.policy import TradePolicy
from tracefold.trading.decision.program import DEFAULT_DEADLINE_SECONDS as TRADING_DECISION_DEADLINE_SECONDS
from tracefold.trading.decision.program import DEFAULT_MAX_TOKENS as TRADING_DECISION_MAX_TOKENS
from tracefold.trading.decision.program import TradingDecisionProgram
from tracefold.trading.decision.regime import RegimePolicy
from tracefold.trading.execution.order import OrderPolicy
from tracefold.trading.pipeline.root import TradingPipeline
from tracefold.trading.pipeline.root import build_pipeline as build_trading_pipeline
from tracefold.trading.pipeline.runtime import TradingConfig


def trading_config_from_settings(settings: Settings) -> TradingConfig:
    """Operator YAML -> the one frozen object a Trading turn runs under."""

    trading = settings.trading
    candidates = trading.candidates
    order = trading.order
    return TradingConfig(
        mode=trading.mode,
        account_ref=trading.account_ref,
        live_symbol=trading.live_symbol,
        poll_seconds=float(trading.poll_seconds),
        oi_metric_version=NEWS_OI_METRIC_VERSION,
        venue_priority=cast(tuple[LiveExchangeId, ...], trading.venues.enabled),
        eligibility=EligibilityPolicy(
            max_age_ms=candidates.max_age_seconds * 1000,
            max_rank_in_window=candidates.max_rank_in_window,
            min_oi_value_usd=candidates.min_oi_value_usd,
            news_lookback_ms=candidates.news_lookback_seconds * 1000,
            oi_lookback_ms=candidates.oi_lookback_seconds * 1000,
            symbol_cooldown_ms=candidates.symbol_cooldown_seconds * 1000,
        ),
        regime=RegimePolicy(
            lookback_ms=trading.regime.lookback_seconds * 1000,
            min_price_move_bps=trading.regime.min_price_move_bps,
            max_price_move_bps=trading.regime.max_price_move_bps,
        ),
        trade=TradePolicy(
            allow_short=trading.policy.allow_short,
            live_min_surprise=trading.policy.live_min_surprise,
            live_max_price_in=trading.policy.live_max_price_in,
            min_whale_long_profit_bps=trading.policy.min_whale_long_profit_bps,
            min_oi_value_usd=candidates.min_oi_value_usd,
        ),
        order=OrderPolicy(
            fixed_notional_usd=order.fixed_notional_usd,
            fixed_stop_bps=order.fixed_stop_bps,
            take_profit_bps=order.take_profit_bps,
            max_holding_ms=order.max_holding_seconds * 1000,
            max_spread_bps=order.max_spread_bps,
            max_open_underlyings=order.max_open_underlyings,
            max_orders_per_day=order.max_orders_per_day,
        ),
        max_dspy_cases_per_day=candidates.max_dspy_cases_per_day,
    )


def _wire_trading_pipeline(
    *,
    settings: Settings,
    db: WorkerDatabase,
    telemetry: TelemetryRegistry | None = None,
) -> TradingPipeline | None:
    """#104. Disabled by default; a disabled Trading context constructs no program and no adapter.

    The runners share the price plane's one-slot cold admission rather than the four News lane slots,
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

        adapter = None
        if trading.mode == "live_reviewed":
            token_path = settings.trading_opentrade_token_file()
            if token_path is None:
                raise ValueError("trading_opentrade_token_file_missing")
            try:
                token = read_secure_secret_text(token_path)
            except SecretFileError as exc:
                raise ValueError(f"trading_opentrade_token_file_{exc.code}") from None
            adapter = OpenTradeAdapter(
                base_url=str(trading.opentrade.base_url),
                token=token,
                request_timeout_seconds=trading.opentrade.request_timeout_seconds,
            )

        return build_trading_pipeline(
            db=WorkerTradingDatabase(db),
            config=trading_config_from_settings(settings),
            bars=_trading_bar_fetcher(settings),
            candidate_projection=news_trade_candidates,
            instrument_projection=news_trade_instruments,
            program=program,
            adapter=adapter,
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
    enabled = set(settings.trading.venues.enabled)

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
