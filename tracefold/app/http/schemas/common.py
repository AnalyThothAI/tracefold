from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

JsonObject = dict[str, Any]


class ApiSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ExactApiSchema(ApiSchema):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ApiEnvelope[T](ExactApiSchema):
    ok: bool
    data: T | None = None
    error: str | None = None
    field: str | None = None


class BootstrapData(ExactApiSchema):
    ws_token: str


class StatusDatabaseData(ExactApiSchema):
    ok: bool
    schema_ok: bool
    current_revision: str | None
    expected_revision: str
    error_code: Literal["database_unavailable", "schema_mismatch"] | None


class WorkersCapabilityData(ExactApiSchema):
    """One Workers business capability, reported apart from whether the process itself is alive.

    `disabled` is an operator's configuration, `unavailable` is a capability that could not be
    constructed from it, and `faulted` is one whose program stopped. None of them make the process
    unready, and none of them switch off the healthy fact APIs beside them (#553 PR-3).
    """

    state: Literal["running", "faulted", "unavailable", "disabled"]
    reason: str | None


class WorkersRuntimeData(ExactApiSchema):
    runtime_id: str | None
    runtime_version: str | None
    state: Literal[
        "starting",
        "running",
        "stopping",
        "stopped",
        "failed",
        "stale",
        "unavailable",
    ]
    started_at_ms: int | None
    heartbeat_at_ms: int | None
    heartbeat_stale_after_ms: Literal[15000]
    fatal_code: (
        Literal[
            "startup_failed",
            "child_failed",
            "control_failed",
            "singleton_lost",
            "runtime_invariant_failed",
            "resource_operation_overrun",
            "graceful_deadline_exceeded",
            "cleanup_failed",
        ]
        | None
    )
    unavailable_reason: (
        Literal[
            "runtime_status_query_failed",
            "runtime_missing",
            "runtime_heartbeat_stale",
            "runtime_starting",
            "runtime_stopping",
            "runtime_stopped",
            "runtime_failed",
        ]
        | None
    )
    # Keyed by capability name rather than by a closed Literal: a new optional Workers loop reports
    # here without a public contract change, and an absent key means "this runtime published none".
    capabilities: dict[str, WorkersCapabilityData]


class ServeRuntimeData(ExactApiSchema):
    runtime_id: str
    runtime_revision: str
    image_digest: str
    started_at_ms: int


class StatusRuntimeData(ExactApiSchema):
    ok: bool
    reasons: list[
        Literal[
            "database_unavailable",
            "database_schema_mismatch",
            "runtime_status_query_failed",
            "runtime_missing",
            "runtime_heartbeat_stale",
            "runtime_starting",
            "runtime_stopping",
            "runtime_stopped",
            "runtime_failed",
        ]
    ]
    db: StatusDatabaseData
    serve_runtime: ServeRuntimeData
    workers_runtime: WorkersRuntimeData


class StatusData(ExactApiSchema):
    measured_at_ms: int
    runtime: StatusRuntimeData


class ReadinessData(ExactApiSchema):
    ok: bool
    reasons: list[str]
    store: Literal["postgresql"]
    db: JsonObject
    composition: JsonObject
