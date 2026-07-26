from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, cast

from psycopg.types.json import Jsonb

from tracefold.macro.research.service import (
    MACRO_RESEARCH_MAX_PRIOR_PUBLICATIONS_PER_PAGE,
    MACRO_RESEARCH_MAX_READ_REFS,
    FrozenMacroEvidenceScope,
    MacroEvidenceCatalog,
    MacroEvidenceRecord,
    MacroObservationQuery,
    MacroPriorResearch,
    require_evidence_in_scope,
    require_prior_research_in_scope,
    resolve_observation_visibility,
)


class MacroResearchRepository:
    """PostgreSQL adapter for one immutable research publication per session."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def publication_exists(self, session_date: date) -> bool:
        row = self.conn.execute(
            "SELECT 1 AS present FROM macro_research_publications WHERE session_date = %s",
            (session_date,),
        ).fetchone()
        return row is not None

    def retry_failed_run(
        self,
        *,
        session_date: date,
        now_ms: int,
    ) -> dict[str, Any]:
        """Atomically grant at most one additional attempt to one failed run."""
        row = self.conn.execute(
            """
            WITH target AS MATERIALIZED (
              SELECT
                runs.*,
                EXISTS (
                  SELECT 1
                  FROM macro_research_publications AS publications
                  WHERE publications.session_date = runs.session_date
                ) AS publication_exists
              FROM macro_research_runs AS runs
              WHERE runs.session_date = %s
              FOR UPDATE
            ),
            updated AS (
              UPDATE macro_research_runs AS runs
              SET status = 'retryable',
                  max_attempts = GREATEST(runs.max_attempts, runs.attempt_count + 1),
                  due_at_ms = %s,
                  leased_until_ms = NULL,
                  lease_owner = NULL,
                  last_error_code = NULL,
                  last_error_message = NULL,
                  updated_at_ms = %s
              FROM target
              WHERE runs.session_date = target.session_date
                AND target.status = 'failed'
                AND NOT target.publication_exists
              RETURNING
                TRUE AS applied,
                'retry_granted'::text AS reason,
                runs.session_date,
                target.status AS previous_status,
                runs.status,
                runs.attempt_count,
                target.max_attempts AS previous_max_attempts,
                runs.max_attempts,
                runs.due_at_ms,
                runs.leased_until_ms,
                runs.lease_owner,
                runs.last_error_code,
                runs.last_error_message
            )
            SELECT * FROM updated
            UNION ALL
            SELECT
              FALSE AS applied,
              CASE
                WHEN target.publication_exists THEN 'publication_exists'
                ELSE 'run_not_failed'
              END AS reason,
              target.session_date,
              target.status AS previous_status,
              target.status AS status,
              target.attempt_count,
              target.max_attempts AS previous_max_attempts,
              target.max_attempts,
              target.due_at_ms,
              target.leased_until_ms,
              target.lease_owner,
              target.last_error_code,
              target.last_error_message
            FROM target
            WHERE NOT EXISTS (SELECT 1 FROM updated)
            """,
            (session_date, int(now_ms), int(now_ms)),
        ).fetchone()
        if row is not None:
            return dict(row)
        return {
            "applied": False,
            "reason": "run_not_found",
            "session_date": session_date,
            "previous_status": None,
            "status": None,
            "attempt_count": None,
            "previous_max_attempts": None,
            "max_attempts": None,
            "due_at_ms": None,
            "leased_until_ms": None,
            "lease_owner": None,
            "last_error_code": None,
            "last_error_message": None,
        }

    def latest_run_session(self) -> date | None:
        row = self.conn.execute(
            "SELECT session_date FROM macro_research_runs ORDER BY session_date DESC LIMIT 1"
        ).fetchone()
        return row["session_date"] if row is not None else None

    def scheduling_state(self, *, through_date: date) -> dict[str, date | None]:
        row = self.conn.execute(
            """
            SELECT
              (
                SELECT session_date
                FROM macro_research_runs
                WHERE session_date <= %s
                  AND status IN ('pending', 'running', 'retryable')
                ORDER BY session_date ASC
                LIMIT 1
              ) AS open_session,
              (
                SELECT MAX(session_date)
                FROM macro_research_runs
              ) AS latest_session
            """,
            (through_date,),
        ).fetchone()
        payload = dict(row or {})
        return {
            "open_session": payload.get("open_session"),
            "latest_session": payload.get("latest_session"),
        }

    def ensure_run(
        self,
        *,
        session_date: date,
        market_cutoff_ms: int,
        sealed_at_ms: int,
        max_attempts: int,
        due_at_ms: int,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            INSERT INTO macro_research_runs(
              session_date,
              market_cutoff_ms,
              status,
              sealed_at_ms,
              max_attempts,
              due_at_ms,
              created_at_ms,
              updated_at_ms
            )
            VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s)
            ON CONFLICT(session_date) DO NOTHING
            RETURNING session_date
            """,
            (
                session_date,
                int(market_cutoff_ms),
                int(sealed_at_ms),
                int(max_attempts),
                int(due_at_ms),
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        return row is not None

    def claim_run(
        self,
        *,
        session_date: date,
        lease_owner: str,
        lease_ms: int,
        now_ms: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            WITH expired_terminal AS (
              UPDATE macro_research_runs
              SET status = 'failed',
                  leased_until_ms = NULL,
                  lease_owner = NULL,
                  last_error_code = 'macro_research_lease_expired_attempt_budget_exhausted',
                  last_error_message = 'macro research lease expired after max attempts',
                  updated_at_ms = %s
              WHERE session_date = %s
                AND status = 'running'
                AND leased_until_ms <= %s
                AND attempt_count >= max_attempts
              RETURNING session_date
            ),
            candidate AS (
              SELECT session_date
              FROM macro_research_runs
              WHERE session_date = %s
                AND (
                  (
                    status IN ('pending', 'retryable')
                    AND due_at_ms <= %s
                  )
                  OR (
                    status = 'running'
                    AND leased_until_ms <= %s
                  )
                )
                AND attempt_count < max_attempts
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE macro_research_runs AS runs
            SET status = 'running',
                attempt_count = runs.attempt_count + 1,
                leased_until_ms = %s,
                lease_owner = %s,
                last_error_code = NULL,
                last_error_message = NULL,
                updated_at_ms = %s
            FROM candidate
            WHERE runs.session_date = candidate.session_date
            RETURNING runs.*
            """,
            (
                int(now_ms),
                session_date,
                int(now_ms),
                session_date,
                int(now_ms),
                int(now_ms),
                int(now_ms) + int(lease_ms),
                _required_text(lease_owner, "lease_owner"),
                int(now_ms),
            ),
        ).fetchone()
        return dict(row) if row is not None else None

    def renew_run_lease(
        self,
        *,
        session_date: date,
        lease_owner: str,
        lease_ms: int,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE macro_research_runs
            SET leased_until_ms = GREATEST(leased_until_ms, %s),
                updated_at_ms = %s
            WHERE session_date = %s
              AND status = 'running'
              AND lease_owner = %s
            RETURNING session_date
            """,
            (
                int(now_ms) + int(lease_ms),
                int(now_ms),
                session_date,
                _required_text(lease_owner, "lease_owner"),
            ),
        ).fetchone()
        return row is not None

    def mark_run_error(
        self,
        *,
        session_date: date,
        lease_owner: str,
        error_code: str,
        error_message: str,
        retry_ms: int,
        now_ms: int,
    ) -> str:
        row = self.conn.execute(
            """
            UPDATE macro_research_runs
            SET status = CASE
                  WHEN attempt_count >= max_attempts THEN 'failed'
                  ELSE 'retryable'
                END,
                due_at_ms = CASE
                  WHEN attempt_count >= max_attempts THEN due_at_ms
                  ELSE %s
                END,
                leased_until_ms = NULL,
                lease_owner = NULL,
                last_error_code = %s,
                last_error_message = %s,
                updated_at_ms = %s
            WHERE session_date = %s
              AND status = 'running'
              AND lease_owner = %s
            RETURNING status
            """,
            (
                int(now_ms) + int(retry_ms),
                _safe_error_code(error_code),
                _safe_error_message(error_message),
                int(now_ms),
                session_date,
                _required_text(lease_owner, "lease_owner"),
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("macro_research_run_error_owner_mismatch")
        return str(row["status"])

    def publish(
        self,
        *,
        session_date: date,
        lease_owner: str,
        artifact: Mapping[str, Any],
        report_markdown: str,
        audit: Mapping[str, Any],
        model_name: str,
        prompt_version: str,
        workflow_version: str,
        artifact_hash: str,
        now_ms: int,
    ) -> bool:
        inserted = self.conn.execute(
            """
            INSERT INTO macro_research_publications(
              session_date,
              market_cutoff_ms,
              artifact_json,
              report_markdown,
              audit_json,
              model_name,
              prompt_version,
              workflow_version,
              artifact_hash,
              published_at_ms
            )
            SELECT
              runs.session_date,
              runs.market_cutoff_ms,
              %s,
              %s,
              %s,
              %s,
              %s,
              %s,
              %s,
              %s
            FROM macro_research_runs AS runs
            WHERE runs.session_date = %s
              AND runs.status = 'running'
              AND runs.lease_owner = %s
            ON CONFLICT(session_date) DO NOTHING
            RETURNING session_date
            """,
            (
                Jsonb(dict(artifact)),
                _required_text(report_markdown, "report_markdown"),
                Jsonb(dict(audit)),
                _required_text(model_name, "model_name"),
                _required_text(prompt_version, "prompt_version"),
                _required_text(workflow_version, "workflow_version"),
                _required_text(artifact_hash, "artifact_hash"),
                int(now_ms),
                session_date,
                _required_text(lease_owner, "lease_owner"),
            ),
        ).fetchone()
        if inserted is None:
            return False
        completed = self.conn.execute(
            """
            UPDATE macro_research_runs
            SET status = 'published',
                leased_until_ms = NULL,
                lease_owner = NULL,
                last_error_code = NULL,
                last_error_message = NULL,
                updated_at_ms = %s
            WHERE session_date = %s
              AND status = 'running'
              AND lease_owner = %s
            RETURNING session_date
            """,
            (int(now_ms), session_date, _required_text(lease_owner, "lease_owner")),
        ).fetchone()
        if completed is None:
            raise RuntimeError("macro_research_publication_completion_failed")
        return True

    def run_record(self, session_date: date) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM macro_research_runs WHERE session_date = %s",
            (session_date,),
        ).fetchone()
        return dict(row) if row is not None else None

    def publication_record(self, session_date: date) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT
              publications.*,
              runs.sealed_at_ms
            FROM macro_research_publications AS publications
            JOIN macro_research_runs AS runs USING (session_date)
            WHERE publications.session_date = %s
            """,
            (session_date,),
        ).fetchone()
        return dict(row) if row is not None else None

    def research_state(self, session_date: date | None = None) -> dict[str, Any] | None:
        if session_date is None:
            target = self.conn.execute(
                "SELECT session_date FROM macro_research_runs ORDER BY session_date DESC LIMIT 1"
            ).fetchone()
            if target is None:
                return None
            session_date = target["session_date"]
        row = self.conn.execute(
            """
            SELECT
              runs.session_date,
              runs.market_cutoff_ms,
              runs.status AS run_status,
              runs.sealed_at_ms,
              runs.attempt_count,
              runs.max_attempts,
              runs.due_at_ms,
              runs.leased_until_ms,
              runs.lease_owner,
              runs.last_error_code,
              runs.last_error_message,
              runs.created_at_ms,
              runs.updated_at_ms,
              publications.artifact_json,
              publications.report_markdown,
              publications.audit_json,
              publications.model_name,
              publications.prompt_version,
              publications.workflow_version,
              publications.artifact_hash,
              publications.published_at_ms
            FROM macro_research_runs AS runs
            LEFT JOIN macro_research_publications AS publications USING (session_date)
            WHERE runs.session_date = %s
            """,
            (session_date,),
        ).fetchone()
        return dict(row) if row is not None else None

    def catalog(self, *, scope: FrozenMacroEvidenceScope) -> MacroEvidenceCatalog:
        observation_rows = self.conn.execute(
            """
            SELECT
              concept_key,
              source_name,
              observed_at,
              source_ts,
              ingested_at_ms
            FROM macro_observations
            WHERE ingested_at_ms <= %s
              AND (
                observed_at <= %s
                OR concept_key LIKE 'event:%%'
              )
            """,
            (int(scope.sealed_at_ms), scope.session_date),
        ).fetchall()
        visible_observations = [
            row
            for row in observation_rows
            if resolve_observation_visibility(
                scope,
                source_timestamp=str(row["source_ts"] or row["observed_at"]),
                ingested_at_ms=int(row["ingested_at_ms"]),
            )
            is not None
        ]
        prior_row = self.conn.execute(
            """
            SELECT COUNT(*)::int AS prior_research_count
            FROM macro_research_publications
            WHERE session_date < %s
              AND published_at_ms <= %s
            """,
            (scope.session_date, int(scope.sealed_at_ms)),
        ).fetchone()
        concept_keys = tuple(sorted({str(row["concept_key"]) for row in visible_observations}))
        source_labels = tuple(sorted({str(row["source_name"]) for row in visible_observations}))
        return MacroEvidenceCatalog(
            session_date=scope.session_date,
            market_cutoff_ms=scope.market_cutoff_ms,
            sealed_at_ms=scope.sealed_at_ms,
            concept_keys=concept_keys,
            source_labels=source_labels,
            observation_count=len(visible_observations),
            prior_research_count=int(prior_row["prior_research_count"] or 0),
        )

    def search_observations(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        query: MacroObservationQuery,
    ) -> tuple[MacroEvidenceRecord, ...]:
        batch_size = min(
            10_000,
            max(250, (int(query.offset) + int(query.limit)) * 20),
        )
        raw_offset = 0
        visible_seen = 0
        selected: list[MacroEvidenceRecord] = []
        while len(selected) < query.limit:
            rows = self.conn.execute(
                """
                SELECT *
                FROM macro_observations
                WHERE ingested_at_ms <= %s
                  AND (%s::date IS NULL OR observed_at >= %s::date)
                  AND (%s::date IS NULL OR observed_at <= %s::date)
                  AND (
                    cardinality(%s::text[]) = 0
                    OR concept_key = ANY(%s::text[])
                  )
                  AND (
                    %s = ''
                    OR concept_key ILIKE %s
                    OR source_name ILIKE %s
                    OR series_key ILIKE %s
                    OR COALESCE(source_ts, '') ILIKE %s
                    OR raw_payload_json::text ILIKE %s
                  )
                ORDER BY observed_at DESC, source_priority DESC, ingested_at_ms DESC,
                         observation_id ASC
                LIMIT %s
                OFFSET %s
                """,
                (
                    int(scope.sealed_at_ms),
                    query.start_date,
                    query.start_date,
                    query.end_date,
                    query.end_date,
                    list(query.concept_keys),
                    list(query.concept_keys),
                    query.query,
                    _like(query.query),
                    _like(query.query),
                    _like(query.query),
                    _like(query.query),
                    _like(query.query),
                    batch_size,
                    raw_offset,
                ),
            ).fetchall()
            if not rows:
                break
            for record in _visible_observation_records(scope, rows):
                if visible_seen < query.offset:
                    visible_seen += 1
                    continue
                selected.append(record)
                if len(selected) >= query.limit:
                    break
            raw_offset += len(rows)
            if len(rows) < batch_size:
                break
        return require_evidence_in_scope(
            scope,
            tuple(selected),
        )

    def read_evidence(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        source_refs: tuple[str, ...],
    ) -> tuple[MacroEvidenceRecord, ...]:
        if len(source_refs) > MACRO_RESEARCH_MAX_READ_REFS:
            raise ValueError("macro_research_read_evidence_limit")
        if len(source_refs) != len(set(source_refs)):
            raise ValueError("macro_research_read_evidence_duplicate_ref")
        observation_ids = [source_ref for source_ref in source_refs if source_ref.startswith("macro-observation:")]
        resolved: dict[str, MacroEvidenceRecord] = {}
        if observation_ids:
            rows = self.conn.execute(
                """
                SELECT *
                FROM macro_observations
                WHERE observation_id = ANY(%s::text[])
                  AND ingested_at_ms <= %s
                """,
                (
                    observation_ids,
                    int(scope.sealed_at_ms),
                ),
            ).fetchall()
            for record in _visible_observation_records(scope, rows):
                resolved[record.evidence_ref] = record
        return require_evidence_in_scope(
            scope,
            tuple(resolved[source_ref] for source_ref in source_refs if source_ref in resolved),
        )

    def prior_research(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        limit: int,
        offset: int,
    ) -> tuple[MacroPriorResearch, ...]:
        bounded_limit = min(
            max(int(limit), 1),
            MACRO_RESEARCH_MAX_PRIOR_PUBLICATIONS_PER_PAGE,
        )
        bounded_offset = max(int(offset), 0)
        rows = self.conn.execute(
            """
            SELECT
              session_date,
              artifact_json,
              published_at_ms
            FROM macro_research_publications
            WHERE session_date < %s
              AND published_at_ms <= %s
            ORDER BY session_date DESC
            LIMIT %s
            OFFSET %s
            """,
            (
                scope.session_date,
                int(scope.sealed_at_ms),
                bounded_limit,
                bounded_offset,
            ),
        ).fetchall()
        records = tuple(
            MacroPriorResearch(
                publication_ref=f"macro-research:{row['session_date'].isoformat()}",
                session_date=row["session_date"],
                title=_artifact_text(row["artifact_json"], "title"),
                executive_summary=_artifact_text(
                    row["artifact_json"],
                    "executive_summary",
                ),
                published_at_ms=int(row["published_at_ms"]),
            )
            for row in rows
        )
        return require_prior_research_in_scope(scope, records)


class PostgresMacroResearchReadPort:
    """Short-lived worker connections for DeepAgents read-only evidence tools."""

    def __init__(
        self,
        *,
        db: Any,
        worker_name: str,
        statement_timeout_seconds: float,
    ) -> None:
        self._db = db
        self._worker_name = _required_text(worker_name, "worker_name")
        self._statement_timeout_seconds = float(statement_timeout_seconds)

    def catalog(self, *, scope: FrozenMacroEvidenceScope) -> MacroEvidenceCatalog:
        with self._session() as repos:
            return cast(
                "MacroEvidenceCatalog",
                repos.macro_research.catalog(scope=scope),
            )

    def search_observations(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        query: MacroObservationQuery,
    ) -> tuple[MacroEvidenceRecord, ...]:
        with self._session() as repos:
            return cast(
                "tuple[MacroEvidenceRecord, ...]",
                repos.macro_research.search_observations(
                    scope=scope,
                    query=query,
                ),
            )

    def read_evidence(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        source_refs: tuple[str, ...],
    ) -> tuple[MacroEvidenceRecord, ...]:
        with self._session() as repos:
            return cast(
                "tuple[MacroEvidenceRecord, ...]",
                repos.macro_research.read_evidence(
                    scope=scope,
                    source_refs=source_refs,
                ),
            )

    def prior_research(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        limit: int,
        offset: int,
    ) -> tuple[MacroPriorResearch, ...]:
        with self._session() as repos:
            return cast(
                "tuple[MacroPriorResearch, ...]",
                repos.macro_research.prior_research(
                    scope=scope,
                    limit=limit,
                    offset=offset,
                ),
            )

    def _session(self) -> Any:
        return self._db.worker_session(
            self._worker_name,
            statement_timeout_seconds=self._statement_timeout_seconds,
        )


def _visible_observation_records(
    scope: FrozenMacroEvidenceScope,
    rows: Any,
) -> tuple[MacroEvidenceRecord, ...]:
    records: list[MacroEvidenceRecord] = []
    for row in rows:
        visibility = resolve_observation_visibility(
            scope,
            source_timestamp=str(row["source_ts"] or row["observed_at"]),
            ingested_at_ms=int(row["ingested_at_ms"]),
        )
        if visibility is None:
            continue
        value = row["value_numeric"]
        value_text = "unavailable" if value is None else str(value)
        unit = str(row["unit"] or "").strip()
        summary = (
            f"{row['concept_key']}={value_text}"
            f"{f' {unit}' if unit else ''}; observed_at={row['observed_at']}; "
            f"quality={row['data_quality']}"
        )
        records.append(
            MacroEvidenceRecord(
                evidence_ref=_observation_ref(row["observation_id"]),
                evidence_kind="observation",
                source_label=str(row["source_name"]),
                concept_key=str(row["concept_key"]),
                source_timestamp=str(row["source_ts"] or row["observed_at"]),
                available_at_ms=visibility.available_at_ms,
                persisted_at_ms=int(row["ingested_at_ms"]),
                observed_at=row["observed_at"],
                summary=summary,
                payload={
                    "observation_id": str(row["observation_id"]),
                    "concept_key": str(row["concept_key"]),
                    "series_key": str(row["series_key"]),
                    "source_priority": int(row["source_priority"]),
                    "value_numeric": None if value is None else str(value),
                    "unit": row["unit"],
                    "frequency": row["frequency"],
                    "data_quality": str(row["data_quality"]),
                    "source_ts": row["source_ts"],
                    "availability": visibility.availability,
                    "raw_payload": dict(row["raw_payload_json"] or {}),
                    "fact_payload_hash": str(row["fact_payload_hash"] or ""),
                },
                lineage={
                    "observation_id": str(row["observation_id"]),
                    "concept_key": str(row["concept_key"]),
                    "series_key": str(row["series_key"]),
                    "source_name": str(row["source_name"]),
                    "source_ts": str(row["source_ts"] or row["observed_at"]),
                    "fact_payload_hash": str(row["fact_payload_hash"] or ""),
                    "availability": visibility.availability,
                },
            )
        )
    return tuple(records)


def _observation_ref(value: object) -> str:
    observation_id = str(value)
    if observation_id.startswith("macro-observation:"):
        return observation_id
    return f"macro-observation:{observation_id}"


def _artifact_text(value: object, field_name: str) -> str:
    payload = value if isinstance(value, Mapping) else {}
    normalized = str(payload.get(field_name) or "").strip()
    if not normalized:
        raise RuntimeError(f"macro_research_prior_{field_name}_missing")
    return normalized


def _like(value: str) -> str:
    return f"%{value}%"


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"macro_research_{field_name}_required")
    return normalized


def _safe_error_code(value: object) -> str:
    normalized = str(value or "macro_research_unknown_error").strip().lower()
    safe = "".join(char if char.isalnum() or char == "_" else "_" for char in normalized)
    return (safe.strip("_") or "macro_research_unknown_error")[:120]


def _safe_error_message(value: object) -> str:
    return str(value or "macro research failed").replace("\n", " ")[:2_000]


__all__ = ["MacroResearchRepository", "PostgresMacroResearchReadPort"]
