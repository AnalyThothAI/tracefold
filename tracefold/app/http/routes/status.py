from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from tracefold.app.workers.runtime import workers_runtime_status
from tracefold.news.health import status_health
from tracefold.news.market_review.instruments import grounding_rollup
from tracefold.platform.config.models import news_model_availability, news_push_availability

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..responses import _validated_etag_json
from ..schemas import common as api_schemas
from ..schemas import status as status_schemas

router = APIRouter()
_StatusEnvelope = api_schemas.ApiEnvelope[status_schemas.NewsStatusData]


@router.get("/news/status", response_model=_StatusEnvelope)
def get_news_status(request: Request) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    settings = runtime.settings
    oi_policy = settings.news.oi
    with runtime.repositories() as repos:
        snapshot = repos.news.status_snapshot(now_ms=now_ms)
        # The live window, read under the running thresholds. It has to be a read rather than a fold over the
        # 24 h counters: rank is spent per symbol inside a sliding 4 h window, and only the rows still inside
        # it say which symbol the next frame will find full.
        occupancy = repos.news.oi_window_occupancy(
            now_ms=now_ms,
            window_ms=oi_policy.window_ms,
            whale_oi_ratio_above_bps=oi_policy.whale_oi_ratio_above_bps,
            oi_change_at_least_bps=oi_policy.oi_change_at_least_bps,
        )
        workers_state, _ = _news_workers_observation(repos.workers_runtime_row(), now_ms=now_ms)
        instruments = repos.instruments.universe_summary()
        # #87: each repository answers only over its own tables and the fold happens here — News knows which
        # tags an Event carried, the instrument universe knows which of them name something listed.
        usage = repos.news.asset_usage_24h(now_ms=now_ms)
        price = repos.price.price_status(now_ms=now_ms)
        grounding = grounding_rollup(
            usage,
            repos.instruments.asset_refs({symbol for symbols in usage.values() for symbol in symbols}),
        )
    # Serve receives no provider credential. It can validate the declared target contract, while Workers
    # owns the secure-file check and provider preflight. A declared target is not callable when Workers is
    # absent or failed, so the public status must not present that state as delivery-ready.
    push = news_push_availability(settings, inspect_secret_file=False)
    models = news_model_availability(settings)
    observed = dict(snapshot.get("broker") or {})
    broker_data = {
        "configured": bool(settings.news.broker.url),
        "connected": observed.get("connected"),
        "queues": {str(k): v for k, v in (observed.get("queues") or {}).items()},
        "error_code": observed.get("error_code"),
        "observed_at_ms": observed.get("observed_at_ms"),
        "last_publish_error_code": observed.get("last_publish_error_code"),
        "last_publish_error_at_ms": observed.get("last_publish_error_at_ms"),
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
        "delivery_available": push.delivery_available and workers_state == "running",
    }
    # No `trade_floors` here (#331). News republished the capital lane's thresholds beside its own
    # gates, which invited a console to compare a Case frozen last week against a floor edited
    # yesterday. The capital lane's rules belong to `/api/trading/*`, and the ones that decided a Case
    # travel with that Case as frozen evidence.
    oi = {
        **snapshot["oi"],
        "policy": oi_policy.model_dump(),
        "window_occupancy": [
            {
                "symbol": row["symbol"],
                "used": int(row["used"]),
                "max_rank_in_window": oi_policy.max_rank_in_window,
                "full": int(row["used"]) >= oi_policy.max_rank_in_window,
            }
            for row in occupancy
        ],
    }
    health = status_health(
        ingest=ingest,
        broker=broker_data,
        pipeline=pipeline,
        delivery=delivery,
        workers_state=workers_state,
        now_ms=now_ms,
        enabled=bool(settings.news.enabled),
        model_configured=models.program_configured,
    )
    state = _derive_state(
        ingest=ingest,
        broker=broker_data,
        workers_state=workers_state,
        settings=settings,
        overall_level=str(health["health"]["overall"]),
    )
    data = {
        "state": state,
        "workers_state": workers_state,
        **health,
        "ingest": ingest,
        "broker": broker_data,
        "pipeline": pipeline,
        "oi": oi,
        "delivery": delivery,
        "learning_retention": snapshot["learning_retention"],
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


def _derive_state(
    *,
    ingest: dict[str, Any],
    broker: dict[str, Any],
    workers_state: str | None,
    settings: Any,
    overall_level: str,
) -> str:
    if not settings.news.enabled or not settings.news.opennews_token or not settings.news.broker.url:
        return "unavailable"
    if workers_state != "running":
        return "warming" if workers_state in {None, "recovering"} else "degraded"
    if broker.get("connected") is False or ingest.get("open_incidents"):
        return "degraded"
    if not ingest.get("connected"):
        return "warming"
    if overall_level in {"warn", "bad"}:
        return "degraded"
    return "ready"


def _news_workers_observation(row: dict[str, Any] | None, *, now_ms: int) -> tuple[str | None, str | None]:
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


def _status_etag_basis(data: dict[str, Any]) -> dict[str, Any]:
    """The status payload minus the fields that move every read, so a poll does not churn the ETag."""

    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key not in {"measured_at_ms"}}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    return {key: stable(item) for key, item in data.items() if key != "measured_at_ms"}


__all__ = ["router"]
