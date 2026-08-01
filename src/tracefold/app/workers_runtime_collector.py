from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from prometheus_client.parser import text_string_to_metric_families
from psycopg import conninfo

from tracefold.platform.postgres.postgres_client import (
    connect_postgres,
    with_password_from_file,
)
from tracefold.platform.postgres.postgres_migrations import latest_migration_version

COLLECTION_DURATION_SECONDS = 30 * 60
SAMPLE_INTERVAL_SECONDS = 10
MAX_SAMPLE_GAP_SECONDS = 15
EXPECTED_SAMPLE_COUNT = COLLECTION_DURATION_SECONDS // SAMPLE_INTERVAL_SECONDS + 1

COLLECTION_SCHEMA_VERSION = "workers_runtime_acceptance_collection_v1"
SAMPLES_FILE = "workers-runtime-samples.jsonl"
COLLECTION_FILE = "workers-runtime-collection.json"

_PROBE_URL = "http://127.0.0.1:8766/readyz"
_METRICS_URL = "http://127.0.0.1:8766/metrics"
_HTTP_TIMEOUT_SECONDS = 1.0
_MAX_PROBE_BYTES = 64 * 1024
_MAX_METRICS_BYTES = 4 * 1024 * 1024
_MAX_RSS_BYTES = 2 * 1024 * 1024 * 1024
_MAX_TRANSACTION_SECONDS = 2.0
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IPV4_ANY = ".".join(("0", "0", "0", "0"))
_ACTIONABLE_STATUSES = ("dirty", "running", "retry_wait")
_RESOURCE_CAPS = {
    "database_business": 2.0,
    "database_control": 1.0,
    "finite_operation": 3.0,
    "model_adapter": 1.0,
    "cpu_process": 1.0,
}
_DOMAINS = ("news", "macro", "profile", "radar")
_FRONTIER_TABLES = {
    "radar": "radar_projection_frontiers",
    "profile": "token_profile_projection_frontiers",
    "macro": "macro_module_frontiers",
    "news": "news_projection_frontiers",
}


@dataclass(frozen=True, slots=True)
class CollectorDependencies:
    clock_ms: Callable[[], int]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    read_sample: Callable[[int], dict[str, Any]]


class _SampleFailure(RuntimeError):
    def __init__(self, stage: str, cause: BaseException | None = None) -> None:
        super().__init__(stage)
        self.stage = str(stage)
        self.cause_type = type(cause).__name__ if cause is not None else None


def collect_workers_runtime_acceptance(bundle_dir: Path, settings: Any) -> dict[str, Any]:
    repository_root = Path(__file__).resolve().parents[3]
    _require_clean_checkout(repository_root)
    bundle = _prepare_bundle(bundle_dir, repository_root=repository_root)
    metadata = _collection_metadata(settings, repository_root=repository_root)
    sampler: _ProductionSampler | None = None
    try:
        sampler = _ProductionSampler(settings, repository_root=repository_root)
        return _collect_fixed_interval(
            bundle,
            metadata=metadata,
            dependencies=CollectorDependencies(
                clock_ms=lambda: int(time.time() * 1_000),
                monotonic=time.monotonic,
                sleep=time.sleep,
                read_sample=sampler.read_sample,
            ),
        )
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            failure = _failure("collector_interrupted", exc)
        elif isinstance(exc, _SampleFailure):
            failure = _failure(exc.stage, exc)
        else:
            failure = _failure("collector_initialization", exc)
        return _write_initialization_failure(bundle, metadata=metadata, failure=failure)
    finally:
        if sampler is not None:
            sampler.close()


def _collect_fixed_interval(
    bundle: Path,
    *,
    metadata: dict[str, Any],
    dependencies: CollectorDependencies,
) -> dict[str, Any]:
    samples_path = bundle / SAMPLES_FILE
    start_monotonic = float(dependencies.monotonic())
    start_at_ms: int | None = None
    samples: list[dict[str, Any]] = []
    failure: dict[str, Any] | None = None

    with samples_path.open("x", encoding="utf-8") as stream:
        for sequence in range(EXPECTED_SAMPLE_COUNT):
            scheduled = start_monotonic + sequence * SAMPLE_INTERVAL_SECONDS
            remaining = scheduled - float(dependencies.monotonic())
            if remaining > 0:
                dependencies.sleep(remaining)
            try:
                sample = dependencies.read_sample(sequence)
                if sequence == 0:
                    start_monotonic = float(dependencies.monotonic())
                    collector_elapsed_seconds = 0.0
                else:
                    collector_elapsed_seconds = float(dependencies.monotonic()) - start_monotonic
                sample["collector_elapsed_seconds"] = collector_elapsed_seconds
                _validate_sample(sample, metadata=metadata, previous=samples[-1] if samples else None)
                if start_at_ms is None:
                    start_at_ms = int(sample["at_ms"])
                samples.append(sample)
                stream.write(_json_line(sample))
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException as exc:
                failed_sample = {
                    "schema_version": COLLECTION_SCHEMA_VERSION,
                    "sequence": sequence,
                    "scheduled_offset_seconds": sequence * SAMPLE_INTERVAL_SECONDS,
                    "at_ms": int(dependencies.clock_ms()),
                    "status": "failed",
                    "failure": _failure(
                        exc.stage if isinstance(exc, _SampleFailure) else "sample_collection",
                        exc,
                    ),
                }
                stream.write(_json_line(failed_sample))
                stream.flush()
                os.fsync(stream.fileno())
                failure = _mapping(failed_sample, "failure", stage="sample_failure")
                break

    if failure is not None:
        result = {
            **metadata,
            "schema_version": COLLECTION_SCHEMA_VERSION,
            "status": "failed",
            "sample_policy": _sample_policy(),
            "sample_count": len(samples),
            "start_at_ms": start_at_ms,
            "end_at_ms": int(dependencies.clock_ms()),
            "failure": failure,
            "samples_path": SAMPLES_FILE,
            "samples_sha256": _sha256_file(samples_path),
        }
    else:
        result = {
            **metadata,
            "schema_version": COLLECTION_SCHEMA_VERSION,
            "status": "passed",
            "sample_policy": _sample_policy(),
            "sample_count": len(samples),
            "start_at_ms": int(samples[0]["at_ms"]),
            "end_at_ms": int(samples[-1]["at_ms"]),
            "samples_path": SAMPLES_FILE,
            "samples_sha256": _sha256_file(samples_path),
            "summary": _summarize(samples),
        }
        if not bool(result["summary"]["all_checks_passed"]):
            result["status"] = "failed"
            result["failure"] = {
                "stage": "acceptance_checks",
                "cause_type": None,
                "failed_checks": list(result["summary"]["failed_checks"]),
            }
    _write_json_exclusive(bundle / COLLECTION_FILE, result)
    return result


