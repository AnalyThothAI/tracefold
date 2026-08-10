from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tracefold.app.workers_runtime import WORKERS_RUNTIME_VERSION
from tracefold.app.workers_runtime_acceptance_v2 import (
    EVIDENCE_SCHEMA_VERSION,
    RAW_EVIDENCE_SCHEMA_VERSION,
    REVIEW_SCHEMA_VERSION,
    SEAL_FILE,
    _repository_head,
    seal_workers_runtime_evidence,
    workers_runtime_evidence_template,
)
from tracefold.app.workers_runtime_collector import (
    COLLECTION_FILE,
    COLLECTION_SCHEMA_VERSION,
    SAMPLES_FILE,
    _collect_fixed_interval,
    _CollectorDependencies,
    _summarize,
)
from tracefold.platform.postgres.postgres_audit import (
    HOT_QUERIES,
    PUBLIC_NO_SQL_ROUTES,
    PUBLIC_ROUTE_QUERY_COVERAGE,
)
from tracefold.platform.postgres.postgres_migrations import latest_migration_version
from tracefold.platform.postgres.projection_frontier import FRONTIER_SPECS

_FRONTIER_DOMAINS = tuple(spec.domain for spec in FRONTIER_SPECS)
_DEADLINE_DOMAINS = ("radar", *_FRONTIER_DOMAINS)

_AUTHORIZATION_URL = "https://github.com/AnalyThothAI/tracefold/issues/33#issuecomment-5149965794"
_AUTHORIZED_BY = "aaurix"
_AUTHORIZED_AT_MS = 1_785_561_696_000
_AUTHORIZATION_STATEMENT = (
    "## Authoritative production hard-cut authorization record — 2026-08-01\n"
    "\n"
    "This records the production operator authorization relayed in the active Codex task and "
    "supersedes every snapshot, backup, restore-drill, and snapshot_restore acceptance clause "
    "in this Issue for the current cutover.\n"
    "\n"
    "Authorized boundary:\n"
    "\n"
    "- deploy the verified implementation by merging directly into `main` and cutting over "
    "the existing production PostgreSQL database in place;\n"
    "- downtime is allowed;\n"
    "- do not create a backup or snapshot and do not perform a restore drill;\n"
    "- old and new Workers must never overlap;\n"
    "- if any gate fails, stop the new Workers and fix forward on the current database before "
    "retrying;\n"
    "- no waiver, placeholder hash, or manually asserted boolean may represent restore or "
    "runtime acceptance as passed.\n"
    "\n"
    "This comment records authorization only. It does not claim that implementation, "
    "deployment, the continuous 30-minute gate, independent review, or Issue completion has "
    "passed."
)
_AUTHORIZATION_STATEMENT_SHA256 = "0fcdc80cfa8e20d74c5f9fcb92bbc6cb611e807b489f2386a260a6afea022886"


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
        COLLECTION_FILE,
        SAMPLES_FILE,
    }

    supporting.write_text('{"cpu":2}\n')
    with pytest.raises(ValueError, match="workers_runtime_sealed_bundle_modified"):
        seal_workers_runtime_evidence(tmp_path)


def test_bundle_refuses_short_real_run(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    evidence["gates"]["real_continuous_30m"]["duration_seconds"] = 1_799
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match="workers_runtime_real_run_too_short"):
        seal_workers_runtime_evidence(tmp_path)
    assert not (tmp_path / SEAL_FILE).exists()


def test_bundle_rejects_historical_snapshot_restore_startup_schema(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    startup = evidence["gates"]["startup_recovery"]
    old_proof = startup.pop("operator_authorized_fix_forward_boundary")
    startup["snapshot_restore"] = old_proof
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match="workers_runtime_startup_recovery_proof_set_invalid"):
        seal_workers_runtime_evidence(tmp_path)
    assert not (tmp_path / SEAL_FILE).exists()


