from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from tracefold.market import BINANCE_WEB3_PROFILE_PROVIDER

_TOKEN_METADATA_PATH = "/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info/ai"
_IMAGE_BASE_URL = "https://bin.bnbstatic.com"


@dataclass(frozen=True, slots=True)
class BinanceWeb3TokenMetadata:
    chain_id: str
    address: str
    symbol: str | None
    name: str | None
    logo_url: str | None
    website: str | None
    twitter_url: str | None
    twitter_username: str | None
    telegram: str | None
    description: str | None
    raw: dict[str, Any]


class BinanceWeb3TokenClient:
    def __init__(
        self,
        *,
        base_url: str = "https://web3.binance.com",
        timeout_seconds: float = 15.0,
        http_client: Any | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Accept-Encoding": "identity",
                "User-Agent": "binance-web3/1.1 (Skill)",
            },
        )

    def token_metadata(self, *, chain_id: str, address: str) -> BinanceWeb3TokenMetadata | None:
        binance_chain_id = binance_chain_id_for_domain_chain(chain_id)
        if binance_chain_id is None:
            return None
        response = self._client.get(
            _TOKEN_METADATA_PATH,
            params={"chainId": binance_chain_id, "contractAddress": str(address).strip()},
        )
        response.raise_for_status()
        payload = response.json()
        if not _success(payload):
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data:
            return None
        links = _links(data.get("links"))
        raw = dict(data)
        raw["source_provider"] = BINANCE_WEB3_PROFILE_PROVIDER
        return BinanceWeb3TokenMetadata(
            chain_id=_domain_chain_id(str(data.get("chainId") or binance_chain_id)),
            address=_address(data.get("contractAddress") or address),
            symbol=_text(data.get("symbol")),
            name=_text(data.get("name")),
            logo_url=_icon_url(data.get("icon")),
            website=links.get("website"),
            twitter_url=links.get("x") or links.get("twitter"),
            twitter_username=_twitter_username(links.get("x") or links.get("twitter")),
            telegram=links.get("tg") or links.get("telegram"),
            description=_text(data.get("description")),
            raw=raw,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def binance_chain_id_for_domain_chain(chain_id: Any) -> str | None:
    normalized = str(chain_id or "").strip().lower()
    if not normalized:
        return None
    if normalized.startswith("eip155:"):
        normalized = normalized.split(":", 1)[1]
    aliases = {
        "1": "1",
        "eth": "1",
        "ethereum": "1",
        "56": "56",
        "bsc": "56",
        "bnb": "56",
        "bnb smart chain": "56",
        "8453": "8453",
        "base": "8453",
        "sol": "CT_501",
        "solana": "CT_501",
        "ct_501": "CT_501",
    }
    return aliases.get(normalized)


def _domain_chain_id(value: str) -> str:
    normalized = str(value or "").strip()
    mapping = {
        "1": "eip155:1",
        "56": "eip155:56",
        "8453": "eip155:8453",
        "CT_501": "solana",
        "ct_501": "solana",
    }
    return mapping.get(normalized, normalized)


def _success(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    code = str(payload.get("code") or "").strip()
    if code and code != "000000":
        return False
    success = payload.get("success")
    return success is not False


def _links(value: Any) -> dict[str, str]:
    links: dict[str, str] = {}
    if not isinstance(value, list):
        return links
    for item in value:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip().lower()
        link = _url(item.get("link"))
        if label and link:
            links[label] = link
    return links


def _icon_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("/"):
        return f"{_IMAGE_BASE_URL}{text}"
    return None


def _url(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.startswith(("http://", "https://")) else None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _address(value: Any) -> str:
    text = str(value or "").strip()
    return text.lower() if text.startswith(("0x", "0X")) else text


def _twitter_username(value: str | None) -> str | None:
    if not value:
        return None
    tail = value.rstrip("/").rsplit("/", 1)[-1].strip()
    return tail.lstrip("@") or None
