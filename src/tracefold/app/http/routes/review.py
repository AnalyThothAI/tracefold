from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response

from tracefold.news import (
    REVIEW_DEFAULT_HOURS,
    REVIEW_MAX_HOURS,
    BlindPairwiseSubmission,
    DeskQuery,
    EventRubricSubmission,
    ExternalMissSubmission,
    Principal,
    ReviewDesk,
    TaskRef,
)

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest, ApiConflict
from ..responses import _etagged, _validated_etag_json
from ..schemas import common as api_schemas
from ..schemas import review as review_schemas

router = APIRouter()
_ReviewEnvelope = api_schemas.ApiEnvelope[review_schemas.NewsReviewData]
_ReviewEvidenceEnvelope = api_schemas.ApiEnvelope[review_schemas.NewsReviewEvidenceData]
_ReviewSubmitEnvelope = api_schemas.ApiEnvelope[review_schemas.NewsReviewSubmitData]


def _review_mutation_runtime(request: Request) -> Any:
    """Reject an invalid mutation envelope before FastAPI parses its body."""

    _validate_query_params(request, supported=set())
    _validate_review_content_type(request)
    _validate_review_body_size(request)
    return _authenticated_runtime(request, allow_query_token=False)


@router.get("/news/review", response_model=_ReviewEnvelope)
def get_news_review(
    request: Request,
    view: Annotated[str, Query(pattern="^(queue|coverage|proposals|market)$")] = "queue",
    mode: Annotated[str, Query(pattern="^(event|pairwise)$")] = "event",
    cohort: Annotated[str, Query(max_length=160)] = "",
    stratum: Annotated[str, Query(max_length=64)] = "",
    proposal: Annotated[str, Query(max_length=128)] = "",
    task: Annotated[str, Query(max_length=300)] = "",
    event: Annotated[str, Query(max_length=128)] = "",
    status: Annotated[str, Query(pattern="^(pending|accepted|all)$")] = "pending",
    hours: Annotated[int, Query(ge=1, le=REVIEW_MAX_HOURS)] = REVIEW_DEFAULT_HOURS,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str, Query(max_length=300)] = "",
) -> Response:
    """Learning ReviewDesk: actionable queue, evidence coverage, proposals, or market observations."""

    _validate_query_params(
        request,
        supported={
            "view",
            "mode",
            "cohort",
            "stratum",
            "proposal",
            "task",
            "event",
            "status",
            "hours",
            "limit",
            "cursor",
            "token",
        },
    )
    runtime = _authenticated_runtime(request)
    query = DeskQuery(
        view=view,
        mode=mode,
        cohort=cohort,
        stratum=stratum,
        proposal=proposal,
        task=task,
        event=event,
        status=status,
        hours=hours,
        limit=limit,
        cursor=cursor,
    )
    with runtime.repositories() as repos:
        try:
            data = ReviewDesk(repos.conn).open(query, principal=_review_principal())
        except ValueError as exc:
            raise ApiBadRequest(str(exc)) from exc
    return _etagged(data, request, envelope=_ReviewEnvelope)


@router.get("/news/review/tasks/{task_id}/evidence", response_model=_ReviewEvidenceEnvelope)
def get_news_review_evidence(
    request: Request,
    task_id: str,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    task_ref = TaskRef(task_id=task_id, task_version=_required_match(if_match))
    with runtime.repositories() as repos:
        try:
            data = ReviewDesk(repos.conn).evidence(task_ref, principal=_review_principal())
        except ValueError as exc:
            if str(exc) == "news_review_task_version_conflict":
                raise ApiConflict(str(exc)) from exc
            raise ApiBadRequest(str(exc)) from exc
    return _etagged(data, request, envelope=_ReviewEvidenceEnvelope)


@router.post("/news/review/tasks/{task_id}/responses", response_model=_ReviewSubmitEnvelope)
def submit_news_review(
    request: Request,
    task_id: str,
    body: EventRubricSubmission | BlindPairwiseSubmission,
    runtime: Annotated[Any, Depends(_review_mutation_runtime)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    task_ref = TaskRef(task_id=task_id, task_version=_required_match(if_match))
    key = _required_idempotency_key(idempotency_key)
    try:
        with runtime.review_transaction() as conn:
            data = ReviewDesk(conn).submit(
                task_ref,
                body,
                principal=_review_principal(),
                idempotency_key=key,
            )
    except ValueError as exc:
        if str(exc) in {"news_review_task_version_conflict", "news_review_idempotency_conflict"}:
            raise ApiConflict(str(exc)) from exc
        raise ApiBadRequest(str(exc)) from exc
    return _validated_etag_json(_ReviewSubmitEnvelope, {"ok": True, "data": data}, data=data, request=request)


@router.post("/news/review/external-misses", response_model=_ReviewSubmitEnvelope)
def submit_news_external_miss(
    request: Request,
    body: ExternalMissSubmission,
    runtime: Annotated[Any, Depends(_review_mutation_runtime)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    key = _required_idempotency_key(idempotency_key)
    try:
        with runtime.review_transaction() as conn:
            data = ReviewDesk(conn).submit(
                None,
                body,
                principal=_review_principal(),
                idempotency_key=key,
            )
    except ValueError as exc:
        if str(exc) == "news_review_idempotency_conflict":
            raise ApiConflict(str(exc)) from exc
        raise ApiBadRequest(str(exc)) from exc
    return _validated_etag_json(_ReviewSubmitEnvelope, {"ok": True, "data": data}, data=data, request=request)


def _review_principal() -> Principal:
    # V1 has one authenticated operator.  We keep that honest instead of
    # inventing reviewer independence from a shared bearer token.
    return Principal(subject="operator")


def _required_match(value: str | None) -> str:
    raw = str(value or "").strip()
    if len(raw) != 66 or not raw.startswith('"') or not raw.endswith('"'):
        raise ApiBadRequest("news_review_if_match_required", field="If-Match")
    version = raw[1:-1]
    if len(version) != 64 or any(char not in "0123456789abcdef" for char in version):
        raise ApiBadRequest("news_review_if_match_invalid", field="If-Match")
    return version


def _required_idempotency_key(value: str | None) -> str:
    raw = str(value or "").strip()
    try:
        return str(UUID(raw))
    except ValueError as exc:
        raise ApiBadRequest("news_review_idempotency_key_invalid", field="Idempotency-Key") from exc


def _validate_review_body_size(request: Request) -> None:
    raw = request.headers.get("content-length")
    if raw is None:
        raise ApiBadRequest("news_review_content_length_required")
    try:
        size = int(raw)
    except ValueError as exc:
        raise ApiBadRequest("news_review_content_length_invalid") from exc
    if size < 0 or size > 32_768:
        raise ApiBadRequest("news_review_body_too_large")


def _validate_review_content_type(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise ApiBadRequest("news_review_content_type_invalid", field="Content-Type")


__all__ = ["router"]
