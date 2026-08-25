from __future__ import annotations

import re
from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..responses import _etagged
from ..schemas import common as api_schemas
from ..schemas import symbols as symbol_schemas

router = APIRouter()
_SymbolEnvelope = api_schemas.ApiEnvelope[symbol_schemas.NewsSymbolData]

# The same shape `news_market_instruments.base_symbol` holds and `normalize_symbol` produces. Bounding it here
# keeps a path segment from reaching two indexed lookups as a 2 KB string.
_BASE_SYMBOL: Final = re.compile(r"^[A-Z0-9._-]{1,24}$")


@router.get("/news/symbols/{base}", response_model=_SymbolEnvelope)
def get_news_symbol(request: Request, base: str) -> Response:
    """What one `base_symbol` is: the names it answers to, and the contracts it names (#207 PR-W1).

    A base no venue we poll has ever listed is `known: false` with empty lists, not a 404. The provider tags
    symbols the universe has never seen — that is exactly what the struck-through chip means — and every one
    of those chips is now a link, so 404 would be the ordinary outcome of a reader following one.
    """

    _validate_query_params(request, supported={"token"})
    normalized = str(base or "").strip().upper().removeprefix("XYZ-")
    if not _BASE_SYMBOL.fullmatch(normalized):
        raise ApiBadRequest("news_symbol_invalid", field="base")
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        contracts = repos.instruments.contracts_for(normalized)
        tradeable = repos.instruments.is_tradeable(normalized)
        # Only a group that collapses something is worth a row, exactly as the Event detail block decides it.
        groups = repos.instruments.aliases_by_base((normalized,), sources=("seed",))
        group = groups.get(normalized)
    return _etagged(
        {
            "base_symbol": normalized,
            "known": bool(contracts),
            "tradeable": tradeable,
            "venues": [contract["venue"] for contract in contracts],
            "contracts": contracts,
            "normalization": group if group and len(group.get("aliases") or []) > 1 else None,
        },
        request,
        envelope=_SymbolEnvelope,
    )


__all__ = ["router"]
