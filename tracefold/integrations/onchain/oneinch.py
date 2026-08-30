"""1inch Classic Swap read-only token search and quote adapter."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from tracefold.trading import (
    OnchainExecutionPlan,
    OnchainProviderToken,
    OnchainProviderUnavailable,
    OnchainQuoteRequest,
    OnchainRouteQuote,
    OnchainTransactionTemplate,
    canonical_evm_address,
    canonical_onchain_asset_seed,
    onchain_wallet_fingerprint,
)

_ORIGIN = "https://api.1inch.com"
_TIMEOUT_SECONDS = 5.0
_CHAIN_NAMES = {1: "Ethereum", 56: "BNB Chain", 8453: "Base", 42161: "Arbitrum One", 4663: "Robinhood Chain"}


class OneInchOnchainClient:
    def __init__(
        self,
        *,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        clock_ms: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("oneinch_onchain_api_key_invalid")
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._client = httpx.AsyncClient(
            base_url=_ORIGIN,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
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
        seed = canonical_onchain_asset_seed(ticker)
        exact_contract = seed if seed.startswith("0x") else None
        payload = await self._get_json(
            "/token/v1.6/search",
            params={
                "query": seed,
                "ignore_listed": "false",
                "only_positive_rating": "true",
                "limit": "20",
            },
        )
        rows: object = payload.get("tokens") if isinstance(payload, Mapping) else payload
        if isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
            raise OnchainProviderUnavailable("oneinch_token_search_response_invalid")
        allowed_chains = set(chain_ids)
        results: list[OnchainProviderToken] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            row_contract = str(row.get("address") or "").strip().lower()
            row_symbol = str(row.get("symbol") or "").strip().upper()
            if (exact_contract is not None and row_contract != exact_contract) or (
                exact_contract is None and row_symbol != seed
            ):
                continue
            if row.get("blacklisted") is not False or row.get("tradingRestricted", False) is not False:
                continue
            try:
                chain_id = int(str(row.get("chainId")))
                if chain_id not in allowed_chains:
                    continue
                rating = int(str(row.get("rating") or 0))
                results.append(
                    OnchainProviderToken(
                        provider="oneinch",
                        chain_id=chain_id,
                        chain_name=_chain_name(chain_id),
                        contract_address=row.get("address"),
                        symbol=row_symbol,
                        name=row.get("name") or row_symbol,
                        decimals=int(str(row.get("decimals"))),
                        verified=rating > 0,
                    )
                )
            except (TypeError, ValueError):
                continue
        return tuple(results)

    async def quote(self, request: OnchainQuoteRequest) -> OnchainRouteQuote:
        started_ms = int(self._clock_ms())
        payload = await self._get_json(
            f"/swap/v6.1/{request.chain_id}/quote",
            params={
                "src": request.input_contract,
                "dst": request.output_contract,
                "amount": str(request.input_amount_raw),
                "includeTokensInfo": "true",
                "includeProtocols": "true",
                "includeGas": "true",
            },
        )
        received_at_ms = int(self._clock_ms())
        try:
            _oneinch_token_identity(payload.get("srcToken"), request.input_contract)
            _oneinch_token_identity(payload.get("dstToken"), request.output_contract)
            output_raw = _positive_int(payload.get("dstAmount"))
            gas_value = payload.get("gas")
            gas_limit = _positive_int(gas_value) if gas_value is not None else None
            return OnchainRouteQuote(
                provider="oneinch",
                chain_id=request.chain_id,
                input_contract=request.input_contract,
                output_contract=request.output_contract,
                input_amount_raw=request.input_amount_raw,
                expected_output_raw=output_raw,
                minimum_output_raw=output_raw * (10_000 - request.slippage_bps) // 10_000,
                expected_output_usd=None,
                provider_fee_usd=None,
                gas_fee_usd=None,
                gas_limit=gas_limit,
                price_impact_bps=None,
                slippage_bps=request.slippage_bps,
                route_labels=_protocol_names(payload.get("protocols")),
                latency_ms=max(0, received_at_ms - started_ms),
                received_at_ms=received_at_ms,
                expires_at_ms=received_at_ms + 15_000,
                simulation_passed=None,
                risk_checked=False,
                risk_blocked=False,
            )
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise OnchainProviderUnavailable("oneinch_quote_response_invalid") from exc

    async def prepare_execution(
        self,
        request: OnchainQuoteRequest,
        *,
        wallet_address: str,
    ) -> OnchainExecutionPlan:
        wallet = canonical_evm_address(wallet_address)
        started_ms = int(self._clock_ms())
        approval_payload, swap_payload = await asyncio.gather(
            self._get_json(
                f"/swap/v6.1/{request.chain_id}/approve/transaction",
                params={
                    "tokenAddress": request.input_contract,
                    "amount": str(request.input_amount_raw),
                },
            ),
            self._get_json(
                f"/swap/v6.1/{request.chain_id}/swap",
                params={
                    "src": request.input_contract,
                    "dst": request.output_contract,
                    "amount": str(request.input_amount_raw),
                    "from": wallet,
                    "origin": wallet,
                    "receiver": wallet,
                    "slippage": _bps_percent(request.slippage_bps),
                    "includeTokensInfo": "true",
                    "includeProtocols": "true",
                    "includeGas": "true",
                    "disableEstimate": "false",
                    "allowPartialFill": "false",
                },
            ),
        )
        prepared_at_ms = int(self._clock_ms())
        try:
            if not isinstance(approval_payload, Mapping) or not isinstance(swap_payload, Mapping):
                raise ValueError("mapping_required")
            _oneinch_token_identity(swap_payload.get("srcToken"), request.input_contract)
            _oneinch_token_identity(swap_payload.get("dstToken"), request.output_contract)
            transaction = swap_payload.get("tx")
            if not isinstance(transaction, Mapping):
                raise ValueError("transaction_required")
            output_raw = _positive_int(swap_payload.get("dstAmount"))
            quote = OnchainRouteQuote(
                provider="oneinch",
                chain_id=request.chain_id,
                input_contract=request.input_contract,
                output_contract=request.output_contract,
                input_amount_raw=request.input_amount_raw,
                expected_output_raw=output_raw,
                minimum_output_raw=output_raw * (10_000 - request.slippage_bps) // 10_000,
                expected_output_usd=None,
                provider_fee_usd=None,
                gas_fee_usd=None,
                gas_limit=_positive_int(transaction.get("gas")),
                price_impact_bps=None,
                slippage_bps=request.slippage_bps,
                route_labels=_protocol_names(swap_payload.get("protocols")),
                latency_ms=max(0, prepared_at_ms - started_ms),
                received_at_ms=prepared_at_ms,
                expires_at_ms=prepared_at_ms + 15_000,
                simulation_passed=True,
                risk_checked=False,
                risk_blocked=False,
            )
            approval = OnchainTransactionTemplate(
                provider="oneinch",
                leg="approval",
                chain_id=request.chain_id,
                from_address=wallet,
                to_address=approval_payload.get("to"),
                data=approval_payload.get("data"),
                value=_nonnegative_int(approval_payload.get("value")),
                gas_limit=None,
                gas_price=_positive_int(approval_payload.get("gasPrice")),
            )
            swap = OnchainTransactionTemplate(
                provider="oneinch",
                leg="swap",
                chain_id=request.chain_id,
                from_address=transaction.get("from"),
                to_address=transaction.get("to"),
                data=transaction.get("data"),
                value=_nonnegative_int(transaction.get("value")),
                gas_limit=_positive_int(transaction.get("gas")),
                gas_price=_positive_int(transaction.get("gasPrice")),
            )
            return OnchainExecutionPlan(
                provider="oneinch",
                wallet_address=wallet,
                wallet_fingerprint=onchain_wallet_fingerprint(wallet),
                request=request,
                quote=quote,
                approval=approval,
                swap=swap,
                prepared_at_ms=prepared_at_ms,
                expires_at_ms=prepared_at_ms + 15_000,
            )
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise OnchainProviderUnavailable("oneinch_execution_response_invalid") from exc

    async def _get_json(self, path: str, *, params: Mapping[str, str]) -> Any:
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OnchainProviderUnavailable("oneinch_request_failed") from exc


def _positive_int(value: object) -> int:
    parsed = int(str(value))
    if parsed <= 0:
        raise ValueError("positive_integer_required")
    return parsed


def _nonnegative_int(value: object) -> int:
    parsed = int(str(value))
    if parsed < 0:
        raise ValueError("nonnegative_integer_required")
    return parsed


def _bps_percent(value: int) -> str:
    from decimal import Decimal

    return format(Decimal(value) / Decimal(100), "f")


def _oneinch_token_identity(value: object, expected_contract: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("token_info_invalid")
    contract = str(value.get("address") or "").strip().lower()
    if contract != expected_contract:
        raise ValueError("token_identity_mismatch")


def _protocol_names(value: object) -> tuple[str, ...]:
    labels: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            name = node.get("name")
            if name:
                labels.append(str(name))
            for child in node.values():
                visit(child)
        elif isinstance(node, Sequence) and not isinstance(node, str | bytes):
            for child in node:
                visit(child)

    visit(value)
    return tuple(dict.fromkeys(labels))


def _chain_name(chain_id: int) -> str:
    return _CHAIN_NAMES.get(chain_id, f"EVM {chain_id}")


__all__ = ["OneInchOnchainClient"]
