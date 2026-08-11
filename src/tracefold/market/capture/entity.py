from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from eth_utils.address import is_address, to_checksum_address
from solders.pubkey import Pubkey

from tracefold.market.identity.chain_identity import evm_query_chain_ids, normalize_query_chain_id

EVM_QUERY_CHAINS = evm_query_chain_ids()


@dataclass(frozen=True, slots=True)
class ExtractedEntity:
    entity_type: str
    raw_value: str
    normalized_value: str
    chain: str | None
    token_resolution_status: str
    confidence: float
    source: str
    text_surface: str = "primary"
    span_start: int = 0
    span_end: int = 0
    sentence_id: int = 0
    local_group_key: str = "primary:0"


def normalize_ca(value: str, *, chain: str | None = None) -> tuple[str, str]:
    text = value.strip()
    normalized_chain = normalize_query_chain_id(chain) if chain is not None else None
    if is_address(text):
        if normalized_chain is None:
            return ("evm_unknown", to_checksum_address(text))
        if normalized_chain in EVM_QUERY_CHAINS:
            return (normalized_chain, to_checksum_address(text))
    if is_valid_ton_friendly_address(text):
        return ("ton", text)
    try:
        pubkey = Pubkey.from_string(text)
    except ValueError as exc:
        raise ValueError(f"invalid token CA: {value}") from exc
    return ("solana", str(pubkey))


def is_valid_ton_friendly_address(raw: str) -> bool:
    if len(raw) != 48:
        return False
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except (binascii.Error, ValueError):
        return False
    if len(decoded) != 36:
        return False
    return _crc16_xmodem(decoded[:34]) == int.from_bytes(decoded[34:], "big")


def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc
