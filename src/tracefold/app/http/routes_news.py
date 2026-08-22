from __future__ import annotations

import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http import schemas_news
from tracefold.app.http.dependencies import _authenticated_runtime
from tracefold.app.http.exceptions import ApiBadRequest, ApiConflict
from tracefold.app.http.responses import _json, _validated_etag_json
from tracefold.app.workers.runtime import WorkersRuntimeRepository, workers_runtime_status
from tracefold.news import (
    QUOTE_REQUEST_SYMBOL_MAX,
    REVIEW_DEFAULT_HOURS,
    REVIEW_MAX_HOURS,
    BlindPairwiseSubmission,
    DeskQuery,
    EventRubricSubmission,
    ExternalMissSubmission,
    Principal,
    ReviewDesk,
    TaskRef,
    grounding_rollup,
    status_health,
)
from tracefold.platform.config.settings import news_model_availability, news_push_availability

router = APIRouter()
_FeedEnvelope = api_schemas.ApiEnvelope[schemas_news.NewsFeedData]
_EventEnvelope = api_schemas.ApiEnvelope[schemas_news.NewsEventDetailData]
_StatusEnvelope = api_schemas.ApiEnvelope[schemas_news.NewsStatusData]
_QuotesEnvelope = api_schemas.ApiEnvelope[schemas_news.NewsQuotesData]
_ReviewEnvelope = api_schemas.ApiEnvelope[schemas_news.NewsReviewData]
_ReviewEvidenceEnvelope = api_schemas.ApiEnvelope[schemas_news.NewsReviewEvidenceData]
_ReviewSubmitEnvelope = api_schemas.ApiEnvelope[schemas_news.NewsReviewSubmitData]

_ADMISSIONS = {
    "candidate",
    "listing_deterministic",
    "suppressed_pr_template",
    "suppressed_low_signal",
    "recovery",
}
_DECISIONS = {"push", "escalate", "drop", "throttled", "degraded"}


def _review_mutation_runtime(request: Request) -> Any:
    """Reject an invalid mutation envelope before FastAPI parses its body."""

    _validate_query_params(request, supported=set())
    _validate_review_content_type(request)
    _validate_review_body_size(request)
    return _authenticated_runtime(request, allow_query_token=False)


@router.get("/news/feed", response_model=_FeedEnvelope)
def get_news_feed(
    request: Request,
    family: Annotated[str, Query(max_length=32)] = "",
    admission: Annotated[str, Query(max_length=40)] = "",
    priority: Annotated[str, Query(pattern="^(high|normal)?$")] = "",
    decision: Annotated[str, Query(max_length=16)] = "",
    symbol: Annotated[str, Query(max_length=16)] = "",
    q: Annotated[str, Query(max_length=200)] = "",
    sort: Annotated[str, Query(pattern="^(latest|priority)$")] = "latest",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str, Query(max_length=200)] = "",
    outcome: Annotated[str, Query(pattern="^(pushed|held|pending)?$")] = "",
    hours: Annotated[int, Query(ge=0, le=168)] = 0,
) -> Response:
    _validate_query_params(
        request,
        supported={
            "family",
            "admission",
            "priority",
            "decision",
            "symbol",
            "q",
            "sort",
            "limit",
            "cursor",
            "outcome",
            "hours",
            "token",
        },
    )
    if admission and admission not in _ADMISSIONS:
        raise ApiBadRequest("news_feed_admission_invalid", field="admission")
    if decision and decision not in _DECISIONS:
        raise ApiBadRequest("news_feed_decision_invalid", field="decision")
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        try:
            data = repos.news.list_feed(
                family=family or None,
                admission=admission or None,
                priority=priority or None,
                decision=decision or None,
                symbol=symbol or None,
                q=q or None,
                sort=sort,
                limit=limit,
                cursor=cursor or None,
                outcome=outcome or None,
                hours=hours or None,
            )
        except ValueError as exc:
            # Only `list_feed` decodes the cursor. Anything that fails while resolving instruments is a
            # server fault and must not come back as a 400 naming a field the caller got right (#87 review).
            raise ApiBadRequest(str(exc), field="cursor") from exc
        _attach_asset_refs(data["events"], repos.instruments)
        _attach_reactions(data["events"], repos.price, now_ms=int(time.time() * 1000))
    return _etagged(data, request, envelope=_FeedEnvelope)


