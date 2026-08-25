"""Liquidation shadow research: registration, point-in-time freeze, outcomes, no capital writes."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from ..candidate.blacklist import Blacklist
from ..candidate.eligibility import Funnel, is_fresh_trigger, liquidation_candidate
from ..candidate.routing import resolve_instrument, signal_exchange_id
from ..contracts import (
    Bar,
    FrozenMarketContext,
    FrozenStrategyContext,
    InstrumentRef,
    LiquidationAggregate,
    LiquidationMarketTrigger,
    LiquidationTradeCandidate,
    LiveExchangeId,
    StrategyId,
    TradingCaseManifest,
    underlying_key,
)
from ..decision.regime import assess, pre_move_bps, select_bar
from ..research.event_study import (
    EVENT_STUDY_POLICY,
    EVENT_STUDY_SETTLEMENT_LAG_MS,
    EVENT_STUDY_VERSION,
    hypothesis_side,
    measure_event,
)
from ..strategy.root import TradingStrategy
from ..telemetry import TradingExternalDataTelemetryPort, external_data_source, observe_provider_call
from .runtime import (
    BAR_INTERVAL_MS,
    COLD_READ_TIMEOUT_SECONDS,
    COLD_WRITE_TIMEOUT_SECONDS,
    BarFetcherFactory,
    InstrumentProjectionReader,
    TradingConfig,
    TradingDatabasePort,
)

log = logging.getLogger("tracefold.trading")

_WINDOW_MS = 60_000
_MAX_PER_TURN = 4
_OUTCOME_RETRY_BASE_MS = 60_000
_OUTCOME_RETRY_MAX_MS = 3_600_000
LIQUIDATION_STRATEGY_IDS: tuple[StrategyId, StrategyId] = (
    "liquidation_continuation_shadow_v1",
    "liquidation_exhaustion_shadow_v1",
)


class LiquidationShadowRunner:
    """Own the research lifecycle so CandidateRunner remains the capital-case pipeline."""

    def __init__(
        self,
        *,
        db: TradingDatabasePort,
        config: TradingConfig,
        bars: BarFetcherFactory,
        instrument_projection: InstrumentProjectionReader,
        strategies: dict[StrategyId, TradingStrategy],
        telemetry: TradingExternalDataTelemetryPort | None,
        clock: Callable[[], int],
    ) -> None:
        self._db = db
        self._config = config
        self._bars = bars
        self._instrument_projection = instrument_projection
        self._strategies = strategies
        self._telemetry = telemetry
        self._clock = clock

    async def turn(self, state: dict[str, Any], *, funnel: Funnel, now: int) -> tuple[int, int]:
        registrations = await self._register(now)
        evaluated = await self._evaluate(state, registrations=registrations, funnel=funnel, now=now)
        completed = await self._complete(funnel=funnel, now=now)
        return evaluated, completed

    async def _register(self, now: int) -> dict[tuple[str, str, str], int]:
        def _write(repos: Any) -> dict[tuple[str, str, str], int]:
            result: dict[tuple[str, str, str], int] = {}
            for strategy_id in LIQUIDATION_STRATEGY_IDS:
                strategy = self._strategies[strategy_id]
                identity = (strategy.strategy_id, strategy.strategy_version, strategy.config_digest)
                result[identity] = repos.trading.register_strategy(
                    strategy_id=strategy.strategy_id,
                    strategy_version=strategy.strategy_version,
                    strategy_config_digest=strategy.config_digest,
                    strategy_config=strategy.config_snapshot,
                    permission=strategy.permission,
                    now_ms=now,
                )
            return result

        return await self._db.tx(
            "trading_strategy_register",
            _write,
            timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
        )

    async def _evaluate(
        self,
        state: dict[str, Any],
        *,
        registrations: dict[tuple[str, str, str], int],
        funnel: Funnel,
        now: int,
    ) -> int:
        blacklist: Blacklist = state["blacklist"]
        context_candidates: list[LiquidationTradeCandidate] = []
        candidates: list[LiquidationTradeCandidate] = []
        identities = state["evaluated_liquidation_identities"]
        funnel.count("liquidation_rows", len(state["liquidation_rows"]))
        for row in state["liquidation_rows"]:
            result = liquidation_candidate(row, now_ms=now, blacklist=blacklist, funnel=funnel)
            if not isinstance(result, LiquidationTradeCandidate):
                continue
            context_candidates.append(result)
            expected = {
                (
                    result.source_key,
                    self._strategies[strategy_id].strategy_id,
                    self._strategies[strategy_id].strategy_version,
                    self._strategies[strategy_id].config_digest,
                )
                for strategy_id in LIQUIDATION_STRATEGY_IDS
            }
            if expected <= identities:
                funnel.count("liquidation_already_evaluated")
            elif is_fresh_trigger(result.received_at_ms, now_ms=now, policy=self._config.eligibility):
                candidates.append(result)
            else:
                funnel.count("liquidation_context_only")

        created = 0
        for trigger in candidates[:_MAX_PER_TURN]:
            exchange = signal_exchange_id(trigger.venue)
            if exchange is None or exchange not in self._config.venue_priority:
                funnel.count("liquidation_shadow_reject:venue_not_enabled")
                continue
            instrument = await self._instrument(trigger, exchange=exchange, now=now)
            if instrument is None:
                funnel.count("liquidation_shadow_reject:no_perp_at_source_venue")
                continue
            bars = await self._fetch_bars(instrument, anchor_at_ms=trigger.received_at_ms)
            anchor = select_bar(
                bars,
                target_ms=trigger.received_at_ms,
                gap_tolerance_ms=self._config.regime.bar_gap_tolerance_ms,
            )
            if anchor is None:
                funnel.count("liquidation_shadow_reject:no_mark_at_cutoff")
                continue
            move = pre_move_bps(bars, anchor_at_ms=trigger.received_at_ms, policy=self._config.regime)
            aggregate = _aggregate(context_candidates, trigger=trigger)
            market = FrozenMarketContext(
                mark_price=anchor.close,
                observed_at_ms=trigger.received_at_ms,
                pre_move_bps=move,
                pre_move_lookback_ms=self._config.regime.lookback_ms,
                # The public source is 5-minute close-only. A 1-minute burst
                # cannot inherit the unrelated 1-hour pre-move as momentum or
                # displacement; those distinct features remain unavailable.
                price_momentum_bps=None,
                price_momentum_window_ms=_WINDOW_MS,
                displacement_bps=None,
                displacement_window_ms=_WINDOW_MS,
            )
            context = FrozenStrategyContext(
                mode=self._config.mode,
                liquidation=trigger,
                liquidation_aggregate=aggregate,
                regime=assess(oi_direction=None, move=move, policy=self._config.regime),
                market=market,
                intensity_decelerating=(
                    None if aggregate.dominant_acceleration_bps is None else aggregate.dominant_acceleration_bps < 0
                ),
            )
            primary = LiquidationMarketTrigger(
                source_key=trigger.source_key,
                observed_at_ms=trigger.event_at_ms,
                persisted_at_ms=trigger.received_at_ms,
                venue=trigger.venue,
            )
            evaluations: list[tuple[TradingCaseManifest, Any, int]] = []
            for strategy_id in LIQUIDATION_STRATEGY_IDS:
                strategy = self._strategies[strategy_id]
                identity = (
                    trigger.source_key,
                    strategy.strategy_id,
                    strategy.strategy_version,
                    strategy.config_digest,
                )
                if identity in identities:
                    continue
                registered = registrations[(strategy.strategy_id, strategy.strategy_version, strategy.config_digest)]
                if trigger.received_at_ms < registered:
                    funnel.count("liquidation_shadow_reject:holdout_precedes_strategy_registration")
                    continue
                manifest = TradingCaseManifest(
                    primary_trigger=primary,
                    contexts=context,
                    strategy_id=strategy.strategy_id,
                    strategy_version=strategy.strategy_version,
                    strategy_config=strategy.config_snapshot,
                    strategy_config_digest=strategy.config_digest,
                    underlying_key=underlying_key(trigger.base_symbol),
                    base_symbol=trigger.base_symbol,
                    cutoff_ms=trigger.received_at_ms,
                    instrument=instrument,
                )
                evaluations.append((manifest, strategy.evaluate(context), registered))
            if not evaluations:
                continue

            def _insert(
                repos: Any,
                rows: list[tuple[TradingCaseManifest, Any, int]] = evaluations,
                trigger_source_key: str = trigger.source_key,
            ) -> int:
                inserted = 0
                for manifest, outcome, registered in rows:
                    inserted += int(
                        repos.trading.insert_strategy_evaluation(
                            evaluation_id=uuid.uuid4().hex,
                            trigger_source_key=trigger_source_key,
                            underlying_key=manifest.underlying_key,
                            trigger_kind="liquidation",
                            strategy_id=manifest.strategy_id,
                            strategy_version=manifest.strategy_version,
                            strategy_config_digest=manifest.strategy_config_digest,
                            manifest=manifest.model_dump(mode="json"),
                            manifest_sha256=manifest.digest(),
                            decision=outcome.decision,
                            rule=outcome.rule,
                            setup=outcome.setup,
                            invalidation=outcome.invalidation,
                            expected_horizon=outcome.expected_horizon,
                            permission=outcome.permission,
                            strategy_registered_at_ms=registered,
                            research_partition="holdout",
                            cutoff_ms=manifest.cutoff_ms,
                            now_ms=now,
                        )
                    )
                return inserted

            inserted = await self._db.tx(
                "trading_liquidation_shadow_insert",
                _insert,
                timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
            )
            created += inserted
            funnel.count("liquidation_shadow_evaluated", inserted)
            funnel.count("liquidation_shadow_duplicate", len(evaluations) - inserted)
        return created

    async def _complete(self, *, funnel: Funnel, now: int) -> int:
        pending = await self._db.read(
            "trading_liquidation_outcomes_pending",
            lambda repos: repos.trading.pending_strategy_outcomes(
                before_cutoff_ms=now - EVENT_STUDY_SETTLEMENT_LAG_MS,
                now_ms=now,
                limit=32,
            ),
            timeout_seconds=COLD_READ_TIMEOUT_SECONDS,
        )
        completed = 0
        bar_cache: dict[tuple[str, str, int], list[Bar] | None] = {}
        for row in pending:
            try:
                manifest = TradingCaseManifest.model_validate(row["manifest"])
            except ValidationError:
                missing_outcome = measure_event(
                    (),
                    cutoff_ms=int(row["cutoff_ms"]),
                    decision=str(row["decision"]),  # type: ignore[arg-type]
                    research_side=None,
                    policy=EVENT_STUDY_POLICY,
                    gap_tolerance_ms=self._config.regime.bar_gap_tolerance_ms,
                )
                missing_outcome["outcome_unavailable_reason"] = "manifest_invalid"
                missing_outcome["missing_data"] = sorted([*missing_outcome["missing_data"], "manifest:invalid"])
                completed += int(await self._persist_outcome(row, outcome=missing_outcome, now=now))
                funnel.count("liquidation_outcome_terminal:manifest_invalid")
                continue
            cutoff = int(row["cutoff_ms"])
            bar_key = (
                manifest.instrument.exchange_id,
                manifest.instrument.provider_symbol,
                cutoff,
            )
            if bar_key not in bar_cache:
                bar_cache[bar_key] = await self._outcome_bars(
                    manifest.instrument,
                    cutoff=cutoff,
                    horizon=EVENT_STUDY_SETTLEMENT_LAG_MS,
                )
            bars = bar_cache[bar_key]
            if bars is None:
                await self._defer_outcome(row, now=now)
                funnel.count("liquidation_outcome_deferred:provider_unavailable")
                continue
            aggregate = manifest.contexts.liquidation_aggregate
            side = hypothesis_side(
                manifest.strategy_id,
                None if aggregate is None else aggregate.dominant_liquidated_side,
            )
            outcome = measure_event(
                bars,
                cutoff_ms=cutoff,
                decision=str(row["decision"]),  # type: ignore[arg-type]
                research_side=side,
                policy=EVENT_STUDY_POLICY,
                gap_tolerance_ms=self._config.regime.bar_gap_tolerance_ms,
            )
            completed += int(await self._persist_outcome(row, outcome=outcome, now=now))
        funnel.count("liquidation_outcome_completed", completed)
        return completed

    async def _persist_outcome(self, row: dict[str, Any], *, outcome: dict[str, Any], now: int) -> bool:
        return bool(
            await self._db.tx(
                "trading_liquidation_outcome_complete",
                lambda repos: repos.trading.complete_strategy_outcome(
                    evaluation_id=str(row["evaluation_id"]),
                    market_outcome=outcome,
                    market_outcome_version=EVENT_STUDY_VERSION,
                    now_ms=now,
                ),
                timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
            )
        )

    async def _defer_outcome(self, row: dict[str, Any], *, now: int) -> None:
        attempts = int(row["outcome_attempt_count"])
        retry_ms = min(_OUTCOME_RETRY_BASE_MS * (2 ** min(attempts, 6)), _OUTCOME_RETRY_MAX_MS)
        await self._db.tx(
            "trading_liquidation_outcome_defer",
            lambda repos: repos.trading.defer_strategy_outcome(
                evaluation_id=str(row["evaluation_id"]),
                expected_attempt_count=attempts,
                retry_at_ms=now + retry_ms,
            ),
            timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
        )

    async def _instrument(
        self, trigger: LiquidationTradeCandidate, *, exchange: LiveExchangeId, now: int
    ) -> InstrumentRef | None:
        rows = await self._db.read(
            "trading_liquidation_instrument",
            lambda repos: self._instrument_projection(repos, trigger.base_symbol, ("binance.perp", "hl.perp")),
            timeout_seconds=COLD_READ_TIMEOUT_SECONDS,
        )
        return resolve_instrument(rows, priority=(exchange,), observed_at_ms=now)

    async def _fetch_bars(self, instrument: InstrumentRef, *, anchor_at_ms: int) -> list[Bar]:
        fetcher = self._bars(instrument.exchange_id)
        if fetcher is None:
            return []
        start = anchor_at_ms - self._config.regime.lookback_ms - BAR_INTERVAL_MS
        try:
            bars = await observe_provider_call(
                self._telemetry,
                name="trading_candidate",
                source=external_data_source(instrument.exchange_id),
                call=fetcher(instrument.provider_symbol, start, anchor_at_ms + BAR_INTERVAL_MS),
            )
        except Exception:
            log.warning("trading liquidation cutoff bar fetch failed")
            return []
        return sorted(bars, key=lambda bar: bar.close_at_ms)

    async def _outcome_bars(self, instrument: InstrumentRef, *, cutoff: int, horizon: int) -> list[Bar] | None:
        fetcher = self._bars(instrument.exchange_id)
        if fetcher is None:
            return None
        try:
            bars = await observe_provider_call(
                self._telemetry,
                name="trading_candidate",
                source=external_data_source(instrument.exchange_id),
                call=fetcher(
                    instrument.provider_symbol,
                    cutoff - BAR_INTERVAL_MS,
                    cutoff + horizon + BAR_INTERVAL_MS,
                ),
            )
        except Exception:
            log.warning("trading liquidation outcome fetch failed")
            return None
        return sorted(bars, key=lambda bar: bar.close_at_ms)


def _aggregate(
    candidates: list[LiquidationTradeCandidate], *, trigger: LiquidationTradeCandidate
) -> LiquidationAggregate:
    visible = [
        item
        for item in candidates
        if item.base_symbol == trigger.base_symbol
        and item.venue == trigger.venue
        and item.received_at_ms <= trigger.received_at_ms
        and trigger.event_at_ms - _WINDOW_MS <= item.event_at_ms <= trigger.event_at_ms
    ]
    long_rows = [item for item in visible if item.liquidated_position_side == "long"]
    short_rows = [item for item in visible if item.liquidated_position_side == "short"]
    long_notional = sum((item.notional_usd for item in long_rows), Decimal("0"))
    short_notional = sum((item.notional_usd for item in short_rows), Decimal("0"))
    total = long_notional + short_notional
    dominant = "long" if long_notional > short_notional else "short" if short_notional > long_notional else None
    dominant_notional = max(long_notional, short_notional)
    dominant_rows = long_rows if dominant == "long" else short_rows if dominant == "short" else []
    dominant_older = sum(
        (item.notional_usd for item in dominant_rows if item.event_at_ms <= trigger.event_at_ms - _WINDOW_MS // 2),
        Decimal("0"),
    )
    dominant_recent = dominant_notional - dominant_older
    dominant_acceleration = None if dominant_older == 0 else int((dominant_recent / dominant_older - 1) * 10_000)
    return LiquidationAggregate(
        window_ms=_WINDOW_MS,
        count=len(visible),
        notional_usd=total,
        long_notional_usd=long_notional,
        short_notional_usd=short_notional,
        long_count=len(long_rows),
        short_count=len(short_rows),
        dominant_liquidated_side=dominant,
        dominant_share_bps=int(dominant_notional / total * 10_000),
        dominant_count=len(dominant_rows),
        dominant_notional_usd=dominant_notional,
        dominant_acceleration_bps=dominant_acceleration,
        source_refs=tuple(item.source_key for item in visible),
    )


__all__ = ["LIQUIDATION_STRATEGY_IDS", "LiquidationShadowRunner"]
