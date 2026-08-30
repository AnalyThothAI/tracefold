"""App composition for the one Production V3 evidence clock (#377)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import yaml
from psycopg import Error as PostgresError

from tracefold.app.cli.evidence_artifacts import (
    load_evidence_artifact,
    publish_evidence_artifact,
    verify_evidence_artifact,
)
from tracefold.app.repository_session import repositories
from tracefold.app.trading_config import capital_lane_config
from tracefold.app.workers.wiring.news_to_trading import (
    MAPPED_NEWS_PROJECTION_VERSION,
    news_trade_instruments,
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
    FutureCaptureReceiptV1,
    FutureDrainReceiptV1,
    FutureHoldoutResultReceiptV1,
    FutureHoldoutResultV1,
    NoCandidateV1,
    candidate_selection_program_sha256,
    eligible_universe_sha256,
    feature_contract_sha256,
    point_in_time_catalog_sha256,
)
from tracefold.trading.evidence_research import (
    build_evidence_capture,
    build_evidence_drain,
    seal_discovery_corpus,
    unblind_future_holdout,
)
from tracefold.trading.evidence_verification import (
    EvidenceVerificationCheckV1,
    FixedWindowAcceptanceV1,
    ProductionReleaseCandidateV1,
    ProductionRollbackReceiptV1,
    verification_report,
)
from tracefold.trading.replay import DirectionalReplayPlan, ReplayBarV1, ReplayMarketSlice, parse_replay_sources
from tracefold.trading.routing import resolve_instrument, signal_exchange_id

EVIDENCE_ROW_LIMIT = 20_000
GIT_EXECUTABLE = "/usr/bin/git"


def handle_trading_evidence(settings: Any, args: Any, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    action = str(getattr(args, "evidence_command", "") or "")
    try:
        if action == "capture":
            return _capture(settings, args, now_ms=now_ms)
        if action == "drain":
            return _drain(settings, args, now_ms=now_ms)
        if action == "corpus-seal":
            return _seal_corpus(settings, args, now_ms=now_ms)
        if action == "candidate-register":
            return _register_candidate(settings, args, now_ms=now_ms)
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
    if partition == "future":
        candidate, candidate_receipt = _load_candidate_pair(args)
        if (
            start_ms != candidate.statistics.future_start_ms
            or end_ms != candidate.statistics.future_end_ms
            or candidate_receipt.created_at_ms >= candidate.statistics.future_start_ms
        ):
            raise ValueError("evidence_future_capture_protocol_mismatch")
        _validate_durable_candidate_receipt(settings, candidate, candidate_receipt)
        with repositories(settings, role="serve") as repos:
            if repos.trading.future_capture_receipt_for_protocol(candidate.protocol_sha256) is not None:
                raise ValueError("evidence_future_capture_already_sealed")
    elif getattr(args, "candidate", "") or getattr(args, "candidate_receipt", ""):
        raise ValueError("evidence_discovery_candidate_forbidden")
    target_binding = None if candidate is None else candidate.binding
    known_at_cutoff_ms = now_ms if candidate is None else candidate.statistics.capture_cutoff_ms
    source_query_contract = canonical_sha256(
        {
            "projection": MAPPED_NEWS_PROJECTION_VERSION,
            "metric_version": NEWS_OI_METRIC_VERSION,
            "query": "trade_evidence_oi_rows_v1",
            "start_observed_at_ms": start_ms,
            "end_observed_at_ms": end_ms,
            "known_at_or_before_ms": known_at_cutoff_ms,
            "target_binding": target_binding,
            "limit": EVIDENCE_ROW_LIMIT,
            "order": "observed_at_ms_event_id",
        }
    )
    spec = EvidenceCaptureSpecV1(
        partition=cast(Any, partition),
        start_ms=start_ms,
        end_ms=end_ms,
        captured_at_ms=now_ms,
        target_binding=target_binding,
        source_query_contract_sha256=source_query_contract,
        protocol_receipt_sha256=None if candidate_receipt is None else candidate_receipt.receipt_sha256,
        protocol_locked_at_ms=None if candidate_receipt is None else candidate_receipt.created_at_ms,
    )
    with repositories(settings, role="serve") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        rows = repos.news.trade_evidence_oi_rows(
            metric_version=NEWS_OI_METRIC_VERSION,
            start_observed_at_ms=start_ms,
            end_observed_at_ms=end_ms,
            known_at_or_before_ms=known_at_cutoff_ms,
            limit=EVIDENCE_ROW_LIMIT,
        )
        if len(rows) >= EVIDENCE_ROW_LIMIT:
            raise RuntimeError("evidence_capture_source_truncated")
        mapped = [to_oi_candidate_row(row) for row in rows]
        if target_binding is not None:
            mapped = [row for row in mapped if binding_for_source_venue(row.get("venue")) == target_binding]

        def instruments(base: str, venue: str, observed_at_ms: int) -> Sequence[InstrumentCandidateRow]:
            return news_trade_instruments(repos, base, (venue,), observed_at_ms=observed_at_ms)

        artifact = build_evidence_capture(mapped, spec=spec, instruments=instruments)
    root = Path(args.out).resolve()
    if candidate is not None and candidate_receipt is not None:
        with repositories(settings, role="workers") as repos, repos.transaction():
            advisory_key = int.from_bytes(bytes.fromhex(candidate.protocol_sha256[:16]), byteorder="big", signed=True)
            repos.conn.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key,))
            if repos.trading.future_capture_receipt_for_protocol(candidate.protocol_sha256) is not None:
                raise ValueError("evidence_future_capture_already_sealed")
            path, digest = publish_evidence_artifact(root, kind="capture", artifact=artifact)
            if digest != artifact.capture_sha256:
                raise RuntimeError("evidence_capture_artifact_identity_invalid")
            receipt = FutureCaptureReceiptV1(
                binding=candidate.binding,
                candidate_receipt_sha256=candidate_receipt.receipt_sha256,
                protocol_sha256=candidate.protocol_sha256,
                sealed_corpus_sha256=candidate.sealed_corpus_sha256,
                capture_sha256=artifact.capture_sha256,
                artifact_sha256=digest,
                artifact_path=str(path),
                created_at_ms=now_ms,
            )
            receipt_path, _ = publish_evidence_artifact(root, kind="capture", artifact=receipt)
            inserted = repos.trading.append_future_capture_receipt(receipt)
        return 0, _receipt_answer(
            receipt,
            path,
            receipt_path,
            inserted=inserted,
            source_count=artifact.source_count,
        )
    path, digest = publish_evidence_artifact(root, kind="capture", artifact=artifact)
    if digest != artifact.capture_sha256:
        raise RuntimeError("evidence_capture_artifact_identity_invalid")
    return 0, _artifact_answer("CAPTURED", path, digest, source_count=artifact.source_count)


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
        if (
            candidate.binding != capture.spec.target_binding
            or candidate_receipt.receipt_sha256 != capture.spec.protocol_receipt_sha256
            or candidate_receipt.created_at_ms != capture.spec.protocol_locked_at_ms
        ):
            raise ValueError("evidence_future_candidate_authority_mismatch")
        if now_ms < candidate.statistics.drain_cutoff_ms:
            raise ValueError("evidence_future_drain_premature")
        _validate_durable_candidate_receipt(settings, candidate, candidate_receipt)
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
        with repositories(settings, role="workers") as repos, repos.transaction():
            advisory_key = int.from_bytes(bytes.fromhex(candidate.protocol_sha256[:16]), byteorder="big", signed=True)
            repos.conn.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key,))
            if repos.trading.future_drain_receipt_for_protocol(candidate.protocol_sha256) is not None:
                raise ValueError("evidence_future_drain_already_sealed")
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
    capture = load_evidence_artifact(Path(args.capture), EvidenceCaptureArtifactV1)
    drain = load_evidence_artifact(Path(args.drain), EvidenceDrainArtifactV1)
    candidate, candidate_receipt = _load_candidate_pair(args)
    _validate_durable_candidate_receipt(settings, candidate, candidate_receipt)
    capture_receipt = _load_durable_future_capture_receipt(
        settings,
        candidate,
        candidate_receipt,
        capture,
        capture_path=Path(args.capture),
    )
    config = capital_lane_config(settings)
    contract = build_execution_policy_contract_receipt()
    if (
        candidate.execution.execution_policy_sha256 != contract.execution_policy_sha256
        or candidate.execution.quote_contract_sha256 != contract.quote_contract_sha256
        or candidate.execution.adapter_contract_sha256 != contract.adapter_contract_sha256[candidate.binding]
    ):
        raise ValueError("evidence_future_execution_contract_drift")
    incidents: tuple[EvidenceIncident, ...] = (
        ("protection_contract_invalid",)
        if candidate.execution.protection_contract_sha256 != contract.protection_contract_sha256
        else ()
    )
    with repositories(settings, role="workers") as repos, repos.transaction():
        advisory_key = int.from_bytes(bytes.fromhex(candidate.protocol_sha256[:16]), byteorder="big", signed=True)
        repos.conn.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key,))
        if repos.trading.future_holdout_receipt_for_protocol(candidate.protocol_sha256) is not None:
            raise ValueError("evidence_future_already_unblinded")
        drain_row = repos.trading.future_drain_receipt_for_protocol(candidate.protocol_sha256)
        if drain_row is None:
            raise ValueError("evidence_future_drain_receipt_missing")
        drain_receipt = FutureDrainReceiptV1.model_validate(drain_row["payload"]["receipt"])
        if (
            drain_receipt.binding != candidate.binding
            or drain_receipt.candidate_receipt_sha256 != candidate_receipt.receipt_sha256
            or drain_receipt.capture_receipt_sha256 != capture_receipt.receipt_sha256
            or drain_receipt.sealed_corpus_sha256 != candidate.sealed_corpus_sha256
            or drain_receipt.capture_sha256 != capture.capture_sha256
            or drain_receipt.drain_sha256 != drain.drain_sha256
            or Path(drain_receipt.artifact_path).resolve() != Path(args.drain).resolve()
            or now_ms <= drain_receipt.created_at_ms
        ):
            raise ValueError("evidence_future_drain_receipt_mismatch")
        result = unblind_future_holdout(
            capture,
            drain,
            candidate=candidate,
            candidate_receipt=candidate_receipt,
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
        receipt = ProductionRollbackReceiptV1.model_validate(_mapping_file(str(args.rollback)))
        return _verify_rollback(settings, receipt, now_ms=now_ms)
    raise ValueError("evidence_verification_subject_required")


def _verify_receipt_chain(settings: Any, receipt_sha256: str, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    with repositories(settings, role="serve") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        current: str | None = receipt_sha256
        while current is not None:
            if current in seen or len(chain) >= 5:
                raise RuntimeError("evidence_receipt_parent_cycle")
            seen.add(current)
            row = repos.trading.evidence_clock_receipt(current)
            if row is None:
                raise RuntimeError("evidence_receipt_missing")
            chain.append(row)
            current = row["parent_receipt_sha256"]
    checks: list[EvidenceVerificationCheckV1] = []
    for index, row in enumerate(chain):
        payload = dict(row["payload"])
        receipt_payload = dict(payload["receipt"])
        kind = str(row["receipt_kind"])
        receipt_model: Any
        artifact_model: Any
        if kind == "DISCOVERY_CORPUS":
            receipt_model = DiscoveryCorpusReceiptV1.model_validate(receipt_payload)
            artifact_model = DiscoveryCorpusArtifactV1
        elif kind == "CANDIDATE_DECISION":
            receipt_model = CandidateDecisionReceiptV1.model_validate(receipt_payload)
            artifact_model = CandidateLockedV1 if row["terminal"] == "CANDIDATE_LOCKED" else None
        elif kind == "FUTURE_CAPTURE":
            receipt_model = FutureCaptureReceiptV1.model_validate(receipt_payload)
            artifact_model = EvidenceCaptureArtifactV1
        elif kind == "FUTURE_DRAIN":
            receipt_model = FutureDrainReceiptV1.model_validate(receipt_payload)
            artifact_model = EvidenceDrainArtifactV1
        elif kind == "FUTURE_RESULT":
            receipt_model = FutureHoldoutResultReceiptV1.model_validate(receipt_payload)
            artifact_model = FutureHoldoutResultV1
        else:
            raise RuntimeError("evidence_receipt_kind_unknown")
        row_identity_valid = (
            receipt_model.receipt_sha256 == row["receipt_sha256"]
            and payload.get("receipt_sha256") == row["receipt_sha256"]
            and payload.get("receipt_kind") == kind
            and payload.get("terminal") == row["terminal"]
            and payload.get("binding") == row["binding"]
            and payload.get("parent_receipt_sha256") == row["parent_receipt_sha256"]
            and payload.get("artifact_sha256") == row["artifact_sha256"]
            and payload.get("corpus_sha256") == row["corpus_sha256"]
            and payload.get("protocol_sha256") == row["protocol_sha256"]
        )
        checks.append(_check(f"receipt_{index}_row_identity", row_identity_valid, kind=kind))
        path = Path(str(receipt_model.artifact_path))
        if artifact_model is None:
            raw = verify_evidence_artifact(path, expected_sha256=str(row["artifact_sha256"]))
            decision = CANDIDATE_DECISION_ADAPTER.validate_python(json.loads(raw))
            artifact_valid = canonical_sha256(decision.model_dump(mode="json")) == row["artifact_sha256"]
        else:
            artifact = load_evidence_artifact(path, artifact_model, expected_sha256=row["artifact_sha256"])
            artifact_valid = canonical_sha256(artifact.model_dump(mode="json")) == row["artifact_sha256"]
        checks.append(_check(f"receipt_{index}_artifact_identity", artifact_valid, kind=kind))
        if index + 1 < len(chain):
            checks.append(
                _check(
                    f"receipt_{index}_parent_link",
                    row["parent_receipt_sha256"] == chain[index + 1]["receipt_sha256"],
                    kind=kind,
                )
            )
    return _verification_answer(
        verification_report(subject=f"receipt:{receipt_sha256}", verified_at_ms=now_ms, checks=checks)
    )


def _verify_case(settings: Any, case_id: str, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    with repositories(settings, role="serve") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = repos.trading.case_verification_snapshot(case_id)
    if snapshot is None:
        checks = [_check("case_exists", False)]
    else:
        case = snapshot["case"]
        intents = snapshot["intents"]
        allowed = case["capital_disposition"] == "allowed"
        intent_conserved = len(intents) == (1 if allowed else 0)
        checks = [
            _check("case_exists", True),
            _check("case_exactly_one_admission_link", snapshot["gate_count"] == 1, count=snapshot["gate_count"]),
            _check(
                "case_policy_capital_disposition_complete",
                case["policy_decision"] is not None and case["capital_disposition"] is not None,
            ),
            _check("case_intent_conservation", intent_conserved, intent_count=len(intents), capital_allowed=allowed),
        ]
        if intents:
            intent = intents[0]
            checks.extend(_intent_checks(intent))
    return _verification_answer(verification_report(subject=f"case:{case_id}", verified_at_ms=now_ms, checks=checks))


def _verify_window(
    settings: Any,
    spec: FixedWindowAcceptanceV1,
    *,
    now_ms: int,
) -> tuple[int, dict[str, Any]]:
    with repositories(settings, role="serve") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = repos.trading.fixed_window_verification_snapshot(spec)
    checks = _window_checks(spec, snapshot, now_ms=now_ms)
    return _verification_answer(
        verification_report(subject=f"fixed-window:{spec.window_sha256}", verified_at_ms=now_ms, checks=checks),
        snapshot={"by_binding": snapshot["by_binding"]},
    )


def _verify_release(
    settings: Any,
    release: ProductionReleaseCandidateV1,
    *,
    now_ms: int,
) -> tuple[int, dict[str, Any]]:
    evidence_receipts = tuple(sorted(release.corpus_receipt_sha256s + release.future_result_receipt_sha256s))
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
        window_snapshot = repos.trading.fixed_window_verification_snapshot(release.acceptance_window)
    identity = runtime_identity()
    contract = build_execution_policy_contract_receipt()
    config = capital_lane_config(settings)
    signed_tag, tag_commit, tag_tree = _git_tag_identity(release.release_tag)
    openapi_sha = _file_sha256(_repository_root() / "docs" / "generated" / "openapi.json")
    web_sha = _tree_sha256(_frontend_dist())
    wheel_identity = installed_nautilus_wheel_identity()
    wheel_sha = wheel_identity.rsplit("sha256:", 1)[-1] if "sha256:" in wheel_identity else None
    receipt_rows = {str(row["receipt_sha256"]): row for row in snapshot["receipts"]}
    grant_rows = {str(row["grant_sha256"]): row for row in snapshot["grants"]}
    risk_rows = {str(row["risk_policy_sha256"]): row for row in snapshot["risk_policies"]}
    binding_rows = {str(row["binding"]): row for row in snapshot["bindings"]}
    canary_rows = {str(row["intent_id"]): row for row in snapshot["canaries"]}
    runtime_rows = {str(row["runtime_id"]): row for row in snapshot["runtime_starts"]}
    corpus_artifacts = {
        str(receipt_rows[digest]["artifact_sha256"])
        for digest in release.corpus_receipt_sha256s
        if digest in receipt_rows
    }
    future_artifacts = {
        str(receipt_rows[digest]["artifact_sha256"])
        for digest in release.future_result_receipt_sha256s
        if digest in receipt_rows
    }
    release_bindings = {row.binding for row in release.bindings}
    receipt_chains_valid = all(
        _database_receipt_chain_valid(digest, receipt_rows, expected_kinds=("DISCOVERY_CORPUS",))
        for digest in release.corpus_receipt_sha256s
    ) and all(
        _database_receipt_chain_valid(
            digest,
            receipt_rows,
            expected_kinds=(
                "FUTURE_RESULT",
                "FUTURE_DRAIN",
                "FUTURE_CAPTURE",
                "CANDIDATE_DECISION",
                "DISCOVERY_CORPUS",
            ),
        )
        for digest in release.future_result_receipt_sha256s
    )
    checks = [
        _check("release_tag_signature_valid", signed_tag),
        _check("release_tag_commit_identity", tag_commit == release.git_commit_sha),
        _check("release_tag_tree_identity", tag_tree == release.git_tree_sha),
        _check("release_migration_head", snapshot["migration_head"] == release.migration_head),
        _check("release_runtime_revision", identity.runtime_revision == release.git_commit_sha),
        _check("release_image_digest", identity.image_digest == release.oci_image_digest),
        _check("release_openapi_identity", openapi_sha == release.openapi_sha256),
        _check("release_web_assets_identity", web_sha == release.web_assets_sha256),
        _check("release_nautilus_wheel_identity", wheel_sha == release.nautilus_wheel_sha256),
        _check("release_nautilus_source_identity", NAUTILUS_RELEASE.git_commit == release.nautilus_source_git_commit),
        _check(
            "release_execution_contract_receipt",
            contract.receipt_sha256 == release.execution_contract_receipt_sha256,
        ),
        _check(
            "release_execution_policy_identity",
            contract.execution_policy_sha256 == release.execution_policy_sha256,
        ),
        _check("release_quote_contract_identity", contract.quote_contract_sha256 == release.quote_contract_sha256),
        _check(
            "release_protection_contract_identity",
            contract.protection_contract_sha256 == release.protection_contract_sha256,
        ),
        _check("release_policy_config_identity", config.policy.config_digest == release.policy_config_sha256),
        _check("release_evidence_receipt_chains_valid", receipt_chains_valid),
        _check(
            "release_corpus_receipts_complete",
            all(
                digest in receipt_rows and receipt_rows[digest]["receipt_kind"] == "DISCOVERY_CORPUS"
                for digest in release.corpus_receipt_sha256s
            ),
            expected=len(release.corpus_receipt_sha256s),
        ),
        _check(
            "release_future_results_promote",
            all(
                digest in receipt_rows
                and receipt_rows[digest]["receipt_kind"] == "FUTURE_RESULT"
                and receipt_rows[digest]["terminal"] == "PROMOTE"
                for digest in release.future_result_receipt_sha256s
            ),
            expected=len(release.future_result_receipt_sha256s),
        ),
        _check("release_promotion_grants_complete", set(grant_rows) == set(release.promotion_grant_sha256s)),
        _check("release_risk_policies_complete", set(risk_rows) == set(release.risk_policy_sha256s)),
        _check(
            "release_risk_policies_target_release",
            all(row["approved_release"] == release.release_tag for row in risk_rows.values()),
        ),
        _check(
            "release_grant_evidence_chain",
            all(
                (payload := dict(row.get("payload") or {})).get("approved_release") == release.release_tag
                and row["binding"] in release_bindings
                and row["risk_policy_sha256"] in release.risk_policy_sha256s
                and row["sealed_corpus_sha256"] in corpus_artifacts
                and row["locked_future_report_sha256"] in future_artifacts
                and payload.get("policy_config_sha256") == release.policy_config_sha256
                and payload.get("execution_policy_sha256") == release.execution_policy_sha256
                and payload.get("quote_contract_sha256") == release.quote_contract_sha256
                and payload.get("protection_contract_sha256") == release.protection_contract_sha256
                for row in grant_rows.values()
            ),
        ),
        _check("release_canary_intents_complete", set(canary_rows) == set(release.canary_intent_ids)),
        _check(
            "release_canary_binding_coverage",
            {str(row["binding"]) for row in canary_rows.values()} == release_bindings,
        ),
        _check(
            "release_canary_authority_chain",
            all(
                row["grant_sha256"] in release.promotion_grant_sha256s
                and row["risk_policy_sha256"] in release.risk_policy_sha256s
                and row["sealed_corpus_sha256"] in corpus_artifacts
                and row["locked_future_report_sha256"] in future_artifacts
                for row in canary_rows.values()
            ),
        ),
        _check(
            "release_canaries_closed_flat",
            all(_canary_closed_flat(row) for row in canary_rows.values()),
        ),
    ]
    for binding in release.bindings:
        row = binding_rows.get(binding.binding)
        payload = {} if row is None else dict(row.get("execution_binding") or {})
        checks.append(
            _check(
                f"release_binding_{binding.binding.lower()}",
                row is not None
                and row["catalog_snapshot_sha256"] == binding.catalog_snapshot_sha256
                and row["capability_snapshot_sha256"] == binding.capability_snapshot_sha256
                and row["execution_binding_sha256"] == binding.execution_binding_sha256
                and int(row["account_generation"]) == binding.account_generation
                and payload.get("account_identity_sha256") == binding.account_identity_sha256
                and payload.get("adapter_contract_sha256") == binding.adapter_contract_sha256
                and payload.get("quote_contract_sha256") == release.quote_contract_sha256
                and payload.get("protection_contract_sha256") == release.protection_contract_sha256
                and payload.get("client_runtime_identity") == binding.client_runtime_identity,
            )
        )
    drill = release.restart_drill
    protected_runtime = runtime_rows.get(str(drill.protected_runtime_id))
    recovered_runtime = runtime_rows.get(str(drill.recovered_runtime_id))
    drill_intent = canary_rows.get(drill.intent_id)
    checks.extend(
        [
            _check(
                "release_restart_runtime_receipts_complete",
                set(runtime_rows) == {str(drill.protected_runtime_id), str(drill.recovered_runtime_id)},
            ),
            _check(
                "release_restart_exact_runtime_identity",
                all(
                    row is not None
                    and row["runtime_revision"] == release.git_commit_sha
                    and row["image_digest"] == release.oci_image_digest
                    and row["nautilus_source_git_commit"] == release.nautilus_source_git_commit
                    and _wheel_sha256(str(row["nautilus_wheel_identity"])) == release.nautilus_wheel_sha256
                    for row in (protected_runtime, recovered_runtime)
                ),
            ),
            _check(
                "release_restart_after_protection_before_flat",
                drill_intent is not None
                and protected_runtime is not None
                and recovered_runtime is not None
                and drill.binding == drill_intent["binding"]
                and protected_runtime["started_at_ms"] <= drill_intent["protected_at_ms"]
                and drill_intent["protected_at_ms"] <= drill.stopped_at_ms
                and drill.stopped_at_ms < recovered_runtime["started_at_ms"]
                and recovered_runtime["started_at_ms"] <= drill.reconciled_at_ms
                and drill.reconciled_at_ms == drill_intent["flat_verified_at_ms"],
            ),
        ]
    )
    checks.extend(_window_checks(release.acceptance_window, window_snapshot, now_ms=now_ms))
    return _verification_answer(
        verification_report(subject=f"release:{release.release_sha256}", verified_at_ms=now_ms, checks=checks),
        snapshot={"by_binding": window_snapshot["by_binding"]},
    )


def _verify_rollback(
    settings: Any,
    receipt: ProductionRollbackReceiptV1,
    *,
    now_ms: int,
) -> tuple[int, dict[str, Any]]:
    with repositories(settings, role="serve") as repos, repos.transaction():
        repos.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = repos.trading.rollback_verification_snapshot(receipt)
    bindings = {str(row["binding"]): row for row in snapshot["bindings"]}
    grants = {str(row["grant_sha256"]): row for row in snapshot["grants"]}
    checks = [
        _check("rollback_not_before_receipt", now_ms >= receipt.rolled_back_at_ms),
        _check("rollback_capital_paused", snapshot["control"] == "PAUSED"),
        _check("rollback_zero_active_intents", snapshot["active_intent_count"] == 0),
        _check("rollback_zero_active_risk", snapshot["active_risk_count"] == 0),
        _check(
            "rollback_bindings_authoritatively_flat",
            all(
                binding in bindings
                and bindings[binding]["account_state"] == "reconciled_flat"
                and bindings[binding]["active_arm_receipt_sha256"] is None
                for binding in receipt.bindings
            ),
        ),
        _check(
            "rollback_grants_revoked_or_expired",
            all(
                digest in grants
                and (
                    grants[digest]["expires_at_ms"] <= receipt.rolled_back_at_ms
                    or (
                        grants[digest]["revoked_at_ms"] is not None
                        and grants[digest]["revoked_at_ms"] <= receipt.rolled_back_at_ms
                    )
                )
                for digest in receipt.grant_sha256s
            ),
        ),
    ]
    return _verification_answer(
        verification_report(subject=f"rollback:{receipt.receipt_sha256}", verified_at_ms=now_ms, checks=checks)
    )


def _window_checks(
    spec: FixedWindowAcceptanceV1,
    snapshot: dict[str, Any],
    *,
    now_ms: int,
) -> list[EvidenceVerificationCheckV1]:
    counts = snapshot["counts"]
    return [
        _check("window_drain_cutoff_reached", now_ms >= spec.drain_cutoff_ms),
        _check(
            "window_minimum_sources",
            counts["source_count"] >= spec.minimum_source_count,
            count=counts["source_count"],
        ),
        _check("window_minimum_cases", counts["case_count"] >= spec.minimum_case_count, count=counts["case_count"]),
        _check(
            "window_minimum_intents",
            counts["intent_count"] >= spec.minimum_intent_count,
            count=counts["intent_count"],
        ),
        _check(
            "window_minimum_closed_flat",
            counts["closed_flat_count"] >= spec.minimum_closed_flat_count,
            count=counts["closed_flat_count"],
        ),
        _check("window_source_admission_unique", counts["source_count"] == counts["unique_source_count"]),
        _check(
            "window_source_disposition_conservation",
            counts["source_count"] == counts["admitted_source_count"] + counts["rejected_or_deferred_source_count"],
        ),
        _check("window_admitted_source_case_conservation", counts["admitted_source_count"] == counts["case_count"]),
        _check("window_gate_links_valid", counts["invalid_gate_link_count"] == 0),
        _check("window_case_links_complete", counts["case_without_gate_count"] == 0),
        _check("window_case_dispositions_complete", counts["case_disposition_missing_count"] == 0),
        _check("window_allowed_case_intent_conservation", counts["allowed_case_intent_mismatch_count"] == 0),
        _check("window_blocked_case_zero_intent", counts["blocked_case_intent_mismatch_count"] == 0),
        _check("window_only_v3_intents", counts["non_v3_intent_count"] == 0),
        _check("window_all_intents_terminal", counts["nonterminal_intent_count"] == 0),
        _check("window_zero_unknown_exposure", counts["exposure_unknown_or_active_count"] == 0),
        _check("window_zero_provider_write_before_fence", counts["provider_write_before_fence_count"] == 0),
        _check("window_zero_unprotected_fill", counts["unprotected_fill_count"] == 0),
        _check("window_closed_flat_proven", counts["closed_flat_proof_missing_count"] == 0),
        _check("window_financial_accounting_complete", counts["financial_accounting_missing_count"] == 0),
    ]


def _intent_checks(intent: dict[str, Any]) -> list[EvidenceVerificationCheckV1]:
    terminal = intent["execution_state"] == "TERMINAL"
    closed_flat = intent["terminal_outcome"] == "CLOSED_FLAT"
    return [
        _check("intent_current_contract", intent["intent_version"] == "trade_intent_v3"),
        _check("intent_terminal", terminal, execution_state=intent["execution_state"]),
        _check(
            "intent_zero_provider_write_before_fence",
            intent["entry_submitted_at_ms"] is None
            or (
                intent["entry_fenced_at_ms"] is not None
                and intent["entry_submitted_at_ms"] >= intent["entry_fenced_at_ms"]
            ),
        ),
        _check(
            "intent_no_unprotected_fill",
            intent["opened_at_ms"] is None
            or (intent["protected_at_ms"] is not None and intent["protection_order_id"] is not None),
        ),
        _check(
            "intent_closed_flat_proof",
            not closed_flat
            or (
                intent["closed_at_ms"] is not None
                and intent["flat_verified_at_ms"] is not None
                and intent["risk_status"] == "SETTLED"
                and intent["settlement_known"] is True
            ),
        ),
    ]


def _check(code: str, passed: bool, **evidence: Any) -> EvidenceVerificationCheckV1:
    return EvidenceVerificationCheckV1(code=code, passed=bool(passed), evidence=evidence)


def _verification_answer(report: Any, **extra: Any) -> tuple[int, dict[str, Any]]:
    data = report.model_dump(mode="json") | {"report_sha256": report.report_sha256} | extra
    return (0 if report.terminal == "VERIFIED" else 1), {"ok": report.terminal == "VERIFIED", "data": data}


def _canary_closed_flat(row: dict[str, Any]) -> bool:
    return bool(
        row["intent_version"] == "trade_intent_v3"
        and row["execution_state"] == "TERMINAL"
        and row["terminal_outcome"] == "CLOSED_FLAT"
        and row["entry_fenced_at_ms"] is not None
        and row["entry_submitted_at_ms"] is not None
        and row["entry_submitted_at_ms"] >= row["entry_fenced_at_ms"]
        and row["opened_at_ms"] is not None
        and row["protected_at_ms"] is not None
        and row["protection_order_id"] is not None
        and row["closed_at_ms"] is not None
        and row["flat_verified_at_ms"] is not None
        and row["realized_pnl_amount"] is not None
        and row["realized_pnl_currency"] is not None
        and row["commissions_by_currency"] is not None
        and row["funding_by_currency"] is not None
        and row["risk_status"] == "SETTLED"
        and row["settlement_known"] is True
    )


def _wheel_sha256(identity: str) -> str | None:
    return identity.rsplit("sha256:", 1)[-1] if "sha256:" in identity else None


def _database_receipt_chain_valid(
    receipt_sha256: str,
    rows: dict[str, dict[str, Any]],
    *,
    expected_kinds: tuple[str, ...],
) -> bool:
    current: str | None = receipt_sha256
    seen: set[str] = set()
    for expected_kind in expected_kinds:
        if current is None or current in seen:
            return False
        seen.add(current)
        row = rows.get(current)
        if (
            row is None
            or row["receipt_kind"] != expected_kind
            or not _database_receipt_identity_valid(row)
            or not _receipt_artifact_identity_valid(row)
        ):
            return False
        current = row["parent_receipt_sha256"]
    return current is None


def _database_receipt_identity_valid(row: dict[str, Any]) -> bool:
    try:
        payload = dict(row["payload"])
        kind = str(row["receipt_kind"])
        receipt_payload = dict(payload["receipt"])
        if kind == "DISCOVERY_CORPUS":
            receipt: Any = DiscoveryCorpusReceiptV1.model_validate(receipt_payload)
        elif kind == "CANDIDATE_DECISION":
            receipt = CandidateDecisionReceiptV1.model_validate(receipt_payload)
            decision = CANDIDATE_DECISION_ADAPTER.validate_python(payload["evidence"])
            if canonical_sha256(decision.model_dump(mode="json")) != row["artifact_sha256"]:
                return False
        elif kind == "FUTURE_CAPTURE":
            receipt = FutureCaptureReceiptV1.model_validate(receipt_payload)
        elif kind == "FUTURE_DRAIN":
            receipt = FutureDrainReceiptV1.model_validate(receipt_payload)
        elif kind == "FUTURE_RESULT":
            receipt = FutureHoldoutResultReceiptV1.model_validate(receipt_payload)
            result = FutureHoldoutResultV1.model_validate(payload["evidence"])
            if result.report_sha256 != row["artifact_sha256"]:
                return False
        else:
            return False
        return bool(
            receipt.receipt_sha256 == row["receipt_sha256"]
            and payload.get("receipt_sha256") == row["receipt_sha256"]
            and payload.get("receipt_kind") == kind
            and payload.get("terminal") == row["terminal"]
            and payload.get("binding") == row["binding"]
            and payload.get("parent_receipt_sha256") == row["parent_receipt_sha256"]
            and payload.get("artifact_sha256") == row["artifact_sha256"]
            and payload.get("corpus_sha256") == row["corpus_sha256"]
            and payload.get("protocol_sha256") == row["protocol_sha256"]
        )
    except (KeyError, TypeError, ValueError):
        return False


def _receipt_artifact_identity_valid(row: dict[str, Any]) -> bool:
    try:
        payload = dict(row["payload"])
        receipt_payload = dict(payload["receipt"])
        kind = str(row["receipt_kind"])
        if kind == "DISCOVERY_CORPUS":
            receipt: Any = DiscoveryCorpusReceiptV1.model_validate(receipt_payload)
            artifact_model: Any = DiscoveryCorpusArtifactV1
        elif kind == "CANDIDATE_DECISION":
            receipt = CandidateDecisionReceiptV1.model_validate(receipt_payload)
            raw = verify_evidence_artifact(Path(receipt.artifact_path), expected_sha256=str(row["artifact_sha256"]))
            decision = CANDIDATE_DECISION_ADAPTER.validate_python(json.loads(raw))
            return bool(canonical_sha256(decision.model_dump(mode="json")) == row["artifact_sha256"])
        elif kind == "FUTURE_CAPTURE":
            receipt = FutureCaptureReceiptV1.model_validate(receipt_payload)
            artifact_model = EvidenceCaptureArtifactV1
        elif kind == "FUTURE_DRAIN":
            receipt = FutureDrainReceiptV1.model_validate(receipt_payload)
            artifact_model = EvidenceDrainArtifactV1
        elif kind == "FUTURE_RESULT":
            receipt = FutureHoldoutResultReceiptV1.model_validate(receipt_payload)
            artifact_model = FutureHoldoutResultV1
        else:
            return False
        artifact = load_evidence_artifact(
            Path(receipt.artifact_path),
            artifact_model,
            expected_sha256=str(row["artifact_sha256"]),
        )
        return bool(canonical_sha256(artifact.model_dump(mode="json")) == row["artifact_sha256"])
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


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
) -> None:
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
