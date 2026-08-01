from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefold.macro.domain import MacroModelExpectedError
from tracefold.macro.fed_roles import match_effective_role
from tracefold.platform.model_candidate import ModelCandidate
from tracefold.platform.resource import ResourceAdmissionTimeout

FED_DOCUMENT_ANALYSIS_PROMPT_VERSION = "fed_document_analysis_v2_evidence_ids"
FED_DOCUMENT_ANALYSIS_SCHEMA_VERSION = "macro_document_analysis_v1"
FED_FOMC_ANALYSIS_LOOKBACK_DAYS = 550
FED_SPEECH_ANALYSIS_LOOKBACK_DAYS = 120
_MAX_ATTEMPTS = 3
_LEASE_MS = 600_000
_RETRY_MS = 300_000
_STATEMENT_TIMEOUT_SECONDS = 120.0

PolicyRelevance = Literal["policy_signal", "not_policy_signal", "uncertain"]
FedStance = Literal["hawkish", "neutral", "dovish", "mixed", "no_call"]
StanceChange = Literal[
    "more_hawkish",
    "unchanged",
    "more_dovish",
    "mixed_change",
    "no_prior",
    "no_call",
]


class FedAnalysisEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    excerpt: str = Field(min_length=1, max_length=600)
    claim: str = Field(min_length=1, max_length=500)


class FedDocumentAnalysisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_relevance: PolicyRelevance
    stance: FedStance
    confidence: float | None = Field(default=None, ge=0, le=1)
    change_from_prior: StanceChange
    rationale: str = Field(min_length=1, max_length=2_000)
    evidence: list[FedAnalysisEvidence] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_semantics(self) -> FedDocumentAnalysisDraft:
        if self.policy_relevance == "policy_signal":
            if self.stance == "no_call" or self.confidence is None or not self.evidence:
                raise ValueError("fed_policy_signal_requires_stance_confidence_evidence")
        elif self.stance != "no_call" or self.confidence is not None:
            raise ValueError("fed_non_signal_requires_no_call")
        return self


class FedDocumentAnalysisAgentProtocol(Protocol):
    model_name: str

    async def analyze(
        self,
        *,
        document: Mapping[str, Any],
        roster_context: Mapping[str, Any] | None,
        prior_analysis: Mapping[str, Any] | None,
        on_model_submitted: Callable[[], None],
    ) -> FedDocumentAnalysisDraft: ...


