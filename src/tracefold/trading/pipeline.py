"""The two Trading runners: one scans and decides, one reconciles. No queue, no second deployable.

Both are bounded polling loops in the existing Workers root. They read their own work from PostgreSQL,
call public venue REST with no database connection held, and write short transactions through the same
one-slot **cold** admission the price loops use — the four News lane slots belong to the Deduper,
Triage and the Deliverer, and a trading backlog must never compete for them.

Failure is local by construction. A venue that times out, a model that will not answer, a blacklist
read that fails: each is a named no-trade for that turn. None of them can affect News ingestion,
judgment, delivery, readiness or shutdown.

Ordering inside a turn is not cosmetic. Blacklist, class, listing, cooldown and the deterministic
regime all run *before* any model or account work, so the cheap deny paths never spend a provider call
— which is what keeps ~77 telemetry frames a day from becoming 77 model calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import ValidationError

from .blacklist import Blacklist
from .candidates import (
    EligibilityPolicy,
    Funnel,
    attach_news,
    attach_oi,
    blacklist_rule,
    news_candidate,
    oi_candidate,
    resolve_instrument,
)
from .decision_program import TradingDecisionProgram
from .models import (
    TRADING_MANIFEST_VERSION,
    Bar,
    CaseKind,
    CaseState,
    ExchangeId,
    ExecutionReceipt,
    InstrumentRef,
    MarketContext,
    NewsTradeCandidate,
    OiRegime,
    OiTradeCandidate,
    OrderSide,
    OrderState,
    PreparedOrder,
    RiskRejection,
    TradeDecision,
    TradingCaseManifest,
    TradingMode,
    canonical_sha256,
    underlying_key,
)
from .order import (
    DEFAULT_ORDER_POLICY,
    OrderPolicy,
    build_payload,
    must_close_at,
    next_state_for,
    realized_bps,
    size_order,
)
from .paper import PaperAdapter, evaluate_paper_exit
from .policy import DEFAULT_TRADE_POLICY, TradePolicy, decide, pre_model_reject, side_to_order_side
from .regime import DEFAULT_REGIME_POLICY, RegimePolicy, assess, pre_move_bps, select_bar
from .repository import TradingRepository

log = logging.getLogger("tracefold.trading")

# A bounded overlap instead of a cursor. Everything inside `max_age_ms` is still fresh enough to trade,
# so re-reading twice that window each turn is what makes a crash, a redeploy or a paused runner
# self-healing; `primary_source_key` rejects whatever the previous turn already turned into a case.
_SCAN_OVERLAP_FACTOR = 3
_COLD_READ_TIMEOUT_SECONDS = 10.0
_COLD_WRITE_TIMEOUT_SECONDS = 10.0
_CASE_LEASE_MS = 60_000
_MAX_CASES_PER_TURN = 4
_RECONCILE_PERIOD_SECONDS = 30.0
_RECONCILE_BATCH = 32
_RECONCILE_BACKOFF_MS = 30_000
# A prepared intent that never reached the network is provably harmless after this, and terminalising
# it is the only way a crash between the insert and the attempt claim gives back its underlying slot.
_PREPARED_TTL_MS = 900_000
# How long a frozen case may wait to be decided. Its own budget, separate from the freshness the
# candidate rules already spent, so queueing behind another case's model call cannot discard a signal.
_CASE_DECISION_TTL_MS = 300_000
# What still stops a News-only case even though it has no quadrant: no price to enter at, and the
# measured chasing bucket above the pre-move ceiling.
_NEWS_ONLY_BLOCKING_REASONS = frozenset({"no_price_fail_closed", "move_above_band_chasing"})
# Five-minute bars: the interval the venue adapters already fetch for the News reaction plane. Coarse
# for a 30-minute holding window, and deliberately so — asking for a finer feed would mean a second
# request cadence and a tick history the project does not have.
_BAR_INTERVAL_MS = 300_000

BarFetcher = Callable[[str, int, int], Awaitable[Sequence[Bar]]]
BarFetcherFactory = Callable[[str], BarFetcher | None]


def _uses_current_news_generation(raw: object) -> bool:
    """Whether an untrusted persisted manifest names the one executable News generation."""

    if not isinstance(raw, Mapping) or raw.get("manifest_version") != TRADING_MANIFEST_VERSION:
        return False
    sources = (("oi", "news_oi_signal_v1"), ("news", "news_semantic_program_v4"))
    found = False
    for source_field, expected_program in sources:
        source = raw.get(source_field)
        if source is None:
            continue
        found = True
        if (
            not isinstance(source, Mapping)
            or source.get("learning_epoch") != "program_v6"
            or source.get("policy_version") != "news_triage_policy_v10"
            or source.get("program_version") != expected_program
        ):
            return False
    return found


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _day_key(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, float(seconds)))


@dataclass(frozen=True, slots=True)
class TradingConfig:
    """Everything a runner needs that is not a collaborator. One object so a turn is reproducible."""

    mode: TradingMode = "paper"
    account_ref: str = "default"
    poll_seconds: float = 2.0
    oi_metric_version: str = "oi_signal_v1"
    venue_priority: tuple[str, ...] = ("binance", "hyperliquid")
    eligibility: EligibilityPolicy = field(default_factory=EligibilityPolicy)
    regime: RegimePolicy = field(default_factory=lambda: DEFAULT_REGIME_POLICY)
    trade: TradePolicy = field(default_factory=lambda: DEFAULT_TRADE_POLICY)
    order: OrderPolicy = field(default_factory=lambda: DEFAULT_ORDER_POLICY)
    max_dspy_cases_per_day: int = 12


@dataclass(frozen=True, slots=True)
class _Plan:
    """One underlying's worth of work, already reduced to at most one candidate."""

    kind: CaseKind
    base_symbol: str
    observed_at_ms: int
    oi: OiTradeCandidate | None
    news: NewsTradeCandidate | None
    source_key: str
    supplemental: tuple[str, ...]


