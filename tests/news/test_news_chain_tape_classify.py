"""What a roster wallet did, read off recorded receipts (#572 PR-1).

Two of these receipts are the real thing, fetched from the public endpoint on 2026-09-06 and recorded
verbatim under `tests/fixtures/chain_tape/`. They are the only evidence that the rule matches the
provider's own numbers: the sell's stablecoin leg is 3,608.596725 and the site's dollar figure for that
fill is 3,608.596725; the buy's is 993.760928 and so is the site's. The rest of the table is built from
those shapes -- a plain move, an airdrop, a pool quoted in something other than the stablecoin, and a
route the wallet is only a middle hop of.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tracefold.news.chain_tape.classify import (
    TRANSFER_TOPIC,
    CashLeg,
    cash_leg,
    classify_receipt,
    has_swap,
    transfers_in,
    usd_face_value,
)
from tracefold.news.chain_tape.contracts import (
    STABLE_CASH_TOKEN,
    USD_SOURCE_STABLE_CASH_LEG,
)
from tracefold.news.chain_tape.evm import (
    address_topic,
    normalize_address,
    topic_address,
    transfer_amount,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "chain_tape"

SELL_WALLET = "0x69326e48f68500fb6cf3b3a7da640737b9cc347b"
BUY_WALLET = "0x80f3b0b712a82172a67e454e313ba6e2b0e7ae64"
FSD = "0x8de9018c1bb82884245f06dede9fe2bebabd1e18"
MADETEST = "0x5d191e73445cd5eb03cbaa56c263f1f9e9a4fcb3"
TSLA_TOKEN = "0x322f0929c4625ed5bad873c95208d54e1c003b2d"
EXECUTOR = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"

CHAIN_ID = 4663
NOW = 1_788_642_791_000


@dataclass(frozen=True, slots=True)
class _Log:
    address: str
    topics: tuple[str, ...]
    data: str
    log_index: int


@dataclass(frozen=True, slots=True)
class _Receipt:
    transaction_hash: str
    block_number: int
    block_hash: str
    transaction_index: int
    status: int
    logs: tuple[_Log, ...]


def _receipt(document: Any) -> _Receipt:
    return _Receipt(
        transaction_hash=str(document["transactionHash"]).lower(),
        block_number=int(str(document["blockNumber"]), 16),
        block_hash=str(document["blockHash"]).lower(),
        transaction_index=int(str(document["transactionIndex"]), 16),
        status=int(str(document["status"]), 16),
        logs=tuple(
            _Log(
                address=str(log["address"]).lower(),
                topics=tuple(str(topic).lower() for topic in log["topics"]),
                data=str(log["data"]),
                log_index=int(str(log["logIndex"]), 16),
            )
            for log in document["logs"]
        ),
    )


def _recorded(name: str) -> _Receipt:
    return _receipt(json.loads((FIXTURES / name).read_text(encoding="utf-8"))["result"])


def _synthetic(name: str) -> _Receipt:
    document = json.loads((FIXTURES / "synthetic_receipts.json").read_text(encoding="utf-8"))
    return _receipt(document[name]["result"])


def _classify(receipt: _Receipt, wallets: tuple[str, ...]) -> Any:
    return classify_receipt(
        receipt,
        roster_wallets=wallets,
        chain_id=CHAIN_ID,
        event_at_ms=NOW,
        received_at_ms=NOW + 500,
        classified_at_ms=NOW + 900,
        roster_version=7,
    )


def _decimals(fill: Any, decimals: int) -> Decimal:
    return Decimal(fill.amount_raw).scaleb(-decimals)


# --------------------------------------------------------------------------- the two measured trades
def test_the_recorded_sell_is_one_fill_priced_at_the_stablecoin_the_executor_collected() -> None:
    """F2P for the whole PR: a multi-hop sell is one row, and its dollar figure is the site's own.

    The route emits five `Swap` events and six FSD transfers. Only one of those transfers is the
    wallet's, so only one row exists -- and 3,608.596725 is what robinhoodtrenches publishes for this
    fill.
    """

    receipt = _recorded("receipt_sell_fsd.json")
    outcome = _classify(receipt, (SELL_WALLET,))

    assert len(outcome.fills) == 1
    fill = outcome.fills[0]
    assert (fill.kind, fill.wallet, fill.token, fill.log_index) == ("sell", SELL_WALLET, FSD, 6)
    assert fill.chain_id == CHAIN_ID
    assert fill.block_number == 55_432_994
    assert _decimals(fill, 18) == Decimal("9412641.983109561976191332")
    assert fill.cash_token == STABLE_CASH_TOKEN
    assert fill.cash_amount_raw == 3_608_596_725
    usd, source = usd_face_value(CashLeg(str(fill.cash_token), int(fill.cash_amount_raw or 0)), cash_decimals=6)
    assert (usd, source) == (Decimal("3608.596725"), USD_SOURCE_STABLE_CASH_LEG)
    assert (outcome.ignored_inbound, outcome.unknown) == (0, 0)


def test_the_recorded_buy_is_priced_by_the_stablecoin_that_entered_the_route() -> None:
    """The buy's money arrives before the token does, and the wallet is the last receiver of that token."""

    receipt = _recorded("receipt_buy_madetest.json")
    outcome = _classify(receipt, (BUY_WALLET,))

    assert len(outcome.fills) == 1
    fill = outcome.fills[0]
    assert (fill.kind, fill.wallet, fill.token, fill.log_index) == ("buy", BUY_WALLET, MADETEST, 38)
    assert _decimals(fill, 18) == Decimal("2647047.037924090220349607")
    assert fill.cash_token == STABLE_CASH_TOKEN
    assert fill.cash_amount_raw == 993_760_928
    usd, _source = usd_face_value(CashLeg(str(fill.cash_token), int(fill.cash_amount_raw or 0)), cash_decimals=6)
    assert usd == Decimal("993.760928")


