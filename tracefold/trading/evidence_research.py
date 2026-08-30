"""Pure capture, drain and discovery-corpus construction for the #377 evidence clock.

The App owns PostgreSQL and public-provider reads.  This module only freezes rows handed to it and
evaluates a drained artifact.  Consequently corpus sealing has no network, credential or provider
write path, and a byte-identical capture + drain + contract produces a byte-identical corpus.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from statistics import NormalDist
from typing import Any, Final, cast

from .admission import AdmissionConfig
from .bindings import binding_for_source_venue
from .blacklist import BlacklistSnapshotV1
from .contract_receipt import ExecutionPolicyContractReceiptV3
from .contracts import (
    InstrumentCandidateRow,
    OiCandidateRow,
    OiTradeCandidate,
    canonical_base_symbol,
    canonical_sha256,
    oi_source_key,
)
from .evidence_clock import (
    CandidateDecisionReceiptV1,
    CandidateLockedV1,
    CapturedSourceV1,
    DiscoveryCorpusArtifactV1,
    DiscoveryFeatureV1,
    EvidenceCaptureArtifactV1,
    EvidenceCaptureSpecV1,
    EvidenceCoverageV1,
    EvidenceDrainArtifactV1,
    EvidenceIncident,
    EvidenceInputV1,
    FundingRateV1,
    FutureHoldoutMetricsV1,
    FutureHoldoutResultV1,
    MarketDrainRowV1,
    NormalizedEvidenceObservationV1,
    PointInTimeCatalogRowV1,
    PointInTimeCatalogV1,
    feature_contract_sha256,
    source_contract_sha256,
)
from .market_context import PriceWindow
from .policy import CapitalPolicy
from .replay import (
    BAR_FIDELITY_VERSION,
    BarEpisodeRunner,
    DirectionalReplayPlan,
    ReplayBarV1,
    ReplayMarketSlice,
    ReplayTerminalOutcomeV1,
    evaluate_replay_market_slices,
    parse_replay_sources,
    plan_replay_scenarios,
    replay_policy_identity,
)
from .research.oi_replay import replay_oi_facts
from .routing import resolve_instrument
from .sources import SourceRejected, normalize_oi_source

EVIDENCE_EVALUATOR_VERSION: Final = "trading_evidence_evaluator_v1"
_SOURCE_VENUE_BY_BINDING = {"BINANCE_USDM": "binance.perp", "HYPERLIQUID_PERP": "hl.perp"}
_EXCHANGE_BY_BINDING = {"BINANCE_USDM": "binance", "HYPERLIQUID_PERP": "hyperliquid"}
_HORIZONS_MS: Final[dict[str, int]] = {
    "3m": 180_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}


@dataclass(frozen=True, slots=True)
class _EvaluatedPopulation:
    raw: tuple[CapturedSourceV1, ...]
    normalized: tuple[NormalizedEvidenceObservationV1, ...]
    features: tuple[DiscoveryFeatureV1, ...]
    episodes: tuple[ReplayTerminalOutcomeV1, ...]
    coverage: EvidenceCoverageV1
    evaluator_program_sha256: str


def _required_int(value: object, error: str) -> int:
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise ValueError(error) from exc


def build_evidence_capture(
    rows: Sequence[OiCandidateRow],
    *,
    spec: EvidenceCaptureSpecV1,
    instruments: Any,
) -> EvidenceCaptureArtifactV1:
    """Freeze every source plus only the catalogue rows known at that source's cutoff."""

    captured: list[CapturedSourceV1] = []
    for raw in rows:
        source = dict(raw)
        observed_at_ms = _required_int(source.get("observed_at_ms"), "evidence_capture_source_clock_missing")
        known_at_ms = _required_int(source.get("verdict_created_at_ms"), "evidence_capture_source_clock_missing")
        venue_value = str(source.get("venue") or "").strip().lower()
        binding = binding_for_source_venue(venue_value)
        venue = venue_value if binding is None else _SOURCE_VENUE_BY_BINDING[binding]
        canonical_asset = canonical_base_symbol(source.get("symbol"))
        catalog_rows: list[PointInTimeCatalogRowV1] = []
        provider_instrument_id: str | None = None
        if binding is not None and canonical_asset:
            projected = list(instruments(canonical_asset, venue, observed_at_ms))
            catalog_rows.extend(
                PointInTimeCatalogRowV1(
                    venue=cast(Any, row["venue"]),
                    venue_symbol=str(row["venue_symbol"]),
                    base_symbol=str(row["base_symbol"]),
                    instrument_class="crypto",
                    quote_asset=None if row.get("quote_asset") is None else str(row["quote_asset"]),
                    status="trading",
                    observed_at_ms=int(row["last_seen_ms"]),
                )
                for row in projected
            )
            catalog_rows.sort(key=lambda row: (row.venue, row.venue_symbol))
            instrument = resolve_instrument(
                [_catalog_candidate(row) for row in catalog_rows],
                priority=(_EXCHANGE_BY_BINDING[binding],),
                observed_at_ms=observed_at_ms,
            )
            provider_instrument_id = None if instrument is None else instrument.provider_symbol
        catalog_payload = tuple(row.model_dump(mode="json") for row in catalog_rows)
        captured.append(
            CapturedSourceV1(
                source_identity=oi_source_key(source.get("event_id"), source.get("metric_version")),
                source_sha256=canonical_sha256(source),
                raw_source=source,
                venue=venue,
                binding=binding,
                canonical_asset=canonical_asset,
                provider_instrument_id=provider_instrument_id,
                observed_at_ms=observed_at_ms,
                known_at_ms=known_at_ms,
                available_at_ms=spec.captured_at_ms,
                catalog=PointInTimeCatalogV1(
                    source_observed_at_ms=observed_at_ms,
                    rows=tuple(catalog_rows),
                    rows_sha256=canonical_sha256(catalog_payload),
                ),
            )
        )
    captured.sort(key=lambda row: (row.observed_at_ms, row.source_identity))
    return EvidenceCaptureArtifactV1(spec=spec, sources=tuple(captured), source_count=len(captured))


def build_evidence_drain(
    capture: EvidenceCaptureArtifactV1,
    *,
    market_slices: Sequence[ReplayMarketSlice],
    drained_at_ms: int,
    max_horizon_ms: int,
    bar_interval_ms: int,
    funding_horizon_ms: int,
    finalization_lag_ms: int,
    cost_model: dict[str, Any],
    funding_rates_by_source: Mapping[str, Sequence[FundingRateV1] | None] | None = None,
) -> EvidenceDrainArtifactV1:
    """Freeze forward market inputs separately from capture and from evaluation."""

    if funding_horizon_ms > max_horizon_ms:
        raise ValueError("evidence_drain_funding_horizon_invalid")

    slices = {item.plan.source.source_key: item for item in market_slices}
    funding = dict(funding_rates_by_source or {})
    if len(slices) != len(market_slices):
        raise ValueError("evidence_drain_duplicate_market_slice")
    rows: list[MarketDrainRowV1] = []
    for source in capture.sources:
        item = slices.pop(source.source_identity, None)
        funding_rows_value = funding.pop(source.source_identity, None)
        funding_requested = funding_rates_by_source is not None and source.source_identity in funding_rates_by_source
        funding_rows = tuple(funding_rows_value or ())
        funding_requested_start_ms = source.observed_at_ms
        funding_requested_end_ms = source.observed_at_ms + funding_horizon_ms
        incidents: set[str] = set()
        if item is not None and item.reason == "venue_provider_outage":
            incidents.add("venue_provider_outage")
        if source.provider_instrument_id is None:
            incidents.add("catalog_reset_or_delist")
        bars = tuple(() if item is None else item.bars)
        if bars:
            bar_payload = tuple(bar.model_dump(mode="json") for bar in bars)
            requested_start_ms = item.start_ms if item is not None else source.observed_at_ms
            requested_end_ms = item.end_ms if item is not None else source.observed_at_ms + max_horizon_ms
            bars_complete = _bars_cover_window(
                bars,
                start_ms=requested_start_ms,
                end_ms=requested_end_ms,
                interval_ms=bar_interval_ms,
            )
            bars_input = (
                EvidenceInputV1(
                    state="AVAILABLE",
                    observed_at_ms=max(bar.close_at_ms for bar in bars),
                    known_at_ms=drained_at_ms,
                    value_sha256=canonical_sha256(bar_payload),
                )
                if bars_complete
                else EvidenceInputV1(state="MISSING", reason="bar_history_incomplete")
            )
        else:
            reason = (
                "instrument_unresolved"
                if source.provider_instrument_id is None
                else ("market_history_missing" if item is None else item.reason or "market_history_missing")
            )
            bars_input = EvidenceInputV1(state="MISSING", reason=reason)
            requested_start_ms = source.observed_at_ms
            requested_end_ms = source.observed_at_ms + max_horizon_ms
        rows.append(
            MarketDrainRowV1(
                source_identity=source.source_identity,
                venue=source.venue,
                provider_instrument_id=source.provider_instrument_id,
                requested_start_ms=requested_start_ms,
                requested_end_ms=requested_end_ms,
                bar_interval_ms=bar_interval_ms,
                funding_requested_start_ms=funding_requested_start_ms,
                funding_requested_end_ms=funding_requested_end_ms,
                bars=bars,
                bars_input=bars_input,
                funding_rates=funding_rows,
                funding_input=(
                    EvidenceInputV1(
                        state="AVAILABLE",
                        observed_at_ms=funding_requested_end_ms - 1,
                        known_at_ms=drained_at_ms,
                        value_sha256=canonical_sha256(tuple(row.model_dump(mode="json") for row in funding_rows)),
                    )
                    if funding_requested and funding_rows_value is not None
                    else EvidenceInputV1(state="MISSING", reason="historical_funding_unavailable")
                ),
                funding_window_sum_bps=(
                    -sum((row.funding_rate for row in funding_rows), start=Decimal(0)) * Decimal(10_000)
                    if funding_requested and funding_rows_value is not None
                    else None
                ),
                bid_ask_input=EvidenceInputV1(state="MISSING", reason="historical_bid_ask_unavailable"),
                liquidity_input=EvidenceInputV1(state="MISSING", reason="historical_executable_liquidity_unavailable"),
                incidents=tuple(sorted(cast(Any, incidents))),
            )
        )
    if slices:
        raise ValueError("evidence_drain_unknown_market_slice")
    if funding:
        raise ValueError("evidence_drain_unknown_funding_source")
    rows.sort(key=lambda row: row.source_identity)
    return EvidenceDrainArtifactV1(
        capture_sha256=capture.capture_sha256,
        partition=capture.spec.partition,
        drained_at_ms=drained_at_ms,
        max_horizon_ms=max_horizon_ms,
        bar_interval_ms=bar_interval_ms,
        funding_horizon_ms=funding_horizon_ms,
        finalization_lag_ms=finalization_lag_ms,
        rows=tuple(rows),
        cost_model=cost_model,
        cost_model_sha256=canonical_sha256(cost_model),
    )


def _bars_cover_window(
    bars: Sequence[ReplayBarV1],
    *,
    start_ms: int,
    end_ms: int,
    interval_ms: int,
) -> bool:
    if not bars or interval_ms <= 0:
        return False
    return (
        bars[0].open_at_ms <= start_ms
        and bars[-1].close_at_ms >= end_ms
        and all(bar.close_at_ms - bar.open_at_ms == interval_ms for bar in bars)
        and all(previous.close_at_ms == current.open_at_ms for previous, current in pairwise(bars))
    )


def seal_discovery_corpus(
    capture: EvidenceCaptureArtifactV1,
    drain: EvidenceDrainArtifactV1,
    *,
    contract: ExecutionPolicyContractReceiptV3,
    admission: AdmissionConfig,
    policy: CapitalPolicy,
    price_window: PriceWindow,
    target_notional: Decimal,
    run_episode: BarEpisodeRunner,
) -> DiscoveryCorpusArtifactV1:
    """Run the existing source/policy/BAR evaluator over frozen files, with zero I/O."""

    _validate_corpus_inputs(capture, drain)
    population = _evaluate_population(
        capture,
        drain,
        admission=admission,
        policy=policy,
        price_window=price_window,
        target_notional=target_notional,
        run_episode=run_episode,
    )
    return DiscoveryCorpusArtifactV1(
        capture_sha256=capture.capture_sha256,
        drain_sha256=drain.drain_sha256,
        discovery_start_ms=capture.spec.start_ms,
        discovery_end_ms=capture.spec.end_ms,
        execution_contract_receipt_sha256=contract.receipt_sha256,
        source_contract_sha256=source_contract_sha256(),
        feature_contract_sha256=_feature_contract_sha256(admission, policy, price_window),
        admission_config_sha256=admission.digest,
        policy_id=policy.policy_id,
        policy_config_sha256=policy.config_digest,
        price_window_sha256=canonical_sha256(price_window.as_dict()),
        cost_model_sha256=drain.cost_model_sha256,
        target_notional=target_notional,
        evaluator_program_sha256=population.evaluator_program_sha256,
        raw_observations=population.raw,
        normalized_observations=population.normalized,
        features=population.features,
        episodes=population.episodes,
        coverage=population.coverage,
        raw_observations_sha256=_rows_sha(population.raw),
        normalized_observations_sha256=_rows_sha(population.normalized),
        features_sha256=_rows_sha(population.features),
        episodes_sha256=_rows_sha(population.episodes),
        coverage_sha256=canonical_sha256(population.coverage.model_dump(mode="json")),
    )


def unblind_future_holdout(
    capture: EvidenceCaptureArtifactV1,
    drain: EvidenceDrainArtifactV1,
    *,
    candidate: CandidateLockedV1,
    candidate_receipt: CandidateDecisionReceiptV1,
    admission: AdmissionConfig,
    policy: CapitalPolicy,
    price_window: PriceWindow,
    target_notional: Decimal,
    run_episode: BarEpisodeRunner,
    evaluated_at_ms: int,
    external_incidents: Sequence[EvidenceIncident] = (),
) -> FutureHoldoutResultV1:
    """One protocol-locked decision after the fixed drain cutoff; persistence enforces one-shot."""

    _validate_future_inputs(capture, drain, candidate, candidate_receipt, evaluated_at_ms=evaluated_at_ms)
    population = _evaluate_population(
        capture,
        drain,
        admission=admission,
        policy=policy,
        price_window=price_window,
        target_notional=target_notional,
        run_episode=run_episode,
    )
    if population.evaluator_program_sha256 != candidate.evaluator_program_sha256:
        raise ValueError("evidence_future_evaluator_identity_mismatch")
    metrics, insufficient = _future_metrics(
        population,
        capture,
        drain,
        candidate,
        target_notional=target_notional,
        external_incidents=external_incidents,
    )
    if insufficient:
        terminal = "INSUFFICIENT_EVIDENCE"
        reasons = tuple(sorted(set(insufficient)))
    elif metrics.primary_confidence_lower_bound_bps is None:
        terminal = "INSUFFICIENT_EVIDENCE"
        reasons = ("primary_confidence_bound_missing",)
    elif metrics.primary_confidence_lower_bound_bps > candidate.statistics.stressed_hurdle_bps:
        terminal = "PROMOTE"
        reasons = ("primary_confidence_lower_bound_above_hurdle",)
    else:
        terminal = "HOLD"
        reasons = ("primary_confidence_lower_bound_not_above_hurdle",)
    return FutureHoldoutResultV1(
        terminal=cast(Any, terminal),
        binding=candidate.binding,
        venue=candidate.venue,
        candidate_receipt_sha256=candidate_receipt.receipt_sha256,
        protocol_sha256=candidate.protocol_sha256,
        sealed_corpus_sha256=candidate.sealed_corpus_sha256,
        future_capture_sha256=capture.capture_sha256,
        future_drain_sha256=drain.drain_sha256,
        evaluator_program_sha256=population.evaluator_program_sha256,
        evaluated_at_ms=evaluated_at_ms,
        metrics=metrics,
        reasons=reasons,
    )


def _evaluate_population(
    capture: EvidenceCaptureArtifactV1,
    drain: EvidenceDrainArtifactV1,
    *,
    admission: AdmissionConfig,
    policy: CapitalPolicy,
    price_window: PriceWindow,
    target_notional: Decimal,
    run_episode: BarEpisodeRunner,
) -> _EvaluatedPopulation:
    raw_rows = [cast(OiCandidateRow, dict(row.raw_source)) for row in capture.sources]
    report = replay_oi_facts(
        raw_rows,
        admission=admission,
        policy=policy,
        now_ms=capture.spec.end_ms,
    )
    parsed = parse_replay_sources(raw_rows)
    capture_by_source = {row.source_identity: row for row in capture.sources}

    def lookup(base_symbol: str, venue: str, observed_at_ms: int) -> Sequence[InstrumentCandidateRow]:
        matches = [
            row
            for row in capture.sources
            if row.canonical_asset == base_symbol and row.venue == venue and row.observed_at_ms == observed_at_ms
        ]
        if len(matches) != 1:
            return ()
        return tuple(_catalog_candidate(row) for row in matches[0].catalog.rows)

    plans = plan_replay_scenarios(
        report.outcomes,
        parsed,
        policy=policy,
        requested_venues=("binance.perp", "hl.perp"),
        instruments=lookup,
    )
    drain_by_source = {row.source_identity: row for row in drain.rows}
    slices = [_slice_for_plan(plan, drain_by_source[plan.source.source_key]) for plan in plans.plans]
    evaluated = evaluate_replay_market_slices(
        slices,
        policy=policy,
        snapshot=None,
        blacklist=BlacklistSnapshotV1(revision=0, active_rows=()),
        run_episode=run_episode,
        price_window=price_window,
        target_notional=target_notional,
    )
    episodes = plans.immediate + evaluated
    episodes.sort(key=lambda row: row.source_identity)
    if {row.source_identity for row in episodes} != set(capture_by_source):
        raise ValueError("evidence_corpus_terminal_accounting_invalid")

    normalized = _normalized_rows(capture, drain, parsed)
    features = _feature_rows(capture, drain, parsed, episodes)
    coverage = _coverage(normalized, drain)
    raw = tuple(capture.sources)
    normalized_tuple = tuple(normalized)
    features_tuple = tuple(features)
    episodes_tuple = tuple(episodes)
    evaluator_sha = canonical_sha256(
        {
            "version": EVIDENCE_EVALUATOR_VERSION,
            "bar_fidelity": BAR_FIDELITY_VERSION,
            "policy_identity": replay_policy_identity(policy),
            "admission_config_sha256": admission.digest,
            "source_contract_sha256": source_contract_sha256(),
            "feature_contract_sha256": _feature_contract_sha256(admission, policy, price_window),
        }
    )
    return _EvaluatedPopulation(
        raw=raw,
        normalized=normalized_tuple,
        features=features_tuple,
        episodes=episodes_tuple,
        coverage=coverage,
        evaluator_program_sha256=evaluator_sha,
    )


def _validate_corpus_inputs(capture: EvidenceCaptureArtifactV1, drain: EvidenceDrainArtifactV1) -> None:
    if capture.spec.partition != "discovery" or drain.partition != "discovery":
        raise ValueError("evidence_discovery_partition_required")
    if drain.capture_sha256 != capture.capture_sha256:
        raise ValueError("evidence_corpus_capture_drain_mismatch")
    capture_ids = {row.source_identity for row in capture.sources}
    drain_ids = {row.source_identity for row in drain.rows}
    if capture_ids != drain_ids:
        raise ValueError("evidence_corpus_drain_conservation_failed")
    if drain.drained_at_ms < capture.spec.end_ms + drain.max_horizon_ms + drain.finalization_lag_ms:
        raise ValueError("evidence_corpus_drain_premature")


def _validate_future_inputs(
    capture: EvidenceCaptureArtifactV1,
    drain: EvidenceDrainArtifactV1,
    candidate: CandidateLockedV1,
    receipt: CandidateDecisionReceiptV1,
    *,
    evaluated_at_ms: int,
) -> None:
    protocol = candidate.statistics
    if (
        receipt.terminal != "CANDIDATE_LOCKED"
        or receipt.protocol_sha256 != candidate.protocol_sha256
        or receipt.artifact_sha256 != candidate.protocol_sha256
        or candidate.locked_at_ms > receipt.created_at_ms
        or receipt.created_at_ms >= protocol.future_start_ms
    ):
        raise ValueError("evidence_future_candidate_receipt_mismatch")
    if receipt.binding != candidate.binding or receipt.sealed_corpus_sha256 != candidate.sealed_corpus_sha256:
        raise ValueError("evidence_future_candidate_authority_mismatch")
    if capture.spec.partition != "future" or drain.partition != "future":
        raise ValueError("evidence_future_partition_required")
    if (
        capture.spec.target_binding != candidate.binding
        or capture.spec.protocol_receipt_sha256 != receipt.receipt_sha256
        or capture.spec.protocol_locked_at_ms != receipt.created_at_ms
        or capture.spec.start_ms != protocol.future_start_ms
        or capture.spec.end_ms != protocol.future_end_ms
    ):
        raise ValueError("evidence_future_capture_protocol_mismatch")
    if drain.capture_sha256 != capture.capture_sha256:
        raise ValueError("evidence_future_capture_drain_mismatch")
    if (
        drain.max_horizon_ms != protocol.max_horizon_ms
        or drain.bar_interval_ms != protocol.bar_interval_ms
        or drain.funding_horizon_ms != protocol.max_horizon_ms
        or drain.finalization_lag_ms != protocol.data_finalization_lag_ms
    ):
        raise ValueError("evidence_future_drain_protocol_mismatch")
    if drain.drained_at_ms < protocol.drain_cutoff_ms or evaluated_at_ms < protocol.drain_cutoff_ms:
        raise ValueError("evidence_future_unblind_premature")
    if drain.cost_model_sha256 != candidate.execution.cost_model_sha256:
        raise ValueError("evidence_future_cost_model_mismatch")
    if {row.source_identity for row in capture.sources} != {row.source_identity for row in drain.rows}:
        raise ValueError("evidence_future_drain_conservation_failed")