def _fuse(
    oi: OiTradeCandidate | None,
    news: NewsTradeCandidate | None,
    *,
    policy: EligibilityPolicy,
) -> _Plan | None:
    """Reduce one underlying's newest OI frame and newest News verdict to at most one plan.

    Whichever fired later is the primary, and the other is attached only when it falls inside its own
    lookback and **at or before** the primary's trigger. Point-in-time in both directions: attaching a
    counterpart from after the cutoff would put the future into a frozen manifest.
    """

    if news is None:
        if oi is None:  # pragma: no cover - the caller only passes keys that have at least one side
            return None
        return _Plan(
            kind="oi_only",
            base_symbol=oi.base_symbol,
            observed_at_ms=oi.observed_at_ms,
            oi=oi,
            news=None,
            source_key=oi.source_key,
            supplemental=(),
        )
    if oi is None:
        return _Plan(
            kind="news_only",
            base_symbol=news.base_symbol,
            observed_at_ms=news.verdict_created_at_ms,
            oi=None,
            news=news,
            source_key=news.source_key,
            supplemental=(),
        )

    if news.verdict_created_at_ms <= oi.observed_at_ms:
        attached = attach_news(oi, [news], policy=policy)
        return _Plan(
            kind="news_oi" if attached is not None else "oi_only",
            base_symbol=oi.base_symbol,
            observed_at_ms=oi.observed_at_ms,
            oi=oi,
            news=attached,
            source_key=oi.source_key,
            supplemental=(attached.source_key,) if attached is not None else (),
        )
    attached_oi = attach_oi(news, [oi], policy=policy)
    return _Plan(
        kind="news_oi" if attached_oi is not None else "news_only",
        base_symbol=news.base_symbol,
        observed_at_ms=news.verdict_created_at_ms,
        oi=attached_oi,
        news=news,
        source_key=news.source_key,
        supplemental=(attached_oi.source_key,) if attached_oi is not None else (),
    )