class MacroDocumentAnalysisService:
    def __init__(
        self,
        *,
        db: Any,
        agent: FedDocumentAnalysisAgentProtocol,
        worker_name: str = "macro_document_analysis",
        lease_owner: str | None = None,
        clock_ms: Callable[[], int] | None = None,
        database: Any | None = None,
        stable_order: int = 30,
    ) -> None:
        self.db = db
        self.agent = agent
        self.worker_name = worker_name
        self.lease_owner = str(lease_owner or worker_name)
        self.clock_ms = clock_ms or _now_ms
        self.database = database
        self.stable_order = int(stable_order)

    async def reconcile(self, *, now_ms: int | None = None) -> int:
        now = int(now_ms if now_ms is not None else self.clock_ms())
        return int(await self._run_db(self._ensure_jobs, now))

    async def peek(self, *, now_ms: int) -> ModelCandidate | None:
        row = await self._run_db(self._peek_job, int(now_ms))
        if row is None:
            return None
        return ModelCandidate(
            kind="macro_document_analysis",
            target_key=str(row["analysis_job_id"]),
            due_at_ms=int(row["next_due_at_ms"]),
            stable_order=self.stable_order,
        )

    async def execute(self, candidate: ModelCandidate) -> bool:
        result = await self.run_once(analysis_job_id=candidate.target_key)
        return str(result["status"]) != "idle"

    async def run_once(
        self,
        *,
        now_ms: int | None = None,
        analysis_job_id: str | None = None,
    ) -> dict[str, Any]:
        now = int(now_ms if now_ms is not None else self.clock_ms())
        try:
            prepared = await self._run_db(self._prepare_job, now, analysis_job_id)
        except ResourceAdmissionTimeout:
            return {"status": "idle", "jobs_written": 0}
        jobs_written = int(prepared["jobs_written"])
        job = prepared["job"]
        if job is None:
            return {"status": "idle", "jobs_written": jobs_written}

        model_started = False

        def mark_model_submitted() -> None:
            nonlocal model_started
            model_started = True

        try:
            loaded = await self._run_db(self._load_job, job)
            document = loaded["document"]
            roster_context = loaded["roster_context"]
            prior = loaded["prior"]
            _require_document_hash(document, str(job["document_hash"]))
            if str(document["document_type"]) == "speech" and roster_context is None:
                draft = _unmatched_speaker_draft(document)
            else:
                draft = await self.agent.analyze(
                    document=document,
                    roster_context=roster_context,
                    prior_analysis=prior,
                    on_model_submitted=mark_model_submitted,
                )
            try:
                analysis = canonicalize_document_analysis(
                    draft,
                    document=document,
                    roster_context=roster_context,
                    prior_analysis=prior,
                )
            except ValueError as exc:
                raise MacroModelExpectedError(f"macro_document_model_output_invalid:{exc}") from exc
            return cast(
                dict[str, Any],
                await self._run_db(
                    self._publish_job,
                    job=job,
                    document=document,
                    roster_context=roster_context,
                    analysis=analysis,
                    jobs_written=jobs_written,
                ),
            )
        except asyncio.CancelledError:
            if not model_started:
                await asyncio.shield(self._release_prework(job))
            raise
        except ResourceAdmissionTimeout:
            if model_started:
                raise
            await self._release_prework(job)
            return {"status": "idle", "jobs_written": jobs_written}
        except MacroModelExpectedError as exc:
            return cast(
                dict[str, Any],
                await self._run_db(
                    self._fail_job,
                    job=job,
                    error=exc,
                    jobs_written=jobs_written,
                ),
            )

    async def _release_prework(self, job: Mapping[str, Any]) -> bool:
        return bool(await self._run_db(self._release_prework_sync, job))

    def _release_prework_sync(self, job: Mapping[str, Any]) -> bool:
        with self._session() as repos, repos.transaction():
            return bool(
                repos.macro.release_document_analysis_claim(
                    analysis_job_id=str(job["analysis_job_id"]),
                    lease_owner=self.lease_owner,
                    claimed_attempt_count=int(job["attempt_count"]),
                )
            )

    def _ensure_jobs(self, now: int) -> int:
        with self._session() as repos, repos.transaction():
            return int(
                repos.macro.ensure_document_analysis_jobs(
                    model_name=self.agent.model_name,
                    prompt_version=FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
                    max_attempts=_MAX_ATTEMPTS,
                    now_ms=now,
                    fomc_lookback_days=FED_FOMC_ANALYSIS_LOOKBACK_DAYS,
                    speech_lookback_days=FED_SPEECH_ANALYSIS_LOOKBACK_DAYS,
                )
            )

    def _peek_job(self, now: int) -> dict[str, Any] | None:
        with self._session() as repos:
            return cast(
                dict[str, Any] | None,
                repos.macro.peek_document_analysis_job(
                    model_name=self.agent.model_name,
                    prompt_version=FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
                    now_ms=now,
                ),
            )

    def _prepare_job(
        self,
        now: int,
        analysis_job_id: str | None = None,
    ) -> dict[str, Any]:
        with self._session() as repos, repos.transaction():
            jobs_written = repos.macro.ensure_document_analysis_jobs(
                model_name=self.agent.model_name,
                prompt_version=FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
                max_attempts=_MAX_ATTEMPTS,
                now_ms=now,
                fomc_lookback_days=FED_FOMC_ANALYSIS_LOOKBACK_DAYS,
                speech_lookback_days=FED_SPEECH_ANALYSIS_LOOKBACK_DAYS,
            )
            job = repos.macro.claim_document_analysis_job(
                model_name=self.agent.model_name,
                prompt_version=FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
                lease_owner=self.lease_owner,
                lease_ms=_LEASE_MS,
                now_ms=now,
                analysis_job_id=analysis_job_id,
            )
        return {"jobs_written": jobs_written, "job": dict(job) if job is not None else None}

    def _load_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        with self._session() as repos:
            document = repos.macro.document_analysis_job_document(str(job["analysis_job_id"]))
            if document is None:
                raise RuntimeError("macro_document_analysis_document_missing")
            role_rows = repos.macro.fed_official_role_history()
            roster_context = _document_roster_context(document, role_rows)
            prior = repos.macro.prior_document_analysis(
                effective_date=_required_date(document["effective_date"]),
                official_id=(str(roster_context["official_id"]) if roster_context is not None else None),
                document_type=str(document["document_type"]),
            )
        return {
            "document": document,
            "roster_context": roster_context,
            "prior": prior,
        }

    def _publish_job(
        self,
        *,
        job: Mapping[str, Any],
        document: Mapping[str, Any],
        roster_context: Mapping[str, Any] | None,
        analysis: Mapping[str, Any],
        jobs_written: int,
    ) -> dict[str, Any]:
        payload_hash = _payload_hash(
            {
                "document_id": str(document["document_id"]),
                "analysis": analysis,
            }
        )
        identity = (
            f"{document['document_id']}|{job['document_hash']}|"
            f"{self.agent.model_name}|{FED_DOCUMENT_ANALYSIS_PROMPT_VERSION}|{payload_hash}"
        )
        analysis_id = "macroan_" + hashlib.sha256(identity.encode()).hexdigest()
        completed_at_ms = int(self.clock_ms())
        with self._session() as repos, repos.transaction():
            written = repos.macro.insert_document_analysis(
                analysis_id=analysis_id,
                document_id=str(document["document_id"]),
                document_hash=str(job["document_hash"]),
                official_id=(str(roster_context["official_id"]) if roster_context is not None else None),
                policy_relevance=str(analysis["policy_relevance"]),
                stance=str(analysis["stance"]),
                confidence=(float(analysis["confidence"]) if analysis["confidence"] is not None else None),
                analysis=analysis,
                model_name=self.agent.model_name,
                prompt_version=FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
                reviewer_disposition="pass",
                created_at_ms=completed_at_ms,
                payload_hash=payload_hash,
            )
            if written != 1:
                raise RuntimeError("macro_document_analysis_insert_conflict")
            completed = repos.macro.complete_document_analysis_job(
                analysis_job_id=str(job["analysis_job_id"]),
                lease_owner=self.lease_owner,
                completed_at_ms=completed_at_ms,
            )
            if not completed:
                raise RuntimeError("macro_document_analysis_stale_claim")
        return {
            "status": "published",
            "analysis_id": analysis_id,
            "document_id": document["document_id"],
            "rows_written": written,
            "jobs_written": jobs_written,
            "policy_relevance": analysis["policy_relevance"],
            "stance": analysis["stance"],
        }

    def _fail_job(
        self,
        *,
        job: Mapping[str, Any],
        error: Exception,
        jobs_written: int,
    ) -> dict[str, Any]:
        completed_at_ms = int(self.clock_ms())
        error_code = _error_code(error)
        with self._session() as repos, repos.transaction():
            failed = repos.macro.fail_document_analysis_job(
                job=job,
                lease_owner=self.lease_owner,
                error_code=error_code,
                next_due_at_ms=completed_at_ms + _RETRY_MS,
                completed_at_ms=completed_at_ms,
            )
            if not failed:
                raise RuntimeError("macro_document_analysis_stale_claim") from error
        return {
            "status": "failed",
            "document_id": job["document_id"],
            "error_code": error_code,
            "jobs_written": jobs_written,
        }

    async def _run_db(
        self,
        function: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if self.database is None:
            return function(*args, **kwargs)
        return await self.database.run_business(
            "macro_document_analysis_db",
            function,
            *args,
            operation_timeout_seconds=_STATEMENT_TIMEOUT_SECONDS,
            **kwargs,
        )

    def _session(self) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=_STATEMENT_TIMEOUT_SECONDS,
        )


