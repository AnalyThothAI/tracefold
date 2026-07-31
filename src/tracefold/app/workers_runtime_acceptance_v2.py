from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from tracefold.platform.postgres.postgres_migrations import latest_migration_version

EVIDENCE_SCHEMA_VERSION = "workers_runtime_acceptance_v2"
SEAL_SCHEMA_VERSION = "workers_runtime_acceptance_v2_seal_v1"
RAW_EVIDENCE_SCHEMA_VERSION = "workers_runtime_raw_evidence_v1"
REVIEW_SCHEMA_VERSION = "workers_runtime_independent_review_v1"
EVIDENCE_FILE = "evidence.json"
SEAL_FILE = "seal.json"
MINIMUM_REAL_RUN_SECONDS = 30 * 60
MAX_BUNDLE_BYTES = 100 * 1024 * 1024

_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CAPACITY_DOMAINS = ("news", "macro", "profile", "radar")
_SEMANTIC_DOMAINS = ("radar", "news", "macro", "profile")
_MAX_RUNTIME_SAMPLE_GAP_MS = 15_000
_STARTUP_PROOFS = (
    "first_heartbeat_readiness",
    "fresh_row_collision",
    "singleton_exclusion",
    "forced_disconnect",
    "crash_restart",
    "native_model_recovery",
    "stale_claimant_rejection",
    "snapshot_restore",
)


def _proof_template() -> dict[str, Any]:
    return {
        "ok": False,
        "artifact_path": "",
        "artifact_sha256": "",
    }


def seal_workers_runtime_evidence(bundle_dir: Path) -> dict[str, Any]:
    root = bundle_dir.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("workers_runtime_evidence_bundle_directory_required")
    evidence_path = root / EVIDENCE_FILE
    if not evidence_path.is_file() or evidence_path.is_symlink():
        raise ValueError("workers_runtime_evidence_json_required")
    evidence = _read_json(evidence_path)
    _validate_evidence(root, evidence)

    existing_seal = root / SEAL_FILE
    if existing_seal.exists():
        seal = _read_json(existing_seal)
        _validate_existing_seal(root, seal)
        return seal

    manifest = [_file_evidence(root, path) for path in _bundle_files(root)]
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


def workers_runtime_evidence_template() -> dict[str, Any]:
    capacity = {
        domain: {
            "actionable_count_start": 0,
            "actionable_count_end": 0,
            "oldest_age_ms_start": 0,
            "oldest_age_ms_end": 0,
            "arrival_count": 0,
            "arrival_rate_per_minute": 0.0,
            "completion_count": 0,
            "completion_rate_per_minute": 0.0,
            "freshness_ok": False,
            "bounded_time_to_clear_ms": None,
        }
        for domain in _CAPACITY_DOMAINS
    }
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
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
            "redacted_enablement": {},
        },
        "gates": {
            "offline_semantic_determinism": {
                "status": "pending",
                "public_semantic_diff": _proof_template(),
                "zero_write_replay": _proof_template(),
                "migration_matrix": _proof_template(),
            },
            "startup_recovery": {
                "status": "pending",
                **{proof: _proof_template() for proof in _STARTUP_PROOFS},
            },
            "real_continuous_30m": {
                "status": "pending",
                "duration_seconds": 0,
                "deadline_misses": {
                    "unresolved_start": 0,
                    "counter_delta": 0,
                    "unresolved_end": 0,
                },
                "unresolved_projection_quarantine": 0,
                "capacity": capacity,
                "capacity_interval": _proof_template(),
                "runtime": _proof_template(),
                "process_resources": _proof_template(),
                "postgres": _proof_template(),
                "resource_admission_service": _proof_template(),
                "public_semantic_diff": _proof_template(),
            },
        },
        "review": {
            "disposition": "pending",
            "reviewer": "",
            "reviewed_at_ms": 0,
            "artifact_path": "",
            "artifact_sha256": "",
        },
    }


