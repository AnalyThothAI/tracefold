"""The Strategy 2026 parser, against the titles production actually sent (#553).

The corpus in `tests/fixtures/opennews_smart_money_2026.json` is every distinct smart-money title in
the retained production window, with the numbers each one states written out beside it. It exists
because the module refused a shape it had decided was hypothetical: a comment claimed the provider's
`K`/`M`/`B` suffix "has never been measured here", and the provider had been writing `$798.18K` and
`$2.21M` all along. Eight of the 113 titles parsed. The rest went out one raw card per record.

A fixture of real provider titles is what keeps that from being re-decided from memory. The expected
values are not what the regex produces -- they were derived from the titles by a separate
token-splitting pass -- so this file can disagree with the parser, which is the only way it can hold
it to anything.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from tracefold.news.liquidations import parse_liquidation
from tracefold.news.smart_money import (
    RAW_REASON_TEMPLATE_UNMATCHED,
    parse_smart_money,
)

CORPUS: Final[dict[str, Any]] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "opennews_smart_money_2026.json").read_text()
)
TITLES: Final[list[dict[str, Any]]] = CORPUS["titles"]

# The first live Strategy 2026 report after the #553 deploy, at 16:28 UTC on 2026-09-05. It was stored
# `market_parse_status=raw` with `smart_money_template_unmatched` and sent as its own raw card instead
# of joining the account's §4.4 group.
PRODUCTION_DEFECT_TITLE: Final = "js-2 Open Long BTC $798.18K , Price $79,817.87"


def _parse(title: str, *, venue: str = "hyperliquid", address: str | None = None, event_at_ms: int = 1_000):
    return parse_smart_money(
        title,
        item_id="a" * 64,
        fact_id="whole",
        source_strategy_id="2026",
        provider_source=venue,
        related_address=address,
        event_at_ms=event_at_ms,
        received_at_ms=2_000,
    )


def test_the_production_report_that_was_stored_raw_reads_as_the_numbers_it_states() -> None:
    """The exact defect. `$798.18K` is 798 180 dollars, and it is not an unproven template."""

    fact = _parse(PRODUCTION_DEFECT_TITLE)
    assert fact is not None
    assert (fact.action, fact.position_side) == ("open", "long")
    assert (fact.raw_instrument, fact.symbol) == ("BTC", "BTC")
    assert fact.reported_notional_usd == Decimal("798180")
    assert fact.price == Decimal("79817.87")
    assert fact.pnl_usd is None
    assert fact.trader_label == "js-2"


@pytest.mark.parametrize("record", TITLES, ids=[record["title"] for record in TITLES])
def test_every_exported_production_title_parses_as_the_corpus_states_it(record: dict[str, Any]) -> None:
    """One case per real title: the numbers it states, or a refusal that names its reason."""

    fact = _parse(record["title"])
    expected = record["expected"]
    if expected is None:
        assert fact is None, "a template this module has not been shown must stay raw"
        assert RAW_REASON_TEMPLATE_UNMATCHED == "smart_money_template_unmatched"
        return
    assert fact is not None
    assert fact.trader_label == expected["trader_label"]
    assert fact.action == expected["action"]
    assert fact.position_side == expected["position_side"]
    assert fact.raw_instrument == expected["raw_instrument"]
    assert fact.symbol == expected["symbol"]
    assert fact.reported_notional_usd == Decimal(expected["reported_notional_usd"])
    assert fact.price == Decimal(expected["price"])
    assert fact.pnl_usd == (None if expected["pnl_usd"] is None else Decimal(expected["pnl_usd"]))


def test_the_whole_corpus_parses_except_the_one_report_that_is_not_a_position_report() -> None:
    """The measurement the fix is worth: 8 of 113 before, 112 of 113 after.

    The one that stays raw is `Withdraw USDC`, which states no side, no price and no position -- there
    is nothing to turn into a typed row, and a raw card is the honest answer to it.
    """

    parsed = [record["title"] for record in TITLES if _parse(record["title"]) is not None]
    refused = [record["title"] for record in TITLES if _parse(record["title"]) is None]
    assert len(TITLES) == 113
    assert len(parsed) == 112
    assert refused == ["js-2 Withdraw USDC $160K"]


def test_the_corpus_still_covers_the_shapes_this_parser_claims_to_read() -> None:
    """A guard on the fixture, not on the parser.

    Every assertion above passes just as well against a corpus someone trimmed to the eight plain
    figures that used to parse. These counts are what makes the file evidence about the provider.
    """

    titles = [record["title"] for record in TITLES]

    def count(pattern: str) -> int:
        return sum(1 for title in titles if re.search(pattern, title))

    assert count(r"\$[\d.]+K ") >= 100  # the suffix that used to be called unmeasured
    assert count(r"\$[\d.]+M ") >= 1
    assert count(r"Price \$\d{1,3},\d{3}") == 2  # a price the provider spells in full
    assert count(r"PNL \+\$[\d.]+K") >= 1 and count(r"PNL -\$[\d.]+K") >= 1
    assert count(r"\$\d+(?:\.\d+)? , Price") >= 8  # the plain figures that parsed before the fix
    assert [title for title in titles if not re.search(r"\b(?:Open|Close)\b", title)] == ["js-2 Withdraw USDC $160K"]


@pytest.mark.parametrize(
    "title",
    [
        # Not a position report at all -- the measured example.
        "js-2 Withdraw USDC $160K",
        # An abbreviated price. The provider spells prices in full, so this is a drifted template and
        # not an invitation to pick a multiplier for it.
        "js-2 Open Long BTC $798.18K , Price $79.81K",
        # A suffix outside the provider's own vocabulary.
        "js-2 Open Long BTC $798.18T , Price $79,817.87",
        # Structure the template does not have: no price, no dollar mark, an unknown action, no figures.
        "js-2 Open Long BTC $798.18K",
        "js-2 Open Long BTC 798.18K , Price $79,817.87",
        "js-2 Adjust Long BTC $798.18K , Price $79,817.87",
        "js-2 Open Sideways BTC $798.18K , Price $79,817.87",
        "js-2 Open Long BTC $ , Price $79,817.87",
        "",
    ],
)
def test_an_unproven_template_is_still_refused(title: str) -> None:
    assert _parse(title) is None


def test_thousands_separators_are_read_in_every_figure() -> None:
    fact = _parse("js-2 Close Short BTC $1,234,567.89 , Price $79,817.87 , PNL -$8,204.10")
    assert fact is not None
    assert fact.reported_notional_usd == Decimal("1234567.89")
    assert fact.price == Decimal("79817.87")
    assert fact.pnl_usd == Decimal("-8204.10")


@pytest.mark.parametrize(
    ("suffix", "multiplier"),
    [("K", Decimal(1_000)), ("M", Decimal(1_000_000)), ("B", Decimal(1_000_000_000))],
)
def test_an_abbreviated_figure_means_the_same_number_on_both_provider_templates(
    suffix: str, multiplier: Decimal
) -> None:
    """One multiplier, read from `liquidations.py` by both parsers.

    Two copies of it are two chances for the same letter to start meaning two numbers, and a reader
    cannot see which of the two a card used.
    """

    account = _parse(f"js-2 Open Long BTC $2.21{suffix} , Price $79,817.87")
    forced = parse_liquidation(
        f"BTC Large Long Liquidation 2.21{suffix} at $79817.87",
        item_id="b" * 64,
        fact_id="whole",
        source_strategy_id="2083",
        provider_source="binance",
        event_at_ms=1_000,
        received_at_ms=2_000,
    )
    assert account is not None and forced is not None
    assert account.reported_notional_usd == forced.notional_usd == Decimal("2.21") * multiplier


def test_a_lowercase_suffix_is_the_same_figure() -> None:
    """The provider's case is not a unit. Both parsers already read the template case-insensitively."""

    fact = _parse("js-2 Open Long BTC $798.18k , Price $79,817.87")
    assert fact is not None and fact.reported_notional_usd == Decimal("798180")


def test_a_missing_event_stamp_is_still_refused() -> None:
    """#544 left the two clocks uncompared; a stamp that is absent is still a stamp that is absent."""

    assert _parse(PRODUCTION_DEFECT_TITLE, event_at_ms=0) is None
    assert _parse(PRODUCTION_DEFECT_TITLE, event_at_ms=-1) is None


def test_the_reported_address_and_venue_are_recorded_as_the_provider_sent_them() -> None:
    fact = _parse(PRODUCTION_DEFECT_TITLE, venue="Hyperliquid", address="0xabc")
    assert fact is not None
    assert (fact.source_venue, fact.account_address) == ("hyperliquid", "0xabc")
    unattributed = _parse(PRODUCTION_DEFECT_TITLE, venue="", address="")
    assert unattributed is not None
    assert (unattributed.source_venue, unattributed.account_address) == (None, None)