def canonicalize_document_analysis(
    draft: FedDocumentAnalysisDraft,
    *,
    document: Mapping[str, Any],
    roster_context: Mapping[str, Any] | None,
    prior_analysis: Mapping[str, Any] | None,
) -> dict[str, Any]:
    content_text = str(document.get("content_text") or "")
    evidence = [{"excerpt": item.excerpt, "claim": item.claim} for item in draft.evidence]
    for item in evidence:
        if item["excerpt"] not in content_text:
            raise ValueError("fed_document_analysis_evidence_not_exact")
    if draft.policy_relevance == "policy_signal" and not evidence:
        raise ValueError("fed_document_analysis_evidence_required")
    if prior_analysis is None and draft.change_from_prior not in {"no_prior", "no_call"}:
        raise ValueError("fed_document_analysis_prior_change_without_prior")
    return {
        "schema_version": FED_DOCUMENT_ANALYSIS_SCHEMA_VERSION,
        "policy_relevance": draft.policy_relevance,
        "stance": draft.stance,
        "confidence": draft.confidence,
        "change_from_prior": draft.change_from_prior,
        "rationale": draft.rationale,
        "evidence": evidence,
        "roster_context": dict(roster_context) if roster_context is not None else None,
        "prior_analysis_id": (str(prior_analysis["analysis_id"]) if prior_analysis is not None else None),
        "source_body_hash": str(document["document_hash"]),
    }


