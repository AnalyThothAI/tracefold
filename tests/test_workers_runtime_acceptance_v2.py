from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tracefold.app.workers_runtime_acceptance_v2 import (
    EVIDENCE_SCHEMA_VERSION,
    RAW_EVIDENCE_SCHEMA_VERSION,
    REVIEW_SCHEMA_VERSION,
    SEAL_FILE,
    _repository_head,
    seal_workers_runtime_evidence,
    workers_runtime_evidence_template,
)
from tracefold.platform.postgres.postgres_migrations import latest_migration_version


@pytest.fixture(autouse=True)
def _acceptance_checkout_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tracefold.app.workers_runtime_acceptance_v2._repository_is_clean",
        lambda: True,
    )


def test_complete_workers_runtime_v2_bundle_seals_once_and_detects_mutation(tmp_path: Path) -> None:
    (tmp_path / "evidence.json").write_text(json.dumps(_complete_evidence(tmp_path)))
    supporting = tmp_path / "metrics.jsonl"
    supporting.write_text('{"cpu":1}\n')

    seal = seal_workers_runtime_evidence(tmp_path)
    assert seal_workers_runtime_evidence(tmp_path) == seal
    assert {item["path"] for item in seal["files"]} == {
        "evidence.json",
        "metrics.jsonl",
        "operator-config.yaml",
        "raw-evidence.json",
        "review.json",
    }

    supporting.write_text('{"cpu":2}\n')
    with pytest.raises(ValueError, match="workers_runtime_sealed_bundle_modified"):
        seal_workers_runtime_evidence(tmp_path)


