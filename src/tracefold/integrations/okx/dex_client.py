from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from tracefold.integrations.okx.http_utils import OkxClientError, items_from_response, rows_from_response
from tracefold.integrations.okx.models import OkxDexTokenCandidate, OkxDexTokenPrice

EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$", re.IGNORECASE)


class OkxDexClient:
    def __init__(
        self,
        *,
        base_url: str = "https://web3.okx.com",
        api_key: str | None = None,
        secret_key: str | None = None,
        passphrase: str | None = None,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = _text(api_key)
        self.secret_key = _text(secret_key)
        self.passphrase = _text(passphrase)
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout_seconds, transport=transport)

    def close(self) -> None:
        self._client.close()

    def search_tokens(self, *, query: str, chain_indexes: list[str] | tuple[str, ...]) -> list[OkxDexTokenCandidate]:
        keyword = _search_keyword(query)
        chains = ",".join(str(chain).strip() for chain in chain_indexes if str(chain).strip())
        if not keyword:
            return []
        rows = self._get(
            "/api/v6/dex/market/token/search",
            params={"search": keyword, "chains": chains},
        )
        candidates: list[OkxDexTokenCandidate] = []
        for row in rows:
            candidate = _candidate_from_row(row)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def token_prices(self, tokens: list[dict[str, str]]) -> list[OkxDexTokenPrice]:
        body_items = [_price_request_item(token) for token in tokens]
        body_items = [item for item in body_items if item is not None]
        if not body_items:
            return []
        rows = self._post("/api/v6/dex/market/price-info", body=body_items)
        prices: list[OkxDexTokenPrice] = []
        for row in rows:
            price = _price_from_row(row)
            if price is not None:
                prices.append(price)
        return prices

    def _get(self, path: str, *, params: dict[str, str]) -> list[dict[str, Any]]:
        return [row for row in self._get_items(path, params=params) if isinstance(row, dict)]

    def _get_items(self, path: str, *, params: dict[str, str]) -> list[Any]:
        request = self._client.build_request("GET", path, params={key: value for key, value in params.items() if value})
        self._sign_request(request, body="")
        try:
            response = self._client.send(request)
        except httpx.HTTPError as exc:
            raise OkxClientError(f"OKX {path} transport failed:{type(exc).__name__}") from exc
        return items_from_response(response, endpoint=path)

    def _post(self, path: str, *, body: list[dict[str, str]]) -> list[dict[str, Any]]:
        raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        request = self._client.build_request(
            "POST",
            path,
            content=raw_body,
            headers={"Content-Type": "application/json"},
        )
        self._sign_request(request, body=raw_body)
        try:
            response = self._client.send(request)
        except httpx.HTTPError as exc:
            raise OkxClientError(f"OKX {path} transport failed:{type(exc).__name__}") from exc
        return rows_from_response(response, endpoint=path)

    def _sign_request(self, request: httpx.Request, *, body: str) -> None:
        if not self.api_key or not self.secret_key or not self.passphrase:
            return
        timestamp = _okx_timestamp()
        request_path = request.url.raw_path.decode("utf-8")
        prehash = f"{timestamp}{request.method.upper()}{request_path}{body}"
        digest = hmac.new(
            self.secret_key.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        request.headers["OK-ACCESS-KEY"] = self.api_key
        request.headers["OK-ACCESS-SIGN"] = base64.b64encode(digest).decode("utf-8")
        request.headers["OK-ACCESS-TIMESTAMP"] = timestamp
        request.headers["OK-ACCESS-PASSPHRASE"] = self.passphrase


def _candidate_from_row(row: dict[str, Any]) -> OkxDexTokenCandidate | None:
    chain_index = _text(row.get("chainIndex") or row.get("chain_index"))
    address = _text(row.get("tokenContractAddress") or row.get("tokenAddress") or row.get("address"))
    symbol = _text(row.get("tokenSymbol") or row.get("symbol"))
    if not chain_index or not address or not symbol:
        return None
    return OkxDexTokenCandidate(
        chain_index=chain_index,
        chain=_text(row.get("chain") or row.get("chainName")),
        address=address,
        symbol=symbol.strip().lstrip("$").upper() if symbol.isascii() else symbol.strip().lstrip("$"),
        name=_text(row.get("tokenName") or row.get("name")),
        price_usd=_float(row.get("price") or row.get("priceUsd")),
        market_cap_usd=_float(row.get("marketCap") or row.get("marketCapUsd")),
        liquidity_usd=_float(row.get("liquidity") or row.get("liquidityUsd")),
        holders=_int(row.get("holders") or row.get("holderCount")),
        community_recognized=_community_recognized(row),
        raw=dict(row),
    )


def _price_from_row(row: dict[str, Any]) -> OkxDexTokenPrice | None:
    chain_index = _text(row.get("chainIndex") or row.get("chain_index"))
    address = _text(row.get("tokenContractAddress") or row.get("tokenAddress") or row.get("address"))
    observed_at_ms = _int(row.get("time") or row.get("observedAtMs") or row.get("observed_at_ms"))
    if not chain_index or not address or observed_at_ms is None:
        return None
    normalized_address = address.lower() if EVM_ADDRESS_RE.match(address) else address
    return OkxDexTokenPrice(
        chain_index=chain_index,
        address=normalized_address,
        observed_at_ms=observed_at_ms,
        price_usd=_float(row.get("price") or row.get("priceUsd")),
        market_cap_usd=_float(row.get("marketCap")),
        liquidity_usd=_float(row.get("liquidity")),
        holders=_int(row.get("holders")),
        raw=dict(row),
    )


def _search_keyword(query: str) -> str:
    stripped = query.strip().lstrip("$")
    if not stripped:
        return ""
    if EVM_ADDRESS_RE.match(stripped):
        return stripped.lower()
    return stripped.upper() if stripped.isascii() else stripped


def _price_request_item(token: dict[str, str]) -> dict[str, str] | None:
    chain_index = _text(token.get("chainIndex") or token.get("chain_index"))
    address = _text(token.get("tokenContractAddress") or token.get("token_address") or token.get("address"))
    if not chain_index or not address:
        return None
    return {
        "chainIndex": chain_index,
        "tokenContractAddress": address.lower() if EVM_ADDRESS_RE.match(address) else address,
    }


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise OkxClientError(f"invalid OKX boolean value: {value}")


def _community_recognized(row: dict[str, Any]) -> bool | None:
    tag_list = row.get("tagList")
    if isinstance(tag_list, dict) and "communityRecognized" in tag_list:
        return _bool_or_none(tag_list.get("communityRecognized"))
    return _bool_or_none(row.get("communityRecognized") or row.get("community_recognized"))


def _okx_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
