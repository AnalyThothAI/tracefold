"""Candidate fusion, decision, and the one-attempt entry path."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import ValidationError

from ..candidate.blacklist import Blacklist
from ..candidate.eligibility import (
    EligibilityPolicy,
    Funnel,
    _uses_current_news_generation,
    blacklist_rule,
    news_candidate,
    oi_candidate,
)
from ..candidate.fusion import _Plan, plan_triggers
from ..candidate.routing import resolve_instrument, signal_exchange_id
from ..contracts import (
    TRADING_LIVE_PREFLIGHT_MAX_AGE_MS,
    Bar,
    CaseKind,
    CaseState,
    ExecutionAdapter,
    InstrumentRef,
    LiveExecutionAdapter,
    MarketContext,
    NewsTradeCandidate,
    OiRegime,
    OiTradeCandidate,
    OrderState,
    PreparedOrder,
    RiskRejection,
    TradeDecision,
    TradingCaseManifest,
    TradingMode,
    canonical_sha256,
    underlying_key,
)
from ..contracts import (
    utc_day_key as _day_key,
)
from ..decision.policy import decide, pre_model_reject, side_to_order_side
from ..decision.program import TradingDecisionProgram
from ..decision.regime import assess, pre_move_bps, select_bar
from ..execution.order import build_payload, size_order
from ..execution.submission import commit_order
from ..storage.root import TradingRepository
from ..telemetry import (
    TradingExternalDataTelemetryPort,
    TradingWorkSemantics,
    external_data_source,
    observe_provider_call,
)
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
_LIVE_PREFLIGHT_MAX_AGE_MS = TRADING_LIVE_PREFLIGHT_MAX_AGE_MS
# What still stops a News-only case even though it has no quadrant: no price to enter at, and the
# measured chasing bucket above the pre-move ceiling.
_NEWS_ONLY_BLOCKING_REASONS = frozenset({"no_price_fail_closed", "move_above_band_chasing"})


class CandidateRunner:
    """Scan, freeze, decide, prepare, commit. One case at a time; provider concurrency is one."""

    work_semantics: ClassVar[tuple[TradingWorkSemantics, ...]] = ("derived_work", "capital_truth")

    def __init__(
        self,
        *,
        db: TradingDatabasePort,
        config: TradingConfig,
        bars: BarFetcherFactory,
        adapter: ExecutionAdapter,
        candidate_projection: _CandidateProjectionReader,
        instrument_projection: _InstrumentProjectionReader,
        program: TradingDecisionProgram | None = None,
        clock: Callable[[], int] = _now_ms,
        telemetry: TradingExternalDataTelemetryPort | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._bars = bars
        self._adapter = adapter
        self._live_adapter = adapter if isinstance(adapter, LiveExecutionAdapter) else None
        self._candidate_projection = candidate_projection
        self._instrument_projection = instrument_projection
        self._program = program
        self._clock = clock
        self._telemetry = telemetry
        self._run_id = uuid.uuid4().hex
        self._live_startup_complete = False

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
        control = str(state["control"])
        if control in ("PAUSED", "CLOSE_ONLY"):
            # Reconciliation and safety closes keep running; only new exposure stops.
            funnel.count(f"scan_skipped_control:{control.lower()}")
            await self._merge_funnel(funnel, now)
            return {"control": control, "funnel": funnel.as_dict()}
        if self._config.mode != "paper" and not await self._ensure_live_startup(funnel=funnel):
            await self._merge_funnel(funnel, now)
            return {"skipped": "live_startup_not_ready", "funnel": funnel.as_dict()}

        plans = self._plan(state, funnel=funnel, now=now)
        created = 0
        for plan in plans[:_MAX_CASES_PER_TURN]:
            if await self._freeze(plan, funnel=funnel, now=now):
                created += 1

        decided = 0
        for _ in range(_MAX_CASES_PER_TURN):
            outcome = await self._advance(funnel=funnel)
            if outcome is None:
                break
            decided += 1

        await self._merge_funnel(funnel, now)
        return {"created": created, "decided": decided, "funnel": funnel.as_dict()}

    async def _ensure_live_startup(self, *, funnel: Funnel) -> bool:
        if self._live_startup_complete:
            return True
        live_adapter = self._live_adapter
        if live_adapter is None:
            funnel.count("startup_reject:read_capability_unavailable")
            return False
        # The canary enables exactly one venue, and source-aligned routing (#211) drops every frame
        # tagged at a venue the operator has not enabled — so the venue proved flat here is the only
        # venue a live case can execute at. Enabling a second venue for a live mode would break that
        # correspondence, which is why `live_reviewed` requires the single-venue configuration.
        venue = self._config.venue_priority[0]
        live_symbol = self._config.live_symbol or ""
        instrument = InstrumentRef(
            exchange_id=venue,
            venue="binance.perp" if venue == "binance" else "hl.perp",
            provider_symbol=live_symbol,
            base_symbol=live_symbol,
            instrument_class="crypto",
            # Startup asks metadata to resolve the unique active swap. Presuming USDT here would
            # reject a valid non-USDT venue before the provider could return exact instrument truth.
            quote_asset=None,
            observed_at_ms=self._clock(),
        )
        try:
            inventory = await observe_provider_call(
                self._telemetry,
                name="trading_candidate",
                source="other",
                call=live_adapter.startup(instrument=instrument, account_ref=self._config.account_ref),
            )
        except Exception:
            log.warning("trading live startup reconciliation failed")
            funnel.count("startup_reject:provider_unavailable")
            return False
        if inventory.exposures:
            # The initial canary allows one account-wide position. Unknown/manual exposure is never
            # adopted or auto-closed, so any remote inventory makes readiness explicitly false.
            funnel.count("startup_reject:external_exposure")
            return False
        if inventory.preflight.observed_account_ref != self._config.account_ref:
            funnel.count("startup_reject:account_identity_unproven")
            return False
        self._live_startup_complete = True
        funnel.count("startup_ready")
        return True

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
            runtime = trading.runtime_state() or {"control": "RUNNING", "day_key": "", "orders_today": 0}
            try:
                blacklist = Blacklist.from_rows(trading.blacklist_rows())
            except Exception:
                log.exception("trading blacklist read failed")
                blacklist = Blacklist.unavailable()
            orders_today = trading.orders_today(day_key=_day_key(now))
            dspy_calls_today = trading.dspy_calls_today(day_key=_day_key(now))
            active = set(trading.active_underlyings())
            in_flight = set(trading.underlyings_in_flight())
            cased = set(trading.source_keys_since(observed_at_ms=now - window))
            oi_rows, news_rows = self._candidate_projection(
                repos,
                self._config.oi_metric_version,
                now - window,
                now,
                elig.max_rank_in_window,
                elig.min_oi_value_usd,
            )
            return {
                "control": runtime.get("control", "RUNNING"),
                "orders_today": orders_today,
                "dspy_calls_today": dspy_calls_today,
                "blacklist": blacklist,
                "active": active,
                "cases_in_flight": in_flight,
                "cased_source_keys": cased,
                "oi_rows": oi_rows,
                "news_rows": news_rows,
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
    def _plan(self, state: dict[str, Any], *, funnel: Funnel, now: int) -> list[_Plan]:
        """Every eligible row this turn read, reduced to at most one plan per underlying.

        Eligibility answers "may this row be used at all"; it no longer answers "is it new enough to
        start something", because those are two windows with two budgets. `plan_triggers` owns the
        second one and owns the point-in-time attachment, so this method is a read boundary and two
        count caps — nothing about time lives here.
        """

        elig = self._config.eligibility
        blacklist: Blacklist = state["blacklist"]
        funnel.count("oi_rows", len(state["oi_rows"]))
        funnel.count("news_rows", len(state["news_rows"]))

        oi_all: list[OiTradeCandidate] = []
        for row in state["oi_rows"]:
            oi_result = oi_candidate(row, now_ms=now, blacklist=blacklist, policy=elig, funnel=funnel)
            if isinstance(oi_result, OiTradeCandidate):
                oi_all.append(oi_result)
        news_all: list[NewsTradeCandidate] = []
        for row in state["news_rows"]:
            news_result = news_candidate(row, now_ms=now, blacklist=blacklist, policy=elig, funnel=funnel)
            if isinstance(news_result, NewsTradeCandidate):
                news_all.append(news_result)

        orders_today = int(state["orders_today"])
        if orders_today >= self._config.order.max_orders_per_day:
            funnel.count("scan_skipped:daily_order_cap")
            return []
        if len(state["active"]) >= self._config.order.max_open_underlyings:
            funnel.count("scan_skipped:max_open_underlyings")
            return []

        # Routability is decided here, before fusion, and not at the freeze (#211). An OI frame whose
        # venue tag names nothing this lane may execute is neither a reason to act nor usable context:
        # leaving it in would let it win coalescing from an older frame that *is* routable and then be
        # refused, and would let a News trigger be killed by an OI frame it merely attached. Measured
        # on live data, every pushed frame in the last week carried a tag, so this is a guard rather
        # than a filter with volume behind it.
        routable_oi: list[OiTradeCandidate] = []
        for signal in oi_all:
            exchange = signal_exchange_id(signal.venue)
            if exchange is None:
                funnel.count("oi_reject:venue_unresolved")
            elif exchange not in self._config.venue_priority:
                funnel.count("oi_reject:venue_not_enabled")
            else:
                routable_oi.append(signal)

        news_context = news_all
        if int(state["dspy_calls_today"]) >= self._config.max_dspy_cases_per_day:
            # No News-bearing case can be decided today. A case is terminal and its source key is
            # unique, so freezing one anyway would spend a frame's only chance to become a case on an
            # answer nobody can buy — and an OI frame that would have traded on arithmetic alone goes
            # with it. Widening the News lookback (#211) is what made that trade-off common enough to
            # matter: an hour of headlines now reclassifies far more frames as `news_oi`.
            funnel.count("scan_skipped:dspy_budget_exhausted", len(news_all))
            news_context = []

        return plan_triggers(
            oi=routable_oi,
            news=news_context,
            now_ms=now,
            policy=elig,
            active_underlyings=state["active"],
            underlyings_in_flight=state["cases_in_flight"],
            cased_source_keys=state["cased_source_keys"],
            funnel=funnel,
        )

    # ------------------------------------------------------------------ freeze
    async def _freeze(self, plan: _Plan, *, funnel: Funnel, now: int) -> bool:
        """Resolve the instrument, compute the regime, then insert one immutable case."""

        seen = await self._db.read(
            "trading_case_seen",
            lambda repos: (
                repos.trading.has_source_key(primary_source_key=plan.source_key),
                repos.trading.last_close_at_ms(underlying_key=underlying_key(plan.base_symbol)),
                self._instrument_projection(repos, plan.base_symbol, ("binance.perp", "hl.perp")),
            ),
            timeout_seconds=_COLD_READ_TIMEOUT_SECONDS,
        )
        already, last_close, instrument_rows = seen
        if already:
            funnel.count("freeze_reject:source_key_seen")
            return False
        if last_close is not None and now - int(last_close) < self._config.eligibility.symbol_cooldown_ms:
            funnel.count("freeze_reject:symbol_cooldown")
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
            return False

        bars = await self._fetch_bars(instrument, anchor_at_ms=plan.observed_at_ms)
        if not bars:
            funnel.count("freeze_reject:no_price_fail_closed")
            return False
        move = pre_move_bps(bars, anchor_at_ms=plan.observed_at_ms, policy=self._config.regime)
        regime = assess(
            oi_direction=plan.oi.oi_direction if plan.oi is not None else None,
            move=move,
            policy=self._config.regime,
        )
        funnel.count(f"regime:{regime.regime.value}")
        if plan.kind != "news_only" and regime.regime is OiRegime.UNCLEAR:
            funnel.count(f"freeze_reject:regime_{regime.reason}")
            return False
        if plan.kind == "news_only" and regime.reason in _NEWS_ONLY_BLOCKING_REASONS:
            # News-only has no OI quadrant, but still needs a cutoff price and obeys the measured
            # chasing ceiling.
            funnel.count(f"freeze_reject:regime_{regime.reason}")
            return False

        # The mark is the bar closed at or before the cutoff; a fresher close would leak future evidence.
        anchor_bar = select_bar(
            bars, target_ms=plan.observed_at_ms, gap_tolerance_ms=self._config.regime.bar_gap_tolerance_ms
        )
        if anchor_bar is None:
            funnel.count("freeze_reject:no_mark_at_cutoff")
            return False
        mark = anchor_bar.close
        manifest = TradingCaseManifest(
            case_kind=plan.kind,
            underlying_key=underlying_key(plan.base_symbol),
            base_symbol=plan.base_symbol,
            cutoff_ms=plan.observed_at_ms,
            oi=plan.oi,
            news=plan.news,
            regime=regime,
            instrument=instrument,
            mark_price=mark,
            pre_move_bps=move,
        )
        blocked = pre_model_reject(
            case_kind=plan.kind,
            mode=self._config.mode,
            regime=regime.regime,
            whale_long_profit_bps=plan.oi.whale_long_profit_bps if plan.oi is not None else None,
            oi_value_usd=plan.oi.oi_value_usd if plan.oi is not None else None,
            policy=self._config.trade,
        )
        if blocked is not None:
            # Cheapest possible refusal: no case row, no claim, no model budget. `decide()` re-applies
            # every one of these, so this is an ordering optimisation and never the correctness gate.
            funnel.count(f"freeze_reject:policy_{blocked.rule}")
            return False

        digest = manifest.digest()

        def _insert(repos: Any) -> bool:
            return bool(
                repos.trading.insert_case(
                    case_id=uuid.uuid4().hex,
                    underlying_key=manifest.underlying_key,
                    case_kind=plan.kind,
                    mode=self._config.mode,
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

        created = await self._db.tx("trading_case_insert", _insert, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        funnel.count("case_created" if created else "freeze_reject:trigger_identity_race")
        return bool(created)

    async def _fetch_bars(self, instrument: InstrumentRef, *, anchor_at_ms: int) -> list[Bar]:
        fetcher = self._bars(instrument.exchange_id)
        if fetcher is None:
            return []
        start = anchor_at_ms - self._config.regime.lookback_ms - _BAR_INTERVAL_MS
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
        # strand the claimed row in RUNNING until every lease expired.  Only undecided cases reach
        # this path. Prepared orders are reconciled from their immutable payload and remain untouched.
        if not _uses_current_news_generation(raw_manifest):
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
        kind: CaseKind = str(claimed["case_kind"])  # type: ignore[assignment]

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

        # #21: the mode frozen into the case, not today's configuration. An operator who edits
        # `mode` while cases are pending must not have a manifest frozen under paper submitted live.
        case_mode: TradingMode = str(claimed["mode"])  # type: ignore[assignment]
        if case_mode != self._config.mode:
            funnel.count("advance_reject:mode_changed")
            await self._settle(case_id, CaseState.BLOCKED, "no_trade", "mode_changed_since_freeze")
            return "mode_changed"
        if case_mode != "paper" and manifest.instrument.base_symbol != self._config.live_symbol:
            funnel.count("advance_reject:live_symbol_not_allowed")
            await self._settle(
                case_id,
                CaseState.BLOCKED,
                "no_trade",
                "live_symbol_not_allowed",
            )
            return "live_symbol_not_allowed"

        decision: TradeDecision | None = None
        program_version: str | None = None
        program_sha256: str | None = None
        program_output: dict[str, Any] | None = None

        frozen_regime = OiRegime(str(claimed["regime"] or OiRegime.UNCLEAR.value))
        early = pre_model_reject(
            case_kind=kind,
            mode=case_mode,
            regime=frozen_regime,
            whale_long_profit_bps=manifest.oi.whale_long_profit_bps if manifest.oi is not None else None,
            oi_value_usd=manifest.oi.oi_value_usd if manifest.oi is not None else None,
            policy=self._config.trade,
        )
        if early is not None:
            funnel.count(f"policy:{early.rule}")
            await self._settle(case_id, CaseState.POLICY_REJECTED, "no_trade", early.rule)
            return early.rule

        if kind in ("news_only", "news_oi"):
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
            program_output = {"decision": decision.model_dump(mode="json"), "trace": result.trace}
            if result.identity is not None:
                program_version = result.identity.version
                program_sha256 = result.identity.sha256
            funnel.count(f"model_decision:{decision.decision}")

        outcome = decide(
            case_kind=kind,
            mode=case_mode,
            regime=frozen_regime,
            decision=decision,
            whale_long_profit_bps=manifest.oi.whale_long_profit_bps if manifest.oi is not None else None,
            oi_value_usd=manifest.oi.oi_value_usd if manifest.oi is not None else None,
            policy=self._config.trade,
        )
        funnel.count(f"policy:{outcome.rule}")

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

        # A fresh sample, not the claim instant: `trading_orders.created_at_ms` is the reported
        # `order_prepared` stage and the model call sits between the two (#211).
        placed = await self._place(
            case_id=case_id,
            manifest=manifest,
            decision_side=outcome.decision,
            mode=case_mode,
            funnel=funnel,
            now=self._clock(),
        )
        await self._settle(
            case_id,
            CaseState.ORDER_PREPARED if placed else CaseState.BLOCKED,
            outcome.decision,
            outcome.rule if placed else "order_blocked",
            program_version=program_version,
            program_sha256=program_sha256,
            program_output=program_output,
        )
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

    # ------------------------------------------------------------------ place
    async def _place(
        self,
        *,
        case_id: str,
        manifest: TradingCaseManifest,
        decision_side: str,
        mode: TradingMode,
        funnel: Funnel,
        now: int,
    ) -> bool:
        side = side_to_order_side(decision_side)
        if side is None:
            return False

        preflight_audit: dict[str, Any] | None = None
        execution_contract_sha256: str | None = None
        price_tick: Decimal | None = None
        hedged = False
        instrument = manifest.instrument
        if mode == "paper":
            market = MarketContext(
                instrument=instrument,
                mark_price=manifest.mark_price,
                observed_at_ms=manifest.cutoff_ms,
                pre_move_bps=manifest.pre_move_bps,
                pre_move_lookback_ms=self._config.regime.lookback_ms,
                spread_bps=None,
                spread_available=False,
            )
        else:
            if instrument.base_symbol != self._config.live_symbol:
                funnel.count("risk_reject:live_symbol_not_allowed")
                return False
            live_adapter = self._live_adapter
            if live_adapter is None:
                funnel.count("risk_reject:live_read_capability_unavailable")
                return False
            try:
                preflight = await observe_provider_call(
                    self._telemetry,
                    name="trading_candidate",
                    source=external_data_source(instrument.exchange_id),
                    call=live_adapter.preflight(instrument=instrument, account_ref=self._config.account_ref),
                )
            except Exception:
                log.warning("trading live preflight failed")
                funnel.count("risk_reject:live_preflight_failed")
                return False
            if not preflight.venue_healthy:
                funnel.count("risk_reject:venue_unhealthy")
                return False
            preflight_now = self._clock()
            if preflight.observed_at_ms > preflight_now + _LIVE_PREFLIGHT_MAX_AGE_MS or (
                preflight_now - preflight.observed_at_ms > _LIVE_PREFLIGHT_MAX_AGE_MS
            ):
                funnel.count("risk_reject:live_preflight_stale")
                return False
            if (
                preflight.requested_account_ref != self._config.account_ref
                or preflight.observed_account_ref != self._config.account_ref
            ):
                funnel.count("risk_reject:account_mismatch")
                return False
            if preflight.positions or preflight.open_orders:
                funnel.count("risk_reject:remote_exposure")
                return False
            if preflight.leverage != 1:
                funnel.count("risk_reject:leverage_not_one")
                return False
            if not preflight.margin_mode:
                funnel.count("risk_reject:margin_mode_unknown")
                return False
            if preflight.available_balance is None:
                funnel.count("risk_reject:balance_unknown")
                return False
            if preflight.available_balance < self._config.order.fixed_notional_usd:
                funnel.count("risk_reject:balance_insufficient")
                return False
            if preflight.price_tick is None:
                funnel.count("risk_reject:price_tick_unknown")
                return False
            instrument = preflight.instrument
            hedged = preflight.hedged
            execution_contract_sha256 = preflight.execution_contract_sha256
            price_tick = preflight.price_tick
            preflight_audit = preflight.audit_payload()
            market = MarketContext(
                instrument=instrument,
                mark_price=preflight.mark_price,
                observed_at_ms=preflight.observed_at_ms,
                pre_move_bps=manifest.pre_move_bps,
                pre_move_lookback_ms=self._config.regime.lookback_ms,
                spread_bps=preflight.spread_bps,
                spread_available=True,
                quantity_step=preflight.quantity_step,
                price_tick=preflight.price_tick,
                min_quantity=preflight.min_quantity,
                min_notional=preflight.min_notional,
                contract_size=preflight.contract_size,
            )
        sized = size_order(side=side, market=market, mode=mode, policy=self._config.order)
        if isinstance(sized, RiskRejection):
            funnel.count(f"risk_reject:{sized.rule}")
            return False

        order_id = uuid.uuid4().hex
        payload = build_payload(
            instrument_exchange_id=instrument.exchange_id,
            provider_symbol=instrument.provider_symbol,
            side=side,
            quantity=sized.quantity,
            stop_price=sized.stop_price,
            take_profit_price=sized.take_profit_price,
            hedged=hedged,
            execution_contract_sha256=execution_contract_sha256,
            price_tick=price_tick,
        )
        state = OrderState.AWAITING_APPROVAL if mode == "live_reviewed" else OrderState.PREPARED

        def _insert(repos: Any) -> bool:
            repos.trading.lock_account(self._config.account_ref)
            # The deny-list is re-read here, not only at the top of the turn. `trading blacklist add`
            # landing a second after a case was frozen used to have no effect on that in-flight case:
            # the only per-symbol operator lever could not stop work already planned.
            try:
                deny = Blacklist.from_rows(repos.trading.blacklist_rows())
            except Exception:
                log.exception("trading blacklist re-read failed")
                deny = Blacklist.unavailable()
            blocked = deny.blocked(manifest.base_symbol, now_ms=now)
            if blocked is not None:
                funnel.count(f"risk_reject:{blacklist_rule(blocked.reason)}")
                return False
            # Re-check the two count caps *here*, under the account lock and inside the transaction
            # that inserts. `_plan` reads them once at the top of a turn, which bounds how many cases
            # are created but not how many orders a single turn places: four cases claimed in one turn
            # could each place an order past a daily cap of one. The partial unique index only
            # enforces one order per underlying, so a count cap has to be counted where it is spent.
            if repos.trading.orders_today(day_key=_day_key(now)) >= self._config.order.max_orders_per_day:
                funnel.count("risk_reject:daily_order_cap")
                return False
            if len(repos.trading.active_underlyings()) >= self._config.order.max_open_underlyings:
                funnel.count("risk_reject:max_open_underlyings")
                return False
            inserted = bool(
                repos.trading.insert_prepared_order(
                    order_id=order_id,
                    case_id=case_id,
                    underlying_key=manifest.underlying_key,
                    # The venue is part of the frozen intent, so the row records the one that was
                    # chosen even in paper. `mode` is what says whether the write was real; storing
                    # "paper" here would erase which venue the case actually routed to.
                    exchange_id=instrument.exchange_id,
                    provider_symbol=instrument.provider_symbol,
                    account_ref=self._config.account_ref,
                    mode=mode,
                    side=side,
                    notional_usd=str(sized.notional_usd),
                    quantity=str(sized.quantity),
                    entry_reference=str(sized.entry_reference),
                    stop_price=str(sized.stop_price),
                    take_profit_price=None if sized.take_profit_price is None else str(sized.take_profit_price),
                    # #209: the two exits that are not prices are frozen here too, so a later config
                    # edit or redeploy changes the next order and never this one.
                    max_holding_ms=self._config.order.max_holding_ms,
                    taker_fee_bps=self._config.order.taker_fee_bps,
                    payload=payload,
                    payload_sha256=canonical_sha256(payload),
                    state=state.value,
                    must_close_at_ms=None,
                    now_ms=now,
                )
            )
            if inserted and preflight_audit is not None:
                repos.trading.record_observation(
                    order_id=order_id,
                    observation_kind="live_preflight",
                    content_sha256=canonical_sha256(preflight_audit),
                    content=preflight_audit,
                    now_ms=now,
                )
            return inserted

        try:
            inserted = await self._db.tx("trading_order_prepare", _insert, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        except Exception:
            log.info("trading order prepare rejected underlying=%s", manifest.underlying_key)
            funnel.count("risk_reject:active_underlying_index")
            return False
        if not inserted:
            funnel.count("risk_reject:case_already_ordered")
            return False

        if state is OrderState.AWAITING_APPROVAL:
            funnel.count("order_awaiting_approval")
            return True

        prepared = PreparedOrder(
            order_id=order_id,
            case_id=case_id,
            underlying_key=manifest.underlying_key,
            account_ref=self._config.account_ref,
            instrument=instrument,
            mode=mode,
            side=side,
            notional_usd=sized.notional_usd,
            quantity=sized.quantity,
            entry_reference=sized.entry_reference,
            stop_price=sized.stop_price,
            take_profit_price=sized.take_profit_price,
            max_holding_ms=self._config.order.max_holding_ms,
            taker_fee_bps=self._config.order.taker_fee_bps,
            payload=payload,
        )
        return await commit_order(
            db=self._db,
            adapter=self._adapter,
            order=prepared,
            count=funnel.count,
            now=now,
            observe_call=lambda call: observe_provider_call(
                self._telemetry,
                name="trading_candidate",
                source=external_data_source(prepared.instrument.exchange_id),
                call=call,
            ),
        )


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
