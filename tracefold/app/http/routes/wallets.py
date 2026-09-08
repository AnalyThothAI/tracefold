"""Public reads for chain wallet research and its supporting tape state.

Buy candidates remain visible without a delivery. Their selection evidence and observed-price
outcomes come from the stored facts, independently of the roster and ingestion state. PostgreSQL
numeric amounts cross the wire as exact text; the browser does not reconstruct trading facts.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..read_cursor import decode_read_cursor, encode_read_cursor
from ..responses import _etagged
from ..schemas import common as api_schemas
from ..schemas import wallets as wallet_schemas

router = APIRouter()
_WalletsEnvelope = api_schemas.ApiEnvelope[wallet_schemas.NewsWalletsData]
_WalletCardsEnvelope = api_schemas.ApiEnvelope[wallet_schemas.NewsWalletCardsData]

# The closed set of windows the card list accepts, as milliseconds. A closed set rather than a free
# integer because the scan it bounds is a real index range on a table the tape appends to: an operator
# who wants a different span is asking for a different page, not for a wider default.
WALLET_CARD_WINDOWS: Final[dict[str, int]] = {
    "24h": 24 * 3_600_000,
    "72h": 72 * 3_600_000,
    "7d": 7 * 24 * 3_600_000,
}
WALLET_CARDS_PAGE_MAX: Final = 200
# The header counts are always a day. The page's tiles answer "what has the tape done today", and a
# window control on the header that disagreed with the one on the table below it would be two answers
# to one question.
WALLET_HEADER_WINDOW_MS: Final = 24 * 3_600_000


@router.get("/news/wallets", response_model=_WalletsEnvelope)
def get_news_wallets(request: Request) -> Response:
    """The tape's own state: its roster, its position, and one day of what it stored and sent.

    Four bounded statements and no parameters. The roster is the current version only -- an earlier
    version is evidence a card carries, not a page a reader browses -- and the two count blocks are
    the last 24 hours on the chain's own clock.
    """

    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    window_to = int(time.time() * 1000)
    window_from = window_to - WALLET_HEADER_WINDOW_MS
    with runtime.repositories() as repos:
        members = repos.news.chain_tape_roster_rows()
        tape = repos.news.chain_tape_state()
        fills = repos.news.chain_tape_fill_totals(from_ms=window_from)
        cards = repos.news.chain_tape_card_totals(from_ms=window_from)
    return _etagged(
        {
            "roster": _roster(members),
            "tape": None if tape is None else dict(tape),
            "fills": fills,
            "cards": cards,
            "window_from_ms": window_from,
            "window_to_ms": window_to,
        },
        request,
        envelope=_WalletsEnvelope,
    )


@router.get("/news/wallets/cards", response_model=_WalletCardsEnvelope)
def get_news_wallet_cards(
    request: Request,
    window: Annotated[str, Query(max_length=8)] = "24h",
    limit: Annotated[int, Query(ge=1, le=WALLET_CARDS_PAGE_MAX)] = 100,
    kind: Annotated[wallet_schemas.WalletCardKindLiteral | None, Query()] = None,
    wallet_address: Annotated[str | None, Query(pattern=r"^0x[0-9a-fA-F]{40}$")] = None,
    token_address: Annotated[str | None, Query(pattern=r"^0x[0-9a-fA-F]{40}$")] = None,
    chain_id: Annotated[int | None, Query(ge=1, lt=2**63)] = None,
    segment_key: Annotated[str | None, Query(max_length=128)] = None,
    view: Literal["segments", "observations"] = "segments",
    cursor: Annotated[str, Query(max_length=512)] = "",
    to_ms: Annotated[int, Query(ge=0, lt=2**63)] = 0,
) -> Response:
    """Retained observations, optionally narrowed by kind and exact wallet/token identity.

    Every card is published, sent or not: whether a reader was told is reported per row and is never a
    filter. A digest says whether the model selected its material; the program renders its sentences.
    """

    _validate_query_params(
        request,
        supported={
            "window",
            "limit",
            "token",
            "kind",
            "wallet_address",
            "token_address",
            "chain_id",
            "segment_key",
            "view",
            "cursor",
            "to_ms",
        },
    )
    span = WALLET_CARD_WINDOWS.get(str(window or "24h"))
    if span is None:
        raise ApiBadRequest("news_wallets_window_invalid", field="window")
    runtime = _authenticated_runtime(request)
    filters = dict(
        kind=kind,
        chain_id=chain_id,
        segment_key=segment_key,
        view=view,
        wallet_address=wallet_address.lower() if wallet_address else None,
        token_address=token_address.lower() if token_address else None,
    )
    scope = [window, filters]
    position = decode_read_cursor(cursor, scope, error="news_wallets_cursor_invalid")
    now_ms = int(time.time() * 1000)
    window_to = position[0] if position else to_ms or now_ms
    if position and to_ms and to_ms != window_to:
        raise ApiBadRequest("news_wallets_cursor_invalid", field="cursor")
    window_from = max(0, window_to - span)
    with runtime.repositories() as repos:
        cards = repos.news.chain_tape_cards(
            from_ms=window_from,
            to_ms=window_to,
            now_ms=now_ms,
            limit=int(limit) + 1,
            cursor_at_ms=position[2] if position else None,
            cursor_id=position[3] if position else "",
            **filters,
        )
        totals = repos.news.chain_tape_research_totals(from_ms=window_from, to_ms=window_to, **filters)
        fills = (
            repos.news.chain_tape_wallet_fills(
                from_ms=window_from,
                to_ms=window_to,
                limit=int(limit) + 1,
                wallet_address=wallet_address.lower(),
                token_address=token_address.lower(),
                chain_id=chain_id,
            )
            if wallet_address and token_address
            else []
        )
    next_cursor = None
    if len(cards) > limit:
        last = cards[limit - 1]
        next_cursor = encode_read_cursor(
            scope, to_ms=window_to, value=0, at_ms=last["event_at_ms"], identity=last["item_id"]
        )
    return _etagged(
        {
            "cards": cards[:limit],
            "totals": totals,
            "next_cursor": next_cursor,
            "view": view,
            "fills": fills[:limit],
            "fills_complete": len(fills) <= limit,
            "window": str(window),
            "window_from_ms": window_from,
            "window_to_ms": window_to,
            "limit": int(limit),
        },
        request,
        envelope=_WalletCardsEnvelope,
    )


def _roster(members: list[dict[str, Any]]) -> dict[str, Any]:
    """One roster version out of its own rows; an empty tape publishes an empty version, not `null`.

    The version and the timestamp are the same on every row by construction -- the read selects one
    version -- so they are lifted here rather than repeated on every member.
    """

    if not members:
        return {"roster_version": 0, "taken_at_ms": None, "provider": None, "members": []}
    return {
        "roster_version": int(members[0]["roster_version"]),
        "taken_at_ms": int(members[0]["taken_at_ms"]),
        "provider": str(members[0]["provider"]),
        "members": [
            {key: value for key, value in member.items() if key not in {"roster_version", "taken_at_ms"}}
            for member in members
        ],
    }


__all__ = ["WALLET_CARDS_PAGE_MAX", "WALLET_CARD_WINDOWS", "router"]