def test_bundle_refuses_short_real_run_and_unverified_restore(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    evidence["gates"]["real_continuous_30m"]["duration_seconds"] = 1_799
    evidence["gates"]["startup_recovery"]["snapshot_restore"]["verified"] = False
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(
        ValueError,
        match=r"workers_runtime_startup_recovery_snapshot_restore_failed|workers_runtime_real_run_too_short",
    ):
        seal_workers_runtime_evidence(tmp_path)
    assert not (tmp_path / SEAL_FILE).exists()


def test_bundle_computes_raw_capacity_convergence(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    news = evidence["gates"]["real_continuous_30m"]["capacity"]["news"]
    news.update(
        {
            "actionable_count_start": 10,
            "actionable_count_end": 10,
            "oldest_age_ms_start": 1_000,
            "oldest_age_ms_end": 2_000,
            "arrival_count": 5,
            "arrival_rate_per_minute": 1.0 / 6.0,
            "completion_count": 5,
            "completion_rate_per_minute": 1.0 / 6.0,
            "freshness_ok": False,
        }
    )
    raw = json.loads((tmp_path / "raw-evidence.json").read_text())
    raw["proofs"]["real_continuous_30m_capacity_interval"]["capacity"]["news"] = dict(news)
    (tmp_path / "raw-evidence.json").write_text(json.dumps(raw))
    _refresh_artifact_hashes(tmp_path, evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match="workers_runtime_capacity_not_converging:news"):
        seal_workers_runtime_evidence(tmp_path)


def test_empty_start_and_end_backlog_passes_with_interval_traffic(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    news = evidence["gates"]["real_continuous_30m"]["capacity"]["news"]
    news.update(
        {
            "arrival_count": 5,
            "arrival_rate_per_minute": 1.0 / 6.0,
            "completion_count": 5,
            "completion_rate_per_minute": 1.0 / 6.0,
        }
    )
    raw = json.loads((tmp_path / "raw-evidence.json").read_text())
    raw["proofs"]["real_continuous_30m_capacity_interval"]["capacity"]["news"] = dict(news)
    (tmp_path / "raw-evidence.json").write_text(json.dumps(raw))
    _refresh_artifact_hashes(tmp_path, evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    seal_workers_runtime_evidence(tmp_path)


def test_bundle_rejects_bare_ok_and_unbound_reviewer_hash(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    evidence["gates"]["offline_semantic_determinism"]["public_semantic_diff"] = {"ok": True}
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="workers_runtime_evidence_artifact_path_required"):
        seal_workers_runtime_evidence(tmp_path)


def test_bundle_rejects_fake_commit_and_opaque_raw_artifact(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    evidence["versions"]["commit_sha"] = "a" * 40
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="workers_runtime_evidence_commit_not_current_checkout"):
        seal_workers_runtime_evidence(tmp_path)

    evidence = _complete_evidence(tmp_path)
    (tmp_path / "raw-evidence.json").write_text('{"sample":"raw"}\n')
    _refresh_artifact_hashes(tmp_path, evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="raw_schema_invalid"):
        seal_workers_runtime_evidence(tmp_path)

    evidence = _complete_evidence(tmp_path)
    evidence["review"]["artifact_sha256"] = "b" * 64
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="workers_runtime_review_artifact_hash_mismatch"):
        seal_workers_runtime_evidence(tmp_path)


def test_bundle_rejects_dirty_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = _complete_evidence(tmp_path)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))
    monkeypatch.setattr(
        "tracefold.app.workers_runtime_acceptance_v2._repository_is_clean",
        lambda: False,
    )

    with pytest.raises(ValueError, match="workers_runtime_evidence_checkout_dirty"):
        seal_workers_runtime_evidence(tmp_path)


def test_template_is_current_and_cannot_seal(tmp_path: Path) -> None:
    template = workers_runtime_evidence_template()
    (tmp_path / "evidence.json").write_text(json.dumps(template))

    assert template["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert template["versions"]["migration_version"] == latest_migration_version()
    assert template["gates"]["offline_semantic_determinism"]["status"] == "pending"
    assert template["gates"]["startup_recovery"]["status"] == "pending"
    assert template["gates"]["real_continuous_30m"]["status"] == "pending"
    assert template["review"]["disposition"] == "pending"

    with pytest.raises(ValueError, match="workers_runtime_evidence_session_required"):
        seal_workers_runtime_evidence(tmp_path)


def _complete_evidence(root: Path) -> dict:
    evidence = workers_runtime_evidence_template()
    evidence["source"].update(
        {
            "session": "controlled-2026-07-31",
            "cutoff_at_ms": 1_800_000_000_000,
        }
    )
    evidence["versions"]["commit_sha"] = _repository_head()
    config_path = root / "operator-config.yaml"
    config_path.write_text("application:\n  environment: test\n")
    evidence["configuration"].update(
        {
            "config_path": str(config_path),
            "redacted_enablement": {
                "collector_enabled": True,
                "news_enabled": True,
                "macro_enabled": True,
            },
        }
    )
    raw_path = root / "raw-evidence.json"
    raw_proofs: dict[str, dict] = {}
    semantic = {
        "status": "passed",
        "domains": {
            domain: {
                "match": True,
                "baseline_sha256": hashlib.sha256(f"{domain}-same".encode()).hexdigest(),
                "candidate_sha256": hashlib.sha256(f"{domain}-same".encode()).hexdigest(),
            }
            for domain in ("radar", "news", "macro", "profile")
        },
    }
    offline = evidence["gates"]["offline_semantic_determinism"]
    offline["status"] = "passed"
    raw_proofs["offline_semantic_determinism_public_semantic_diff"] = semantic
    raw_proofs["offline_semantic_determinism_zero_write_replay"] = {
        "status": "passed",
        "tests_passed": 6,
        "serving_rows_written": 0,
    }
    raw_proofs["offline_semantic_determinism_migration_matrix"] = {
        "status": "passed",
        "outer_states": {state: True for state in ("clean", "dirty", "running", "retry_wait", "quarantined")},
        "native_retry_verified": True,
        "unknown_owner_abort_verified": True,
    }
    startup = evidence["gates"]["startup_recovery"]
    startup["status"] = "passed"
    for name in (
        "first_heartbeat_readiness",
        "fresh_row_collision",
        "singleton_exclusion",
        "forced_disconnect",
        "crash_restart",
        "native_model_recovery",
        "stale_claimant_rejection",
    ):
        raw_proofs[f"startup_recovery_{name}"] = {
            "status": "passed",
            "checks": {name: True},
        }
    startup["snapshot_restore"] = {
        "executed": True,
        "verified": True,
    }
    raw_proofs["startup_recovery_snapshot_restore"] = {
        "status": "passed",
        "executed": True,
        "verified": True,
        "restore_exit_code": 0,
        "backup_sha256": "1" * 64,
        "restored_table_count": 91,
    }
    real = evidence["gates"]["real_continuous_30m"]
    real["status"] = "passed"
    real["duration_seconds"] = 1_800
    real["runtime"] = {
        "continuous_readiness": True,
        "continuous_heartbeat": True,
        "restart_count": 0,
    }
    start_at_ms = 1_800_000_000_000
    end_at_ms = start_at_ms + 1_800_000
    raw_proofs["real_continuous_30m_runtime"] = {
        "status": "passed",
        "start_at_ms": start_at_ms,
        "end_at_ms": end_at_ms,
        "samples": [
            {
                "at_ms": at_ms,
                "heartbeat_at_ms": at_ms,
                "state": "running",
                "ready": True,
                "runtime_id": "runtime-test",
                "process_id": 1234,
            }
            for at_ms in range(start_at_ms, end_at_ms + 1, 15_000)
        ],
    }
    raw_proofs["real_continuous_30m_capacity_interval"] = {
        "status": "passed",
        "duration_seconds": 1_800,
        "deadline_misses": dict(real["deadline_misses"]),
        "unresolved_projection_quarantine": 0,
        "capacity": json.loads(json.dumps(real["capacity"])),
    }
    for name in (
        "process_resources",
        "postgres",
        "resource_admission_service",
    ):
        raw_proofs[f"real_continuous_30m_{name}"] = {
            "status": "passed",
            "checks": {f"{name}_bounded": True},
        }
    raw_proofs["real_continuous_30m_public_semantic_diff"] = semantic
    raw_artifact = {
        "schema_version": RAW_EVIDENCE_SCHEMA_VERSION,
        "source": evidence["source"],
        "versions": evidence["versions"],
        "configuration": evidence["configuration"],
        "proofs": raw_proofs,
    }
    raw_path.write_text(json.dumps(raw_artifact))

    def proof() -> dict:
        return {
            "ok": True,
            "artifact_path": raw_path.name,
            "artifact_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        }

    for name in ("public_semantic_diff", "zero_write_replay", "migration_matrix"):
        offline[name] = proof()
    for name in (
        "first_heartbeat_readiness",
        "fresh_row_collision",
        "singleton_exclusion",
        "forced_disconnect",
        "crash_restart",
        "native_model_recovery",
        "stale_claimant_rejection",
    ):
        startup[name] = proof()
    startup["snapshot_restore"].update(proof())
    for name in (
        "capacity_interval",
        "runtime",
        "process_resources",
        "postgres",
        "resource_admission_service",
        "public_semantic_diff",
    ):
        real[name].update(proof())

    review_path = root / "review.json"
    reviewed_at_ms = 1_800_001_800_000
    review_artifact = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "source": evidence["source"],
        "versions": evidence["versions"],
        "configuration": evidence["configuration"],
        "disposition": "pass",
        "reviewer": "independent-test-reviewer",
        "reviewed_at_ms": reviewed_at_ms,
        "reviewed_artifacts": {
            raw_path.name: hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        },
    }
    review_path.write_text(json.dumps(review_artifact))
    evidence["review"] = {
        "disposition": "pass",
        "reviewer": "independent-test-reviewer",
        "reviewed_at_ms": reviewed_at_ms,
        "artifact_path": review_path.name,
        "artifact_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
    }
    return evidence


def _refresh_artifact_hashes(root: Path, evidence: dict) -> None:
    raw_path = root / "raw-evidence.json"
    raw_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    for gate in evidence["gates"].values():
        for value in gate.values():
            if isinstance(value, dict) and value.get("artifact_path") == raw_path.name:
                value["artifact_sha256"] = raw_hash
    review_path = root / "review.json"
    review = json.loads(review_path.read_text())
    review["reviewed_artifacts"] = {raw_path.name: raw_hash}
    review_path.write_text(json.dumps(review))
    evidence["review"]["artifact_sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
