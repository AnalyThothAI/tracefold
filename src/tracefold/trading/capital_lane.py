"""The Decision lane: one Binance OI Source to independent Policy and Capital facts (#350).

**One business action.** `await lane.advance()` is the whole App-facing interface. The caller does not
learn the admission order, the underlying de-duplication, the bar cutoff, the manifest construction,
the catalog resolution, the Case lease, the Capital attribution or the transaction boundaries. It owns
the poll interval, the stop event and the process lifecycle, and nothing else.

**One fixed ordering, and it is the whole state machine.**

    bounded OI projection snapshot
    -> normalize source
    -> Binance-live / research-only split
    -> deterministic admission
    -> resolve the active credential-free Binance public catalog
    -> fetch closed Binance bars (outside every transaction)
    -> Case + CASE_CREATED admission row, atomically
    -> pure deterministic OI policy
    -> NO_TRADE + Capital NOT_APPLICABLE
       or LONG + exact Capital BLOCKED reason
    -> zero new Intent, entry fence, or provider economic write before #360

**What replaced what (#331).** This module is the replacement for `TradingPipeline`, `CandidateRunner`,
the News/OI trigger fusion, the strategy registry, the DSPy decision program and the liquidation shadow
runner — not a facade over them. Everything the old cluster did that this does not do is gone because
the product decided it should be:

* an editorial News verdict is not a Source of this lane and there is no code path that offers one;
* a Hyperliquid frame is answered `RESEARCH_ONLY` at admission instead of being carried four stages
  further to fail as `intent_instrument_not_allowed`;
* a live instrument is resolved from the active public catalog owned by Trading rather than a News
  projection; the catalog states public truth and grants no execution permission;
* the polling-driven funnel is gone: every number a product surface reports is a bounded aggregation
  over durable rows.

**Failure semantics.** Expected business refusals are a closed vocabulary written durably. Everything
else — a PostgreSQL timeout, a serialization failure, a repository bug — propagates out of `advance()`
with its transaction rolled back, so the Case stays claimable and the Source is not consumed by an
infrastructure fault. A missing `trading_runtime_state` row halts the turn before any scan, Case,
provider call or Intent.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar, Final, Literal, Protocol

from pydantic import ValidationError

from .admission import (
    AdmissionConfig,
    AdmissionResult,
    AdmissionRow,
    admit_trigger,
    admit_venue,
    case_created,
    defer,
    reject,
    source_rejected,
)
from .contracts import (
    LIVE_EXCHANGE_ID,
    LIVE_VENUE,
    TRADING_MANIFEST_VERSION,
    Bar,
    CaseState,
    FrozenMarketContext,
    FrozenPolicyContext,
    InstrumentRef,
    OiCandidateRow,
    OiMarketTrigger,
    OiTradeCandidate,
    TradingCaseManifest,
    oi_source_key,
)
from .market_context import PriceWindow, pre_move_bps, select_bar
from .policy import CapitalPolicy
from .sources import SourceRejected, normalize_oi_source
from .storage.lane import CapitalAuthority
from .storage.root import TradingRepository
from .telemetry import TradingExternalDataTelemetryPort, TradingWorkSemantics, observe_provider_call

log = logging.getLogger("tracefold.trading")

BAR_INTERVAL_MS: Final = 300_000
COLD_READ_TIMEOUT_SECONDS: Final = 10.0
COLD_WRITE_TIMEOUT_SECONDS: Final = 10.0
# A bounded overlap instead of a cursor. Everything inside `max_age_ms` is still fresh enough to
# trade, so re-reading a multiple of that window each turn is what makes a crash, a redeploy or a
# paused lane self-healing; `primary_source_key` rejects whatever a previous turn already used.
_SCAN_OVERLAP_FACTOR: Final = 3
_CASE_LEASE_MS: Final = 60_000
# One freeze per turn, and it is a capital rule rather than a throughput choice.
#
# `ux_trading_intents_one_active` admits a single nonterminal Intent, so at most one frozen Case can
# reach `INTENT_EMITTED` at a time. (Until #348 this paragraph said "one fenced entry per UTC day"
# instead, and that rule is gone — but the conclusion does not depend on it, and the reason to keep
# this constant at 1 is unchanged.) Freezing four meant that when the first answered `long`, the
# other three were decided against a fence the first had just taken and settled
# `BLOCKED / capacity_exhausted` — a *terminal* state, which put their `primary_source_key` beyond
# re-admission forever. That is precisely the confusion admission refuses to make one stage earlier:
# the lane was full, the Source was not unusable. At one freeze per turn the loser is answered
# `DEFERRED / lane_capacity_exhausted` instead, and wins a later scan inside its own trigger budget —
# 150 turns at the shipped 2 s poll and 300 s freshness, against a measured two Cases an hour.
_MAX_FREEZES_PER_TURN: Final = 1
# Draining already-frozen Cases is not the same question: a restart or a paused lane can leave several
# claimable, and deciding them costs no capital — every one of them is refused by the same fence.
_MAX_DECISIONS_PER_TURN: Final = 4
# How long a frozen Case may wait to be decided. Its own budget, separate from the freshness admission
# already spent, so queueing behind another Case cannot silently discard a signal — and short enough
# that a paused lane resumed hours later cannot size and stop off a stale bar close.
_CASE_DECISION_TTL_MS: Final = 300_000
# The lane persists about 90 OI facts a day, so this is a few thousand rows — small enough that "why
# was there no Case last Tuesday" stays answerable.
_ADMISSION_RETENTION_MS: Final = 90 * 86_400_000


class TradingDatabasePort(Protocol):
    """The lane's whole view of the process database: one bounded read, one bounded transaction.

    Capital safety is why this is a port and not a shared client. The lane may not open a session,
    pick a pool, choose a lane, or hold a connection across a provider call; it names an operation and
    a deadline and gets a repository session back. The composition root satisfies it on the one-slot
    heavy admission shared with Event Reaction and Janitor (#88, #104), so a trading backlog can never
    compete for the four News lane slots.
    """

    async def tx[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float) -> T: ...

    async def read[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float) -> T: ...


BarFetcher = Callable[[str, int, int], Awaitable[Sequence[Bar]]]
# `(repos, metric_version, after_ms, until_ms) -> the OI source rows`. The repository session stays
# opaque: this context never learns which repositories it carries, and no Trading threshold crosses
# the seam — the projection answers "which facts exist", admission answers "which of them may trigger".
OiProjectionReader = Callable[[Any, str, int, int], Sequence[OiCandidateRow]]

LaneOutcome = Literal["ADVANCED", "HALTED"]


def now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class CapitalLaneConfig:
    """Everything one turn executes that is not a collaborator. One object, so a turn is reproducible.

    No venue list: there is one live venue and it is code-owned. No model budget: the lane makes no
    model call. No poll interval: the process owns its own loop.
    """

    oi_metric_version: str = "oi_signal_v1"
    admission: AdmissionConfig = field(default_factory=AdmissionConfig)
    price_window: PriceWindow = field(default_factory=PriceWindow)
    policy: CapitalPolicy = field(default_factory=CapitalPolicy)
    target_notional_usd: Decimal = Decimal("10")

    @property
    def scan_horizon_ms(self) -> int:
        return self.admission.max_age_ms * _SCAN_OVERLAP_FACTOR


@dataclass(frozen=True, slots=True)
class LaneTurn:
    """One turn's receipt. Telemetry, never truth: PostgreSQL rows are the only business answer."""

    outcome: LaneOutcome
    reason: str
    sources: int = 0
    research_only: int = 0
    cases_created: int = 0
    no_trade: int = 0
    blocked: int = 0

    def as_dict(self) -> dict[str, str | int]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "sources": self.sources,
            "research_only": self.research_only,
            "cases_created": self.cases_created,
            "no_trade": self.no_trade,
            "blocked": self.blocked,
        }


