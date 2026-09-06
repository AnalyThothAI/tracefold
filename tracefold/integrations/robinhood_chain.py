"""Read-only JSON-RPC access to Robinhood Chain (chain id 4663), for the News wallet tape (#572 PR-1).

Five calls and nothing else: the head block, `eth_getLogs` over a block range, one transaction receipt,
one block header for its timestamp, and the two ERC-20 metadata reads (`symbol`, `decimals`) behind a
per-token cache. There is no signer, no nonce, no `eth_sendRawTransaction` and no account: this adapter
can only read what the chain already published.

Three facts about the public endpoint decided the shape here:

* it answers `403` to some default library user agents (`Python-urllib/3.x` is refused), so every request
  carries an explicit product user agent rather than whatever the HTTP library happens to send;
* it is documented as rate-limited and not for production use, so one turn makes one bounded attempt per
  call and never retries inside the adapter -- a failed turn is the loop's business, and the next turn
  re-reads from the durable high-water mark;
* `eth_getLogs` answers a 100,000-block range filtered by a 35-address topic array in 1.1-1.7 s, while
  `eth_call` state older than ~6,100 blocks is gone. Logs are therefore the catch-up mechanism and state
  is not, which is why nothing here reads a historical balance (#572 §3.3).

`blockTimestamp` is present on a log but is always `0x0` on this endpoint, so the block's own header is
what dates an event. Headers are immutable once mined, which is why they are cached for the process's
life while nothing else is.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx

from tracefold.integrations.http_bounds import ResponseTooLarge, read_bounded
from tracefold.news.chain_tape.evm import normalize_address

# The one chain this adapter speaks to. Carried on every stored fill so a second chain can never be
# read as this one (#572 §5.2).
ROBINHOOD_CHAIN_ID: Final = 4663
ROBINHOOD_CHAIN_RPC_URL: Final = "https://rpc.mainnet.chain.robinhood.com"

# Not a browser string and not a lie: the endpoint refuses some library defaults, and an operator reading
# the provider's logs should be able to tell who is calling.
CHAIN_RPC_USER_AGENT: Final = "tracefold-news-chain-tape/1.0 (+https://github.com/AnalyThothAI/tracefold)"

_CONNECT_TIMEOUT_SECONDS: Final = 5.0
_READ_TIMEOUT_SECONDS: Final = 10.0
# A 100,000-block log answer measured at ~1.5 MB. The ceiling is for a pathological response, not for that.
_MAX_BYTES: Final = 32 * 1024 * 1024

_SYMBOL_SELECTOR: Final = "0x95d89b41"
_DECIMALS_SELECTOR: Final = "0x313ce567"
_BALANCE_OF_SELECTOR: Final = "0x70a08231"


class ChainRpcError(RuntimeError):
    """An anticipated RPC failure: timeout, transport, HTTP status, or a payload this adapter cannot read.

    `code` is a stable identifier for logs and telemetry (`chain_rpc_timeout`, `chain_rpc_http_error`,
    `chain_rpc_blocked`, `chain_rpc_rate_limited`, `chain_rpc_payload_invalid`, `chain_rpc_error`). It
    never carries a response body.
    """

    def __init__(self, code: str, *, status_code: int | None = None, rpc_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.rpc_code = rpc_code


@dataclass(frozen=True, slots=True)
class ChainLog:
    """One decoded log entry. `topics` stay hex strings: this adapter reads, it does not interpret ABIs."""

    address: str
    topics: tuple[str, ...]
    data: str
    block_number: int
    block_hash: str
    transaction_hash: str
    transaction_index: int
    log_index: int
    removed: bool = False


@dataclass(frozen=True, slots=True)
class ChainReceipt:
    """One transaction receipt: where it landed, whether it succeeded, and every log it emitted."""

    transaction_hash: str
    block_number: int
    block_hash: str
    transaction_index: int
    status: int
    logs: tuple[ChainLog, ...]


@dataclass(frozen=True, slots=True)
class ChainToken:
    """What an ERC-20 says about itself. Either field may be `None` for a contract that answers neither."""

    address: str
    symbol: str | None
    decimals: int | None


class RobinhoodChainClient:
    """One bounded JSON-RPC session. Every method makes exactly one attempt and raises `ChainRpcError`."""

    def __init__(
        self,
        *,
        rpc_url: str = ROBINHOOD_CHAIN_RPC_URL,
        chain_id: int = ROBINHOOD_CHAIN_ID,
        transport: httpx.AsyncBaseTransport | None = None,
        read_timeout_seconds: float = _READ_TIMEOUT_SECONDS,
    ) -> None:
        self.rpc_url = str(rpc_url).rstrip("/")
        self.chain_id = int(chain_id)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS),
            follow_redirects=False,
            transport=transport,
            headers={"user-agent": CHAIN_RPC_USER_AGENT, "content-type": "application/json"},
        )
        self._block_timestamps_ms: dict[int, int] = {}
        self._tokens: dict[str, ChainToken] = {}
        self._last_bytes = 0

    @property
    def last_response_bytes(self) -> int:
        """Byte count of the most recent answer, for the caller's provider-bytes counter."""

        return self._last_bytes

    async def aclose(self) -> None:
        await self._client.aclose()

    async def block_number(self) -> int:
        return _as_int(await self._call("eth_blockNumber", []))

    async def logs(
        self,
        *,
        from_block: int,
        to_block: int,
        topics: Sequence[str | None | Sequence[str]],
    ) -> tuple[ChainLog, ...]:
        """`eth_getLogs` over one block range with one topic filter and no `address` restriction.

        No contract filter is deliberate: the roster's tokens are not known in advance and change every
        hour, while the wallets are the stable thing. Filtering by the indexed sender/receiver topic is
        what makes a 100,000-block range answer in under two seconds (#572 §3.3).
        """

        payload = {
            "fromBlock": hex(max(0, int(from_block))),
            "toBlock": hex(max(0, int(to_block))),
            "topics": [_topic_filter(item) for item in topics],
        }
        result = await self._call("eth_getLogs", [payload])
        if not isinstance(result, list):
            raise ChainRpcError("chain_rpc_payload_invalid")
        return tuple(_log(item) for item in result)

    async def receipt(self, transaction_hash: str) -> ChainReceipt | None:
        """One receipt, or `None` when the node does not have that transaction."""

        result = await self._call("eth_getTransactionReceipt", [str(transaction_hash)])
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise ChainRpcError("chain_rpc_payload_invalid")
        return ChainReceipt(
            transaction_hash=str(result.get("transactionHash") or "").lower(),
            block_number=_as_int(result.get("blockNumber")),
            block_hash=str(result.get("blockHash") or "").lower(),
            transaction_index=_as_int(result.get("transactionIndex")),
            status=_as_int(result.get("status")),
            logs=tuple(_log(item) for item in result.get("logs") or ()),
        )

    async def block_timestamp_ms(self, block_number: int) -> int:
        """The block header's own time, in milliseconds. Cached: a mined header does not change.

        A log's `blockTimestamp` is `0x0` on this endpoint, so this is the only source of event time.
        """

        number = int(block_number)
        cached = self._block_timestamps_ms.get(number)
        if cached is not None:
            return cached
        result = await self._call("eth_getBlockByNumber", [hex(max(0, number)), False])
        if not isinstance(result, Mapping):
            raise ChainRpcError("chain_rpc_payload_invalid")
        stamp_ms = _as_int(result.get("timestamp")) * 1000
        if stamp_ms <= 0:
            raise ChainRpcError("chain_rpc_payload_invalid")
        self._block_timestamps_ms[number] = stamp_ms
        return stamp_ms

    async def token(self, address: str) -> ChainToken:
        """`symbol` and `decimals` for one ERC-20, cached for the life of the process.

        A contract that reverts or answers an unreadable word is not an error: it is a token with no
        readable metadata, and the fill still records its raw integer amount.
        """

        normalized = normalize_address(address)
        if not normalized:
            raise ValueError("chain_address_invalid")
        cached = self._tokens.get(normalized)
        if cached is not None:
            return cached
        symbol = _decode_string(await self._maybe_call(normalized, _SYMBOL_SELECTOR))
        decimals = _decode_uint8(await self._maybe_call(normalized, _DECIMALS_SELECTOR))
        resolved = ChainToken(address=normalized, symbol=symbol, decimals=decimals)
        self._tokens[normalized] = resolved
        return resolved

    async def balance_of(self, token: str, wallet: str, *, block_number: int) -> int | None:
        """`balanceOf(wallet)` on one ERC-20 at one historical block, or `None` when the node cannot say.

        This is the one call in this adapter that asks for *state*, and the public endpoint keeps about
        6,100 blocks of it -- roughly ten minutes (#572 §3.3). Beyond that window the node answers
        `-32000 metadata is not found`, which is a fact about the endpoint rather than a fault: it comes
        back as `None`, and the caller falls back to the provider's own reported bag and says so on the
        card. A revert is `None` for the same reason.

        The block is `latest`-relative only in the sense that the caller chose it: the sell rule asks at
        `block_number - 1`, so the answer is the balance the wallet held immediately before the trade.
        """

        holder = normalize_address(wallet)
        contract = normalize_address(token)
        if not holder or not contract:
            raise ValueError("chain_address_invalid")
        data = _BALANCE_OF_SELECTOR + holder[2:].rjust(64, "0")
        try:
            result = await self._call("eth_call", [{"to": contract, "data": data}, hex(max(0, int(block_number)))])
        except ChainRpcError as exc:
            if exc.rpc_code is None:
                raise
            # An RPC-level error here is the node declining to answer for this block -- pruned state or
            # an execution revert. Both are "we do not know", never "the balance was zero".
            return None
        if not isinstance(result, str):
            return None
        word = result.strip()
        if not word.startswith("0x") or len(word) < 3:
            return None
        try:
            return int(word[2:], 16)
        except ValueError:
            return None

    async def _maybe_call(self, address: str, selector: str) -> str | None:
        try:
            result = await self._call("eth_call", [{"to": address, "data": selector}, "latest"])
        except ChainRpcError as exc:
            if exc.rpc_code is None:
                raise
            # An execution revert is the contract's answer, not a provider failure.
            return None
        return result if isinstance(result, str) else None

    async def _call(self, method: str, params: list[Any]) -> Any:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            async with self._client.stream("POST", self.rpc_url, json=body) as response:
                if response.status_code in {401, 403, 451}:
                    raise ChainRpcError("chain_rpc_blocked", status_code=response.status_code)
                if response.status_code in {418, 429}:
                    raise ChainRpcError("chain_rpc_rate_limited", status_code=response.status_code)
                if response.status_code >= 400:
                    raise ChainRpcError("chain_rpc_http_error", status_code=response.status_code)
                # Streamed, so the ceiling stops the read rather than describing it afterwards.
                raw = await read_bounded(response, max_bytes=_MAX_BYTES)
        except httpx.TimeoutException:
            raise ChainRpcError("chain_rpc_timeout") from None
        except ResponseTooLarge:
            raise ChainRpcError("chain_rpc_payload_too_large") from None
        except httpx.HTTPError:
            raise ChainRpcError("chain_rpc_transport_error") from None
        self._last_bytes = len(raw)
        try:
            payload = json.loads(raw)
        except ValueError:
            raise ChainRpcError("chain_rpc_payload_invalid") from None
        if not isinstance(payload, Mapping):
            raise ChainRpcError("chain_rpc_payload_invalid")
        error = payload.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, Mapping) else None
            raise ChainRpcError("chain_rpc_error", rpc_code=None if code is None else int(code))
        return payload.get("result")