def _document_roster_context(
    document: Mapping[str, Any],
    role_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if str(document["document_type"]) != "speech":
        return None
    metadata = document.get("metadata_json")
    speaker_name = str(metadata.get("speaker_name") or "") if isinstance(metadata, dict) else ""
    role = match_effective_role(
        speaker_name,
        effective_date=_required_date(document["effective_date"]),
        role_rows=role_rows,
    )
    if role is None:
        return None
    return {
        "official_id": role["official_id"],
        "official_name": role["official_name"],
        "role_title": role["role_title"],
        "organization": role["organization"],
        "fomc_participant": bool(role["fomc_participant"]),
        "fomc_voter": bool(role["fomc_voter"]),
        "effective_start": str(role["effective_start"]),
        "effective_end": str(role["effective_end"]) if role["effective_end"] else None,
        "role_fact_id": role["role_fact_id"],
    }


def _unmatched_speaker_draft(document: Mapping[str, Any]) -> FedDocumentAnalysisDraft:
    speaker_name = ""
    metadata = document.get("metadata_json")
    if isinstance(metadata, dict):
        speaker_name = str(metadata.get("speaker_name") or "")
    return FedDocumentAnalysisDraft(
        policy_relevance="uncertain",
        stance="no_call",
        confidence=None,
        change_from_prior="no_call",
        rationale=(
            f"讲话者无法与讲话日有效的 FOMC role fact 闭合，因此不进入官员沟通分布。speaker={speaker_name or 'unknown'}"
        ),
        evidence=[],
    )


def _require_document_hash(document: Mapping[str, Any], expected_hash: str) -> None:
    actual_hash = str(document.get("document_hash") or "")
    if actual_hash != expected_hash:
        raise ValueError("macro_document_analysis_body_hash_mismatch")


def _required_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _error_code(exc: Exception) -> str:
    normalized = "".join(char if char.isalnum() else "_" for char in str(exc).lower())
    return (normalized.strip("_") or type(exc).__name__.lower())[:120]


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = [
    "FED_DOCUMENT_ANALYSIS_PROMPT_VERSION",
    "FED_DOCUMENT_ANALYSIS_SCHEMA_VERSION",
    "FED_FOMC_ANALYSIS_LOOKBACK_DAYS",
    "FED_SPEECH_ANALYSIS_LOOKBACK_DAYS",
    "FedAnalysisEvidence",
    "FedDocumentAnalysisAgentProtocol",
    "FedDocumentAnalysisDraft",
    "MacroDocumentAnalysisService",
    "canonicalize_document_analysis",
]
