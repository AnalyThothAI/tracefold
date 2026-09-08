"""Read roster-wallet movements through token paths in one receipt (#572, #614).

A traded token must connect its terminal sender or recipient to an address emitting a recognized
Swap. An unrelated gift cannot borrow another token's swap. Each terminal recipient is considered,
including recipients outside the roster when checking cash allocation: several assets or recipients
sharing the same funding remain visible trades with unknown cash, never several full-dollar copies.

The supported routed settlement reads cash entering the wallet's counterparty. Direct buys also fit
that shape; direct sells whose proceeds return to the wallet remain `transfer_out`. The pinned USDG
leg is money, never a position of its own. Without a swap, inbound movements are counted and omitted,
and outbound movements remain `transfer_out`. A token path is evidence of a route, not validation of a
pool's authenticity or a complete account of arbitrary batch-executor economics.

Two measured transactions anchor this, and both are recorded as fixtures:

* sell `0x5c10c3cf…9ac5f0`, block 55432994: wallet `0x69326e48…cc347b` sends 9,412,641.983109562 FSD at
  log index 6, the route emits five `Swap` events (two V3, three V4) and six FSD transfers, and one
  stablecoin transfer of 3,608.596725 reaches the executor `0xb92fe925…4fff4f`. The site reports that
  fill's dollar value as 3,608.596725.
* buy `0x42f41c07…23742b`: 993.760928 stablecoin enters the executor at log index 1, the route emits
  seven `Swap` events (one V3, six V4), and 2,647,047.037924 MADETEST reaches wallet `0x80f3b0b7…7ae64`
  at log index 38 -- the last transfer of that token. The site reports 993.760928.

One roster wallet `Transfer` is one fill. A five-hop route is one row, not five, because the wallet
moved once; the hops are the route's business.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .contracts import (
    STABLE_CASH_TOKEN,
    SWAP_TOPICS,
    USD_SOURCE_STABLE_CASH_LEG,
    ClassifiedFill,
    FillKind,
)
from .evm import TRANSFER_TOPIC, normalize_address, topic_address, transfer_amount


class ReceiptLike(Protocol):
    """One receipt. The classifier never calls the network, so this is all it needs."""

    @property
    def transaction_hash(self) -> str: ...

    @property
    def block_number(self) -> int: ...

    @property
    def block_hash(self) -> str: ...

    @property
    def status(self) -> int: ...

    @property
    def logs(self) -> tuple[Any, ...]: ...


@dataclass(frozen=True, slots=True)
class TokenTransfer:
    """One ERC-20 `Transfer` in a receipt, decoded."""

    token: str
    sender: str
    recipient: str
    amount_raw: int
    log_index: int


@dataclass(frozen=True, slots=True)
class CashLeg:
    """The money side of a routed trade: which token settled it and how much of it moved."""

    token: str
    amount_raw: int


@dataclass(frozen=True, slots=True)
class ReceiptClassification:
    """What one receipt produced: the fills to store, and the two things that are only counted."""

    fills: tuple[ClassifiedFill, ...]
    ignored_inbound: int = 0
    unknown: int = 0


def transfers_in(receipt: ReceiptLike) -> tuple[TokenTransfer, ...]:
    """Every readable ERC-20 `Transfer` in the receipt, in log order."""

    out: list[TokenTransfer] = []
    for log in receipt.logs:
        topics = tuple(log.topics)
        if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
            continue
        token = normalize_address(log.address)
        sender = topic_address(topics[1])
        recipient = topic_address(topics[2])
        amount = transfer_amount(log.data)
        if not token or not sender or not recipient or amount is None:
            continue
        out.append(
            TokenTransfer(
                token=token,
                sender=sender,
                recipient=recipient,
                amount_raw=amount,
                log_index=int(log.log_index),
            )
        )
    return tuple(out)


def has_swap(receipt: ReceiptLike) -> bool:
    """Whether any pool in this receipt reported a swap. Presence, never amounts."""

    return any(topics[0] in SWAP_TOPICS for log in receipt.logs if (topics := tuple(log.topics)))


def cash_leg(
    transfers: Sequence[TokenTransfer],
    *,
    traded_token: str,
    counterparty: str,
) -> CashLeg | None:
    """The money the wallet's counterparty collected, in the token it collected it in.

    "Counterparty" is the address on the other side of the wallet's own leg -- the executor a Robinhood
    Chain trade is routed through. Both directions settle the same way: on a sell the executor receives
    the proceeds, on a buy it receives the funding, and in the two measured transactions that transfer
    is exactly the dollar figure the provider's own site publishes for the fill.

    When several tokens reached the counterparty, the pinned stablecoin wins; otherwise the largest
    aggregate does, and its token is recorded so a reader can see the trade was not settled in cash.
    """

    totals: dict[str, int] = {}
    for transfer in transfers:
        if transfer.token == traded_token or transfer.recipient != counterparty:
            continue
        totals[transfer.token] = totals.get(transfer.token, 0) + transfer.amount_raw
    if not totals:
        return None
    if STABLE_CASH_TOKEN in totals:
        return CashLeg(token=STABLE_CASH_TOKEN, amount_raw=totals[STABLE_CASH_TOKEN])
    token = max(totals, key=lambda key: (totals[key], key))
    return CashLeg(token=token, amount_raw=totals[token])


def usd_face_value(cash: CashLeg | None, *, cash_decimals: int | None) -> tuple[Decimal | None, str | None]:
    """A dollar figure only when the cash leg is the pinned stablecoin and its scale is known.

    Any other quote token -- pools here are quoted in tokenised equities as well -- leaves `usd` NULL.
    That is `unpriced`, not zero, and PR-3 may price it from a pair feed.
    """

    if cash is None or cash.token != STABLE_CASH_TOKEN or cash_decimals is None or cash_decimals < 0:
        return None, None
    return Decimal(cash.amount_raw).scaleb(-int(cash_decimals)), USD_SOURCE_STABLE_CASH_LEG


def classify_receipt(
    receipt: ReceiptLike,
    *,
    roster_wallets: Collection[str],
    chain_id: int,
    event_at_ms: int,
    received_at_ms: int,
    classified_at_ms: int,
    roster_version: int,
) -> ReceiptClassification:
    """Every roster-wallet movement in this receipt, read as a fill, an ignored inbound, or an unknown.

    A reverted transaction moved nothing: it produces no fills and is not counted as noise either.
    """

    if int(receipt.status) != 1:
        return ReceiptClassification(fills=())
    wallets = {normalize_address(wallet) for wallet in roster_wallets}
    wallets.discard("")
    transfers = transfers_in(receipt)
    if not transfers or not wallets:
        return ReceiptClassification(fills=())
    pools = {
        normalize_address(log.address)
        for log in receipt.logs
        if (topics := tuple(log.topics)) and topics[0] in SWAP_TOPICS
    }
    swap = bool(pools)
    legs = _trade_legs(transfers, pools=pools)

    fills: list[ClassifiedFill] = []
    ignored_inbound = 0
    unknown = 0
    for transfer in transfers:
        outbound = transfer.sender in wallets
        inbound = transfer.recipient in wallets
        if not outbound and not inbound:
            continue
        # A log has one identity, so a roster-to-roster movement is recorded once. The sending side is
        # the one kept: an outbound movement is the fact the exit rules in PR-2 are built on, and the
        # receiving side is visible in the same row's counterparty.
        wallet = transfer.sender if outbound else transfer.recipient
        if swap and transfer.token == STABLE_CASH_TOKEN:
            # The money side of somebody's trade, not a position of its own (see the module docstring).
            continue
        key = (transfer.log_index, outbound)
        cash = legs.get(key)
        kind: FillKind | None = (
            ("sell" if outbound else "buy") if key in legs else ("transfer_out" if outbound else None)
        )
        if kind is None:
            if swap:
                unknown += 1
            else:
                ignored_inbound += 1
            continue
        if kind == "transfer_out" and swap:
            unknown += 1
        fills.append(
            ClassifiedFill(
                chain_id=int(chain_id),
                tx_hash=str(receipt.transaction_hash).lower(),
                log_index=transfer.log_index,
                block_number=int(receipt.block_number),
                block_hash=str(receipt.block_hash).lower(),
                wallet=wallet,
                token=transfer.token,
                kind=kind,
                amount_raw=transfer.amount_raw,
                event_at_ms=int(event_at_ms),
                received_at_ms=int(received_at_ms),
                classified_at_ms=int(classified_at_ms),
                roster_version=int(roster_version),
                cash_token=None if cash is None else cash.token,
                cash_amount_raw=None if cash is None else cash.amount_raw,
            )
        )
    return ReceiptClassification(fills=tuple(fills), ignored_inbound=ignored_inbound, unknown=unknown)


def _trade_legs(transfers: Sequence[TokenTransfer], *, pools: set[str]) -> dict[tuple[int, bool], CashLeg | None]:
    """Terminal token movements connected to a swap, with cash only where its allocation is unique.

    Inspect every recipient, including wallets outside the roster: a tracked recipient of a shared
    purchase did not pay the whole route's bill. A gift has no token path from a swap emitter and cannot
    borrow an unrelated swap in the same receipt. This is a bounded receipt interpretation, not a pool
    authenticity check or an allocation guess for an arbitrary batch executor.
    """

    if not pools:
        return {}
    sent = {(item.token, item.sender) for item in transfers}
    received = {(item.token, item.recipient) for item in transfers}
    legs: dict[tuple[int, bool], tuple[str, CashLeg | None]] = {}
    for item in transfers:
        if item.token == STABLE_CASH_TOKEN or item.amount_raw <= 0:
            continue
        for outbound in (True, False):
            wallet = item.sender if outbound else item.recipient
            other_movements = received if outbound else sent
            if wallet in pools or (item.token, wallet) in other_movements:
                continue
            counterparty = item.recipient if outbound else item.sender
            if not _reaches_pool(transfers, token=item.token, start=counterparty, outbound=outbound, pools=pools):
                continue
            cash = cash_leg(transfers, traded_token=item.token, counterparty=counterparty)
            # A direct sale returning its cash to the wallet is still an outbound movement: the
            # executor settlement rule has no dollar allocation for it. Preserve that existing limit.
            if outbound and cash is None:
                continue
            legs[(item.log_index, outbound)] = (counterparty, cash)
    claims = Counter((counterparty, cash.token) for counterparty, cash in legs.values() if cash is not None)
    return {
        key: cash if cash is not None and claims[(counterparty, cash.token)] == 1 else None
        for key, (counterparty, cash) in legs.items()
    }


def _reaches_pool(
    transfers: Sequence[TokenTransfer], *, token: str, start: str, outbound: bool, pools: set[str]
) -> bool:
    """Follow this token toward a swap for a sell, or back toward its source swap for a buy."""

    pending = [start]
    seen: set[str] = set()
    while pending:
        address = pending.pop()
        if address in pools:
            return True
        if address in seen:
            continue
        seen.add(address)
        for item in transfers:
            if item.token != token:
                continue
            origin, destination = (item.sender, item.recipient) if outbound else (item.recipient, item.sender)
            if origin == address and destination not in seen:
                pending.append(destination)
    return False


__all__ = [
    "CashLeg",
    "ReceiptClassification",
    "ReceiptLike",
    "TokenTransfer",
    "cash_leg",
    "classify_receipt",
    "has_swap",
    "transfers_in",
    "usd_face_value",
]
