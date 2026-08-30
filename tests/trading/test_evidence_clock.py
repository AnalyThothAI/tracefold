"""The #377 evidence clock is point-in-time, deterministic and fail closed."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app.cli.commands import trading_evidence as evidence_cli
from tracefold.integrations.nautilus.replay import run_bar_episode
from tracefold.trading.admission import AdmissionConfig
from tracefold.trading.contract_receipt import build_execution_policy_contract_receipt
from tracefold.trading.contracts import canonical_sha256
from tracefold.trading.evidence_clock import (
    CandidateDecisionReceiptV1,
    CandidateExecutionProtocolV1,
    CandidateLockedV1,
    DiscoveryCorpusReceiptV1,
    EvidenceCaptureArtifactV1,
    EvidenceCaptureSpecV1,
    EvidenceDrainArtifactV1,
    FundingRateV1,
    FutureCaptureBatchV1,
    FutureStatisticalProtocolV1,
    NoCandidateV1,
    PointInTimeCatalogRowV1,
    PointInTimeCatalogV1,
    candidate_selection_program_sha256,
)
from tracefold.trading.evidence_research import (
    build_evidence_capture,
    build_evidence_drain,
    seal_discovery_corpus,
    unblind_future_holdout,
)
from tracefold.trading.market_context import PriceWindow
from tracefold.trading.policy import CapitalPolicy
from tracefold.trading.replay import DirectionalReplayPlan, ReplayBarV1, ReplayMarketSlice
from tracefold.trading.routing import resolve_instrument
from tracefold.trading.sources import SourceRejected, normalize_oi_source

NOW = 1_900_000_000_000


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_id": "event-tut",
        "verdict_created_at_ms": NOW + 1,
        "final_decision": "drop",
        "source_rule": "whale_ratio_below_threshold",
        "source_strategy_id": "1019",
        "source_contract_version": "opennews_oi_source_v1",
        "measurement_window_ms": 300_000,
        "learning_epoch": "bundle_00000000",
        "program_version": "news_oi_signal_v2",
        "program_sha256": "a" * 64,
        "policy_version": "news_triage_policy_v11",
        "judgment_contract_version": "news_judgment_v2",
        "judgment_origin": "oi",
        "judgment_sha256": "c" * 64,
        "runtime_manifest_sha": "d" * 64,
        "metric_version": "oi_signal_v1",
        "symbol": "TUT",
        "direction": "rise",
        "oi_change_bps": 1_548,
        "oi_value_usd": 23_010_000,
        "whale_long_profit_bps": 9_074,
        "whale_oi_ratio_bps": 5_424,
        "rank_in_window": 1,
        "observed_at_ms": NOW,
        "ingest_mode": "live",
        "venue": "binance",
    }
    row.update(overrides)
    return row


def _instruments(_base: str, _venue: str, observed_at_ms: int) -> list[dict[str, Any]]:
    return [
        {
            "venue": "binance.perp",
            "venue_symbol": "TUTUSDT",
            "base_symbol": "TUT",
            "instrument_class": "crypto",
            "quote_asset": "USDT",
            "status": "trading",
            "last_seen_ms": observed_at_ms - 1,
        }
    ]


def _capture(rows: list[dict[str, Any]] | None = None):
    spec = EvidenceCaptureSpecV1(
        partition="discovery",
        start_ms=NOW - 1,
        end_ms=NOW + 2,
        captured_at_ms=NOW + 10,
        source_query_contract_sha256="1" * 64,
    )
    return build_evidence_capture(rows or [_row()], spec=spec, instruments=_instruments)


def _market_slice(**source_overrides: Any) -> ReplayMarketSlice:
    raw = _row(**source_overrides)
    source = normalize_oi_source(raw)  # type: ignore[arg-type]
    assert not isinstance(source, SourceRejected)
    observed_at_ms = int(raw["observed_at_ms"])
    catalog = _instruments("TUT", "binance.perp", observed_at_ms)
    instrument = resolve_instrument(  # type: ignore[arg-type]
        catalog,
        priority=("binance",),
        observed_at_ms=observed_at_ms,
    )
    assert instrument is not None
    plan = DirectionalReplayPlan(
        source=source,
        instrument=instrument,
        venue="binance.perp",
        instrument_id="TUTUSDT-PERP.BINANCE",
    )
    prices = [Decimal("0.090") + Decimal(index) / Decimal("10000") for index in range(79)]
    bars = [
        ReplayBarV1(
            venue="binance.perp",
            instrument_id=plan.instrument_id,
            open_at_ms=observed_at_ms - 7_200_000 + index * 300_000,
            close_at_ms=observed_at_ms - 6_900_000 + index * 300_000,
            open=price,
            high=price,
            low=price,
            close=price,
            volume="10000",
        )
        for index, price in enumerate(prices)
    ]
    return ReplayMarketSlice(
        plan=plan,
        bars=bars,
        reason=None,
        start_ms=observed_at_ms - 7_200_000,
        end_ms=observed_at_ms + 16_500_000,
    )


def test_missing_measurement_is_not_silently_defaulted_to_zero() -> None:
    parsed = normalize_oi_source(_row(oi_value_usd=None))  # type: ignore[arg-type]

    assert isinstance(parsed, SourceRejected)
    assert parsed.rule == "oi_value_usd_missing"


def test_point_in_time_catalog_rejects_a_future_listing() -> None:
    row = PointInTimeCatalogRowV1(
        venue="binance.perp",
        venue_symbol="TUTUSDT",
        base_symbol="TUT",
        quote_asset="USDT",
        observed_at_ms=NOW + 1,
    )
    with pytest.raises(ValueError, match="evidence_catalog_future_leakage"):
        PointInTimeCatalogV1(
            source_observed_at_ms=NOW,
            rows=(row,),
            rows_sha256="0" * 64,
        )


def test_capture_drain_and_seal_are_separate_and_deterministic() -> None:
    capture = _capture()
    cost_model = {"version": "stressed_cost_v1", "taker_bps": "5", "slippage_bps": "5"}
    drain = build_evidence_drain(
        capture,
        market_slices=[_market_slice()],
        drained_at_ms=capture.spec.end_ms + 16_500_000,
        max_horizon_ms=14_400_000,
        bar_interval_ms=300_000,
        funding_horizon_ms=180_000,
        finalization_lag_ms=2_100_000,
        cost_model=cost_model,
    )
    kwargs = {
        "contract": build_execution_policy_contract_receipt(),
        "admission": AdmissionConfig(min_oi_value_usd=5_000_000),
        "policy": CapitalPolicy(),
        "price_window": PriceWindow(),
        "target_notional": Decimal("10"),
        "run_episode": run_bar_episode,
    }

    first = seal_discovery_corpus(capture, drain, **kwargs)
    second = seal_discovery_corpus(capture, drain, **kwargs)

    assert first.corpus_sha256 == second.corpus_sha256
    assert first.terminal == "SOURCE_FEATURE_DISCOVERY_CORPUS_V1_SEALED"
    assert first.coverage.source_count == 1
    assert first.coverage.complete_market_count == 1
    assert first.cost_model_sha256 == drain.cost_model_sha256
    assert len(first.raw_observations) == len(first.normalized_observations) == len(first.episodes) == 1
    assert first.normalized_observations[0].missing_inputs == ("funding", "bid_ask", "liquidity")
    assert first.episodes[0].capital_admission == "NOT_APPLICABLE"


def test_drain_preserves_exact_funding_rates_and_long_cashflow_sign() -> None:
    capture = _capture()
    source_identity = capture.sources[0].source_identity
    funding = FundingRateV1(
        venue="binance.perp",
        provider_instrument_id="TUTUSDT",
        funding_at_ms=NOW + 1,
        funding_rate="0.0001",
    )
    drain = build_evidence_drain(
        capture,
        market_slices=[_market_slice()],
        funding_rates_by_source={source_identity: (funding,)},
        drained_at_ms=NOW + 16_500_002,
        max_horizon_ms=14_400_000,
        bar_interval_ms=300_000,
        funding_horizon_ms=180_000,
        finalization_lag_ms=2_100_000,
        cost_model={"version": "test"},
    )

    row = drain.rows[0]
    assert row.funding_input.state == "AVAILABLE"
    assert row.funding_rates == (funding,)
    assert row.funding_window_sum_bps == Decimal("-1.0000")


def test_drain_marks_a_middle_bar_gap_missing_instead_of_evaluating_partial_history() -> None:
    capture = _capture()
    complete = _market_slice()
    broken = ReplayMarketSlice(
        plan=complete.plan,
        bars=[bar for index, bar in enumerate(complete.bars) if index != 20],
        reason=None,
        start_ms=complete.start_ms,
        end_ms=complete.end_ms,
    )

    drain = build_evidence_drain(
        capture,
        market_slices=[broken],
        drained_at_ms=NOW + 16_500_002,
        max_horizon_ms=14_400_000,
        bar_interval_ms=300_000,
        funding_horizon_ms=180_000,
        finalization_lag_ms=2_100_000,
        cost_model={"version": "test"},
    )

    assert drain.rows[0].bars
    assert drain.rows[0].bars_input.state == "MISSING"
    assert drain.rows[0].bars_input.reason == "bar_history_incomplete"


def test_capture_and_drain_reject_clock_or_payload_identity_tampering() -> None:
    capture = _capture()
    capture_payload = capture.model_dump(mode="json")
    capture_payload["sources"][0]["available_at_ms"] = capture.spec.captured_at_ms + 1
    with pytest.raises(ValueError, match="evidence_capture_availability_clock_mismatch"):
        EvidenceCaptureArtifactV1.model_validate(capture_payload)

    drain = build_evidence_drain(
        capture,
        market_slices=[_market_slice()],
        drained_at_ms=NOW + 16_500_002,
        max_horizon_ms=14_400_000,
        bar_interval_ms=300_000,
        funding_horizon_ms=180_000,
        finalization_lag_ms=2_100_000,
        cost_model={"version": "test"},
    )
    drain_payload = drain.model_dump(mode="json")
    drain_payload["rows"][0]["bars_input"]["value_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="evidence_drain_bar_identity_invalid"):
        EvidenceDrainArtifactV1.model_validate(drain_payload)


def test_future_capture_batch_records_schedule_and_collection_health() -> None:
    capture = _capture()
    source = capture.sources[0]
    batch = FutureCaptureBatchV1(
        binding="BINANCE_USDM",
        candidate_receipt_sha256="1" * 64,
        protocol_sha256="2" * 64,
        batch_start_ms=NOW - 1,
        batch_end_ms=NOW + 2,
        captured_at_ms=NOW + 10,
        capture_lag_ms=8,
        sources=(source,),
        source_count=1,
        late_source_count=0,
        catalog_missing_count=0,
    )

    payload = batch.model_dump(mode="json")
    payload["late_source_count"] = 1
    with pytest.raises(ValueError, match="evidence_future_batch_health_invalid"):
        FutureCaptureBatchV1.model_validate(payload)


def test_public_evidence_handler_uses_database_clock_for_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    args = SimpleNamespace(evidence_command="verify")
    monkeypatch.setattr(evidence_cli, "_database_now_ms", lambda _settings: NOW + 99)
    monkeypatch.setattr(
        evidence_cli,
        "_verify",
        lambda _settings, _args, *, now_ms: (0, {"ok": True, "data": {"verified_at_ms": now_ms}}),
    )

    code, answer = evidence_cli.handle_trading_evidence(SimpleNamespace(), args, now_ms=1)

    assert code == 0
    assert answer["data"]["verified_at_ms"] == NOW + 99


def test_future_partition_refuses_discovery_overlap_and_premature_unblind() -> None:
    spec = EvidenceCaptureSpecV1(
        partition="future",
        start_ms=NOW,
        end_ms=NOW + 1_000,
        captured_at_ms=NOW + 2_000,
        target_binding="BINANCE_USDM",
        source_query_contract_sha256="1" * 64,
        protocol_receipt_sha256="2" * 64,
        protocol_locked_at_ms=NOW - 1,
    )
    capture = build_evidence_capture(
        [_row(observed_at_ms=NOW + 1, verdict_created_at_ms=NOW + 2)],
        spec=spec,
        instruments=_instruments,
    )
    assert capture.spec.partition == "future"
    with pytest.raises(ValueError, match="evidence_future_source_known_after_capture_cutoff"):
        build_evidence_capture(
            [_row(observed_at_ms=NOW + 1, verdict_created_at_ms=spec.end_ms + 1)],
            spec=spec,
            instruments=_instruments,
        )

    drain = build_evidence_drain(
        capture,
        market_slices=[],
        drained_at_ms=NOW + 2_000,
        max_horizon_ms=14_400_000,
        bar_interval_ms=300_000,
        funding_horizon_ms=180_000,
        finalization_lag_ms=1,
        cost_model={"version": "test"},
    )
    with pytest.raises(ValueError, match="evidence_discovery_partition_required"):
        seal_discovery_corpus(
            capture,
            drain,
            contract=build_execution_policy_contract_receipt(),
            admission=AdmissionConfig(),
            policy=CapitalPolicy(),
            price_window=PriceWindow(),
            target_notional=Decimal("10"),
            run_episode=run_bar_episode,
        )


def test_future_drain_cli_refuses_provider_io_before_the_locked_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = SimpleNamespace(spec=SimpleNamespace(partition="future", target_binding="BINANCE_USDM"))
    candidate = SimpleNamespace(
        binding="BINANCE_USDM",
        statistics=SimpleNamespace(drain_cutoff_ms=NOW + 10),
    )
    receipt = SimpleNamespace(receipt_sha256="1" * 64, created_at_ms=NOW)
    capture.spec.protocol_receipt_sha256 = receipt.receipt_sha256
    capture.spec.protocol_locked_at_ms = receipt.created_at_ms
    monkeypatch.setattr(evidence_cli, "load_evidence_artifact", lambda *_args, **_kwargs: capture)
    monkeypatch.setattr(evidence_cli, "_load_candidate_pair", lambda _args: (candidate, receipt))

    async def provider_io_must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("provider I/O ran before the future drain cutoff")

    monkeypatch.setattr(evidence_cli, "_fetch_market_inputs", provider_io_must_not_run)
    args = SimpleNamespace(capture="capture.json", candidate="candidate.json", candidate_receipt="receipt.json")

    with pytest.raises(ValueError, match="evidence_future_drain_premature"):
        evidence_cli._drain(SimpleNamespace(), args, now_ms=NOW)


def test_candidate_registration_rejects_an_unknown_selection_program(monkeypatch: pytest.MonkeyPatch) -> None:
    corpus_sha256 = "4" * 64
    receipt = DiscoveryCorpusReceiptV1(
        corpus_sha256=corpus_sha256,
        artifact_sha256=corpus_sha256,
        artifact_path="corpus.json",
        capture_sha256="1" * 64,
        drain_sha256="2" * 64,
        execution_contract_receipt_sha256="3" * 64,
        source_count=0,
        created_at_ms=NOW,
    )
    trading = SimpleNamespace(
        evidence_clock_receipt_for_artifact=lambda *_args, **_kwargs: {
            "payload": {"receipt": receipt.model_dump(mode="json")}
        }
    )

    class RepositoryContext:
        def __enter__(self) -> SimpleNamespace:
            return SimpleNamespace(trading=trading)

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(evidence_cli, "repositories", lambda *_args, **_kwargs: RepositoryContext())
    monkeypatch.setattr(
        evidence_cli,
        "load_evidence_artifact",
        lambda *_args, **_kwargs: SimpleNamespace(corpus_sha256=corpus_sha256),
    )
    decision = NoCandidateV1(
        binding="BINANCE_USDM",
        venue="binance.usdm",
        sealed_corpus_sha256=corpus_sha256,
        corpus_artifact_sha256=corpus_sha256,
        selection_program_sha256="5" * 64,
        selection_evidence_sha256="6" * 64,
        reason="no_complete_directional_discovery_episode",
        decided_at_ms=NOW + 1,
        decided_by="test-operator",
    )

    with pytest.raises(ValueError, match="evidence_candidate_selection_program_mismatch"):
        evidence_cli._validate_candidate_registration(SimpleNamespace(), decision)


def _candidate(corpus_sha256: str, evaluator_sha256: str) -> tuple[CandidateLockedV1, dict[str, Any]]:
    contract = build_execution_policy_contract_receipt()
    cost_model: dict[str, Any] = {
        "fee_model": {"version": "nautilus_bar_taker_fee_v1", "per_side_bps": "5"},
        "funding_model": {
            "version": "provider_history_replay_holding_v1",
            "mark_price": "first_5m_close_at_or_after_funding_event",
        },
        "spread_slippage_model": {"version": "additional_stress_bps_v1"},
        "latency_model": {"version": "first_closed_5m_bar_at_or_after_known_at_v1"},
        "additional_stressed_cost_bps": "2",
    }
    execution = CandidateExecutionProtocolV1(
        intent_ttl_ms=60_000,
        target_notional="10",
        max_risk_amount="0.25",
        quote_contract_sha256=contract.quote_contract_sha256,
        max_quote_age_ms=2_000,
        max_spread_bps=30,
        max_entry_drift_bps=25,
        stop_loss_bps=200,
        protection_contract_sha256=contract.protection_contract_sha256,
        max_holding_ms=180_000,
        execution_policy_sha256=contract.execution_policy_sha256,
        adapter_contract_sha256=contract.adapter_contract_sha256["BINANCE_USDM"],
        capability_requirements=("execution_eligible", "protection_eligible"),
        fee_model=cost_model["fee_model"],
        funding_model=cost_model["funding_model"],
        spread_slippage_model=cost_model["spread_slippage_model"],
        latency_model=cost_model["latency_model"],
        additional_stressed_cost_bps="2",
        cost_model_sha256=canonical_sha256(cost_model),
        benchmark="zero_return_bps_v1",
        max_symbol_concentration_bps=10_000,
        max_day_concentration_bps=10_000,
    )
    incidents = {
        "venue_provider_outage": "INSUFFICIENT_EVIDENCE",
        "source_mass_missingness": "INSUFFICIENT_EVIDENCE",
        "catalog_reset_or_delist": "INSUFFICIENT_EVIDENCE",
        "provider_correction": "INSUFFICIENT_EVIDENCE",
        "bar_or_funding_missing": "INSUFFICIENT_EVIDENCE",
        "protection_contract_invalid": "INSUFFICIENT_EVIDENCE",
        "clock_or_known_at_violation": "INSUFFICIENT_EVIDENCE",
    }
    statistics = FutureStatisticalProtocolV1(
        future_start_ms=NOW + 10_000,
        future_end_ms=NOW + 20_000,
        capture_cutoff_ms=NOW + 20_000,
        capture_interval_ms=300_000,
        maximum_capture_lag_ms=300_000,
        max_horizon_ms=14_400_000,
        data_finalization_lag_ms=2_100_000,
        drain_cutoff_ms=NOW + 16_520_000,
        secondary_diagnostics=("concentration", "drawdown", "mfe_mae"),
        stressed_hurdle_bps="1",
        confidence_level_bps=9_500,
        bootstrap_block_days=1,
        bootstrap_samples=100,
        bootstrap_seed=377,
        minimum_effective_n=1,
        minimum_detectable_excess_bps=100,
        assumed_standard_deviation_bps=1,
        minimum_power_bps=9_000,
        minimum_coverage_bps=10_000,
        maximum_missingness_bps=0,
        incident_handling=incidents,
    )
    return (
        CandidateLockedV1(
            binding="BINANCE_USDM",
            venue="binance.usdm",
            sealed_corpus_sha256=corpus_sha256,
            corpus_artifact_sha256=corpus_sha256,
            discovery_start_ms=NOW - 1,
            discovery_end_ms=NOW + 2,
            source_contract_sha256="4" * 64,
            feature_contract_sha256="5" * 64,
            point_in_time_catalog_sha256="6" * 64,
            eligible_universe_sha256="7" * 64,
            selection_program_sha256=candidate_selection_program_sha256(),
            selection_evidence_sha256="8" * 64,
            policy_id="source_native_oi_smart_money_long_v3",
            policy_config_sha256=CapitalPolicy().config_digest,
            execution_contract_receipt_sha256=contract.receipt_sha256,
            execution=execution,
            statistics=statistics,
            evaluator_program_sha256=evaluator_sha256,
            locked_at_ms=NOW + 5_000,
            preregistered_by="test-operator",
        ),
        cost_model,
    )


def test_candidate_execution_rejects_a_labelled_but_unimplemented_cost_model() -> None:
    candidate, _ = _candidate("1" * 64, "2" * 64)
    payload = candidate.execution.model_dump(mode="json")
    payload["fee_model"] = {"version": "claimed_maker_fee_v1", "per_side_bps": "1"}
    cost_payload = {
        "fee_model": payload["fee_model"],
        "funding_model": payload["funding_model"],
        "spread_slippage_model": payload["spread_slippage_model"],
        "latency_model": payload["latency_model"],
        "additional_stressed_cost_bps": payload["additional_stressed_cost_bps"],
    }
    payload["cost_model_sha256"] = canonical_sha256(cost_payload)

    with pytest.raises(ValueError, match="evidence_candidate_cost_model_unsupported"):
        CandidateExecutionProtocolV1.model_validate(payload)


def test_future_unblind_is_protocol_locked_and_missing_funding_is_insufficient() -> None:
    discovery_capture = _capture()
    discovery_drain = build_evidence_drain(
        discovery_capture,
        market_slices=[_market_slice()],
        drained_at_ms=discovery_capture.spec.end_ms + 16_500_000,
        max_horizon_ms=14_400_000,
        bar_interval_ms=300_000,
        funding_horizon_ms=180_000,
        finalization_lag_ms=2_100_000,
        cost_model={"version": "discovery"},
    )
    corpus = seal_discovery_corpus(
        discovery_capture,
        discovery_drain,
        contract=build_execution_policy_contract_receipt(),
        admission=AdmissionConfig(min_oi_value_usd=5_000_000),
        policy=CapitalPolicy(),
        price_window=PriceWindow(),
        target_notional=Decimal("10"),
        run_episode=run_bar_episode,
    )
    candidate, cost_model = _candidate(corpus.corpus_sha256, corpus.evaluator_program_sha256)
    receipt = CandidateDecisionReceiptV1(
        terminal="CANDIDATE_LOCKED",
        binding=candidate.binding,
        sealed_corpus_sha256=candidate.sealed_corpus_sha256,
        artifact_sha256=candidate.protocol_sha256,
        artifact_path="candidate.json",
        protocol_sha256=candidate.protocol_sha256,
        created_at_ms=candidate.locked_at_ms,
    )
    future_spec = EvidenceCaptureSpecV1(
        partition="future",
        start_ms=candidate.statistics.future_start_ms,
        end_ms=candidate.statistics.future_end_ms,
        captured_at_ms=candidate.statistics.future_end_ms,
        target_binding=candidate.binding,
        source_query_contract_sha256="9" * 64,
        protocol_receipt_sha256=receipt.receipt_sha256,
        protocol_locked_at_ms=candidate.locked_at_ms,
    )
    future_capture = build_evidence_capture(
        [
            _row(
                event_id="future-tut",
                observed_at_ms=candidate.statistics.future_start_ms + 1,
                verdict_created_at_ms=candidate.statistics.future_start_ms + 2,
            )
        ],
        spec=future_spec,
        instruments=_instruments,
    )
    future_drain = build_evidence_drain(
        future_capture,
        market_slices=[],
        drained_at_ms=candidate.statistics.drain_cutoff_ms,
        max_horizon_ms=candidate.statistics.max_horizon_ms,
        bar_interval_ms=candidate.statistics.bar_interval_ms,
        funding_horizon_ms=candidate.statistics.max_horizon_ms,
        finalization_lag_ms=candidate.statistics.data_finalization_lag_ms,
        cost_model=cost_model,
    )
    wrong_funding_horizon = future_drain.model_copy(
        update={"funding_horizon_ms": candidate.statistics.max_horizon_ms - 1}
    )
    kwargs = {
        "candidate": candidate,
        "candidate_receipt": receipt,
        "admission": AdmissionConfig(min_oi_value_usd=5_000_000),
        "policy": CapitalPolicy(),
        "price_window": PriceWindow(),
        "target_notional": Decimal("10"),
        "run_episode": run_bar_episode,
    }
    with pytest.raises(ValueError, match="evidence_future_unblind_premature"):
        unblind_future_holdout(
            future_capture,
            future_drain,
            evaluated_at_ms=candidate.statistics.drain_cutoff_ms - 1,
            **kwargs,
        )
    with pytest.raises(ValueError, match="evidence_future_drain_protocol_mismatch"):
        unblind_future_holdout(
            future_capture,
            wrong_funding_horizon,
            evaluated_at_ms=candidate.statistics.drain_cutoff_ms,
            **kwargs,
        )

    first = unblind_future_holdout(
        future_capture,
        future_drain,
        evaluated_at_ms=candidate.statistics.drain_cutoff_ms,
        **kwargs,
    )
    second = unblind_future_holdout(
        future_capture,
        future_drain,
        evaluated_at_ms=candidate.statistics.drain_cutoff_ms,
        **kwargs,
    )

    assert first.terminal == "INSUFFICIENT_EVIDENCE"
    assert "bar_or_funding_missing_incident" in first.reasons
    assert first.report_sha256 == second.report_sha256


def test_future_long_return_adds_the_signed_funding_cashflow() -> None:
    discovery_capture = _capture()
    discovery_drain = build_evidence_drain(
        discovery_capture,
        market_slices=[_market_slice()],
        drained_at_ms=discovery_capture.spec.end_ms + 16_500_000,
        max_horizon_ms=14_400_000,
        bar_interval_ms=300_000,
        funding_horizon_ms=180_000,
        finalization_lag_ms=2_100_000,
        cost_model={"version": "discovery"},
    )
    corpus = seal_discovery_corpus(
        discovery_capture,
        discovery_drain,
        contract=build_execution_policy_contract_receipt(),
        admission=AdmissionConfig(min_oi_value_usd=5_000_000),
        policy=CapitalPolicy(),
        price_window=PriceWindow(),
        target_notional=Decimal("10"),
        run_episode=run_bar_episode,
    )
    candidate, cost_model = _candidate(corpus.corpus_sha256, corpus.evaluator_program_sha256)
    receipt = CandidateDecisionReceiptV1(
        terminal="CANDIDATE_LOCKED",
        binding=candidate.binding,
        sealed_corpus_sha256=candidate.sealed_corpus_sha256,
        artifact_sha256=candidate.protocol_sha256,
        artifact_path="candidate.json",
        protocol_sha256=candidate.protocol_sha256,
        created_at_ms=candidate.locked_at_ms,
    )
    observed_at_ms = candidate.statistics.future_start_ms + 1
    event_id = "future-funded-tut"
    future_capture = build_evidence_capture(
        [_row(event_id=event_id, observed_at_ms=observed_at_ms, verdict_created_at_ms=observed_at_ms + 1)],
        spec=EvidenceCaptureSpecV1(
            partition="future",
            start_ms=candidate.statistics.future_start_ms,
            end_ms=candidate.statistics.future_end_ms,
            captured_at_ms=candidate.statistics.future_end_ms,
            target_binding=candidate.binding,
            source_query_contract_sha256="9" * 64,
            protocol_receipt_sha256=receipt.receipt_sha256,
            protocol_locked_at_ms=receipt.created_at_ms,
        ),
        instruments=_instruments,
    )
    source_id = future_capture.sources[0].source_identity
    market_slice = _market_slice(
        event_id=event_id,
        observed_at_ms=observed_at_ms,
        verdict_created_at_ms=observed_at_ms + 1,
    )
    paid_funding = FundingRateV1(
        venue="binance.perp",
        provider_instrument_id="TUTUSDT",
        funding_at_ms=observed_at_ms + 450_000,
        funding_rate="0.0001",
    )
    pre_entry_funding = paid_funding.model_copy(update={"funding_at_ms": observed_at_ms + 1})

    def evaluate(funding: tuple[FundingRateV1, ...], *, protection_invalid: bool = False):
        drain = build_evidence_drain(
            future_capture,
            market_slices=[market_slice],
            funding_rates_by_source={source_id: funding},
            drained_at_ms=candidate.statistics.drain_cutoff_ms,
            max_horizon_ms=candidate.statistics.max_horizon_ms,
            bar_interval_ms=candidate.statistics.bar_interval_ms,
            funding_horizon_ms=candidate.statistics.max_horizon_ms,
            finalization_lag_ms=candidate.statistics.data_finalization_lag_ms,
            cost_model=cost_model,
        )
        return unblind_future_holdout(
            future_capture,
            drain,
            candidate=candidate,
            candidate_receipt=receipt,
            admission=AdmissionConfig(min_oi_value_usd=5_000_000),
            policy=CapitalPolicy(),
            price_window=PriceWindow(),
            target_notional=Decimal("10"),
            run_episode=run_bar_episode,
            evaluated_at_ms=candidate.statistics.drain_cutoff_ms,
            external_incidents=("protection_contract_invalid",) if protection_invalid else (),
        )

    without_funding = evaluate(())
    with_paid_funding = evaluate((paid_funding,))
    with_pre_entry_funding = evaluate((pre_entry_funding,))
    invalid_protection = evaluate((), protection_invalid=True)
    assert without_funding.metrics.mean_net_including_funding_return_bps is not None
    paid_delta = (
        without_funding.metrics.mean_net_including_funding_return_bps
        - with_paid_funding.metrics.mean_net_including_funding_return_bps
    )
    assert Decimal("0.9") < paid_delta < Decimal("1.1")
    assert (
        with_pre_entry_funding.metrics.mean_net_including_funding_return_bps
        == without_funding.metrics.mean_net_including_funding_return_bps
    )
    assert invalid_protection.terminal == "INSUFFICIENT_EVIDENCE"
    assert "protection_contract_invalid_incident" in invalid_protection.reasons
    assert without_funding.metrics.estimated_power_bps == 10_000


def test_clock_and_protection_incidents_cannot_be_predeclared_as_keep() -> None:
    candidate, _ = _candidate("4" * 64, "f" * 64)
    values = candidate.statistics.model_dump()
    values["incident_handling"]["clock_or_known_at_violation"] = "KEEP_AS_PREDECLARED_MISSING"

    with pytest.raises(ValueError, match="evidence_future_integrity_incident_must_fail_closed"):
        FutureStatisticalProtocolV1.model_validate(values)
