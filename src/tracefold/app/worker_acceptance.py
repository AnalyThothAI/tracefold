from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from tracefold.app.worker_manifest import worker_names
from tracefold.platform.postgres.postgres_audit import (
    PUBLIC_NO_SQL_ROUTES,
    PUBLIC_ROUTE_QUERY_COVERAGE,
)
from tracefold.platform.postgres.postgres_migrations import latest_migration_version

EVIDENCE_SCHEMA_VERSION = "issue_32_worker_acceptance_v1"
SEAL_SCHEMA_VERSION = "issue_32_worker_acceptance_seal_v1"
EVIDENCE_FILE = "evidence.json"
SEAL_FILE = "seal.json"
MINIMUM_REAL_RUN_SECONDS = 30 * 60
MAX_BUNDLE_BYTES = 100 * 1024 * 1024

_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_REAL_SECTIONS = (
    "workload_counts",
    "resource_metrics",
    "endpoint_latency",
    "shard_timings",
    "lane_pool_waits",
    "fact_commit_latency",
    "queue_age",
    "postgres",
    "semantic_diff",
    "permission_tests",
    "runtime_status",
    "model_reservation",
)


def seal_issue_32_evidence(bundle_dir: Path) -> dict[str, Any]:
    root = bundle_dir.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("issue_32_evidence_bundle_directory_required")
    evidence_path = root / EVIDENCE_FILE
    if not evidence_path.is_file() or evidence_path.is_symlink():
        raise ValueError("issue_32_evidence_json_required")
    evidence = _read_json(evidence_path)
    _validate_evidence(evidence)

    existing_seal = root / SEAL_FILE
    if existing_seal.exists():
        seal = _read_json(existing_seal)
        _validate_existing_seal(root, seal)
        return seal

    files = _bundle_files(root)
    manifest = [_file_evidence(root, path) for path in files]
    root_hash = _sha256_bytes(_canonical_json(manifest))
    seal = {
        "schema_version": SEAL_SCHEMA_VERSION,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "sealed_at_ms": int(time.time() * 1_000),
        "root_sha256": root_hash,
        "files": manifest,
    }
    temporary = root / f".{SEAL_FILE}.{os.getpid()}.tmp"
    try:
        temporary.write_bytes(_canonical_json(seal) + b"\n")
        os.replace(temporary, existing_seal)
    finally:
        temporary.unlink(missing_ok=True)
    return seal


def issue_32_evidence_template() -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "issue": 32,
        "source": {
            "repository": "AnalyThothAI/tracefold",
            "session": "",
            "cutoff_at_ms": 0,
        },
        "versions": {
            "commit_sha": "",
            "migration_version": latest_migration_version(),
        },
        "configuration": {
            "config_path": "",
            "workers_config_path": None,
            "redacted_enablement": {},
        },
        "gates": {
            "controlled_offline": {
                "status": "pending",
                "workload_counts": {},
                "semantic_diff": {},
                "permission_tests": {},
                "query_audit": {},
            },
            "startup_recovery": {
                "status": "pending",
                "recovered_claims": {},
                "singleton_lock": {},
                "startup_scan": {},
            },
            "real_continuous_30m": {
                "status": "pending",
                "duration_seconds": 0,
                "steady_paths": [],
                **{name: {} for name in _REQUIRED_REAL_SECTIONS},
            },
        },
        "review": {
            "outcome": "pending",
            "reviewer": "",
            "reviewed_at_ms": 0,
        },
    }


