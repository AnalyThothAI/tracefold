"""The little EVM encoding the wallet tape reads: addresses, topic words, and a `uint256` log payload.

Pure string work with no network and no ABI library. It lives beside the classifier because the
classifier is the thing that reasons about topics; the RPC adapter imports it rather than keeping a
second copy, the same way the venue adapters import the instrument vocabulary they answer in.
"""

from __future__ import annotations

_HEX_DIGITS = frozenset("0123456789abcdef")


def normalize_address(value: str) -> str:
    """Lowercase 0x-prefixed 20-byte address, or `""` when the value is not one.

    Case is not identity on chain and the provider mixes checksummed and lowercase forms, so an address
    takes one shape before it is compared, stored, or used as a topic.
    """

    text = str(value or "").strip().lower()
    if not text.startswith("0x"):
        return ""
    body = text[2:]
    if len(body) != 40 or not set(body) <= _HEX_DIGITS:
        return ""
    return f"0x{body}"


def address_topic(address: str) -> str:
    """The 32-byte topic word for an address, as `eth_getLogs` indexes it."""

    normalized = normalize_address(address)
    if not normalized:
        raise ValueError("chain_address_invalid")
    return "0x" + normalized[2:].rjust(64, "0")


def topic_address(topic: str) -> str:
    """The address inside a 32-byte topic word, or `""` when the word does not hold one."""

    text = str(topic or "").strip().lower()
    if not text.startswith("0x") or len(text) != 66:
        return ""
    return normalize_address(f"0x{text[-40:]}")


def transfer_amount(data: str) -> int | None:
    """The `uint256` in a `Transfer` log's payload, or `None` when it is not one readable word."""

    text = str(data or "").strip().lower()
    if not text.startswith("0x"):
        return None
    body = text[2:]
    if not body or len(body) > 64 or not set(body) <= _HEX_DIGITS:
        return None
    return int(body, 16)


__all__ = ["address_topic", "normalize_address", "topic_address", "transfer_amount"]