def _future_metrics(
    population: _EvaluatedPopulation,
    capture: EvidenceCaptureArtifactV1,
    drain: EvidenceDrainArtifactV1,
    candidate: CandidateLockedV1,
    *,
    target_notional: Decimal,
    external_incidents: Sequence[EvidenceIncident],
) -> tuple[FutureHoldoutMetricsV1, list[str]]:
    protocol = candidate.statistics
    source_by_id = {row.source_identity: row for row in capture.sources}
    drain_by_id = {row.source_identity: row for row in drain.rows}
    returns: list[tuple[str, Decimal]] = []
    incomplete_reasons: Counter[str] = Counter()
    for outcome in population.episodes:
        market = drain_by_id[outcome.source_identity]
        if outcome.decision != "DIRECTIONAL" or outcome.execution != "CLOSED":
            incomplete_reasons[f"episode:{outcome.execution_reason}"] += 1
            continue
        if outcome.net_excluding_funding is None:
            incomplete_reasons["episode:net_excluding_funding_missing"] += 1
            continue
        if market.funding_input.state not in {"AVAILABLE", "CORRECTED"}:
            incomplete_reasons["input:funding_missing"] += 1
            continue
        if outcome.opened_at_ms is None or outcome.closed_at_ms is None or outcome.closed_at_ms <= outcome.opened_at_ms:
            incomplete_reasons["episode:holding_window_missing"] += 1
            continue
        holding_funding_bps = _holding_funding_return_bps(outcome, market, target_notional=target_notional)
        if holding_funding_bps is None:
            incomplete_reasons["input:funding_mark_missing"] += 1
            continue
        net_bps = (
            outcome.net_excluding_funding / target_notional * Decimal(10_000)
            + holding_funding_bps
            - candidate.execution.additional_stressed_cost_bps
        )
        returns.append((outcome.source_identity, net_bps))

    source_count = len(capture.sources)
    required_complete = 0
    for row in drain.rows:
        inputs = {
            "bars": row.bars_input,
            "funding": row.funding_input,
        }
        if all(inputs[name].state in {"AVAILABLE", "CORRECTED"} for name in protocol.required_inputs):
            required_complete += 1
        for name in protocol.required_inputs:
            if inputs[name].state not in {"AVAILABLE", "CORRECTED"}:
                incomplete_reasons[f"input:{name}_{inputs[name].state.lower()}"] += 1
    coverage_bps = 0 if source_count == 0 else required_complete * 10_000 // source_count
    missingness_bps = 10_000 - coverage_bps
    ordered_returns = sorted(
        returns,
        key=lambda item: (source_by_id[item[0]].observed_at_ms, item[0]),
    )
    values = [value for _, value in ordered_returns]
    mean = None if not values else sum(values, Decimal(0)) / Decimal(len(values))
    by_day: dict[int, list[Decimal]] = defaultdict(list)
    for source_id, value in ordered_returns:
        by_day[source_by_id[source_id].observed_at_ms // 86_400_000].append(value)
    effective_n = _effective_block_count(tuple(by_day), block_days=protocol.bootstrap_block_days)
    power_bps = _estimated_power_bps(candidate, effective_n=effective_n)
    insufficient: list[str] = []
    if effective_n < protocol.minimum_effective_n:
        insufficient.append("minimum_effective_n_not_met")
    if power_bps < protocol.minimum_power_bps:
        insufficient.append("minimum_power_not_met")
    if coverage_bps < protocol.minimum_coverage_bps:
        insufficient.append("minimum_coverage_not_met")
    if missingness_bps > protocol.maximum_missingness_bps:
        insufficient.append("maximum_missingness_exceeded")
    if any(row.known_at_ms < row.observed_at_ms for row in capture.sources):
        insufficient.append("clock_or_known_at_violation")
    lower = None if not values else _bootstrap_lower_bound(by_day, candidate)
    mfe_values = [Decimal(row.mfe_bps) for row in population.episodes if row.mfe_bps is not None]
    mae_values = [Decimal(row.mae_bps) for row in population.episodes if row.mae_bps is not None]
    concentrations = _concentrations(ordered_returns, source_by_id)
    if concentrations["max_symbol"] > candidate.execution.max_symbol_concentration_bps:
        insufficient.append("maximum_symbol_concentration_exceeded")
    if concentrations["max_day"] > candidate.execution.max_day_concentration_bps:
        insufficient.append("maximum_day_concentration_exceeded")
    allowed_external_incidents = {"protection_contract_invalid", "clock_or_known_at_violation"}
    if set(external_incidents).difference(allowed_external_incidents):
        raise ValueError("evidence_future_external_incident_invalid")
    observed_incidents = {incident for row in drain.rows for incident in row.incidents}
    observed_incidents.update(external_incidents)
    if any(row.corrections for row in drain.rows):
        observed_incidents.add("provider_correction")
    insufficient.extend(
        f"{incident}_incident"
        for incident in sorted(observed_incidents)
        if protocol.incident_handling[cast(Any, incident)] == "INSUFFICIENT_EVIDENCE"
    )
    if missingness_bps > protocol.maximum_missingness_bps and (
        protocol.incident_handling["source_mass_missingness"] == "INSUFFICIENT_EVIDENCE"
    ):
        insufficient.append("source_mass_missingness_incident")
    if incomplete_reasons and (protocol.incident_handling["bar_or_funding_missing"] == "INSUFFICIENT_EVIDENCE"):
        insufficient.append("bar_or_funding_missing_incident")
    drawdown = _max_drawdown(values)
    capacity_values = [
        Decimal(str(source_by_id[source_id].raw_source["oi_value_usd"])) for source_id, _ in ordered_returns
    ]
    metrics = FutureHoldoutMetricsV1(
        source_count=source_count,
        effective_n=effective_n,
        estimated_power_bps=power_bps,
        coverage_bps=coverage_bps,
        missingness_bps=missingness_bps,
        mean_net_including_funding_return_bps=mean,
        benchmark_excess_bps=mean,
        primary_confidence_lower_bound_bps=lower,
        mfe_bps={"p50": _percentile(mfe_values, 5_000), "p95": _percentile(mfe_values, 9_500)},
        mae_bps={"p50": _percentile(mae_values, 5_000), "p05": _percentile(mae_values, 500)},
        max_drawdown_bps=drawdown,
        tail_loss_bps=_percentile(values, 500),
        turnover=None if not values else Decimal(len(values)),
        capacity_proxy=None if not capacity_values else min(capacity_values),
        concentration_bps=concentrations,
        missing_by_reason=dict(sorted(incomplete_reasons.items())),
        sensitivity={
            "mean_without_additional_stress_bps": (
                None if mean is None else mean + candidate.execution.additional_stressed_cost_bps
            ),
            "stressed_hurdle_bps": candidate.statistics.stressed_hurdle_bps,
        },
    )
    return metrics, insufficient


def _holding_funding_return_bps(
    outcome: ReplayTerminalOutcomeV1,
    market: MarketDrainRowV1,
    *,
    target_notional: Decimal,
) -> Decimal | None:
    if outcome.opened_at_ms is None or outcome.closed_at_ms is None or outcome.quantity is None:
        return None
    total = Decimal(0)
    for rate in market.funding_rates:
        if not outcome.opened_at_ms <= rate.funding_at_ms < outcome.closed_at_ms:
            continue
        mark = next((bar.close for bar in market.bars if bar.close_at_ms >= rate.funding_at_ms), None)
        if mark is None:
            return None
        total -= rate.funding_rate * outcome.quantity * mark / target_notional * Decimal(10_000)
    return total


def _effective_block_count(days: Sequence[int], *, block_days: int) -> int:
    """Conservative independent-unit count for the preregistered UTC-day block design."""

    if not days:
        return 0
    return max(1, len(set(days)) // block_days)


def _estimated_power_bps(candidate: CandidateLockedV1, *, effective_n: int) -> int:
    """Pre-result power from the locked MDE and variance assumption, never observed returns."""

    if effective_n == 0:
        return 0
    protocol = candidate.statistics
    standardized = (
        math.sqrt(effective_n)
        * float(protocol.minimum_detectable_excess_bps)
        / float(protocol.assumed_standard_deviation_bps)
    )
    critical = NormalDist().inv_cdf(protocol.confidence_level_bps / 10_000)
    power = NormalDist().cdf(standardized - critical)
    return max(0, min(10_000, int(power * 10_000)))


def _bootstrap_lower_bound(by_day: dict[int, list[Decimal]], candidate: CandidateLockedV1) -> Decimal:
    protocol = candidate.statistics
    days = sorted(by_day)
    samples: list[Decimal] = []
    blocks_per_sample = (len(days) + protocol.bootstrap_block_days - 1) // protocol.bootstrap_block_days
    for sample in range(protocol.bootstrap_samples):
        selected_days: list[int] = []
        for block in range(blocks_per_sample):
            token = f"{protocol.bootstrap_seed}:{sample}:{block}".encode()
            start = int.from_bytes(hashlib.sha256(token).digest()[:8], "big") % len(days)
            selected_days.extend(days[(start + offset) % len(days)] for offset in range(protocol.bootstrap_block_days))
        selected = [value for day in selected_days[: len(days)] for value in by_day[day]]
        samples.append(sum(selected, Decimal(0)) / Decimal(len(selected)))
    samples.sort()
    lower_tail_bps = 10_000 - protocol.confidence_level_bps
    index = min(len(samples) - 1, max(0, len(samples) * lower_tail_bps // 10_000))
    return samples[index]


def _percentile(values: Sequence[Decimal], quantile_bps: int) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, (len(ordered) - 1) * quantile_bps // 10_000))
    return ordered[index]


def _max_drawdown(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    cumulative = Decimal(0)
    peak = Decimal(0)
    drawdown = Decimal(0)
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    return drawdown


def _concentrations(
    returns: Sequence[tuple[str, Decimal]],
    source_by_id: dict[str, CapturedSourceV1],
) -> dict[str, int]:
    if not returns:
        return {"max_day": 0, "max_symbol": 0, "right_tail": 0}
    day = Counter(source_by_id[source_id].observed_at_ms // 86_400_000 for source_id, _ in returns)
    symbol = Counter(source_by_id[source_id].canonical_asset for source_id, _ in returns)
    positive = sum(value > 0 for _, value in returns)
    total = len(returns)
    return {
        "max_day": max(day.values()) * 10_000 // total,
        "max_symbol": max(symbol.values()) * 10_000 // total,
        "right_tail": positive * 10_000 // total,
    }


def _catalog_candidate(row: PointInTimeCatalogRowV1) -> InstrumentCandidateRow:
    return InstrumentCandidateRow(
        venue=row.venue,
        venue_symbol=row.venue_symbol,
        base_symbol=row.base_symbol,
        instrument_class=row.instrument_class,
        quote_asset=row.quote_asset,
        status=row.status,
        last_seen_ms=row.observed_at_ms,
    )


def _slice_for_plan(plan: DirectionalReplayPlan, row: MarketDrainRowV1) -> ReplayMarketSlice:
    if row.provider_instrument_id != plan.instrument.provider_symbol or row.venue != plan.venue:
        raise ValueError("evidence_drain_wrong_venue_routing")
    reason = None if row.bars_input.state in {"AVAILABLE", "CORRECTED"} else row.bars_input.reason
    return ReplayMarketSlice(
        plan=plan,
        bars=list(row.bars),
        reason=reason,
        start_ms=row.requested_start_ms,
        end_ms=row.requested_end_ms,
    )


def _normalized_rows(
    capture: EvidenceCaptureArtifactV1,
    drain: EvidenceDrainArtifactV1,
    parsed: dict[str, OiTradeCandidate],
) -> list[NormalizedEvidenceObservationV1]:
    drain_by_source = {row.source_identity: row for row in drain.rows}
    normalized: list[NormalizedEvidenceObservationV1] = []
    for source in capture.sources:
        candidate = normalize_oi_source(cast(OiCandidateRow, source.raw_source))
        missing = tuple(
            name
            for name, value in (
                ("bars", drain_by_source[source.source_identity].bars_input),
                ("funding", drain_by_source[source.source_identity].funding_input),
                ("bid_ask", drain_by_source[source.source_identity].bid_ask_input),
                ("liquidity", drain_by_source[source.source_identity].liquidity_input),
            )
            if value.state not in {"AVAILABLE", "CORRECTED"}
        )
        if isinstance(candidate, SourceRejected):
            disposition, reason, source_sha = "EXCLUDED", candidate.rule, None
        elif source.binding is None:
            disposition, reason, source_sha = "EXCLUDED", "venue_ambiguous", None
        elif source.provider_instrument_id is None:
            disposition, reason, source_sha = "EXCLUDED", "instrument_unresolved", None
        else:
            disposition, reason = "VALID", "source_valid"
            source_sha = canonical_sha256(
                {
                    "source_strategy_id": candidate.source_strategy_id,
                    "source_contract_version": candidate.source_contract_version,
                    "measurement_window_ms": candidate.measurement_window_ms,
                }
            )
        normalized.append(
            NormalizedEvidenceObservationV1(
                source_identity=source.source_identity,
                disposition=cast(Any, disposition),
                reason=reason,
                venue=source.venue,
                binding=source.binding,
                canonical_asset=source.canonical_asset,
                provider_instrument_id=source.provider_instrument_id,
                observed_at_ms=source.observed_at_ms,
                known_at_ms=source.known_at_ms,
                available_at_ms=source.available_at_ms,
                source_contract_sha256=source_sha,
                catalog_sha256=source.catalog.rows_sha256,
                missing_inputs=missing,
            )
        )
    normalized.sort(key=lambda row: row.source_identity)
    if set(parsed) - {row.source_identity for row in normalized}:
        raise ValueError("evidence_corpus_normalization_unknown_source")
    return normalized


def _feature_rows(
    capture: EvidenceCaptureArtifactV1,
    drain: EvidenceDrainArtifactV1,
    parsed: dict[str, OiTradeCandidate],
    episodes: list[Any],
) -> list[DiscoveryFeatureV1]:
    drain_by_source = {row.source_identity: row for row in drain.rows}
    outcome_by_source = {row.source_identity: row for row in episodes}
    groups: dict[tuple[str, int], list[OiTradeCandidate]] = defaultdict(list)
    for source_identity, candidate in parsed.items():
        captured = next(row for row in capture.sources if row.source_identity == source_identity)
        if captured.binding is not None and captured.provider_instrument_id is not None:
            groups[(captured.venue, candidate.observed_at_ms)].append(candidate)
    ranks: dict[str, tuple[int, int]] = {}
    for group in groups.values():
        ordered = sorted(group, key=lambda row: (-row.oi_change_bps, row.base_symbol, row.source_key))
        for index, candidate in enumerate(ordered, 1):
            ranks[candidate.source_key] = (index, len(ordered))

    features: list[DiscoveryFeatureV1] = []
    for source_identity, (rank, size) in ranks.items():
        candidate = parsed[source_identity]
        row = drain_by_source[source_identity]
        returns = _horizon_returns(candidate.observed_at_ms, row.bars)
        missing = tuple(cast(Any, name) for name in _HORIZONS_MS if returns[name] is None)
        outcome = outcome_by_source[source_identity]
        features.append(
            DiscoveryFeatureV1(
                source_identity=source_identity,
                venue=cast(Any, row.venue),
                observed_at_ms=candidate.observed_at_ms,
                canonical_asset=candidate.base_symbol,
                cross_sectional_rank=rank,
                eligible_universe_size=size,
                returns_bps=cast(Any, returns),
                mfe_bps=outcome.mfe_bps,
                mae_bps=outcome.mae_bps,
                missing_horizons=missing,
            )
        )
    features.sort(key=lambda row: row.source_identity)
    return features


def _horizon_returns(observed_at_ms: int, bars: tuple[ReplayBarV1, ...]) -> dict[str, int | None]:
    anchors = [bar.close for bar in bars if bar.close_at_ms <= observed_at_ms]
    anchor = None if not anchors else anchors[-1]
    values: dict[str, int | None] = {}
    for name, horizon_ms in _HORIZONS_MS.items():
        target = observed_at_ms + horizon_ms
        selected = next((bar.close for bar in bars if bar.close_at_ms >= target), None)
        values[name] = None if anchor is None or selected is None else int((selected / anchor - 1) * Decimal(10_000))
    return values


def _coverage(
    normalized: list[NormalizedEvidenceObservationV1],
    drain: EvidenceDrainArtifactV1,
) -> EvidenceCoverageV1:
    missing: Counter[str] = Counter()
    valid = sum(row.disposition == "VALID" for row in normalized)
    for row in normalized:
        if row.disposition == "EXCLUDED":
            missing[f"excluded:{row.reason}"] += 1
        for value in row.missing_inputs:
            missing[f"missing:{value}"] += 1
    drain_by_source = {row.source_identity: row for row in drain.rows}
    complete = sum(
        row.disposition == "VALID"
        and drain_by_source[row.source_identity].bars_input.state in {"AVAILABLE", "CORRECTED"}
        for row in normalized
    )
    total = len(normalized)
    return EvidenceCoverageV1(
        source_count=total,
        valid_count=valid,
        excluded_count=total - valid,
        complete_market_count=complete,
        missing_by_reason=dict(sorted(missing.items())),
        coverage_bps=0 if total == 0 else complete * 10_000 // total,
    )


def _rows_sha(rows: Sequence[Any]) -> str:
    return canonical_sha256(tuple(row.model_dump(mode="json") for row in rows))


def _feature_contract_sha256(
    admission: AdmissionConfig,
    policy: CapitalPolicy,
    price_window: PriceWindow,
) -> str:
    return feature_contract_sha256(
        admission_config_sha256=admission.digest,
        price_window=price_window.as_dict(),
        policy_id=policy.policy_id,
        policy_config_sha256=policy.config_digest,
    )


__all__ = [
    "EVIDENCE_EVALUATOR_VERSION",
    "build_evidence_capture",
    "build_evidence_drain",
    "seal_discovery_corpus",
    "unblind_future_holdout",
]
