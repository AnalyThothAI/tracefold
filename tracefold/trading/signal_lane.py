"""The one production Alpha action: Source -> Case -> TradeSignalV1.

`SignalLane.advance()` owns admission, point-in-time evidence, pure Alpha evaluation, and the atomic
Case/Signal commit. It knows no execution profile, credential, account, balance, position, size,
leverage, order, protection, capital grant or reservation.

The one Runtime fact it reads is the published route catalogue on
`trading_execution_runtime_state.routes`: which `market_key`s a configured Runtime can reach at all.
That is a catalogue, not permission, sizing or a route choice — the lane still names no venue,
instrument, account or quantity — and reading it is what keeps the turn's single Case freeze off a
market whose only possible execution answer is `instrument_unmapped`.
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
from .sources import SourceRejected, normalize_oi_source
from .storage.execution_stream import prepare_trade_signal
from .storage.lane import SignalLaneSnapshot
from .storage.root import TradingRepositories, TradingRepository
from .telemetry import TradingExternalDataTelemetryPort, TradingWorkSemantics, observe_provider_call

log = logging.getLogger("tracefold.trading")

BAR_INTERVAL_MS: Final = 300_000
COLD_READ_TIMEOUT_SECONDS: Final = 10.0
COLD_WRITE_TIMEOUT_SECONDS: Final = 10.0
SIGNAL_TTL_MS: Final = 180_000
_SCAN_OVERLAP_FACTOR: Final = 3
_CASE_LEASE_MS: Final = 60_000
_MAX_FREEZES_PER_TURN: Final = 1
_MAX_DECISIONS_PER_TURN: Final = 4
_CASE_DECISION_TTL_MS: Final = 300_000
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
    oi_metric_version: str = "oi_signal_v1"
    admission: AdmissionConfig = field(default_factory=AdmissionConfig)
    price_window: PriceWindow = field(default_factory=PriceWindow)
    policy: AlphaPolicy = field(default_factory=lambda: ALPHA_POLICY)
    signal_ttl_ms: int = SIGNAL_TTL_MS

    def __post_init__(self) -> None:
        if not 1_000 <= self.signal_ttl_ms <= self.admission.max_age_ms:
            raise ValueError("trading_signal_ttl_invalid")

    @property
    def scan_horizon_ms(self) -> int:
        return self.admission.max_age_ms * _SCAN_OVERLAP_FACTOR


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
        release_revision: str,
        clock: Callable[[], int] = now_ms,
        telemetry: TradingExternalDataTelemetryPort | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._bars = bars
        self._oi_projection = oi_projection
        self._release_revision = release_revision
        self._clock = clock
        self._telemetry = telemetry
        self._run_id = uuid.uuid4().hex
        self._alpha_contract_sha256 = canonical_sha256(
            {
                "policy_id": config.policy.policy_id,
                "policy_version": config.policy.policy_version,
                "policy_config": config.policy.config_snapshot,
            }
        )

    @property
    def alpha_contract_sha256(self) -> str:
        return self._alpha_contract_sha256

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
        for candidate in admitted[:_MAX_FREEZES_PER_TURN]:
            if await self._freeze(candidate, now=now, results=results):
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
        admitted: dict[str, OiTradeCandidate] = {}
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
                underlyings_in_flight=snapshot.underlyings_in_flight,
                cased_source_keys=snapshot.cased_source_keys,
            )
            if verdict is not None:
                results[normalized.source_key] = verdict
                continue
            executable = snapshot.executable_market_keys
            candidate_market = market_key(normalized.base_symbol)
            if executable is not None and candidate_market not in executable:
                # Terminal: the catalogue is the venue's listing, so no later scan of the same frame
                # reaches a different answer while this Runtime generation is the current one. A new
                # catalogue arrives as a new Runtime start, and `gate_config_digest` is unchanged, so
                # the row simply keeps this reason until a Source with a listed market arrives.
                results[normalized.source_key] = reject(
                    normalized,
                    stage="eligibility",
                    reason="instrument_unmapped",
                    evidence={"market_key": candidate_market},
                )
                continue
            incumbent = admitted.get(normalized.underlying_key)
            if incumbent is None:
                admitted[normalized.underlying_key] = normalized
                continue
            loser, winner = (
                (incumbent, normalized)
                if (normalized.observed_at_ms, normalized.source_key) > (incumbent.observed_at_ms, incumbent.source_key)
                else (normalized, incumbent)
            )
            admitted[winner.underlying_key] = winner
            results[loser.source_key] = defer(
                loser,
                stage="eligibility",
                reason="superseded_by_newer_trigger",
            )
        return sorted(admitted.values(), key=lambda item: item.observed_at_ms, reverse=True)

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
                release_revision=self._release_revision,
                now_ms=now,
            ),
            timeout_seconds=COLD_WRITE_TIMEOUT_SECONDS,
        )
        if created:
            results.pop(candidate.source_key, None)
            return True
        results[candidate.source_key] = reject(candidate, stage="freeze", reason="already_consumed")
        return False

    async def _fetch_bars(self, candidate: OiTradeCandidate, *, anchor_at_ms: int) -> list[Bar]:
        target = anchor_at_ms - self._config.price_window.lookback_ms
        start = (target // BAR_INTERVAL_MS - 1) * BAR_INTERVAL_MS
        try:
            bars = await observe_provider_call(
                self._telemetry,
                name="trading_signal_lane",
                source="binance" if candidate.venue == "binance.usdm" else "hyperliquid",
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
            lambda repos: _trading(repos).claim_case(run_id=self._run_id, lease_ms=_CASE_LEASE_MS, now_ms=now),
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
        if now - int(claimed["created_at_ms"]) > _CASE_DECISION_TTL_MS:
            return await self._block(case_id, "case_stale", now)
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
        evidence_sha256 = canonical_sha256({"case_manifest_sha256": manifest.digest(), "alpha_evidence": evidence})
        signal_id = canonical_sha256(
            {
                "signal_version": "trade_signal_v1",
                "case_id": case_id,
                "alpha_contract_sha256": self._alpha_contract_sha256,
                "market_key": manifest.market_key,
                "direction": "long",
                "observed_at_ns": observed_at_ns,
                "expires_at_ns": expires_at_ns,
                "evidence_sha256": evidence_sha256,
            }
        )
        prepared = prepare_trade_signal(
            signal_id=signal_id,
            case_id=case_id,
            alpha_contract_sha256=self._alpha_contract_sha256,
            market_key=manifest.market_key,
            direction="long",
            observed_at_ns=observed_at_ns,
            expires_at_ns=expires_at_ns,
            evidence_sha256=evidence_sha256,
            alpha_metadata={"policy_rule": decision.rule},
        )
        committed = await self._db.tx(
            "trading_signal_commit",
            lambda repos: _trading(repos).commit_signal(
                case_id=case_id,
                run_id=self._run_id,
                manifest=manifest,
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
                trading.record_gate_decision(now_ms=now, release_revision=self._release_revision, **row)

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
