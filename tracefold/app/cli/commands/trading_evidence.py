"""App composition for the one Production V3 evidence clock (#377)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast
from urllib.request import ProxyHandler, Request, build_opener

import yaml
from psycopg import Error as PostgresError

from tracefold.app.cli.evidence_artifacts import (
    load_evidence_artifact,
    publish_evidence_artifact,
)
from tracefold.app.repository_session import repositories
from tracefold.app.trading_config import capital_lane_config
from tracefold.app.workers.runtime import WorkersRuntimeRepository
from tracefold.app.workers.wiring.news_to_trading import (
    MAPPED_NEWS_PROJECTION_VERSION,
    TradeEvidenceCatalogProjectionRow,
    TradeEvidenceCollectionHealthRow,
    to_evidence_catalog_candidate_row,
    to_fixed_window_source_fact,
    to_oi_candidate_row,
)
from tracefold.integrations.nautilus import NAUTILUS_RELEASE, installed_nautilus_wheel_identity
from tracefold.integrations.nautilus.replay import run_bar_episode
from tracefold.integrations.venues import (
    VenueBar,
    VenueExpectedError,
    VenueFundingRate,
    fetch_binance_bars,
    fetch_binance_funding_rates,
    fetch_hyperliquid_bars,
    fetch_hyperliquid_funding_rates,
)
from tracefold.news import OI_METRIC_VERSION as NEWS_OI_METRIC_VERSION
from tracefold.platform.runtime_identity import runtime_identity
from tracefold.trading import MAX_RECEIVE_AGE_NS, binding_for_source_venue
from tracefold.trading.contract_receipt import build_execution_policy_contract_receipt
from tracefold.trading.contracts import InstrumentCandidateRow, OiCandidateRow, canonical_sha256
from tracefold.trading.evidence_clock import (
    CANDIDATE_DECISION_ADAPTER,
    EVIDENCE_BAR_INTERVAL_MS,
    CandidateDecisionReceiptV1,
    CandidateDecisionV1,
    CandidateLockedV1,
    DiscoveryCorpusArtifactV1,
    DiscoveryCorpusReceiptV1,
    EvidenceCaptureArtifactV1,
    EvidenceCaptureSpecV1,
    EvidenceDrainArtifactV1,
    EvidenceIncident,
    FundingRateV1,
    FutureCaptureBatchV1,
    FutureCaptureReceiptV1,
    FutureDrainReceiptV1,
    FutureHoldoutResultReceiptV1,
    NoCandidateV1,
    candidate_selection_evidence_sha256,
    candidate_selection_program_sha256,
    eligible_universe_sha256,
    feature_contract_sha256,
    future_capture_health_summary,
    point_in_time_catalog_sha256,
    prepare_future_capture_batch,
)
from tracefold.trading.evidence_research import (
    BlindBarIntervalObservationV1,
    BlindMarketHealthSummaryV1,
    BlindMarketProbeObservationV1,
    FutureCollectorObservationV1,
    FutureWorkersObservationV1,
    build_evidence_capture,
    build_evidence_drain,
    build_future_capture_collection_health,
    seal_discovery_corpus,
    summarize_blind_market_health,
    unblind_future_holdout,
)
from tracefold.trading.evidence_verification import (
    FixedWindowAcceptanceV1,
    ProductionReleaseCandidateV1,
    ProductionRollbackReceiptV2,
    ReleaseVerificationObservationsV1,
    ServeRuntimeObservationV1,
    case_verification_checks,
    fixed_window_binding_report,
    fixed_window_verification_checks,
    prepare_production_release_registration,
    receipt_artifact_requests,
    receipt_chain_verification_checks,
    receipt_chains_valid,
    release_verification_checks,
    rollback_verification_checks,
    verification_report,
)
from tracefold.trading.replay import DirectionalReplayPlan, ReplayBarV1, ReplayMarketSlice, parse_replay_sources
from tracefold.trading.routing import resolve_instrument, signal_exchange_id

EVIDENCE_ROW_LIMIT = 20_000
EVIDENCE_CATALOG_ROW_LIMIT = 100_000
GIT_EXECUTABLE = "/usr/bin/git"


def handle_trading_evidence(settings: Any, args: Any, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    action = str(getattr(args, "evidence_command", "") or "")
    try:
        now_ms = _database_now_ms(settings)
        if action == "capture":
            return _capture(settings, args, now_ms=now_ms)
        if action == "drain":
            return _drain(settings, args, now_ms=now_ms)
        if action == "corpus-seal":
            return _seal_corpus(settings, args, now_ms=now_ms)
        if action == "candidate-register":
            return _register_candidate(settings, args, now_ms=now_ms)
        if action == "release-register":
            return _register_release(settings, args)
        if action == "future-unblind":
            return _unblind(settings, args, now_ms=now_ms)
        if action == "verify":
            return _verify(settings, args, now_ms=now_ms)
        return 2, {"ok": False, "error": f"unknown evidence action: {action}"}
    except (OSError, PostgresError):
        return 1, {"ok": False, "error": "trading_evidence_io_unavailable"}
    except (RuntimeError, ValueError) as exc:
        return 1, {"ok": False, "error": str(exc) or "trading_evidence_failed"}


def _capture(settings: Any, args: Any, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    partition = str(args.partition)
    start_ms, end_ms = int(args.start_ms), int(args.end_ms)
    candidate: CandidateLockedV1 | None = None
    candidate_receipt: CandidateDecisionReceiptV1 | None = None
    candidate_recorded_at_ms: int | None = None
    existing_batches: tuple[FutureCaptureBatchV1, ...] = ()
    if partition == "future":
        candidate, candidate_receipt = _load_candidate_pair(args)
        if (
            start_ms != candidate.statistics.future_start_ms
            or end_ms != candidate.statistics.future_end_ms
            or candidate_receipt.created_at_ms >= candidate.statistics.future_start_ms
        ):
            raise ValueError("evidence_future_capture_protocol_mismatch")
        candidate_recorded_at_ms = _validate_durable_candidate_receipt(settings, candidate, candidate_receipt)
        with repositories(settings, role="serve") as repos:
            if repos.trading.future_capture_receipt_for_protocol(candidate.protocol_sha256) is not None:
                raise ValueError("evidence_future_capture_already_sealed")
            existing_batches = repos.trading.future_capture_batches(candidate.protocol_sha256)
    elif getattr(args, "candidate", "") or getattr(args, "candidate_receipt", ""):
        raise ValueError("evidence_discovery_candidate_forbidden")
    target_binding = None if candidate is None else candidate.binding
    query_start_ms, query_end_ms = start_ms, end_ms
    if candidate is not None and candidate_receipt is not None:
        if candidate_recorded_at_ms is None:
            raise RuntimeError("evidence_future_candidate_recorded_clock_missing")
        query_start_ms = existing_batches[-1].batch_end_ms if existing_batches else start_ms
        if query_start_ms >= end_ms:
            return _seal_future_capture_batches(
                settings,
                args,
                candidate=candidate,
                candidate_receipt=candidate_receipt,
                candidate_recorded_at_ms=int(candidate_recorded_at_ms),
                batches=existing_batches,
                now_ms=now_ms,
            )
        query_end_ms = min(query_start_ms + candidate.statistics.capture_interval_ms, end_ms)
        if now_ms < query_end_ms:
            raise ValueError("evidence_future_capture_batch_not_due")
        if now_ms > query_end_ms + candidate.statistics.maximum_capture_lag_ms:
            raise ValueError("evidence_future_capture_batch_late")
    known_at_cutoff_ms = now_ms if candidate is None else query_end_ms
    available_at_cutoff_ms = now_ms
    source_query_contract = canonical_sha256(
        {
            "projection": MAPPED_NEWS_PROJECTION_VERSION,
            "metric_version": NEWS_OI_METRIC_VERSION,
            "query": "trade_evidence_oi_rows_v2",
            "start_observed_at_ms": query_start_ms,
            "end_observed_at_ms": query_end_ms,
            "known_at_or_before_ms": known_at_cutoff_ms,
            "available_at_or_before_ms": available_at_cutoff_ms,
            "target_binding": target_binding,
            "limit": EVIDENCE_ROW_LIMIT,
            "order": "observed_at_ms_event_id",
        }
    )
    spec = EvidenceCaptureSpecV1(
        partition=cast(Any, partition),
        start_ms=query_start_ms,
        end_ms=query_end_ms,
        captured_at_ms=now_ms,
        target_binding=target_binding,
        source_query_contract_sha256=source_query_contract,
        protocol_receipt_sha256=None if candidate_receipt is None else candidate_receipt.receipt_sha256,
        protocol_locked_at_ms=candidate_recorded_at_ms,
    )
    catalog_rows: list[TradeEvidenceCatalogProjectionRow] = []
    news_collection_health: TradeEvidenceCollectionHealthRow | None = None
    workers_row: dict[str, Any] | None = None
    with repositories(settings, role="serve") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        rows = repos.news.trade_evidence_oi_rows(
            metric_version=NEWS_OI_METRIC_VERSION,
            start_observed_at_ms=query_start_ms,
            end_observed_at_ms=query_end_ms,
            known_at_or_before_ms=known_at_cutoff_ms,
            available_at_or_before_ms=available_at_cutoff_ms,
            limit=EVIDENCE_ROW_LIMIT,
        )
        if len(rows) >= EVIDENCE_ROW_LIMIT:
            raise RuntimeError("evidence_capture_source_truncated")
        catalog_rows = repos.news.trade_evidence_catalog_rows(
            metric_version=NEWS_OI_METRIC_VERSION,
            start_observed_at_ms=query_start_ms,
            end_observed_at_ms=query_end_ms,
            known_at_or_before_ms=known_at_cutoff_ms,
            available_at_or_before_ms=available_at_cutoff_ms,
            source_limit=EVIDENCE_ROW_LIMIT,
            catalog_limit=EVIDENCE_CATALOG_ROW_LIMIT,
        )
        if len(catalog_rows) >= EVIDENCE_CATALOG_ROW_LIMIT:
            raise RuntimeError("evidence_capture_catalog_truncated")
        if candidate is not None:
            source_venues = (
                ("binance", "binance.perp", "binance.usdm")
                if candidate.binding == "BINANCE_USDM"
                else ("hyperliquid", "hl.perp", "hyperliquid.perp")
            )
            news_collection_health = repos.news.trade_evidence_collection_health(
                start_observed_at_ms=query_start_ms,
                end_observed_at_ms=query_end_ms,
                available_at_or_before_ms=now_ms,
                source_venues=source_venues,
            )
            workers_row = WorkersRuntimeRepository(repos.conn).read()

    mapped = [to_oi_candidate_row(row) for row in rows]
    if target_binding is not None:
        mapped = [row for row in mapped if binding_for_source_venue(row.get("venue")) == target_binding]
    catalogs: dict[tuple[str, str, int], list[InstrumentCandidateRow]] = {}
    for row in catalog_rows:
        key = (row["base_symbol"], row["venue"], row["source_observed_at_ms"])
        catalogs.setdefault(key, []).append(to_evidence_catalog_candidate_row(row))

    def instruments(base: str, venue: str, observed_at_ms: int) -> Sequence[InstrumentCandidateRow]:
        return catalogs.get((base, venue, observed_at_ms), ())

    artifact = build_evidence_capture(mapped, spec=spec, instruments=instruments)
    root = Path(args.out).resolve()
    if candidate is not None and candidate_receipt is not None:
        if news_collection_health is None:
            raise RuntimeError("evidence_future_collector_health_missing")
        collector_health = FutureCollectorObservationV1(
            connected=news_collection_health["connected"],
            last_frame_at_ms=news_collection_health["last_frame_at_ms"],
            last_error_code=news_collection_health["last_error_code"],
            expected_source_count=news_collection_health["expected_source_count"],
            batch_end_ms=query_end_ms,
        )
        workers_health = (
            None
            if workers_row is None
            else FutureWorkersObservationV1(
                lifecycle_state=str(workers_row["lifecycle_state"]),
                heartbeat_at_ms=workers_row["heartbeat_at_ms"],
            )
        )
        market_health = asyncio.run(_fetch_blind_market_health(artifact, start_ms=query_start_ms, end_ms=query_end_ms))
        health = build_future_capture_collection_health(
            artifact.sources,
            collector=collector_health,
            workers=workers_health,
            market=market_health,
        )
        batch = FutureCaptureBatchV1(
            binding=candidate.binding,
            candidate_receipt_sha256=candidate_receipt.receipt_sha256,
            protocol_sha256=candidate.protocol_sha256,
            batch_start_ms=query_start_ms,
            batch_end_ms=query_end_ms,
            captured_at_ms=now_ms,
            capture_lag_ms=now_ms - query_end_ms,
            sources=artifact.sources,
            source_count=artifact.source_count,
            late_source_count=sum(row.available_at_ms > query_end_ms for row in artifact.sources),
            catalog_missing_count=sum(
                row.provider_instrument_id is None or not row.catalog.rows for row in artifact.sources
            ),
            health=health,
        )
        prepared_batch = prepare_future_capture_batch(batch)
        with repositories(settings, role="workers") as repos, repos.transaction():
            advisory_key = int.from_bytes(bytes.fromhex(candidate.protocol_sha256[:16]), byteorder="big", signed=True)
            repos.conn.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key,))
            if repos.trading.future_capture_receipt_for_protocol(candidate.protocol_sha256) is not None:
                raise ValueError("evidence_future_capture_already_sealed")
            inserted = repos.trading.append_future_capture_batch(prepared_batch)
        if query_end_ms < end_ms:
            return 0, {
                "ok": True,
                "data": {
                    "terminal": "FUTURE_CAPTURE_BATCH_APPENDED",
                    "batch_sha256": batch.batch_sha256,
                    "batch_start_ms": batch.batch_start_ms,
                    "batch_end_ms": batch.batch_end_ms,
                    "source_count": batch.source_count,
                    "capture_lag_ms": batch.capture_lag_ms,
                    "late_source_count": batch.late_source_count,
                    "catalog_missing_count": batch.catalog_missing_count,
                    "collection_health": batch.health.model_dump(mode="json"),
                    "inserted": inserted,
                    "next_batch_start_ms": query_end_ms,
                },
            }
        with repositories(settings, role="serve") as repos:
            batches = repos.trading.future_capture_batches(candidate.protocol_sha256)
        return _seal_future_capture_batches(
            settings,
            args,
            candidate=candidate,
            candidate_receipt=candidate_receipt,
            candidate_recorded_at_ms=cast(int, candidate_recorded_at_ms),
            batches=batches,
            now_ms=now_ms,
        )
    path, digest = publish_evidence_artifact(root, kind="capture", artifact=artifact)
    if digest != artifact.capture_sha256:
        raise RuntimeError("evidence_capture_artifact_identity_invalid")
    return 0, _artifact_answer("CAPTURED", path, digest, source_count=artifact.source_count)


def _seal_future_capture_batches(
    settings: Any,
    args: Any,
    *,
    candidate: CandidateLockedV1,
    candidate_receipt: CandidateDecisionReceiptV1,
    candidate_recorded_at_ms: int,
    batches: tuple[FutureCaptureBatchV1, ...],
    now_ms: int,
) -> tuple[int, dict[str, Any]]:
    protocol = candidate.statistics
    expected_start = protocol.future_start_ms
    for batch in batches:
        expected_end = min(expected_start + protocol.capture_interval_ms, protocol.future_end_ms)
        if (
            batch.protocol_sha256 != candidate.protocol_sha256
            or batch.candidate_receipt_sha256 != candidate_receipt.receipt_sha256
            or batch.binding != candidate.binding
            or batch.batch_start_ms != expected_start
            or batch.batch_end_ms != expected_end
        ):
            raise ValueError("evidence_future_capture_batch_chain_invalid")
        expected_start = expected_end
    if expected_start != protocol.future_end_ms:
        raise ValueError("evidence_future_capture_batches_incomplete")
    sources = tuple(
        sorted(
            (source for batch in batches for source in batch.sources),
            key=lambda row: (row.observed_at_ms, row.source_identity),
        )
    )
    if len({row.source_identity for row in sources}) != len(sources):
        raise ValueError("evidence_future_capture_duplicate_source")
    spec = EvidenceCaptureSpecV1(
        partition="future",
        start_ms=protocol.future_start_ms,
        end_ms=protocol.future_end_ms,
        captured_at_ms=now_ms,
        target_binding=candidate.binding,
        source_query_contract_sha256=canonical_sha256(
            {
                "projection": MAPPED_NEWS_PROJECTION_VERSION,
                "query": "append_only_future_capture_batches_v1",
                "batch_sha256s": tuple(batch.batch_sha256 for batch in batches),
            }
        ),
        protocol_receipt_sha256=candidate_receipt.receipt_sha256,
        protocol_locked_at_ms=candidate_recorded_at_ms,
    )
    artifact = EvidenceCaptureArtifactV1(spec=spec, sources=sources, source_count=len(sources))
    root = Path(args.out).resolve()
    path, digest = publish_evidence_artifact(root, kind="capture", artifact=artifact)
    if digest != artifact.capture_sha256:
        raise RuntimeError("evidence_capture_artifact_identity_invalid")
    batch_health_sha256, collection_incidents = future_capture_health_summary(
        batches,
        maximum_missingness_bps=candidate.statistics.maximum_missingness_bps,
    )
    receipt = FutureCaptureReceiptV1(
        binding=candidate.binding,
        candidate_receipt_sha256=candidate_receipt.receipt_sha256,
        protocol_sha256=candidate.protocol_sha256,
        sealed_corpus_sha256=candidate.sealed_corpus_sha256,
        capture_sha256=artifact.capture_sha256,
        artifact_sha256=digest,
        artifact_path=str(path),
        batch_count=len(batches),
        batch_health_sha256=batch_health_sha256,
        collection_incidents=collection_incidents,
        created_at_ms=now_ms,
    )
    receipt_path, _ = publish_evidence_artifact(root, kind="capture", artifact=receipt)
    with repositories(settings, role="workers") as repos, repos.transaction():
        advisory_key = int.from_bytes(bytes.fromhex(candidate.protocol_sha256[:16]), byteorder="big", signed=True)
        repos.conn.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key,))
        if repos.trading.future_capture_receipt_for_protocol(candidate.protocol_sha256) is not None:
            raise ValueError("evidence_future_capture_already_sealed")
        inserted = repos.trading.append_future_capture_receipt(receipt)
    return 0, _receipt_answer(
        receipt,
        path,
        receipt_path,
        inserted=inserted,
        source_count=artifact.source_count,
        batch_count=len(batches),
        maximum_capture_lag_ms=max((batch.capture_lag_ms for batch in batches), default=0),
        late_source_count=sum(batch.late_source_count for batch in batches),
        catalog_missing_count=sum(batch.catalog_missing_count for batch in batches),
    )


def _drain(settings: Any, args: Any, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    capture = load_evidence_artifact(Path(args.capture), EvidenceCaptureArtifactV1)
    has_candidate = bool(str(getattr(args, "candidate", "") or ""))
    has_receipt = bool(str(getattr(args, "candidate_receipt", "") or ""))
    candidate: CandidateLockedV1 | None = None
    candidate_receipt: CandidateDecisionReceiptV1 | None = None
    capture_receipt: FutureCaptureReceiptV1 | None = None
    if has_candidate or has_receipt:
        candidate, candidate_receipt = _load_candidate_pair(args)
    if capture.spec.partition == "future":
        if candidate is None or candidate_receipt is None:
            raise ValueError("evidence_future_candidate_required")
        if now_ms < candidate.statistics.drain_cutoff_ms:
            raise ValueError("evidence_future_drain_premature")
        candidate_recorded_at_ms = _validate_durable_candidate_receipt(settings, candidate, candidate_receipt)
        if (
            candidate.binding != capture.spec.target_binding
            or candidate_receipt.receipt_sha256 != capture.spec.protocol_receipt_sha256
            or candidate_recorded_at_ms != capture.spec.protocol_locked_at_ms
        ):
            raise ValueError("evidence_future_candidate_authority_mismatch")
        capture_receipt = _load_durable_future_capture_receipt(
            settings,
            candidate,
            candidate_receipt,
            capture,
            capture_path=Path(args.capture),
        )
        if now_ms <= capture_receipt.created_at_ms:
            raise ValueError("evidence_future_drain_before_capture_receipt")
        with repositories(settings, role="serve") as repos:
            if repos.trading.future_drain_receipt_for_protocol(candidate.protocol_sha256) is not None:
                raise ValueError("evidence_future_drain_already_sealed")
        max_horizon_ms = candidate.statistics.max_horizon_ms
        finalization_lag_ms = candidate.statistics.data_finalization_lag_ms
        cost_model = _candidate_cost_model(candidate)
        funding_horizon_ms = max_horizon_ms
    else:
        if candidate is not None or candidate_receipt is not None:
            raise ValueError("evidence_discovery_candidate_forbidden")
        if args.max_horizon_ms is None or args.finalization_lag_ms is None:
            raise ValueError("evidence_discovery_drain_window_required")
        max_horizon_ms = int(args.max_horizon_ms)
        finalization_lag_ms = int(args.finalization_lag_ms)
        cost_model = _mapping_file(str(args.cost_model))
        funding_horizon_ms = max_horizon_ms
    slices, funding = asyncio.run(
        _fetch_market_inputs(
            capture,
            now_ms=now_ms,
            max_horizon_ms=max_horizon_ms,
            funding_horizon_ms=funding_horizon_ms,
        )
    )
    artifact = build_evidence_drain(
        capture,
        market_slices=slices,
        funding_rates_by_source=funding,
        drained_at_ms=now_ms,
        max_horizon_ms=max_horizon_ms,
        bar_interval_ms=EVIDENCE_BAR_INTERVAL_MS,
        funding_horizon_ms=funding_horizon_ms,
        finalization_lag_ms=finalization_lag_ms,
        cost_model=cost_model,
    )
    missing = sum(row.bars_input.state == "MISSING" or row.funding_input.state == "MISSING" for row in artifact.rows)
    root = Path(args.out).resolve()
    if capture.spec.partition == "future":
        if candidate is None or candidate_receipt is None or capture_receipt is None:
            raise RuntimeError("evidence_future_candidate_required")
        path, digest = publish_evidence_artifact(root, kind="drain", artifact=artifact)
        if digest != artifact.drain_sha256:
            raise RuntimeError("evidence_drain_artifact_identity_invalid")
        receipt = FutureDrainReceiptV1(
            binding=candidate.binding,
            candidate_receipt_sha256=candidate_receipt.receipt_sha256,
            capture_receipt_sha256=capture_receipt.receipt_sha256,
            protocol_sha256=candidate.protocol_sha256,
            sealed_corpus_sha256=candidate.sealed_corpus_sha256,
            capture_sha256=capture.capture_sha256,
            drain_sha256=artifact.drain_sha256,
            artifact_sha256=digest,
            artifact_path=str(path),
            created_at_ms=now_ms,
        )
        receipt_path, _ = publish_evidence_artifact(root, kind="drain", artifact=receipt)
        with repositories(settings, role="workers") as repos, repos.transaction():
            advisory_key = int.from_bytes(bytes.fromhex(candidate.protocol_sha256[:16]), byteorder="big", signed=True)
            repos.conn.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key,))
            if repos.trading.future_drain_receipt_for_protocol(candidate.protocol_sha256) is not None:
                raise ValueError("evidence_future_drain_already_sealed")
            inserted = repos.trading.append_future_drain_receipt(receipt)
        return 0, _receipt_answer(
            receipt,
            path,
            receipt_path,
            inserted=inserted,
            source_count=len(artifact.rows),
            incomplete_count=missing,
        )
    path, digest = publish_evidence_artifact(root, kind="drain", artifact=artifact)
    if digest != artifact.drain_sha256:
        raise RuntimeError("evidence_drain_artifact_identity_invalid")
    return 0, _artifact_answer("DRAINED", path, digest, source_count=len(artifact.rows), incomplete_count=missing)


def _seal_corpus(settings: Any, args: Any, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    capture = load_evidence_artifact(Path(args.capture), EvidenceCaptureArtifactV1)
    drain = load_evidence_artifact(Path(args.drain), EvidenceDrainArtifactV1)
    config = capital_lane_config(settings)
    artifact = seal_discovery_corpus(
        capture,
        drain,
        contract=build_execution_policy_contract_receipt(),
        admission=config.admission,
        policy=config.policy,
        price_window=config.price_window,
        target_notional=config.target_notional_usd,
        run_episode=run_bar_episode,
    )
    root = Path(args.out).resolve()
    path, digest = publish_evidence_artifact(root, kind="corpus", artifact=artifact)
    receipt = DiscoveryCorpusReceiptV1(
        corpus_sha256=artifact.corpus_sha256,
        artifact_sha256=digest,
        artifact_path=str(path),
        capture_sha256=artifact.capture_sha256,
        drain_sha256=artifact.drain_sha256,
        execution_contract_receipt_sha256=artifact.execution_contract_receipt_sha256,
        source_count=len(artifact.raw_observations),
        created_at_ms=now_ms,
    )
    receipt_path, _ = publish_evidence_artifact(root, kind="corpus", artifact=receipt)
    with repositories(settings, role="workers") as repos, repos.transaction():
        inserted = repos.trading.append_discovery_corpus_receipt(receipt)
    return 0, _receipt_answer(receipt, path, receipt_path, inserted=inserted)


def _register_candidate(settings: Any, args: Any, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    document = _mapping_file(str(args.file))
    decision = CANDIDATE_DECISION_ADAPTER.validate_python(document)
    if isinstance(decision, CandidateLockedV1):
        if decision.locked_at_ms > now_ms or now_ms >= decision.statistics.future_start_ms:
            raise ValueError("evidence_candidate_registration_not_before_future")
    elif decision.decided_at_ms > now_ms:
        raise ValueError("evidence_no_candidate_decision_in_future")
    _validate_candidate_registration(settings, decision)
    root = Path(args.out).resolve()
    path, digest = publish_evidence_artifact(root, kind="candidate", artifact=decision)
    receipt = CandidateDecisionReceiptV1(
        terminal=decision.terminal,
        binding=decision.binding,
        sealed_corpus_sha256=decision.sealed_corpus_sha256,
        artifact_sha256=digest,
        artifact_path=str(path),
        protocol_sha256=decision.protocol_sha256,
        created_at_ms=now_ms,
    )
    receipt_path, _ = publish_evidence_artifact(root, kind="candidate", artifact=receipt)
    with repositories(settings, role="workers") as repos, repos.transaction():
        inserted = repos.trading.append_candidate_decision_receipt(receipt, decision)
    return 0, _receipt_answer(receipt, path, receipt_path, inserted=inserted)


def _unblind(settings: Any, args: Any, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    capture_path = Path(args.capture).resolve()
    drain_path = Path(args.drain).resolve()
    capture = load_evidence_artifact(capture_path, EvidenceCaptureArtifactV1)
    drain = load_evidence_artifact(drain_path, EvidenceDrainArtifactV1)
    candidate, candidate_receipt = _load_candidate_pair(args)
    candidate_recorded_at_ms = _validate_durable_candidate_receipt(settings, candidate, candidate_receipt)
    capture_receipt = _load_durable_future_capture_receipt(
        settings,
        candidate,
        candidate_receipt,
        capture,
        capture_path=capture_path,
    )
    config = capital_lane_config(settings)
    contract = build_execution_policy_contract_receipt()
    if (
        candidate.execution.execution_policy_sha256 != contract.execution_policy_sha256
        or candidate.execution.quote_contract_sha256 != contract.quote_contract_sha256
        or candidate.execution.adapter_contract_sha256 != contract.adapter_contract_sha256[candidate.binding]
    ):
        raise ValueError("evidence_future_execution_contract_drift")
    incidents_set = set(capture_receipt.collection_incidents)
    if candidate.execution.protection_contract_sha256 != contract.protection_contract_sha256:
        incidents_set.add("protection_contract_invalid")
    incidents: tuple[EvidenceIncident, ...] = tuple(sorted(incidents_set))
    with repositories(settings, role="serve") as repos:
        if repos.trading.future_holdout_receipt_for_protocol(candidate.protocol_sha256) is not None:
            raise ValueError("evidence_future_already_unblinded")
        drain_row = repos.trading.future_drain_receipt_for_protocol(candidate.protocol_sha256)
    drain_receipt = _validated_future_drain_receipt(
        drain_row,
        candidate=candidate,
        candidate_receipt=candidate_receipt,
        capture_receipt=capture_receipt,
        capture=capture,
        drain=drain,
        drain_path=drain_path,
        now_ms=now_ms,
    )
    result = unblind_future_holdout(
        capture,
        drain,
        candidate=candidate,
        candidate_receipt=candidate_receipt,
        candidate_recorded_at_ms=candidate_recorded_at_ms,
        admission=config.admission,
        policy=config.policy,
        price_window=config.price_window,
        target_notional=config.target_notional_usd,
        run_episode=run_bar_episode,
        evaluated_at_ms=now_ms,
        external_incidents=incidents,
    )
    root = Path(args.out).resolve()
    path, digest = publish_evidence_artifact(root, kind="future-result", artifact=result)
    receipt = FutureHoldoutResultReceiptV1(
        terminal=result.terminal,
        binding=result.binding,
        candidate_receipt_sha256=result.candidate_receipt_sha256,
        protocol_sha256=result.protocol_sha256,
        sealed_corpus_sha256=result.sealed_corpus_sha256,
        report_sha256=result.report_sha256,
        artifact_sha256=digest,
        artifact_path=str(path),
        created_at_ms=now_ms,
    )
    receipt_path, _ = publish_evidence_artifact(root, kind="future-result", artifact=receipt)
    with repositories(settings, role="workers") as repos, repos.transaction():
        advisory_key = int.from_bytes(bytes.fromhex(candidate.protocol_sha256[:16]), byteorder="big", signed=True)
        repos.conn.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key,))
        if repos.trading.future_holdout_receipt_for_protocol(candidate.protocol_sha256) is not None:
            raise ValueError("evidence_future_already_unblinded")
        locked_drain_row = repos.trading.future_drain_receipt_for_protocol(candidate.protocol_sha256)
        if locked_drain_row is None or locked_drain_row["receipt_sha256"] != drain_receipt.receipt_sha256:
            raise ValueError("evidence_future_drain_receipt_changed")
        inserted = repos.trading.append_future_holdout_result_receipt(receipt, result)
    return 0, _receipt_answer(receipt, path, receipt_path, inserted=inserted)


def _verify(settings: Any, args: Any, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    if str(getattr(args, "receipt", "") or ""):
        return _verify_receipt_chain(settings, str(args.receipt), now_ms=now_ms)
    if str(getattr(args, "case_id", "") or ""):
        return _verify_case(settings, str(args.case_id), now_ms=now_ms)
    if str(getattr(args, "window", "") or ""):
        spec = FixedWindowAcceptanceV1.model_validate(_mapping_file(str(args.window)))
        return _verify_window(settings, spec, now_ms=now_ms)
    if str(getattr(args, "release", "") or ""):
        release = ProductionReleaseCandidateV1.model_validate(_mapping_file(str(args.release)))
        return _verify_release(settings, release, now_ms=now_ms)
    if str(getattr(args, "rollback", "") or ""):
        receipt = ProductionRollbackReceiptV2.model_validate(_mapping_file(str(args.rollback)))
        return _verify_rollback(settings, receipt, now_ms=now_ms)
    raise ValueError("evidence_verification_subject_required")


def _register_release(settings: Any, args: Any) -> tuple[int, dict[str, Any]]:
    release = ProductionReleaseCandidateV1.model_validate(_mapping_file(str(args.file)))
    serve = _observe_serve_runtime(settings)
    prepared = prepare_production_release_registration(release, serve)
    with repositories(settings, role="workers") as repos, repos.transaction():
        registration = repos.trading.register_production_release(prepared)
    return 0, {
        "ok": True,
        "data": {
            "release_sha256": prepared.release_sha256,
            "window_sha256": prepared.window_sha256,
            "registered_at_ms": int(registration["registered_at_ms"]),
            "workers_runtime_id": str(registration["workers_runtime_id"]),
            "serve_runtime_id": str(registration["serve_runtime_id"]),
            "inserted": bool(registration["inserted"]),
        },
    }


def _observe_serve_runtime(settings: Any) -> ServeRuntimeObservationV1:
    request = Request(
        f"http://127.0.0.1:{int(settings.api.port)}/api/status",
        headers={"Authorization": f"Bearer {settings.ws_token}"},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=5.0) as response:
        envelope = json.loads(response.read())
    data = dict(envelope.get("data") or {})
    runtime = dict(data.get("runtime") or {})
    serve = dict(runtime.get("serve_runtime") or {})
    return ServeRuntimeObservationV1.model_validate({**serve, "measured_at_ms": data.get("measured_at_ms")})


def _verify_receipt_chain(settings: Any, receipt_sha256: str, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    with repositories(settings, role="serve") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        current: str | None = receipt_sha256
        while current is not None and current not in seen and len(chain) < 5:
            seen.add(current)
            row = repos.trading.evidence_clock_receipt(current)
            if row is None:
                break
            chain.append(row)
            current = row["parent_receipt_sha256"]
    artifact_bytes = _read_receipt_artifacts(chain)
    checks = receipt_chain_verification_checks(receipt_sha256, chain, artifact_bytes)
    return _verification_answer(
        verification_report(subject=f"receipt:{receipt_sha256}", verified_at_ms=now_ms, checks=checks)
    )


def _verify_case(settings: Any, case_id: str, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    with repositories(settings, role="serve") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = repos.trading.case_verification_snapshot(case_id)
    checks = case_verification_checks(snapshot)
    return _verification_answer(verification_report(subject=f"case:{case_id}", verified_at_ms=now_ms, checks=checks))


def _verify_window(
    settings: Any,
    spec: FixedWindowAcceptanceV1,
    *,
    now_ms: int,
) -> tuple[int, dict[str, Any]]:
    serve = _observe_serve_runtime(settings)
    with repositories(settings, role="serve") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        source_rows = repos.news.trade_fixed_window_oi_sources(
            metric_version=NEWS_OI_METRIC_VERSION,
            start_observed_at_ms=spec.start_ms,
            end_observed_at_ms=spec.end_ms,
            drain_cutoff_ms=spec.drain_cutoff_ms,
            limit=EVIDENCE_ROW_LIMIT,
        )
        if len(source_rows) >= EVIDENCE_ROW_LIMIT:
            raise RuntimeError("evidence_fixed_window_source_truncated")
        snapshot = repos.trading.fixed_window_verification_snapshot(spec)
    sources = tuple(to_fixed_window_source_fact(row) for row in source_rows)
    checks = fixed_window_verification_checks(spec, snapshot, sources=sources, serve=serve, now_ms=now_ms)
    by_binding = fixed_window_binding_report(snapshot["by_binding"], sources, snapshot["gate_source_keys"])
    return _verification_answer(
        verification_report(
            subject=f"fixed-window:{spec.window_sha256}",
            verified_at_ms=now_ms,
            checks=checks,
            binding_report=by_binding,
        ),
        snapshot={"by_binding": by_binding},
    )


def _verify_release(
    settings: Any,
    release: ProductionReleaseCandidateV1,
    *,
    now_ms: int,
) -> tuple[int, dict[str, Any]]:
    evidence_receipts = tuple(sorted(release.corpus_receipt_sha256s + release.future_result_receipt_sha256s))
    serve = _observe_serve_runtime(settings)
    with repositories(settings, role="serve") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = repos.trading.release_verification_snapshot(
            evidence_receipts=evidence_receipts,
            promotion_grants=release.promotion_grant_sha256s,
            risk_policies=release.risk_policy_sha256s,
            canary_intents=release.canary_intent_ids,
            restart_runtime_ids=(
                str(release.restart_drill.protected_runtime_id),
                str(release.restart_drill.recovered_runtime_id),
            ),
        )
        window_source_rows = repos.news.trade_fixed_window_oi_sources(
            metric_version=NEWS_OI_METRIC_VERSION,
            start_observed_at_ms=release.acceptance_window.start_ms,
            end_observed_at_ms=release.acceptance_window.end_ms,
            drain_cutoff_ms=release.acceptance_window.drain_cutoff_ms,
            limit=EVIDENCE_ROW_LIMIT,
        )
        if len(window_source_rows) >= EVIDENCE_ROW_LIMIT:
            raise RuntimeError("evidence_fixed_window_source_truncated")
        window_snapshot = repos.trading.fixed_window_verification_snapshot(release.acceptance_window)
    identity = runtime_identity()
    contract = build_execution_policy_contract_receipt()
    config = capital_lane_config(settings)
    signed_tag, tag_commit, tag_tree = _git_tag_identity(release.release_tag)
    openapi_sha = _file_sha256(_repository_root() / "docs" / "generated" / "openapi.json")
    web_sha = _tree_sha256(_frontend_dist())
    wheel_identity = installed_nautilus_wheel_identity()
    wheel_sha = wheel_identity.rsplit("sha256:", 1)[-1] if "sha256:" in wheel_identity else None
    receipt_artifacts = _read_receipt_artifacts(snapshot["receipts"])
    all_receipt_roots = release.corpus_receipt_sha256s + release.future_result_receipt_sha256s
    all_receipt_chains_valid = receipt_chains_valid(all_receipt_roots, snapshot["receipts"], receipt_artifacts)
    window_sources = tuple(to_fixed_window_source_fact(row) for row in window_source_rows)
    by_binding = fixed_window_binding_report(
        window_snapshot["by_binding"],
        window_sources,
        window_snapshot["gate_source_keys"],
    )
    checks = release_verification_checks(
        release,
        snapshot,
        window_snapshot,
        ReleaseVerificationObservationsV1(
            tag_signature_valid=signed_tag,
            tag_commit=tag_commit,
            tag_tree=tag_tree,
            runtime_revision=identity.runtime_revision,
            image_digest=identity.image_digest,
            openapi_sha256=openapi_sha,
            web_assets_sha256=web_sha,
            nautilus_wheel_sha256=wheel_sha,
            nautilus_source_git_commit=NAUTILUS_RELEASE.git_commit,
            execution_contract_receipt_sha256=contract.receipt_sha256,
            execution_policy_sha256=contract.execution_policy_sha256,
            quote_contract_sha256=contract.quote_contract_sha256,
            protection_contract_sha256=contract.protection_contract_sha256,
            policy_config_sha256=config.policy.config_digest,
            receipt_chains_valid=all_receipt_chains_valid,
            serve_runtime=serve,
        ),
        window_sources=window_sources,
        now_ms=now_ms,
    )
    return _verification_answer(
        verification_report(
            subject=f"release:{release.release_sha256}",
            verified_at_ms=now_ms,
            checks=checks,
            binding_report=by_binding,
        ),
        snapshot={"by_binding": by_binding},
    )


def _verify_rollback(
    settings: Any,
    receipt: ProductionRollbackReceiptV2,
    *,
    now_ms: int,
) -> tuple[int, dict[str, Any]]:
    release = ProductionReleaseCandidateV1.model_validate(_mapping_file(receipt.release_candidate_artifact_path))
    serve = _observe_serve_runtime(settings)
    with repositories(settings, role="serve") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = repos.trading.rollback_verification_snapshot(receipt)
    checks = rollback_verification_checks(receipt, release, snapshot, serve=serve, now_ms=now_ms)
    return _verification_answer(
        verification_report(subject=f"rollback:{receipt.receipt_sha256}", verified_at_ms=now_ms, checks=checks)
    )


def _verification_answer(report: Any, **extra: Any) -> tuple[int, dict[str, Any]]:
    data = report.model_dump(mode="json") | {"report_sha256": report.report_sha256} | extra
    return (0 if report.terminal == "VERIFIED" else 1), {"ok": report.terminal == "VERIFIED", "data": data}


def _read_receipt_artifacts(rows: list[dict[str, Any]]) -> dict[str, bytes | None]:
    payloads: dict[str, bytes | None] = {}
    for request in receipt_artifact_requests(rows):
        try:
            payloads[request.receipt_sha256] = Path(request.artifact_path).read_bytes()
        except OSError:
            payloads[request.receipt_sha256] = None
    return payloads


def _git_tag_identity(tag: str) -> tuple[bool, str | None, str | None]:
    root = _repository_root()
    try:
        verified = subprocess.run(  # noqa: S603 -- fixed executable and schema-validated tag
            [GIT_EXECUTABLE, "verify-tag", "--raw", tag],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        commit = subprocess.run(  # noqa: S603 -- fixed executable and schema-validated tag
            [GIT_EXECUTABLE, "rev-parse", f"{tag}^{{commit}}"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        tree = subprocess.run(  # noqa: S603 -- fixed executable and schema-validated tag
            [GIT_EXECUTABLE, "rev-parse", f"{tag}^{{tree}}"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False, None, None
    return verified.returncode == 0, commit, tree


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _frontend_dist() -> Path | None:
    root = _repository_root()
    for path in (root / "tracefold" / "web" / "dist", root / "web" / "dist"):
        if (path / "index.html").is_file():
            return path
    return None


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _tree_sha256(root: Path | None) -> str | None:
    if root is None:
        return None
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        return None
    return canonical_sha256(
        tuple({"path": path.relative_to(root).as_posix(), "sha256": _file_sha256(path)} for path in files)
    )


async def _fetch_blind_market_health(
    capture: EvidenceCaptureArtifactV1,
    *,
    start_ms: int,
    end_ms: int,
) -> BlindMarketHealthSummaryV1:
    """Fetch only raw provider clocks; Trading owns every continuity interpretation."""

    unique = {(plan.venue, plan.instrument.provider_symbol): plan for plan in _capture_plans(capture)}
    plans = tuple(unique[key] for key in sorted(unique))
    semaphore = asyncio.Semaphore(8)
    aligned_start = start_ms // EVIDENCE_BAR_INTERVAL_MS * EVIDENCE_BAR_INTERVAL_MS
    aligned_end = (end_ms + EVIDENCE_BAR_INTERVAL_MS - 1) // EVIDENCE_BAR_INTERVAL_MS * EVIDENCE_BAR_INTERVAL_MS

    funding_start_ms = max(0, end_ms - 24 * 60 * 60 * 1000)

    async def probe(plan: DirectionalReplayPlan) -> BlindMarketProbeObservationV1:
        bars: Sequence[VenueBar] = ()
        funding: Sequence[VenueFundingRate] = ()
        bar_error_code: str | None = None
        funding_error_code: str | None = None
        try:
            async with semaphore:
                if plan.venue == "binance.perp":
                    bars = await fetch_binance_bars(
                        plan.instrument.provider_symbol,
                        venue=plan.venue,
                        start_ms=aligned_start,
                        end_ms=aligned_end,
                    )
                else:
                    bars = await fetch_hyperliquid_bars(
                        plan.instrument.provider_symbol,
                        venue=plan.venue,
                        start_ms=aligned_start,
                        end_ms=aligned_end,
                    )
        except VenueExpectedError:
            bar_error_code = "venue_provider_outage"
        try:
            async with semaphore:
                if plan.venue == "binance.perp":
                    funding = await fetch_binance_funding_rates(
                        plan.instrument.provider_symbol,
                        start_ms=funding_start_ms,
                        end_ms=end_ms,
                    )
                else:
                    funding = await fetch_hyperliquid_funding_rates(
                        plan.instrument.provider_symbol,
                        start_ms=funding_start_ms,
                        end_ms=end_ms,
                    )
        except VenueExpectedError:
            funding_error_code = "venue_provider_outage"
        return BlindMarketProbeObservationV1(
            venue=plan.venue,
            provider_instrument_id=plan.instrument.provider_symbol,
            bar_start_ms=aligned_start,
            bar_end_ms=aligned_end,
            bars=tuple(
                BlindBarIntervalObservationV1(open_at_ms=row.open_at_ms, close_at_ms=row.close_at_ms) for row in bars
            ),
            bar_error_code=bar_error_code,
            funding_start_ms=funding_start_ms,
            funding_end_ms=end_ms,
            funding_at_ms=tuple(row.funding_at_ms for row in funding),
            funding_error_code=funding_error_code,
        )

    return summarize_blind_market_health(await asyncio.gather(*(probe(plan) for plan in plans)))


async def _fetch_market_inputs(
    capture: EvidenceCaptureArtifactV1,
    *,
    now_ms: int,
    max_horizon_ms: int,
    funding_horizon_ms: int,
) -> tuple[list[ReplayMarketSlice], dict[str, tuple[FundingRateV1, ...] | None]]:
    plans = _capture_plans(capture)
    semaphore = asyncio.Semaphore(8)

    async def fetch(plan: DirectionalReplayPlan) -> tuple[ReplayMarketSlice, tuple[FundingRateV1, ...] | None]:
        source = plan.source
        requested_start_ms = max(0, source.observed_at_ms - 3_930_000)
        requested_end_ms = source.observed_at_ms + max_horizon_ms
        start_ms = requested_start_ms // EVIDENCE_BAR_INTERVAL_MS * EVIDENCE_BAR_INTERVAL_MS
        end_ms = (
            (requested_end_ms + EVIDENCE_BAR_INTERVAL_MS - 1) // EVIDENCE_BAR_INTERVAL_MS * EVIDENCE_BAR_INTERVAL_MS
        )
        funding_end_ms = source.observed_at_ms + funding_horizon_ms
        try:
            async with semaphore:
                if plan.venue == "binance.perp":
                    bars = await fetch_binance_bars(
                        plan.instrument.provider_symbol,
                        venue=plan.venue,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                    funding = await fetch_binance_funding_rates(
                        plan.instrument.provider_symbol,
                        start_ms=source.observed_at_ms,
                        end_ms=funding_end_ms,
                    )
                else:
                    bars = await fetch_hyperliquid_bars(
                        plan.instrument.provider_symbol,
                        venue=plan.venue,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                    funding = await fetch_hyperliquid_funding_rates(
                        plan.instrument.provider_symbol,
                        start_ms=source.observed_at_ms,
                        end_ms=funding_end_ms,
                    )
        except VenueExpectedError:
            return ReplayMarketSlice(plan, [], "venue_provider_outage", start_ms, end_ms), None
        replay_bars = [_to_replay_bar(plan, row) for row in bars if row.close_at_ms <= min(end_ms, now_ms)]
        normalized_funding = tuple(_to_funding_rate(row) for row in funding)
        return (
            ReplayMarketSlice(
                plan,
                replay_bars,
                None if replay_bars else "market_history_missing",
                start_ms,
                end_ms,
            ),
            normalized_funding,
        )

    fetched = await asyncio.gather(*(fetch(plan) for plan in plans))
    slices = [item[0] for item in fetched]
    funding = {item[0].plan.source.source_key: item[1] for item in fetched}
    return slices, funding


def _capture_plans(capture: EvidenceCaptureArtifactV1) -> list[DirectionalReplayPlan]:
    parsed = parse_replay_sources([cast(OiCandidateRow, source.raw_source) for source in capture.sources])
    plans: list[DirectionalReplayPlan] = []
    for captured in capture.sources:
        source = parsed.get(captured.source_identity)
        exchange = None if source is None else signal_exchange_id(source.venue)
        if source is None or exchange is None or captured.provider_instrument_id is None:
            continue
        rows = [
            cast(
                InstrumentCandidateRow,
                row.model_dump(mode="python") | {"last_seen_ms": row.observed_at_ms},
            )
            for row in captured.catalog.rows
        ]
        instrument = resolve_instrument(rows, priority=(exchange,), observed_at_ms=source.observed_at_ms)
        if instrument is None:
            continue
        instrument_id = (
            f"{instrument.provider_symbol}-PERP.BINANCE"
            if captured.venue == "binance.perp"
            else f"{instrument.provider_symbol}-PERP.HYPERLIQUID"
        )
        plans.append(
            DirectionalReplayPlan(
                source=source,
                instrument=instrument,
                venue=cast(Any, captured.venue),
                instrument_id=instrument_id,
            )
        )
    return plans


def _to_replay_bar(plan: DirectionalReplayPlan, row: VenueBar) -> ReplayBarV1:
    return ReplayBarV1(
        venue=plan.venue,
        instrument_id=plan.instrument_id,
        open_at_ms=row.open_at_ms,
        close_at_ms=row.close_at_ms,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
    )


def _to_funding_rate(row: VenueFundingRate) -> FundingRateV1:
    return FundingRateV1(
        venue=row.venue,
        provider_instrument_id=row.provider_instrument_id,
        funding_at_ms=row.funding_at_ms,
        funding_rate=row.funding_rate,
    )


def _candidate_cost_model(candidate: CandidateLockedV1) -> dict[str, Any]:
    execution = candidate.execution
    return {
        "fee_model": execution.fee_model,
        "funding_model": execution.funding_model,
        "spread_slippage_model": execution.spread_slippage_model,
        "latency_model": execution.latency_model,
        "additional_stressed_cost_bps": str(execution.additional_stressed_cost_bps),
    }


def _mapping_file(path_value: str) -> dict[str, Any]:
    if not path_value:
        raise ValueError("trading_evidence_document_required")
    value = yaml.safe_load(Path(path_value).expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("trading_evidence_document_invalid")
    return value


def _load_candidate_pair(args: Any) -> tuple[CandidateLockedV1, CandidateDecisionReceiptV1]:
    candidate_value = str(getattr(args, "candidate", "") or "")
    receipt_value = str(getattr(args, "candidate_receipt", "") or "")
    if not candidate_value or not receipt_value:
        raise ValueError("evidence_future_candidate_required")
    candidate_path = Path(candidate_value)
    receipt_path = Path(receipt_value)
    candidate = load_evidence_artifact(candidate_path, CandidateLockedV1)
    receipt = load_evidence_artifact(receipt_path, CandidateDecisionReceiptV1)
    if (
        receipt.terminal != "CANDIDATE_LOCKED"
        or receipt.binding != candidate.binding
        or receipt.sealed_corpus_sha256 != candidate.sealed_corpus_sha256
        or receipt.artifact_sha256 != candidate.protocol_sha256
        or receipt.protocol_sha256 != candidate.protocol_sha256
        or Path(receipt.artifact_path).resolve() != candidate_path.resolve()
        or candidate.locked_at_ms > receipt.created_at_ms
    ):
        raise ValueError("evidence_future_candidate_receipt_mismatch")
    return candidate, receipt


def _validate_durable_candidate_receipt(
    settings: Any,
    candidate: CandidateLockedV1,
    receipt: CandidateDecisionReceiptV1,
) -> int:
    with repositories(settings, role="serve") as repos:
        row = repos.trading.evidence_clock_receipt(receipt.receipt_sha256)
    if row is None:
        raise ValueError("evidence_future_candidate_receipt_not_durable")
    payload = dict(row["payload"])
    if (
        row["receipt_kind"] != "CANDIDATE_DECISION"
        or row["terminal"] != "CANDIDATE_LOCKED"
        or row["binding"] != candidate.binding
        or row["artifact_sha256"] != candidate.protocol_sha256
        or row["corpus_sha256"] != candidate.sealed_corpus_sha256
        or row["protocol_sha256"] != candidate.protocol_sha256
        or payload.get("receipt") != receipt.model_dump(mode="json")
        or payload.get("evidence") != candidate.model_dump(mode="json")
    ):
        raise ValueError("evidence_future_candidate_receipt_not_durable")
    recorded_at_ms = int(row.get("recorded_at_ms") or 0)
    if not receipt.created_at_ms <= recorded_at_ms < candidate.statistics.future_start_ms:
        raise ValueError("evidence_future_candidate_recorded_clock_invalid")
    return recorded_at_ms


def _load_durable_future_capture_receipt(
    settings: Any,
    candidate: CandidateLockedV1,
    candidate_receipt: CandidateDecisionReceiptV1,
    capture: EvidenceCaptureArtifactV1,
    *,
    capture_path: Path,
) -> FutureCaptureReceiptV1:
    with repositories(settings, role="serve") as repos:
        row = repos.trading.future_capture_receipt_for_protocol(candidate.protocol_sha256)
    if row is None:
        raise ValueError("evidence_future_capture_receipt_missing")
    receipt = FutureCaptureReceiptV1.model_validate(row["payload"]["receipt"])
    if (
        receipt.binding != candidate.binding
        or receipt.candidate_receipt_sha256 != candidate_receipt.receipt_sha256
        or receipt.protocol_sha256 != candidate.protocol_sha256
        or receipt.sealed_corpus_sha256 != candidate.sealed_corpus_sha256
        or receipt.capture_sha256 != capture.capture_sha256
        or Path(receipt.artifact_path).resolve() != capture_path.resolve()
    ):
        raise ValueError("evidence_future_capture_receipt_mismatch")
    return receipt


def _validated_future_drain_receipt(
    row: dict[str, Any] | None,
    *,
    candidate: CandidateLockedV1,
    candidate_receipt: CandidateDecisionReceiptV1,
    capture_receipt: FutureCaptureReceiptV1,
    capture: EvidenceCaptureArtifactV1,
    drain: EvidenceDrainArtifactV1,
    drain_path: Path,
    now_ms: int,
) -> FutureDrainReceiptV1:
    if row is None:
        raise ValueError("evidence_future_drain_receipt_missing")
    receipt = FutureDrainReceiptV1.model_validate(row["payload"]["receipt"])
    if (
        receipt.binding != candidate.binding
        or receipt.candidate_receipt_sha256 != candidate_receipt.receipt_sha256
        or receipt.capture_receipt_sha256 != capture_receipt.receipt_sha256
        or receipt.sealed_corpus_sha256 != candidate.sealed_corpus_sha256
        or receipt.capture_sha256 != capture.capture_sha256
        or receipt.drain_sha256 != drain.drain_sha256
        or Path(receipt.artifact_path).resolve() != drain_path.resolve()
        or now_ms <= receipt.created_at_ms
    ):
        raise ValueError("evidence_future_drain_receipt_mismatch")
    return receipt


def _database_now_ms(settings: Any) -> int:
    """Use PostgreSQL's clock for every irreversible evidence transition."""

    with repositories(settings, role="serve") as repos:
        row = repos.conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms"
        ).fetchone()
    if row is None or int(row["now_ms"]) <= 0:
        raise RuntimeError("evidence_database_clock_unavailable")
    return int(row["now_ms"])


