from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from curl_cffi import CurlOpt
from curl_cffi import requests as curl_requests
from eth_utils import is_address

CURL_IPRESOLVE_V4 = 1
DEFAULT_CURL_IMPERSONATE = "chrome142"


@dataclass(frozen=True, slots=True)
class GmgnTokenInfo:
    chain: str
    address: str
    symbol: str
    name: str | None
    icon_url: str | None
    banner_url: str | None
    decimals: int | None
    price: float | None
    previous_price: float | None
    market_cap: float | None
    liquidity: float | None
    holder_count: int | None
    circulating_supply: float | None
    total_supply: float | None
    max_supply: float | None
    website: str | None
    twitter_username: str | None
    telegram: str | None
    gmgn_url: str | None
    geckoterminal_url: str | None
    description: str | None
    pool: dict[str, Any] | None
    dev: dict[str, Any] | None
    stat: dict[str, Any] | None
    link: dict[str, Any] | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GmgnTokenInfoLookup:
    info: GmgnTokenInfo | None
    cache_status: str


class GmgnOpenApiError(RuntimeError):
    pass


class GmgnOpenApiProviderUnavailableError(GmgnOpenApiError):
    def __init__(self, message: str, *, cooldown_seconds: float | None = None) -> None:
        super().__init__(message)
        self.cooldown_seconds = cooldown_seconds


class GmgnOpenApiTransientError(GmgnOpenApiError):
    pass


class GmgnOpenApiClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://openapi.gmgn.ai",
        timeout_seconds: float = 5.0,
        force_ipv4: bool = True,
        curl_impersonate: str = DEFAULT_CURL_IMPERSONATE,
        transport: httpx.BaseTransport | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._headers = {"X-APIKEY": api_key, "Content-Type": "application/json"}
        self._curl_session: curl_requests.Session | None = None
        self._httpx_client: httpx.Client | None = None
        resolved_transport = transport
        if resolved_transport is None and force_ipv4:
            self._curl_session = curl_requests.Session(
                impersonate=curl_impersonate,
                curl_options={CurlOpt.IPRESOLVE: CURL_IPRESOLVE_V4},
            )
        else:
            self._httpx_client = httpx.Client(
                base_url=self.base_url,
                timeout=timeout_seconds,
                transport=resolved_transport,
                headers=self._headers,
            )

    def close(self) -> None:
        if self._httpx_client is not None:
            self._httpx_client.close()
        if self._curl_session is not None:
            self._curl_session.close()

    def lookup_token_info(self, *, chain: str, address: str) -> GmgnTokenInfoLookup:
        api_chain = _api_chain(chain)
        api_address = _api_address(chain=api_chain, address=address)
        data = self._request("GET", "/v1/token/info", {"chain": api_chain, "address": api_address})
        info = _token_info_from_response(chain=chain, address=api_address, data=data)
        return GmgnTokenInfoLookup(info=info, cache_status="miss")

    def _request(self, method: str, path: str, params: dict[str, str]) -> Any:
        query = {
            **params,
            "timestamp": str(int(time.time())),
            "client_id": str(uuid.uuid4()),
        }
        response = self._send(method, path, query)
        text = response["text"]
        try:
            payload = response["json"]()
        except ValueError as exc:
            message = _non_json_error_message(method=method, path=path, response=response)
            if _is_provider_unavailable_non_json(response):
                raise GmgnOpenApiProviderUnavailableError(
                    message,
                    cooldown_seconds=_provider_cooldown_seconds(response=response, payload=None),
                ) from exc
            if _is_transient_status(response["status_code"]):
                raise GmgnOpenApiTransientError(message) from exc
            raise GmgnOpenApiError(message) from exc
        if not isinstance(payload, dict):
            raise GmgnOpenApiError(f"{method} {path} returned invalid envelope")
        if payload.get("code") != 0:
            message = payload.get("message") or payload.get("error") or text
            if _is_provider_unavailable_payload(response=response, payload=payload):
                raise GmgnOpenApiProviderUnavailableError(
                    f"{method} {path} provider unavailable: {message}",
                    cooldown_seconds=_provider_cooldown_seconds(response=response, payload=payload),
                )
            if _is_transient_status(response["status_code"]):
                raise GmgnOpenApiTransientError(f"{method} {path} transient HTTP {response['status_code']}: {message}")
            if path == "/v1/token/info" and str(message).strip().casefold() == "invalid argument":
                return None
            raise GmgnOpenApiError(f"{method} {path} failed: {message}")
        return payload.get("data")

    def _send(self, method: str, path: str, query: dict[str, str]) -> dict[str, Any]:
        if self._httpx_client is not None:
            try:
                response = self._httpx_client.request(method, path, params=query)
            except httpx.HTTPError as exc:
                raise GmgnOpenApiTransientError(f"{method} {path} transient network error: {exc}") from exc
            return {
                "status_code": response.status_code,
                "headers": response.headers,
                "text": response.text,
                "json": response.json,
            }
        if self._curl_session is None:
            raise GmgnOpenApiError("GMGN HTTP client is not initialized")
        try:
            response = self._curl_session.request(
                method,
                f"{self.base_url}{path}",
                params=query,
                headers=self._headers,
                timeout=self._timeout_seconds,
            )
        except Exception as exc:
            raise GmgnOpenApiTransientError(f"{method} {path} transient network error: {exc}") from exc
        return {
            "status_code": response.status_code,
            "headers": response.headers,
            "text": response.text,
            "json": response.json,
        }