class CandidateRunner:
    """Scan, freeze, decide, prepare, commit. One case at a time; provider concurrency is one."""

    def __init__(
        self,
        *,
        db: Any,
        config: TradingConfig,
        bars: BarFetcherFactory,
        adapter: Any,
        program: TradingDecisionProgram | None = None,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        self._db = db
        self._config = config
        self._bars = bars
        self._adapter = adapter
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
            return {
                "control": runtime.get("control", "RUNNING"),
                "orders_today": trading.orders_today(day_key=_day_key(now)),
                "dspy_calls_today": trading.dspy_calls_today(day_key=_day_key(now)),
                "blacklist": blacklist,
                "active": set(trading.active_underlyings()),
                "oi_rows": repos.news.trade_candidate_oi_rows(
                    metric_version=self._config.oi_metric_version,
                    after_created_at_ms=now - window,
                    until_created_at_ms=now,
                    max_rank_in_window=elig.max_rank_in_window,
                    min_oi_value_usd=elig.min_oi_value_usd,
                ),
                "news_rows": repos.news.trade_candidate_news_rows(
                    after_created_at_ms=now - window,
                    until_created_at_ms=now,
                ),
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

        # One pass per underlying, not two loops over two lists. The two-loop shape had a fatal
        # property: the News loop skipped any underlying the OI loop had already planned, so
        # `attach_oi` could only ever be reached for an underlying with no OI candidate — which is
        # exactly when it must return None. `oi_lookback_seconds` was dead configuration and the News
        # loop could emit nothing but `news_only`.
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
                repos.news.trade_candidate_instrument(base_symbol=plan.base_symbol, venues=("binance.perp", "hl.perp")),
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
            # A News-only case has no OI frame and therefore no quadrant — rejecting it on UNCLEAR is
            # what made the kind structurally unreachable. What it does *not* lose is the measured
            # ceiling: a symbol that has already run 12% in the hour is the loss bucket whatever fired
            # the case, and it still needs a price at the cutoff to have an entry at all.
            funnel.count(f"freeze_reject:regime_{regime.reason}")
            return False

        # The mark is the bar closed at or before the cutoff, not the newest bar fetched. The manifest
        # is a point-in-time document stamped `cutoff_ms`; taking the freshest close would put a price
        # from after the cutoff into the frozen evidence the model reads and the entry references.
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
            funnel=funnel,
            now=now,
        )


async def _one_attempt(
    *,
    db: Any,
    order: PreparedOrder,
    kind: Literal["entry", "exit"],
    call: Callable[[], Awaitable[ExecutionReceipt]],
    now: int,
) -> tuple[ExecutionReceipt | None, str]:
    """The durable one-attempt contract, shared by the entry and the exit.

    OpenTrade publishes no client idempotency key, so nothing downstream can deduplicate a resend.
    The protection is entirely local and has three parts, and **both** capital writes need all three:

    1. the ledger records the attempt and the transaction **commits before** the network call, so a
       crash between them leaves evidence that an attempt may exist;
    2. the claim is conditional, so a second caller finds zero rows updated and must not call;
    3. any exception terminalises as `AMBIGUOUS`, whose only legal successor is a read.

    The exit used to have none of them: `adapter.close()` was called bare, so an exception left the row
    untouched and the same close was re-issued every thirty seconds forever.
    """

    claim = await db.tx(
        "trading_attempt_claim",
        lambda repos: repos.trading.claim_attempt(order_id=order.order_id, kind=kind, now_ms=now),
        timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
    )
    if claim != "claimed":
        log.info("trading %s attempt refused order=%s reason=%s", kind, order.order_id, claim)
        return None, claim

    try:
        return await call(), "claimed"
    except Exception as exc:
        reason = f"provider_exception:{type(exc).__name__}"
        log.warning("trading %s attempt did not answer: %s", kind, type(exc).__name__)
        await db.tx(
            "trading_attempt_ambiguous",
            lambda repos: repos.trading.update_order(
                order_id=order.order_id,
                state=OrderState.AMBIGUOUS.value,
                state_reason=f"{kind}_{reason}",
                next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                now_ms=now,
            ),
            timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
        )
        return None, "ambiguous"


async def commit_order(
    *,
    db: Any,
    adapter: Any,
    order: PreparedOrder,
    policy: OrderPolicy,
    funnel: Funnel | None = None,
    now: int,
) -> bool:
    """Durable intent, then exactly one provider attempt, then whatever the answer turns out to be."""

    receipt, claim = await _one_attempt(
        db=db,
        order=order,
        kind="entry",
        call=lambda: adapter.submit(order),
        now=now,
    )
    if receipt is None:
        if claim != "ambiguous":
            # The attempt was already spent by an earlier caller, who already counted it. Counting it
            # again would charge the day twice for one order.
            if funnel is not None:
                funnel.count("commit_reject:already_attempted")
            return False
        # It raised and is now recorded AMBIGUOUS. That does count: an order that may exist at the
        # venue is exposure.
        await db.tx(
            "trading_order_day_count",
            lambda repos: repos.trading.bump_orders_today(day_key=_day_key(now), now_ms=now),
            timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
        )
        if funnel is not None:
            funnel.count("order_ambiguous")
        return True

    state = next_state_for(receipt)
    opened = now if state is OrderState.ACKNOWLEDGED else None
    deadline = must_close_at(opened_at_ms=now, policy=policy) if opened is not None else None

    def _apply(repos: Any) -> None:
        repos.trading.update_order(
            order_id=order.order_id,
            state=state.value,
            state_reason=receipt.reason,
            remote_order_id=receipt.remote_order_id,
            filled_quantity=None if receipt.filled_quantity is None else str(receipt.filled_quantity),
            average_price=None if receipt.average_price is None else str(receipt.average_price),
            position_opened_at_ms=opened,
            must_close_at_ms=deadline,
            next_reconcile_at_ms=now,
            closed_at_ms=now if state is OrderState.REJECTED else None,
            now_ms=now,
        )
        repos.trading.record_observation(
            order_id=order.order_id,
            observation_kind="submit",
            content_sha256=canonical_sha256(receipt.model_dump(mode="json")),
            content=receipt.model_dump(mode="json"),
            now_ms=now,
        )
        if state is not OrderState.REJECTED:
            repos.trading.bump_orders_today(day_key=_day_key(now), now_ms=now)

    await db.tx("trading_order_receipt", _apply, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
    if funnel is not None:
        funnel.count(f"order_{state.value.lower()}")
    return state is not OrderState.REJECTED


class ReconcileRunner:
    """Read-only truth-seeking plus the two deterministic exits: the protective stop and the clock."""

    def __init__(
        self,
        *,
        db: Any,
        config: TradingConfig,
        bars: BarFetcherFactory,
        adapter: Any,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        self._db = db
        self._config = config
        self._bars = bars
        self._adapter = adapter
        self._clock = clock

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.turn()
            except Exception:
                log.exception("trading reconcile turn failed")
            await _sleep_or_stop(stop_event, _RECONCILE_PERIOD_SECONDS)

    async def turn(self) -> dict[str, Any]:
        now = self._clock()
        scan = await self._db.read(
            "trading_reconcile_scan",
            lambda repos: (
                repos.trading.due_orders(now_ms=now, limit=_RECONCILE_BATCH),
                (repos.trading.runtime_state() or {}).get("control", "RUNNING"),
            ),
            timeout_seconds=_COLD_READ_TIMEOUT_SECONDS,
        )
        due, control = scan
        resolved = 0
        for row in due:
            try:
                if await self._reconcile(row, control=str(control), now=now):
                    resolved += 1
            except Exception:
                log.exception("trading reconcile failed order=%s", row.get("order_id"))
        return {"due": len(due), "control": control, "resolved": resolved}

    async def _reconcile(self, row: dict[str, Any], *, control: str, now: int) -> bool:
        order_id = str(row["order_id"])
        state = str(row["state"])
        order = _order_from_row(row, max_holding_ms=self._config.order.max_holding_ms)

        if state in (OrderState.AMBIGUOUS.value, OrderState.RECONCILING.value):
            return await self._resolve_ambiguity(order, row, now=now)

        if state == OrderState.AWAITING_APPROVAL.value:
            await self._defer(order_id, state, now)
            return False

        if state == OrderState.APPROVED.value:
            if control in ("PAUSED", "CLOSE_ONLY"):
                # Both block *new* exposure and an approved-but-unsubmitted order is new exposure.
                # The reconciler reading the control state at all is the fix: before this, no control
                # setting reached it, so `close-only` and `paused` were indistinguishable and neither
                # could stop an approved entry from being submitted.
                await self._defer(order_id, state, now)
                return False
            # The one place an approved order becomes a provider write. The caps are re-checked here
            # because `22eabf66` moved them into `_place._insert` — "count it where it is spent" — and
            # an AWAITING_APPROVAL order is inserted long before it is spent. Without this, ten orders
            # approved over several days all submit in one turn regardless of `max_orders_per_day`.
            over_cap = await self._db.read(
                "trading_approved_caps",
                lambda repos: (
                    repos.trading.orders_today(day_key=_day_key(now)) >= self._config.order.max_orders_per_day,
                    len(repos.trading.active_underlyings()) > self._config.order.max_open_underlyings,
                ),
                timeout_seconds=_COLD_READ_TIMEOUT_SECONDS,
            )
            if any(over_cap):
                await self._defer(order_id, state, now)
                return False
            await commit_order(db=self._db, adapter=self._adapter, order=order, policy=self._config.order, now=now)
            return True

        if state == OrderState.SAFETY_CLOSING.value:
            # The exit's analogue of the `SUBMITTING` orphan. Without it a crash between the attempt
            # claim and the receipt write left a row the catch-all re-deferred forever, holding the
            # slot with an exit that could never be re-claimed.
            await self._db.tx(
                "trading_reconcile_exit_orphan",
                lambda repos: repos.trading.update_order(
                    order_id=order_id,
                    state=OrderState.AMBIGUOUS.value,
                    state_reason="exit_safety_closing_after_restart",
                    next_reconcile_at_ms=now,
                    now_ms=now,
                ),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
            return True

        if state == OrderState.SUBMITTING.value:
            # A `SUBMITTING` row that survived a restart is not a pending call — it is an attempt
            # whose answer was lost. Never resent, and never rerouted to the other venue.
            await self._db.tx(
                "trading_reconcile_orphan",
                lambda repos: repos.trading.update_order(
                    order_id=order_id,
                    state=OrderState.AMBIGUOUS.value,
                    state_reason="entry_submitting_after_restart",
                    next_reconcile_at_ms=now,
                    now_ms=now,
                ),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
            return True

        if state == OrderState.PREPARED.value:
            # `provider_attempt_count = 0` proves no network call ever left, so an expired prepared
            # intent is provably harmless to terminalise. Without this a crash between the insert and
            # the attempt claim held one of `max_open_underlyings` forever with no CLI able to clear it.
            if int(row.get("provider_attempt_count") or 0) == 0 and now - int(row["created_at_ms"]) > _PREPARED_TTL_MS:
                await self._db.tx(
                    "trading_reconcile_expire_prepared",
                    lambda repos: repos.trading.update_order(
                        order_id=order_id,
                        state=OrderState.REJECTED.value,
                        state_reason="prepared_expired_never_submitted",
                        closed_at_ms=now,
                        next_reconcile_at_ms=None,
                        now_ms=now,
                    ),
                    timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
                )
                return True
            await self._defer(order_id, state, now)
            return False

        if state in (OrderState.ACKNOWLEDGED.value, OrderState.OPEN.value, OrderState.PARTIAL.value):
            return await self._manage_open(order, row, now=now)

        await self._defer(order_id, state, now)
        return False

    async def _defer(self, order_id: str, state: str, now: int) -> None:
        await self._db.tx(
            "trading_reconcile_defer",
            lambda repos: repos.trading.reschedule_order(
                order_id=order_id,
                expected_state=state,
                next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                now_ms=now,
            ),
            timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
        )

    async def _resolve_ambiguity(self, order: PreparedOrder, row: dict[str, Any], *, now: int) -> bool:
        """Read, never resend. A read that cannot prove either answer escalates to a human."""

        reason = str(row.get("state_reason") or "")
        exiting = reason.startswith("exit_") or reason == "close_ambiguous"

        observe = getattr(self._adapter, "observe", None)
        if observe is None:
            await self._escalate(order.order_id, "no_read_capability", now)
            return True
        try:
            observation = await observe(order)
        except Exception:
            # The row is still AMBIGUOUS; `reschedule_order` matches on the state it actually has, so
            # passing an aspirational one silently updated nothing and defeated the backoff.
            await self._defer(order.order_id, str(row["state"]), now)
            return False

        if observation is None:
            if exiting:
                # An ambiguous *exit* that observes no position is the close having landed, not the
                # entry never having existed. Reading it as `proven_absent` recorded a completed round
                # trip as though nothing happened and discarded its realised PnL.
                await self._escalate(order.order_id, "exit_ambiguous_position_absent", now)
                return True
            # Proven absent by a successful read: the intent never landed, so the underlying is free.
            await self._db.tx(
                "trading_reconcile_absent",
                lambda repos: repos.trading.update_order(
                    order_id=order.order_id,
                    state=OrderState.REJECTED.value,
                    state_reason="proven_absent",
                    closed_at_ms=now,
                    next_reconcile_at_ms=None,
                    now_ms=now,
                ),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
            return True

        if observation.state != "ACKNOWLEDGED":
            # The venue answered, but not with a live order. Adopting any non-None observation as OPEN
            # would book a position that the provider is telling us does not exist.
            await self._escalate(order.order_id, f"observed_{observation.state.lower()}", now)
            return True

        # The position existed at least as early as the attempt. Using `now` would silently extend the
        # holding deadline by however long reconciliation took to prove it.
        opened = int(row.get("position_opened_at_ms") or row.get("created_at_ms") or now)

        def _adopt(repos: Any) -> None:
            if exiting:
                # The close did not take effect — the venue still shows the position — so re-issuing it
                # cannot double-close. This release is the only thing that makes an ambiguous exit
                # recoverable; without it the position was unclosable and the row hot-looped forever.
                repos.trading.release_exit_attempt(order_id=order.order_id, now_ms=now)
            repos.trading.record_observation(
                order_id=order.order_id,
                observation_kind="reconcile",
                content_sha256=canonical_sha256(observation.model_dump(mode="json")),
                content=observation.model_dump(mode="json"),
                now_ms=now,
            )
            repos.trading.update_order(
                order_id=order.order_id,
                state=OrderState.OPEN.value,
                state_reason="resolved_by_read",
                remote_order_id=observation.remote_order_id,
                filled_quantity=None if observation.filled_quantity is None else str(observation.filled_quantity),
                average_price=None if observation.average_price is None else str(observation.average_price),
                position_opened_at_ms=opened,
                must_close_at_ms=must_close_at(opened_at_ms=opened, policy=self._config.order),
                next_reconcile_at_ms=now,
                now_ms=now,
            )

        await self._db.tx("trading_reconcile_adopt", _adopt, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        return True

    async def _escalate(self, order_id: str, reason: str, now: int) -> None:
        await self._db.tx(
            "trading_reconcile_escalate",
            lambda repos: repos.trading.update_order(
                order_id=order_id,
                state=OrderState.MANUAL_REVIEW_REQUIRED.value,
                state_reason=reason,
                next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                now_ms=now,
            ),
            timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
        )

    async def _manage_open(self, order: PreparedOrder, row: dict[str, Any], *, now: int) -> bool:
        order_id = str(row["order_id"])
        if str(row["mode"]) != "paper":
            # A live position's fill, stop and close are provider facts. Simulating them from candles
            # would be a local opinion about an account this lane cannot yet read (PR-C).
            await self._defer(order_id, str(row["state"]), now)
            return False

        opened = int(row.get("position_opened_at_ms") or now)
        deadline = int(row.get("must_close_at_ms") or must_close_at(opened_at_ms=opened, policy=self._config.order))
        fetcher = self._bars(str(row["exchange_id"]))
        bars: Sequence[Bar] = ()
        if fetcher is not None:
            try:
                bars = await fetcher(str(row["provider_symbol"]), opened, now + _BAR_INTERVAL_MS)
            except Exception:
                log.warning("trading reconcile bar fetch failed order=%s", order_id)
                bars = ()

        current_state = str(row["state"])
        if current_state == OrderState.ACKNOWLEDGED.value:
            # `_defer` used to double as this transition and stopped doing so when it became guarded.
            # Both states route here, so nothing broke mechanically — but `trading status` showed live
            # positions as ACKNOWLEDGED and the documented machine no longer matched the code.
            await self._db.tx(
                "trading_reconcile_open",
                lambda repos: repos.trading.promote_acknowledged(order_id=order_id, now_ms=now),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
            # The batch snapshot is now behind the row. Every later guarded write in this call has to
            # use the state the row actually holds, or it silently matches nothing — the same shape
            # that made the guarded `_defer` a no-op in the first place.
            current_state = OrderState.OPEN.value

        exit_at = evaluate_paper_exit(
            side=cast(OrderSide, row["side"]),
            entry=Decimal(str(row["entry_reference"])),
            stop_price=Decimal(str(row["stop_price"])),
            take_profit_price=None if row.get("take_profit_price") is None else Decimal(str(row["take_profit_price"])),
            opened_at_ms=opened,
            must_close_at_ms=deadline,
            bars=bars,
            now_ms=now,
        )
        if exit_at is None:
            if now >= deadline:
                # The clock says close and the feed cannot price it. Deferring forever would hold the
                # active-underlying slot with no alarm, so the deadline escalates instead of waiting.
                await self._escalate(order_id, "max_holding_without_price", now)
                return True
            await self._defer(order_id, current_state, now)
            return False

        receipt, claim = await _one_attempt(
            db=self._db,
            order=order,
            kind="exit",
            call=lambda: self._adapter.close(order, quantity=order.quantity),
            now=now,
        )
        if receipt is None:
            if claim == "exhausted":
                # The bounded retry is spent. Escalate rather than returning silently: an unwritten
                # row stays due and re-enters this path every turn. The reason comes from the claim
                # itself, not from the batch snapshot — that copy of `exit_attempt_total` predates the
                # increment the claim just tested, so it was always one behind the value it guarded.
                await self._escalate(order_id, "exit_attempts_exhausted", now)
            elif claim in ("already_spent", "wrong_state"):
                await self._defer(order_id, current_state, now)
            return True
        if receipt.state == "AMBIGUOUS":
            await self._db.tx(
                "trading_close_ambiguous",
                lambda repos: repos.trading.update_order(
                    order_id=order_id,
                    state=OrderState.AMBIGUOUS.value,
                    state_reason="exit_close_ambiguous",
                    next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                    now_ms=now,
                ),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
            return True
        if receipt.state == "REJECTED":
            # The venue refused the close, so the position is still open. Writing CLOSED here booked a
            # fabricated PnL and freed the underlying for a second order on top of a live position.
            await self._escalate(order_id, f"exit_rejected:{receipt.reason}", now)
            return True

        bps = realized_bps(
            side=cast(OrderSide, row["side"]),
            entry=Decimal(str(row["entry_reference"])),
            exit_price=exit_at.exit_price,
            fee_bps=self._config.order.taker_fee_bps,
        )

        def _close(repos: Any) -> None:
            repos.trading.record_observation(
                order_id=order_id,
                observation_kind="close",
                content_sha256=canonical_sha256(receipt.model_dump(mode="json")),
                content=receipt.model_dump(mode="json"),
                now_ms=now,
            )
            repos.trading.update_order(
                order_id=order_id,
                state=OrderState.CLOSED.value,
                state_reason=exit_at.reason,
                exit_price=str(exit_at.exit_price),
                exit_reason=exit_at.reason,
                realized_bps=bps,
                # Two timestamps on purpose: `position_closed_at_ms` is a real exit and is what the
                # cooldown and the realised-PnL denominator read; `closed_at_ms` only says the row is
                # terminal, and four different paths write it.
                position_closed_at_ms=exit_at.exit_at_ms,
                closed_at_ms=exit_at.exit_at_ms,
                next_reconcile_at_ms=None,
                now_ms=now,
            )

        await self._db.tx("trading_close", _close, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        return True


def _order_from_row(row: dict[str, Any], *, max_holding_ms: int) -> PreparedOrder:
    return PreparedOrder(
        order_id=str(row["order_id"]),
        case_id=str(row["case_id"]),
        underlying_key=str(row["underlying_key"]),
        instrument=InstrumentRef(
            exchange_id=cast(ExchangeId, row["exchange_id"]),
            venue=str(row["exchange_id"]),
            provider_symbol=str(row["provider_symbol"]),
            base_symbol=str(row["underlying_key"]).removeprefix("crypto:"),
            instrument_class="crypto",
            observed_at_ms=int(row["created_at_ms"]),
        ),
        mode=cast(TradingMode, row["mode"]),
        side=cast(OrderSide, row["side"]),
        notional_usd=Decimal(str(row["notional_usd"])),
        quantity=Decimal(str(row["quantity"])),
        entry_reference=Decimal(str(row["entry_reference"])),
        stop_price=Decimal(str(row["stop_price"])),
        take_profit_price=None if row.get("take_profit_price") is None else Decimal(str(row["take_profit_price"])),
        must_close_after_ms=max_holding_ms,
        payload=dict(row.get("payload") or {}),
    )


@dataclass(slots=True)
class TradingPipeline:
    """Exactly two runners in the existing Workers root. No queue, no second process."""

    candidate: CandidateRunner
    reconcile: ReconcileRunner

    def runners(self) -> list[tuple[str, Callable[[asyncio.Event], Any]]]:
        return [
            ("trading-candidate", lambda stop: self.candidate.run(stop_event=stop)),
            ("trading-reconcile", lambda stop: self.reconcile.run(stop_event=stop)),
        ]

    async def close(self) -> None:
        return None


def build_pipeline(
    *,
    db: Any,
    config: TradingConfig,
    bars: BarFetcherFactory,
    program: TradingDecisionProgram | None = None,
    adapter: Any | None = None,
) -> TradingPipeline:
    """Compose the two runners. A live mode without a real adapter refuses to start."""

    if adapter is None:
        if config.mode != "paper":
            raise ValueError("trading_live_mode_requires_execution_adapter")
        adapter = PaperAdapter()
    return TradingPipeline(
        candidate=CandidateRunner(db=db, config=config, bars=bars, adapter=adapter, program=program),
        reconcile=ReconcileRunner(db=db, config=config, bars=bars, adapter=adapter),
    )


__all__ = [
    "BarFetcher",
    "BarFetcherFactory",
    "CandidateRunner",
    "ReconcileRunner",
    "TradingConfig",
    "TradingPipeline",
    "build_pipeline",
    "commit_order",
]