def _validate_evidence(root: Path, payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise ValueError("workers_runtime_evidence_schema_invalid")
    source = _mapping(payload, "source")
    if source.get("repository") != "AnalyThothAI/tracefold":
        raise ValueError("workers_runtime_evidence_repository_invalid")
    _required_text(source, "session")
    _required_int(source, "cutoff_at_ms")

    versions = _mapping(payload, "versions")
    commit_sha = _required_text(versions, "commit_sha")
    if not _COMMIT_PATTERN.fullmatch(commit_sha):
        raise ValueError("workers_runtime_evidence_commit_sha_invalid")
    if commit_sha != _repository_head():
        raise ValueError("workers_runtime_evidence_commit_not_current_checkout")
    if not _repository_is_clean():
        raise ValueError("workers_runtime_evidence_checkout_dirty")
    if versions.get("migration_version") != latest_migration_version():
        raise ValueError("workers_runtime_evidence_migration_version_invalid")

    configuration = _mapping(payload, "configuration")
    config_path = Path(_required_text(configuration, "config_path"))
    if not config_path.is_absolute():
        raise ValueError("workers_runtime_evidence_config_path_must_be_absolute")
    if not config_path.is_file():
        raise ValueError("workers_runtime_evidence_config_path_required")
    enablement = _mapping(configuration, "redacted_enablement")
    if not enablement or any(not isinstance(value, bool | type(None)) for value in enablement.values()):
        raise ValueError("workers_runtime_evidence_redacted_enablement_invalid")
    if any(_looks_sensitive(key) for key in enablement):
        raise ValueError("workers_runtime_evidence_sensitive_configuration_forbidden")

    referenced_artifacts: dict[str, str] = {}
    gates = _mapping(payload, "gates")
    offline = _passed_gate(gates, "offline_semantic_determinism")
    for name in ("public_semantic_diff", "zero_write_replay", "migration_matrix"):
        _require_proof(
            root,
            offline,
            name,
            gate="offline_semantic_determinism",
            evidence=payload,
            referenced_artifacts=referenced_artifacts,
        )

    startup = _passed_gate(gates, "startup_recovery")
    for name in _STARTUP_PROOFS:
        _require_proof(
            root,
            startup,
            name,
            gate="startup_recovery",
            evidence=payload,
            referenced_artifacts=referenced_artifacts,
        )
    restore = _mapping(startup, "snapshot_restore")
    if restore.get("executed") is not True or restore.get("verified") is not True:
        raise ValueError("workers_runtime_startup_recovery_snapshot_restore_failed")

    real = _passed_gate(gates, "real_continuous_30m")
    duration_seconds = _required_int(real, "duration_seconds")
    if duration_seconds < MINIMUM_REAL_RUN_SECONDS:
        raise ValueError("workers_runtime_real_run_too_short")
    misses = _mapping(real, "deadline_misses")
    if any(_required_int(misses, name) != 0 for name in ("unresolved_start", "counter_delta", "unresolved_end")):
        raise ValueError("workers_runtime_deadline_miss_gate_failed")
    if _required_int(real, "unresolved_projection_quarantine") != 0:
        raise ValueError("workers_runtime_projection_quarantine_gate_failed")
    capacity_raw = _require_proof(
        root,
        real,
        "capacity_interval",
        gate="real_continuous_30m",
        evidence=payload,
        referenced_artifacts=referenced_artifacts,
    )
    if _required_int(capacity_raw, "duration_seconds") != duration_seconds:
        raise ValueError("workers_runtime_capacity_duration_mismatch")
    raw_misses = _mapping(capacity_raw, "deadline_misses")
    if raw_misses != misses:
        raise ValueError("workers_runtime_deadline_miss_raw_mismatch")
    if _required_int(capacity_raw, "unresolved_projection_quarantine") != _required_int(
        real, "unresolved_projection_quarantine"
    ):
        raise ValueError("workers_runtime_projection_quarantine_raw_mismatch")
    raw_capacity = _mapping(capacity_raw, "capacity")
    if raw_capacity != _mapping(real, "capacity"):
        raise ValueError("workers_runtime_capacity_raw_mismatch")
    _validate_capacity(raw_capacity, duration_seconds=duration_seconds)
    for name in (
        "runtime",
        "process_resources",
        "postgres",
        "resource_admission_service",
        "public_semantic_diff",
    ):
        raw = _require_proof(
            root,
            real,
            name,
            gate="real_continuous_30m",
            evidence=payload,
            referenced_artifacts=referenced_artifacts,
        )
        if name == "runtime":
            _validate_runtime_samples(raw, duration_seconds=duration_seconds)
    runtime = _mapping(real, "runtime")
    if (
        runtime.get("continuous_readiness") is not True
        or runtime.get("continuous_heartbeat") is not True
        or _required_int(runtime, "restart_count") != 0
    ):
        raise ValueError("workers_runtime_continuity_gate_failed")

    review = _mapping(payload, "review")
    if review.get("disposition") != "pass":
        raise ValueError("workers_runtime_reviewer_pass_required")
    _required_text(review, "reviewer")
    _required_int(review, "reviewed_at_ms")
    review_artifact, review_path, _ = _validate_artifact(root, review, label="review")
    _validate_review_artifact(
        review_artifact,
        evidence=payload,
        review=review,
        referenced_artifacts=referenced_artifacts,
        review_path=review_path,
    )


def _validate_capacity(capacity: dict[str, Any], *, duration_seconds: int) -> None:
    if set(capacity) != set(_CAPACITY_DOMAINS):
        raise ValueError("workers_runtime_capacity_domains_invalid")
    for domain in _CAPACITY_DOMAINS:
        row = _mapping(capacity, domain)
        start = _required_int(row, "actionable_count_start")
        end = _required_int(row, "actionable_count_end")
        oldest_start = _required_int(row, "oldest_age_ms_start")
        oldest_end = _required_int(row, "oldest_age_ms_end")
        arrivals = _required_int(row, "arrival_count")
        completions = _required_int(row, "completion_count")
        reported_arrival_rate = _required_float(row, "arrival_rate_per_minute")
        reported_completion_rate = _required_float(row, "completion_rate_per_minute")
        minutes = duration_seconds / 60.0
        arrival_rate = arrivals / minutes
        completion_rate = completions / minutes
        if not _same_rate(reported_arrival_rate, arrival_rate):
            raise ValueError(f"workers_runtime_capacity_arrival_rate_invalid:{domain}")
        if not _same_rate(reported_completion_rate, completion_rate):
            raise ValueError(f"workers_runtime_capacity_completion_rate_invalid:{domain}")
        if start == 0 and end == 0:
            continue
        converging = completion_rate > arrival_rate and oldest_end < oldest_start
        cleared = end == 0
        freshness = row.get("freshness_ok") is True
        bounded_clear = row.get("bounded_time_to_clear_ms")
        bounded = bounded_clear is not None and _nonnegative_int(bounded_clear) > 0
        if not (converging and (cleared or freshness or bounded)):
            raise ValueError(f"workers_runtime_capacity_not_converging:{domain}")


def _passed_gate(gates: dict[str, Any], name: str) -> dict[str, Any]:
    gate = _mapping(gates, name)
    if gate.get("status") != "passed":
        raise ValueError(f"workers_runtime_{name}_gate_not_passed")
    return gate


def _require_proof(
    root: Path,
    payload: dict[str, Any],
    name: str,
    *,
    gate: str,
    evidence: dict[str, Any],
    referenced_artifacts: dict[str, str],
) -> dict[str, Any]:
    proof = _mapping(payload, name)
    if proof.get("ok") is not True:
        raise ValueError(f"workers_runtime_{gate}_{name}_failed")
    label = f"{gate}_{name}"
    artifact, relative, artifact_hash = _validate_artifact(root, proof, label=label)
    referenced_artifacts[relative] = artifact_hash
    return _validate_raw_proof(artifact, label=label, evidence=evidence)


def _validate_artifact(
    root: Path,
    payload: dict[str, Any],
    *,
    label: str,
) -> tuple[Any, str, str]:
    relative = _required_text(payload, "artifact_path")
    candidate = Path(relative)
    if candidate.is_absolute() or candidate.as_posix() != relative or ".." in candidate.parts:
        raise ValueError(f"workers_runtime_{label}_artifact_path_invalid")
    if relative in {EVIDENCE_FILE, SEAL_FILE}:
        raise ValueError(f"workers_runtime_{label}_artifact_path_invalid")
    path = root / candidate
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"workers_runtime_{label}_artifact_required")
    expected = _required_text(payload, "artifact_sha256")
    if not _SHA256_PATTERN.fullmatch(expected):
        raise ValueError(f"workers_runtime_{label}_artifact_hash_invalid")
    if _sha256_bytes(path.read_bytes()) != expected:
        raise ValueError(f"workers_runtime_{label}_artifact_hash_mismatch")
    return _read_json(path), relative, expected