def _token_info_from_response(*, chain: str, address: str, data: Any) -> GmgnTokenInfo | None:
    if not isinstance(data, dict):
        return None
    symbol = _string(data.get("symbol"))
    if not symbol:
        return None
    price_payload = data.get("price") if isinstance(data.get("price"), dict) else None
    price = _float_or_none(data.get("price")) or _float_or_none((price_payload or {}).get("price"))
    market_cap = _market_cap(data, price=price)
    link = data.get("link") if isinstance(data.get("link"), dict) else None
    stat = data.get("stat") if isinstance(data.get("stat"), dict) else None
    pool = data.get("pool") if isinstance(data.get("pool"), dict) else None
    return GmgnTokenInfo(
        chain=_internal_chain(chain),
        address=_string(data.get("address")) or address,
        symbol=symbol.strip().lstrip("$").upper() if symbol.isascii() else symbol.strip().lstrip("$"),
        name=_string(data.get("name")),
        icon_url=_string(data.get("logo")) or _string(data.get("icon_url")),
        banner_url=_string(data.get("banner")),
        decimals=_int_or_none(data.get("decimals")),
        price=price,
        previous_price=_float_or_none(data.get("previous_price"))
        or _float_or_none((price_payload or {}).get("price_5m")),
        market_cap=market_cap,
        liquidity=_float_or_none(data.get("liquidity")) or _float_or_none((pool or {}).get("liquidity")),
        holder_count=_int_or_none(data.get("holder_count") or data.get("holders") or (stat or {}).get("holder_count")),
        circulating_supply=_float_or_none(data.get("circulating_supply")),
        total_supply=_float_or_none(data.get("total_supply")),
        max_supply=_float_or_none(data.get("max_supply")),
        website=_link_string(link, "website"),
        twitter_username=_link_string(link, "twitter_username") or _link_string(link, "twitter"),
        telegram=_link_string(link, "telegram"),
        gmgn_url=_link_string(link, "gmgn"),
        geckoterminal_url=_link_string(link, "geckoterminal"),
        description=_link_string(link, "description") or _string(data.get("description")),
        pool=dict(pool) if pool is not None else None,
        dev=dict(data["dev"]) if isinstance(data.get("dev"), dict) else None,
        stat=dict(stat) if stat is not None else None,
        link=dict(link) if link is not None else None,
        raw=dict(data),
    )


def _api_chain(chain: str) -> str:
    normalized = chain.strip().lower()
    if normalized == "eip155:1":
        return "eth"
    if normalized == "eip155:56":
        return "bsc"
    if normalized == "eip155:8453":
        return "base"
    if normalized == "solana":
        return "sol"
    return normalized


def _non_json_error_message(*, method: str, path: str, response: dict[str, Any]) -> str:
    status_code = response["status_code"]
    if _is_provider_unavailable_non_json(response):
        return f"{method} {path} blocked by Cloudflare challenge HTTP {status_code}"
    if _is_transient_status(status_code):
        return f"{method} {path} transient HTTP {status_code}"
    return f"{method} {path} returned non-json HTTP {status_code}"


def _is_provider_unavailable_non_json(response: dict[str, Any]) -> bool:
    return _is_cloudflare_challenge(str(response.get("text") or ""))


def _is_cloudflare_challenge(text: str) -> bool:
    normalized = text.lower()
    return "just a moment" in normalized or "cf-chl" in normalized or "cloudflare" in normalized


def _is_provider_unavailable_payload(*, response: dict[str, Any], payload: dict[str, Any]) -> bool:
    if int(response["status_code"]) == 429:
        return True
    api_error = str(payload.get("error") or "").strip().upper()
    return api_error in {"RATE_LIMIT_EXCEEDED", "RATE_LIMIT_BANNED", "ERROR_RATE_LIMIT_BLOCKED"}


def _is_transient_status(status_code: int) -> bool:
    return int(status_code) in {408, 409, 500, 502, 503, 504}


def _provider_cooldown_seconds(*, response: dict[str, Any], payload: dict[str, Any] | None) -> float | None:
    reset_at = _reset_at_from_payload(payload) or _reset_at_from_headers(response.get("headers"))
    if reset_at is None:
        return None
    return max(0.0, reset_at - time.time())


def _reset_at_from_payload(payload: dict[str, Any] | None) -> float | None:
    if not isinstance(payload, dict):
        return None
    try:
        value = float(payload.get("reset_at"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _reset_at_from_headers(headers: Any) -> float | None:
    if headers is None:
        return None
    raw = _header_value(headers, "x-ratelimit-reset") or _header_value(headers, "retry-after")
    if not raw:
        return None
    text = str(raw).strip()
    try:
        value = float(text)
    except ValueError:
        try:
            return parsedate_to_datetime(text).timestamp()
        except (TypeError, ValueError, OSError):
            return None
    if value <= 0:
        return None
    if value < 10_000_000:
        return time.time() + value
    return value


def _header_value(headers: Any, key: str) -> str | None:
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(key)
    if value is None:
        value = getter(key.upper())
    return str(value) if value is not None else None


def _api_address(*, chain: str, address: str) -> str:
    if chain in {"eth", "base", "bsc", "robinhood"} and is_address(address):
        return address.lower()
    return address


def _internal_chain(chain: str) -> str:
    normalized = chain.strip().lower()
    if normalized == "sol":
        return "solana"
    return normalized


def _market_cap(data: dict[str, Any], *, price: float | None) -> float | None:
    price_payload = data.get("price") if isinstance(data.get("price"), dict) else None
    direct = _float_or_none(data.get("market_cap") or data.get("mcap") or data.get("market_cap_usd")) or _float_or_none(
        (price_payload or {}).get("market_cap") or (price_payload or {}).get("market_cap_usd")
    )
    if direct is not None:
        return direct
    supply = _float_or_none(data.get("circulating_supply"))
    if price is None or supply is None:
        return None
    return price * supply


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _link_string(link: dict[str, Any] | None, key: str) -> str | None:
    if link is None:
        return None
    return _string(link.get(key))
