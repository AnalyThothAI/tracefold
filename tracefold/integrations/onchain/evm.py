"""One local EVM signer and a small JSON-RPC adapter shared by every route."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import httpx
from eth_account import Account
from eth_utils import to_checksum_address

from tracefold.trading import (
    OnchainSignedTransaction,
    OnchainTransactionTemplate,
    canonical_evm_address,
)

_PRIVATE_KEY_RE = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")
_TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")


class EvmPrivateKeySigner:
    """The sole manual-wallet signer used for OKX, 1inch, and future route adapters."""

    def __init__(self, private_key: str) -> None:
        normalized = private_key.strip()
        if _PRIVATE_KEY_RE.fullmatch(normalized) is None:
            raise ValueError("onchain_wallet_private_key_invalid")
        self._account = Account.from_key(normalized)
        self.address = canonical_evm_address(self._account.address)

    def sign(
        self,
        template: OnchainTransactionTemplate,
        *,
        nonce: int,
        gas_limit: int,
        gas_price: int,
    ) -> OnchainSignedTransaction:
        if template.from_address != self.address:
            raise ValueError("onchain_signer_wallet_mismatch")
        transaction = {
            "chainId": template.chain_id,
            "nonce": int(nonce),
            "to": to_checksum_address(template.to_address),
            "data": template.data,
            "value": template.value,
            "gas": int(gas_limit),
            "gasPrice": int(gas_price),
        }
        signed = self._account.sign_transaction(transaction)
        raw = "0x" + bytes(signed.raw_transaction).hex()
        tx_hash = "0x" + bytes(signed.hash).hex()
        return OnchainSignedTransaction(
            provider=template.provider,
            leg=template.leg,
            chain_id=template.chain_id,
            wallet_address=self.address,
            nonce=nonce,
            raw_transaction=raw,
            transaction_hash=tx_hash,
        )


class EvmJsonRpcClient:
    def __init__(
        self,
        *,
        rpc_url: str,
        chain_id: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized = rpc_url.strip()
        if not normalized.startswith("https://"):
            raise ValueError("onchain_rpc_url_invalid")
        if chain_id <= 0:
            raise ValueError("onchain_rpc_chain_invalid")
        self.chain_id = int(chain_id)
        self._request_id = 0
        self._client = httpx.AsyncClient(
            base_url=normalized,
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def verify_chain(self) -> None:
        observed = int(await self._rpc("eth_chainId", []), 16)
        if observed != self.chain_id:
            raise RuntimeError("onchain_rpc_chain_mismatch")

    async def pending_nonce(self, address: str) -> int:
        value = await self._rpc("eth_getTransactionCount", [canonical_evm_address(address), "pending"])
        return int(str(value), 16)

    async def gas_price(self) -> int:
        value = int(str(await self._rpc("eth_gasPrice", [])), 16)
        if value <= 0:
            raise RuntimeError("onchain_rpc_gas_price_invalid")
        return value

    async def estimate_gas(self, template: OnchainTransactionTemplate) -> int:
        value = await self._rpc(
            "eth_estimateGas",
            [
                {
                    "from": template.from_address,
                    "to": template.to_address,
                    "data": template.data,
                    "value": hex(template.value),
                }
            ],
        )
        gas = int(str(value), 16)
        if gas <= 0:
            raise RuntimeError("onchain_rpc_gas_estimate_invalid")
        return gas * 12 // 10

    async def simulate(self, template: OnchainTransactionTemplate) -> None:
        result = await self._rpc(
            "eth_call",
            [
                {
                    "from": template.from_address,
                    "to": template.to_address,
                    "data": template.data,
                    "value": hex(template.value),
                },
                "latest",
            ],
        )
        if not isinstance(result, str) or not result.startswith("0x"):
            raise RuntimeError("onchain_rpc_simulation_invalid")

    async def allowance(self, *, token: str, owner: str, spender: str) -> int:
        owner_word = canonical_evm_address(owner)[2:].rjust(64, "0")
        spender_word = canonical_evm_address(spender)[2:].rjust(64, "0")
        value = await self._rpc(
            "eth_call",
            [
                {
                    "to": canonical_evm_address(token),
                    "data": f"0xdd62ed3e{owner_word}{spender_word}",
                },
                "latest",
            ],
        )
        return int(str(value), 16)

    async def send_raw_transaction(self, signed: OnchainSignedTransaction) -> str:
        if signed.raw_transaction is None:
            raise ValueError("onchain_signed_transaction_raw_missing")
        result = str(await self._rpc("eth_sendRawTransaction", [signed.raw_transaction])).lower()
        if _TX_HASH_RE.fullmatch(result) is None or result != signed.transaction_hash:
            raise RuntimeError("onchain_rpc_transaction_hash_mismatch")
        return result

    async def receipt(self, transaction_hash: str) -> Mapping[str, Any] | None:
        if _TX_HASH_RE.fullmatch(transaction_hash) is None:
            raise ValueError("onchain_rpc_transaction_hash_invalid")
        result = await self._rpc("eth_getTransactionReceipt", [transaction_hash])
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise RuntimeError("onchain_rpc_receipt_invalid")
        return result

    async def transaction_known(self, transaction_hash: str) -> bool:
        normalized = transaction_hash.lower()
        if _TX_HASH_RE.fullmatch(normalized) is None:
            raise ValueError("onchain_rpc_transaction_hash_invalid")
        result = await self._rpc("eth_getTransactionByHash", [normalized])
        if result is None:
            return False
        if not isinstance(result, Mapping) or str(result.get("hash", "")).lower() != normalized:
            raise RuntimeError("onchain_rpc_transaction_invalid")
        return True

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        try:
            response = await self._client.post(
                "",
                json={"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("onchain_rpc_request_failed") from exc
        if not isinstance(payload, Mapping) or payload.get("id") != self._request_id:
            raise RuntimeError("onchain_rpc_response_invalid")
        if payload.get("error") is not None or "result" not in payload:
            raise RuntimeError("onchain_rpc_rejected")
        return payload["result"]


__all__ = ["EvmJsonRpcClient", "EvmPrivateKeySigner"]