class CapitalLane:
    """Scan, admit, freeze, run Policy, and persist the independent Capital disposition."""

    work_semantics: ClassVar[tuple[TradingWorkSemantics, ...]] = ("derived_work", "capital_truth")

    def __init__(
        self,
        *,
        db: TradingDatabasePort,
        config: CapitalLaneConfig,
        bars: BarFetcher,
        oi_projection: OiProjectionReader,
        news_generation: str,
        clock: Callable[[], int] = now_ms,
        telemetry: TradingExternalDataTelemetryPort | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._bars = bars
        self._oi_projection = oi_projection
        # The News generation this process may advance a persisted Case under. Supplied by the App
        # seam, which is the only thing that knows both capabilities; Trading never reads a News table.
        self._news_generation = news_generation
        self._clock = clock
        self._telemetry = telemetry
        self._run_id = uuid.uuid4().hex

    async def start(self) -> None:
        """Publish the configured Decision Plane before the first provider or Source read."""

        await self._record_runtime(state="STARTING", reason=None)

    # ------------------------------------------------------------------ the one business action
    async def advance(self) -> LaneTurn:
        """Move the capital lane forward by one turn.

        Raises rather than returning a business answer when the database is unavailable or a
        repository operation fails in a way this lane does not model. The caller logs and retries; no
        Source is consumed and no Case is terminalised by an infrastructure fault.
        """

        try:
            turn = await self._advance_turn()
        except Exception:
            # Preserve the original fault if even the fault receipt cannot be written.
            with suppress(Exception):
                await self._record_runtime(state="FAULTED", reason="decision_turn_fault")
            raise
        await self._record_runtime(state="RUNNING", reason=None)
        return turn

    async def _advance_turn(self) -> LaneTurn:
        now = self._clock()
        authority = await self._db.read(
            "trading_capital_authority",
            lambda repos: _trading(repos).capital_authority(
                since_ms=now - self._config.scan_horizon_ms,
                now_ms=now,
            ),
            timeout_seconds=COLD_READ_TIMEOUT_SECONDS,
        )
        if authority is None:
            # No runtime authority row: no scan, no Case, no provider call, no Intent. This is
            # infrastructure state, not a business decision, and nothing durable records a refusal.
            return LaneTurn(outcome="HALTED", reason="runtime_state_missing")
        rows = await self._db.read(
            "trading_oi_projection",
            lambda repos: list(
                self._oi_projection(
                    repos,
                    self._config.oi_metric_version,
                    now - self._config.scan_horizon_ms,
                    now,
                )
            ),
            timeout_seconds=COLD_READ_TIMEOUT_SECONDS,
        )

        results: dict[str, AdmissionResult] = {}
        admitted = self._admit(rows, authority=authority, now=now, results=results)
        created = 0
        for candidate in admitted[:_MAX_FREEZES_PER_TURN]:
            if await self._freeze(candidate, authority=authority, now=now, results=results):
                created += 1
        for candidate in admitted[_MAX_FREEZES_PER_TURN:]:
            results[candidate.source_key] = defer(
                candidate,
                stage="eligibility",
                reason="lane_capacity_exhausted",
                evidence={"lane_full": "freezes_per_turn"},
            )
        await self._flush_admission(results, now)
        await self._maintain_admission(now)

        no_trade = blocked = 0
        for _ in range(_MAX_DECISIONS_PER_TURN):
            decided = await self._decide_one()
            if decided is None:
                break
            if decided is CaseState.BLOCKED:
                blocked += 1
            elif decided is CaseState.NO_TRADE:
                no_trade += 1
            else:
                raise RuntimeError(f"trading_decision_state_unexpected:{decided}")
        return LaneTurn(
            outcome="ADVANCED",
            reason="advanced",
            sources=len(rows),
            research_only=sum(1 for item in results.values() if item.status == "RESEARCH_ONLY"),
            cases_created=created,
            no_trade=no_trade,
            blocked=blocked,
        )

    async def _record_runtime(self, *, state: str, reason: str | None) -> None:
        now = self._clock()
        updated = await self._db.tx(
            "trading_decision_runtime",
            lambda repos: _trading(repos).set_decision_runtime(
                state=state,
                heartbeat_at_ms=now,
                reason=reason,
                now_ms=now,
            ),
            timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
        )
        if not updated:
            raise RuntimeError("trading_decision_runtime_missing")

    # ------------------------------------------------------------------ admit
    def _admit(
        self,
        rows: Sequence[OiCandidateRow],
        *,
        authority: CapitalAuthority,
        now: int,
        results: dict[str, AdmissionResult],
    ) -> list[OiTradeCandidate]:
        """Every row this turn read, answered exactly once, reduced to at most one plan per issuer.

        The order is source, venue, then the situational rules, because that is the order in which an
        answer stops being about the frame and starts being about the moment. Coalescing runs last:
        several frames for one issuer inside the window are one thesis, and the newest wins.
        """

        config = self._config
        admitted: dict[str, OiTradeCandidate] = {}
        for row in rows:
            normalized = normalize_oi_source(row)
            if isinstance(normalized, SourceRejected):
                results[_source_key(row, config.oi_metric_version)] = source_rejected(
                    normalized,
                    source_key=_source_key(row, config.oi_metric_version),
                    observed_at_ms=int(row.get("observed_at_ms") or row.get("verdict_created_at_ms") or 0),
                )
                continue
            venue = admit_venue(normalized)
            if venue is not None:
                results[normalized.source_key] = venue
                continue
            verdict = admit_trigger(
                normalized,
                now_ms=now,
                config=config.admission,
                blacklist=authority.blacklist,
                active_underlyings=authority.active_underlyings,
                underlyings_in_flight=authority.underlyings_in_flight,
                cased_source_keys=authority.cased_source_keys,
            )
            if verdict is not None:
                results[normalized.source_key] = verdict
                continue
            incumbent = admitted.get(normalized.underlying_key)
            if incumbent is None:
                admitted[normalized.underlying_key] = normalized
                continue
            # One issuer, one thesis per turn. The loser is *coalesced*, not retired: nothing durable
            # stops it, and it wins the next scan as soon as the winner has produced its Case.
            loser, winner = (
                (incumbent, normalized)
                if (normalized.observed_at_ms, normalized.source_key) > (incumbent.observed_at_ms, incumbent.source_key)
                else (normalized, incumbent)
            )
            admitted[winner.underlying_key] = winner
            results[loser.source_key] = defer(loser, stage="eligibility", reason="superseded_by_newer_trigger")
        # A recovery obligation is a Capital fact, not a reason to stop Decision. The per-turn freeze
        # bound below still meters work, while same-underlying duplicate theses remain an Admission fact.
        return sorted(admitted.values(), key=lambda item: item.observed_at_ms, reverse=True)

    # ------------------------------------------------------------------ freeze
    async def _freeze(
        self,
        candidate: OiTradeCandidate,
        *,
        authority: CapitalAuthority,
        now: int,
        results: dict[str, AdmissionResult],
    ) -> bool:
        """Resolve the public catalog, fetch closed bars, then write one immutable Case.

        Everything refused here is refused because a *manifest could not be frozen* — no executable
        contract in the active universe, no candle, no mark at the cutoff. A valid but unfavourable
        frame is not among them: the Case is created and the policy names the refusal, so the manifest
        records what was rejected instead of the frame disappearing before anything durable saw it.
        """

        snapshot = authority.catalog
        instrument_row = None if snapshot is None else snapshot.resolve(candidate.base_symbol)
        if snapshot is None or instrument_row is None:
            # Retryable: a public catalog refresh can list this issuer, and the expiry sweep closes
            # the row when the frame goes stale.
            results[candidate.source_key] = defer(candidate, stage="catalog", reason="catalog_absent")
            return False
        instrument = InstrumentRef(
            exchange_id=LIVE_EXCHANGE_ID,
            venue=LIVE_VENUE,
            provider_symbol=instrument_row.provider_symbol,
            base_symbol=candidate.base_symbol,
            instrument_class="crypto",
            quote_asset=instrument_row.settlement_asset,
            observed_at_ms=now,
        )

        bars = await self._fetch_bars(instrument, anchor_at_ms=candidate.observed_at_ms)
        if not bars:
            results[candidate.source_key] = defer(candidate, stage="market_context", reason="market_data_unavailable")
            return False
        # The mark is the bar closed at or before the cutoff; a fresher close would leak future
        # evidence into a decision that claims to have been taken at the frame's own instant.
        anchor = select_bar(
            bars,
            target_ms=candidate.observed_at_ms,
            gap_tolerance_ms=self._config.price_window.bar_gap_tolerance_ms,
        )
        if anchor is None:
            # Terminal: the gap is a property of this frame's own cutoff, so no later scan of the same
            # frame can find a candle that was never published.
            results[candidate.source_key] = reject(candidate, stage="market_context", reason="market_data_invalid")
            return False

        policy = self._config.policy
        manifest = TradingCaseManifest(
            primary_trigger=OiMarketTrigger(
                source_key=candidate.source_key,
                observed_at_ms=candidate.observed_at_ms,
                persisted_at_ms=candidate.verdict_created_at_ms,
                venue=candidate.venue,
            ),
            contexts=FrozenPolicyContext(
                oi=candidate,
                market=FrozenMarketContext(
                    mark_price=anchor.close,
                    observed_at_ms=candidate.observed_at_ms,
                    pre_move_bps=pre_move_bps(
                        bars,
                        anchor_at_ms=candidate.observed_at_ms,
                        window=self._config.price_window,
                    ),
                    pre_move_lookback_ms=self._config.price_window.lookback_ms,
                ),
            ),
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            policy_config=policy.config_snapshot,
            policy_config_digest=policy.config_digest,
            underlying_key=candidate.underlying_key,
            base_symbol=candidate.base_symbol,
            cutoff_ms=candidate.observed_at_ms,
            instrument=instrument,
            venue_catalog_snapshot_sha256=snapshot.snapshot_sha256,
        )
        case_id = uuid.uuid4().hex
        linked = case_created(candidate, case_id=case_id)
        created = await self._db.tx(
            "trading_case_create",
            lambda repos: _trading(repos).create_case(
                case_id=case_id,
                manifest=manifest,
                admission=self._admission_row(linked),
                now_ms=now,
            ),
            timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
        )
        if created:
            # Already committed beside the Case row; leaving it out of the turn's flush is what keeps
            # the flush from re-deciding a row that is now terminal.
            results.pop(candidate.source_key, None)
            return True
        # The unique constraint refused: another turn or another process already made a Case of this
        # Source, or of another Source for the same issuer.
        results[candidate.source_key] = reject(candidate, stage="freeze", reason="already_consumed")
        return False

    async def _fetch_bars(self, instrument: InstrumentRef, *, anchor_at_ms: int) -> list[Bar]:
        """Closed Binance bars around the cutoff. Outside every transaction, by construction."""

        target = anchor_at_ms - self._config.price_window.lookback_ms
        start = (target // BAR_INTERVAL_MS - 1) * BAR_INTERVAL_MS
        try:
            bars = await observe_provider_call(
                self._telemetry,
                name="trading_capital_lane",
                source="binance",
                call=self._bars(instrument.provider_symbol, start, anchor_at_ms + BAR_INTERVAL_MS),
            )
        except Exception:
            log.warning("trading bar fetch failed symbol=%s", instrument.provider_symbol)
            return []
        return sorted(bars, key=lambda bar: bar.close_at_ms)

    # ------------------------------------------------------------------ decide
    async def _decide_one(self) -> CaseState | None:
        """Claim one Case, run the pure policy, and commit exactly one terminal answer."""

        now = self._clock()
        claimed = await self._db.tx(
            "trading_case_claim",
            lambda repos: _trading(repos).claim_case(run_id=self._run_id, lease_ms=_CASE_LEASE_MS, now_ms=now),
            timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
        )
        if claimed is None:
            return None
        case_id = str(claimed["case_id"])
        raw = claimed.get("manifest")

        # Do the generation check before parsing: a manifest from a retired shape cannot satisfy v7's
        # required identities, and letting validation raise would strand the claimed row in RUNNING
        # until its lease expired.
        if not _uses_current_news_generation(raw, news_generation=self._news_generation):
            return await self._block(case_id, "source_generation_retired", now)
        try:
            manifest = TradingCaseManifest.model_validate(raw)
        except ValidationError:
            return await self._block(case_id, "manifest_invalid", now)
        policy = self._config.policy
        if manifest.policy_id != policy.policy_id or manifest.policy_version != policy.policy_version:
            # A Case frozen under a retired policy identity is not this code's to decide. There is no
            # decoder for it and there will not be one: replaying it would mean executing rules it was
            # never frozen under.
            return await self._block(case_id, "policy_identity_retired", now)
        # A Case carries a mark frozen at its cutoff. Measured from `created_at_ms`, not from the
        # trigger: admission already spent the trigger's budget, and reusing it here would block a
        # frame frozen at 280 s old the moment the previous Case took twenty seconds to decide.
        if now - int(claimed["created_at_ms"]) > _CASE_DECISION_TTL_MS:
            return await self._block(case_id, "case_stale", now)

        decision = policy.decide(manifest.contexts)
        evidence = decision.evidence()
        if decision.decision == "no_trade":
            settled = await self._db.tx(
                "trading_case_no_trade",
                lambda repos: _trading(repos).settle_case(
                    case_id=case_id,
                    run_id=self._run_id,
                    state=CaseState.NO_TRADE,
                    policy_decision="no_trade",
                    policy_reason=decision.rule,
                    policy_checks=evidence,
                    capital_disposition="not_applicable",
                    capital_reason=None,
                    now_ms=self._clock(),
                ),
                timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
            )
            if not settled:
                # Another worker terminalised it while this one was deciding. Its receipt stands.
                return None
            return CaseState.NO_TRADE

        commit_at = self._clock()
        commit = await self._db.tx(
            "trading_capital_disposition_commit",
            lambda repos: _trading(repos).commit_capital_disposition(
                case_id=case_id,
                run_id=self._run_id,
                manifest=manifest,
                policy_reason=decision.rule,
                policy_checks=evidence,
                now_ms=commit_at,
            ),
            timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
        )
        return commit.state

    async def _block(self, case_id: str, reason: str, now: int) -> CaseState | None:
        settled = await self._db.tx(
            "trading_case_block",
            lambda repos: _trading(repos).settle_case(
                case_id=case_id,
                run_id=self._run_id,
                state=CaseState.BLOCKED,
                policy_decision="not_run",
                policy_reason=reason,
                policy_checks=None,
                capital_disposition="not_applicable",
                capital_reason=None,
                now_ms=now,
            ),
            timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
        )
        return CaseState.BLOCKED if settled else None

    # ------------------------------------------------------------------ admission ledger
    def _admission_row(self, result: AdmissionResult) -> AdmissionRow:
        return result.row(gate_config_digest=self._config.admission.digest)

    async def _flush_admission(self, results: dict[str, AdmissionResult], now: int) -> None:
        """Write the turn's admission answers in one transaction.

        `CASE_CREATED` is the exception and is committed with its Case; everything else lands here.
        A failure propagates: the ledger is the only durable explanation of a lane that produced
        nothing, and quietly losing a turn of it is how `oi_rows = 0` became unanswerable.
        """

        if not results:
            return
        rows = [self._admission_row(result) for result in results.values()]

        def _write(repos: Any) -> None:
            trading = _trading(repos)
            for row in rows:
                trading.record_gate_decision(now_ms=now, **row)

        await self._db.tx("trading_admission_write", _write, timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS)

    async def _maintain_admission(self, now: int) -> None:
        """Close decisions the clock has answered, and drop the ones past retention."""

        stale_before = now - self._config.admission.max_age_ms
        purge_before = now - _ADMISSION_RETENTION_MS

        def _maintain(repos: Any) -> None:
            trading = _trading(repos)
            trading.expire_stale_gate_decisions(stale_before_ms=stale_before, now_ms=now)
            trading.purge_gate_decisions(observed_before_ms=purge_before)

        await self._db.tx("trading_admission_maintenance", _maintain, timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS)


def _trading(repos: Any) -> TradingRepository:
    trading: TradingRepository = repos.trading
    return trading


def _source_key(row: OiCandidateRow, metric_version: str) -> str:
    return oi_source_key(row.get("event_id"), row.get("metric_version") or metric_version)


def _uses_current_news_generation(raw: object, *, news_generation: str) -> bool:
    """Whether an untrusted persisted manifest names the News generation Trading may still act on.

    The generation is *passed in*, not compared against a literal (#314). Trading holds no News literal
    and reads no News table; `tracefold.app` knows both capabilities and is the only place that may
    hand one to the other.

    The upstream projection joins the running bundle's epoch, so a stale row cannot become a *new*
    Case; but this runs on Cases already persisted. A Case frozen under one bundle and left undecided
    across a deployment would otherwise receive Capital attribution under a generation it was never reasoned
    under, because `program_version` and `policy_version` do not move when a prompt or a model slot
    does.
    """

    if not isinstance(raw, dict) or raw.get("manifest_version") != TRADING_MANIFEST_VERSION:
        return False
    contexts = raw.get("contexts")
    if not isinstance(contexts, dict):
        return False
    source = contexts.get("oi")
    return (
        isinstance(source, dict)
        and str(source.get("learning_epoch") or "") == news_generation
        and source.get("policy_version") == "news_triage_policy_v10"
        and source.get("program_version") == "news_oi_signal_v1"
    )


__all__ = [
    "BAR_INTERVAL_MS",
    "BarFetcher",
    "CapitalLane",
    "CapitalLaneConfig",
    "LaneTurn",
    "OiProjectionReader",
    "TradingDatabasePort",
]