def test_a_receipt_of_another_wallets_trade_produces_nothing() -> None:
    """The roster is the filter. The same receipt with a different roster is not a fill of theirs."""

    assert _classify(_recorded("receipt_sell_fsd.json"), (BUY_WALLET,)).fills == ()


# --------------------------------------------------------------------------- the rest of the table
@pytest.mark.parametrize(
    ("fixture", "wallets", "kinds", "ignored_inbound", "unknown"),
    [
        ("transfer_out_plain", (SELL_WALLET,), ("transfer_out",), 0, 0),
        ("airdrop_in", (SELL_WALLET,), (), 1, 0),
        ("sell_non_stable_cash_leg", (SELL_WALLET,), ("sell",), 0, 0),
        ("route_middle_hop", (SELL_WALLET,), ("transfer_out",), 0, 2),
        ("reverted", (SELL_WALLET,), (), 0, 0),
    ],
)
def test_the_classification_table(
    fixture: str,
    wallets: tuple[str, ...],
    kinds: tuple[str, ...],
    ignored_inbound: int,
    unknown: int,
) -> None:
    outcome = _classify(_synthetic(fixture), wallets)

    assert tuple(fill.kind for fill in outcome.fills) == kinds
    assert outcome.ignored_inbound == ignored_inbound
    assert outcome.unknown == unknown


def test_an_airdrop_is_counted_and_never_stored() -> None:
    """#570's capacity note, as a rule: a token somebody pushed at a wallet is noise with a number."""

    outcome = _classify(_synthetic("airdrop_in"), (SELL_WALLET,))

    assert outcome.fills == ()
    assert outcome.ignored_inbound == 1


def test_a_pool_quoted_in_something_other_than_the_stablecoin_leaves_usd_null() -> None:
    """`unpriced` is not zero: the quantity and its token are recorded and PR-3 may price them."""

    outcome = _classify(_synthetic("sell_non_stable_cash_leg"), (SELL_WALLET,))
    fill = outcome.fills[0]

    assert fill.cash_token == TSLA_TOKEN
    assert fill.cash_amount_raw == 4_328_537_395_523_139_000
    assert usd_face_value(CashLeg(TSLA_TOKEN, int(fill.cash_amount_raw or 0)), cash_decimals=18) == (None, None)


def test_a_reverted_transaction_moves_nothing_and_counts_as_nothing() -> None:
    outcome = _classify(_synthetic("reverted"), (SELL_WALLET,))

    assert (outcome.fills, outcome.ignored_inbound, outcome.unknown) == ((), 0, 0)


def test_a_stable_leg_with_no_readable_scale_is_not_a_dollar_figure() -> None:
    assert usd_face_value(CashLeg(STABLE_CASH_TOKEN, 1_000_000), cash_decimals=None) == (None, None)
    assert usd_face_value(None, cash_decimals=6) == (None, None)


# --------------------------------------------------------------------------- the parts
def test_the_cash_leg_is_what_reached_the_wallets_counterparty() -> None:
    """Both measured trades settle the same way, which is why one rule reads both."""

    sell = transfers_in(_recorded("receipt_sell_fsd.json"))
    assert cash_leg(sell, traded_token=FSD, counterparty=EXECUTOR) == CashLeg(STABLE_CASH_TOKEN, 3_608_596_725)

    buy = transfers_in(_recorded("receipt_buy_madetest.json"))
    assert cash_leg(buy, traded_token=MADETEST, counterparty=EXECUTOR) == CashLeg(STABLE_CASH_TOKEN, 993_760_928)


def test_a_counterparty_that_collected_nothing_has_no_cash_leg() -> None:
    sell = transfers_in(_recorded("receipt_sell_fsd.json"))

    assert cash_leg(sell, traded_token=FSD, counterparty=BUY_WALLET) is None


def test_both_recorded_receipts_carry_a_swap_and_the_plain_transfer_does_not() -> None:
    assert has_swap(_recorded("receipt_sell_fsd.json")) is True
    assert has_swap(_recorded("receipt_buy_madetest.json")) is True
    assert has_swap(_synthetic("transfer_out_plain")) is False


def test_the_recorded_sell_holds_the_six_fsd_transfers_the_route_emitted() -> None:
    """The multi-hop shape, stated: six legs of the traded token, one of which is the wallet's."""

    transfers = transfers_in(_recorded("receipt_sell_fsd.json"))
    fsd = [transfer for transfer in transfers if transfer.token == FSD]

    assert len(fsd) == 6
    assert sum(1 for transfer in fsd if transfer.sender == SELL_WALLET) == 1


def test_address_and_amount_decoding_round_trips_and_refuses_a_malformed_word() -> None:
    assert normalize_address("0x69326E48F68500FB6CF3B3A7DA640737B9CC347B") == SELL_WALLET
    assert normalize_address("69326e48") == ""
    assert topic_address(address_topic(SELL_WALLET)) == SELL_WALLET
    assert topic_address("0xdead") == ""
    assert transfer_amount("0x" + "0" * 63 + "a") == 10
    assert transfer_amount("0x" + "f" * 65) is None
    assert transfer_amount("not-hex") is None


def test_the_transfer_topic_is_the_erc20_signature_the_chain_indexes() -> None:
    assert TRANSFER_TOPIC == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
