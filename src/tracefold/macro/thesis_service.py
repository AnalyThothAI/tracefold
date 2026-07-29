from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from tracefold.macro.calculations import calculate_features
from tracefold.macro.domain import MACRO_MODULE_IDS
from tracefold.macro.module_payloads import build_typed_module_payload
from tracefold.macro.registry import DATASET_REGISTRY
from tracefold.macro.session_calendar import is_us_market_session
from tracefold.macro.thesis import (
    MACRO_THESIS_OUTCOME_DATASETS,
    MacroEvidencePackV3,
    MacroThesisAgent,
    MacroThesisReviewer,
    MacroThesisReviewFailure,
    compile_evidence_pack_v3,
    evaluate_live_delta,
    evaluate_outcome_replay,
    pending_outcome_replay,
    run_thesis_review_cycle,
)
from tracefold.macro.thesis_repository import publication_payload

_NEW_YORK = ZoneInfo("America/New_York")
_PUBLICATION_TIME = clock_time(8, 50)


@dataclass(frozen=True, slots=True)
class MacroThesisRunView:
    session_date: date
    status: str
    evidence_pack_id: str | None
    publication_id: str | None
    model_calls: int = 0
    reviews: int = 0
    publication_rows_written: int = 0
    live_delta_rows_written: int = 0
    outcome_rows_written: int = 0
    error_code: str | None = None
    error_message: str | None = None


