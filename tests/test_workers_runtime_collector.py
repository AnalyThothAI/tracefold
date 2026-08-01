from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from psycopg import conninfo

from tracefold.app.workers_runtime import WORKERS_RUNTIME_VERSION
from tracefold.app.workers_runtime_collector import (
    _LOOPBACK_PROXY,
    COLLECTION_FILE,
    COLLECTION_SCHEMA_VERSION,
    EXPECTED_SAMPLE_COUNT,
    SAMPLES_FILE,
    _collect_fixed_interval,
    _CollectorDependencies,
    _dsn_for_compose_endpoint,
    _parse_worker_metrics,
    _SampleFailure,
    _summarize,
    _validate_sample,
    validate_workers_runtime_collection,
)
from tracefold.news import NEWS_STORY_PUBLISH_TIMEOUT_SECONDS
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.postgres_audit import (
    HOT_QUERIES,
    PUBLIC_NO_SQL_ROUTES,
    PUBLIC_ROUTE_QUERY_COVERAGE,
)
from tracefold.platform.postgres.projection_frontier import FRONTIER_SPECS

_FRONTIER_DOMAINS = tuple(spec.domain for spec in FRONTIER_SPECS)


def test_runtime_and_frontier_contract_follow_news_hard_cut() -> None:
    assert WORKERS_RUNTIME_VERSION == "2"
    assert _FRONTIER_DOMAINS == ("radar", "profile", "macro")


def test_loopback_probe_never_uses_operator_system_proxy() -> None:
    assert _LOOPBACK_PROXY.proxies == {}


class _VirtualClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.base_ms = 1_800_000_000_000

    def monotonic(self) -> float:
        return self.seconds

    def clock_ms(self) -> int:
        return self.base_ms + int(self.seconds * 1_000)

    def sleep(self, seconds: float) -> None:
        self.seconds += max(0.0, float(seconds))