def _validate_raw_proof(
    artifact: Any,
    *,
    label: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(artifact, dict) or artifact.get("schema_version") != RAW_EVIDENCE_SCHEMA_VERSION:
        raise ValueError(f"workers_runtime_{label}_raw_schema_invalid")
    for section in ("source", "versions", "configuration"):
        if artifact.get(section) != evidence.get(section):
            raise ValueError(f"workers_runtime_{label}_{section}_mismatch")
    proofs = _mapping(artifact, "proofs")
    raw = _mapping(proofs, label)
    if raw.get("status") != "passed":
        raise ValueError(f"workers_runtime_{label}_raw_not_passed")
    if label.endswith("public_semantic_diff"):
        _validate_semantic_diff(raw, label=label)
    elif label.endswith("zero_write_replay"):
        if _required_int(raw, "tests_passed") <= 0 or _required_int(raw, "serving_rows_written") != 0:
            raise ValueError(f"workers_runtime_{label}_raw_invalid")
    elif label.endswith("migration_matrix"):
        states = _mapping(raw, "outer_states")
        if set(states) != {"clean", "dirty", "running", "retry_wait", "quarantined"} or not all(
            value is True for value in states.values()
        ):
            raise ValueError("workers_runtime_migration_matrix_states_invalid")
        if raw.get("native_retry_verified") is not True or raw.get("unknown_owner_abort_verified") is not True:
            raise ValueError("workers_runtime_migration_matrix_closure_invalid")
    elif label.endswith("snapshot_restore"):
        if (
            raw.get("executed") is not True
            or raw.get("verified") is not True
            or _required_int(raw, "restore_exit_code") != 0
            or not _SHA256_PATTERN.fullmatch(_required_text(raw, "backup_sha256"))
            or _required_int(raw, "restored_table_count") <= 0
        ):
            raise ValueError("workers_runtime_snapshot_restore_raw_invalid")
    elif label.endswith("capacity_interval"):
        _mapping(raw, "deadline_misses")
        _mapping(raw, "capacity")
    elif label.endswith("runtime"):
        _required_list(raw, "samples")
    else:
        checks = _mapping(raw, "checks")
        if not checks or not all(value is True for value in checks.values()):
            raise ValueError(f"workers_runtime_{label}_raw_checks_invalid")
    return raw


def _validate_semantic_diff(raw: dict[str, Any], *, label: str) -> None:
    domains = _mapping(raw, "domains")
    if set(domains) != set(_SEMANTIC_DOMAINS):
        raise ValueError(f"workers_runtime_{label}_domains_invalid")
    for domain in _SEMANTIC_DOMAINS:
        row = _mapping(domains, domain)
        if row.get("match") is not True:
            raise ValueError(f"workers_runtime_{label}_{domain}_mismatch")
        for key in ("baseline_sha256", "candidate_sha256"):
            if not _SHA256_PATTERN.fullmatch(_required_text(row, key)):
                raise ValueError(f"workers_runtime_{label}_{domain}_hash_invalid")
        if row["baseline_sha256"] != row["candidate_sha256"]:
            raise ValueError(f"workers_runtime_{label}_{domain}_hash_mismatch")


def _validate_runtime_samples(raw: dict[str, Any], *, duration_seconds: int) -> None:
    start_at_ms = _required_int(raw, "start_at_ms")
    end_at_ms = _required_int(raw, "end_at_ms")
    if end_at_ms - start_at_ms < duration_seconds * 1_000:
        raise ValueError("workers_runtime_runtime_sample_duration_invalid")
    samples = _required_list(raw, "samples")
    if len(samples) < 2:
        raise ValueError("workers_runtime_runtime_samples_insufficient")
    runtime_ids: set[str] = set()
    process_ids: set[int] = set()
    prior_at: int | None = None
    prior_heartbeat: int | None = None
    for value in samples:
        if not isinstance(value, dict):
            raise ValueError("workers_runtime_runtime_sample_invalid")
        at_ms = _required_int(value, "at_ms")
        heartbeat_at_ms = _required_int(value, "heartbeat_at_ms")
        if value.get("state") != "running" or value.get("ready") is not True:
            raise ValueError("workers_runtime_runtime_continuity_raw_failed")
        if not 0 <= at_ms - heartbeat_at_ms <= _MAX_RUNTIME_SAMPLE_GAP_MS:
            raise ValueError("workers_runtime_runtime_heartbeat_stale")
        if prior_at is not None and not 0 < at_ms - prior_at <= _MAX_RUNTIME_SAMPLE_GAP_MS:
            raise ValueError("workers_runtime_runtime_sample_gap_invalid")
        if prior_heartbeat is not None and heartbeat_at_ms < prior_heartbeat:
            raise ValueError("workers_runtime_runtime_heartbeat_regressed")
        runtime_ids.add(_required_text(value, "runtime_id"))
        process_ids.add(_required_int(value, "process_id"))
        prior_at = at_ms
        prior_heartbeat = heartbeat_at_ms
    if _required_int(samples[0], "at_ms") != start_at_ms or _required_int(samples[-1], "at_ms") != end_at_ms:
        raise ValueError("workers_runtime_runtime_sample_boundary_invalid")
    if len(runtime_ids) != 1 or len(process_ids) != 1:
        raise ValueError("workers_runtime_runtime_restart_detected")


def _validate_review_artifact(
    artifact: Any,
    *,
    evidence: dict[str, Any],
    review: dict[str, Any],
    referenced_artifacts: dict[str, str],
    review_path: str,
) -> None:
    if not isinstance(artifact, dict) or artifact.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise ValueError("workers_runtime_review_schema_invalid")
    for section in ("source", "versions", "configuration"):
        if artifact.get(section) != evidence.get(section):
            raise ValueError(f"workers_runtime_review_{section}_mismatch")
    if (
        artifact.get("disposition") != "pass"
        or artifact.get("reviewer") != review.get("reviewer")
        or artifact.get("reviewed_at_ms") != review.get("reviewed_at_ms")
    ):
        raise ValueError("workers_runtime_review_identity_mismatch")
    reviewed = _mapping(artifact, "reviewed_artifacts")
    if reviewed != referenced_artifacts or review_path in reviewed:
        raise ValueError("workers_runtime_review_artifact_set_mismatch")


def _validate_existing_seal(root: Path, seal: Any) -> None:
    if not isinstance(seal, dict) or seal.get("schema_version") != SEAL_SCHEMA_VERSION:
        raise ValueError("workers_runtime_existing_seal_invalid")
    current = [_file_evidence(root, path) for path in _bundle_files(root)]
    if seal.get("files") != current:
        raise ValueError("workers_runtime_sealed_bundle_modified")
    if seal.get("root_sha256") != _sha256_bytes(_canonical_json(current)):
        raise ValueError("workers_runtime_existing_seal_hash_invalid")


def _bundle_files(root: Path) -> list[Path]:
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.name != SEAL_FILE and not path.name.startswith(f".{SEAL_FILE}.")
    ]
    if any(path.is_symlink() for path in files):
        raise ValueError("workers_runtime_evidence_symlink_forbidden")
    if sum(path.stat().st_size for path in files) > MAX_BUNDLE_BYTES:
        raise ValueError("workers_runtime_evidence_bundle_too_large")
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
        raise ValueError(f"workers_runtime_evidence_json_invalid:{path.name}") from exc