@router.get("/news/events/{event_id}", response_model=_EventEnvelope)
def get_news_event(request: Request, event_id: str) -> Response:
    _validate_query_params(request, supported={"token"})
    if not event_id or len(event_id) > 128:
        raise ApiBadRequest("news_event_id_invalid", field="event_id")
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        data = repos.news.event_detail(event_id)
        if data is not None:
            _attach_asset_refs([data["event"]], repos.instruments)
            data["normalization"] = _normalization(data["event"], repos.instruments)
            now_ms = int(time.time() * 1000)
            data["reactions"] = repos.price.event_reactions(event_id)
            data["reaction"] = repos.price.event_reaction_aggregates([event_id], now_ms=now_ms).get(event_id)
    if data is None:
        return _json({"ok": False, "error": "news_event_not_found"}, status_code=404)
    return _etagged(data, request, envelope=_EventEnvelope)


@router.get("/news/quotes", response_model=_QuotesEnvelope)
def get_news_quotes(
    request: Request,
    symbols: Annotated[str, Query(max_length=2000)] = "",
) -> Response:
    """Current quotes for a bounded symbol batch (#88).

    Deliberately not part of `/api/news/feed`: a price that changes every few seconds would invalidate the
    feed's ETag on every poll and drag the feed and count queries along with it. The browser derives this
    batch from the `assets[]` the feed already returned, so one query serves every row on screen.
    """

    _validate_query_params(request, supported={"symbols", "token"})
    requested = _requested_symbols(symbols)
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        quotes = repos.price.quotes_for_symbols(requested, now_ms=now_ms)
    return _etagged({"quotes": quotes, "measured_at_ms": now_ms}, request, envelope=_QuotesEnvelope)


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


@router.get("/news/status", response_model=_StatusEnvelope)
def get_news_status(request: Request) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    settings = runtime.settings
    with runtime.repositories() as repos:
        snapshot = repos.news.status_snapshot(now_ms=now_ms)
        workers_state, _ = _news_workers_observation(repos.conn, now_ms=now_ms)
        instruments = repos.instruments.universe_summary()
        # #87: each repository answers only over its own tables and the fold happens here — News knows which
        # tags an Event carried, the instrument universe knows which of them name something listed.
        usage = repos.news.asset_usage_24h(now_ms=now_ms)
        price = repos.price.price_status(now_ms=now_ms)
        grounding = grounding_rollup(
            usage,
            repos.instruments.asset_refs({symbol for symbols in usage.values() for symbol in symbols}),
        )
    push = news_push_availability(settings)
    models = news_model_availability(settings)
    observed = dict(snapshot.get("broker") or {})
    broker_data = {
        "configured": bool(settings.news.broker.url),
        "connected": observed.get("connected"),
        "queues": {str(k): v for k, v in (observed.get("queues") or {}).items()},
        "error_code": observed.get("error_code"),
        "observed_at_ms": observed.get("observed_at_ms"),
    }
    ingest = {**snapshot["ingest"], "token_configured": bool(settings.news.opennews_token)}
    pipeline = {
        **snapshot["pipeline"],
        **grounding,
        "triage_model": models.triage_model,
        "reader_card_model": models.reader_card_model,
        "reader_card_dedicated": models.reader_card_dedicated,
        "triage_fallback_model": models.triage_fallback_model,
        "reader_card_fallback_model": models.reader_card_fallback_model,
        "reader_card_fallback_dedicated": models.reader_card_fallback_dedicated,
    }
    delivery = {
        **snapshot["delivery"],
        "delivery_available": push.delivery_available,
    }
    state = _derive_state(ingest=ingest, broker=broker_data, workers_state=workers_state, settings=settings)
    control = {
        "paused": bool(snapshot["control"].get("paused")),
        "mutes": list(snapshot["control"].get("mutes") or []),
    }
    health = status_health(
        ingest=ingest,
        broker=broker_data,
        pipeline=pipeline,
        delivery=delivery,
        control=control,
        workers_state=workers_state,
        now_ms=now_ms,
        enabled=bool(settings.news.enabled),
        model_configured=models.program_configured,
    )
    data = {
        "state": state,
        "workers_state": workers_state,
        **health,
        "ingest": ingest,
        "broker": broker_data,
        "pipeline": pipeline,
        "delivery": delivery,
        "learning_retention": snapshot["learning_retention"],
        "control": control,
        "watchlist": sorted(settings.news.watchlist_symbols),
        "instruments": instruments,
        "price": price,
        "measured_at_ms": now_ms,
    }
    return _validated_etag_json(
        _StatusEnvelope,
        {"ok": True, "data": data},
        data=data,
        etag_data=_status_etag_basis(data),
        request=request,
        weak=True,
    )


