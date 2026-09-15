"""Read-only token episodes. Auxiliary roster failures do not obscure events."""

from __future__ import annotations

import time
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from tracefold.news.chain_tape.rules import FAST_WINDOW_MS, SLOW_WINDOW_MS

from ..dependencies import _authenticated_runtime, _now_ms, _validate_query_params
from ..exceptions import ApiBadRequest
from ..read_cursor import decode_read_cursor, encode_read_cursor
from ..responses import _etagged, _json
from ..schemas import common as api_schemas
from ..schemas import wallets as wallet_schemas

router = APIRouter()
_WalletsEnvelope = api_schemas.ApiEnvelope[wallet_schemas.NewsWalletsData]
_EventsEnvelope = api_schemas.ApiEnvelope[wallet_schemas.NewsWalletEventsData]
_DetailEnvelope = api_schemas.ApiEnvelope[wallet_schemas.NewsWalletEventDetailData]
WALLET_HISTORY_RANGES: Final = {"24h": 86_400_000, "72h": 259_200_000, "7d": 604_800_000}
WALLET_EVENTS_PAGE_MAX: Final = 200
WALLET_FUNNEL_WINDOW_MS: Final = 86_400_000
# The funnel window ends on a whole minute. The page polls this read every ten seconds, and a window
# that moved every millisecond would make the response body different every time and the ETag useless
# on a read whose answer changes hourly. A reader asking why nothing alerted all morning is not served
# by the last sixty seconds being in the count.
WALLET_FUNNEL_BUCKET_MS: Final = 60_000
# The tape polls every two seconds and stamps `scanned_at_ms` on every successful turn, so a cutoff a
# whole minute behind the clock means turns are failing or overrunning, not that the chain was quiet.
# The browser used to make this judgement with a bare `60_000`; it is one server-owned number (#649 §7.3).
COLLECTION_LAG_MS: Final = 60_000


@router.get("/news/wallets", response_model=_WalletsEnvelope)
def get_news_wallets(request: Request) -> Response:
    """Whether the current list, the current collection and the send chain can produce an alert at all.

    A reader who sees no events needs to tell "nothing qualified" from "nothing could have qualified",
    and every number that answers that is counted here rather than in the browser: the quality pool
    against the two quorums, the monitoring support behind it, how far behind the chain cutoff is, and
    what happened to the episodes that did exist.
    """

    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    now = _now_ms()
    chain_tape = runtime.settings.news.chain_tape
    until = now - now % WALLET_FUNNEL_BUCKET_MS
    since = max(0, until - WALLET_FUNNEL_WINDOW_MS)
    with runtime.repositories() as repos:
        members = repos.news.chain_tape_roster_rows()
        tape = repos.news.chain_tape_state()
        funnel = repos.news.wallet_notification_funnel(from_ms=since, to_ms=until)
    cutoff = None if tape is None else tape["scanned_at_ms"]
    coverage_from = None if tape is None else tape["coverage_from_ms"]
    rules = chain_tape.rules
    return _etagged(
        {
            "roster": _roster(
                members,
                tape,
                cutoff=cutoff,
                coverage_from_ms=coverage_from,
                window=chain_tape.roster.window,
            ),
            "tape": tape,
            "thresholds": {
                "fast_n": rules.net_buy_fast_n,
                "slow_n": rules.net_buy_slow_n,
                "sufficient": _supported(members, cutoff, coverage_from, FAST_WINDOW_MS) >= rules.net_buy_fast_n
                or _supported(members, cutoff, coverage_from, SLOW_WINDOW_MS) >= rules.net_buy_slow_n,
            },
            "funnel": {**funnel, "window_from_ms": since, "window_to_ms": until},
            "collection_lagging": cutoff is None or now - int(cutoff) > COLLECTION_LAG_MS,
            "notifications_enabled": bool(chain_tape.notifications_enabled),
        },
        request,
        envelope=_WalletsEnvelope,
    )


@router.get("/news/wallets/events", response_model=_EventsEnvelope)
def get_news_wallet_events(
    request: Request,
    history_range: Literal["24h", "72h", "7d"] = "24h",
    limit: Annotated[int, Query(ge=1, le=WALLET_EVENTS_PAGE_MAX)] = 50,
    cursor: Annotated[str, Query(max_length=512)] = "",
    to_ms: Annotated[int, Query(ge=0, lt=2**63)] = 0,
) -> Response:
    _validate_query_params(request, supported={"token", "history_range", "limit", "cursor", "to_ms"})
    runtime = _authenticated_runtime(request)
    scope = ["wallet_events", history_range]
    position = decode_read_cursor(cursor, scope, error="news_wallet_events_cursor_invalid")
    until = position[0] if position else to_ms or int(time.time() * 1000)
    if position and to_ms and to_ms != until:
        raise ApiBadRequest("news_wallet_events_cursor_invalid", field="cursor")
    since = max(0, until - WALLET_HISTORY_RANGES[history_range])
    with runtime.repositories() as repos, repos.news.wallet_read_snapshot():
        events = repos.news.wallet_events(
            from_ms=since,
            to_ms=until,
            before_at_ms=position[2] if position else None,
            before_id=position[3] if position else None,
            limit=limit + 1,
        )
        totals = repos.news.wallet_event_totals(from_ms=since, to_ms=until)
    next_cursor = None
    if len(events) > limit:
        last = events[limit - 1]
        next_cursor = encode_read_cursor(
            scope, to_ms=until, value=0, at_ms=last["event_at_ms"], identity=last["item_id"]
        )
    return _etagged(
        {
            "events": [_event(row) for row in events[:limit]],
            "totals": totals,
            "next_cursor": next_cursor,
            "history_range": history_range,
            "history_from_ms": since,
            "history_to_ms": until,
            "limit": limit,
        },
        request,
        envelope=_EventsEnvelope,
    )


