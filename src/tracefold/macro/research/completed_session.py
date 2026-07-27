from __future__ import annotations

import asyncio
import hashlib
import json
import time as wall_time
from calendar import monthrange
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from tracefold.macro.research.service import (
    FrozenMacroEvidenceScope,
    MacroResearchAgent,
    MacroResearchAgentResult,
    MacroResearchIntegrityError,
    require_artifact_integrity,
)

_NEW_YORK = ZoneInfo("America/New_York")
_REGULAR_CLOSE = time(16, 0)
_EARLY_CLOSE = time(13, 0)


@dataclass(frozen=True, slots=True)
class MacroSessionView:
    session_date: date
    status: str
    market_cutoff_ms: int
    evidence_pack_id: str
    sealed_at_ms: int
    attempt_count: int
    max_attempts: int
    due_at_ms: int
    artifact: dict[str, Any] | None = None
    report_markdown: str | None = None
    audit: dict[str, Any] | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    workflow_version: str | None = None
    artifact_hash: str | None = None
    published_at_ms: int | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    model_calls: int = 0
    run_rows_written: int = 0
    publication_rows_written: int = 0

    @property
    def publication_exists(self) -> bool:
        return self.artifact is not None


@dataclass(frozen=True, slots=True)
class _PreparedRun:
    claimed: dict[str, Any] | None
    run_rows_written: int


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    model_calls: int = 0
    run_rows_written: int = 0
    publication_rows_written: int = 0
    artifact_hash: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class _MacroResearchLeaseLost(RuntimeError):
    pass


class MacroResearchNotReady(RuntimeError):
    def __init__(self, *, session_date: date, reason: str) -> None:
        self.session_date = session_date
        self.reason = _required_text(reason, "reason")
        super().__init__(f"macro_research_not_ready:{session_date.isoformat()}:{self.reason}")