def _validate_evidence(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("issue_32_evidence_object_required")
    if payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION or payload.get("issue") != 32:
        raise ValueError("issue_32_evidence_schema_invalid")

    source = _mapping(payload, "source")
    if source.get("repository") != "AnalyThothAI/tracefold":
        raise ValueError("issue_32_evidence_repository_invalid")
    _required_text(source, "session")
    _required_int(source, "cutoff_at_ms")

    versions = _mapping(payload, "versions")
    commit_sha = _required_text(versions, "commit_sha")
    if not _COMMIT_PATTERN.fullmatch(commit_sha):
        raise ValueError("issue_32_evidence_commit_sha_invalid")
    if versions.get("migration_version") != latest_migration_version():
        raise ValueError("issue_32_evidence_migration_version_invalid")

    configuration = _mapping(payload, "configuration")
    config_path = Path(_required_text(configuration, "config_path"))
    if not config_path.is_absolute():
        raise ValueError("issue_32_evidence_config_path_must_be_absolute")
    if configuration.get("workers_config_path") is not None:
        raise ValueError("issue_32_evidence_workers_config_forbidden")
    enablement = _mapping(configuration, "redacted_enablement")
    if not enablement or any(not isinstance(value, (bool, type(None))) for value in enablement.values()):
        raise ValueError("issue_32_evidence_redacted_enablement_invalid")
    if any(_looks_sensitive(key) for key in enablement):
        raise ValueError("issue_32_evidence_sensitive_configuration_forbidden")

    gates = _mapping(payload, "gates")
    offline = _passed_gate(gates, "controlled_offline")
    startup = _passed_gate(gates, "startup_recovery")
    real = _passed_gate(gates, "real_continuous_30m")
    _required_sections(
        offline,
        (
            "workload_counts",
            "semantic_diff",
            "permission_tests",
            "query_audit",
        ),
        gate="controlled_offline",
    )
    _require_ok(offline, "semantic_diff", gate="controlled_offline")
    _require_ok(offline, "permission_tests", gate="controlled_offline")
    _require_query_audit(offline, gate="controlled_offline", analyze=False)

    _required_sections(
        startup,
        (
            "recovered_claims",
            "singleton_lock",
            "startup_scan",
        ),
        gate="startup_recovery",
    )
    if startup["startup_scan"].get("full_rebuild_or_backlog_scan") is not False:
        raise ValueError("issue_32_startup_scan_contract_failed")

    if _required_int(real, "duration_seconds") < MINIMUM_REAL_RUN_SECONDS:
        raise ValueError("issue_32_real_run_too_short")
    _required_sections(real, _REQUIRED_REAL_SECTIONS, gate="real_continuous_30m")
    paths = set(_string_list(real, "steady_paths"))
    if paths != set(worker_names()):
        raise ValueError("issue_32_real_run_steady_paths_incomplete")
    for section in (
        "resource_metrics",
        "endpoint_latency",
        "shard_timings",
        "lane_pool_waits",
        "fact_commit_latency",
        "queue_age",
        "runtime_status",
        "model_reservation",
    ):
        _require_ok(real, section, gate="real_continuous_30m")
    _require_ok(real, "semantic_diff", gate="real_continuous_30m")
    _require_ok(real, "permission_tests", gate="real_continuous_30m")
    postgres = _mapping(real, "postgres")
    if postgres.get("ok") is not True:
        raise ValueError("issue_32_real_continuous_30m_postgres_failed")
    query_audit = postgres.get("query_audit")
    if not isinstance(query_audit, dict):
        raise ValueError("issue_32_real_continuous_30m_query_audit_required")
    _validate_query_audit(query_audit, analyze=True, gate="real_continuous_30m")

    review = _mapping(payload, "review")
    if review.get("outcome") != "pass":
        raise ValueError("issue_32_evidence_reviewer_pass_required")
    _required_text(review, "reviewer")
    _required_int(review, "reviewed_at_ms")


def _passed_gate(gates: dict[str, Any], name: str) -> dict[str, Any]:
    gate = _mapping(gates, name)
    if gate.get("status") != "passed":
        raise ValueError(f"issue_32_{name}_gate_not_passed")
    return gate


def _required_sections(payload: dict[str, Any], names: tuple[str, ...], *, gate: str) -> None:
    missing = [name for name in names if not isinstance(payload.get(name), dict) or not payload[name]]
    if missing:
        raise ValueError(f"issue_32_{gate}_sections_missing:{','.join(missing)}")


def _require_ok(payload: dict[str, Any], name: str, *, gate: str) -> None:
    section = _mapping(payload, name)
    if section.get("ok") is not True:
        raise ValueError(f"issue_32_{gate}_{name}_failed")


def _require_query_audit(payload: dict[str, Any], *, gate: str, analyze: bool) -> None:
    query_audit = _mapping(payload, "query_audit")
    _validate_query_audit(query_audit, analyze=analyze, gate=gate)


def _validate_query_audit(payload: dict[str, Any], *, analyze: bool, gate: str) -> None:
    if payload.get("ok") is not True:
        raise ValueError(f"issue_32_{gate}_query_audit_failed")
    if analyze and payload.get("analyze") is not True:
        raise ValueError(f"issue_32_{gate}_query_audit_analyze_required")
    coverage = payload.get("route_coverage")
    expected_query_routes = {route: list(query_names) for route, query_names in PUBLIC_ROUTE_QUERY_COVERAGE.items()}
    if (
        not isinstance(coverage, dict)
        or coverage.get("missing_query_names")
        or coverage.get("query_routes") != expected_query_routes
        or coverage.get("no_sql_routes") != sorted(PUBLIC_NO_SQL_ROUTES)
    ):
        raise ValueError(f"issue_32_{gate}_query_route_coverage_incomplete")
    queries = payload.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError(f"issue_32_{gate}_query_evidence_required")
    if any(not isinstance(query, dict) or query.get("ok") is not True for query in queries):
        raise ValueError(f"issue_32_{gate}_query_plan_failed")
    expected_query_names = {
        query_name for route_query_names in PUBLIC_ROUTE_QUERY_COVERAGE.values() for query_name in route_query_names
    }
    actual_query_names = {str(query["name"]) for query in queries if isinstance(query, dict) and query.get("name")}
    if not expected_query_names.issubset(actual_query_names):
        raise ValueError(f"issue_32_{gate}_query_plan_missing")
    if analyze and any(query.get("violations") for query in queries):
        raise ValueError(f"issue_32_{gate}_query_plan_violation")


def _validate_existing_seal(root: Path, seal: Any) -> None:
    if not isinstance(seal, dict) or seal.get("schema_version") != SEAL_SCHEMA_VERSION:
        raise ValueError("issue_32_existing_seal_invalid")
    current = [_file_evidence(root, path) for path in _bundle_files(root)]
    if seal.get("files") != current:
        raise ValueError("issue_32_sealed_bundle_modified")
    if seal.get("root_sha256") != _sha256_bytes(_canonical_json(current)):
        raise ValueError("issue_32_existing_seal_hash_invalid")


def _bundle_files(root: Path) -> list[Path]:
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.name != SEAL_FILE and not path.name.startswith(f".{SEAL_FILE}.")
    ]
    if any(path.is_symlink() for path in files):
        raise ValueError("issue_32_evidence_symlink_forbidden")
    total_bytes = sum(path.stat().st_size for path in files)
    if total_bytes > MAX_BUNDLE_BYTES:
        raise ValueError("issue_32_evidence_bundle_too_large")
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _file_evidence(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256_bytes(path.read_bytes()),
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"issue_32_evidence_json_invalid:{path.name}") from exc


def _mapping(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"issue_32_evidence_{name}_object_required")
    return value


def _required_text(payload: dict[str, Any], name: str) -> str:
    value = str(payload.get(name) or "").strip()
    if not value:
        raise ValueError(f"issue_32_evidence_{name}_required")
    return value


def _required_int(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool):
        raise ValueError(f"issue_32_evidence_{name}_integer_required")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"issue_32_evidence_{name}_integer_required") from exc
    if parsed < 0:
        raise ValueError(f"issue_32_evidence_{name}_integer_required")
    return parsed


def _string_list(payload: dict[str, Any], name: str) -> list[str]:
    value = payload.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"issue_32_evidence_{name}_list_required")
    return value


def _looks_sensitive(value: str) -> bool:
    normalized = str(value).lower()
    return any(token in normalized for token in ("password", "secret", "token", "credential", "dsn"))


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "EVIDENCE_FILE",
    "EVIDENCE_SCHEMA_VERSION",
    "MINIMUM_REAL_RUN_SECONDS",
    "SEAL_FILE",
    "SEAL_SCHEMA_VERSION",
    "issue_32_evidence_template",
    "seal_issue_32_evidence",
]