def _mapping(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"workers_runtime_evidence_{name}_object_required")
    return value


def _required_list(payload: dict[str, Any], name: str) -> list[Any]:
    value = payload.get(name)
    if not isinstance(value, list):
        raise ValueError(f"workers_runtime_evidence_{name}_array_required")
    return value


def _required_text(payload: dict[str, Any], name: str) -> str:
    value = str(payload.get(name) or "").strip()
    if not value:
        raise ValueError(f"workers_runtime_evidence_{name}_required")
    return value


def _required_int(payload: dict[str, Any], name: str) -> int:
    if name not in payload:
        raise ValueError(f"workers_runtime_evidence_{name}_integer_required")
    return _nonnegative_int(payload[name], name=name)


def _nonnegative_int(value: Any, *, name: str = "value") -> int:
    if isinstance(value, bool):
        raise ValueError(f"workers_runtime_evidence_{name}_integer_required")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"workers_runtime_evidence_{name}_integer_required") from exc
    if parsed < 0:
        raise ValueError(f"workers_runtime_evidence_{name}_integer_required")
    return parsed


def _required_float(payload: dict[str, Any], name: str) -> float:
    value = payload.get(name)
    if isinstance(value, bool):
        raise ValueError(f"workers_runtime_evidence_{name}_number_required")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"workers_runtime_evidence_{name}_number_required") from exc
    if parsed < 0:
        raise ValueError(f"workers_runtime_evidence_{name}_number_required")
    return parsed