class CompletedSessionMacro:
    """Own one completed-session research lifecycle around one DeepAgent call."""

    def __init__(
        self,
        *,
        db: Any,
        settings: Any,
        agent: MacroResearchAgent,
        worker_name: str = "macro_research",
        lease_owner: str | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if db is None:
            raise RuntimeError("macro_research_db_required")
        self._db = db
        self._settings = settings
        self._agent = agent
        self._worker_name = _required_text(worker_name, "worker_name")
        self._lease_owner = lease_owner or (f"{self._worker_name}:{uuid4().hex}")
        self._clock_ms = clock_ms or _now_ms

    async def run(
        self,
        session_date: date | None = None,
    ) -> MacroSessionView:
        target_session = await self._resolve_run_target(session_date)
        outcome = await self._run_completed_session(target_session)
        view = await self.read(target_session)
        if view is None:
            raise RuntimeError("macro_research_run_state_missing")
        return replace(
            view,
            model_calls=outcome.model_calls,
            run_rows_written=outcome.run_rows_written,
            publication_rows_written=outcome.publication_rows_written,
            artifact_hash=outcome.artifact_hash or view.artifact_hash,
            last_error_code=outcome.error_code or view.last_error_code,
            last_error_message=outcome.error_message or view.last_error_message,
        )

    async def read(
        self,
        session_date: date | None = None,
    ) -> MacroSessionView | None:
        target_session = session_date or resolve_completed_session(
            now_ms=int(self._clock_ms()),
            settle_delay_seconds=self._settle_delay_seconds(),
        )
        row = await asyncio.to_thread(
            self._read_state,
            session_date=target_session,
        )
        return _session_view(row) if row is not None else None

    async def _run_completed_session(
        self,
        session_date: date,
    ) -> _RunOutcome:
        session_close_ms = completed_session_close_ms(session_date)
        now_ms = int(self._clock_ms())
        due_at_ms = session_close_ms + self._settle_delay_seconds() * 1_000
        if now_ms < due_at_ms:
            raise ValueError("macro_research_session_not_completed")

        prepared = await asyncio.to_thread(
            self._prepare_run,
            session_date=session_date,
            due_at_ms=due_at_ms,
            now_ms=now_ms,
        )
        if prepared.claimed is None:
            return _RunOutcome(
                run_rows_written=prepared.run_rows_written,
            )

        claimed = prepared.claimed
        scope = FrozenMacroEvidenceScope(
            session_date=session_date,
            market_cutoff_ms=int(claimed["market_cutoff_ms"]),
            sealed_at_ms=int(claimed["sealed_at_ms"]),
            evidence_pack_id=str(claimed["evidence_pack_id"]),
        )
        try:
            analysis = await self._analyze_with_lease_heartbeat(scope)
            artifact, artifact_hash = _validated_artifact(analysis, scope=scope)
            published_at_ms = max(int(self._clock_ms()), int(claimed["market_cutoff_ms"]))
            published = await asyncio.to_thread(
                self._publish,
                session_date=session_date,
                analysis=analysis,
                artifact=artifact,
                artifact_hash=artifact_hash,
                now_ms=published_at_ms,
            )
        except _MacroResearchLeaseLost:
            return _RunOutcome(
                run_rows_written=prepared.run_rows_written,
            )
        except Exception as exc:
            error_code = _error_code(exc)
            error_message = _error_message(exc)
            await asyncio.to_thread(
                self._mark_error,
                session_date=session_date,
                error_code=error_code,
                error_message=error_message,
                now_ms=max(int(self._clock_ms()), now_ms),
            )
            return _RunOutcome(
                model_calls=1,
                run_rows_written=prepared.run_rows_written + 1,
                error_code=error_code,
                error_message=error_message,
            )

        if not published:
            return _RunOutcome(
                model_calls=int(analysis.audit.model_calls),
                run_rows_written=prepared.run_rows_written,
            )
        return _RunOutcome(
            model_calls=int(analysis.audit.model_calls),
            run_rows_written=prepared.run_rows_written + 1,
            publication_rows_written=1,
            artifact_hash=artifact_hash,
        )

    async def _analyze_with_lease_heartbeat(
        self,
        scope: FrozenMacroEvidenceScope,
    ) -> MacroResearchAgentResult:
        analysis_task = asyncio.create_task(
            self._agent.analyze(scope),
            name=f"{self._worker_name}:analysis:{scope.scope_id}",
        )
        heartbeat_task = asyncio.create_task(
            self._renew_lease_until_cancelled(session_date=scope.session_date),
            name=f"{self._worker_name}:lease-heartbeat:{scope.scope_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                (analysis_task, heartbeat_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                await heartbeat_task
                raise RuntimeError("macro_research_lease_heartbeat_stopped")
            return await analysis_task
        finally:
            for task in (analysis_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                analysis_task,
                heartbeat_task,
                return_exceptions=True,
            )

    async def _renew_lease_until_cancelled(
        self,
        *,
        session_date: date,
    ) -> None:
        heartbeat_seconds = self._lease_ms() / 3_000
        while True:
            await asyncio.sleep(heartbeat_seconds)
            renewed = await asyncio.to_thread(
                self._renew_lease,
                session_date=session_date,
                now_ms=int(self._clock_ms()),
            )
            if not renewed:
                raise _MacroResearchLeaseLost("macro_research_lease_ownership_lost")

    async def _resolve_run_target(
        self,
        session_date: date | None,
    ) -> date:
        current_session = resolve_completed_session(
            now_ms=int(self._clock_ms()),
            settle_delay_seconds=self._settle_delay_seconds(),
        )
        if session_date is not None:
            if not is_us_market_session(session_date) or session_date > current_session:
                raise ValueError("macro_research_session_not_completed")
            return session_date
        state = await asyncio.to_thread(
            self._scheduling_state,
            through_date=current_session,
        )
        open_session = state["open_session"]
        if isinstance(open_session, date):
            return open_session
        latest_session = state["latest_session"]
        if isinstance(latest_session, date) and latest_session < current_session:
            candidate = next_market_session(latest_session)
            if candidate <= current_session:
                return candidate
        return current_session

    def _prepare_run(
        self,
        *,
        session_date: date,
        due_at_ms: int,
        now_ms: int,
    ) -> _PreparedRun:
        with self._repository_session() as repos, repos.transaction():
            repos.require_transaction(operation="macro_research_prepare")
            if repos.macro_research.publication_exists(session_date):
                return _PreparedRun(
                    claimed=None,
                    run_rows_written=0,
                )
            evidence_pack = repos.macro.evidence_pack(session_date=session_date)
            if evidence_pack is None:
                raise MacroResearchNotReady(
                    session_date=session_date,
                    reason="evidence_pack_missing",
                )
            evidence_cutoff_ms = int(evidence_pack["judgment_cutoff_ms"])
            inserted = repos.macro_research.ensure_run(
                session_date=session_date,
                market_cutoff_ms=evidence_cutoff_ms,
                evidence_pack_id=str(evidence_pack["evidence_pack_id"]),
                sealed_at_ms=now_ms,
                max_attempts=self._max_attempts(),
                due_at_ms=due_at_ms,
                now_ms=now_ms,
            )
            existing = repos.macro_research.run_record(session_date)
            if existing is None:
                raise RuntimeError("macro_research_run_missing_after_ensure")
            if int(existing["market_cutoff_ms"]) != evidence_cutoff_ms:
                raise MacroResearchIntegrityError("macro_research_run_cutoff_mismatch")
            claimed = repos.macro_research.claim_run(
                session_date=session_date,
                lease_owner=self._lease_owner,
                lease_ms=self._lease_ms(),
                now_ms=now_ms,
            )
            if claimed is None:
                return _PreparedRun(
                    claimed=None,
                    run_rows_written=int(inserted),
                )
            return _PreparedRun(
                claimed=claimed,
                run_rows_written=int(inserted) + 1,
            )

    def _read_state(
        self,
        *,
        session_date: date,
    ) -> dict[str, Any] | None:
        with self._repository_session() as repos, repos.transaction():
            return cast(
                "dict[str, Any] | None",
                repos.macro_research.research_state(session_date),
            )

    def _scheduling_state(
        self,
        *,
        through_date: date,
    ) -> dict[str, date | None]:
        with self._repository_session() as repos, repos.transaction():
            return cast(
                "dict[str, date | None]",
                repos.macro_research.scheduling_state(through_date=through_date),
            )

    def _publish(
        self,
        *,
        session_date: date,
        analysis: MacroResearchAgentResult,
        artifact: dict[str, Any],
        artifact_hash: str,
        now_ms: int,
    ) -> bool:
        with self._repository_session() as repos, repos.transaction():
            repos.require_transaction(operation="macro_research_publish")
            published = repos.macro_research.publish(
                session_date=session_date,
                lease_owner=self._lease_owner,
                artifact=artifact,
                report_markdown=analysis.report_markdown,
                audit=analysis.audit.model_dump(mode="json"),
                model_name=analysis.model_name,
                prompt_version=analysis.prompt_version,
                workflow_version=analysis.workflow_version,
                artifact_hash=artifact_hash,
                now_ms=now_ms,
            )
            if published:
                return True
            if repos.macro_research.publication_exists(session_date):
                return False
            raise RuntimeError("macro_research_publish_conflict")

    def _mark_error(
        self,
        *,
        session_date: date,
        error_code: str,
        error_message: str,
        now_ms: int,
    ) -> str:
        with self._repository_session() as repos, repos.transaction():
            repos.require_transaction(operation="macro_research_error")
            return str(
                repos.macro_research.mark_run_error(
                    session_date=session_date,
                    lease_owner=self._lease_owner,
                    error_code=error_code,
                    error_message=error_message,
                    retry_ms=self._retry_ms(),
                    now_ms=now_ms,
                )
            )

    def _renew_lease(
        self,
        *,
        session_date: date,
        now_ms: int,
    ) -> bool:
        with self._repository_session() as repos, repos.transaction():
            repos.require_transaction(operation="macro_research_renew_lease")
            return bool(
                repos.macro_research.renew_run_lease(
                    session_date=session_date,
                    lease_owner=self._lease_owner,
                    lease_ms=self._lease_ms(),
                    now_ms=now_ms,
                )
            )

    def _repository_session(self) -> Any:
        return self._db.worker_session(
            self._worker_name,
            statement_timeout_seconds=float(self._settings.statement_timeout_seconds),
        )

    def _settle_delay_seconds(self) -> int:
        return int(self._settings.settle_delay_seconds)

    def _lease_ms(self) -> int:
        return int(self._settings.lease_ms)

    def _retry_ms(self) -> int:
        return int(self._settings.retry_ms)

    def _max_attempts(self) -> int:
        return int(self._settings.max_attempts)


def resolve_completed_session(
    *,
    now_ms: int,
    settle_delay_seconds: int,
) -> date:
    instant = datetime.fromtimestamp(
        int(now_ms) / 1_000,
        tz=UTC,
    ).astimezone(_NEW_YORK)
    candidate = instant.date()
    settle_ms = int(settle_delay_seconds) * 1_000
    while True:
        if not is_us_market_session(candidate):
            candidate -= timedelta(days=1)
            continue
        if candidate == instant.date() and int(now_ms) < completed_session_close_ms(candidate) + settle_ms:
            candidate -= timedelta(days=1)
            continue
        return candidate


def completed_session_close_ms(session_date: date) -> int:
    if not is_us_market_session(session_date):
        raise ValueError(f"macro_research_market_session_required:{session_date.isoformat()}")
    close = datetime.combine(
        session_date,
        _session_close(session_date),
        tzinfo=_NEW_YORK,
    )
    return int(close.astimezone(UTC).timestamp() * 1_000)


def next_market_session(session_date: date) -> date:
    candidate = session_date
    while True:
        candidate += timedelta(days=1)
        if is_us_market_session(candidate):
            return candidate


def is_us_market_session(session_date: date) -> bool:
    return session_date.weekday() < 5 and session_date not in _us_market_holidays(session_date.year)


def _session_view(row: Mapping[str, Any]) -> MacroSessionView:
    artifact = row.get("artifact_json")
    audit = row.get("audit_json")
    return MacroSessionView(
        session_date=row["session_date"],
        status=str(row["run_status"]),
        market_cutoff_ms=int(row["market_cutoff_ms"]),
        evidence_pack_id=str(row["evidence_pack_id"]),
        sealed_at_ms=int(row["sealed_at_ms"]),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        due_at_ms=int(row["due_at_ms"]),
        artifact=dict(artifact) if isinstance(artifact, Mapping) else None,
        report_markdown=(str(row["report_markdown"]) if row.get("report_markdown") is not None else None),
        audit=dict(audit) if isinstance(audit, Mapping) else None,
        model_name=_optional_text(row.get("model_name")),
        prompt_version=_optional_text(row.get("prompt_version")),
        workflow_version=_optional_text(row.get("workflow_version")),
        artifact_hash=_optional_text(row.get("artifact_hash")),
        published_at_ms=(int(row["published_at_ms"]) if row.get("published_at_ms") is not None else None),
        last_error_code=_optional_text(row.get("last_error_code")),
        last_error_message=_optional_text(row.get("last_error_message")),
    )


def _validated_artifact(
    analysis: MacroResearchAgentResult,
    *,
    scope: FrozenMacroEvidenceScope,
) -> tuple[dict[str, Any], str]:
    if analysis.audit.scope_id != scope.scope_id:
        raise MacroResearchIntegrityError("macro_research_audit_scope_mismatch")
    artifact = require_artifact_integrity(
        analysis.artifact,
        scope=scope,
        verified_evidence_refs=frozenset(analysis.audit.verified_source_refs),
    )
    payload = artifact.model_dump(mode="json")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return payload, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _session_close(session_date: date) -> time:
    thanksgiving = _nth_weekday(
        session_date.year,
        11,
        weekday=3,
        occurrence=4,
    )
    early_close_days = {
        thanksgiving + timedelta(days=1),
        date(session_date.year, 7, 3),
        date(session_date.year, 12, 24),
    }
    if session_date in early_close_days and is_us_market_session(session_date):
        return _EARLY_CLOSE
    return _REGULAR_CLOSE


def _us_market_holidays(year: int) -> set[date]:
    holidays = {
        _observed_fixed_holiday(date(year, 1, 1)),
        _nth_weekday(year, 1, weekday=0, occurrence=3),
        _nth_weekday(year, 2, weekday=0, occurrence=3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, weekday=0),
        _observed_fixed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, weekday=0, occurrence=1),
        _nth_weekday(year, 11, weekday=3, occurrence=4),
        _observed_fixed_holiday(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed_fixed_holiday(date(year, 6, 19)))
    return holidays


def _observed_fixed_holiday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(
    year: int,
    month: int,
    *,
    weekday: int,
    occurrence: int,
) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, *, weekday: int) -> date:
    last = date(year, month, monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _error_code(exc: Exception) -> str:
    name = type(exc).__name__
    normalized = "".join(char.lower() if char.isalnum() else "_" for char in name).strip("_")
    return f"macro_research_{normalized or 'error'}"[:120]


def _error_message(exc: Exception) -> str:
    return str(exc or "macro research failed").replace("\n", " ")[:2_000]


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"macro_research_{field_name}_required")
    return normalized


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _now_ms() -> int:
    return int(wall_time.time() * 1_000)


__all__ = [
    "CompletedSessionMacro",
    "MacroResearchNotReady",
    "MacroSessionView",
    "completed_session_close_ms",
    "is_us_market_session",
    "next_market_session",
    "resolve_completed_session",
]
