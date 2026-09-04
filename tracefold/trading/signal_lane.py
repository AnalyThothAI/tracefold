"""The one production Alpha action: Source -> Case -> TradeSignalV1.

`SignalLane.advance()` owns admission, point-in-time evidence, pure Alpha evaluation, and the atomic
Case/Signal commit. It knows no execution profile, credential, account, balance, position, size,
leverage, order, protection, capital grant or reservation.

It reads no Runtime fact at all. The route catalogue it used to read was a second answer to a
question the Runtime already answers by name on the entry path, one scan behind and needing a
projection special case for "no catalogue published"; and the per-turn freeze budget it protected is
gone with it (#537 PR-3). One undecided Case per underlying is the database's own partial unique
index, not a rule restated here.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar, Final, Literal, Protocol

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
    Bar,
    CaseState,
    FrozenMarketContext,
    FrozenPolicyContext,
    OiCandidateRow,
    OiMarketTrigger,
    OiTradeCandidate,
    TradingCaseManifest,
    canonical_sha256,
    oi_source_key,
)
from .market_context import PriceWindow, pre_move_bps, select_bar
from .policy import ALPHA_POLICY, AlphaPolicy
from .sources import SourceRejected, normalize_oi_source, telemetry_source
from .storage.execution_stream import prepare_trade_signal
from .storage.lane import SignalLaneSnapshot
from .storage.root import TradingRepositories, TradingRepository
from .telemetry import TradingExternalDataTelemetryPort, TradingWorkSemantics, observe_provider_call

log = logging.getLogger("tracefold.trading")

BAR_INTERVAL_MS: Final = 300_000
COLD_READ_TIMEOUT_SECONDS: Final = 10.0
COLD_WRITE_TIMEOUT_SECONDS: Final = 10.0
SIGNAL_TTL_MS: Final = 180_000
_MAX_DECISIONS_PER_TURN: Final = 4
_ADMISSION_RETENTION_MS: Final = 90 * 86_400_000


class TradingDatabasePort(Protocol):
    async def tx[T](self, name: str, fn: Callable[[TradingRepositories], T], *, timeout_seconds: float) -> T: ...

    async def read[T](self, name: str, fn: Callable[[TradingRepositories], T], *, timeout_seconds: float) -> T: ...


BarFetcher = Callable[[OiTradeCandidate, int, int], Awaitable[Sequence[Bar]]]
OiProjectionReader = Callable[[str, int, int], Awaitable[Sequence[OiCandidateRow]]]
LaneOutcome = Literal["ADVANCED", "HALTED"]


def now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def market_key(base_symbol: str) -> str:
    """The venue-neutral perpetual market identity carried across the execution boundary."""

    return f"crypto:perp:{base_symbol}:USDT"


@dataclass(frozen=True, slots=True)
class SignalLaneConfig:
    # The upstream metric contract this lane keys its source identities on. It has no default: the
    # App composition seam is the only construction site and it always passes the value, so a literal
    # here was a second copy of a constant this package may not import (#537 PR-4).
    oi_metric_version: str
    admission: AdmissionConfig = field(default_factory=AdmissionConfig)
    price_window: PriceWindow = field(default_factory=PriceWindow)
    policy: AlphaPolicy = field(default_factory=lambda: ALPHA_POLICY)
    signal_ttl_ms: int = SIGNAL_TTL_MS

    def __post_init__(self) -> None:
        if not 1_000 <= self.signal_ttl_ms <= self.admission.max_age_ms:
            raise ValueError("trading_signal_ttl_invalid")

    @property
    def scan_horizon_ms(self) -> int:
        """Exactly the window a frame can still be admitted in, and not a minute more.

        It was three times that. A frame outside `max_age_ms` has one possible answer — `trigger_stale`
        — and the ledger already holds it, so the two extra windows only re-asked a closed question:
        the median frame was re-evaluated 439 times before the sweep closed it (#537 PR-3).
        """

        return self.admission.max_age_ms


@dataclass(frozen=True, slots=True)
class LaneTurn:
    outcome: LaneOutcome
    reason: str
    sources: int = 0
    cases_created: int = 0
    no_trade: int = 0
    blocked: int = 0
    signals_emitted: int = 0

    def as_dict(self) -> dict[str, str | int]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "sources": self.sources,
            "cases_created": self.cases_created,
            "no_trade": self.no_trade,
            "blocked": self.blocked,
            "signals_emitted": self.signals_emitted,
        }


class SignalLane:
    """Scan, admit, freeze, run Alpha, and atomically persist Case + Signal."""

    work_semantics: ClassVar[tuple[TradingWorkSemantics, ...]] = ("derived_work", "signal_truth")

    def __init__(
        self,
        *,
        db: TradingDatabasePort,
        config: SignalLaneConfig,
        bars: BarFetcher,
        oi_projection: OiProjectionReader,
        clock: Callable[[], int] = now_ms,
        telemetry: TradingExternalDataTelemetryPort | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._bars = bars
        self._oi_projection = oi_projection
        self._clock = clock
        self._telemetry = telemetry
        self._run_id = uuid.uuid4().hex

    async def advance(self) -> LaneTurn:
        return await self._advance_turn()

    async def _advance_turn(self) -> LaneTurn:
        now = self._clock()
        snapshot = await self._db.read(
            "trading_signal_lane_snapshot",
            lambda repos: _trading(repos).signal_lane_snapshot(since_ms=now - self._config.scan_horizon_ms),
            timeout_seconds=COLD_READ_TIMEOUT_SECONDS,
        )
        rows = list(
            await self._oi_projection(
                self._config.oi_metric_version,
                now - self._config.scan_horizon_ms,
                now,
            )
        )

        results: dict[str, AdmissionResult] = {}
        admitted = self._admit(rows, snapshot=snapshot, now=now, results=results)
        created = 0
        for candidate in admitted:
            if await self._freeze(candidate, now=now, results=results):
                created += 1
        await self._flush_admission(results, now)
        await self._maintain_admission(now)

        no_trade = blocked = signals_emitted = 0
        for _ in range(_MAX_DECISIONS_PER_TURN):
            decided = await self._decide_one()
            if decided is None:
                break
            if decided is CaseState.BLOCKED:
                blocked += 1
            elif decided is CaseState.NO_TRADE:
                no_trade += 1
            elif decided is CaseState.SIGNAL_EMITTED:
                signals_emitted += 1
            else:  # pragma: no cover - the repository writer owns this closed set
                raise RuntimeError(f"trading_decision_state_unexpected:{decided}")
        return LaneTurn(
            outcome="ADVANCED",
            reason="advanced",
            sources=len(rows),
            cases_created=created,
            no_trade=no_trade,
            blocked=blocked,
            signals_emitted=signals_emitted,
        )

    def _admit(
        self,
        rows: Sequence[OiCandidateRow],
        *,
        snapshot: SignalLaneSnapshot,
        now: int,
        results: dict[str, AdmissionResult],
    ) -> list[OiTradeCandidate]:
        """Every frame in the window that may become a Case, newest first.

        No per-turn budget and no per-underlying tournament. Both were software copies of a rule the
        database already holds — `ux_trading_case_in_flight_underlying` — and freezing one Case per
        turn is what made the budget necessary in the first place (#537 PR-3).
        """

        admitted: list[OiTradeCandidate] = []
        for row in rows:
            normalized = normalize_oi_source(row)
            if isinstance(normalized, SourceRejected):
                results[_source_key(row, self._config.oi_metric_version)] = source_rejected(
                    normalized,
                    source_key=_source_key(row, self._config.oi_metric_version),
                    observed_at_ms=int(row.get("observed_at_ms") or row.get("available_at_ms") or 0),
                )
                continue
            venue_result = admit_venue(normalized)
            if venue_result is not None:
                results[normalized.source_key] = venue_result
                continue
            verdict = admit_trigger(
                normalized,
                now_ms=now,
                config=self._config.admission,
                cased_source_keys=snapshot.cased_source_keys,
            )
            if verdict is not None:
                results[normalized.source_key] = verdict
                continue
            admitted.append(normalized)
        return sorted(admitted, key=lambda item: item.observed_at_ms, reverse=True)

    async def _freeze(
        self,
        candidate: OiTradeCandidate,
        *,
        now: int,
        results: dict[str, AdmissionResult],
    ) -> bool:
        bars = await self._fetch_bars(candidate, anchor_at_ms=candidate.observed_at_ms)
        if not bars:
            results[candidate.source_key] = defer(
                candidate,
                stage="market_context",
                reason="market_data_unavailable",
            )
            return False
        anchor = select_bar(
            bars,
            target_ms=candidate.observed_at_ms,
            gap_tolerance_ms=self._config.price_window.bar_gap_tolerance_ms,
        )
        if anchor is None:
            results[candidate.source_key] = reject(
                candidate,
                stage="market_context",
                reason="market_data_invalid",
            )
            return False

        policy = self._config.policy
        manifest = TradingCaseManifest(
            primary_trigger=OiMarketTrigger(
                source_key=candidate.source_key,
                observed_at_ms=candidate.observed_at_ms,
                persisted_at_ms=candidate.available_at_ms,
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
            market_key=market_key(candidate.base_symbol),
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
            results.pop(candidate.source_key, None)
            return True
        # The insert is `ON CONFLICT DO NOTHING` over every unique constraint the table has, so this
        # is the one durable answer for both of them: this source already produced a Case, or its
        # issuer already holds the one undecided Case that `ux_trading_case_in_flight_underlying`
        # allows. Either way the frame is consumed and no later scan changes it.
        results[candidate.source_key] = reject(candidate, stage="freeze", reason="already_consumed")
        return False

    async def _fetch_bars(self, candidate: OiTradeCandidate, *, anchor_at_ms: int) -> list[Bar]:
        target = anchor_at_ms - self._config.price_window.lookback_ms
        start = (target // BAR_INTERVAL_MS - 1) * BAR_INTERVAL_MS
        try:
            bars = await observe_provider_call(
                self._telemetry,
                name="trading_signal_lane",
                source=telemetry_source(candidate.venue),
                call=self._bars(candidate, start, anchor_at_ms + BAR_INTERVAL_MS),
            )
        except Exception:
            log.warning("trading bar fetch failed market=%s", market_key(candidate.base_symbol))
            return []
        return sorted(bars, key=lambda bar: bar.close_at_ms)

    async def _decide_one(self) -> CaseState | None:
        now = self._clock()
        claimed = await self._db.tx(
            "trading_case_claim",
            lambda repos: _trading(repos).claim_case(run_id=self._run_id, now_ms=now),
            timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
        )
        if claimed is None:
            return None
        case_id = str(claimed["case_id"])
        try:
            manifest = TradingCaseManifest.model_validate(claimed.get("manifest"))
        except ValidationError:
            # Includes a Case frozen under an earlier manifest version: the pinned `manifest_version`
            # refuses it here rather than re-deciding a layout this policy never ran against.
            return await self._block(case_id, "manifest_invalid", now)
        policy = self._config.policy
        if manifest.policy_id != policy.policy_id or manifest.policy_version != policy.policy_version:
            return await self._block(case_id, "policy_identity_retired", now)
        # One clock over the Case, and it is the Source's. A second budget measured from the freeze
        # could only ever expire after this one at any `max_age_ms` an operator would set, so it never
        # decided anything (#537 PR-3).
        source_deadline_ms = manifest.primary_trigger.observed_at_ms + self._config.admission.max_age_ms
        if now >= source_deadline_ms:
            return await self._block(case_id, "source_stale", now)

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
                    now_ms=self._clock(),
                ),
                timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
            )
            return CaseState.NO_TRADE if settled else None

        commit_at = self._clock()
        if commit_at >= source_deadline_ms:
            return await self._block(case_id, "source_stale", commit_at)
        observed_at_ns = commit_at * 1_000_000
        expires_at_ns = min(commit_at + self._config.signal_ttl_ms, source_deadline_ms) * 1_000_000
        # One Case emits at most one Signal, and a re-run of the same Case at the same instant is
        # the same Signal; the two facts that identify it are all the id needs (#520 PR-C).
        signal_id = canonical_sha256({"case_id": case_id, "observed_at_ns": observed_at_ns})
        prepared = prepare_trade_signal(
            signal_id=signal_id,
            case_id=case_id,
            market_key=manifest.market_key,
            direction="long",
            observed_at_ns=observed_at_ns,
            expires_at_ns=expires_at_ns,
        )
        committed = await self._db.tx(
            "trading_signal_commit",
            lambda repos: _trading(repos).commit_signal(
                case_id=case_id,
                run_id=self._run_id,
                policy_reason=decision.rule,
                policy_checks=evidence,
                prepared=prepared,
                now_ms=commit_at,
            ),
            timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
        )
        return CaseState.SIGNAL_EMITTED if committed else None

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
                now_ms=now,
            ),
            timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
        )
        return CaseState.BLOCKED if settled else None

    def _admission_row(self, result: AdmissionResult) -> AdmissionRow:
        return result.row(gate_config_digest=self._config.admission.digest)

    async def _flush_admission(self, results: dict[str, AdmissionResult], now: int) -> None:
        if not results:
            return
        rows = [self._admission_row(result) for result in results.values()]

        def _write(repos: TradingRepositories) -> None:
            trading = _trading(repos)
            for row in rows:
                trading.record_gate_decision(now_ms=now, **row)

        await self._db.tx("trading_admission_write", _write, timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS)

    async def _maintain_admission(self, now: int) -> None:
        stale_before = now - self._config.admission.max_age_ms
        purge_before = now - _ADMISSION_RETENTION_MS

        def _maintain(repos: TradingRepositories) -> None:
            trading = _trading(repos)
            trading.expire_stale_gate_decisions(stale_before_ms=stale_before, now_ms=now)
            trading.purge_gate_decisions(observed_before_ms=purge_before)

        await self._db.tx("trading_admission_maintenance", _maintain, timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS)


def _trading(repos: TradingRepositories) -> TradingRepository:
    return repos.trading


def _source_key(row: OiCandidateRow, metric_version: str) -> str:
    return oi_source_key(row.get("event_id"), row.get("metric_version") or metric_version)


__all__ = [
    "BAR_INTERVAL_MS",
    "SIGNAL_TTL_MS",
    "BarFetcher",
    "LaneTurn",
    "OiProjectionReader",
    "SignalLane",
    "SignalLaneConfig",
    "TradingDatabasePort",
    "market_key",
]
