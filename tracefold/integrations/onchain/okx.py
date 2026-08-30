"""OKX DEX Aggregator read-only token-directory and quote adapter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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
    okx_approval_proxy_address,
    onchain_wallet_fingerprint,
)

_ORIGIN = "https://web3.okx.com"
_TIMEOUT_SECONDS = 10.0
_CHAIN_NAMES = {1: "Ethereum", 56: "BNB Chain", 8453: "Base", 42161: "Arbitrum One", 4663: "Robinhood Chain"}


class OkxOnchainClient:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        passphrase: str,
        transport: httpx.AsyncBaseTransport | None = None,
        clock_ms: Any | None = None,
    ) -> None:
        if not api_key.strip() or not api_secret.strip() or not passphrase.strip():
            raise ValueError("okx_onchain_credentials_invalid")
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()
        self._passphrase = passphrase.strip()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
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
        seed = canonical_onchain_asset_seed(ticker)
        exact_contract = seed if seed.startswith("0x") else None
        results: list[OnchainProviderToken] = []
        payloads = await asyncio.gather(
            *(
                self._get_json(
                    "/api/v6/dex/aggregator/all-tokens",
                    params={"chainIndex": str(chain_id)},
                )
                for chain_id in chain_ids
            )
        )
        for chain_id, payload in zip(chain_ids, payloads, strict=True):
            rows = payload.get("data")
            if isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
                raise OnchainProviderUnavailable("okx_token_directory_response_invalid")
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                row_contract = str(row.get("tokenContractAddress") or "").strip().lower()
                row_symbol = str(row.get("tokenSymbol") or "").strip().upper()
                if (exact_contract is not None and row_contract != exact_contract) or (
                    exact_contract is None and row_symbol != seed
                ):
                    continue
                try:
                    results.append(
                        OnchainProviderToken(
                            provider="okx",
                            chain_id=chain_id,
                            chain_name=_chain_name(chain_id),
                            contract_address=row.get("tokenContractAddress"),
                            symbol=row_symbol,
                            name=row.get("tokenName") or row_symbol,
                            decimals=int(str(row.get("decimals"))),
                            verified=False,
                        )
                    )
                except (TypeError, ValueError):
                    continue
        return tuple(results)

    async def quote(self, request: OnchainQuoteRequest) -> OnchainRouteQuote:
        started_ms = int(self._clock_ms())
        payload = await self._get_json(
            "/api/v6/dex/aggregator/quote",
            params={
                "chainIndex": str(request.chain_id),
                "amount": str(request.input_amount_raw),
                "fromTokenAddress": request.input_contract,
                "toTokenAddress": request.output_contract,
                "swapMode": "exactIn",
            },
        )
        rows = payload.get("data")
        if isinstance(rows, str | bytes) or not isinstance(rows, Sequence) or len(rows) != 1:
            raise OnchainProviderUnavailable("okx_quote_response_invalid")
        row = rows[0]
        if not isinstance(row, Mapping):
            raise OnchainProviderUnavailable("okx_quote_response_invalid")
        received_at_ms = int(self._clock_ms())
        try:
            output_raw = _positive_int(row.get("toTokenAmount"))
            from_amount = _positive_int(row.get("fromTokenAmount"))
            if from_amount != request.input_amount_raw or str(row.get("chainIndex")) != str(request.chain_id):
                raise ValueError("amount_mismatch")
            from_honeypot = _okx_token_honeypot(row.get("fromToken"), request.input_contract)
            to_honeypot = _okx_token_honeypot(row.get("toToken"), request.output_contract)
            risk_checked = from_honeypot is not None and to_honeypot is not None
            risk_blocked = from_honeypot is True or to_honeypot is True
            return OnchainRouteQuote(
                provider="okx",
                chain_id=request.chain_id,
                input_contract=request.input_contract,
                output_contract=request.output_contract,
                input_amount_raw=request.input_amount_raw,
                expected_output_raw=output_raw,
                minimum_output_raw=output_raw * (10_000 - request.slippage_bps) // 10_000,
                expected_output_usd=None,
                provider_fee_usd=None,
                gas_fee_usd=_okx_network_fee_usd(row),
                gas_limit=None,
                price_impact_bps=_percentage_to_bps(row.get("priceImpactPercent")),
                slippage_bps=request.slippage_bps,
                route_labels=_okx_route_labels(row.get("dexRouterList")),
                latency_ms=max(0, received_at_ms - started_ms),
                received_at_ms=received_at_ms,
                expires_at_ms=received_at_ms + 15_000,
                simulation_passed=None,
                risk_checked=risk_checked,
                risk_blocked=risk_blocked,
            )
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise OnchainProviderUnavailable("okx_quote_response_invalid") from exc

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
                "/api/v6/dex/aggregator/approve-transaction",
                params={
                    "chainIndex": str(request.chain_id),
                    "tokenContractAddress": request.input_contract,
                    "approveAmount": str(request.input_amount_raw),
                },
            ),
            self._get_json(
                "/api/v6/dex/aggregator/swap",
                params={
                    "chainIndex": str(request.chain_id),
                    "amount": str(request.input_amount_raw),
                    "fromTokenAddress": request.input_contract,
                    "toTokenAddress": request.output_contract,
                    "swapMode": "exactIn",
                    "slippagePercent": _bps_percent(request.slippage_bps),
                    "userWalletAddress": wallet,
                    "swapReceiverAddress": wallet,
                },
            ),
        )
        prepared_at_ms = int(self._clock_ms())
        try:
            approval_row = _single_mapping(approval_payload.get("data"))
            swap_row = _single_mapping(swap_payload.get("data"))
            router = _required_mapping(swap_row.get("routerResult"))
            transaction = _required_mapping(swap_row.get("tx"))
            approval_proxy = canonical_evm_address(approval_row.get("dexContractAddress"))
            if approval_proxy != okx_approval_proxy_address(request.chain_id):
                raise ValueError("approval_proxy_mismatch")
            minimum_output_raw = _positive_int(transaction.get("minReceiveAmount"))
            if _percentage_to_bps(transaction.get("slippagePercent")) != request.slippage_bps:
                raise ValueError("slippage_mismatch")
            quote = _okx_route_quote(
                request,
                router,
                started_ms=started_ms,
                received_at_ms=prepared_at_ms,
                minimum_output_raw=minimum_output_raw,
            )
            if quote.risk_blocked or minimum_output_raw > quote.expected_output_raw:
                raise ValueError("risk_blocked")
            approval = OnchainTransactionTemplate(
                provider="okx",
                leg="approval",
                chain_id=request.chain_id,
                from_address=wallet,
                to_address=request.input_contract,
                data=approval_row.get("data"),
                value=0,
                gas_limit=_positive_int(approval_row.get("gasLimit")),
                gas_price=_positive_int(approval_row.get("gasPrice")),
            )
            swap = OnchainTransactionTemplate(
                provider="okx",
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
                provider="okx",
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
            raise OnchainProviderUnavailable("okx_execution_response_invalid") from exc

    async def _get_json(self, path: str, *, params: Mapping[str, str]) -> Mapping[str, Any]:
        request = self._client.build_request("GET", path, params=params)
        timestamp = _okx_timestamp(int(self._clock_ms()))
        prehash = f"{timestamp}GET{request.url.raw_path.decode()}"
        signature = base64.b64encode(
            hmac.new(self._api_secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        request.headers.update(
            {
                "OK-ACCESS-KEY": self._api_key,
                "OK-ACCESS-SIGN": signature,
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self._passphrase,
            }
        )
        try:
            response = await self._client.send(request)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OnchainProviderUnavailable("okx_request_failed") from exc
        if not isinstance(payload, Mapping) or str(payload.get("code")) != "0":
            raise OnchainProviderUnavailable("okx_provider_rejected")
        return payload


def _okx_timestamp(now_ms: int) -> str:
    value = datetime.fromtimestamp(now_ms / 1_000, tz=UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
    return format(Decimal(value) / Decimal(100), "f")


def _required_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("mapping_required")
    return value


def _single_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence) or len(value) != 1:
        raise ValueError("single_mapping_required")
    return _required_mapping(value[0])


def _okx_route_quote(
    request: OnchainQuoteRequest,
    row: Mapping[str, Any],
    *,
    started_ms: int,
    received_at_ms: int,
    minimum_output_raw: int | None = None,
) -> OnchainRouteQuote:
    output_raw = _positive_int(row.get("toTokenAmount"))
    from_amount = _positive_int(row.get("fromTokenAmount"))
    if from_amount != request.input_amount_raw or str(row.get("chainIndex")) != str(request.chain_id):
        raise ValueError("amount_mismatch")
    from_honeypot = _okx_token_honeypot(row.get("fromToken"), request.input_contract)
    to_honeypot = _okx_token_honeypot(row.get("toToken"), request.output_contract)
    return OnchainRouteQuote(
        provider="okx",
        chain_id=request.chain_id,
        input_contract=request.input_contract,
        output_contract=request.output_contract,
        input_amount_raw=request.input_amount_raw,
        expected_output_raw=output_raw,
        minimum_output_raw=(
            minimum_output_raw
            if minimum_output_raw is not None
            else output_raw * (10_000 - request.slippage_bps) // 10_000
        ),
        expected_output_usd=None,
        provider_fee_usd=None,
        gas_fee_usd=_okx_network_fee_usd(row),
        gas_limit=None,
        price_impact_bps=_percentage_to_bps(row.get("priceImpactPercent")),
        slippage_bps=request.slippage_bps,
        route_labels=_okx_route_labels(row.get("dexRouterList")),
        latency_ms=max(0, received_at_ms - started_ms),
        received_at_ms=received_at_ms,
        expires_at_ms=received_at_ms + 15_000,
        simulation_passed=None,
        risk_checked=from_honeypot is not None and to_honeypot is not None,
        risk_blocked=from_honeypot is True or to_honeypot is True,
    )


def _percentage_to_bps(value: object) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value)) * Decimal(100)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("percentage_invalid") from exc
    if not parsed.is_finite():
        raise ValueError("percentage_invalid")
    return int(abs(parsed))


def _okx_network_fee_usd(row: Mapping[str, Any]) -> Decimal | None:
    value = row.get("tradeFee")
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("trade_fee_invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("trade_fee_invalid")
    return parsed


def _okx_token_honeypot(value: object, expected_contract: str) -> bool | None:
    if not isinstance(value, Mapping):
        raise ValueError("token_info_invalid")
    contract = str(value.get("tokenContractAddress") or "").strip().lower()
    if contract != expected_contract:
        raise ValueError("token_identity_mismatch")
    flag = value.get("isHoneyPot")
    if flag is None:
        return None
    if not isinstance(flag, bool):
        raise ValueError("token_honeypot_flag_invalid")
    return flag


def _okx_route_labels(value: object) -> tuple[str, ...]:
    labels: list[str] = []
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for router in value:
            if not isinstance(router, Mapping):
                continue
            protocols = router.get("dexProtocol")
            if isinstance(protocols, Mapping) and protocols.get("dexName"):
                labels.append(str(protocols["dexName"]))
            elif isinstance(protocols, Sequence) and not isinstance(protocols, str | bytes):
                labels.extend(
                    str(protocol["dexName"])
                    for protocol in protocols
                    if isinstance(protocol, Mapping) and protocol.get("dexName")
                )
    return tuple(dict.fromkeys(labels))


def _chain_name(chain_id: int) -> str:
    return _CHAIN_NAMES.get(chain_id, f"EVM {chain_id}")


__all__ = ["OkxOnchainClient"]