def test_operator_authorized_fix_forward_boundary_rejects_bare_checks(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    raw = json.loads((tmp_path / "raw-evidence.json").read_text())
    raw["proofs"]["startup_recovery_operator_authorized_fix_forward_boundary"] = {
        "status": "passed",
        "checks": {"operator_authorized": True, "fix_forward": True},
    }
    (tmp_path / "raw-evidence.json").write_text(json.dumps(raw))
    _refresh_artifact_hashes(tmp_path, evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(
        ValueError,
        match="workers_runtime_operator_authorized_fix_forward_boundary_shape_invalid",
    ):
        seal_workers_runtime_evidence(tmp_path)
    assert not (tmp_path / SEAL_FILE).exists()


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("backup_policy", "snapshot_first", "workers_runtime_operator_authorized_fix_forward_boundary_invalid"),
        ("recovery_policy", "restore", "workers_runtime_operator_authorized_fix_forward_boundary_invalid"),
    ),
)
def test_operator_authorized_fix_forward_boundary_is_exact(
    tmp_path: Path,
    field: str,
    value: str,
    error: str,
) -> None:
    evidence = _complete_evidence(tmp_path)
    raw = json.loads((tmp_path / "raw-evidence.json").read_text())
    raw["proofs"]["startup_recovery_operator_authorized_fix_forward_boundary"]["boundary"][field] = value
    (tmp_path / "raw-evidence.json").write_text(json.dumps(raw))
    _refresh_artifact_hashes(tmp_path, evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match=error):
        seal_workers_runtime_evidence(tmp_path)
    assert not (tmp_path / SEAL_FILE).exists()


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        (
            "source_url",
            "https://github.com/AnalyThothAI/tracefold/issues/33#issuecomment-5149965795",
            "workers_runtime_operator_authorization_source_url_invalid",
        ),
        ("authorized_by", "another-user", "workers_runtime_operator_authorization_identity_invalid"),
        ("authorized_at_ms", _AUTHORIZED_AT_MS + 1, "workers_runtime_operator_authorization_time_invalid"),
        ("statement", f"{_AUTHORIZATION_STATEMENT}\n", "workers_runtime_operator_authorization_statement_invalid"),
        (
            "statement_sha256",
            "f" * 64,
            "workers_runtime_operator_authorization_statement_hash_invalid",
        ),
    ),
)
def test_operator_authorization_is_exactly_bound_to_issue_comment(
    tmp_path: Path,
    field: str,
    value: object,
    error: str,
) -> None:
    evidence = _complete_evidence(tmp_path)
    raw = json.loads((tmp_path / "raw-evidence.json").read_text())
    authorization = raw["proofs"]["startup_recovery_operator_authorized_fix_forward_boundary"]["authorization"]
    authorization[field] = value
    (tmp_path / "raw-evidence.json").write_text(json.dumps(raw))
    _refresh_artifact_hashes(tmp_path, evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match=error):
        seal_workers_runtime_evidence(tmp_path)
    assert not (tmp_path / SEAL_FILE).exists()


