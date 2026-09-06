"""The chain wallet tape's two read routes (#572 PR-3).

`/api/news/market` already publishes a wallet observation the way it publishes every other market
kind. These two answer the question that surface cannot: what the *tape* is doing. Which wallets it
follows and on which of the two lists, how far it has read and what its last turn said, what it stored
and what share of it nothing could price, and -- for the cards it opened -- what the token did one and
four hours after a reader was told.

Neither route asks anything of the editorial pipeline, of Trading or of a model, and both are bounded
by their own window. The card list links back to `/api/news/market/{item_id}`, which stays the one
place a single observation is read in full.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
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
) -> Response:
    """Cards the tape opened in the window, newest first, each beside its two price receipts.

    Every card is published, sent or not: whether a reader was told is reported per row and is never a
    filter. A digest carries its own sentences and says whether the model wrote them, which is the one
    thing a reader of this page cannot get from the card itself.
    """

    _validate_query_params(request, supported={"window", "limit", "token"})
    span = WALLET_CARD_WINDOWS.get(str(window or "24h"))
    if span is None:
        raise ApiBadRequest("news_wallets_window_invalid", field="window")
    runtime = _authenticated_runtime(request)
    window_to = int(time.time() * 1000)
    window_from = window_to - span
    with runtime.repositories() as repos:
        cards = repos.news.chain_tape_cards(from_ms=window_from, to_ms=window_to, limit=int(limit))
    return _etagged(
        {
            "cards": cards,
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
