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
from urllib.request import ProxyHandler, build_opener

from prometheus_client.parser import text_string_to_metric_families
from psycopg import conninfo

from tracefold.app.database import WORKER_DATABASE_LOCK_TIMEOUT_SECONDS
from tracefold.app.provider_ownership import gmgn_stream_enabled
from tracefold.app.workers_runtime import WORKERS_RUNTIME_VERSION
from tracefold.market import TOKEN_RADAR_PROJECTION_VERSION
from tracefold.news import NEWS_STORY_PUBLISH_TIMEOUT_SECONDS
from tracefold.platform.postgres.postgres_audit import (
    HOT_QUERIES,
    PUBLIC_NO_SQL_ROUTES,
    PUBLIC_ROUTE_QUERY_COVERAGE,
    PostgresQueryAudit,
)
from tracefold.platform.postgres.postgres_client import (
    connect_postgres,
    with_password_from_file,
)
from tracefold.platform.postgres.postgres_migrations import latest_migration_version
from tracefold.platform.postgres.projection_frontier import FRONTIER_SPECS

COLLECTION_DURATION_SECONDS = 30 * 60
SAMPLE_INTERVAL_SECONDS = 10
MAX_SAMPLE_GAP_SECONDS = 15
EXPECTED_SAMPLE_COUNT = COLLECTION_DURATION_SECONDS // SAMPLE_INTERVAL_SECONDS + 1

COLLECTION_SCHEMA_VERSION = "workers_runtime_acceptance_collection_v2"
SAMPLES_FILE = "workers-runtime-samples.jsonl"
COLLECTION_FILE = "workers-runtime-collection.json"

_PROBE_URL = "http://127.0.0.1:8766/readyz"
_METRICS_URL = "http://127.0.0.1:8766/metrics"
_HTTP_TIMEOUT_SECONDS = 1.0
_MAX_PROBE_BYTES = 64 * 1024
_MAX_METRICS_BYTES = 4 * 1024 * 1024
_MAX_RSS_BYTES = 2 * 1024 * 1024 * 1024
_MAX_TRANSACTION_SECONDS = NEWS_STORY_PUBLISH_TIMEOUT_SECONDS
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
_DOMAINS = tuple(spec.domain for spec in FRONTIER_SPECS)
_FRONTIER_TABLES = {spec.domain: spec.table for spec in FRONTIER_SPECS}
_REQUIRED_METRIC_FAMILIES = {
    "tracefold_worker_projection_deadline_misses",
    "tracefold_worker_projection_soft_slo_overruns",
    "tracefold_worker_projection_transitions",
    "tracefold_worker_resource_active",
    "tracefold_worker_resource_admission_seconds",
    "tracefold_worker_resource_service_seconds",
}
_RESOURCE_METRIC_NAMES = {
    "resource_admission": {
        "tracefold_worker_resource_admission_seconds_count",
        "tracefold_worker_resource_admission_seconds_sum",
    },
    "resource_service": {
        "tracefold_worker_resource_service_seconds_count",
        "tracefold_worker_resource_service_seconds_sum",
    },
}
_RESOURCE_OUTCOMES = {
    "resource_admission": {"accepted", "timeout"},
    "resource_service": {"cancelled", "error", "success"},
}
_LOOPBACK_PROXY = ProxyHandler({})
_LOOPBACK_HTTP = build_opener(_LOOPBACK_PROXY)


