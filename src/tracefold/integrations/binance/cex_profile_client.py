from __future__ import annotations

from typing import Any

import httpx

from tracefold.market import BINANCE_CEX_PROFILE_PROVIDER

SOURCE = "binance_marketing_symbol_list"
_SYMBOL_LIST_PATH = "/bapi/composite/v1/public/marketing/symbol/list"


class BinanceCexProfileClient:
    def __init__(
        self,
        *,
        base_url: str = "https://www.binance.com",
        timeout_seconds: float = 15.0,
        http_client: Any | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={"User-Agent": "Mozilla/5.0"},
        )

    def token_profiles(self) -> list[dict[str, Any]]:
        response = self._client.get(_SYMBOL_LIST_PATH)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []

        selected: dict[str, tuple[int, dict[str, Any]]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            symbol = _symbol(item.get("baseAsset") or item.get("name") or item.get("mapperName"))
            logo_url = _url(item.get("logo"))
            if not symbol or not logo_url:
                continue
            rank = _rank(item.get("rank"))
            normalized_item = dict(item)
            current = selected.get(symbol)
            if current is None or rank < current[0]:
                selected[symbol] = (rank, normalized_item)

        profiles: list[dict[str, Any]] = []
        for symbol, (_, item) in sorted(selected.items()):
            logo_url = _url(item.get("logo"))
            if not logo_url:
                continue
            profiles.append(
                {
                    "base_symbol": symbol,
                    "provider": BINANCE_CEX_PROFILE_PROVIDER,
                    "symbol": symbol,
                    "name": _name(item.get("name")) or symbol,
                    "logo_url": logo_url,
                    "source_ref": f"{SOURCE}:{symbol}",
                    "raw_payload": item,
                }
            )
        return profiles

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _symbol(value: Any) -> str | None:
    text = str(value or "").strip().lstrip("$").upper()
    return text or None


def _name(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _url(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.startswith(("http://", "https://")) else None


def _rank(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 1_000_000_000