def test_fixed_production_interval_collects_1800_seconds_without_real_wait(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    clock = _VirtualClock()
    metadata = _metadata()

    result = _collect_fixed_interval(
        bundle,
        metadata=metadata,
        dependencies=_CollectorDependencies(
            clock_ms=clock.clock_ms,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            read_sample=lambda sequence: _sample(sequence, clock=clock),
        ),
    )

    assert result["status"] == "passed"
    assert result["sample_count"] == EXPECTED_SAMPLE_COUNT == 181
    assert result["end_at_ms"] - result["start_at_ms"] == 1_800_000
    assert result["summary"]["max_sample_gap_ms"] == 10_000
    assert result["summary"]["all_checks_passed"] is True
    assert clock.seconds == 1_800.0
    assert len((bundle / SAMPLES_FILE).read_text().splitlines()) == EXPECTED_SAMPLE_COUNT
    assert json.loads((bundle / COLLECTION_FILE).read_text())["status"] == "passed"


def test_collection_validator_recomputes_summary_from_all_jsonl_samples(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    clock = _VirtualClock()
    metadata = _metadata()
    result = _collect_fixed_interval(
        bundle,
        metadata=metadata,
        dependencies=_CollectorDependencies(
            clock_ms=clock.clock_ms,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            read_sample=lambda sequence: _sample(sequence, clock=clock),
        ),
    )

    assert validate_workers_runtime_collection(bundle, expected_metadata=metadata) == result["summary"]


def test_collection_validator_rejects_claimed_summary_mutation(tmp_path: Path) -> None:
    bundle = _complete_collection(tmp_path)
    collection_path = bundle / COLLECTION_FILE
    collection = json.loads(collection_path.read_text())
    collection["summary"]["process_resources"]["max_process_rss_bytes"] += 1
    collection_path.write_text(json.dumps(collection))

    with pytest.raises(ValueError, match="workers_runtime_collection_summary_mismatch"):
        validate_workers_runtime_collection(bundle, expected_metadata=_metadata())


def test_collection_validator_rejects_jsonl_mutation(tmp_path: Path) -> None:
    bundle = _complete_collection(tmp_path)
    samples_path = bundle / SAMPLES_FILE
    lines = samples_path.read_text().splitlines()
    sample = json.loads(lines[90])
    sample["container"]["process_rss_bytes"] += 1
    lines[90] = json.dumps(sample)
    samples_path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ValueError, match="workers_runtime_collection_samples_hash_mismatch"):
        validate_workers_runtime_collection(bundle, expected_metadata=_metadata())


def test_fixed_interval_duration_uses_monotonic_elapsed_not_wall_clock_boundary(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    clock = _VirtualClock()

    def read_sample(sequence: int) -> dict:
        sample = _sample(sequence, clock=clock)
        sample["at_ms"] = clock.base_ms + sequence * 10_000 + (100 if sequence == 0 else 0)
        sample["probe"]["heartbeat_at_ms"] = sample["at_ms"]
        return sample

    result = _collect_fixed_interval(
        bundle,
        metadata=_metadata(),
        dependencies=_CollectorDependencies(
            clock_ms=clock.clock_ms,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            read_sample=read_sample,
        ),
    )

    assert result["end_at_ms"] - result["start_at_ms"] == 1_799_900
    assert result["summary"]["duration_seconds"] == 1_800.0
    assert result["status"] == "passed"


def test_sample_failure_stops_immediately_and_preserves_raw_failure(tmp_path: Path) -> None:
    bundle = tmp_path / "failed-bundle"
    bundle.mkdir()
    clock = _VirtualClock()
    calls: list[int] = []

    def read_sample(sequence: int) -> dict:
        calls.append(sequence)
        sample = _sample(sequence, clock=clock)
        if sequence == 5:
            sample["probe"]["ready"] = False
        return sample

    result = _collect_fixed_interval(
        bundle,
        metadata=_metadata(),
        dependencies=_CollectorDependencies(
            clock_ms=clock.clock_ms,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            read_sample=read_sample,
        ),
    )

    assert result["status"] == "failed"
    assert result["sample_count"] == 5
    assert result["failure"] == {"stage": "worker_not_ready", "cause_type": None}
    assert calls == list(range(6))
    raw = [json.loads(line) for line in (bundle / SAMPLES_FILE).read_text().splitlines()]
    assert len(raw) == 6
    assert raw[-1]["status"] == "failed"
    assert raw[-1]["failure"]["stage"] == "worker_not_ready"


def test_production_dsn_uses_explicit_compose_loopback_port() -> None:
    rewritten = _dsn_for_compose_endpoint(
        "postgresql://tracefold_workers:secret@postgres:5432/tracefold",
        "0.0.0.0:56532",
    )

    parsed = conninfo.conninfo_to_dict(rewritten)
    assert parsed["host"] == "127.0.0.1"
    assert parsed["port"] == "56532"
    assert parsed["user"] == "tracefold_workers"
    assert parsed["password"] == "secret"


def test_prometheus_parser_preserves_required_families_and_separate_resource_evidence() -> None:
    telemetry = TelemetryRegistry()
    telemetry.record_resource_admission(
        "database_control",
        "runtime_heartbeat",
        "accepted",
        0.001,
    )
    telemetry.record_resource_service(
        "database_control",
        "runtime_heartbeat",
        "success",
        0.002,
    )

    parsed = _parse_worker_metrics(telemetry.render_prometheus_text())

    assert set(parsed["resource_active"]) == {
        "database_business",
        "database_control",
        "finite_operation",
        "model_adapter",
        "cpu_process",
    }
    assert "tracefold_worker_projection_soft_slo_overruns" in parsed["metric_families"]
    assert parsed["resource_admission"][0]["labels"]["outcome"] == "accepted"
    assert parsed["resource_service"][0]["labels"]["outcome"] == "success"


def test_collector_elapsed_gap_over_15_seconds_fails_closed() -> None:
    clock = _VirtualClock()
    previous = _sample(0, clock=clock)
    clock.seconds = 16.0
    current = _sample(1, clock=clock)

    with pytest.raises(_SampleFailure, match="collector_elapsed_gap"):
        _validate_sample(current, metadata=_metadata(), previous=previous)


def test_wall_clock_sample_gap_over_15_seconds_fails_closed() -> None:
    clock = _VirtualClock()
    previous = _sample(0, clock=clock)
    clock.seconds = 16.0
    current = _sample(1, clock=clock)
    current["collector_elapsed_seconds"] = 10.0

    with pytest.raises(_SampleFailure, match="sample_gap"):
        _validate_sample(current, metadata=_metadata(), previous=previous)


@pytest.mark.parametrize(
    ("path", "value", "failed_check"),
    [
        (("probe", "process_id"), 124, "process_identity_constant"),
        (("probe", "runtime_id"), "runtime-2", "runtime_identity_constant"),
        (("probe", "runtime_revision"), "b" * 40, "runtime_revision_constant"),
        (("container", "container_id"), "container-2", "container_identity_constant"),
        (("container", "restart_count"), 1, "restart_delta_zero"),
    ],
)
def test_runtime_identity_or_restart_change_fails_summary(path, value, failed_check) -> None:
    samples = _samples()
    _set_path(samples[-1], path, value)

    summary = _summarize(samples)

    assert summary["all_checks_passed"] is False
    assert failed_check in summary["failed_checks"]


def test_projection_transition_counter_regression_fails_closed() -> None:
    samples = _samples()
    domain = _FRONTIER_DOMAINS[0]
    samples[0]["telemetry"]["projection_transitions_total"][domain]["arrival"] = 2.0
    samples[-1]["telemetry"]["projection_transitions_total"][domain]["arrival"] = 1.0

    with pytest.raises(_SampleFailure, match=f"projection_transition_counter_regressed:{domain}"):
        _summarize(samples)


def test_required_metric_family_missing_fails_closed() -> None:
    clock = _VirtualClock()
    sample = _sample(0, clock=clock)
    sample["telemetry"]["metric_families"].remove("tracefold_worker_resource_service_seconds")

    with pytest.raises(_SampleFailure, match="worker_metric_family_missing"):
        _validate_sample(sample, metadata=_metadata(), previous=None)


@pytest.mark.parametrize("key", ("resource_admission", "resource_service"))
def test_empty_resource_metric_evidence_fails_closed(key: str) -> None:
    clock = _VirtualClock()
    sample = _sample(0, clock=clock)
    sample["telemetry"][key] = []

    with pytest.raises(_SampleFailure, match=f"worker_{key}_required"):
        _validate_sample(sample, metadata=_metadata(), previous=None)


@pytest.mark.parametrize(
    ("path", "stage"),
    [
        (("projection_deadline_misses_total", "radar"), "projection_deadline_counter_regressed:radar"),
        (("projection_soft_slo_overruns_total", "radar"), "projection_soft_slo_counter_regressed:radar"),
        (("projection_transitions_total", "radar", "completion"), "projection_transition_counter_regressed:radar"),
    ],
)
def test_cumulative_projection_metric_regression_fails_closed(path, stage) -> None:
    clock = _VirtualClock()
    previous = _sample(0, clock=clock)
    _set_path(previous["telemetry"], path, 1.0)
    clock.seconds = 10.0
    current = _sample(1, clock=clock)

    with pytest.raises(_SampleFailure, match=stage):
        _validate_sample(current, metadata=_metadata(), previous=previous)


def test_resource_metric_regression_fails_closed() -> None:
    clock = _VirtualClock()
    previous = _sample(0, clock=clock)
    previous["telemetry"]["resource_service"] = _resource_rows("service", count=2, seconds=0.2)
    clock.seconds = 10.0
    current = _sample(1, clock=clock)

    with pytest.raises(_SampleFailure, match="worker_resource_service_counter_regressed"):
        _validate_sample(current, metadata=_metadata(), previous=previous)


def test_first_sample_requires_passing_analyzed_query_audit() -> None:
    clock = _VirtualClock()
    missing = _sample(0, clock=clock)
    del missing["postgres"]["query_audit"]
    with pytest.raises(_SampleFailure, match="postgres_query_audit"):
        _validate_sample(missing, metadata=_metadata(), previous=None)

    failed = _sample(0, clock=clock)
    failed["postgres"]["query_audit"]["queries"][0]["violations"] = ["temp_spill"]
    failed["postgres"]["query_audit"]["queries"][0]["ok"] = False
    with pytest.raises(_SampleFailure, match="postgres_query_audit_violation"):
        _validate_sample(failed, metadata=_metadata(), previous=None)


def test_postgres_wait_distribution_must_cover_every_worker_connection() -> None:
    clock = _VirtualClock()
    sample = _sample(0, clock=clock)
    sample["postgres"]["waits_by_type"] = {"Client": 3}

    with pytest.raises(_SampleFailure, match="postgres_wait_count_mismatch"):
        _validate_sample(sample, metadata=_metadata(), previous=None)


@pytest.mark.parametrize(
    ("path", "value", "stage"),
    [
        (("container", "restart_count"), -1, "workers_restart_count"),
        (("container", "process_rss_bytes"), float("nan"), "workers_process_rss"),
        (("postgres", "max_transaction_seconds"), float("inf"), "postgres_transaction_duration"),
        (
            ("telemetry", "resource_active", "cpu_process"),
            float("nan"),
            "worker_resource_active:cpu_process",
        ),
    ],
)
def test_invalid_or_nonfinite_raw_numbers_fail_closed(path, value, stage) -> None:
    clock = _VirtualClock()
    sample = _sample(0, clock=clock)
    _set_path(sample, path, value)

    with pytest.raises(_SampleFailure, match=stage):
        _validate_sample(sample, metadata=_metadata(), previous=None)


def test_summary_binds_waits_query_audit_resource_metrics_and_soft_slo() -> None:
    summary = _summarize(_samples())

    assert summary["postgres"]["max_waits_by_type"] == {"Client": 3, "none": 1}
    assert summary["postgres"]["query_audit"]["ok"] is True
    assert summary["postgres"]["query_audit"]["query_count"] == len(HOT_QUERIES)
    assert summary["resource_metrics"]["admission"]["count_delta"] == 180
    assert summary["resource_metrics"]["service"]["count_delta"] == 180
    assert summary["projection_soft_slo_overruns"]["radar"]["counter_delta"] == 0.0


def test_collection_rejects_negative_restart_after_all_hashes_and_summary_are_refreshed(tmp_path: Path) -> None:
    bundle = _complete_collection(tmp_path)
    samples_path = bundle / SAMPLES_FILE
    samples = [json.loads(line) for line in samples_path.read_text().splitlines()]
    for sample in samples:
        sample["container"]["restart_count"] = -1
    samples_path.write_text("".join(json.dumps(sample, sort_keys=True) + "\n" for sample in samples))

    collection_path = bundle / COLLECTION_FILE
    collection = json.loads(collection_path.read_text())
    collection["samples_sha256"] = hashlib.sha256(samples_path.read_bytes()).hexdigest()
    collection["summary"] = _summarize(samples)
    collection_path.write_text(json.dumps(collection))

    with pytest.raises(
        ValueError,
        match="workers_runtime_collection_sample_invalid:workers_restart_count",
    ):
        validate_workers_runtime_collection(bundle, expected_metadata=_metadata())


@pytest.mark.parametrize(
    ("path", "value", "stage"),
    [
        (("container", "container_memory_bytes"), 2 * 1024 * 1024 * 1024, "workers_container_memory_limit"),
        (("postgres", "worker_connections"), 5, "postgres_worker_connection_limit"),
        (("postgres", "lock_wait_count"), 1, "postgres_lock_wait"),
        (
            ("postgres", "max_transaction_seconds"),
            NEWS_STORY_PUBLISH_TIMEOUT_SECONDS + 0.1,
            "postgres_transaction_duration",
        ),
        (("telemetry", "resource_active", "finite_operation"), 4.0, "worker_resource_active_limit"),
    ],
)
def test_resource_or_postgres_cap_violation_fails_sample(path, value, stage) -> None:
    clock = _VirtualClock()
    sample = _sample(0, clock=clock)
    _set_path(sample, path, value)

    with pytest.raises(_SampleFailure, match=stage):
        _validate_sample(sample, metadata=_metadata(), previous=None)


def test_nonconverging_nonempty_domain_backlog_fails_summary() -> None:
    samples = _samples()
    domain = _FRONTIER_DOMAINS[0]
    start = samples[0]["postgres"]["frontiers"][domain]
    end = samples[-1]["postgres"]["frontiers"][domain]
    start.update({"actionable_count": 10, "oldest_age_ms": 1_000})
    end.update({"actionable_count": 10, "oldest_age_ms": 2_000})
    end["counts_by_status"] = {"dirty": 10}
    samples[-1]["telemetry"]["projection_transitions_total"][domain].update({"arrival": 5.0, "completion": 5.0})

    summary = _summarize(samples)

    assert summary["capacity"][domain]["passes"] is False
    assert "frontier_capacity_converges" in summary["failed_checks"]


def test_database_wide_temp_growth_is_recorded_without_blame() -> None:
    samples = _samples()
    samples[-1]["postgres"]["temp_files"] += 1
    samples[-1]["postgres"]["temp_bytes"] += 4096

    summary = _summarize(samples)

    assert summary["all_checks_passed"] is True
    assert summary["postgres"]["temp_files_delta"] == 1
    assert summary["postgres"]["temp_bytes_delta"] == 4096


def _samples() -> list[dict]:
    clock = _VirtualClock()
    samples = []
    for sequence in range(EXPECTED_SAMPLE_COUNT):
        clock.seconds = float(sequence * 10)
        samples.append(_sample(sequence, clock=clock))
    return samples


def _complete_collection(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    clock = _VirtualClock()
    _collect_fixed_interval(
        bundle,
        metadata=_metadata(),
        dependencies=_CollectorDependencies(
            clock_ms=clock.clock_ms,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            read_sample=lambda sequence: _sample(sequence, clock=clock),
        ),
    )
    return bundle


def _set_path(payload: dict, path: tuple[str, ...], value: object) -> None:
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def _metadata() -> dict:
    return {
        "source": {
            "repository": "AnalyThothAI/tracefold",
            "session": "virtual-runtime-acceptance",
            "cutoff_at_ms": 1_800_000_000_000,
        },
        "versions": {
            "commit_sha": "a" * 40,
            "migration_version": "0233",
        },
        "configuration": {
            "config_path": "/operator/config.yaml",
            "redacted_enablement": {"news_enabled": True},
        },
    }


def _sample(sequence: int, *, clock: _VirtualClock) -> dict:
    at_ms = clock.clock_ms()
    frontiers = {
        domain: {
            "actionable_count": 0,
            "oldest_age_ms": 0,
            "unresolved_deadline_misses": 0,
            "unresolved_quarantine": 0,
            "counts_by_status": {},
        }
        for domain in _FRONTIER_DOMAINS
    }
    transitions = {domain: {"arrival": 0.0, "completion": 0.0} for domain in _FRONTIER_DOMAINS}
    return {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "sequence": sequence,
        "scheduled_offset_seconds": sequence * 10,
        "collector_elapsed_seconds": clock.seconds,
        "at_ms": at_ms,
        "status": "passed",
        "checkout": {"commit_sha": "a" * 40, "clean": True},
        "probe": {
            "ok": True,
            "ready": True,
            "runtime_id": "runtime-1",
            "runtime_version": WORKERS_RUNTIME_VERSION,
            "runtime_revision": "a" * 40,
            "process_id": 123,
            "lifecycle_state": "running",
            "heartbeat_at_ms": at_ms,
            "heartbeat_stale_after_ms": 15_000,
            "unavailable_reason": None,
            "probe_rtt_ms": 1.0,
        },
        "container": {
            "container_id": "container-1",
            "image_id": "image-1",
            "image_revision": "a" * 40,
            "restart_count": 0,
            "running": True,
            "oom_killed": False,
            "host_process_id": 456,
            "process_rss_bytes": 256 * 1024 * 1024,
            "container_memory_bytes": 512 * 1024 * 1024,
        },
        "postgres": {
            "worker_connections": 4,
            "lock_wait_count": 0,
            "waits_by_type": {"Client": 3, "none": 1},
            "max_transaction_seconds": 0.1,
            "temp_files": 7,
            "temp_bytes": 4096,
            "frontiers": frontiers,
            **({"query_audit": _query_audit()} if sequence == 0 else {}),
        },
        "telemetry": {
            "metric_families": sorted(
                {
                    "tracefold_worker_projection_deadline_misses",
                    "tracefold_worker_projection_soft_slo_overruns",
                    "tracefold_worker_projection_transitions",
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
            "projection_deadline_misses_total": {domain: 0.0 for domain in _FRONTIER_DOMAINS},
            "projection_soft_slo_overruns_total": {domain: 0.0 for domain in _FRONTIER_DOMAINS},
            "projection_transitions_total": transitions,
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
