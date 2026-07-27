from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, cast

from psycopg.types.json import Jsonb

from tracefold.macro.research.service import (
    MACRO_RESEARCH_MAX_PRIOR_PUBLICATIONS_PER_PAGE,
    MACRO_RESEARCH_MAX_READ_REFS,
    FrozenMacroEvidenceScope,
    MacroEvidenceCatalog,
    MacroEvidenceQuery,
    MacroEvidenceRecord,
    MacroPriorResearch,
    require_evidence_in_scope,
    require_prior_research_in_scope,
)


class MacroResearchRepository:
    """Durable research lifecycle and frozen Evidence Pack read adapter."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def publication_exists(self, session_date: date) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM macro_research_publications WHERE session_date = %s",
                (session_date,),
            ).fetchone()
            is not None
        )

    def retry_failed_run(self, *, session_date: date, now_ms: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            UPDATE macro_research_runs
            SET status = 'retryable',
                max_attempts = GREATEST(max_attempts, attempt_count + 1),
                due_at_ms = %s,
                leased_until_ms = NULL,
                lease_owner = NULL,
                last_error_code = NULL,
                last_error_message = NULL,
                updated_at_ms = %s
            WHERE session_date = %s
              AND status = 'failed'
              AND NOT EXISTS (
                SELECT 1
                FROM macro_research_publications
                WHERE macro_research_publications.session_date = macro_research_runs.session_date
              )
            RETURNING *
            """,
            (int(now_ms), int(now_ms), session_date),
        ).fetchone()
        if row is None:
            raise ValueError("macro_research_failed_run_not_retryable")
        return dict(row)

    def latest_run_session(self) -> date | None:
        row = self.conn.execute("SELECT MAX(session_date) AS session_date FROM macro_research_runs").fetchone()
        return row["session_date"] if row is not None else None

    def scheduling_state(self, *, through_date: date) -> dict[str, date | None]:
        row = self.conn.execute(
            """
            SELECT
              MAX(session_date) AS latest_session,
              MAX(session_date) FILTER (
                WHERE status IN ('pending', 'running', 'retryable', 'failed')
                  AND session_date <= %s
              ) AS open_session
            FROM macro_research_runs
            """,
            (through_date,),
        ).fetchone()
        return {
            "latest_session": row["latest_session"] if row is not None else None,
            "open_session": row["open_session"] if row is not None else None,
        }

    def ensure_run(
        self,
        *,
        session_date: date,
        market_cutoff_ms: int,
        evidence_pack_id: str,
        sealed_at_ms: int,
        max_attempts: int,
        due_at_ms: int,
        now_ms: int,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO macro_research_runs(
              session_date, market_cutoff_ms, evidence_pack_id, status, sealed_at_ms,
              attempt_count, max_attempts, due_at_ms, created_at_ms, updated_at_ms
            )
            VALUES (%s, %s, %s, 'pending', %s, 0, %s, %s, %s, %s)
            ON CONFLICT(session_date) DO NOTHING
            """,
            (
                session_date,
                int(market_cutoff_ms),
                _required_text(evidence_pack_id, "evidence_pack_id"),
                int(sealed_at_ms),
                int(max_attempts),
                int(due_at_ms),
                int(now_ms),
                int(now_ms),
            ),
        )
        return int(cursor.rowcount)

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
                  last_error_code = 'macro_research_lease_expired',
                  last_error_message = 'research lease expired after attempt budget',
                  updated_at_ms = %s
              WHERE session_date = %s
                AND status = 'running'
                AND leased_until_ms <= %s
                AND attempt_count >= max_attempts
              RETURNING session_date
            ), candidate AS (
              SELECT session_date
              FROM macro_research_runs
              WHERE session_date = %s
                AND (
                  (status IN ('pending', 'retryable') AND due_at_ms <= %s)
                  OR (status = 'running' AND leased_until_ms <= %s)
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
                reviewer_disposition = NULL,
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
        return (
            self.conn.execute(
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
            is not None
        )

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
            SET status = CASE WHEN attempt_count >= max_attempts THEN 'failed' ELSE 'retryable' END,
                due_at_ms = CASE
                  WHEN attempt_count >= max_attempts THEN due_at_ms
                  ELSE %s
                END,
                leased_until_ms = NULL,
                lease_owner = NULL,
                reviewer_disposition = CASE
                  WHEN %s LIKE 'macro_research_reviewer_%%'
                  THEN split_part(%s, 'macro_research_reviewer_', 2)
                  ELSE reviewer_disposition
                END,
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
                error_code,
                error_code,
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
        reviewer_disposition = str(artifact.get("reviewer_disposition") or "")
        if reviewer_disposition != "pass":
            raise ValueError("macro_research_publication_requires_reviewer_pass")
        inserted = self.conn.execute(
            """
            INSERT INTO macro_research_publications(
              session_date, market_cutoff_ms, evidence_pack_id, artifact_json,
              report_markdown, audit_json, reviewer_disposition, model_name,
              prompt_version, workflow_version, artifact_hash, published_at_ms
            )
            SELECT
              runs.session_date, runs.market_cutoff_ms, runs.evidence_pack_id,
              %s, %s, %s, %s, %s, %s, %s, %s, %s
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
                reviewer_disposition,
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
                reviewer_disposition = 'pass',
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
            SELECT publications.*, runs.sealed_at_ms
            FROM macro_research_publications AS publications
            JOIN macro_research_runs AS runs USING (session_date)
            WHERE publications.session_date = %s
            """,
            (session_date,),
        ).fetchone()
        return dict(row) if row is not None else None

    def research_state(self, session_date: date | None = None) -> dict[str, Any] | None:
        if session_date is None:
            row = self.conn.execute("SELECT MAX(session_date) AS session_date FROM macro_research_runs").fetchone()
            session_date = row["session_date"] if row is not None else None
        if session_date is None:
            return None
        row = self.conn.execute(
            """
            SELECT
              runs.session_date,
              runs.market_cutoff_ms,
              runs.evidence_pack_id,
              runs.status AS run_status,
              runs.sealed_at_ms,
              runs.attempt_count,
              runs.max_attempts,
              runs.due_at_ms,
              runs.leased_until_ms,
              runs.lease_owner,
              runs.reviewer_disposition AS run_reviewer_disposition,
              runs.last_error_code,
              runs.last_error_message,
              runs.created_at_ms,
              runs.updated_at_ms,
              publications.artifact_json,
              publications.report_markdown,
              publications.audit_json,
              publications.reviewer_disposition,
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
        pack = self._scope_pack(scope)
        records = _pack_records(pack)
        prior_row = self.conn.execute(
            """
            SELECT COUNT(*)::int AS count
            FROM macro_research_publications
            WHERE session_date < %s
              AND published_at_ms <= %s
            """,
            (scope.session_date, int(scope.sealed_at_ms)),
        ).fetchone()
        return MacroEvidenceCatalog(
            session_date=scope.session_date,
            market_cutoff_ms=scope.market_cutoff_ms,
            sealed_at_ms=scope.sealed_at_ms,
            concept_keys=tuple(sorted({str(record.concept_key) for record in records if record.concept_key})),
            source_labels=tuple(sorted({record.source_label for record in records})),
            observation_count=len(records),
            prior_research_count=int(prior_row["count"] or 0),
        )

    def search_evidence(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        query: MacroEvidenceQuery,
    ) -> tuple[MacroEvidenceRecord, ...]:
        records = _pack_records(self._scope_pack(scope))
        query_text = query.query.lower()
        selected = []
        for record in records:
            if query.concept_keys and record.concept_key not in query.concept_keys:
                continue
            if (
                query.start_date is not None
                and record.observed_at is not None
                and record.observed_at < query.start_date
            ):
                continue
            if query.end_date is not None and record.observed_at is not None and record.observed_at > query.end_date:
                continue
            if query_text:
                searchable = (f"{record.concept_key} {record.source_label} {record.summary} {record.payload}").lower()
                if query_text not in searchable:
                    continue
            selected.append(record)
        page = tuple(selected[query.offset : query.offset + query.limit])
        return require_evidence_in_scope(scope, page)

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
        by_ref = {record.evidence_ref: record for record in _pack_records(self._scope_pack(scope))}
        return require_evidence_in_scope(
            scope,
            tuple(by_ref[source_ref] for source_ref in source_refs if source_ref in by_ref),
        )

    def prior_research(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        limit: int,
        offset: int,
    ) -> tuple[MacroPriorResearch, ...]:
        bounded_limit = min(max(int(limit), 1), MACRO_RESEARCH_MAX_PRIOR_PUBLICATIONS_PER_PAGE)
        rows = self.conn.execute(
            """
            SELECT session_date, artifact_json, published_at_ms
            FROM macro_research_publications
            WHERE session_date < %s
              AND published_at_ms <= %s
            ORDER BY session_date DESC
            LIMIT %s OFFSET %s
            """,
            (
                scope.session_date,
                int(scope.sealed_at_ms),
                bounded_limit,
                max(int(offset), 0),
            ),
        ).fetchall()
        records = tuple(
            MacroPriorResearch(
                publication_ref=f"macro-research:{row['session_date'].isoformat()}",
                session_date=row["session_date"],
                title=_artifact_text(row["artifact_json"], "title"),
                executive_summary=_artifact_text(row["artifact_json"], "executive_summary"),
                published_at_ms=int(row["published_at_ms"]),
            )
            for row in rows
        )
        return require_prior_research_in_scope(scope, records)

    def _scope_pack(self, scope: FrozenMacroEvidenceScope) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT *
            FROM macro_evidence_packs
            WHERE evidence_pack_id = %s
              AND session_date = %s
              AND schema_version = 'macro_evidence_pack_v2'
              AND judgment_cutoff_ms <= %s
              AND created_at_ms <= %s
            """,
            (
                scope.evidence_pack_id,
                scope.session_date,
                int(scope.market_cutoff_ms),
                int(scope.sealed_at_ms),
            ),
        ).fetchone()
        if row is None:
            raise ValueError("macro_research_evidence_pack_out_of_scope")
        return dict(row)


class PostgresMacroResearchReadPort:
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
            return cast("MacroEvidenceCatalog", repos.macro_research.catalog(scope=scope))

    def search_evidence(
        self,
        *,
        scope: FrozenMacroEvidenceScope,
        query: MacroEvidenceQuery,
    ) -> tuple[MacroEvidenceRecord, ...]:
        with self._session() as repos:
            return cast(
                "tuple[MacroEvidenceRecord, ...]",
                repos.macro_research.search_evidence(scope=scope, query=query),
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
                repos.macro_research.read_evidence(scope=scope, source_refs=source_refs),
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


def _pack_records(pack: dict[str, Any]) -> tuple[MacroEvidenceRecord, ...]:
    payload = pack["payload_json"]
    if not isinstance(payload, Mapping):
        raise ValueError("macro_evidence_pack_payload_invalid")
    pack_id = str(pack["evidence_pack_id"])
    cutoff_ms = int(pack["judgment_cutoff_ms"])
    persisted_at_ms = int(pack["created_at_ms"])
    session_date = pack["session_date"]
    cutoff_timestamp = datetime.fromtimestamp(cutoff_ms / 1_000, tz=UTC).isoformat()
    records: list[MacroEvidenceRecord] = []
    for module in payload.get("modules", ()):
        if not isinstance(module, Mapping):
            continue
        module_id = str(module.get("module_id") or "")
        if not module_id:
            continue
        records.append(
            MacroEvidenceRecord(
                evidence_ref=f"macro-pack:{pack_id}:module:{module_id}",
                evidence_kind="module",
                source_label=f"Evidence Pack / {module.get('label') or module_id}",
                concept_key=module_id,
                source_timestamp=cutoff_timestamp,
                available_at_ms=cutoff_ms,
                persisted_at_ms=persisted_at_ms,
                observed_at=session_date,
                published_at_ms=cutoff_ms,
                url=None,
                summary=(
                    f"{module.get('label') or module_id}; "
                    f"coverage={dict(module.get('status') or {}).get('coverage')}; "
                    f"top_changes={len(dict(module.get('summary') or {}).get('top_changes') or ())}"
                ),
                payload=dict(module),
                lineage={
                    "evidence_pack_id": pack_id,
                    "compiler_version": pack["compiler_version"],
                    "payload_hash": pack["payload_hash"],
                },
            )
        )
        evidence = module.get("evidence")
        latest_facts = evidence.get("latest_facts", ()) if isinstance(evidence, Mapping) else ()
        for fact in latest_facts:
            if not isinstance(fact, Mapping) or not fact.get("fact_ref"):
                continue
            available_at_ms = int(fact.get("published_at_ms") or fact.get("received_at_ms") or cutoff_ms)
            reference = _optional_date(fact.get("reference")) or session_date
            records.append(
                MacroEvidenceRecord(
                    evidence_ref=str(fact["fact_ref"]),
                    evidence_kind="observation",
                    source_label=str(fact.get("dataset_id") or "macro fact"),
                    concept_key=str(fact.get("dataset_id") or module_id),
                    source_timestamp=datetime.fromtimestamp(
                        available_at_ms / 1_000,
                        tz=UTC,
                    ).isoformat(),
                    available_at_ms=available_at_ms,
                    persisted_at_ms=persisted_at_ms,
                    observed_at=reference,
                    published_at_ms=(int(fact["published_at_ms"]) if fact.get("published_at_ms") is not None else None),
                    url=str(fact["source_url"]) if fact.get("source_url") else None,
                    summary=(
                        f"{fact.get('dataset_id')}={fact.get('value')} {fact.get('unit')}; "
                        f"reference={fact.get('reference')}"
                    ),
                    payload=dict(fact),
                    lineage={"evidence_pack_id": pack_id, "module_id": module_id},
                )
            )
        for feature in module.get("features", ()):
            if not isinstance(feature, Mapping) or not feature.get("feature_id"):
                continue
            records.append(
                MacroEvidenceRecord(
                    evidence_ref=f"macro-pack:{pack_id}:feature:{feature['feature_id']}",
                    evidence_kind="feature",
                    source_label=f"Calculation Registry / {feature['feature_id']}",
                    concept_key=str(feature["feature_id"]),
                    source_timestamp=cutoff_timestamp,
                    available_at_ms=cutoff_ms,
                    persisted_at_ms=persisted_at_ms,
                    observed_at=_optional_date(feature.get("as_of_date")) or session_date,
                    published_at_ms=cutoff_ms,
                    url=None,
                    summary=(
                        f"{feature['feature_id']}={feature.get('value_numeric')} "
                        f"{feature.get('unit')}; formula={feature.get('formula_version')}"
                    ),
                    payload=dict(feature),
                    lineage={"evidence_pack_id": pack_id, "module_id": module_id},
                )
            )
    by_ref = {record.evidence_ref: record for record in records}
    return tuple(by_ref.values())


def _artifact_text(value: object, field_name: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("macro_research_artifact_invalid")
    return _required_text(value.get(field_name), field_name)


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"macro_research_{field_name}_required")
    return text


def _safe_error_code(value: object) -> str:
    return _required_text(value, "error_code")[:120]


def _safe_error_message(value: object) -> str:
    return _required_text(value, "error_message").replace("\n", " ")[:2_000]


def _optional_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


__all__ = ["MacroResearchRepository", "PostgresMacroResearchReadPort"]