def _same_rate(left: float, right: float) -> bool:
    return abs(left - right) <= max(1e-9, abs(right) * 1e-9)


def _looks_sensitive(value: str) -> bool:
    normalized = str(value).lower()
    return any(token in normalized for token in ("password", "secret", "token", "credential", "dsn"))


def _repository_head() -> str:
    repository_root = Path(__file__).resolve().parents[3]
    try:
        dot_git = repository_root / ".git"
        if dot_git.is_file():
            marker = dot_git.read_text().strip()
            if not marker.startswith("gitdir: "):
                raise ValueError("gitdir_marker_invalid")
            git_dir = (repository_root / marker.removeprefix("gitdir: ")).resolve()
        else:
            git_dir = dot_git
        head = (git_dir / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            value = head
        else:
            ref = head.removeprefix("ref: ")
            common_dir = git_dir
            common_marker = git_dir / "commondir"
            if common_marker.is_file():
                common_dir = (git_dir / common_marker.read_text().strip()).resolve()
            loose_ref = common_dir / ref
            if loose_ref.is_file():
                value = loose_ref.read_text().strip()
            else:
                packed = (common_dir / "packed-refs").read_text().splitlines()
                value = next(
                    line.split(" ", 1)[0]
                    for line in packed
                    if not line.startswith(("#", "^")) and line.endswith(f" {ref}")
                )
    except (OSError, StopIteration, ValueError) as exc:
        raise ValueError("workers_runtime_evidence_repository_head_unavailable") from exc
    if not _COMMIT_PATTERN.fullmatch(value):
        raise ValueError("workers_runtime_evidence_repository_head_invalid")
    return value


def _repository_is_clean() -> bool:
    repository_root = Path(__file__).resolve().parents[3]
    git_executable = shutil.which("git")
    if git_executable is None:
        raise ValueError("workers_runtime_evidence_repository_status_unavailable")
    try:
        result = subprocess.run(  # noqa: S603 - executable is resolved locally and arguments are fixed
            [git_executable, "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("workers_runtime_evidence_repository_status_unavailable") from exc
    return not result.stdout.strip()


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "EVIDENCE_FILE",
    "EVIDENCE_SCHEMA_VERSION",
    "MINIMUM_REAL_RUN_SECONDS",
    "RAW_EVIDENCE_SCHEMA_VERSION",
    "REVIEW_SCHEMA_VERSION",
    "SEAL_FILE",
    "SEAL_SCHEMA_VERSION",
    "seal_workers_runtime_evidence",
    "workers_runtime_evidence_template",
]