def validate_workers_runtime_collection(
    bundle_dir: Path,
    *,
    expected_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Revalidate one production collection from its raw JSONL samples."""
    root = Path(bundle_dir).expanduser().resolve()
    collection_path = root / COLLECTION_FILE
    samples_path = root / SAMPLES_FILE
    if not collection_path.is_file() or collection_path.is_symlink():
        raise ValueError("workers_runtime_collection_json_required")
    if not samples_path.is_file() or samples_path.is_symlink():
        raise ValueError("workers_runtime_collection_samples_required")
    try:
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("workers_runtime_collection_json_invalid") from exc
    if not isinstance(collection, dict) or collection.get("schema_version") != COLLECTION_SCHEMA_VERSION:
        raise ValueError("workers_runtime_collection_schema_invalid")
    if collection.get("status") != "passed":
        raise ValueError("workers_runtime_collection_not_passed")
    metadata = {section: collection.get(section) for section in ("source", "versions", "configuration")}
    if expected_metadata is not None:
        for section, value in metadata.items():
            if value != expected_metadata.get(section):
                raise ValueError(f"workers_runtime_collection_{section}_mismatch")
    if collection.get("sample_policy") != _sample_policy():
        raise ValueError("workers_runtime_collection_sample_policy_invalid")
    if collection.get("samples_path") != SAMPLES_FILE:
        raise ValueError("workers_runtime_collection_samples_path_invalid")
    claimed_samples_hash = str(collection.get("samples_sha256") or "")
    if not _SHA256_PATTERN.fullmatch(claimed_samples_hash):
        raise ValueError("workers_runtime_collection_samples_hash_invalid")
    if _sha256_file(samples_path) != claimed_samples_hash:
        raise ValueError("workers_runtime_collection_samples_hash_mismatch")

    samples: list[dict[str, Any]] = []
    try:
        with samples_path.open(encoding="utf-8") as stream:
            for sequence, line in enumerate(stream):
                if not line.strip():
                    raise ValueError("workers_runtime_collection_sample_json_invalid")
                decoded = json.loads(line)
                if not isinstance(decoded, dict):
                    raise ValueError("workers_runtime_collection_sample_json_invalid")
                _validate_sample(
                    decoded,
                    metadata=metadata,
                    previous=samples[-1] if samples else None,
                )
                if decoded.get("sequence") != sequence:
                    raise _SampleFailure("sample_sequence")
                if decoded.get("scheduled_offset_seconds") != sequence * SAMPLE_INTERVAL_SECONDS:
                    raise _SampleFailure("sample_schedule")
                samples.append(decoded)
    except _SampleFailure as exc:
        raise ValueError(f"workers_runtime_collection_sample_invalid:{exc.stage}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("workers_runtime_collection_sample_json_invalid") from exc
    if len(samples) != EXPECTED_SAMPLE_COUNT:
        raise ValueError("workers_runtime_collection_sample_count_invalid")

    try:
        summary = _summarize(samples)
    except _SampleFailure as exc:
        raise ValueError(f"workers_runtime_collection_summary_invalid:{exc.stage}") from exc
    if collection.get("sample_count") != len(samples):
        raise ValueError("workers_runtime_collection_sample_count_mismatch")
    if collection.get("start_at_ms") != samples[0]["at_ms"]:
        raise ValueError("workers_runtime_collection_start_mismatch")
    if collection.get("end_at_ms") != samples[-1]["at_ms"]:
        raise ValueError("workers_runtime_collection_end_mismatch")
    if collection.get("summary") != summary:
        raise ValueError("workers_runtime_collection_summary_mismatch")
    if summary.get("all_checks_passed") is not True:
        raise ValueError("workers_runtime_collection_checks_failed")
    return summary


class _ProductionSampler:
    def __init__(self, settings: Any, *, repository_root: Path) -> None:
        self._settings = settings
        self._repository_root = repository_root
        self._docker = shutil.which("docker")
        if self._docker is None:
            raise _SampleFailure("docker_cli_unavailable")
        postgres = settings.storage.postgres
        dsn = with_password_from_file(
            settings.postgres_dsn("workers"),
            settings.postgres_password_file("workers"),
        )
        try:
            compose_endpoint = self._run_text(
                [
                    self._docker,
                    "compose",
                    "--project-name",
                    "tracefold",
                    "port",
                    "postgres",
                    "5432",
                ],
                stage="postgres_compose_endpoint",
            ).strip()
            self._conn = connect_postgres(
                _dsn_for_compose_endpoint(dsn, compose_endpoint),
                connect_timeout_seconds=postgres.connect_timeout_seconds,
            )
            self._conn.autocommit = True
            self._conn.execute("SET application_name = 'tracefold_acceptance_collector'")
            self._conn.execute("SET default_transaction_read_only = on")
            self._conn.execute("SET statement_timeout = '1000ms'")
        except BaseException as exc:
            raise _SampleFailure("postgres_collector_connection", exc) from exc

    def close(self) -> None:
        self._conn.close()

    def read_sample(self, sequence: int) -> dict[str, Any]:
        probe, probe_rtt_ms = self._read_probe()
        metrics_text = self._read_http_text(
            _METRICS_URL,
            max_bytes=_MAX_METRICS_BYTES,
            stage="worker_metrics",
        )
        container = self._read_container(probe_process_id=int(probe.get("process_id") or 0))
        at_ms = int(time.time() * 1_000)
        postgres = self._read_postgres(now_ms=at_ms)
        return {
            "schema_version": COLLECTION_SCHEMA_VERSION,
            "sequence": int(sequence),
            "scheduled_offset_seconds": int(sequence) * SAMPLE_INTERVAL_SECONDS,
            "at_ms": at_ms,
            "status": "passed",
            "checkout": {
                "commit_sha": _git_head(self._repository_root),
                "clean": _repository_is_clean(self._repository_root),
            },
            "probe": {
                **probe,
                "ready": probe.get("ok") is True,
                "probe_rtt_ms": probe_rtt_ms,
            },
            "container": container,
            "postgres": postgres,
            "telemetry": {
                **_parse_worker_metrics(metrics_text),
            },
        }

    def _read_probe(self) -> tuple[dict[str, Any], float]:
        started = time.monotonic()
        payload = self._read_http_text(
            _PROBE_URL,
            max_bytes=_MAX_PROBE_BYTES,
            stage="worker_readiness",
        )
        elapsed_ms = (time.monotonic() - started) * 1_000
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise _SampleFailure("worker_readiness_json", exc) from exc
        if not isinstance(decoded, dict):
            raise _SampleFailure("worker_readiness_payload")
        return decoded, elapsed_ms

    @staticmethod
    def _read_http_text(url: str, *, max_bytes: int, stage: str) -> str:
        try:
            with urlopen(url, timeout=_HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310 -- fixed loopback URLs only
                payload = response.read(max_bytes + 1)
        except BaseException as exc:
            raise _SampleFailure(stage, exc) from exc
        if len(payload) > max_bytes:
            raise _SampleFailure(f"{stage}_oversized")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _SampleFailure(f"{stage}_encoding", exc) from exc

    def _read_container(self, *, probe_process_id: int) -> dict[str, Any]:
        container_id = self._run_text(
            [
                self._docker,
                "compose",
                "--project-name",
                "tracefold",
                "ps",
                "-q",
                "workers",
            ],
            stage="workers_container_lookup",
        ).strip()
        if not container_id or "\n" in container_id:
            raise _SampleFailure("workers_container_identity")
        inspect = self._run_json(
            [self._docker, "inspect", container_id],
            stage="workers_container_inspect",
        )
        if not isinstance(inspect, list) or len(inspect) != 1 or not isinstance(inspect[0], dict):
            raise _SampleFailure("workers_container_inspect_payload")
        row = inspect[0]
        state = _mapping(row, "State", stage="workers_container_state")
        config = _mapping(row, "Config", stage="workers_container_config")
        labels = config.get("Labels")
        if not isinstance(labels, dict):
            labels = {}
        memory_payload = self._run_json(
            [
                self._docker,
                "exec",
                container_id,
                "python",
                "-c",
                (
                    "import json,pathlib,sys;"
                    "p=pathlib.Path('/proc')/sys.argv[1]/'status';"
                    "rss=int(next(x.split()[1] for x in p.read_text().splitlines() if x.startswith('VmRSS:')))*1024;"
                    "paths=(pathlib.Path('/sys/fs/cgroup/memory.current'),"
                    "pathlib.Path('/sys/fs/cgroup/memory/memory.usage_in_bytes'));"
                    "memory=int(next(x.read_text().strip() for x in paths if x.is_file()));"
                    "print(json.dumps({'process_rss_bytes':rss,'container_memory_bytes':memory}))"
                ),
                str(probe_process_id),
            ],
            stage="workers_memory",
        )
        if not isinstance(memory_payload, dict):
            raise _SampleFailure("workers_memory_payload")
        return {
            "container_id": str(row.get("Id") or ""),
            "image_id": str(row.get("Image") or ""),
            "image_revision": str(labels.get("org.opencontainers.image.revision") or ""),
            "restart_count": _nonnegative_int(row.get("RestartCount"), stage="workers_restart_count"),
            "running": bool(state.get("Running")),
            "oom_killed": bool(state.get("OOMKilled")),
            "host_process_id": _positive_int(state.get("Pid"), stage="workers_host_process_id"),
            "process_rss_bytes": _nonnegative_int(
                memory_payload.get("process_rss_bytes"),
                stage="workers_process_rss",
            ),
            "container_memory_bytes": _nonnegative_int(
                memory_payload.get("container_memory_bytes"),
                stage="workers_container_memory",
            ),
        }

    def _read_postgres(self, *, now_ms: int) -> dict[str, Any]:
        try:
            activity = self._conn.execute(
                """
                SELECT
                  COUNT(*) AS worker_connections,
                  COUNT(*) FILTER (WHERE wait_event_type = 'Lock') AS lock_wait_count,
                  COALESCE(
                    MAX(EXTRACT(EPOCH FROM (clock_timestamp() - xact_start)))
                      FILTER (WHERE xact_start IS NOT NULL),
                    0
                  ) AS max_transaction_seconds
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND application_name LIKE 'tracefold_workers%'
                """
            ).fetchone()
            database = self._conn.execute(
                """
                SELECT temp_files, temp_bytes
                FROM pg_stat_database
                WHERE datname = current_database()
                """
            ).fetchone()
            frontier_rows = self._conn.execute(_frontier_snapshot_sql(), {"now_ms": int(now_ms)}).fetchall()
        except BaseException as exc:
            raise _SampleFailure("postgres_sample", exc) from exc
        if activity is None or database is None:
            raise _SampleFailure("postgres_sample_payload")
        frontiers = {
            domain: {
                "actionable_count": 0,
                "oldest_age_ms": 0,
                "unresolved_deadline_misses": 0,
                "unresolved_quarantine": 0,
                "counts_by_status": {},
            }
            for domain in _DOMAINS
        }
        for raw in frontier_rows:
            row = dict(raw)
            domain = str(row["domain"])
            frontiers[domain] = {
                "actionable_count": int(row["actionable_count"]),
                "oldest_age_ms": int(row["oldest_age_ms"]),
                "unresolved_deadline_misses": int(row["unresolved_deadline_misses"]),
                "unresolved_quarantine": int(row["unresolved_quarantine"]),
                "counts_by_status": dict(row["counts_by_status"] or {}),
            }
        return {
            "worker_connections": int(activity["worker_connections"]),
            "lock_wait_count": int(activity["lock_wait_count"]),
            "max_transaction_seconds": float(activity["max_transaction_seconds"]),
            "temp_files": int(database["temp_files"]),
            "temp_bytes": int(database["temp_bytes"]),
            "frontiers": frontiers,
        }

    def _run_text(self, arguments: list[str | None], *, stage: str) -> str:
        if any(argument is None for argument in arguments):
            raise _SampleFailure(stage)
        try:
            result = subprocess.run(  # noqa: S603 -- resolved Docker executable and fixed arguments
                [str(argument) for argument in arguments],
                cwd=self._repository_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=3.0,
            )
        except BaseException as exc:
            raise _SampleFailure(stage, exc) from exc
        return result.stdout

    def _run_json(self, arguments: list[str | None], *, stage: str) -> Any:
        payload = self._run_text(arguments, stage=stage)
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise _SampleFailure(f"{stage}_json", exc) from exc


def _validate_sample(
    sample: dict[str, Any],
    *,
    metadata: dict[str, Any],
    previous: dict[str, Any] | None,
) -> None:
    if sample.get("schema_version") != COLLECTION_SCHEMA_VERSION or sample.get("status") != "passed":
        raise _SampleFailure("sample_schema")
    sequence = _nonnegative_int(sample.get("sequence"), stage="sample_sequence")
    expected_sequence = 0 if previous is None else int(previous["sequence"]) + 1
    if sequence != expected_sequence:
        raise _SampleFailure("sample_sequence")
    if _nonnegative_int(sample.get("scheduled_offset_seconds"), stage="sample_schedule") != (
        sequence * SAMPLE_INTERVAL_SECONDS
    ):
        raise _SampleFailure("sample_schedule")
    checkout = _mapping(sample, "checkout", stage="checkout_state")
    if checkout.get("clean") is not True or checkout.get("commit_sha") != metadata["versions"]["commit_sha"]:
        raise _SampleFailure("checkout_changed_during_collection")
    at_ms = _nonnegative_int(sample.get("at_ms"), stage="sample_time")
    elapsed_seconds = _nonnegative_float(
        sample.get("collector_elapsed_seconds"),
        stage="collector_elapsed",
    )
    if previous is None:
        if elapsed_seconds != 0:
            raise _SampleFailure("collector_elapsed_start")
    else:
        prior_elapsed = _nonnegative_float(
            previous.get("collector_elapsed_seconds"),
            stage="previous_collector_elapsed",
        )
        elapsed_gap = elapsed_seconds - prior_elapsed
        if not 0 < elapsed_gap <= MAX_SAMPLE_GAP_SECONDS:
            raise _SampleFailure("collector_elapsed_gap")
    if previous is not None:
        gap_ms = at_ms - _nonnegative_int(previous.get("at_ms"), stage="previous_sample_time")
        if not 0 < gap_ms <= MAX_SAMPLE_GAP_SECONDS * 1_000:
            raise _SampleFailure("sample_gap")
    probe = _mapping(sample, "probe", stage="worker_readiness_payload")
    if (
        probe.get("ready") is not True
        or probe.get("lifecycle_state") != "running"
        or probe.get("unavailable_reason") is not None
    ):
        raise _SampleFailure("worker_not_ready")
    heartbeat_at_ms = _nonnegative_int(probe.get("heartbeat_at_ms"), stage="worker_heartbeat")
    if not 0 <= at_ms - heartbeat_at_ms <= MAX_SAMPLE_GAP_SECONDS * 1_000:
        raise _SampleFailure("worker_heartbeat_stale")
    revision = str(probe.get("runtime_revision") or "")
    if not _COMMIT_PATTERN.fullmatch(revision) or revision != metadata["versions"]["commit_sha"]:
        raise _SampleFailure("worker_runtime_revision_mismatch")
    _positive_int(probe.get("process_id"), stage="worker_process_id")
    if not str(probe.get("runtime_id") or "").strip():
        raise _SampleFailure("worker_runtime_id")
    if previous is not None:
        prior_probe = _mapping(previous, "probe", stage="previous_worker_readiness_payload")
        prior_heartbeat = _nonnegative_int(prior_probe.get("heartbeat_at_ms"), stage="previous_worker_heartbeat")
        if heartbeat_at_ms < prior_heartbeat:
            raise _SampleFailure("worker_heartbeat_regressed")
    if float(probe.get("probe_rtt_ms") or -1) < 0 or float(probe.get("probe_rtt_ms") or 0) > 1_000:
        raise _SampleFailure("worker_probe_latency")

    container = _mapping(sample, "container", stage="workers_container_payload")
    if container.get("running") is not True:
        raise _SampleFailure("workers_container_not_running")
    if container.get("oom_killed") is not False:
        raise _SampleFailure("workers_container_oom_killed")
    if container.get("image_revision") != revision:
        raise _SampleFailure("workers_image_revision_mismatch")
    if not str(container.get("container_id") or "").strip():
        raise _SampleFailure("workers_container_identity")
    if not str(container.get("image_id") or "").strip():
        raise _SampleFailure("workers_image_identity")
    _nonnegative_int(container.get("process_rss_bytes"), stage="workers_process_rss")
    if _nonnegative_int(container.get("container_memory_bytes"), stage="workers_container_memory") >= _MAX_RSS_BYTES:
        raise _SampleFailure("workers_container_memory_limit")

    postgres = _mapping(sample, "postgres", stage="postgres_payload")
    if _nonnegative_int(postgres.get("worker_connections"), stage="postgres_worker_connections") > 4:
        raise _SampleFailure("postgres_worker_connection_limit")
    if _nonnegative_int(postgres.get("lock_wait_count"), stage="postgres_lock_wait_count") != 0:
        raise _SampleFailure("postgres_lock_wait")
    if float(postgres.get("max_transaction_seconds") or 0) > _MAX_TRANSACTION_SECONDS:
        raise _SampleFailure("postgres_transaction_duration")
    frontiers = _mapping(postgres, "frontiers", stage="projection_frontiers")
    if set(frontiers) != set(_DOMAINS):
        raise _SampleFailure("projection_frontier_domains")
    for domain in _DOMAINS:
        frontier = _mapping(frontiers, domain, stage=f"projection_frontier_{domain}")
        if _nonnegative_int(frontier.get("unresolved_deadline_misses"), stage=f"{domain}_deadline_misses") != 0:
            raise _SampleFailure(f"{domain}_unresolved_deadline_miss")
        if _nonnegative_int(frontier.get("unresolved_quarantine"), stage=f"{domain}_quarantine") != 0:
            raise _SampleFailure(f"{domain}_projection_quarantine")

    telemetry = _mapping(sample, "telemetry", stage="worker_telemetry")
    active = _mapping(telemetry, "resource_active", stage="worker_resource_active")
    for capability, cap in _RESOURCE_CAPS.items():
        value = float(active.get(capability, 0.0))
        if value < 0 or value > cap:
            raise _SampleFailure(f"worker_resource_active_limit:{capability}")


def _summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if len(samples) != EXPECTED_SAMPLE_COUNT:
        raise _SampleFailure("sample_count")
    first = samples[0]
    last = samples[-1]
    duration_seconds = float(last["collector_elapsed_seconds"])
    duration_ms = duration_seconds * 1_000
    gaps_ms = [int(right["at_ms"]) - int(left["at_ms"]) for left, right in pairwise(samples)]
    probes = [_mapping(sample, "probe", stage="worker_readiness_payload") for sample in samples]
    containers = [_mapping(sample, "container", stage="workers_container_payload") for sample in samples]
    postgres_samples = [_mapping(sample, "postgres", stage="postgres_payload") for sample in samples]
    telemetry_samples = [_mapping(sample, "telemetry", stage="worker_telemetry") for sample in samples]

    runtime_ids = {str(probe.get("runtime_id") or "") for probe in probes}
    process_ids = {int(probe["process_id"]) for probe in probes}
    revisions = {str(probe.get("runtime_revision") or "") for probe in probes}
    container_ids = {str(container.get("container_id") or "") for container in containers}
    image_ids = {str(container.get("image_id") or "") for container in containers}
    restart_counts = [int(container["restart_count"]) for container in containers]
    rss_values = [int(container["process_rss_bytes"]) for container in containers]
    container_memory_values = [int(container["container_memory_bytes"]) for container in containers]
    probe_rtts = [float(probe["probe_rtt_ms"]) for probe in probes]

    temp_file_delta = int(postgres_samples[-1]["temp_files"]) - int(postgres_samples[0]["temp_files"])
    temp_byte_delta = int(postgres_samples[-1]["temp_bytes"]) - int(postgres_samples[0]["temp_bytes"])
    deadline_start = _deadline_counter_total(telemetry_samples[0])
    deadline_end = _deadline_counter_total(telemetry_samples[-1])
    capacity = _capacity_summary(first, last, duration_ms=duration_ms)

    checks = {
        "duration_at_least_1800_seconds": duration_seconds >= COLLECTION_DURATION_SECONDS,
        "sample_count_exact": len(samples) == EXPECTED_SAMPLE_COUNT,
        "sample_gap_at_most_15_seconds": bool(gaps_ms) and max(gaps_ms) <= MAX_SAMPLE_GAP_SECONDS * 1_000,
        "runtime_identity_constant": len(runtime_ids) == 1,
        "process_identity_constant": len(process_ids) == 1,
        "runtime_revision_constant": len(revisions) == 1,
        "container_identity_constant": len(container_ids) == 1,
        "image_identity_constant": len(image_ids) == 1,
        "restart_delta_zero": restart_counts[-1] - restart_counts[0] == 0 and len(set(restart_counts)) == 1,
        "oom_killed_false": all(container.get("oom_killed") is False for container in containers),
        "continuous_readiness": all(probe.get("ready") is True for probe in probes),
        "probe_rtt_at_most_1_second": max(probe_rtts) <= 1_000,
        "container_memory_below_2_gib": max(container_memory_values) < _MAX_RSS_BYTES,
        "postgres_connections_at_most_4": max(int(row["worker_connections"]) for row in postgres_samples) <= 4,
        "postgres_lock_wait_zero": max(int(row["lock_wait_count"]) for row in postgres_samples) == 0,
        "postgres_transaction_at_most_2_seconds": max(float(row["max_transaction_seconds"]) for row in postgres_samples)
        <= _MAX_TRANSACTION_SECONDS,
        "postgres_temp_delta_zero": temp_file_delta == 0 and temp_byte_delta == 0,
        "deadline_counter_delta_zero": deadline_end - deadline_start == 0,
        "unresolved_deadline_misses_zero": all(
            int(frontier["unresolved_deadline_misses"]) == 0
            for row in postgres_samples
            for frontier in _mapping(row, "frontiers", stage="projection_frontiers").values()
        ),
        "projection_quarantine_zero": all(
            int(frontier["unresolved_quarantine"]) == 0
            for row in postgres_samples
            for frontier in _mapping(row, "frontiers", stage="projection_frontiers").values()
        ),
        "resource_active_within_caps": all(
            0 <= float(_mapping(row, "resource_active", stage="worker_resource_active").get(capability, 0.0)) <= cap
            for row in telemetry_samples
            for capability, cap in _RESOURCE_CAPS.items()
        ),
        "four_domain_capacity_converges": all(bool(row["passes"]) for row in capacity.values()),
    }
    failed_checks = sorted(name for name, passed in checks.items() if not passed)
    return {
        "all_checks_passed": not failed_checks,
        "checks": checks,
        "failed_checks": failed_checks,
        "duration_seconds": duration_seconds,
        "max_sample_gap_ms": max(gaps_ms),
        "runtime": {
            "runtime_id": next(iter(runtime_ids)),
            "process_id": next(iter(process_ids)),
            "runtime_revision": next(iter(revisions)),
            "restart_count_start": restart_counts[0],
            "restart_count_end": restart_counts[-1],
            "max_probe_rtt_ms": max(probe_rtts),
        },
        "process_resources": {
            "max_process_rss_bytes": max(rss_values),
            "max_container_memory_bytes": max(container_memory_values),
            "container_memory_limit_bytes": _MAX_RSS_BYTES,
        },
        "postgres": {
            "max_worker_connections": max(int(row["worker_connections"]) for row in postgres_samples),
            "max_lock_wait_count": max(int(row["lock_wait_count"]) for row in postgres_samples),
            "max_transaction_seconds": max(float(row["max_transaction_seconds"]) for row in postgres_samples),
            "temp_files_delta": temp_file_delta,
            "temp_bytes_delta": temp_byte_delta,
        },
        "deadline_misses": {
            "unresolved_start": _unresolved_total(postgres_samples[0], "unresolved_deadline_misses"),
            "counter_delta": deadline_end - deadline_start,
            "unresolved_end": _unresolved_total(postgres_samples[-1], "unresolved_deadline_misses"),
        },
        "unresolved_projection_quarantine": _unresolved_total(
            postgres_samples[-1],
            "unresolved_quarantine",
        ),
        "capacity": capacity,
    }


def _capacity_summary(
    first: dict[str, Any],
    last: dict[str, Any],
    *,
    duration_ms: float,
) -> dict[str, dict[str, Any]]:
    minutes = duration_ms / 60_000
    first_frontiers = _mapping(_mapping(first, "postgres", stage="postgres_payload"), "frontiers", stage="frontiers")
    last_frontiers = _mapping(_mapping(last, "postgres", stage="postgres_payload"), "frontiers", stage="frontiers")
    first_transitions = _mapping(
        _mapping(first, "telemetry", stage="worker_telemetry"),
        "projection_transitions_total",
        stage="projection_transitions",
    )
    last_transitions = _mapping(
        _mapping(last, "telemetry", stage="worker_telemetry"),
        "projection_transitions_total",
        stage="projection_transitions",
    )
    result: dict[str, dict[str, Any]] = {}
    for domain in _DOMAINS:
        start = _mapping(first_frontiers, domain, stage=f"frontier_{domain}")
        end = _mapping(last_frontiers, domain, stage=f"frontier_{domain}")
        start_counts = _mapping_or_empty(first_transitions.get(domain))
        end_counts = _mapping_or_empty(last_transitions.get(domain))
        arrivals = int(float(end_counts.get("arrival", 0.0)) - float(start_counts.get("arrival", 0.0)))
        completions = int(float(end_counts.get("completion", 0.0)) - float(start_counts.get("completion", 0.0)))
        if arrivals < 0 or completions < 0:
            raise _SampleFailure(f"projection_transition_counter_regressed:{domain}")
        actionable_start = int(start["actionable_count"])
        actionable_end = int(end["actionable_count"])
        oldest_start = int(start["oldest_age_ms"])
        oldest_end = int(end["oldest_age_ms"])
        arrival_rate = arrivals / minutes
        completion_rate = completions / minutes
        empty_boundary = actionable_start == 0 and actionable_end == 0
        freshness_ok = int(end["unresolved_deadline_misses"]) == 0
        converging = completion_rate > arrival_rate and oldest_end < oldest_start
        net_per_ms = (completion_rate - arrival_rate) / 60_000
        bounded_clear = int(actionable_end / net_per_ms) if actionable_end > 0 and net_per_ms > 0 else None
        passes = empty_boundary or (converging and (actionable_end == 0 or freshness_ok or bounded_clear is not None))
        result[domain] = {
            "actionable_count_start": actionable_start,
            "actionable_count_end": actionable_end,
            "oldest_age_ms_start": oldest_start,
            "oldest_age_ms_end": oldest_end,
            "arrival_count": arrivals,
            "arrival_rate_per_minute": arrival_rate,
            "completion_count": completions,
            "completion_rate_per_minute": completion_rate,
            "freshness_ok": freshness_ok,
            "bounded_time_to_clear_ms": bounded_clear,
            "passes": passes,
        }
    return result


def _parse_worker_metrics(payload: str) -> dict[str, Any]:
    resource_active = {capability: 0.0 for capability in _RESOURCE_CAPS}
    deadline_misses: dict[str, float] = {domain: 0.0 for domain in _DOMAINS}
    transitions: dict[str, dict[str, float]] = {domain: {"arrival": 0.0, "completion": 0.0} for domain in _DOMAINS}
    resource_service: list[dict[str, Any]] = []
    resource_admission: list[dict[str, Any]] = []
    try:
        families = text_string_to_metric_families(payload)
        for family in families:
            for sample in family.samples:
                labels = dict(sample.labels)
                if sample.name == "tracefold_worker_resource_active":
                    resource_active[str(labels.get("capability") or "unknown")] = float(sample.value)
                elif sample.name == "tracefold_worker_projection_deadline_misses_total":
                    domain = str(labels.get("domain") or "unknown")
                    deadline_misses[domain] = deadline_misses.get(domain, 0.0) + float(sample.value)
                elif sample.name == "tracefold_worker_projection_transitions_total":
                    domain = str(labels.get("domain") or "unknown")
                    transition = str(labels.get("transition") or "unknown")
                    transitions.setdefault(domain, {})[transition] = float(sample.value)
                elif sample.name in {
                    "tracefold_worker_resource_service_seconds_count",
                    "tracefold_worker_resource_service_seconds_sum",
                }:
                    resource_service.append({"name": sample.name, "labels": labels, "value": float(sample.value)})
                elif sample.name in {
                    "tracefold_worker_resource_admission_seconds_count",
                    "tracefold_worker_resource_admission_seconds_sum",
                }:
                    resource_admission.append({"name": sample.name, "labels": labels, "value": float(sample.value)})
    except BaseException as exc:
        raise _SampleFailure("worker_metrics_parse", exc) from exc
    return {
        "resource_active": resource_active,
        "projection_deadline_misses_total": deadline_misses,
        "projection_transitions_total": transitions,
        "resource_service": resource_service,
        "resource_admission": resource_admission,
    }


def _frontier_snapshot_sql() -> str:
    union = "\nUNION ALL\n".join(
        f"SELECT '{domain}'::text AS domain, status, first_dirty_at_ms, deadline_at_ms FROM {table}"
        for domain, table in _FRONTIER_TABLES.items()
    )
    actionable = ", ".join(f"'{status}'" for status in _ACTIONABLE_STATUSES)
    return f"""
        WITH frontier AS (
          {union}
        )
        SELECT
          domain,
          COALESCE(SUM(status_count) FILTER (WHERE status IN ({actionable})), 0) AS actionable_count,
          COALESCE(
            GREATEST(
              0,
              %(now_ms)s - MIN(first_dirty_at_ms) FILTER (WHERE status IN ({actionable}))
            ),
            0
          ) AS oldest_age_ms,
          COALESCE(SUM(overdue_count), 0) AS unresolved_deadline_misses,
          COALESCE(SUM(status_count) FILTER (WHERE status = 'quarantined'), 0) AS unresolved_quarantine,
          jsonb_object_agg(status, status_count ORDER BY status) AS counts_by_status
        FROM (
          SELECT
            domain,
            status,
            MIN(first_dirty_at_ms) AS first_dirty_at_ms,
            MIN(deadline_at_ms) AS deadline_at_ms,
            COUNT(*) AS status_count,
            COUNT(*) FILTER (
              WHERE status IN ({actionable})
                AND deadline_at_ms IS NOT NULL
                AND deadline_at_ms < %(now_ms)s
            ) AS overdue_count
          FROM frontier
          GROUP BY domain, status
        ) grouped
        GROUP BY domain
        ORDER BY domain
    """


def _prepare_bundle(bundle_dir: Path, *, repository_root: Path) -> Path:
    requested = Path(bundle_dir).expanduser()
    if not requested.is_absolute():
        raise ValueError("workers_runtime_collection_bundle_must_be_absolute")
    resolved = requested.resolve()
    if resolved == repository_root or resolved.is_relative_to(repository_root):
        raise ValueError("workers_runtime_collection_bundle_must_be_outside_checkout")
    try:
        resolved.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError("workers_runtime_collection_bundle_must_not_exist") from exc
    return resolved


def _collection_metadata(settings: Any, *, repository_root: Path) -> dict[str, Any]:
    commit = _git_head(repository_root)
    return {
        "source": {
            "repository": "AnalyThothAI/tracefold",
            "session": f"production-{int(time.time() * 1_000)}",
            "cutoff_at_ms": int(time.time() * 1_000),
        },
        "versions": {
            "commit_sha": commit,
            "migration_version": latest_migration_version(),
        },
        "configuration": {
            "config_path": str((settings.app_home / "config.yaml").resolve()),
            "redacted_enablement": {
                "collector_enabled": bool(settings.upstream.channels),
                "news_enabled": bool(settings.news.enabled),
                "macro_enabled": bool(settings.providers.macro_sources.enabled),
                "model_configured": bool(settings.llm.api_key or settings.llm.openrouter_api_key),
            },
        },
    }


def _git_head(repository_root: Path) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("workers_runtime_collection_git_unavailable")
    try:
        result = subprocess.run(  # noqa: S603 -- resolved executable and fixed arguments
            [executable, "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except BaseException as exc:
        raise ValueError("workers_runtime_collection_git_head_unavailable") from exc
    commit = result.stdout.strip()
    if not _COMMIT_PATTERN.fullmatch(commit):
        raise ValueError("workers_runtime_collection_git_head_invalid")
    return commit


def _require_clean_checkout(repository_root: Path) -> None:
    if not _repository_is_clean(repository_root):
        raise ValueError("workers_runtime_collection_checkout_dirty")


def _repository_is_clean(repository_root: Path) -> bool:
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("workers_runtime_collection_git_unavailable")
    try:
        result = subprocess.run(  # noqa: S603 -- resolved executable and fixed arguments
            [executable, "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except BaseException as exc:
        raise ValueError("workers_runtime_collection_git_status_unavailable") from exc
    return not result.stdout.strip()


def _dsn_for_compose_endpoint(dsn: str, endpoint: str) -> str:
    normalized = str(endpoint).strip()
    if not normalized or "\n" in normalized or ":" not in normalized:
        raise _SampleFailure("postgres_compose_endpoint_payload")
    host, raw_port = normalized.rsplit(":", 1)
    host = host.strip().strip("[]")
    if host in {"", _IPV4_ANY, "::"}:
        host = "127.0.0.1"
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise _SampleFailure("postgres_compose_endpoint_payload", exc) from exc
    if not 1 <= port <= 65_535:
        raise _SampleFailure("postgres_compose_endpoint_payload")
    try:
        parts = dict(conninfo.conninfo_to_dict(str(dsn)))
        parts["host"] = host
        parts["port"] = str(port)
        return str(conninfo.make_conninfo(**parts))
    except BaseException as exc:
        raise _SampleFailure("postgres_compose_dsn", exc) from exc


def _write_initialization_failure(
    bundle: Path,
    *,
    metadata: dict[str, Any],
    failure: dict[str, Any],
) -> dict[str, Any]:
    samples_path = bundle / SAMPLES_FILE
    if not samples_path.exists():
        samples_path.write_text("", encoding="utf-8")
    result = {
        **metadata,
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "status": "failed",
        "sample_policy": _sample_policy(),
        "sample_count": 0,
        "start_at_ms": None,
        "end_at_ms": int(time.time() * 1_000),
        "failure": failure,
        "samples_path": SAMPLES_FILE,
        "samples_sha256": _sha256_file(samples_path),
    }
    _write_json_exclusive(bundle / COLLECTION_FILE, result)
    return result


def _sample_policy() -> dict[str, int]:
    return {
        "duration_seconds": COLLECTION_DURATION_SECONDS,
        "interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "max_gap_seconds": MAX_SAMPLE_GAP_SECONDS,
        "expected_sample_count": EXPECTED_SAMPLE_COUNT,
    }


def _deadline_counter_total(telemetry: Mapping[str, Any]) -> int:
    values = _mapping(telemetry, "projection_deadline_misses_total", stage="deadline_metrics")
    return int(sum(float(value) for value in values.values()))


def _unresolved_total(postgres: Mapping[str, Any], key: str) -> int:
    frontiers = _mapping(postgres, "frontiers", stage="projection_frontiers")
    return sum(int(_mapping(frontiers, domain, stage=f"frontier_{domain}")[key]) for domain in _DOMAINS)


def _mapping(payload: Mapping[str, Any], key: str, *, stage: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise _SampleFailure(stage)
    return dict(value)


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _nonnegative_int(value: Any, *, stage: str) -> int:
    if isinstance(value, bool):
        raise _SampleFailure(stage)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _SampleFailure(stage, exc) from exc
    if parsed < 0:
        raise _SampleFailure(stage)
    return parsed


def _positive_int(value: Any, *, stage: str) -> int:
    parsed = _nonnegative_int(value, stage=stage)
    if parsed <= 0:
        raise _SampleFailure(stage)
    return parsed


def _nonnegative_float(value: Any, *, stage: str) -> float:
    if isinstance(value, bool):
        raise _SampleFailure(stage)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise _SampleFailure(stage, exc) from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise _SampleFailure(stage)
    return parsed


def _failure(stage: str, exc: BaseException | None) -> dict[str, Any]:
    cause_type = None
    if isinstance(exc, _SampleFailure):
        cause_type = exc.cause_type
    elif exc is not None:
        cause_type = type(exc).__name__
    return {"stage": str(stage), "cause_type": cause_type}


def _json_line(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"


def _write_json_exclusive(path: Path, payload: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(_json_line(payload))
        stream.flush()
        os.fsync(stream.fileno())


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "COLLECTION_DURATION_SECONDS",
    "COLLECTION_FILE",
    "EXPECTED_SAMPLE_COUNT",
    "MAX_SAMPLE_GAP_SECONDS",
    "SAMPLES_FILE",
    "SAMPLE_INTERVAL_SECONDS",
    "CollectorDependencies",
    "collect_workers_runtime_acceptance",
    "validate_workers_runtime_collection",
]