@dataclass(frozen=True, slots=True)
class _CollectorDependencies:
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
            dependencies=_CollectorDependencies(
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
    dependencies: _CollectorDependencies,
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
        postgres = self._read_postgres(
            now_ms=at_ms,
            include_query_audit=sequence == 0,
        )
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
            with _LOOPBACK_HTTP.open(url, timeout=_HTTP_TIMEOUT_SECONDS) as response:
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

    def _read_postgres(
        self,
        *,
        now_ms: int,
        include_query_audit: bool,
    ) -> dict[str, Any]:
        try:
            activity = self._conn.execute(
                """
                WITH worker_connections AS (
                  SELECT
                    activity.pid,
                    COALESCE(wait_event_type, 'none') AS wait_event_type,
                    COALESCE(
                      EXTRACT(EPOCH FROM (clock_timestamp() - xact_start)),
                      0
                    ) AS transaction_seconds,
                    COALESCE(
                      MAX(
                        EXTRACT(EPOCH FROM (clock_timestamp() - waiting_lock.waitstart))
                      ) FILTER (WHERE NOT waiting_lock.granted),
                      0
                    ) AS lock_wait_seconds
                  FROM pg_stat_activity activity
                  LEFT JOIN pg_locks waiting_lock
                    ON waiting_lock.pid = activity.pid
                   AND NOT waiting_lock.granted
                   AND activity.wait_event_type = 'Lock'
                  WHERE activity.datname = current_database()
                    AND activity.application_name LIKE 'tracefold_workers%'
                  GROUP BY activity.pid, activity.wait_event_type, activity.xact_start
                ), worker_activity AS (
                  SELECT
                    wait_event_type,
                    COUNT(*) AS wait_count,
                    MAX(transaction_seconds) AS max_transaction_seconds,
                    MAX(lock_wait_seconds) AS max_lock_wait_seconds
                  FROM worker_connections
                  GROUP BY wait_event_type
                )
                SELECT
                  COALESCE(SUM(wait_count), 0) AS worker_connections,
                  COALESCE(MAX(max_transaction_seconds), 0) AS max_transaction_seconds,
                  COALESCE(MAX(max_lock_wait_seconds), 0) AS max_lock_wait_seconds,
                  COALESCE(
                    jsonb_object_agg(wait_event_type, wait_count ORDER BY wait_event_type),
                    '{}'::jsonb
                  ) AS waits_by_type
                FROM worker_activity
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
            query_audit = (
                PostgresQueryAudit(
                    self._conn,
                    token_radar_projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                    now_ms=now_ms,
                ).run(analyze=True)
                if include_query_audit
                else None
            )
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
        waits_by_type = {
            str(wait_type): int(wait_count) for wait_type, wait_count in dict(activity["waits_by_type"] or {}).items()
        }
        payload = {
            "worker_connections": int(activity["worker_connections"]),
            "lock_wait_count": int(waits_by_type.get("Lock", 0)),
            "max_lock_wait_seconds": float(activity["max_lock_wait_seconds"]),
            "waits_by_type": waits_by_type,
            "max_transaction_seconds": float(activity["max_transaction_seconds"]),
            "temp_files": int(database["temp_files"]),
            "temp_bytes": int(database["temp_bytes"]),
            "frontiers": frontiers,
        }
        if query_audit is not None:
            payload["query_audit"] = query_audit
        return payload

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
        probe.get("ok") is not True
        or probe.get("ready") is not True
        or probe.get("lifecycle_state") != "running"
        or probe.get("unavailable_reason") is not None
    ):
        raise _SampleFailure("worker_not_ready")
    if str(probe.get("runtime_version") or "") != WORKERS_RUNTIME_VERSION:
        raise _SampleFailure("worker_runtime_version")
    heartbeat_at_ms = _nonnegative_int(probe.get("heartbeat_at_ms"), stage="worker_heartbeat")
    if not 0 <= at_ms - heartbeat_at_ms <= MAX_SAMPLE_GAP_SECONDS * 1_000:
        raise _SampleFailure("worker_heartbeat_stale")
    if (
        _positive_int(
            probe.get("heartbeat_stale_after_ms"),
            stage="worker_heartbeat_stale_after",
        )
        > MAX_SAMPLE_GAP_SECONDS * 1_000
    ):
        raise _SampleFailure("worker_heartbeat_policy")
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
    if _nonnegative_float(probe.get("probe_rtt_ms"), stage="worker_probe_latency") > 1_000:
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
    _nonnegative_int(container.get("restart_count"), stage="workers_restart_count")
    _positive_int(container.get("host_process_id"), stage="workers_host_process_id")
    _nonnegative_int(container.get("process_rss_bytes"), stage="workers_process_rss")
    if _nonnegative_int(container.get("container_memory_bytes"), stage="workers_container_memory") >= _MAX_RSS_BYTES:
        raise _SampleFailure("workers_container_memory_limit")

    postgres = _mapping(sample, "postgres", stage="postgres_payload")
    worker_connections = _positive_int(
        postgres.get("worker_connections"),
        stage="postgres_worker_connections",
    )
    if worker_connections > 4:
        raise _SampleFailure("postgres_worker_connection_limit")
    waits_by_type = _mapping(postgres, "waits_by_type", stage="postgres_waits_by_type")
    if not waits_by_type:
        raise _SampleFailure("postgres_waits_by_type")
    normalized_waits: dict[str, int] = {}
    for raw_wait_type, raw_count in waits_by_type.items():
        wait_type = str(raw_wait_type).strip()
        if not wait_type or wait_type in normalized_waits:
            raise _SampleFailure("postgres_waits_by_type")
        normalized_waits[wait_type] = _nonnegative_int(
            raw_count,
            stage=f"postgres_wait_count:{wait_type}",
        )
    if sum(normalized_waits.values()) != worker_connections:
        raise _SampleFailure("postgres_wait_count_mismatch")
    lock_wait_count = _nonnegative_int(
        postgres.get("lock_wait_count"),
        stage="postgres_lock_wait_count",
    )
    if lock_wait_count != normalized_waits.get("Lock", 0):
        raise _SampleFailure("postgres_lock_wait_count_mismatch")
    max_lock_wait_seconds = _nonnegative_float(
        postgres.get("max_lock_wait_seconds"),
        stage="postgres_lock_wait_duration",
    )
    if lock_wait_count == 0 and max_lock_wait_seconds != 0:
        raise _SampleFailure("postgres_lock_wait_duration_mismatch")
    if max_lock_wait_seconds > WORKER_DATABASE_LOCK_TIMEOUT_SECONDS:
        raise _SampleFailure("postgres_lock_wait_duration")
    if (
        _nonnegative_float(
            postgres.get("max_transaction_seconds"),
            stage="postgres_transaction_duration",
        )
        > _MAX_TRANSACTION_SECONDS
    ):
        raise _SampleFailure("postgres_transaction_duration")
    temp_files = _nonnegative_int(postgres.get("temp_files"), stage="postgres_temp_files")
    temp_bytes = _nonnegative_int(postgres.get("temp_bytes"), stage="postgres_temp_bytes")
    if previous is not None:
        previous_postgres = _mapping(previous, "postgres", stage="previous_postgres_payload")
        if temp_files < _nonnegative_int(
            previous_postgres.get("temp_files"),
            stage="previous_postgres_temp_files",
        ) or temp_bytes < _nonnegative_int(
            previous_postgres.get("temp_bytes"),
            stage="previous_postgres_temp_bytes",
        ):
            raise _SampleFailure("postgres_temp_counter_regressed")
    if sequence == 0:
        _validate_query_audit(_mapping(postgres, "query_audit", stage="postgres_query_audit"))
    elif "query_audit" in postgres:
        raise _SampleFailure("postgres_query_audit_repeated")
    frontiers = _mapping(postgres, "frontiers", stage="projection_frontiers")
    if set(frontiers) != set(_DOMAINS):
        raise _SampleFailure("projection_frontier_domains")
    for domain in _DOMAINS:
        frontier = _mapping(frontiers, domain, stage=f"projection_frontier_{domain}")
        actionable_count = _nonnegative_int(
            frontier.get("actionable_count"),
            stage=f"{domain}_actionable_count",
        )
        _nonnegative_int(frontier.get("oldest_age_ms"), stage=f"{domain}_oldest_age")
        deadline_misses = _nonnegative_int(
            frontier.get("unresolved_deadline_misses"),
            stage=f"{domain}_deadline_misses",
        )
        quarantine = _nonnegative_int(
            frontier.get("unresolved_quarantine"),
            stage=f"{domain}_quarantine",
        )
        counts_by_status = _mapping(
            frontier,
            "counts_by_status",
            stage=f"{domain}_counts_by_status",
        )
        if not set(counts_by_status).issubset({"clean", "dirty", "running", "retry_wait", "quarantined"}):
            raise _SampleFailure(f"{domain}_frontier_status")
        normalized_status_counts = {
            status: _nonnegative_int(value, stage=f"{domain}_frontier_status:{status}")
            for status, value in counts_by_status.items()
        }
        if actionable_count != sum(normalized_status_counts.get(status, 0) for status in _ACTIONABLE_STATUSES):
            raise _SampleFailure(f"{domain}_actionable_count_mismatch")
        if quarantine != normalized_status_counts.get("quarantined", 0):
            raise _SampleFailure(f"{domain}_quarantine_count_mismatch")
        if deadline_misses > actionable_count:
            raise _SampleFailure(f"{domain}_deadline_count_mismatch")
        if deadline_misses != 0:
            raise _SampleFailure(f"{domain}_unresolved_deadline_miss")
        if quarantine != 0:
            raise _SampleFailure(f"{domain}_projection_quarantine")

    telemetry = _mapping(sample, "telemetry", stage="worker_telemetry")
    previous_telemetry = (
        _mapping(previous, "telemetry", stage="previous_worker_telemetry") if previous is not None else None
    )
    _validate_telemetry(telemetry, previous=previous_telemetry)


def _validate_query_audit(audit: dict[str, Any]) -> None:
    if audit.get("ok") is not True or audit.get("engine") != "postgresql" or audit.get("analyze") is not True:
        raise _SampleFailure("postgres_query_audit_failed")
    thresholds = _mapping(audit, "thresholds", stage="postgres_query_audit_thresholds")
    if not thresholds:
        raise _SampleFailure("postgres_query_audit_thresholds")
    for name, value in thresholds.items():
        _nonnegative_float(value, stage=f"postgres_query_audit_threshold:{name}")

    coverage = _mapping(audit, "route_coverage", stage="postgres_query_audit_route_coverage")
    expected_routes = json.loads(json.dumps(PUBLIC_ROUTE_QUERY_COVERAGE, sort_keys=True))
    observed_routes = json.loads(json.dumps(coverage.get("query_routes"), sort_keys=True))
    if observed_routes != expected_routes:
        raise _SampleFailure("postgres_query_audit_route_coverage")
    if coverage.get("no_sql_routes") != sorted(PUBLIC_NO_SQL_ROUTES):
        raise _SampleFailure("postgres_query_audit_no_sql_routes")
    if coverage.get("missing_query_names") != []:
        raise _SampleFailure("postgres_query_audit_route_gap")

    queries = audit.get("queries")
    if not isinstance(queries, list) or not queries:
        raise _SampleFailure("postgres_query_audit_queries")
    expected_names = {str(item["name"]) for item in HOT_QUERIES}
    observed_names: set[str] = set()
    for raw in queries:
        if not isinstance(raw, Mapping):
            raise _SampleFailure("postgres_query_audit_query")
        query = dict(raw)
        name = str(query.get("name") or "").strip()
        if not name or name in observed_names:
            raise _SampleFailure("postgres_query_audit_query_name")
        observed_names.add(name)
        if query.get("ok") is not True or query.get("violations") != []:
            raise _SampleFailure(f"postgres_query_audit_violation:{name}")
        plan = query.get("plan")
        if not isinstance(plan, list) or not plan:
            raise _SampleFailure(f"postgres_query_audit_plan:{name}")
        metrics = _mapping(query, "metrics", stage=f"postgres_query_audit_metrics:{name}")
        if metrics.get("plan_json_valid") is not True:
            raise _SampleFailure(f"postgres_query_audit_plan:{name}")
        for metric in ("execution_time_ms", "planning_time_ms"):
            value = metrics.get(metric)
            if value is not None:
                _nonnegative_float(value, stage=f"postgres_query_audit_metric:{name}:{metric}")
        for metric in (
            "returned_rows",
            "read_rows",
            "temp_read_blocks",
            "temp_written_blocks",
        ):
            _nonnegative_int(
                metrics.get(metric),
                stage=f"postgres_query_audit_metric:{name}:{metric}",
            )
        _nonnegative_float(
            metrics.get("read_return_amplification"),
            stage=f"postgres_query_audit_metric:{name}:read_return_amplification",
        )
        if metrics.get("large_seq_scans") != []:
            raise _SampleFailure(f"postgres_query_audit_large_seq_scan:{name}")
    if observed_names != expected_names:
        raise _SampleFailure("postgres_query_audit_query_coverage")


def _validate_telemetry(
    telemetry: dict[str, Any],
    *,
    previous: dict[str, Any] | None,
) -> None:
    raw_families = telemetry.get("metric_families")
    if not isinstance(raw_families, list) or any(
        not isinstance(name, str) or not name.strip() for name in raw_families
    ):
        raise _SampleFailure("worker_metric_families")
    metric_families = set(raw_families)
    if len(metric_families) != len(raw_families):
        raise _SampleFailure("worker_metric_families")
    missing_families = _REQUIRED_METRIC_FAMILIES - metric_families
    if missing_families:
        raise _SampleFailure(f"worker_metric_family_missing:{sorted(missing_families)[0]}")

    active = _mapping(telemetry, "resource_active", stage="worker_resource_active")
    if set(active) != set(_RESOURCE_CAPS):
        raise _SampleFailure("worker_resource_active_capabilities")
    for capability, cap in _RESOURCE_CAPS.items():
        value = _nonnegative_float(
            active.get(capability),
            stage=f"worker_resource_active:{capability}",
        )
        if value > cap:
            raise _SampleFailure(f"worker_resource_active_limit:{capability}")

    deadline_misses = _counter_domain_values(
        telemetry,
        "projection_deadline_misses_total",
        stage="projection_deadline_metrics",
    )
    soft_slo_overruns = _counter_domain_values(
        telemetry,
        "projection_soft_slo_overruns_total",
        stage="projection_soft_slo_metrics",
    )
    transitions = _mapping(
        telemetry,
        "projection_transitions_total",
        stage="projection_transition_metrics",
    )
    if set(transitions) != set(_DOMAINS):
        raise _SampleFailure("projection_transition_domains")
    normalized_transitions: dict[str, dict[str, float]] = {}
    for domain in _DOMAINS:
        domain_values = _mapping(
            transitions,
            domain,
            stage=f"projection_transition_metrics:{domain}",
        )
        if set(domain_values) != {"arrival", "completion"}:
            raise _SampleFailure(f"projection_transition_shape:{domain}")
        normalized_transitions[domain] = {
            transition: _nonnegative_float(
                domain_values.get(transition),
                stage=f"projection_transition_metric:{domain}:{transition}",
            )
            for transition in ("arrival", "completion")
        }

    resources = {key: _validate_resource_metric_rows(telemetry.get(key), key=key) for key in _RESOURCE_METRIC_NAMES}
    if previous is None:
        return

    previous_deadline_misses = _counter_domain_values(
        previous,
        "projection_deadline_misses_total",
        stage="previous_projection_deadline_metrics",
    )
    previous_soft_slo = _counter_domain_values(
        previous,
        "projection_soft_slo_overruns_total",
        stage="previous_projection_soft_slo_metrics",
    )
    previous_transitions = _mapping(
        previous,
        "projection_transitions_total",
        stage="previous_projection_transition_metrics",
    )
    for domain in _DOMAINS:
        if deadline_misses[domain] < previous_deadline_misses[domain]:
            raise _SampleFailure(f"projection_deadline_counter_regressed:{domain}")
        if soft_slo_overruns[domain] < previous_soft_slo[domain]:
            raise _SampleFailure(f"projection_soft_slo_counter_regressed:{domain}")
        previous_domain_transitions = _mapping(
            previous_transitions,
            domain,
            stage=f"previous_projection_transition_metrics:{domain}",
        )
        for transition in ("arrival", "completion"):
            previous_value = _nonnegative_float(
                previous_domain_transitions.get(transition),
                stage=f"previous_projection_transition_metric:{domain}:{transition}",
            )
            if normalized_transitions[domain][transition] < previous_value:
                raise _SampleFailure(f"projection_transition_counter_regressed:{domain}")
    for key, current_rows in resources.items():
        previous_rows = _validate_resource_metric_rows(previous.get(key), key=key)
        for series, previous_value in previous_rows.items():
            current_value = current_rows.get(series)
            if current_value is None:
                raise _SampleFailure(f"worker_{key}_series_disappeared")
            if current_value < previous_value:
                raise _SampleFailure(f"worker_{key}_counter_regressed")


def _counter_domain_values(
    telemetry: Mapping[str, Any],
    key: str,
    *,
    stage: str,
) -> dict[str, float]:
    values = _mapping(telemetry, key, stage=stage)
    if set(values) != set(_DOMAINS):
        raise _SampleFailure(f"{stage}_domains")
    return {domain: _nonnegative_float(values.get(domain), stage=f"{stage}:{domain}") for domain in _DOMAINS}


def _validate_resource_metric_rows(
    raw_rows: Any,
    *,
    key: str,
) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    if not isinstance(raw_rows, list) or not raw_rows:
        raise _SampleFailure(f"worker_{key}_required")
    allowed_names = _RESOURCE_METRIC_NAMES[key]
    allowed_outcomes = _RESOURCE_OUTCOMES[key]
    rows: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    grouped_names: dict[tuple[tuple[str, str], ...], set[str]] = {}
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise _SampleFailure(f"worker_{key}_row")
        row = dict(raw)
        name = str(row.get("name") or "")
        if name not in allowed_names:
            raise _SampleFailure(f"worker_{key}_name")
        labels = _mapping(row, "labels", stage=f"worker_{key}_labels")
        if set(labels) != {"capability", "operation", "outcome"}:
            raise _SampleFailure(f"worker_{key}_labels")
        capability = str(labels["capability"]).strip()
        operation = str(labels["operation"]).strip()
        outcome = str(labels["outcome"]).strip()
        if capability not in _RESOURCE_CAPS or not operation or outcome not in allowed_outcomes:
            raise _SampleFailure(f"worker_{key}_labels")
        normalized_labels = tuple(
            sorted(
                (
                    ("capability", capability),
                    ("operation", operation),
                    ("outcome", outcome),
                )
            )
        )
        series = (name, normalized_labels)
        if series in rows:
            raise _SampleFailure(f"worker_{key}_duplicate_series")
        value = _nonnegative_float(row.get("value"), stage=f"worker_{key}_value")
        if name.endswith("_count") and not value.is_integer():
            raise _SampleFailure(f"worker_{key}_count")
        rows[series] = value
        grouped_names.setdefault(normalized_labels, set()).add(name)
    if any(names != allowed_names for names in grouped_names.values()):
        raise _SampleFailure(f"worker_{key}_incomplete_series")
    return rows


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
    soft_slo = {
        domain: {
            "counter_start": float(telemetry_samples[0]["projection_soft_slo_overruns_total"][domain]),
            "counter_delta": float(telemetry_samples[-1]["projection_soft_slo_overruns_total"][domain])
            - float(telemetry_samples[0]["projection_soft_slo_overruns_total"][domain]),
            "counter_end": float(telemetry_samples[-1]["projection_soft_slo_overruns_total"][domain]),
        }
        for domain in _DOMAINS
    }
    resource_metrics = {
        "admission": _resource_metric_summary(
            telemetry_samples[0],
            telemetry_samples[-1],
            key="resource_admission",
        ),
        "service": _resource_metric_summary(
            telemetry_samples[0],
            telemetry_samples[-1],
            key="resource_service",
        ),
        "max_active": {
            capability: max(
                float(_mapping(row, "resource_active", stage="worker_resource_active")[capability])
                for row in telemetry_samples
            )
            for capability in _RESOURCE_CAPS
        },
    }
    wait_types = sorted(
        {
            str(wait_type)
            for row in postgres_samples
            for wait_type in _mapping(row, "waits_by_type", stage="postgres_waits_by_type")
        }
    )
    max_waits_by_type = {
        wait_type: max(
            int(_mapping(row, "waits_by_type", stage="postgres_waits_by_type").get(wait_type, 0))
            for row in postgres_samples
        )
        for wait_type in wait_types
    }
    query_audit = _query_audit_summary(_mapping(postgres_samples[0], "query_audit", stage="postgres_query_audit"))
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
        "postgres_lock_wait_within_budget": max(float(row["max_lock_wait_seconds"]) for row in postgres_samples)
        <= WORKER_DATABASE_LOCK_TIMEOUT_SECONDS,
        "postgres_transaction_within_steady_budget": max(
            float(row["max_transaction_seconds"]) for row in postgres_samples
        )
        <= _MAX_TRANSACTION_SECONDS,
        "postgres_query_audit_passed": bool(query_audit["ok"]),
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
            0 <= float(_mapping(row, "resource_active", stage="worker_resource_active")[capability]) <= cap
            for row in telemetry_samples
            for capability, cap in _RESOURCE_CAPS.items()
        ),
        "resource_admission_observed_during_interval": resource_metrics["admission"]["count_delta"] > 0,
        "resource_service_observed_during_interval": resource_metrics["service"]["count_delta"] > 0,
        "frontier_capacity_converges": all(bool(row["passes"]) for row in capacity.values()),
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
            "max_lock_wait_seconds": max(float(row["max_lock_wait_seconds"]) for row in postgres_samples),
            "max_transaction_seconds": max(float(row["max_transaction_seconds"]) for row in postgres_samples),
            "temp_files_delta": temp_file_delta,
            "temp_bytes_delta": temp_byte_delta,
            "max_waits_by_type": max_waits_by_type,
            "query_audit": query_audit,
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
        "projection_soft_slo_overruns": soft_slo,
        "resource_metrics": resource_metrics,
        "capacity": capacity,
    }


def _query_audit_summary(audit: dict[str, Any]) -> dict[str, Any]:
    _validate_query_audit(audit)
    queries = audit["queries"]
    return {
        "ok": True,
        "analyze": True,
        "query_count": len(queries),
        "query_names": sorted(str(query["name"]) for query in queries),
        "route_gap_count": len(audit["route_coverage"]["missing_query_names"]),
        "violations": sorted({str(violation) for query in queries for violation in query["violations"]}),
        "artifact_sha256": hashlib.sha256(
            json.dumps(
                audit,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def _resource_metric_summary(
    first: Mapping[str, Any],
    last: Mapping[str, Any],
    *,
    key: str,
) -> dict[str, Any]:
    start_rows = _validate_resource_metric_rows(first.get(key), key=key)
    end_rows = _validate_resource_metric_rows(last.get(key), key=key)
    count_name = next(name for name in _RESOURCE_METRIC_NAMES[key] if name.endswith("_count"))
    sum_name = next(name for name in _RESOURCE_METRIC_NAMES[key] if name.endswith("_sum"))

    def total(rows: Mapping[tuple[str, tuple[tuple[str, str], ...]], float], name: str) -> float:
        return sum(value for (metric_name, _labels), value in rows.items() if metric_name == name)

    count_start = total(start_rows, count_name)
    count_end = total(end_rows, count_name)
    seconds_start = total(start_rows, sum_name)
    seconds_end = total(end_rows, sum_name)
    if count_end < count_start or seconds_end < seconds_start:
        raise _SampleFailure(f"worker_{key}_counter_regressed")
    return {
        "series_count_start": len(start_rows) // 2,
        "series_count_end": len(end_rows) // 2,
        "count_start": int(count_start),
        "count_delta": int(count_end - count_start),
        "count_end": int(count_end),
        "seconds_sum_start": seconds_start,
        "seconds_sum_delta": seconds_end - seconds_start,
        "seconds_sum_end": seconds_end,
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
    resource_active: dict[str, float] = {}
    deadline_misses: dict[str, float] = {domain: 0.0 for domain in _DOMAINS}
    soft_slo_overruns: dict[str, float] = {domain: 0.0 for domain in _DOMAINS}
    transitions: dict[str, dict[str, float]] = {domain: {"arrival": 0.0, "completion": 0.0} for domain in _DOMAINS}
    resource_service: list[dict[str, Any]] = []
    resource_admission: list[dict[str, Any]] = []
    metric_families: set[str] = set()
    try:
        families = text_string_to_metric_families(payload)
        for family in families:
            metric_families.add(str(family.name))
            for sample in family.samples:
                labels = dict(sample.labels)
                if sample.name == "tracefold_worker_resource_active":
                    resource_active[str(labels.get("capability") or "unknown")] = float(sample.value)
                elif sample.name == "tracefold_worker_projection_deadline_misses_total":
                    domain = str(labels.get("domain") or "unknown")
                    deadline_misses[domain] = deadline_misses.get(domain, 0.0) + float(sample.value)
                elif sample.name == "tracefold_worker_projection_soft_slo_overruns_total":
                    domain = str(labels.get("domain") or "unknown")
                    soft_slo_overruns[domain] = soft_slo_overruns.get(domain, 0.0) + float(sample.value)
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
        "metric_families": sorted(metric_families),
        "resource_active": resource_active,
        "projection_deadline_misses_total": deadline_misses,
        "projection_soft_slo_overruns_total": soft_slo_overruns,
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
                "collector_enabled": gmgn_stream_enabled(settings),
                "news_enabled": bool(settings.news.enabled),
                "macro_enabled": bool(settings.providers.macro_sources.enabled),
                "news_brief_openrouter_configured": bool(settings.llm.openrouter_api_key),
                "news_brief_groq_configured": bool(settings.llm.groq_api_key),
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
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise _SampleFailure(stage)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
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
    "collect_workers_runtime_acceptance",
    "validate_workers_runtime_collection",
]