class MacroThesisService:
    def __init__(
        self,
        *,
        db: Any,
        settings: Any,
        agent: MacroThesisAgent | None,
        reviewer: MacroThesisReviewer | None,
        configuration_error: str | None = None,
        backfill_worker_enabled: bool = False,
        worker_name: str = "macro_thesis",
        lease_owner: str | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if db is None:
            raise RuntimeError("macro_thesis_db_required")
        self._db = db
        self._settings = settings
        self._agent = agent
        self._reviewer = reviewer
        self._configuration_error = str(configuration_error or "").strip() or None
        self._backfill_worker_enabled = backfill_worker_enabled
        self._worker_name = worker_name
        self._lease_owner = lease_owner or f"{worker_name}:{uuid4().hex}"
        self._clock_ms = clock_ms or _now_ms

    async def run_due(self, *, now_ms: int | None = None) -> MacroThesisRunView:
        now = int(now_ms if now_ms is not None else self._clock_ms())
        session_date = resolve_thesis_session(now_ms=now)
        cutoff_ms = thesis_cutoff_ms(session_date)
        if now < cutoff_ms:
            return MacroThesisRunView(session_date, "not_due", None, None)

        existing = await asyncio.to_thread(self._state, session_date)
        if existing is not None and existing.get("thesis_json") is not None:
            live, outcome = await asyncio.to_thread(self._refresh_deterministic, existing, now)
            return MacroThesisRunView(
                session_date=session_date,
                status="published",
                evidence_pack_id=str(existing["evidence_pack_id"]),
                publication_id=str(existing["publication_id"]),
                live_delta_rows_written=live,
                outcome_rows_written=outcome,
            )
        if self._configuration_error is not None:
            state = await asyncio.to_thread(
                self._prepare_configuration_error,
                session_date=session_date,
                cutoff_ms=cutoff_ms,
                now_ms=now,
            )
            return _view_from_state(session_date, state)

        prepared = await asyncio.to_thread(
            self._prepare_and_claim,
            session_date=session_date,
            cutoff_ms=cutoff_ms,
            now_ms=now,
        )
        if prepared is None:
            state = await asyncio.to_thread(self._state, session_date)
            return _view_from_state(session_date, state)
        pack = MacroEvidencePackV3.model_validate(prepared["payload_json"])

        try:
            publication, reviews = await self._run_with_heartbeat(pack)
            written, live, outcome = await asyncio.to_thread(
                self._publish,
                evidence_pack=pack,
                publication=publication,
                reviews=reviews,
                now_ms=max(now, int(self._clock_ms())),
            )
            return MacroThesisRunView(
                session_date=session_date,
                status="published" if written else "exists",
                evidence_pack_id=pack.evidence_pack_id,
                publication_id=publication.publication_id,
                model_calls=len(reviews) * 2,
                reviews=len(reviews),
                publication_rows_written=int(written),
                live_delta_rows_written=live,
                outcome_rows_written=outcome,
            )
        except Exception as exc:
            error_code, retryable, terminal_status = _classify_error(exc)
            error_message = str(exc or "macro thesis failed").replace("\n", " ")[:2_000]
            error_at_ms = max(now, int(self._clock_ms()))
            if isinstance(exc, MacroThesisReviewFailure):
                await asyncio.to_thread(
                    self._record_reviews,
                    session_date=session_date,
                    reviews=exc.reviews,
                    created_at_ms=error_at_ms,
                )
            await asyncio.to_thread(
                self._mark_error,
                session_date=session_date,
                error_code=error_code,
                error_message=error_message,
                retryable=retryable,
                terminal_status=terminal_status,
                now_ms=error_at_ms,
            )
            return MacroThesisRunView(
                session_date=session_date,
                status="retryable" if retryable else terminal_status,
                evidence_pack_id=pack.evidence_pack_id,
                publication_id=None,
                error_code=error_code,
                error_message=error_message,
            )

    def _prepare_and_claim(
        self,
        *,
        session_date: date,
        cutoff_ms: int,
        now_ms: int,
    ) -> dict[str, Any] | None:
        existing = self._state(session_date)
        if existing is not None:
            pack_row = self._evidence_pack(str(existing["evidence_pack_id"]))
            if pack_row is None:
                raise RuntimeError("macro_thesis_run_pack_missing")
            with self._session() as repos, repos.transaction():
                claimed = repos.macro_thesis.claim_run(
                    session_date=session_date,
                    lease_owner=self._lease_owner,
                    lease_ms=self._lease_ms(),
                    now_ms=now_ms,
                )
            return pack_row if claimed is not None else None

        pack = self._build_pack(
            session_date=session_date,
            cutoff_ms=cutoff_ms,
            now_ms=now_ms,
        )
        with self._session() as repos, repos.transaction():
            repos.macro_thesis.insert_evidence_pack(pack)
            repos.macro_thesis.ensure_run(
                pack=pack,
                due_at_ms=cutoff_ms,
                max_attempts=self._max_attempts(),
                now_ms=now_ms,
            )
            claimed = repos.macro_thesis.claim_run(
                session_date=session_date,
                lease_owner=self._lease_owner,
                lease_ms=self._lease_ms(),
                now_ms=now_ms,
            )
        if claimed is None:
            return None
        return {
            "evidence_pack_id": pack.evidence_pack_id,
            "payload_json": pack.model_dump(mode="json"),
        }

    def _prepare_configuration_error(
        self,
        *,
        session_date: date,
        cutoff_ms: int,
        now_ms: int,
    ) -> dict[str, Any] | None:
        existing = self._state(session_date)
        if existing is None:
            pack = self._build_pack(
                session_date=session_date,
                cutoff_ms=cutoff_ms,
                now_ms=now_ms,
            )
            with self._session() as repos, repos.transaction():
                repos.macro_thesis.insert_evidence_pack(pack)
                repos.macro_thesis.ensure_run(
                    pack=pack,
                    due_at_ms=cutoff_ms,
                    max_attempts=self._max_attempts(),
                    now_ms=now_ms,
                )
                repos.macro_thesis.mark_configuration_error_before_attempt(
                    session_date=session_date,
                    error_code="macro_thesis_configuration_error",
                    error_message=self._configuration_error or "configuration_error",
                    now_ms=now_ms,
                )
        else:
            with self._session() as repos, repos.transaction():
                repos.macro_thesis.mark_configuration_error_before_attempt(
                    session_date=session_date,
                    error_code="macro_thesis_configuration_error",
                    error_message=self._configuration_error or "configuration_error",
                    now_ms=now_ms,
                )
        return self._state(session_date)

    def _build_pack(
        self,
        *,
        session_date: date,
        cutoff_ms: int,
        now_ms: int,
    ) -> MacroEvidencePackV3:
        modules = self._compile_modules(cutoff_ms=cutoff_ms)
        prior = self._prior_publication(session_date)
        prior_pack = (
            self._evidence_pack(str(prior["evidence_pack_id"]))
            if prior is not None and prior.get("evidence_pack_id")
            else None
        )
        return compile_evidence_pack_v3(
            session_date=session_date,
            cutoff_ms=cutoff_ms,
            sealed_at_ms=max(now_ms, cutoff_ms),
            modules=modules,
            prior_publication=prior,
            prior_evidence_pack=(
                dict(prior_pack["payload_json"])
                if prior_pack is not None and isinstance(prior_pack.get("payload_json"), dict)
                else None
            ),
        )

    def _compile_modules(self, *, cutoff_ms: int) -> tuple[dict[str, Any], ...]:
        all_specs = tuple(DATASET_REGISTRY.values())
        series_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "series")
        market_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "market_observation")
        position_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "market_position")
        settlement_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "market_settlement")
        release_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "release")
        document_ids = tuple(spec.dataset_id for spec in all_specs if spec.fact_family == "document")
        with self._session() as repos:
            series_rows = repos.macro.series_history(
                dataset_ids=series_ids,
                limit_per_dataset=10_000,
                received_before_ms=cutoff_ms,
            )
            market_rows = repos.macro_market.market_history(
                dataset_ids=market_ids,
                limit_per_dataset=5_000,
                received_before_ms=cutoff_ms,
            )
            position_rows = repos.macro_market.position_history(
                dataset_ids=position_ids,
                received_before_ms=cutoff_ms,
            )
            settlement_rows = repos.macro_market.settlement_history(
                dataset_ids=settlement_ids,
                received_before_ms=cutoff_ms,
            )
            release_rows = repos.macro.release_history(
                dataset_ids=release_ids,
                received_before_ms=cutoff_ms,
            )
            document_rows = repos.macro.document_history(
                dataset_ids=document_ids,
                received_before_ms=cutoff_ms,
            )
            role_rows = repos.macro.fed_official_role_history(received_before_ms=cutoff_ms)
            analysis_rows = repos.macro.document_analysis_history(received_before_ms=cutoff_ms)
            target_states = repos.macro.receipt_states_at(cutoff_ms=cutoff_ms)
        features = calculate_features(series_rows)
        modules = []
        for module_id in MACRO_MODULE_IDS:
            module = build_typed_module_payload(
                module_id=module_id,
                now_ms=cutoff_ms,
                series_rows=series_rows,
                market_rows=market_rows,
                position_rows=position_rows,
                settlement_rows=settlement_rows,
                release_rows=release_rows,
                document_rows=document_rows,
                target_states=target_states,
                role_rows=role_rows,
                analysis_rows=analysis_rows,
                analysis_job_state=None,
                backfill_worker_enabled=self._backfill_worker_enabled,
            )
            module["features"] = [feature for feature in features if feature.get("module_id") == module_id]
            modules.append(module)
        return tuple(modules)

    async def _run_with_heartbeat(
        self,
        pack: MacroEvidencePackV3,
    ) -> tuple[Any, Any]:
        if self._agent is None or self._reviewer is None:
            raise RuntimeError("macro_thesis_configuration_error")
        analysis = asyncio.create_task(
            run_thesis_review_cycle(
                evidence_pack=pack,
                agent=self._agent,
                reviewer=self._reviewer,
                published_at_ms=max(int(self._clock_ms()), pack.cutoff_ms),
            )
        )
        heartbeat = asyncio.create_task(self._heartbeat(pack.session_date))
        try:
            done, _ = await asyncio.wait((analysis, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat
                raise RuntimeError("macro_thesis_lease_heartbeat_stopped")
            return await analysis
        finally:
            for task in (analysis, heartbeat):
                if not task.done():
                    task.cancel()
            await asyncio.gather(analysis, heartbeat, return_exceptions=True)

    async def _heartbeat(self, session_date: date) -> None:
        while True:
            await asyncio.sleep(max(1.0, self._lease_ms() / 3_000))
            renewed = await asyncio.to_thread(
                self._renew_lease,
                session_date=session_date,
                now_ms=int(self._clock_ms()),
            )
            if not renewed:
                raise RuntimeError("macro_thesis_lease_lost")

    def _publish(
        self,
        *,
        evidence_pack: MacroEvidencePackV3,
        publication: Any,
        reviews: Any,
        now_ms: int,
    ) -> tuple[bool, int, int]:
        with self._session() as repos, repos.transaction():
            for index, review in enumerate(reviews, start=1):
                repos.macro_thesis.record_review(
                    session_date=publication.session_date,
                    review=review,
                    review_sequence=index,
                    created_at_ms=now_ms,
                )
            written = repos.macro_thesis.publish(
                publication=publication,
                lease_owner=self._lease_owner,
            )
            delta = evaluate_live_delta(
                publication=publication,
                modules=evidence_pack.modules,
                evaluated_at_ms=now_ms,
            )
            live = repos.macro_thesis.insert_live_delta(delta)
            outcome = repos.macro_thesis.insert_outcome_replay(
                pending_outcome_replay(publication=publication, evaluated_at_ms=now_ms)
            )
        return written, live, outcome

    def _refresh_deterministic(self, state: dict[str, Any], now_ms: int) -> tuple[int, int]:
        from tracefold.macro.thesis import MacroThesisV1

        publication = MacroThesisV1.model_validate(state["thesis_json"])
        with self._session() as repos, repos.transaction():
            modules = [dict(row["payload_json"]) for row in repos.macro.all_modules_current()]
            market_rows = repos.macro_market.market_history(
                dataset_ids=tuple(
                    dataset_id for dataset_id in MACRO_THESIS_OUTCOME_DATASETS if dataset_id != "fred.vixcls"
                ),
                limit_per_dataset=5_000,
                received_before_ms=now_ms,
            )
            vix_rows = repos.macro.series_history(
                dataset_ids=("fred.vixcls",),
                limit_per_dataset=5_000,
                received_before_ms=now_ms,
            )
            market_rows.extend(
                {
                    **row,
                    "observed_at_ms": int(
                        datetime.combine(
                            row["reference_date"],
                            clock_time(21, 0),
                            tzinfo=UTC,
                        ).timestamp()
                        * 1_000
                    ),
                }
                for row in vix_rows
            )
            live = repos.macro_thesis.insert_live_delta(
                evaluate_live_delta(
                    publication=publication,
                    modules=modules,
                    evaluated_at_ms=now_ms,
                )
            )
            outcome = repos.macro_thesis.insert_outcome_replay(
                evaluate_outcome_replay(
                    publication=publication,
                    market_rows=market_rows,
                    evaluated_at_ms=now_ms,
                )
            )
        return live, outcome

    def _mark_error(self, **kwargs: Any) -> None:
        with self._session() as repos, repos.transaction():
            repos.macro_thesis.mark_error(
                lease_owner=self._lease_owner,
                retry_ms=self._retry_ms(),
                **kwargs,
            )

    def _record_reviews(
        self,
        *,
        session_date: date,
        reviews: Sequence[Any],
        created_at_ms: int,
    ) -> None:
        with self._session() as repos, repos.transaction():
            for index, review in enumerate(reviews, start=1):
                repos.macro_thesis.record_review(
                    session_date=session_date,
                    review=review,
                    review_sequence=index,
                    created_at_ms=created_at_ms,
                )

    def _renew_lease(self, *, session_date: date, now_ms: int) -> bool:
        with self._session() as repos, repos.transaction():
            return bool(
                repos.macro_thesis.renew_lease(
                    session_date=session_date,
                    lease_owner=self._lease_owner,
                    lease_ms=self._lease_ms(),
                    now_ms=now_ms,
                )
            )

    def _state(self, session_date: date) -> dict[str, Any] | None:
        with self._session() as repos:
            return cast(dict[str, Any] | None, repos.macro_thesis.state(session_date))

    def _evidence_pack(self, evidence_pack_id: str) -> dict[str, Any] | None:
        with self._session() as repos:
            return cast(dict[str, Any] | None, repos.macro_thesis.evidence_pack(evidence_pack_id))

    def _prior_publication(self, session_date: date) -> dict[str, Any] | None:
        with self._session() as repos:
            return publication_payload(repos.macro_thesis.prior_publication(session_date))

    def _session(self) -> Any:
        return self._db.worker_session(
            self._worker_name,
            statement_timeout_seconds=float(self._settings.statement_timeout_seconds),
        )

    def _lease_ms(self) -> int:
        return int(self._settings.lease_ms)

    def _retry_ms(self) -> int:
        return int(self._settings.retry_ms)

    def _max_attempts(self) -> int:
        return int(self._settings.max_attempts)


def resolve_thesis_session(*, now_ms: int) -> date:
    instant = datetime.fromtimestamp(int(now_ms) / 1_000, tz=_NEW_YORK)
    candidate = instant.date()
    if is_us_market_session(candidate) and instant.timetz().replace(tzinfo=None) >= _PUBLICATION_TIME:
        return candidate
    candidate -= timedelta(days=1)
    while not is_us_market_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def thesis_cutoff_ms(session_date: date) -> int:
    if not is_us_market_session(session_date):
        raise ValueError(f"macro_thesis_market_session_required:{session_date.isoformat()}")
    return int(datetime.combine(session_date, _PUBLICATION_TIME, tzinfo=_NEW_YORK).timestamp() * 1_000)


def _classify_error(exc: Exception) -> tuple[str, bool, str]:
    message = str(exc or "").lower()
    name = type(exc).__name__.lower()
    if "reviewer_block" in message or "not_passed_after_revision" in message:
        return "macro_thesis_reviewer_block", False, "not_published"
    if "graphrecursionerror" in name or "recursion limit" in message:
        return "macro_thesis_agent_step_limit", False, "not_published"
    if any(
        token in message
        for token in (
            "unsupported_model",
            "model_not_found",
            "invalid_api_key",
            "authentication",
            "permission_denied",
            "thinking mode does not support this tool_choice",
        )
    ):
        return "macro_thesis_configuration_error", False, "config_error"
    retryable = any(
        token in f"{name}:{message}"
        for token in ("timeout", "ratelimit", "rate_limit", "connection", "serviceunavailable", "temporar")
    )
    normalized = "".join(character if character.isalnum() else "_" for character in name).strip("_")
    return f"macro_thesis_{normalized or 'error'}"[:120], retryable, "failed"


def _view_from_state(session_date: date, state: dict[str, Any] | None) -> MacroThesisRunView:
    if state is None:
        return MacroThesisRunView(session_date, "missing", None, None)
    return MacroThesisRunView(
        session_date=session_date,
        status=str(state["status"]),
        evidence_pack_id=str(state["evidence_pack_id"]),
        publication_id=str(state["publication_id"]) if state.get("publication_id") else None,
        error_code=str(state["last_error_code"]) if state.get("last_error_code") else None,
        error_message=str(state["last_error_message"]) if state.get("last_error_message") else None,
    )


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = [
    "MacroThesisRunView",
    "MacroThesisService",
    "resolve_thesis_session",
    "thesis_cutoff_ms",
]