def _validate_candidate_registration(settings: Any, decision: CandidateDecisionV1) -> None:
    with repositories(settings, role="serve") as repos:
        row = repos.trading.evidence_clock_receipt_for_artifact(
            decision.sealed_corpus_sha256,
            kind="DISCOVERY_CORPUS",
        )
    if row is None:
        raise ValueError("evidence_candidate_corpus_receipt_missing")
    receipt = DiscoveryCorpusReceiptV1.model_validate(row["payload"]["receipt"])
    corpus = load_evidence_artifact(
        Path(receipt.artifact_path),
        DiscoveryCorpusArtifactV1,
        expected_sha256=receipt.artifact_sha256,
    )
    if decision.corpus_artifact_sha256 != corpus.corpus_sha256 or decision.sealed_corpus_sha256 != corpus.corpus_sha256:
        raise ValueError("evidence_candidate_corpus_identity_mismatch")
    decision_at_ms = decision.locked_at_ms if isinstance(decision, CandidateLockedV1) else decision.decided_at_ms
    if decision_at_ms < receipt.created_at_ms:
        raise ValueError("evidence_candidate_before_corpus_seal")
    if decision.selection_program_sha256 != candidate_selection_program_sha256():
        raise ValueError("evidence_candidate_selection_program_mismatch")
    expected_terminal, expected_selection_sha = candidate_selection_evidence_sha256(corpus, decision.binding)
    if decision.terminal != expected_terminal or decision.selection_evidence_sha256 != expected_selection_sha:
        raise ValueError("evidence_candidate_selection_result_mismatch")
    if isinstance(decision, NoCandidateV1):
        return

    config = capital_lane_config(settings)
    contract = build_execution_policy_contract_receipt()
    expected_feature_sha256 = feature_contract_sha256(
        admission_config_sha256=config.admission.digest,
        price_window=config.price_window.as_dict(),
        policy_id=config.policy.policy_id,
        policy_config_sha256=config.policy.config_digest,
    )
    execution = decision.execution
    exact = contract.exact_execution_values
    mismatched = (
        decision.discovery_start_ms != corpus.discovery_start_ms
        or decision.discovery_end_ms != corpus.discovery_end_ms
        or decision.source_contract_sha256 != corpus.source_contract_sha256
        or decision.feature_contract_sha256 != corpus.feature_contract_sha256
        or decision.feature_contract_sha256 != expected_feature_sha256
        or decision.point_in_time_catalog_sha256 != point_in_time_catalog_sha256(corpus, decision.binding)
        or decision.eligible_universe_sha256 != eligible_universe_sha256(corpus, decision.binding)
        or decision.policy_id != corpus.policy_id
        or decision.policy_id != config.policy.policy_id
        or decision.policy_config_sha256 != corpus.policy_config_sha256
        or decision.policy_config_sha256 != config.policy.config_digest
        or decision.execution_contract_receipt_sha256 != corpus.execution_contract_receipt_sha256
        or decision.execution_contract_receipt_sha256 != contract.receipt_sha256
        or decision.evaluator_program_sha256 != corpus.evaluator_program_sha256
        or execution.intent_ttl_ms != int(cast(Any, exact["ttl_ms"]))
        or execution.target_notional != corpus.target_notional
        or execution.target_notional != config.target_notional_usd
        or execution.cost_model_sha256 != corpus.cost_model_sha256
        or execution.max_quote_age_ms != MAX_RECEIVE_AGE_NS // 1_000_000
        or execution.max_spread_bps != int(cast(Any, exact["max_spread_bps"]))
        or execution.max_entry_drift_bps != int(cast(Any, exact["max_entry_drift_bps"]))
        or execution.stop_loss_bps != int(cast(Any, exact["stop_loss_bps"]))
        or execution.max_holding_ms != int(cast(Any, exact["max_holding_ms"]))
        or execution.execution_policy_sha256 != contract.execution_policy_sha256
        or execution.adapter_contract_sha256 != contract.adapter_contract_sha256[decision.binding]
        or execution.quote_contract_sha256 != contract.quote_contract_sha256
        or execution.protection_contract_sha256 != contract.protection_contract_sha256
    )
    if mismatched:
        raise ValueError("evidence_candidate_execution_contract_mismatch")


def _artifact_answer(terminal: str, path: Path, digest: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {"terminal": terminal, "artifact_path": str(path), "artifact_sha256": digest, **extra},
    }


def _receipt_answer(receipt: Any, path: Path, receipt_path: Path, *, inserted: bool, **extra: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "terminal": receipt.terminal,
            "artifact_path": str(path),
            "artifact_sha256": receipt.artifact_sha256,
            "receipt_path": str(receipt_path),
            "receipt_sha256": receipt.receipt_sha256,
            "inserted": inserted,
            **extra,
        },
    }


__all__ = ["EVIDENCE_ROW_LIMIT", "handle_trading_evidence"]
