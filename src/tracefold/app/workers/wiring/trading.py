from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import dspy  # type: ignore[import-untyped]
from loguru import logger

from tracefold.app.llm import configured_lm_endpoint
from tracefold.app.worker_database import WorkerDatabase
from tracefold.integrations.venues import fetch_binance_candles, fetch_hyperliquid_candles
from tracefold.news import OI_METRIC_VERSION as NEWS_OI_METRIC_VERSION
from tracefold.platform.config.models import Settings
from tracefold.trading import DEFAULT_DEADLINE_SECONDS as TRADING_DECISION_DEADLINE_SECONDS
from tracefold.trading import DEFAULT_MAX_TOKENS as TRADING_DECISION_MAX_TOKENS
from tracefold.trading import Bar as TradingBar
from tracefold.trading import (
    EligibilityPolicy,
    OrderPolicy,
    RegimePolicy,
    TradePolicy,
    TradingConfig,
    TradingDecisionProgram,
)
from tracefold.trading import build_pipeline as build_trading_pipeline


class _TradingColdDb:
    """Trading's database admission: one slot on the heavy-business lane, never the News lane.

    Same shape and same reasoning as the price plane's cold lane (#88). The composition root owns it
    because `WorkerDatabase` is an app type and `tracefold.trading` depends on `platform` only.
    """

    def __init__(self, db: WorkerDatabase) -> None:
        self._db = db
        self._lane = db.heavy_business()

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        def _run() -> Any:
            with self._db.worker_session(name, timeout_seconds) as repos, repos.transaction():
                return fn(repos)

        return await self._lane.run_business(name, _run, operation_timeout_seconds=timeout_seconds)

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        def _run() -> Any:
            with self._db.worker_session(name, timeout_seconds) as repos:
                return fn(repos)

        return await self._lane.run_business(name, _run, operation_timeout_seconds=timeout_seconds)


def trading_config_from_settings(settings: Settings) -> TradingConfig:
    """Operator YAML -> the one frozen object a Trading turn runs under."""

    trading = settings.trading
    candidates = trading.candidates
    order = trading.order
    return TradingConfig(
        mode=trading.mode,
        account_ref=trading.account_ref,
        poll_seconds=float(trading.poll_seconds),
        oi_metric_version=NEWS_OI_METRIC_VERSION,
        venue_priority=trading.venues.enabled,
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


def _news_trade_candidates(
    repos: Any,
    metric_version: str,
    after_created_at_ms: int,
    until_created_at_ms: int,
    max_rank_in_window: int,
    min_oi_value_usd: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """App-owned handoff from the News projection into Trading's candidate reader."""

    return (
        repos.news.trade_candidate_oi_rows(
            metric_version=metric_version,
            after_created_at_ms=after_created_at_ms,
            until_created_at_ms=until_created_at_ms,
            max_rank_in_window=max_rank_in_window,
            min_oi_value_usd=min_oi_value_usd,
        ),
        repos.news.trade_candidate_news_rows(
            after_created_at_ms=after_created_at_ms,
            until_created_at_ms=until_created_at_ms,
        ),
    )


def _news_trade_instruments(repos: Any, base_symbol: str, venues: Sequence[str]) -> list[dict[str, Any]]:
    """App-owned handoff from News instrument facts into Trading's venue resolver."""

    return repos.news.trade_candidate_instrument(base_symbol=base_symbol, venues=venues)


def _wire_trading_pipeline(*, settings: Settings, db: WorkerDatabase) -> Any | None:
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

        return build_trading_pipeline(
            db=_TradingColdDb(db),
            config=trading_config_from_settings(settings),
            bars=_trading_bar_fetcher(settings),
            candidate_projection=_news_trade_candidates,
            instrument_projection=_news_trade_instruments,
            program=program,
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
