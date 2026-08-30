from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg import Error as PostgresError

from tests.integration.test_trading_capital_lane import _admission, _manifest
from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tests.trading_v3_fixtures import capital_evidence_fixture, set_evidence_database_clock
from tracefold.app.cli.commands import trading_evidence as evidence_cli
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.workers.runtime import WorkersRuntimeRepository
from tracefold.platform.config.models import Settings
from tracefold.trading.contracts import canonical_sha256
from tracefold.trading.evidence_clock import (
    DiscoveryCorpusReceiptV1,
    FutureCaptureBatchV1,
    FutureCaptureReceiptV1,
    future_capture_health_summary,
    prepare_future_capture_batch,
)
from tracefold.trading.evidence_research import (
    BlindMarketHealthSummaryV1,
    FutureCollectorObservationV1,
    FutureWorkersObservationV1,
    build_future_capture_collection_health,
)
from tracefold.trading.evidence_verification import (
    FixedWindowAcceptanceV1,
    NautilusRuntimeStartV1,
    ProductionReleaseCandidateV1,
    ProductionRollbackReceiptV2,
    ServeRuntimeObservationV1,
    prepare_production_release_registration,
)
from tracefold.trading.storage.root import TradingRepository

pytestmark = pytest.mark.integration

START = 1_900_000_000_000
END = START + 7 * 86_400_000


@pytest.fixture
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _window() -> FixedWindowAcceptanceV1:
    return FixedWindowAcceptanceV1(
        start_ms=START,
        end_ms=END,
        drain_cutoff_ms=END + 1,
        release_tag="production-v3-rc1",
        git_commit_sha="1" * 40,
        oci_image_digest="tracefold@sha256:" + "3" * 64,
        gate_version="candidate_gate_v1",
        gate_config_digest="a" * 64,
        minimum_source_count=1,
        minimum_case_count=1,
        minimum_intent_count=1,
        minimum_closed_flat_count=1,
    )


def test_empty_fixed_window_snapshot_cannot_manufacture_operational_activity(conn: Any) -> None:
    snapshot = TradingRepository(conn).fixed_window_verification_snapshot(_window())

    assert snapshot["counts"]["gate_source_count"] == 0
    assert snapshot["counts"]["case_count"] == 0
    assert snapshot["counts"]["intent_count"] == 0
    assert snapshot["counts"]["closed_flat_count"] == 0
    assert snapshot["by_binding"] == []


def test_fixed_window_per_binding_report_includes_case_reasons_and_missing_financials(conn: Any) -> None:
    repos = repositories_for_connection(conn)
    manifest = _manifest("fixed-window-report")
    admission = _admission(manifest) | {
        "gate_version": _window().gate_version,
        "gate_config_digest": _window().gate_config_digest,
    }
    with repos.transaction():
        assert repos.trading.create_case(
            case_id="fixed-window-report-case",
            manifest=manifest,
            admission=admission,
            release_revision=_window().git_commit_sha,
            now_ms=START,
        )

    snapshot = repos.trading.fixed_window_verification_snapshot(_window())

    assert snapshot["gate_source_keys"] == (manifest.primary_trigger.source_key,)
    assert snapshot["by_binding"] == [
        {
            "binding": "BINANCE_USDM",
            "case_count": 1,
            "intent_count": 0,
            "closed_flat_count": 0,
            "reservation_count": 0,
            "fenced_count": 0,
            "submitted_count": 0,
            "opened_count": 0,
            "protected_count": 0,
            "closed_count": 0,
            "flat_verified_count": 0,
            "financial_missing_count": 0,
            "policy_decisions": {"not_run": 1},
            "policy_reasons": {"not_run": 1},
            "capital_dispositions": {"not_applicable": 1},
            "capital_reasons": {"missing": 1},
            "q1_reasons": {},
            "q2_reasons": {},
            "reservation_states": {},
            "execution_states": {},
            "execution_phases": {},
            "terminal_outcomes": {},
            "execution_reasons": {},
            "financials": [],
            "stage_latency": {
                "fence_avg_ms": None,
                "submit_avg_ms": None,
                "open_avg_ms": None,
                "protect_avg_ms": None,
                "close_avg_ms": None,
                "flat_avg_ms": None,
                "flat_max_ms": None,
            },
        }
    ]


