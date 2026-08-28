"""Candidate fusion, decision, and the one-attempt entry path."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from pydantic import ValidationError

from ..candidate.blacklist import Blacklist
from ..candidate.eligibility import (
    EligibilityPolicy,
    Funnel,
    Rejected,
    _uses_current_news_generation,
    news_candidate,
    oi_candidate,
)
from ..candidate.fusion import _Plan, plan_triggers
from ..candidate.gate import (
    CANDIDATE_GATE_VERSION,
    CandidateGateResult,
    GateConfig,
    admit_context,
    admit_route,
    admit_trigger,
    case_created,
    defer,
    reject,
    source_rejected,
)
from ..candidate.routing import resolve_instrument, signal_exchange_id
from ..contracts import (
    Bar,
    CaseState,
    FrozenMarketContext,
    FrozenStrategyContext,
    InstrumentRef,
    NewsMarketTrigger,
    NewsTradeCandidate,
    OiMarketTrigger,
    OiTradeCandidate,
    TradeDecision,
    TradingCaseManifest,
    TriggerKind,
    oi_source_key,
    underlying_key,
)
from ..contracts import (
    utc_day_key as _day_key,
)
from ..decision.program import TradingDecisionProgram
from ..decision.regime import assess, pre_move_bps, select_bar
from ..intent import TradeIntent, capability_instrument_id, is_executable_instrument
from ..storage.root import TradingRepository
from ..strategy.root import capital_strategy_id, strategies, strategy_from_manifest
from ..telemetry import (
    TradingExternalDataTelemetryPort,
    TradingWorkSemantics,
    external_data_source,
    observe_provider_call,
)
from .liquidation_shadow import LiquidationShadowRunner
from .runtime import (
    BAR_INTERVAL_MS as _BAR_INTERVAL_MS,
)
from .runtime import (
    COLD_READ_TIMEOUT_SECONDS as _COLD_READ_TIMEOUT_SECONDS,
)
from .runtime import (
    COLD_WRITE_TIMEOUT_SECONDS as _COLD_WRITE_TIMEOUT_SECONDS,
)
from .runtime import (
    BarFetcherFactory,
    TradingConfig,
    TradingDatabasePort,
)
from .runtime import (
    CandidateProjectionReader as _CandidateProjectionReader,
)
from .runtime import (
    InstrumentProjectionReader as _InstrumentProjectionReader,
)
from .runtime import cutoff_history_start_ms as _cutoff_history_start_ms
from .runtime import (
    now_ms as _now_ms,
)
from .runtime import (
    sleep_or_stop as _sleep_or_stop,
)

log = logging.getLogger("tracefold.trading")

# A bounded overlap instead of a cursor. Everything inside `max_age_ms` is still fresh enough to trade,
# so re-reading a multiple of that window each turn is what makes a crash, a redeploy or a paused
# runner self-healing; `primary_source_key` rejects whatever the previous turn already made a case of.
_SCAN_OVERLAP_FACTOR = 3
_CASE_LEASE_MS = 60_000
_MAX_CASES_PER_TURN = 4
# How long a frozen case may wait to be decided. Its own budget, separate from the freshness the
# candidate rules already spent, so queueing behind another case's model call cannot discard a signal.
_CASE_DECISION_TTL_MS = 300_000
# How long an admission decision is kept. The lane persists about 90 OI facts a day, so this is a few
# thousand rows — small enough that the question "why was there no case last Tuesday" stays answerable.
_GATE_RETENTION_MS = 90 * 86_400_000


class CandidateRunner:
    """Scan, freeze, decide, and atomically publish one immutable Intent."""

    work_semantics: ClassVar[tuple[TradingWorkSemantics, ...]] = ("derived_work", "capital_truth")

    def __init__(
        self,
        *,
        db: TradingDatabasePort,
        config: TradingConfig,
        bars: BarFetcherFactory,
        candidate_projection: _CandidateProjectionReader,
        instrument_projection: _InstrumentProjectionReader,
        news_generation: str,
        program: TradingDecisionProgram | None = None,
        clock: Callable[[], int] = _now_ms,
        telemetry: TradingExternalDataTelemetryPort | None = None,
    ) -> None:
        self._db = db
        self._config = config
        # The News generation this process may advance a persisted Case under. Supplied by the app seam,
        # which is the only thing that knows both capabilities; Trading never reads a News table.
        self._news_generation = news_generation
        self._bars = bars
        self._candidate_projection = candidate_projection
        self._instrument_projection = instrument_projection
        self._program = program
        self._strategies = strategies(min_whale_long_profit_bps=config.trade.min_whale_long_profit_bps)
        # One object holding every number this lane executes on the way in, and its digest is half the
        # durable decision's key: editing a threshold starts a new record rather than rewriting what the
        # previous threshold decided.
        self._gate_config = GateConfig.from_policy(config.eligibility, venue_priority=config.venue_priority)
        self._clock = clock
        self._telemetry = telemetry
        self._liquidation_shadow = LiquidationShadowRunner(
            db=db,
            config=config,
            bars=bars,
            instrument_projection=instrument_projection,
            strategies=self._strategies,
            telemetry=telemetry,
            clock=clock,
        )
        self._run_id = uuid.uuid4().hex

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            started = time.perf_counter()
            try:
                result = await self.turn()
            except Exception:
                if self._telemetry is not None:
                    self._telemetry.record_external_data_turn(
                        "trading_candidate",
                        "error",
                        time.perf_counter() - started,
                    )
                log.exception("trading candidate turn failed")
            else:
                if self._telemetry is not None:
                    self._telemetry.record_external_data_turn(
                        "trading_candidate",
                        "error" if result.get("skipped") == "state_unavailable" else "success",
                        time.perf_counter() - started,
                    )
            await _sleep_or_stop(stop_event, self._config.poll_seconds)

    # ------------------------------------------------------------------ turn
    async def turn(self) -> dict[str, Any]:
        now = self._clock()
        funnel = Funnel()
        state = await self._read_state(now)
        if state is None:
            return {"skipped": "state_unavailable"}
        shadow_evaluated, shadow_completed = await self._liquidation_shadow.turn(state, funnel=funnel, now=now)
        control = str(state["control"])
        if control in ("PAUSED", "CLOSE_ONLY"):
            # Reconciliation and safety closes keep running; only new exposure stops.
            funnel.count(f"scan_skipped_control:{control.lower()}")
            await self._merge_funnel(funnel, now)
            return {
                "control": control,
                "shadow_completed": shadow_completed,
                "shadow_evaluated": shadow_evaluated,
                "funnel": funnel.as_dict(),
            }
        # Every OI source this turn looked at gets exactly one entry here, and the whole map is written
        # once at the end of the turn. `CASE_CREATED` is the exception: it is committed inside the case
        # insert's own transaction, because a case with no admission row — or an admission row naming a
        # case that failed to insert — is precisely the ambiguity the ledger exists to remove.
        gate: dict[str, CandidateGateResult] = {}
        plans = self._plan(state, funnel=funnel, now=now, gate=gate)
        created = 0
        for plan in plans[:_MAX_CASES_PER_TURN]:
            if await self._freeze(plan, funnel=funnel, now=now, gate=gate):
                created += 1
        for plan in plans[_MAX_CASES_PER_TURN:]:
            self._defer_for_capacity(plan, funnel=funnel, gate=gate)

        decided = 0
        for _ in range(_MAX_CASES_PER_TURN):
            outcome = await self._advance(funnel=funnel)
            if outcome is None:
                break
            decided += 1

        await self._write_gate(gate, now)
        await self._maintain_gate(now)
        await self._merge_funnel(funnel, now)
        return {
            "created": created,
            "decided": decided,
            "gate": len(gate),
            "shadow_completed": shadow_completed,
            "shadow_evaluated": shadow_evaluated,
            "funnel": funnel.as_dict(),
        }

    # ------------------------------------------------------------------ gate ledger
    def _record(self, result: CandidateGateResult, *, funnel: Funnel, gate: dict[str, CandidateGateResult]) -> None:
        """One answer per source per turn, counted under the same name the ledger files it under."""

        gate[result.source_key] = result
        funnel.count(result.funnel_key)

    def _defer_for_capacity(
        self,
        plan: _Plan,
        *,
        funnel: Funnel,
        gate: dict[str, CandidateGateResult],
    ) -> None:
        """A source that passed every rule about itself, refused because the lane had no room."""

        source = plan.oi if plan.trigger_kind == "oi" else plan.news
        if source is None:
            return
        self._record(defer(source, stage="eligibility", reason="lane_capacity_exhausted"), funnel=funnel, gate=gate)

    async def _write_gate(self, gate: dict[str, CandidateGateResult], now: int) -> None:
        """Flush the turn's admission answers. One transaction; a failure here never blocks capital."""

        if not gate:
            return
        results = list(gate.values())
        digest = self._gate_config.digest

        def _write(repos: Any) -> None:
            for result in results:
                repos.trading.record_gate_decision(
                    source_key=result.source_key,
                    gate_version=CANDIDATE_GATE_VERSION,
                    gate_config_digest=digest,
                    trigger_kind=result.trigger_kind,
                    underlying_key=result.underlying_key,
                    source_observed_at_ms=result.source_observed_at_ms,
                    status=result.status,
                    stage=result.stage,
                    reason=result.reason,
                    retryable=result.retryable,
                    evidence=result.evidence,
                    case_id=result.case_id,
                    now_ms=now,
                )

        try:
            await self._db.tx("trading_gate_write", _write, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        except Exception:
            # The ledger is evidence, not authority. Losing a turn's worth of it is a reporting gap the
            # next scan closes; refusing to trade because a reporting write failed would be worse.
            log.exception("trading candidate gate write failed")

    async def _maintain_gate(self, now: int) -> None:
        """Close decisions the clock has answered, and drop the ones past retention."""

        stale_before = now - self._config.eligibility.max_age_ms
        purge_before = now - _GATE_RETENTION_MS

        def _maintain(repos: Any) -> None:
            repos.trading.expire_stale_gate_decisions(stale_before_ms=stale_before, now_ms=now)
            repos.trading.purge_gate_decisions(observed_before_ms=purge_before)

        try:
            await self._db.tx("trading_gate_maintenance", _maintain, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        except Exception:
            log.exception("trading candidate gate maintenance failed")

    async def _merge_funnel(self, funnel: Funnel, now: int) -> None:
        counts = funnel.as_dict()
        if not counts:
            return
        try:
            await self._db.tx(
                "trading_funnel_merge",
                lambda repos: repos.trading.merge_funnel(day_key=_day_key(now), counts=counts, now_ms=now),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
        except Exception:
            log.warning("trading funnel merge failed")

    # ------------------------------------------------------------------ read
    async def _read_state(self, now: int) -> dict[str, Any] | None:
        elig = self._config.eligibility
        window = scan_horizon_ms(elig)

        def _read(repos: Any) -> dict[str, Any]:
            trading: TradingRepository = repos.trading
            runtime = trading.runtime_state() or {"control": "RUNNING", "day_key": ""}
            try:
                blacklist = Blacklist.from_rows(trading.blacklist_rows())
            except Exception:
                log.exception("trading blacklist read failed")
                blacklist = Blacklist.unavailable()
            entries_today = trading.entry_fences_today(day_key=_day_key(now))
            dspy_calls_today = trading.dspy_calls_today(day_key=_day_key(now))
            active = set(trading.active_intent_underlyings())
            in_flight = set(trading.underlyings_in_flight())
            cased = set(trading.source_keys_since(observed_at_ms=now - window))
            evaluated_liquidations = set(trading.strategy_evaluation_identities_since(cutoff_ms=now - window))
            oi_rows, news_rows, liquidation_rows = self._candidate_projection(
                repos,
                self._config.oi_metric_version,
                now - window,
                now,
            )
            return {
                "control": runtime.get("control", "RUNNING"),
                "entries_today": entries_today,
                "dspy_calls_today": dspy_calls_today,
                "blacklist": blacklist,
                "active": active,
                "cases_in_flight": in_flight,
                "cased_source_keys": cased,
                "evaluated_liquidation_identities": evaluated_liquidations,
                "oi_rows": oi_rows,
                "news_rows": news_rows,
                "liquidation_rows": liquidation_rows,
            }

        try:
            state: dict[str, Any] = await self._db.read(
                "trading_scan", _read, timeout_seconds=_COLD_READ_TIMEOUT_SECONDS
            )
        except Exception:
            log.exception("trading scan read failed")
            return None
        return state

    # ------------------------------------------------------------------ plan
    def _plan(
        self,
        state: dict[str, Any],
        *,
        funnel: Funnel,
        now: int,
        gate: dict[str, CandidateGateResult],
    ) -> list[_Plan]:
        """Every row this turn read, admitted once, and reduced to at most one plan per underlying.

        The OI lane runs the Candidate Gate here and nowhere else (#264). Three questions used to be
        asked at three different depths — "is the row usable" in `oi_candidate`, "is it fresh" inside
        `plan_triggers`, "is it routable" in a loop between them — and none of the three left a durable
        trace, so `oi_rows = 0` could mean any of them or none. Now each OI source leaves exactly one
        named answer in `gate` before this method returns.

        `plan_triggers` keeps what it is good at and nothing else: point-in-time attachment and the
        coalescing that reduces several triggers for one issuer to one plan.
        """

        elig = self._config.eligibility
        blacklist: Blacklist = state["blacklist"]
        funnel.count("oi_rows", len(state["oi_rows"]))
        funnel.count("news_rows", len(state["news_rows"]))

        # The context set a News trigger may attach from: routable, and passing the two rules that read
        # only the frame's own frozen numbers. An unroutable frame is deliberately not in it (#211) —
        # leaving it in would let it win coalescing from an older frame that *is* routable and then be
        # refused, and would let a News trigger be killed by an OI frame it merely attached. Rank and
        # the liquidity floor are here for a different reason: they say whether the fact may ground a
        # capital decision at all, so a set gated only for *triggering* would let a News verdict freeze
        # a case on the exact thin frame the floor exists to exclude.
        oi_context: list[OiTradeCandidate] = []
        # The subset the gate admitted as a reason to open a case *now*.
        oi_triggers: list[OiTradeCandidate] = []
        for row in state["oi_rows"]:
            oi_result = oi_candidate(row, funnel=funnel)
            if isinstance(oi_result, Rejected):
                self._record(
                    source_rejected(
                        oi_result,
                        source_key=oi_source_key(row.get("event_id"), row.get("metric_version")),
                        observed_at_ms=int(row.get("observed_at_ms") or row.get("verdict_created_at_ms") or 0),
                    ),
                    funnel=funnel,
                    gate=gate,
                )
                continue
            routing = admit_route(oi_result, config=self._gate_config)
            if routing is None and admit_context(oi_result, config=self._gate_config) is None:
                oi_context.append(oi_result)
            verdict = (
                admit_trigger(
                    oi_result,
                    now_ms=now,
                    config=self._gate_config,
                    blacklist=blacklist,
                    active_underlyings=state["active"],
                    underlyings_in_flight=state["cases_in_flight"],
                    cased_source_keys=state["cased_source_keys"],
                )
                or routing
            )
            if verdict is not None:
                self._record(verdict, funnel=funnel, gate=gate)
                continue
            oi_triggers.append(oi_result)

        news_all: list[NewsTradeCandidate] = []
        for row in state["news_rows"]:
            news_result = news_candidate(row, now_ms=now, blacklist=blacklist, policy=elig, funnel=funnel)
            if isinstance(news_result, NewsTradeCandidate):
                news_all.append(news_result)

        plans = plan_triggers(
            oi=oi_context,
            news=news_all,
            now_ms=now,
            policy=elig,
            oi_trigger_keys={signal.source_key for signal in oi_triggers},
            active_underlyings=state["active"],
            underlyings_in_flight=state["cases_in_flight"],
            cased_source_keys=state["cased_source_keys"],
            funnel=funnel,
        )
        # A trigger that is no plan's *trigger* lost this turn's coalescing — either to a newer frame for
        # the same issuer, or to a News verdict that won the group and attached it as context instead.
        # Both are the same fact about admission and both get the same row: it did not trigger, and it is
        # not retired, because nothing durable stops it and it wins the next scan as soon as whatever
        # beat it has produced its case. Keyed on the trigger alone rather than on the whole manifest —
        # a frame folded in as a counterpart is *used*, which is a different question from *triggered*,
        # and treating the two as one left it with no row at all for a turn.
        triggered = {plan.source_key for plan in plans}
        for signal in oi_triggers:
            if signal.source_key not in triggered:
                self._record(
                    defer(signal, stage="eligibility", reason="superseded_by_newer_trigger"),
                    funnel=funnel,
                    gate=gate,
                )
        # The News lane's own admission rule, and the last double gate in the system (#273). A News
        # trigger with no OI frame beside it used to freeze a case anyway, so that
        # `news_oi_alignment_v1` could refuse it with `oi_context_missing` — 64 of production's 76
        # cases, every one of them that answer and nothing else. The refusal is the same; where it is
        # recorded is what changes, and a case table whose rows are all the same non-event is not a
        # ledger anyone can read. `DEFERRED`, because an OI frame for the same issuer can still land
        # inside the trigger budget; the expiry sweep closes the row when none does.
        admitted: list[_Plan] = []
        for plan in plans:
            if plan.trigger_kind == "news" and plan.oi is None and plan.news is not None:
                self._record(
                    defer(plan.news, stage="eligibility", reason="oi_context_missing"),
                    funnel=funnel,
                    gate=gate,
                )
                continue
            admitted.append(plan)

        # The capacity decision runs *after* coalescing, not before it. It used to return early, and
        # the early return only recorded `oi_triggers` — so on a capped day every eligible News
        # candidate got neither a case nor a gate row and aged out of the scan horizon silently,
        # leaving a hole in the ledger exactly where an operator would go looking for one. Running it
        # here costs one pure reduction and gives every plan's trigger the same named answer.
        #
        # Order matters: a News trigger with no OI context is refused above on its own rule. Being
        # told the lane was full would be a different, and wrong, explanation.
        entries_today = int(state["entries_today"])
        capped = entries_today >= 1
        if capped:
            funnel.count("scan_skipped:daily_entry_fence")
        elif state["active"]:
            funnel.count("scan_skipped:active_intent")
            capped = True
        if capped:
            # The lane is full, not the source unusable. Every admitted frame still gets its row, or a
            # busy day would leave a hole in the one ledger that is supposed to explain the whole lane.
            for plan in admitted:
                self._defer_for_capacity(plan, funnel=funnel, gate=gate)
            return []
        return admitted

    # ------------------------------------------------------------------ freeze
    async def _freeze(
        self,
        plan: _Plan,
        *,
        funnel: Funnel,
        now: int,
        gate: dict[str, CandidateGateResult],
    ) -> bool:
        """Resolve the instrument, compute the regime, then insert one immutable case.

        Everything refused here is refused because a *manifest could not be frozen* — no listing, no
        candle, no mark at the cutoff. A valid but unfavourable regime is no longer among them (#264):
        the case is created and the strategy names the refusal, so the manifest records what was
        rejected instead of the frame disappearing before anything durable saw it.
        """

        # The plan's *trigger*, never whichever candidate happens to be present. A News-triggered plan
        # carries an OI frame as context, and filing this plan's refusals under that frame's source key
        # would overwrite the answer the gate already reached about it as a trigger of its own.
        source: OiTradeCandidate | NewsTradeCandidate | None = plan.oi if plan.trigger_kind == "oi" else plan.news

        def _gate(result: CandidateGateResult) -> None:
            self._record(result, funnel=funnel, gate=gate)

        seen = await self._db.read(
            "trading_case_seen",
            lambda repos: (
                repos.trading.has_source_key(primary_source_key=plan.source_key),
                repos.trading.last_intent_close_at_ms(underlying_key=underlying_key(plan.base_symbol)),
                self._instrument_projection(repos, plan.base_symbol, ("binance.perp", "hl.perp")),
            ),
            timeout_seconds=_COLD_READ_TIMEOUT_SECONDS,
        )
        already, last_close, instrument_rows = seen
        if already:
            funnel.count("freeze_reject:source_key_seen")
            if source is not None:
                _gate(reject(source, stage="eligibility", reason="already_consumed"))
            return False
        if last_close is not None and now - int(last_close) < self._config.eligibility.symbol_cooldown_ms:
            funnel.count("freeze_reject:symbol_cooldown")
            if source is not None:
                _gate(defer(source, stage="eligibility", reason="cooldown"))
            return False

        # Source-aligned routing (#211). An OI frame is a claim about *one venue's* open interest, so
        # the static operator priority must not be allowed to answer it: a Hyperliquid frame that
        # resolved to a Binance perp produced an order against a book whose OI did nothing of the kind.
        # `_plan` has already dropped every frame whose venue this lane cannot execute, so these two
        # refusals are unreachable from the scanner and exist because `_freeze` must not depend on a
        # caller having filtered for it.
        priority: Sequence[str] = self._config.venue_priority
        if plan.oi is not None:
            aligned = signal_exchange_id(plan.oi.venue)
            if aligned is None:
                funnel.count("freeze_reject:venue_unresolved")
                return False
            if aligned not in self._config.venue_priority:
                funnel.count("freeze_reject:venue_not_enabled")
                return False
            priority = (aligned,)
        instrument = resolve_instrument(instrument_rows, priority=priority, observed_at_ms=now)
        if instrument is None:
            # A Gate class of `crypto` is not a listing. `WMT` reaches here with a Binance perp whose
            # own catalogue class is `equity`, and this is where it stops. With a signal venue in hand
            # the same absence means something narrower and is counted separately: the issuer may well
            # be listed, just not on the venue whose open interest moved.
            funnel.count(
                "freeze_reject:no_perp_at_signal_venue" if plan.oi is not None else "freeze_reject:no_native_perp"
            )
            if source is not None:
                # Retryable: the universe snapshot refreshes, and an issuer can be listed at the venue
                # whose open interest moved. The expiry sweep closes the row when the frame goes stale.
                _gate(defer(source, stage="routing", reason="no_native_perp"))
            return False

        bars = await self._fetch_bars(instrument, anchor_at_ms=plan.observed_at_ms)
        if not bars:
            funnel.count("freeze_reject:no_price_fail_closed")
            if source is not None:
                _gate(defer(source, stage="market_context", reason="market_data_unavailable"))
            return False
        move = pre_move_bps(bars, anchor_at_ms=plan.observed_at_ms, policy=self._config.regime)
        regime = assess(
            oi_direction=plan.oi.oi_direction if plan.oi is not None else None,
            move=move,
            policy=self._config.regime,
        )
        funnel.count(f"regime:{regime.regime.value}")
        if regime.reason == "no_price_fail_closed":
            # Missing market data is the gate's, in both lanes: with no price there is no mark to size
            # from and no pre-move to freeze, so there is no manifest to write.
            funnel.count(f"freeze_reject:regime_{regime.reason}")
            if source is not None:
                _gate(defer(source, stage="market_context", reason="market_data_unavailable"))
            return False
        # The mark is the bar closed at or before the cutoff; a fresher close would leak future evidence.
        anchor_bar = select_bar(
            bars, target_ms=plan.observed_at_ms, gap_tolerance_ms=self._config.regime.bar_gap_tolerance_ms
        )
        if anchor_bar is None:
            funnel.count("freeze_reject:no_mark_at_cutoff")
            if source is not None:
                # Terminal: the gap is a property of this frame's own cutoff, so no later scan of the
                # same frame can find a candle that was never published.
                _gate(reject(source, stage="market_context", reason="market_data_invalid"))
            return False
        mark = anchor_bar.close
        strategy_id = capital_strategy_id(
            trigger_kind=plan.trigger_kind,
            has_oi=plan.oi is not None,
            has_news=plan.news is not None,
        )
        if strategy_id is None:
            funnel.count("freeze_reject:no_capital_strategy")
            if source is not None:
                _gate(reject(source, stage="freeze", reason="source_contract_invalid"))
            return False
        strategy = self._strategies[strategy_id]
        market_context = FrozenMarketContext(
            mark_price=mark,
            observed_at_ms=plan.observed_at_ms,
            pre_move_bps=move,
            pre_move_lookback_ms=self._config.regime.lookback_ms,
        )
        trigger = (
            OiMarketTrigger(
                source_key=plan.source_key,
                observed_at_ms=plan.source_observed_at_ms,
                persisted_at_ms=plan.trigger_persisted_at_ms,
                venue=str(plan.oi.venue),
            )
            if plan.trigger_kind == "oi" and plan.oi is not None
            else NewsMarketTrigger(
                source_key=plan.source_key,
                observed_at_ms=plan.source_observed_at_ms,
                persisted_at_ms=plan.trigger_persisted_at_ms,
            )
        )
        contexts = FrozenStrategyContext(
            # Strategy permission remains the frozen decision-plane vocabulary. Execution is always
            # Binance USD-M Demo and no longer depends on an operator-selected backend mode.
            mode="paper",
            oi=plan.oi,
            news=plan.news,
            regime=regime,
            market=market_context,
        )
        manifest = TradingCaseManifest(
            primary_trigger=trigger,
            contexts=contexts,
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.strategy_version,
            strategy_config=strategy.config_snapshot,
            strategy_config_digest=strategy.config_digest,
            underlying_key=underlying_key(plan.base_symbol),
            base_symbol=plan.base_symbol,
            cutoff_ms=plan.observed_at_ms,
            instrument=instrument,
        )

        digest = manifest.digest()
        case_id = uuid.uuid4().hex
        linked = case_created(source, case_id=case_id) if source is not None else None

        def _insert(repos: Any) -> bool:
            inserted = bool(
                repos.trading.insert_case(
                    case_id=case_id,
                    underlying_key=manifest.underlying_key,
                    trigger_kind=plan.trigger_kind,
                    strategy_id=strategy.strategy_id,
                    strategy_version=strategy.strategy_version,
                    strategy_config_digest=strategy.config_digest,
                    mode="paper",
                    primary_source_key=plan.source_key,
                    supplemental_source_keys=plan.supplemental,
                    manifest=manifest.model_dump(mode="json"),
                    manifest_sha256=digest,
                    regime=regime.regime.value,
                    observed_at_ms=plan.observed_at_ms,
                    source_observed_at_ms=plan.source_observed_at_ms,
                    trigger_persisted_at_ms=plan.trigger_persisted_at_ms,
                    now_ms=now,
                )
            )
            # Same transaction, deliberately. A case with no admission row, or an admission row naming
            # a case that was never written, is exactly the ambiguity the ledger exists to remove.
            if inserted and linked is not None:
                repos.trading.record_gate_decision(
                    source_key=linked.source_key,
                    gate_version=CANDIDATE_GATE_VERSION,
                    gate_config_digest=self._gate_config.digest,
                    trigger_kind=linked.trigger_kind,
                    underlying_key=linked.underlying_key,
                    source_observed_at_ms=linked.source_observed_at_ms,
                    status=linked.status,
                    stage=linked.stage,
                    reason=linked.reason,
                    retryable=linked.retryable,
                    evidence=linked.evidence,
                    case_id=case_id,
                    now_ms=now,
                )
            return inserted

        created = await self._db.tx("trading_case_insert", _insert, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        funnel.count("case_created" if created else "freeze_reject:trigger_identity_race")
        # Keyed on the *trigger*, not on `plan.oi` being present. A News-triggered plan carries the OI
        # frame as context, and treating that as the frame's own admission answer dropped the
        # `superseded_by_newer_trigger` row this turn had already decided for it — and counted a
        # `case_created` against a case it did not trigger.
        if source is not None and linked is not None:
            if created:
                # Already committed beside the case row; leaving it out of the turn's flush is what
                # keeps the flush from re-deciding a row that is now terminal.
                gate.pop(linked.source_key, None)
                funnel.count(linked.funnel_key)
            else:
                _gate(reject(source, stage="freeze", reason="already_consumed"))
        return bool(created)

    async def _fetch_bars(self, instrument: InstrumentRef, *, anchor_at_ms: int) -> list[Bar]:
        fetcher = self._bars(instrument.exchange_id)
        if fetcher is None:
            return []
        start = _cutoff_history_start_ms(
            anchor_at_ms=anchor_at_ms,
            lookback_ms=self._config.regime.lookback_ms,
        )
        try:
            bars = await observe_provider_call(
                self._telemetry,
                name="trading_candidate",
                source=external_data_source(instrument.exchange_id),
                call=fetcher(instrument.provider_symbol, start, anchor_at_ms + _BAR_INTERVAL_MS),
            )
        except Exception:
            log.warning(
                "trading bar fetch failed venue=%s symbol=%s",
                instrument.exchange_id,
                instrument.provider_symbol,
            )
            return []
        return sorted(bars, key=lambda bar: bar.close_at_ms)

    # ------------------------------------------------------------------ advance
    async def _advance(self, *, funnel: Funnel) -> str | None:
        now = self._clock()
        claimed = await self._db.tx(
            "trading_case_claim",
            lambda repos: repos.trading.claim_case(run_id=self._run_id, lease_ms=_CASE_LEASE_MS, now_ms=now),
            timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
        )
        if claimed is None:
            return None

        case_id = str(claimed["case_id"])
        raw_manifest = claimed.get("manifest")
        # #160 is a generation hard cut.  Do this check before Pydantic parsing because the old v1
        # manifest cannot satisfy v2's required upstream identities; letting validation raise would
        # strand the claimed row in RUNNING until every lease expired. Only undecided Cases reach
        # this path; emitted Intents are already outside CandidateRunner's ownership.
        if not _uses_current_news_generation(raw_manifest, news_generation=self._news_generation):
            funnel.count("advance_reject:news_generation_retired")
            await self._settle(
                case_id,
                CaseState.BLOCKED,
                "no_trade",
                "news_generation_retired",
            )
            return "news_generation_retired"
        try:
            manifest = TradingCaseManifest.model_validate(raw_manifest)
        except ValidationError:
            funnel.count("advance_reject:manifest_invalid")
            await self._settle(case_id, CaseState.BLOCKED, "no_trade", "manifest_invalid")
            return "manifest_invalid"
        trigger_kind: TriggerKind = str(claimed["trigger_kind"])  # type: ignore[assignment]
        strategy = strategy_from_manifest(manifest)
        if strategy is None or trigger_kind != manifest.trigger_kind:
            funnel.count("advance_reject:strategy_identity_retired")
            await self._settle(case_id, CaseState.BLOCKED, "no_trade", "strategy_identity_retired")
            return "strategy_identity_retired"

        # A case carries a mark frozen at its cutoff. `max_age_ms` gates *creation*; nothing gated how
        # long a created case could sit unclaimed, so a paused lane resumed hours later would size and
        # stop off a stale bar close and book the whole interim move as free PnL.
        #
        # Measured from `created_at_ms`, not from the trigger: eligibility has already spent the
        # trigger's budget, so reusing it here blocked a frame frozen at 280 s old the moment the
        # previous case's model call took twenty seconds — and `BLOCKED` plus a unique source key
        # means that signal can never produce another case.
        if now - int(claimed["created_at_ms"]) > _CASE_DECISION_TTL_MS:
            funnel.count("advance_reject:case_stale")
            await self._settle(case_id, CaseState.BLOCKED, "no_trade", "case_stale")
            return "case_stale"

        decision: TradeDecision | None = None
        program_version: str | None = None
        program_sha256: str | None = None
        program_output: dict[str, Any] | None = None

        outcome = strategy.evaluate(manifest.contexts)
        if manifest.strategy_id == "news_oi_alignment_v1" and outcome.rule == "model_absent":
            budget = await self._db.read(
                "trading_dspy_budget",
                lambda repos: repos.trading.dspy_calls_today(day_key=_day_key(now)),
                timeout_seconds=_COLD_READ_TIMEOUT_SECONDS,
            )
            if int(budget) >= self._config.max_dspy_cases_per_day:
                funnel.count("advance_reject:dspy_budget")
                await self._settle(case_id, CaseState.NO_TRADE, "no_trade", "dspy_budget_exhausted")
                return "budget"
            if self._program is None:
                funnel.count("advance_reject:program_unconfigured")
                await self._settle(case_id, CaseState.NO_TRADE, "no_trade", "program_unconfigured")
                return "unconfigured"
            await self._db.tx(
                "trading_dspy_budget_bump",
                lambda repos: repos.trading.bump_dspy_calls(day_key=_day_key(now), now_ms=now),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
            result = await observe_provider_call(
                self._telemetry,
                name="trading_candidate",
                source="model",
                call=self._program.decide(manifest),
            )
            decision = result.decision
            outcome = strategy.evaluate(manifest.contexts.model_copy(update={"news_decision": decision}))
            program_output = {
                "decision": decision.model_dump(mode="json"),
                "strategy_outcome": outcome.model_dump(mode="json"),
                "trace": result.trace,
            }
            if result.identity is not None:
                program_version = result.identity.version
                program_sha256 = result.identity.sha256
            funnel.count(f"model_decision:{decision.decision}")
        elif program_output is None:
            program_output = {"strategy_outcome": outcome.model_dump(mode="json")}
        funnel.count(f"strategy:{manifest.strategy_id}:{outcome.rule}")

        if outcome.decision == "no_trade":
            await self._settle(
                case_id,
                CaseState.POLICY_REJECTED,
                "no_trade",
                outcome.rule,
                program_version=program_version,
                program_sha256=program_sha256,
                program_output=program_output,
            )
            return outcome.rule

        if outcome.permission == "shadow":
            funnel.count("advance_reject:strategy_permission")
            await self._settle(
                case_id,
                CaseState.POLICY_REJECTED,
                "no_trade",
                "strategy_permission",
                program_version=program_version,
                program_sha256=program_sha256,
                program_output=program_output,
            )
            return "strategy_permission"

        if outcome.decision != "long":
            funnel.count("advance_reject:intent_side_not_allowed")
            await self._settle(
                case_id,
                CaseState.POLICY_REJECTED,
                "no_trade",
                "intent_side_not_allowed",
                program_version=program_version,
                program_sha256=program_sha256,
                program_output=program_output,
            )
            return "intent_side_not_allowed"
        if not capability_instrument_id(manifest.instrument):
            funnel.count("advance_reject:intent_instrument_not_allowed")
            await self._settle(
                case_id,
                CaseState.POLICY_REJECTED,
                "no_trade",
                "intent_instrument_not_allowed",
                program_version=program_version,
                program_sha256=program_sha256,
                program_output=program_output,
            )
            return "intent_instrument_not_allowed"

        emitted = await self._emit_intent(
            case_id=case_id,
            manifest=manifest,
            policy_reason=outcome.rule,
            program_version=program_version,
            program_sha256=program_sha256,
            program_output=program_output,
        )
        funnel.count("intent_emitted" if emitted else "advance_reject:intent_admission")
        if not emitted:
            await self._settle(
                case_id,
                CaseState.BLOCKED,
                "no_trade",
                "intent_admission_blocked",
                program_version=program_version,
                program_sha256=program_sha256,
                program_output=program_output,
            )
            return "intent_admission_blocked"
        return outcome.rule

    async def _settle(
        self,
        case_id: str,
        state: CaseState,
        policy_decision: str,
        policy_reason: str,
        *,
        program_version: str | None = None,
        program_sha256: str | None = None,
        program_output: dict[str, Any] | None = None,
    ) -> None:
        """Terminalise a claimed case at the instant it is actually decided.

        The clock is sampled here rather than inherited from the top of `_advance` (#211). A single
        sample made `decided_at_ms` equal to the moment the case was *claimed*, so the model call —
        the one expensive step in the turn, and the only one with a daily budget — measured as zero
        and fell inside no reported stage at all.
        """

        now = self._clock()
        await self._db.tx(
            "trading_case_settle",
            lambda repos: repos.trading.settle_case(
                case_id=case_id,
                run_id=self._run_id,
                state=state.value,
                policy_decision=policy_decision,
                policy_reason=policy_reason,
                program_version=program_version,
                program_sha256=program_sha256,
                program_output=program_output,
                now_ms=now,
            ),
            timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
        )

    async def _emit_intent(
        self,
        *,
        case_id: str,
        manifest: TradingCaseManifest,
        policy_reason: str,
        program_version: str | None,
        program_sha256: str | None,
        program_output: dict[str, Any] | None,
    ) -> bool:
        now = self._clock()

        def _insert(repos: Any) -> bool:
            instrument_id = capability_instrument_id(manifest.instrument)
            evidence = repos.trading.intent_admission_evidence(
                instrument_id=instrument_id,
                underlying_key=manifest.underlying_key,
                now_ms=now,
            )
            if evidence is None:
                return False
            snapshot, blacklist = evidence
            if not is_executable_instrument(manifest.instrument, snapshot):
                return False
            intent = TradeIntent.create(
                case_id=case_id,
                case_manifest_sha256=manifest.digest(),
                execution_capability_snapshot_sha256=snapshot.snapshot_sha256,
                blacklist_snapshot=blacklist,
                instrument_id=instrument_id,
                underlying_key=manifest.underlying_key,
                created_at_ms=now,
                reference_price=manifest.mark_price,
                target_notional_usd=self._config.fixed_notional_usd,
            )
            if not repos.trading.insert_intent(intent):
                return False
            settled = repos.trading.settle_case(
                case_id=case_id,
                run_id=self._run_id,
                state=CaseState.INTENT_EMITTED.value,
                policy_decision="long",
                policy_reason=policy_reason,
                program_version=program_version,
                program_sha256=program_sha256,
                program_output=program_output,
                now_ms=now,
            )
            if not settled:
                # The caller owns the transaction; raising here rolls the Intent insert back too.
                raise RuntimeError("trading_intent_case_transition_failed")
            return True

        try:
            return bool(
                await self._db.tx(
                    "trading_intent_emit",
                    _insert,
                    timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
                )
            )
        except Exception:
            log.info("trading intent emission rejected case_id=%s", case_id)
            return False


def scan_horizon_ms(policy: EligibilityPolicy) -> int:
    """How far back one scan must read so that every legally attachable row is visible.

    A trigger may be up to `max_age_ms` old, and it may attach a counterpart up to that counterpart's
    own lookback before *its* cutoff — so the oldest row that can legally enter a manifest frozen now
    is `max_age + max(lookback)` old. Reading only `max_age x overlap` made the configured 60 m / 30 m
    windows unreachable at the query, before fusion ever got a chance to honour them. The recovery
    overlap stays as the third floor: after a crash or a paused lane the scan has to re-see triggers it
    never turned into cases, and `primary_source_key` is what makes re-seeing them free.
    """

    return max(
        policy.max_age_ms + policy.news_lookback_ms,
        policy.max_age_ms + policy.oi_lookback_ms,
        policy.max_age_ms * _SCAN_OVERLAP_FACTOR,
    )


__all__ = [
    "CandidateRunner",
    "scan_horizon_ms",
]
