"""Binance Alpha token discovery with an explicit Web3 quote capability gap.

The official public Alpha Token List is useful evidence for ticker-to-contract resolution. Binance
does not currently publish a general-token Web3 swap quote contract, so this adapter deliberately
does not substitute Alpha market data, Convert, or Prediction Trading for an onchain route quote.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import httpx

from tracefold.trading import (
    OnchainExecutionPlan,
    OnchainProviderToken,
    OnchainProviderUnavailable,
    OnchainQuoteRequest,
    OnchainRouteQuote,
    canonical_onchain_asset_seed,
)

_ORIGIN = "https://www.binance.com"
_TOKEN_LIST_PATH = "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
_TIMEOUT_SECONDS = 5.0


class BinanceOnchainClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=_ORIGIN,
            timeout=httpx.Timeout(_TIMEOUT_SECONDS),
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search_tokens(
        self,
        ticker: str,
        *,
        chain_ids: tuple[int, ...],
    ) -> tuple[OnchainProviderToken, ...]:
        try:
            response = await self._client.get(_TOKEN_LIST_PATH)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OnchainProviderUnavailable("binance_alpha_token_list_request_failed") from exc
        if not isinstance(payload, Mapping) or str(payload.get("code")) != "000000":
            raise OnchainProviderUnavailable("binance_alpha_token_list_rejected")
        rows = payload.get("data")
        if isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
            raise OnchainProviderUnavailable("binance_alpha_token_list_response_invalid")

        seed = canonical_onchain_asset_seed(ticker)
        exact_contract = seed if seed.startswith("0x") else None
        allowed_chains = set(chain_ids)
        results: list[OnchainProviderToken] = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("offline") is not False or row.get("fullyDelisted") is not False:
                continue
            row_contract = str(row.get("contractAddress") or "").strip().lower()
            row_symbol = str(row.get("symbol") or "").strip().upper()
            if (exact_contract is not None and row_contract != exact_contract) or (
                exact_contract is None and row_symbol != seed
            ):
                continue
            try:
                chain_id = int(str(row.get("chainId")))
                if chain_id not in allowed_chains:
                    continue
                results.append(
                    OnchainProviderToken(
                        provider="binance",
                        chain_id=chain_id,
                        chain_name=str(row.get("chainName") or f"EVM {chain_id}"),
                        contract_address=row.get("contractAddress"),
                        symbol=row_symbol,
                        name=row.get("name") or row_symbol,
                        decimals=int(str(row.get("decimals"))),
                        verified=True,
                    )
                )
            except (TypeError, ValueError):
                continue
        return tuple(results)

    async def quote(self, _request: OnchainQuoteRequest) -> OnchainRouteQuote:
        raise OnchainProviderUnavailable("binance_general_web3_swap_api_unpublished")

    async def prepare_execution(
        self,
        _request: OnchainQuoteRequest,
        *,
        wallet_address: str,
    ) -> OnchainExecutionPlan:
        del wallet_address
        raise OnchainProviderUnavailable("binance_general_web3_swap_api_unpublished")


__all__ = ["BinanceOnchainClient"]