def test_future_capture_batches_are_contiguous_and_append_only(conn: Any) -> None:
    repository = TradingRepository(conn)
    corpus, candidate_receipt, candidate, _, _ = capital_evidence_fixture()
    health = build_future_capture_collection_health(
        (),
        collector=FutureCollectorObservationV1(
            connected=True,
            last_frame_at_ms=candidate.statistics.future_end_ms,
            last_error_code=None,
            expected_source_count=0,
            batch_end_ms=candidate.statistics.future_end_ms,
        ),
        workers=FutureWorkersObservationV1(
            lifecycle_state="running",
            heartbeat_at_ms=candidate.statistics.future_end_ms,
        ),
        market=BlindMarketHealthSummaryV1(
            market_instrument_count=0,
            bar_continuous_count=0,
            funding_probe_ok_count=0,
        ),
    )
    batch = FutureCaptureBatchV1(
        binding=candidate.binding,
        candidate_receipt_sha256=candidate_receipt.receipt_sha256,
        protocol_sha256=candidate.protocol_sha256,
        batch_start_ms=candidate.statistics.future_start_ms,
        batch_end_ms=candidate.statistics.future_end_ms,
        captured_at_ms=candidate.statistics.future_end_ms,
        capture_lag_ms=0,
        sources=(),
        source_count=0,
        late_source_count=0,
        catalog_missing_count=0,
        health=health,
    )
    health_sha256, incidents = future_capture_health_summary(
        (batch,), maximum_missingness_bps=candidate.statistics.maximum_missingness_bps
    )
    capture_receipt = FutureCaptureReceiptV1(
        binding=candidate.binding,
        candidate_receipt_sha256=candidate_receipt.receipt_sha256,
        protocol_sha256=candidate.protocol_sha256,
        sealed_corpus_sha256=candidate.sealed_corpus_sha256,
        capture_sha256="8" * 64,
        artifact_sha256="8" * 64,
        artifact_path="test-evidence/future-capture.json",
        batch_count=1,
        batch_health_sha256=health_sha256,
        collection_incidents=incidents,
        created_at_ms=candidate.statistics.future_end_ms + 1,
    )
    with conn.transaction():
        set_evidence_database_clock(repository, corpus.created_at_ms)
        assert repository.append_discovery_corpus_receipt(corpus)
        set_evidence_database_clock(repository, candidate_receipt.created_at_ms)
        assert repository.append_candidate_decision_receipt(candidate_receipt, candidate)

    with pytest.raises(PostgresError, match="future_capture_parent_invalid"), conn.transaction():
        set_evidence_database_clock(repository, capture_receipt.created_at_ms)
        repository.append_future_capture_receipt(capture_receipt)

    with conn.transaction():
        set_evidence_database_clock(repository, batch.captured_at_ms)
        prepared = prepare_future_capture_batch(batch)
        assert repository.append_future_capture_batch(prepared)
        assert not repository.append_future_capture_batch(prepared)

    forged = capture_receipt.model_copy(update={"batch_health_sha256": "0" * 64})
    with pytest.raises(PostgresError, match="future_capture_parent_invalid"), conn.transaction():
        set_evidence_database_clock(repository, forged.created_at_ms)
        repository.append_future_capture_receipt(forged)

    with conn.transaction():
        set_evidence_database_clock(repository, capture_receipt.created_at_ms)
        assert repository.append_future_capture_receipt(capture_receipt)

    with pytest.raises(PostgresError, match="append_only"), conn.transaction():
        conn.execute(
            "UPDATE trading_evidence_future_capture_batches SET source_count = 1 WHERE protocol_sha256 = %s",
            (candidate.protocol_sha256,),
        )


def test_public_handler_reads_real_postgres_and_artifact_bytes(conn: Any, tmp_path: Path) -> None:
    raw = b"{}"
    digest = hashlib.sha256(raw).hexdigest()
    artifact = tmp_path / "corpus.json"
    artifact.write_bytes(raw)
    receipt = DiscoveryCorpusReceiptV1(
        corpus_sha256=digest,
        artifact_sha256=digest,
        artifact_path=str(artifact),
        capture_sha256="a" * 64,
        drain_sha256="b" * 64,
        execution_contract_receipt_sha256="c" * 64,
        source_count=0,
        created_at_ms=10,
    )
    with conn.transaction():
        repository = TradingRepository(conn)
        set_evidence_database_clock(repository, 10)
        assert repository.append_discovery_corpus_receipt(receipt)

    settings = Settings(ws_token="secret", storage=postgres_settings_storage())
    args = SimpleNamespace(
        evidence_command="verify",
        receipt=receipt.receipt_sha256,
        case_id="",
        window="",
        release="",
        rollback="",
    )
    code, response = evidence_cli.handle_trading_evidence(settings, args, now_ms=0)

    assert code == 1
    checks = {row["code"]: row for row in response["data"]["checks"]}
    assert checks["receipt_0_row_identity"]["passed"] is True
    assert checks["receipt_0_artifact_identity"]["passed"] is False


def test_public_release_handler_uses_real_git_and_postgres(
    conn: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.trading.test_evidence_verification import _release_payload

    commit = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD^{tree}"],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    payload = _release_payload()
    payload.update(
        {
            "release_tag": "HEAD",
            "git_commit_sha": commit,
            "git_tree_sha": tree,
            "workers_runtime_revision": commit,
            "serve_runtime_revision": commit,
        }
    )
    window = dict(payload["acceptance_window"])
    window.update({"release_tag": "HEAD", "git_commit_sha": commit})
    payload["acceptance_window"] = window
    payload["approval_sha256"] = canonical_sha256(payload)
    release_path = tmp_path / "release.json"
    release_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        evidence_cli,
        "_observe_serve_runtime",
        lambda _settings: ServeRuntimeObservationV1(
            runtime_id="00000000-0000-0000-0000-000000000011",
            runtime_revision=commit,
            image_digest=str(payload["oci_image_digest"]),
            started_at_ms=START - 100,
            measured_at_ms=END + 1,
        ),
    )
    settings = Settings(ws_token="secret", storage=postgres_settings_storage())
    args = SimpleNamespace(
        evidence_command="verify",
        receipt="",
        case_id="",
        window="",
        release=str(release_path),
        rollback="",
    )
    code, response = evidence_cli.handle_trading_evidence(settings, args, now_ms=0)

    assert code == 1
    checks = {row["code"]: row for row in response["data"]["checks"]}
    assert checks["release_tag_commit_identity"]["passed"] is True
    assert checks["release_tag_tree_identity"]["passed"] is True
    assert checks["release_tag_signature_valid"]["passed"] is False


def test_release_registration_binds_actual_workers_and_serve_before_window(conn: Any) -> None:
    from tests.trading.test_evidence_verification import _release_payload

    payload = _release_payload()
    payload["approval_sha256"] = canonical_sha256(payload)
    release = ProductionReleaseCandidateV1.model_validate(payload)
    serve = ServeRuntimeObservationV1(
        runtime_id="00000000-0000-0000-0000-000000000011",
        runtime_revision=release.git_commit_sha,
        image_digest=release.oci_image_digest,
        started_at_ms=START - 1_000,
        measured_at_ms=START - 20,
    )
    prepared = prepare_production_release_registration(release, serve)
    with conn.transaction():
        workers = WorkersRuntimeRepository(conn)
        assert workers.begin(
            runtime_id="00000000-0000-0000-0000-000000000010",
            runtime_version="2",
            runtime_revision=release.git_commit_sha,
            image_digest=release.oci_image_digest,
            started_at_ms=START - 1_000,
            now_ms=START - 1_000,
        )
        workers.transition(
            runtime_id="00000000-0000-0000-0000-000000000010",
            lifecycle_state="running",
            now_ms=START - 20,
        )
        repository = TradingRepository(conn)
        set_evidence_database_clock(repository, START - 10)
        registration = repository.register_production_release(prepared)

    assert registration["inserted"] is True
    assert registration["registered_at_ms"] == START - 10
    assert str(registration["workers_runtime_id"]) == "00000000-0000-0000-0000-000000000010"
    assert str(registration["serve_runtime_id"]) == "00000000-0000-0000-0000-000000000011"
    with pytest.raises(PostgresError, match="append_only"), conn.transaction():
        conn.execute(
            "UPDATE trading_production_release_registrations SET release_tag = 'forged' WHERE release_sha256 = %s",
            (release.release_sha256,),
        )


def test_rollback_snapshot_requires_real_flat_bindings_and_revoked_grants(conn: Any) -> None:
    unsigned: dict[str, object] = {
        "rollback_version": "production_v3_rollback_receipt_v2",
        "release_candidate_sha256": "1" * 64,
        "release_candidate_artifact_path": "evidence/release.json",
        "bindings": ["BINANCE_USDM"],
        "grant_sha256s": ["2" * 64],
        "rolled_back_at_ms": START,
        "restart_workers_runtime_id": "00000000-0000-0000-0000-000000000020",
        "restart_serve_runtime_id": "00000000-0000-0000-0000-000000000021",
        "rolled_back_by": "operator",
        "statement": "ALL_ENABLED_VENUES_FLAT_GRANTS_REVOKED_CAPITAL_PAUSED_NO_TERMINAL_INTENT_REVIVAL",
    }
    from tracefold.trading.contracts import canonical_sha256

    receipt = ProductionRollbackReceiptV2.model_validate(unsigned | {"receipt_sha256": canonical_sha256(unsigned)})
    snapshot = TradingRepository(conn).rollback_verification_snapshot(receipt)

    assert snapshot["control"] == "PAUSED"
    assert snapshot["active_intent_count"] == 0
    assert snapshot["active_risk_count"] == 0
    assert snapshot["grants"] == []


def test_release_snapshot_reads_append_only_nautilus_process_generations(conn: Any) -> None:
    repos = TradingRepository(conn)
    starts = tuple(
        NautilusRuntimeStartV1(
            runtime_id=f"00000000-0000-0000-0000-{index:012d}",
            runtime_revision="1" * 40,
            image_digest="tracefold@sha256:" + "2" * 64,
            nautilus_version="1.231.0",
            nautilus_source_git_commit="3" * 40,
            nautilus_wheel_identity="linux@sha256:" + "4" * 64,
            started_at_ms=START + index,
        )
        for index in (1, 2)
    )
    for start in starts:
        assert repos.append_nautilus_runtime_start(start)

    snapshot = repos.release_verification_snapshot(
        evidence_receipts=(),
        promotion_grants=(),
        risk_policies=(),
        canary_intents=("5" * 64,),
        restart_runtime_ids=(str(starts[0].runtime_id), str(starts[1].runtime_id)),
    )

    assert [row["runtime_id"] for row in snapshot["runtime_starts"]] == sorted(
        str(start.runtime_id) for start in starts
    )
    assert snapshot["canaries"] == []
