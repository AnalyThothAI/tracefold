"""DEX Screener discovery evidence with exact onchain ERC-20 metadata verification.

This adapter never quotes or executes a trade. It fills the long-tail discovery gap left by
route-provider token directories, then verifies the candidate contract on the reported EVM chain
before returning it to the domain resolver.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from eth_abi.abi import decode as decode_abi

from tracefold.trading import (
    OnchainProviderToken,
    OnchainProviderUnavailable,
    canonical_onchain_asset_seed,
)

_ORIGIN = "https://api.dexscreener.com"
_TIMEOUT_SECONDS = 5.0
_CHAIN_IDS = {
    "ethereum": 1,
    "bsc": 56,
    "base": 8453,
    "arbitrum": 42161,
    "robinhood": 4663,
}
_CHAIN_NAMES = {
    1: "Ethereum",
    56: "BNB Chain",
    8453: "Base",
    42161: "Arbitrum One",
    4663: "Robinhood Chain",
}
_PUBLIC_DISCOVERY_RPCS = {
    56: "https://bsc-dataseed.binance.org",
    8453: "https://mainnet.base.org",
    42161: "https://arb1.arbitrum.io/rpc",
    4663: "https://rpc.mainnet.chain.robinhood.com",
}
_SELECTORS = {
    "name": "0x06fdde03",
    "symbol": "0x95d89b41",
    "decimals": "0x313ce567",
}


class DexScreenerOnchainDiscoveryClient:
    """Find a ticker or CA globally, then prove contract metadata on the matching chain."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        rpc_urls: Mapping[int, str] | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=_ORIGIN,
            timeout=httpx.Timeout(_TIMEOUT_SECONDS),
            follow_redirects=False,
            transport=transport,
        )
        self._rpc_urls = dict(_PUBLIC_DISCOVERY_RPCS)
        if rpc_urls is not None:
            self._rpc_urls.update({int(chain_id): str(url) for chain_id, url in rpc_urls.items() if str(url).strip()})
        self._metadata_cache: dict[tuple[int, str], OnchainProviderToken | None] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def search_tokens(
        self,
        ticker: str,
        *,
        chain_ids: tuple[int, ...],
    ) -> tuple[OnchainProviderToken, ...]:
        seed = canonical_onchain_asset_seed(ticker)
        exact_contract = seed if seed.startswith("0x") else None
        try:
            if exact_contract is None:
                payload = await self._get_payload("/latest/dex/search", params={"q": seed})
            else:
                payload = await self._get_payload(f"/latest/dex/tokens/{exact_contract}")
        except (httpx.HTTPError, ValueError) as exc:
            raise OnchainProviderUnavailable("dexscreener_token_search_request_failed") from exc
        if not isinstance(payload, Mapping):
            raise OnchainProviderUnavailable("dexscreener_token_search_response_invalid")
        pairs = payload.get("pairs")
        if isinstance(pairs, str | bytes) or not isinstance(pairs, Sequence):
            raise OnchainProviderUnavailable("dexscreener_token_search_response_invalid")

        allowed_chains = set(chain_ids)
        grouped: dict[tuple[int, str], dict[str, Any]] = defaultdict(
            lambda: {"liquidity_usd": Decimal("0"), "pairs": set()}
        )
        for pair in pairs:
            if not isinstance(pair, Mapping):
                continue
            chain_id = _CHAIN_IDS.get(str(pair.get("chainId") or "").strip().lower())
            if chain_id is None or chain_id not in allowed_chains:
                continue
            token = _matching_token(pair, seed, exact_contract=exact_contract)
            if token is None:
                continue
            address = str(token.get("address") or "").strip().lower()
            if len(address) != 42 or not address.startswith("0x"):
                continue
            item = grouped[(chain_id, address)]
            item["symbol"] = str(token.get("symbol") or "").strip().upper()
            item["name"] = str(token.get("name") or item["symbol"]).strip()
            item["liquidity_usd"] += _liquidity_usd(pair)
            item["pairs"].add(str(pair.get("pairAddress") or ""))

        ranked = sorted(
            grouped.items(),
            key=lambda item: (-item[1]["liquidity_usd"], item[0][0], item[0][1]),
        )[:8]
        by_chain: dict[int, list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
        for (chain_id, address), evidence in ranked:
            by_chain[chain_id].append((address, evidence))
        outcomes = await asyncio.gather(
            *(self._verify_chain(chain_id, values) for chain_id, values in by_chain.items())
        )
        verified = tuple(value for values in outcomes for value in values)
        if grouped and not verified:
            raise OnchainProviderUnavailable("dexscreener_onchain_metadata_unavailable")
        return verified

    async def _get_payload(self, path: str, *, params: Mapping[str, str] | None = None) -> Any:
        for attempt in range(3):
            try:
                response = await self._client.get(path, params=params)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.2 * (attempt + 1))
        raise RuntimeError("dexscreener_retry_invariant")

    async def _verify_chain(
        self,
        chain_id: int,
        values: list[tuple[str, Mapping[str, Any]]],
    ) -> tuple[OnchainProviderToken, ...]:
        observations: list[OnchainProviderToken] = []
        for address, evidence in values:
            value = await self._verified_observation(chain_id=chain_id, address=address, evidence=evidence)
            if value is not None:
                observations.append(value)
            await asyncio.sleep(0.1)
        return tuple(observations)

    async def _verified_observation(
        self,
        *,
        chain_id: int,
        address: str,
        evidence: Mapping[str, Any],
    ) -> OnchainProviderToken | None:
        rpc_url = self._rpc_urls.get(chain_id)
        if rpc_url is None:
            return None
        cache_key = (chain_id, address)
        cached = self._metadata_cache.get(cache_key)
        if cached is not None:
            return cached.model_copy(
                update={
                    "liquidity_usd": evidence.get("liquidity_usd"),
                    "pair_count": len(evidence.get("pairs") or ()),
                }
            )
        try:
            code, name_raw, symbol_raw, decimals_raw = await self._rpc_batch(rpc_url, address)
            if not isinstance(code, str) or code in {"0x", "0x0"}:
                return None
            symbol = _decode_abi_text(symbol_raw).strip().upper()
            name = _decode_abi_text(name_raw).strip()
            decimals = int(str(decimals_raw), 16)
            if symbol != str(evidence.get("symbol") or "").strip().upper() or not 0 <= decimals <= 255:
                return None
            observation = OnchainProviderToken(
                provider="dexscreener",
                chain_id=chain_id,
                chain_name=_CHAIN_NAMES.get(chain_id, f"EVM {chain_id}"),
                contract_address=address,
                symbol=symbol,
                name=name or str(evidence.get("name") or symbol),
                decimals=decimals,
                verified=True,
                liquidity_usd=evidence.get("liquidity_usd"),
                pair_count=len(evidence.get("pairs") or ()),
            )
            self._metadata_cache[cache_key] = observation
            return observation
        except (ArithmeticError, TypeError, ValueError, httpx.HTTPError):
            return None

    async def _rpc_batch(self, rpc_url: str, address: str) -> tuple[Any, Any, Any, Any]:
        calls = (
            ("eth_getCode", [address, "latest"]),
            ("eth_call", [{"to": address, "data": _SELECTORS["name"]}, "latest"]),
            ("eth_call", [{"to": address, "data": _SELECTORS["symbol"]}, "latest"]),
            ("eth_call", [{"to": address, "data": _SELECTORS["decimals"]}, "latest"]),
        )
        for attempt in range(3):
            try:
                response = await self._client.post(
                    rpc_url,
                    json=[
                        {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
                        for index, (method, params) in enumerate(calls)
                    ],
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))
                continue
            if not isinstance(payload, str | bytes) and isinstance(payload, Sequence):
                by_id = {
                    int(str(item["id"])): item["result"]
                    for item in payload
                    if isinstance(item, Mapping) and item.get("error") is None and "id" in item and "result" in item
                }
                if set(by_id) == set(range(len(calls))):
                    return by_id[0], by_id[1], by_id[2], by_id[3]
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
        raise ValueError("onchain_discovery_rpc_response_invalid")


