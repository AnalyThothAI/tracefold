"""Platform-owned process identity, liveness and diagnostic detail.

Every mutation is fenced by the instance UUID. Business ledgers contain no process heartbeats.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from .audit import INDEXED_ROW_SCAN_BUDGET, ReadQuerySpec

ProcessKind = Literal["workers", "analysis", "executor"]
LifecycleState = Literal["starting", "running", "stopping", "stopped", "failed"]

ANALYSIS_RUNTIME_SQL = """
SELECT process_key AS runtime_id,lifecycle_state,heartbeat_at_ms,d.active_policy,d.program_sha,d.model_name,
       d.model_configured,d.publish_signals,d.config_digest,fault_code
  FROM runtime_processes CROSS JOIN LATERAL jsonb_to_record(detail) AS d(
    active_policy text,program_sha text,model_name text,model_configured boolean,
    publish_signals boolean,config_digest text)
 WHERE process_kind='analysis' AND process_key=%s
"""
_PROCESS_COLUMNS = """process_kind,process_key,instance_id,lifecycle_state,started_at_ms,heartbeat_at_ms,
                      fatal_code,fault_code,runtime_version,runtime_revision,image_digest,detail"""


class AnalysisProcessDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    active_policy: str
    program_sha: str
    model_name: str | None
    model_configured: bool
    publish_signals: bool
    config_digest: str


class RuntimeProcesses:
    def __init__(self, conn: Any, *, kind: ProcessKind = "workers", key: str = "singleton") -> None:
        self.conn, self.kind, self.key = conn, kind, key
        if not key:
            raise ValueError("runtime_process_key_required")

    def process(self, *, kind: ProcessKind, key: str) -> RuntimeProcesses:
        return RuntimeProcesses(self.conn, kind=kind, key=key)

    def begin(
        self,
        *,
        instance_id: str,
        started_at_ms: int,
        now_ms: int,
        runtime_version: str | None = None,
        runtime_revision: str | None = None,
        image_digest: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> bool:
        stale_after_ms = 5_000 if self.kind == "executor" else 15_000
        if self.kind == "workers" and not all(
            str(value or "").strip() for value in (runtime_version, runtime_revision, image_digest)
        ):
            raise ValueError("workers_runtime_identity_required")
        row = self.conn.execute(
            """INSERT INTO runtime_processes(process_kind,process_key,instance_id,lifecycle_state,
                 started_at_ms,heartbeat_at_ms,runtime_version,runtime_revision,image_digest,detail)
               VALUES (%s,%s,%s,'starting',%s,%s,%s,%s,%s,%s)
               ON CONFLICT(process_kind,process_key) DO UPDATE SET
                 instance_id=EXCLUDED.instance_id,lifecycle_state='starting',started_at_ms=EXCLUDED.started_at_ms,
                 heartbeat_at_ms=EXCLUDED.heartbeat_at_ms,fatal_code=NULL,fault_code=NULL,
                 runtime_version=EXCLUDED.runtime_version,runtime_revision=EXCLUDED.runtime_revision,
                 image_digest=EXCLUDED.image_digest,detail=EXCLUDED.detail
               WHERE runtime_processes.lifecycle_state IN ('stopped','failed')
                  OR runtime_processes.heartbeat_at_ms < %s
               RETURNING instance_id""",
            (
                self.kind,
                self.key,
                UUID(instance_id),
                started_at_ms,
                now_ms,
                runtime_version,
                runtime_revision,
                image_digest,
                Jsonb(dict(detail or {})),
                now_ms - stale_after_ms,
            ),
        ).fetchone()
        return row is not None

    def _require_owner(self, cursor: Any) -> None:
        if cursor.rowcount != 1:
            raise RuntimeError("runtime_process_identity_lost")

    def transition(
        self, *, instance_id: str, lifecycle_state: LifecycleState, now_ms: int, fatal_code: str | None = None
    ) -> None:
        if (lifecycle_state == "failed") != (fatal_code is not None):
            raise ValueError("runtime_process_fatal_pair")
        self._require_owner(
            self.conn.execute(
                """UPDATE runtime_processes SET lifecycle_state=%s,heartbeat_at_ms=GREATEST(heartbeat_at_ms,%s),
                 fatal_code=%s WHERE process_kind=%s AND process_key=%s AND instance_id=%s""",
                (lifecycle_state, now_ms, fatal_code, self.kind, self.key, UUID(instance_id)),
            )
        )

    def set_detail(self, *, instance_id: str, detail: Mapping[str, Any]) -> None:
        self._require_owner(
            self.conn.execute(
                "UPDATE runtime_processes SET detail=%s WHERE process_kind=%s AND process_key=%s AND instance_id=%s",
                (Jsonb(dict(detail)), self.kind, self.key, UUID(instance_id)),
            )
        )

    def heartbeat(
        self, *, instance_id: str, now_ms: int, fault_code: str | None = None, detail: Mapping[str, Any] | None = None
    ) -> None:
        self._require_owner(
            self.conn.execute(
                """UPDATE runtime_processes SET heartbeat_at_ms=GREATEST(heartbeat_at_ms,%s),fault_code=%s,
                 detail=COALESCE(%s,detail)
               WHERE process_kind=%s AND process_key=%s AND instance_id=%s""",
                (
                    now_ms,
                    fault_code,
                    None if detail is None else Jsonb(dict(detail)),
                    self.kind,
                    self.key,
                    UUID(instance_id),
                ),
            )
        )

    def read(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT {_PROCESS_COLUMNS} FROM runtime_processes WHERE process_kind=%s AND process_key=%s",  # noqa: S608
            (self.kind, self.key),
        ).fetchone()
        return None if row is None else dict(row)

    def is_running(self, *, now_ms: int, stale_after_ms: int = 5_000) -> bool:
        row = self.read()
        return bool(
            row
            and row["lifecycle_state"] == "running"
            and row["fault_code"] is None
            and now_ms - row["heartbeat_at_ms"] <= stale_after_ms
        )

    def analysis_detail(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(ANALYSIS_RUNTIME_SQL, (key,)).fetchone()
        return None if row is None else dict(row)

    def workers_row(self) -> dict[str, Any] | None:
        query = workers_runtime_read_query()
        row = self.conn.execute(query.sql, query.params).fetchone()
        return None if row is None else dict(row)


def workers_runtime_read_query() -> ReadQuerySpec:
    return ReadQuerySpec(
        name="workers_runtime",
        sql="""SELECT instance_id::text AS runtime_id,runtime_version,lifecycle_state,started_at_ms,
                      heartbeat_at_ms,fatal_code,runtime_revision,image_digest,detail->'capabilities' AS capabilities
                 FROM runtime_processes WHERE process_kind='workers' AND process_key='singleton'""",
        max_read_return_amplification=4.0,
        max_scanned_rows=INDEXED_ROW_SCAN_BUDGET,
    )
