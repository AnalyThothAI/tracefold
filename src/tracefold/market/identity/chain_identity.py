from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class _ChainCapability:
    canonical_id: str
    query_id: str
    aliases: tuple[str, ...]
    evm_address: bool


_CHAIN_CAPABILITIES = (
    _ChainCapability("solana", "solana", ("sol", "solana"), False),
    _ChainCapability("eip155:1", "eth", ("eip155:1", "eth", "ethereum"), True),
    _ChainCapability("eip155:56", "bsc", ("eip155:56", "bsc", "bnb", "bnb_chain"), True),
    _ChainCapability("eip155:8453", "base", ("eip155:8453", "base"), True),
    _ChainCapability("ton", "ton", ("ton", "toncoin", "the open network"), False),
    _ChainCapability("robinhood", "robinhood", ("robinhood",), True),
)
_CHAIN_CAPABILITY_BY_ALIAS = {alias: capability for capability in _CHAIN_CAPABILITIES for alias in capability.aliases}


def canonical_chain_id(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    capability = _CHAIN_CAPABILITY_BY_ALIAS.get(normalized)
    return capability.canonical_id if capability is not None else normalized


def normalize_query_chain_id(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    capability = _CHAIN_CAPABILITY_BY_ALIAS.get(normalized)
    return capability.query_id if capability is not None else normalized


def evm_query_chain_ids() -> frozenset[str]:
    return frozenset(
        {
            "evm",
            "evm_unknown",
            *(capability.query_id for capability in _CHAIN_CAPABILITIES if capability.evm_address),
        }
    )


def canonical_evm_chain_ids() -> tuple[str, ...]:
    return tuple(capability.canonical_id for capability in _CHAIN_CAPABILITIES if capability.evm_address)


def canonical_chain_address(chain_id: Any, address: Any) -> str:
    chain = canonical_chain_id(chain_id)
    value = str(address or "").strip()
    return value.lower() if chain.startswith("eip155:") or value.startswith(("0x", "0X")) else value


def chain_address_key(chain_id: Any, address: Any) -> tuple[str, str]:
    chain = canonical_chain_id(chain_id)
    return (chain, canonical_chain_address(chain, address))


__all__ = [
    "canonical_chain_address",
    "canonical_chain_id",
    "canonical_evm_chain_ids",
    "chain_address_key",
    "evm_query_chain_ids",
    "normalize_query_chain_id",
]