def _topic_filter(item: str | None | Sequence[str]) -> Any:
    if item is None:
        return None
    if isinstance(item, str):
        return item
    return list(item)


def _log(item: Any) -> ChainLog:
    if not isinstance(item, Mapping):
        raise ChainRpcError("chain_rpc_payload_invalid")
    topics = item.get("topics") or ()
    if not isinstance(topics, Sequence) or isinstance(topics, str | bytes):
        raise ChainRpcError("chain_rpc_payload_invalid")
    return ChainLog(
        address=normalize_address(str(item.get("address") or "")),
        topics=tuple(str(topic).lower() for topic in topics),
        data=str(item.get("data") or "0x"),
        block_number=_as_int(item.get("blockNumber")),
        block_hash=str(item.get("blockHash") or "").lower(),
        transaction_hash=str(item.get("transactionHash") or "").lower(),
        transaction_index=_as_int(item.get("transactionIndex")),
        log_index=_as_int(item.get("logIndex")),
        removed=bool(item.get("removed")),
    )


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ChainRpcError("chain_rpc_payload_invalid")
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        raise ChainRpcError("chain_rpc_payload_invalid") from None


def _decode_uint8(word: str | None) -> int | None:
    if not word:
        return None
    try:
        value = int(str(word), 16)
    except ValueError:
        return None
    return value if 0 <= value <= 255 else None


def _decode_string(word: str | None) -> str | None:
    """ABI-decode a `string` return, tolerating the `bytes32` shape some older tokens use."""

    if not word or not str(word).startswith("0x"):
        return None
    body = bytes.fromhex(_even(str(word)[2:]))
    if len(body) >= 64:
        offset = int.from_bytes(body[:32], "big")
        if 0 < offset <= len(body) - 32:
            length = int.from_bytes(body[offset : offset + 32], "big")
            if 0 < length <= len(body) - offset - 32:
                return _clean(body[offset + 32 : offset + 32 + length])
    return _clean(body.rstrip(b"\x00")) if body else None


def _even(text: str) -> str:
    return text if len(text) % 2 == 0 else f"0{text}"


def _clean(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    printable = "".join(character for character in text if character.isprintable())
    return printable[:32] or None


__all__ = [
    "CHAIN_RPC_USER_AGENT",
    "ROBINHOOD_CHAIN_ID",
    "ROBINHOOD_CHAIN_RPC_URL",
    "ChainLog",
    "ChainReceipt",
    "ChainRpcError",
    "ChainToken",
    "RobinhoodChainClient",
]