def test_operator_authorization_rejects_rehashed_statement_tampering(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    raw = json.loads((tmp_path / "raw-evidence.json").read_text())
    authorization = raw["proofs"]["startup_recovery_operator_authorized_fix_forward_boundary"]["authorization"]
    authorization["statement"] = "different instruction"
    authorization["statement_sha256"] = hashlib.sha256(authorization["statement"].encode()).hexdigest()
    (tmp_path / "raw-evidence.json").write_text(json.dumps(raw))
    _refresh_artifact_hashes(tmp_path, evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match="workers_runtime_operator_authorization_statement_invalid"):
        seal_workers_runtime_evidence(tmp_path)
    assert not (tmp_path / SEAL_FILE).exists()


def test_bundle_rejects_collection_summary_mutation_even_when_hashes_are_refreshed(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    collection_path = tmp_path / COLLECTION_FILE
    collection = json.loads(collection_path.read_text())
    collection["summary"]["postgres"]["max_worker_connections"] += 1
    collection_path.write_text(json.dumps(collection))
    _refresh_artifact_hashes(tmp_path, evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match="workers_runtime_collection_summary_mismatch"):
        seal_workers_runtime_evidence(tmp_path)


def test_bundle_rejects_jsonl_mutation_even_when_all_declared_hashes_are_refreshed(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    samples_path = tmp_path / SAMPLES_FILE
    lines = samples_path.read_text().splitlines()
    sample = json.loads(lines[90])
    sample["container"]["process_rss_bytes"] += 1
    lines[90] = json.dumps(sample)
    samples_path.write_text("\n".join(lines) + "\n")
    collection_path = tmp_path / COLLECTION_FILE
    collection = json.loads(collection_path.read_text())
    collection["samples_sha256"] = hashlib.sha256(samples_path.read_bytes()).hexdigest()
    collection_path.write_text(json.dumps(collection))
    _refresh_artifact_hashes(tmp_path, evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match="workers_runtime_collection_summary_mismatch"):
        seal_workers_runtime_evidence(tmp_path)


def test_bundle_rejects_collection_metadata_not_bound_to_evidence(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    collection_path = tmp_path / COLLECTION_FILE
    collection = json.loads(collection_path.read_text())
    collection["source"]["session"] = "different-production-session"
    collection_path.write_text(json.dumps(collection))
    _refresh_artifact_hashes(tmp_path, evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match="workers_runtime_collection_source_mismatch"):
        seal_workers_runtime_evidence(tmp_path)


def test_bundle_revalidates_runtime_identity_from_jsonl(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    samples_path = tmp_path / SAMPLES_FILE
    samples = [json.loads(line) for line in samples_path.read_text().splitlines()]
    samples[90]["probe"]["runtime_id"] = "replacement-runtime"
    samples_path.write_text("".join(f"{json.dumps(sample)}\n" for sample in samples))
    collection_path = tmp_path / COLLECTION_FILE
    collection = json.loads(collection_path.read_text())
    collection["samples_sha256"] = hashlib.sha256(samples_path.read_bytes()).hexdigest()
    collection["summary"] = _summarize(samples)
    collection_path.write_text(json.dumps(collection))
    _refresh_artifact_hashes(tmp_path, evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match="workers_runtime_collection_checks_failed"):
        seal_workers_runtime_evidence(tmp_path)


def test_bundle_revalidates_heartbeat_freshness_from_jsonl(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    samples_path = tmp_path / SAMPLES_FILE
    samples = [json.loads(line) for line in samples_path.read_text().splitlines()]
    samples[90]["probe"]["heartbeat_at_ms"] = samples[90]["at_ms"] - 15_001
    samples_path.write_text("".join(f"{json.dumps(sample)}\n" for sample in samples))
    collection_path = tmp_path / COLLECTION_FILE
    collection = json.loads(collection_path.read_text())
    collection["samples_sha256"] = hashlib.sha256(samples_path.read_bytes()).hexdigest()
    collection_path.write_text(json.dumps(collection))
    _refresh_artifact_hashes(tmp_path, evidence)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match="workers_runtime_collection_sample_invalid:worker_heartbeat_stale"):
        seal_workers_runtime_evidence(tmp_path)


def test_bundle_rejects_retired_hand_fillable_real_proof(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    evidence["gates"]["real_continuous_30m"]["runtime"] = {
        "ok": True,
        "artifact_path": "raw-evidence.json",
        "artifact_sha256": hashlib.sha256((tmp_path / "raw-evidence.json").read_bytes()).hexdigest(),
    }
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match="workers_runtime_real_continuous_30m_proof_set_invalid"):
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
    assert "operator_authorized_fix_forward_boundary" in template["gates"]["startup_recovery"]
    assert "snapshot_restore" not in template["gates"]["startup_recovery"]
    real = template["gates"]["real_continuous_30m"]
    assert real["status"] == "pending"
    assert set(real) == {
        "status",
        "duration_seconds",
        "deadline_misses",
        "unresolved_projection_quarantine",
        "capacity",
        "production_collection",
        "public_semantic_diff",
    }
    assert template["review"]["disposition"] == "pending"

    with pytest.raises(ValueError, match="workers_runtime_evidence_session_required"):
        seal_workers_runtime_evidence(tmp_path)


def _complete_evidence(root: Path) -> dict:
    (root / COLLECTION_FILE).unlink(missing_ok=True)
    (root / SAMPLES_FILE).unlink(missing_ok=True)
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
    raw_proofs["startup_recovery_operator_authorized_fix_forward_boundary"] = {
        "status": "passed",
        "authorization": {
            "source_kind": "github_issue_comment",
            "source_url": _AUTHORIZATION_URL,
            "authority_role": "production_operator",
            "authorized_by": _AUTHORIZED_BY,
            "authorized_at_ms": _AUTHORIZED_AT_MS,
            "statement": _AUTHORIZATION_STATEMENT,
            "statement_sha256": _AUTHORIZATION_STATEMENT_SHA256,
        },
        "boundary": {
            "deployment_target": "main_production",
            "database_migration": "in_place",
            "backup_policy": "no_backup",
            "recovery_policy": "fix_forward",
            "downtime_allowed": True,
        },
    }
    real = evidence["gates"]["real_continuous_30m"]
    real["status"] = "passed"
    clock = _AcceptanceClock()
    collection = _collect_fixed_interval(
        root,
        metadata={
            "source": evidence["source"],
            "versions": evidence["versions"],
            "configuration": evidence["configuration"],
        },
        dependencies=_CollectorDependencies(
            clock_ms=clock.clock_ms,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            read_sample=lambda sequence: _collection_sample(
                sequence,
                clock=clock,
                commit=evidence["versions"]["commit_sha"],
            ),
        ),
    )
    summary = collection["summary"]
    real["duration_seconds"] = summary["duration_seconds"]
    real["deadline_misses"] = summary["deadline_misses"]
    real["unresolved_projection_quarantine"] = summary["unresolved_projection_quarantine"]
    real["capacity"] = summary["capacity"]
    raw_proofs["real_continuous_30m_public_semantic_diff"] = semantic
    raw_artifact = {
        "schema_version": RAW_EVIDENCE_SCHEMA_VERSION,
        "source": evidence["source"],
        "versions": evidence["versions"],
        "configuration": evidence["configuration"],
        "proofs": raw_proofs,
    }
    raw_path.write_text(json.dumps(raw_artifact))

    def proof(path: Path) -> dict:
        return {
            "ok": True,
            "artifact_path": path.name,
            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    for name in ("public_semantic_diff", "zero_write_replay", "migration_matrix"):
        offline[name] = proof(raw_path)
    for name in (
        "first_heartbeat_readiness",
        "fresh_row_collision",
        "singleton_exclusion",
        "forced_disconnect",
        "crash_restart",
        "native_model_recovery",
        "stale_claimant_rejection",
        "operator_authorized_fix_forward_boundary",
    ):
        startup[name] = proof(raw_path)
    real["production_collection"] = proof(root / COLLECTION_FILE)
    real["public_semantic_diff"] = proof(raw_path)

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
            COLLECTION_FILE: hashlib.sha256((root / COLLECTION_FILE).read_bytes()).hexdigest(),
            SAMPLES_FILE: hashlib.sha256((root / SAMPLES_FILE).read_bytes()).hexdigest(),
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


class _AcceptanceClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.base_ms = 1_800_000_000_000

    def monotonic(self) -> float:
        return self.seconds

    def clock_ms(self) -> int:
        return self.base_ms + int(self.seconds * 1_000)

    def sleep(self, seconds: float) -> None:
        self.seconds += max(0.0, float(seconds))


def _collection_sample(sequence: int, *, clock: _AcceptanceClock, commit: str) -> dict:
    at_ms = clock.clock_ms()
    domains = _FRONTIER_DOMAINS
    return {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "sequence": sequence,
        "scheduled_offset_seconds": sequence * 10,
        "at_ms": at_ms,
        "status": "passed",
        "checkout": {"commit_sha": commit, "clean": True},
        "probe": {
            "ok": True,
            "ready": True,
            "runtime_id": "runtime-test",
            "runtime_version": WORKERS_RUNTIME_VERSION,
            "runtime_revision": commit,
            "process_id": 1234,
            "lifecycle_state": "running",
            "heartbeat_at_ms": at_ms,
            "heartbeat_stale_after_ms": 15_000,
            "unavailable_reason": None,
            "probe_rtt_ms": 1.0,
        },
        "container": {
            "container_id": "container-test",
            "image_id": "image-test",
            "image_revision": commit,
            "restart_count": 0,
            "running": True,
            "oom_killed": False,
            "host_process_id": 5678,
            "process_rss_bytes": 256 * 1024 * 1024,
            "container_memory_bytes": 512 * 1024 * 1024,
        },
        "serve_container": {
            "container_id": "serve-container-test",
            "image_id": "image-test",
            "image_revision": commit,
            "restart_count": 0,
            "running": True,
            "oom_killed": False,
            "host_process_id": 5679,
        },
        "postgres": {
            "worker_connections": 4,
            "lock_wait_count": 0,
            "max_lock_wait_seconds": 0.0,
            "waits_by_type": {"Client": 3, "none": 1},
            "max_transaction_seconds": 0.1,
            "temp_files": 7,
            "temp_bytes": 4096,
            "frontiers": {
                domain: {
                    "actionable_count": 0,
                    "oldest_age_ms": 0,
                    "unresolved_deadline_misses": 0,
                    "unresolved_quarantine": 0,
                    "counts_by_status": {},
                }
                for domain in domains
            },
            **({"query_audit": _query_audit()} if sequence == 0 else {}),
        },
        "token_radar_api": {
            "unconditional": {
                "status": 200,
                "latency_ms": 10.0,
                "bytes": 1024,
                "items": 1,
                "etag_sha256": "b" * 64,
                "data_sha256": "c" * 64,
            },
            "conditional": {
                "status": 304,
                "latency_ms": 5.0,
                "bytes": 0,
                "etag_sha256": "b" * 64,
            },
        },
        "token_radar_database": {
            "before": {"data_sha256": "c" * 64, "items": 1},
            "after": {"data_sha256": "c" * 64, "items": 1},
        },
        "telemetry": {
            "metric_families": sorted(
                {
                    "tracefold_worker_projection_deadline_misses",
                    "tracefold_worker_projection_transitions",
                    "tracefold_worker_processing_seconds",
                    "tracefold_worker_jobs",
                    "tracefold_worker_last_run_timestamp_seconds",
                    "tracefold_worker_projection_rows",
                    "tracefold_worker_projection_bytes",
                    "tracefold_worker_projection_cache",
                    "tracefold_worker_resource_active",
                    "tracefold_worker_resource_admission_seconds",
                    "tracefold_worker_resource_service_seconds",
                }
            ),
            "resource_active": {
                "database_business": 0.0,
                "database_control": 0.0,
                "finite_operation": 0.0,
                "model_adapter": 0.0,
                "cpu_process": 0.0,
            },
            "projection_deadline_misses_total": {domain: 0.0 for domain in _DEADLINE_DOMAINS},
            "projection_transitions_total": {domain: {"arrival": 0.0, "completion": 0.0} for domain in domains},
            "last_run_timestamp_seconds": {
                "token_radar_current": at_ms / 1_000 - float(sequence % 3) * 10.0,
            },
            "projection_rows": [
                {
                    "labels": {"worker": "token_radar_current", "stage": stage},
                    "value": float(value),
                }
                for stage, value in (("input", 12), ("eligible", 3), ("public", 3))
            ],
            "projection_bytes": [
                {
                    "labels": {"worker": "token_radar_current", "direction": direction},
                    "value": float(value),
                }
                for direction, value in (("input", 1024), ("output", 512))
            ],
            "processing_seconds": _processing_rows(count=sequence // 3 + 1),
            "jobs_total": [
                {
                    "labels": {"worker": "token_radar_current", "status": "published"},
                    "value": float(sequence // 3 + 1),
                }
            ],
            "resource_service": _resource_rows(
                "service",
                count=sequence + 1,
                seconds=(sequence + 1) * 0.002,
            ),
            "resource_admission": _resource_rows(
                "admission",
                count=sequence + 1,
                seconds=(sequence + 1) * 0.001,
            ),
        },
    }


def _processing_rows(*, count: int) -> list[dict]:
    return [
        {
            "name": "tracefold_worker_processing_seconds_bucket",
            "labels": {"worker": "token_radar_current", "le": boundary},
            "value": float(count),
        }
        for boundary in ("2.0", "5.0", "+Inf")
    ] + [
        {
            "name": "tracefold_worker_processing_seconds_count",
            "labels": {"worker": "token_radar_current"},
            "value": float(count),
        },
        {
            "name": "tracefold_worker_processing_seconds_sum",
            "labels": {"worker": "token_radar_current"},
            "value": float(count),
        },
    ]


def _resource_rows(kind: str, *, count: int, seconds: float) -> list[dict]:
    prefix = f"tracefold_worker_resource_{kind}_seconds"
    labels = {
        "capability": "database_control",
        "operation": "runtime_heartbeat",
        "outcome": "accepted" if kind == "admission" else "success",
    }
    return [
        {"name": f"{prefix}_count", "labels": labels, "value": float(count)},
        {"name": f"{prefix}_sum", "labels": labels, "value": float(seconds)},
    ]


def _query_audit() -> dict:
    metrics = {
        "plan_json_valid": True,
        "execution_time_ms": 0.1,
        "planning_time_ms": 0.1,
        "returned_rows": 0,
        "read_rows": 0,
        "read_return_amplification": 0.0,
        "temp_read_blocks": 0,
        "temp_written_blocks": 0,
        "large_seq_scans": [],
    }
    return {
        "ok": True,
        "engine": "postgresql",
        "analyze": True,
        "thresholds": {
            "large_seq_scan_plan_rows": 10_000,
            "max_read_return_amplification": 100,
            "temp_blocks": 0,
        },
        "route_coverage": {
            "query_routes": json.loads(json.dumps(PUBLIC_ROUTE_QUERY_COVERAGE)),
            "no_sql_routes": sorted(PUBLIC_NO_SQL_ROUTES),
            "missing_query_names": [],
        },
        "queries": [
            {
                "ok": True,
                "name": str(query["name"]),
                "plan": [{"Plan": {"Node Type": "Result"}}],
                "metrics": dict(metrics),
                "violations": [],
            }
            for query in HOT_QUERIES
        ],
    }


def _refresh_artifact_hashes(root: Path, evidence: dict) -> None:
    reviewed_artifacts: dict[str, str] = {}
    for gate in evidence["gates"].values():
        for value in gate.values():
            if isinstance(value, dict) and value.get("artifact_path"):
                artifact_path = root / value["artifact_path"]
                artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                value["artifact_sha256"] = artifact_hash
                reviewed_artifacts[value["artifact_path"]] = artifact_hash
    samples_path = root / SAMPLES_FILE
    reviewed_artifacts[SAMPLES_FILE] = hashlib.sha256(samples_path.read_bytes()).hexdigest()
    review_path = root / "review.json"
    review = json.loads(review_path.read_text())
    review["reviewed_artifacts"] = reviewed_artifacts
    review_path.write_text(json.dumps(review))
    evidence["review"]["artifact_sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