@router.get("/news/wallets/events/{episode_id}", response_model=_DetailEnvelope)
def get_news_wallet_event(
    request: Request,
    episode_id: str,
    fills_cursor: Annotated[str, Query(max_length=512)] = "",
    limit: Annotated[int, Query(ge=1, le=WALLET_EVENTS_PAGE_MAX)] = 100,
) -> Response:
    _validate_query_params(request, supported={"token", "fills_cursor", "limit"})
    runtime = _authenticated_runtime(request)
    scope = ["wallet_event_fills", episode_id]
    position = decode_read_cursor(fills_cursor, scope, error="news_wallet_fills_cursor_invalid")
    with runtime.repositories() as repos, repos.news.wallet_read_snapshot():
        event = repos.news.wallet_event(episode_id)
        if event is None:
            return _json({"ok": False, "error": "news_wallet_event_not_found"}, status_code=404)
        until = position[0] if position else event["latest_snapshot"]["cutoff_at_ms"]
        fills = repos.news.wallet_event_fills(
            chain_id=event["chain_id"],
            token=event["token"],
            from_ms=event["initial_snapshot"]["slow"]["from_ms"],
            to_ms=until,
            cutoff_block=event["latest_snapshot"]["cutoff_block"],
            cutoff_log=event["latest_snapshot"]["cutoff_log"],
            before_block=position[1] if position else None,
            before_log=position[2] if position else None,
            limit=limit + 1,
        )
        outcomes = repos.news.wallet_outcomes(episode_id)
    next_cursor = None
    if len(fills) > limit:
        last = fills[limit - 1]
        next_cursor = encode_read_cursor(
            scope, to_ms=until, value=last["block_number"], at_ms=last["log_index"], identity=last["tx_hash"]
        )
    return _etagged(
        {
            "event": _event(event),
            "fills": fills[:limit],
            "next_fills_cursor": next_cursor,
            "outcomes": outcomes,
        },
        request,
        envelope=_DetailEnvelope,
    )


def _event(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "chain_id",
        "token",
        "token_symbol",
        "trigger_tx_hash",
        "received_at_ms",
        "detected_at_ms",
        "last_effective_buy_at_ms",
        "ended_at_ms",
        "initial_snapshot",
        "latest_snapshot",
        "change_reason",
        "updated_at_ms",
        "intent_at_ms",
        "first_attempt_at_ms",
        "settled_at_ms",
        "reference_at_ms",
        "reference_source",
    )
    return {
        **{field: row[field] for field in fields},
        "episode_id": row["item_id"],
        "triggered_at_ms": row["event_at_ms"],
        # One projection, computed in SQL beside the facts it reads (#649 §7.1). The route used to
        # invent `pending` here for "eligible and no error", which is how a notification-stage
        # rejection with no intent -- `wallet_not_selected`, `episode_already_reported`, a discarded
        # intent -- read as "waiting to be sent" for the rest of its life.
        "notification_state": row["notification_state"],
        "notification_reason": row["notification_error"],
        "notification_next_due_at_ms": (
            row["notification_next_due_at_ms"] if row["notification_state"] == "pending" else None
        ),
        "attempts": row["attempts"] or 0,
        "reference_price": None if row["reference_price"] is None else str(row["reference_price"]),
    }


def _roster(
    members: list[dict[str, Any]],
    tape: dict[str, Any] | None,
    *,
    cutoff: int | None,
    coverage_from_ms: int | None,
    window: str,
) -> dict[str, Any]:
    """The published version is the last refresh that actually succeeded; a failed one publishes nothing.

    The refresh half is the refresh task's own record rather than a guess from the collection turn's
    error (#649 §5.1). `news-wallet-roster` writes `roster_last_attempt_at_ms` on every attempt and
    `roster_last_success_at_ms` only on one that published, so "throttled for five hours" and "the list
    genuinely did not change" are two different answers here instead of one silence. A refresh that has
    never been tried has no attempt stamp and no error, which is also not a failure.
    """

    published = {
        "version": 0 if not members else int(members[0]["roster_version"]),
        "taken_at_ms": None if not members else int(members[0]["taken_at_ms"]),
        "provider": None if not members else str(members[0]["provider"]),
    }
    return {
        **published,
        "window": window,
        "quality_count": sum(member["rank_quality"] is not None for member in members),
        "whale_count": sum(member["rank_whale"] is not None for member in members),
        "supported_quality_count": _supported(members, cutoff, coverage_from_ms, FAST_WINDOW_MS),
        "last_attempt_at_ms": None if tape is None else tape["roster_last_attempt_at_ms"],
        "last_success_at_ms": None if tape is None else tape["roster_last_success_at_ms"],
        "last_error": None if tape is None else tape["roster_last_error"],
        "members": [
            {key: value for key, value in member.items() if key not in {"roster_version", "taken_at_ms"}}
            for member in members
        ],
    }


def _supported(members: list[dict[str, Any]], cutoff: int | None, coverage_from_ms: int | None, window_ms: int) -> int:
    """Quality addresses whose monitoring already covers a whole `window_ms` at the collection cutoff.

    The same test `rules.py` applies to a member inside a window, asked of the roster as a whole: both
    the address's own `monitoring_from_ms` and the tape's coverage must start before the window does.
    """

    if cutoff is None or coverage_from_ms is None:
        return 0
    start = int(cutoff) - window_ms
    if int(coverage_from_ms) > start:
        return 0
    return sum(
        member["rank_quality"] is not None
        and member["monitoring_from_ms"] is not None
        and int(member["monitoring_from_ms"]) <= start
        for member in members
    )