def _matching_token(
    pair: Mapping[str, Any],
    seed: str,
    *,
    exact_contract: str | None,
) -> Mapping[str, Any] | None:
    for key in ("baseToken", "quoteToken"):
        value = pair.get(key)
        if not isinstance(value, Mapping):
            continue
        address = str(value.get("address") or "").strip().lower()
        symbol = str(value.get("symbol") or "").strip().upper()
        if (exact_contract is not None and address == exact_contract) or (exact_contract is None and symbol == seed):
            return value
    return None


def _liquidity_usd(pair: Mapping[str, Any]) -> Decimal:
    liquidity = pair.get("liquidity")
    if not isinstance(liquidity, Mapping):
        return Decimal("0")
    try:
        value = Decimal(str(liquidity.get("usd") or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return value if value.is_finite() and value > 0 else Decimal("0")


def _decode_abi_text(value: object) -> str:
    raw = bytes.fromhex(str(value).removeprefix("0x"))
    try:
        decoded = decode_abi(["string"], raw)[0]
        if isinstance(decoded, str):
            return decoded
    except (TypeError, ValueError, OverflowError):
        pass
    if len(raw) == 32:
        return raw.rstrip(b"\x00").decode("utf-8")
    raise ValueError("onchain_discovery_erc20_text_invalid")


__all__ = ["DexScreenerOnchainDiscoveryClient"]