def _attach_asset_refs(events: list[dict[str, Any]], instruments: Any) -> None:
    """Resolve every Event's provider coin tags against the instrument universe, in one batch (#87).

    Assembly lives in the route because the two halves have different owners: `NewsRepository` reads the tags off
    `news_events`, `InstrumentsRepository` reads what they name. One round trip per response, not one per Event.
    """

    refs = instruments.asset_refs({symbol for event in events for symbol in (event.get("grounded_assets") or [])})
    for event in events:
        # One entry per instrument named, not per tag: the provider ships both `CL` and `XYZ-CL` for the same
        # contract, and once those resolve they are byte-identical. The browser happens to dedupe before
        # rendering, but a payload that hands out the same chip twice is the API's fault, not the client's.
        seen: set[str] = set()
        assets: list[dict[str, Any]] = []
        for symbol in event.get("grounded_assets") or []:
            # Keyed by the raw tag, exactly as `asset_refs` returns it — upper-casing here would miss a
            # lower-case tag and silently render it as naming nothing.
            ref = refs.get(str(symbol)) or {
                "symbol": str(symbol).upper(),
                "base_symbol": str(symbol).upper(),
                "venue": None,
                "listed": False,
            }
            if str(ref["symbol"]) in seen:
                continue
            seen.add(str(ref["symbol"]))
            assets.append(ref)
        event["assets"] = assets


def _requested_symbols(raw: str) -> list[str]:
    """A deduplicated, bounded symbol list. The server deduplicates again so a noisy client cannot amplify work."""

    out: list[str] = []
    for part in str(raw or "").split(","):
        symbol = part.strip()
        if not symbol:
            continue
        if len(symbol) > 32:
            raise ApiBadRequest("news_quotes_symbol_invalid", field="symbols")
        if symbol not in out:
            out.append(symbol)
    if len(out) > QUOTE_REQUEST_SYMBOL_MAX:
        raise ApiBadRequest("news_quotes_symbols_too_many", field="symbols")
    return out


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


def _attach_reactions(events: list[dict[str, Any]], price: Any, *, now_ms: int) -> None:
    """One bounded batch for the whole page: at most `limit` Event ids, never one query per row."""

    aggregates = price.event_reaction_aggregates([event["event_id"] for event in events], now_ms=now_ms)
    for event in events:
        event["reaction"] = aggregates.get(str(event["event_id"]))


def _normalization(event: dict[str, Any], instruments: Any) -> list[dict[str, Any]]:
    """The alias groups this Event's assets fall into — only the ones that actually collapse something.

    A base that answers to exactly one name tells the reader nothing; the block exists to explain why several
    contracts share one storyline bucket.

    Venue-derived aliases are excluded (#87 review). `learn_aliases_from_universe` writes an `XYZ-{base}` row
    for every builder-DEX base and a `dex:SYMBOL` form besides, so counting those would fire the block on
    routine commodity and index Events — `GOLD XAU XAUT XYZ-GOLD -> GOLD` explains nothing a reader did not
    already assume. What is worth a row is the operator-owned collapse the storyline identity depends on:
    SKHY / SKHX / SKHYNIX.
    """

    bases = {str(asset["base_symbol"]) for asset in event.get("assets") or []}
    groups = instruments.aliases_by_base(bases, sources=("seed",))
    return [group for _, group in sorted(groups.items()) if len(group.get("aliases") or []) > 1]


def _derive_state(*, ingest: dict[str, Any], broker: dict[str, Any], workers_state: str | None, settings: Any) -> str:
    if not settings.news.enabled or not settings.news.opennews_token or not settings.news.broker.url:
        return "unavailable"
    if workers_state != "running":
        return "warming" if workers_state in {None, "recovering"} else "degraded"
    if broker.get("connected") is False or ingest.get("open_incidents"):
        return "degraded"
    if not ingest.get("connected"):
        return "warming"
    return "ready"


def _news_workers_observation(conn: Any, *, now_ms: int) -> tuple[str | None, str | None]:
    row = WorkersRuntimeRepository(conn).read()
    if row is None:
        return None, None
    status = workers_runtime_status(row, now_ms=now_ms)
    state = str(status["state"])
    if state == "running":
        return "running", None
    if state in {"starting", "stopping"}:
        return "recovering", f"workers_runtime_{state}"
    if state == "stale":
        return "stalled", "workers_runtime_heartbeat_stale"
    if state in {"stopped", "failed"}:
        return "stalled", f"workers_runtime_{state}"
    raise RuntimeError("news_workers_runtime_state_invalid")


def _etagged(data: dict[str, Any], request: Request, *, envelope: type[BaseModel]) -> JSONResponse | Response:
    return _validated_etag_json(envelope, {"ok": True, "data": data}, data=data, request=request)


def _validate_query_params(request: Request, *, supported: set[str]) -> None:
    for name in request.query_params:
        if name not in supported:
            raise ApiBadRequest("unsupported_query_param", field=name)


def _status_etag_basis(data: dict[str, Any]) -> dict[str, Any]:
    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key not in {"measured_at_ms"}}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    return stable(data)


__all__ = ["router"]
