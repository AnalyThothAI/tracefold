"""Candidate fusion, decision, and the one-attempt entry path."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from ..candidate.blacklist import Blacklist
from ..candidate.eligibility import (
    Funnel,
    _uses_current_news_generation,
    blacklist_rule,
    news_candidate,
    oi_candidate,
)
from ..candidate.fusion import _fuse, _Plan
from ..candidate.routing import resolve_instrument
from ..contracts import (
    Bar,
    CaseKind,
    CaseState,
    InstrumentRef,
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
# so re-reading twice that window each turn is what makes a crash, a redeploy or a paused runner
# self-healing; `primary_source_key` rejects whatever the previous turn already turned into a case.
_SCAN_OVERLAP_FACTOR = 3
_CASE_LEASE_MS = 60_000
_MAX_CASES_PER_TURN = 4
# How long a frozen case may wait to be decided. Its own budget, separate from the freshness the
# candidate rules already spent, so queueing behind another case's model call cannot discard a signal.
_CASE_DECISION_TTL_MS = 300_000
# What still stops a News-only case even though it has no quadrant: no price to enter at, and the
# measured chasing bucket above the pre-move ceiling.
_NEWS_ONLY_BLOCKING_REASONS = frozenset({"no_price_fail_closed", "move_above_band_chasing"})


class CandidateRunner:
    """Scan, freeze, decide, prepare, commit. One case at a time; provider concurrency is one."""

    def __init__(
        self,
        *,
        db: TradingDatabasePort,
        config: TradingConfig,
        bars: BarFetcherFactory,
        adapter: Any,
        candidate_projection: _CandidateProjectionReader,
        instrument_projection: _InstrumentProjectionReader,
        program: TradingDecisionProgram | None = None,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        self._db = db
        self._config = config
        self._bars = bars
        self._adapter = adapter
        self._candidate_projection = candidate_projection
        self._instrument_projection = instrument_projection
        self._program = program
        self._clock = clock
        self._run_id = uuid.uuid4().hex

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.turn()
            except Exception:
                log.exception("trading candidate turn failed")
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
        window = self._config.eligibility.max_age_ms * _SCAN_OVERLAP_FACTOR
        elig = self._config.eligibility

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

        active: set[str] = state["active"]
        orders_today = int(state["orders_today"])
        if orders_today >= self._config.order.max_orders_per_day:
            funnel.count("scan_skipped:daily_order_cap")
            return []
        if len(active) >= self._config.order.max_open_underlyings:
            funnel.count("scan_skipped:max_open_underlyings")
            return []

        # One pass per underlying: two separate loops made `attach_oi` unreachable whenever OI existed,
        # leaving `oi_lookback_seconds` dead and the News loop able to emit only `news_only`.
        newest_oi: dict[str, OiTradeCandidate] = {}
        for candidate in oi_all:
            key = underlying_key(candidate.base_symbol)
            current = newest_oi.get(key)
            if current is None or candidate.observed_at_ms > current.observed_at_ms:
                newest_oi[key] = candidate
        newest_news: dict[str, NewsTradeCandidate] = {}
        for item in news_all:
            key = underlying_key(item.base_symbol)
            seen = newest_news.get(key)
            if seen is None or item.verdict_created_at_ms > seen.verdict_created_at_ms:
                newest_news[key] = item

        plans: dict[str, _Plan] = {}
        for key in sorted(set(newest_oi) | set(newest_news)):
            if key in active:
                funnel.count("plan_reject:active_underlying")
                continue
            plan = _fuse(newest_oi.get(key), newest_news.get(key), policy=elig)
            if plan is not None:
                plans[key] = plan
        for plan in plans.values():
            funnel.count(f"plan_kind:{plan.kind}")
        return sorted(plans.values(), key=lambda item: item.observed_at_ms, reverse=True)

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

        instrument = resolve_instrument(instrument_rows, priority=self._config.venue_priority, observed_at_ms=now)
        if instrument is None:
            # A Gate class of `crypto` is not a listing. `WMT` reaches here with a Binance perp whose
            # own catalogue class is `equity`, and this is where it stops.
            funnel.count("freeze_reject:no_native_perp")
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
                    now_ms=now,
                )
            )

        created = await self._db.tx("trading_case_insert", _insert, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        funnel.count("case_created" if created else "freeze_reject:source_key_race")
        return bool(created)

    async def _fetch_bars(self, instrument: InstrumentRef, *, anchor_at_ms: int) -> list[Bar]:
        fetcher = self._bars(instrument.exchange_id)
        if fetcher is None:
            return []
        start = anchor_at_ms - self._config.regime.lookback_ms - _BAR_INTERVAL_MS
        try:
            bars = await fetcher(instrument.provider_symbol, start, anchor_at_ms + _BAR_INTERVAL_MS)
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
                now=now,
            )
            return "news_generation_retired"
        try:
            manifest = TradingCaseManifest.model_validate(raw_manifest)
        except ValidationError:
            funnel.count("advance_reject:manifest_invalid")
            await self._settle(case_id, CaseState.BLOCKED, "no_trade", "manifest_invalid", now=now)
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
            await self._settle(case_id, CaseState.BLOCKED, "no_trade", "case_stale", now=now)
            return "case_stale"

        # #21: the mode frozen into the case, not today's configuration. An operator who edits
        # `mode` while cases are pending must not have a manifest frozen under paper submitted live.
        case_mode: TradingMode = str(claimed["mode"])  # type: ignore[assignment]
        if case_mode != self._config.mode:
            funnel.count("advance_reject:mode_changed")
            await self._settle(case_id, CaseState.BLOCKED, "no_trade", "mode_changed_since_freeze", now=now)
            return "mode_changed"

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
            await self._settle(case_id, CaseState.POLICY_REJECTED, "no_trade", early.rule, now=now)
            return early.rule

        if kind in ("news_only", "news_oi"):
            budget = await self._db.read(
                "trading_dspy_budget",
                lambda repos: repos.trading.dspy_calls_today(day_key=_day_key(now)),
                timeout_seconds=_COLD_READ_TIMEOUT_SECONDS,
            )
            if int(budget) >= self._config.max_dspy_cases_per_day:
                funnel.count("advance_reject:dspy_budget")
                await self._settle(case_id, CaseState.NO_TRADE, "no_trade", "dspy_budget_exhausted", now=now)
                return "budget"
            if self._program is None:
                funnel.count("advance_reject:program_unconfigured")
                await self._settle(case_id, CaseState.NO_TRADE, "no_trade", "program_unconfigured", now=now)
                return "unconfigured"
            await self._db.tx(
                "trading_dspy_budget_bump",
                lambda repos: repos.trading.bump_dspy_calls(day_key=_day_key(now), now_ms=now),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
            result = await self._program.decide(manifest)
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
                now=now,
            )
            return outcome.rule

        placed = await self._place(
            case_id=case_id,
            manifest=manifest,
            decision_side=outcome.decision,
            mode=case_mode,
            funnel=funnel,
            now=now,
        )
        await self._settle(
            case_id,
            CaseState.ORDER_PREPARED if placed else CaseState.BLOCKED,
            outcome.decision,
            outcome.rule if placed else "order_blocked",
            program_version=program_version,
            program_sha256=program_sha256,
            program_output=program_output,
            now=now,
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
        now: int,
    ) -> None:
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

        market = MarketContext(
            instrument=manifest.instrument,
            mark_price=manifest.mark_price,
            observed_at_ms=manifest.cutoff_ms,
            pre_move_bps=manifest.pre_move_bps,
            pre_move_lookback_ms=self._config.regime.lookback_ms,
            spread_bps=None,
            spread_available=False,
        )
        sized = size_order(side=side, market=market, mode=mode, policy=self._config.order)
        if isinstance(sized, RiskRejection):
            funnel.count(f"risk_reject:{sized.rule}")
            return False

        order_id = uuid.uuid4().hex
        payload = build_payload(
            instrument_exchange_id=manifest.instrument.exchange_id,
            provider_symbol=manifest.instrument.provider_symbol,
            side=side,
            quantity=sized.quantity,
            stop_price=sized.stop_price,
            take_profit_price=sized.take_profit_price,
            hedged=False,
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
            return bool(
                repos.trading.insert_prepared_order(
                    order_id=order_id,
                    case_id=case_id,
                    underlying_key=manifest.underlying_key,
                    # The venue is part of the frozen intent, so the row records the one that was
                    # chosen even in paper. `mode` is what says whether the write was real; storing
                    # "paper" here would erase which venue the case actually routed to.
                    exchange_id=manifest.instrument.exchange_id,
                    provider_symbol=manifest.instrument.provider_symbol,
                    account_ref=self._config.account_ref,
                    mode=mode,
                    side=side,
                    notional_usd=str(sized.notional_usd),
                    quantity=str(sized.quantity),
                    entry_reference=str(sized.entry_reference),
                    stop_price=str(sized.stop_price),
                    take_profit_price=None if sized.take_profit_price is None else str(sized.take_profit_price),
                    payload=payload,
                    payload_sha256=canonical_sha256(payload),
                    state=state.value,
                    must_close_at_ms=None,
                    now_ms=now,
                )
            )

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
            instrument=manifest.instrument,
            mode=mode,
            side=side,
            notional_usd=sized.notional_usd,
            quantity=sized.quantity,
            entry_reference=sized.entry_reference,
            stop_price=sized.stop_price,
            take_profit_price=sized.take_profit_price,
            must_close_after_ms=self._config.order.max_holding_ms,
            payload=payload,
        )
        return await commit_order(
            db=self._db,
            adapter=self._adapter,
            order=prepared,
            policy=self._config.order,
            count=funnel.count,
            now=now,
        )


__all__ = [
    "CandidateRunner",
]
