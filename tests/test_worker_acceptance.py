from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracefold.app.worker_acceptance import (
    EVIDENCE_SCHEMA_VERSION,
    SEAL_FILE,
    issue_32_evidence_template,
    seal_issue_32_evidence,
)
from tracefold.app.worker_manifest import worker_names
from tracefold.platform.postgres.postgres_audit import (
    PUBLIC_NO_SQL_ROUTES,
    PUBLIC_ROUTE_QUERY_COVERAGE,
)
from tracefold.platform.postgres.postgres_migrations import latest_migration_version


def test_complete_issue_32_bundle_seals_once_and_detects_later_mutation(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_complete_evidence()))
    supporting = tmp_path / "metrics.jsonl"
    supporting.write_text('{"cpu":1}\n')

    seal = seal_issue_32_evidence(tmp_path)
    repeated = seal_issue_32_evidence(tmp_path)

    assert repeated == seal
    assert (tmp_path / SEAL_FILE).is_file()
    assert {item["path"] for item in seal["files"]} == {
        "evidence.json",
        "metrics.jsonl",
    }

    supporting.write_text('{"cpu":2}\n')
    with pytest.raises(ValueError, match="issue_32_sealed_bundle_modified"):
        seal_issue_32_evidence(tmp_path)


def test_issue_32_bundle_refuses_short_real_run(tmp_path: Path) -> None:
    evidence = _complete_evidence()
    evidence["gates"]["real_continuous_30m"]["duration_seconds"] = 1_799
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(ValueError, match="issue_32_real_run_too_short"):
        seal_issue_32_evidence(tmp_path)

    assert not (tmp_path / SEAL_FILE).exists()


def test_issue_32_bundle_refuses_unreviewed_or_incomplete_query_evidence(
    tmp_path: Path,
) -> None:
    evidence = _complete_evidence()
    evidence["review"]["outcome"] = "pending"
    evidence["gates"]["real_continuous_30m"]["postgres"]["query_audit"]["route_coverage"]["missing_query_names"] = [
        "news_feed"
    ]
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))

    with pytest.raises(
        ValueError,
        match=r"query_route_coverage_incomplete|reviewer_pass_required",
    ):
        seal_issue_32_evidence(tmp_path)


def test_issue_32_template_is_current_and_deliberately_cannot_seal(
    tmp_path: Path,
) -> None:
    template = issue_32_evidence_template()
    (tmp_path / "evidence.json").write_text(json.dumps(template))

    assert template["versions"]["migration_version"] == latest_migration_version()
    assert template["gates"]["controlled_offline"]["status"] == "pending"
    assert template["gates"]["startup_recovery"]["status"] == "pending"
    assert template["gates"]["real_continuous_30m"]["status"] == "pending"
    assert template["gates"]["real_continuous_30m"]["steady_paths"] == []
    assert template["review"]["outcome"] == "pending"

    with pytest.raises(ValueError, match="issue_32_evidence_session_required"):
        seal_issue_32_evidence(tmp_path)

    assert not (tmp_path / SEAL_FILE).exists()


def _complete_evidence() -> dict:
    offline_query_audit = _query_audit(analyze=False)
    real_query_audit = _query_audit(analyze=True)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "issue": 32,
        "source": {
            "repository": "AnalyThothAI/tracefold",
            "session": "controlled-2026-07-30",
            "cutoff_at_ms": 1_800_000_000_000,
        },
        "versions": {
            "commit_sha": "a" * 40,
            "migration_version": latest_migration_version(),
        },
        "configuration": {
            "config_path": "/operator/tracefold/config.yaml",
            "workers_config_path": None,
            "redacted_enablement": {
                "collector_enabled": True,
                "news_enabled": True,
                "macro_enabled": True,
            },
        },
        "gates": {
            "controlled_offline": {
                "status": "passed",
                "workload_counts": {"events": 100},
                "semantic_diff": {"ok": True},
                "permission_tests": {"ok": True},
                "query_audit": offline_query_audit,
            },
            "startup_recovery": {
                "status": "passed",
                "recovered_claims": {"count": 2},
                "singleton_lock": {"ok": True},
                "startup_scan": {
                    "ok": True,
                    "full_rebuild_or_backlog_scan": False,
                },
            },
            "real_continuous_30m": {
                "status": "passed",
                "duration_seconds": 1_800,
                "steady_paths": list(worker_names()),
                "workload_counts": {"events": 100},
                "resource_metrics": {"ok": True},
                "endpoint_latency": {"ok": True},
                "shard_timings": {"ok": True},
                "lane_pool_waits": {"ok": True},
                "fact_commit_latency": {"ok": True},
                "queue_age": {"ok": True},
                "postgres": {"ok": True, "query_audit": real_query_audit},
                "semantic_diff": {"ok": True},
                "permission_tests": {"ok": True},
                "runtime_status": {"ok": True},
                "model_reservation": {"ok": True},
            },
        },
        "review": {
            "outcome": "pass",
            "reviewer": "test-reviewer",
            "reviewed_at_ms": 1_800_001_800_000,
        },
    }


def _query_audit(*, analyze: bool) -> dict:
    query_names = sorted(
        {query_name for route_query_names in PUBLIC_ROUTE_QUERY_COVERAGE.values() for query_name in route_query_names}
    )
    return {
        "ok": True,
        "analyze": analyze,
        "route_coverage": {
            "query_routes": {route: list(names) for route, names in PUBLIC_ROUTE_QUERY_COVERAGE.items()},
            "no_sql_routes": sorted(PUBLIC_NO_SQL_ROUTES),
            "missing_query_names": [],
        },
        "queries": [
            {
                "name": name,
                "ok": True,
                "violations": [],
            }
            for name in query_names
        ],
    }
