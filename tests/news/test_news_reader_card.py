"""The reader card is one value object with one formatter, and it still sends the cards it sent (#562 PR-A).

Two claims, both about characters rather than structure:

* **Nothing a reader sees moved, except where this branch says it does.** Every card in
  `reader_card_production_cards.json` is a card production actually sent, with the inputs that
  produced it. The News first card and all four market families are rebuilt through `ReaderCard` and
  the Feishu serializer and compared as JSON values -- key order is PostgreSQL's, since the frozen
  snapshot is `jsonb`, so a canonical dump is the byte comparison that means anything here. #562
  PR-G is the first change to move a sent card's characters, and `DELIBERATE_CHANGES` is the whole
  of what it moved -- there and in the branch corpus, which is the other recorded one.
* **What #562 PR-B added is written down rather than regenerated.** `reader_card_quoted_cards.json`
  holds the market card surfaces production has never sent -- the quote line, the report's own price
  and PNL, the OI whale columns -- rendered by this repository and reviewed line by line. The two
  corpora above are untouched by that change: every card in them is rendered with no quote, and the
  reported-price and whale fields none of their observations carry.
* **The card and the console write a number the same way.** `card_money_format.json` is one table read
  by this module and by `web/tests/unit/features/news/newsPriceAlignment.test.ts`. Editing the rule on
  one surface without the other fails both.

The fixture separates two things that are easy to blur. `card` is what this repository renders for
those inputs and is asserted for all 48. `sent_card` is the JSON the provider received; it matches
`card` for the 16 cards sent by today's code, and differs for 32 older market cards in exactly two
already-shipped ways -- the #553 header-separator fix, and the same change's rule that a relative
`/news/market/<id>` is not a link a Feishu client can follow, so no button is offered. Their markdown
body, which is everything the reader reads, is asserted identical to what was sent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import pytest

from tracefold.news import card_format as fmt
from tracefold.news.delivery import render_first_card
from tracefold.news.market_notifications import MarketObservation, MarketTrack, render_market_card
from tracefold.news.reader_card import ReaderCardQuote

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "news"
PRODUCTION_CARDS: Final[list[dict[str, Any]]] = json.loads(
    (FIXTURES / "reader_card_production_cards.json").read_text(encoding="utf-8")
)
BRANCH_CARDS: Final[dict[str, Any]] = json.loads(
    (FIXTURES / "reader_card_branch_cards.json").read_text(encoding="utf-8")
)
QUOTED_CARDS: Final[dict[str, Any]] = json.loads(
    (FIXTURES / "reader_card_quoted_cards.json").read_text(encoding="utf-8")
)
MONEY_FORMAT: Final[dict[str, Any]] = json.loads((FIXTURES / "card_money_format.json").read_text(encoding="utf-8"))


# Every line #562 PR-G changes about a card already written down, by the entry that carries it. The
# two corpora keyed here are records -- `sent_card` is what a provider received, and every branch
# `card` is what the renderers on 7b9628ca0 wrote -- so neither is edited; what this branch renders
# differently is named here instead, one whole line at a time, and any other difference still fails.
# An empty replacement is a line this branch no longer prints.
#
# Three reasons, and no fourth: the money rule (a market card's dollar figures are the quote line's
# formatter, so a reader is not asked to read `开多 $200840` three lines above `行情 ARB $0.1938`),
# the Close caveat (printed by a card that printed a Close), and the largest reported amount (chosen
# as a number: the three-report liquidation also reported `1000000`, and `max` over the text
# answered `980000` because `"9" > "1"`).
DELIBERATE_CHANGES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "26f40a0bf365ddeb483e7b5c34dd7aef": (("最大单笔来源报告金额 $1000000", "最大单笔来源报告金额 $1,000,000.00"),),
    "21ba1086b3635b00637114f76d8d79ce": (("最大单笔来源报告金额 $743120", "最大单笔来源报告金额 $743,120.00"),),
    "market-smart-money-action-change-six-reports": (
        (
            "开多 $160180 · 开多 · 平多 $7500.25 · 开空 $1000000",
            "开多 $160,180.00 · 开多 · 平多 $7,500.25 · 开空 $1,000,000.00",
        ),
    ),
    "market-smart-money-verified-address": (
        ("开空 $2500000 · 平空 $1250000", "开空 $2,500,000.00 · 平空 $1,250,000.00"),
    ),
    "market-smart-money-unlabelled-account": (("Close 只表示来源报告的平仓/减仓动作，不代表账户已全部清仓。", ""),),
    "market-liquidation-three-reports": (("最大单笔来源报告金额 $980000", "最大单笔来源报告金额 $1,000,000.00"),),
}
# Every dollar amount a card prints, compact `$1.20B` included, so a figure that escaped the money
# rule is found rather than skipped by a pattern that only knows the shape it was supposed to have.
DOLLAR_FIGURE: Final = re.compile(r"-?\$[\d,]+(?:\.\d+)?[KMB]?")
# The one line that is compact on purpose: an open-interest total is a magnitude (see
# `card_format.usd_compact`).
COMPACT_LINE_PREFIX: Final = "OI $"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# Every market card whose figures this repository writes. The `raw` family is excluded because it
# writes none: its body is the provider's own sentence, `js-2 Open Long BTC $798.18K` and all, quoted
# rather than formatted -- rewriting a number inside quoted text would change what the source said.
FORMATTED_MARKET_CARDS: Final[list[dict[str, Any]]] = [
    entry
    for corpus in (PRODUCTION_CARDS, BRANCH_CARDS["entries"], QUOTED_CARDS["entries"])
    for entry in corpus
    if entry["source"] == "market" and entry["inputs"]["track"]["family"] != "raw"
]


def _as_this_branch_renders(entry: dict[str, Any], recorded: dict[str, Any]) -> dict[str, Any]:
    """A recorded card with this branch's named line changes applied to it, and nothing else.

    Whole lines, matched exactly: a pair whose line is not in the recorded body raises rather than
    silently excusing a difference somewhere else, and a card with no entry in the table is returned
    as it was recorded.
    """

    changes = DELIBERATE_CHANGES.get(entry["id"], ())
    if not changes:
        return recorded
    card = json.loads(json.dumps(recorded))
    for element in card["elements"]:
        if element["tag"] != "markdown":
            continue
        lines = element["content"].split("\n")
        for before, after in changes:
            index = lines.index(before)
            lines[index : index + 1] = [after] if after else []
        element["content"] = "\n".join(lines)
    return card


def _render(entry: dict[str, Any]) -> dict[str, Any]:
    inputs = entry["inputs"]
    if entry["source"] == "news":
        return render_first_card(**inputs)
    return render_market_card(
        track=MarketTrack(**inputs["track"]),
        reason=inputs["reason"],
        observations=[MarketObservation(**row) for row in inputs["observations"]],
        detail_url=inputs["detail_url"],
        action_changes=inputs["action_changes"],
        quotes=[ReaderCardQuote(**quote) for quote in inputs.get("quotes", ())],
    )


def _body(card: dict[str, Any]) -> str:
    return next(element["content"] for element in card["elements"] if element["tag"] == "markdown")


@pytest.mark.parametrize("entry", PRODUCTION_CARDS, ids=lambda entry: f"{entry['source']}-{entry['id'][:12]}")
def test_a_production_card_is_rebuilt_character_for_character(entry: dict[str, Any]) -> None:
    """The whole card: header, body, button and note, from the same facts through the new model."""

    assert _canonical(_render(entry)) == _canonical(entry["card"])


@pytest.mark.parametrize("entry", PRODUCTION_CARDS, ids=lambda entry: f"{entry['source']}-{entry['id'][:12]}")
def test_the_body_a_reader_read_is_the_body_this_repository_writes(entry: dict[str, Any]) -> None:
    """Every line of every sent card, including the 32 that predate the #553 header and link fix.

    `sent_card` is a record of what the provider received and is never edited; the money rule this
    branch applies to it is stated in `DELIBERATE_CHANGES` instead, so a body that moved for any
    other reason still fails here.
    """

    assert _body(_render(entry)) == _body(_as_this_branch_renders(entry, entry["sent_card"]))


def test_the_corpus_covers_both_renderers_and_says_which_cards_are_whole_matches() -> None:
    """Not vacuous: the fixture is real traffic, and its two claims are counted rather than assumed."""

    families = {(entry["source"], entry.get("kind"), entry.get("reason")) for entry in PRODUCTION_CARDS}
    assert families == {
        ("news", None, None),
        ("market", "oi", "first"),
        ("market", "liquidation", "first"),
        ("market", "smart_money", "raw"),
    }
    whole = [entry for entry in PRODUCTION_CARDS if entry["reproduces_sent_card"]]
    assert len(PRODUCTION_CARDS) == 48
    assert len(whole) == 16
    assert {entry["source"] for entry in whole} == {"news", "market"}
    for entry in whole:
        assert _canonical(entry["card"]) == _canonical(_as_this_branch_renders(entry, entry["sent_card"]))


@pytest.mark.parametrize(
    "entry",
    [entry for entry in PRODUCTION_CARDS if not entry["reproduces_sent_card"]],
    ids=lambda entry: entry["id"][:12],
)
def test_a_card_sent_before_the_553_fix_differs_only_where_that_fix_changed_it(entry: dict[str, Any]) -> None:
    """The 32 older market cards, held to the two named differences and byte-equal everywhere else.

    #553 changed exactly two things about a market card. The header gained its separator: the old join
    wrote `持仓异动 FLOCK` and `市场原文· 原文 —`, where a missing space, the qualifier's own separator
    and a `—` standing in for an instrument that was never going to be named were three pieces of
    punctuation doing a word's job. And a relative `/news/market/<id>` stopped being offered as a
    button, because no Feishu or Telegram client can follow one — the note line carries the item id
    instead, which is what an operator needs to reach the same page.

    Asserting "the template matches and the tags differ" would have passed for a card that had lost a
    line. This states each difference and requires everything else to be identical.
    """

    rebuilt, sent = _render(entry), entry["sent_card"]
    assert entry["source"] == "market"

    rebuilt_title = rebuilt["header"]["title"]["content"]
    sent_title = sent["header"]["title"]["content"]
    if sent_title.endswith(" —"):
        # A raw report names no instrument. The old header printed the placeholder and swallowed the
        # space before the qualifier's separator; both are gone, and nothing else about it moved.
        assert sent_title == f"{rebuilt_title.replace(' · ', '· ', 1)} —"
    else:
        assert rebuilt_title.replace(" · ", " ") == sent_title
    assert rebuilt["header"]["template"] == sent["header"]["template"]
    assert rebuilt["config"] == sent["config"]

    sent_elements = {element["tag"]: element for element in sent["elements"]}
    rebuilt_elements = {element["tag"]: element for element in rebuilt["elements"]}
    assert set(sent_elements) == {"markdown", "action", "note"}
    assert set(rebuilt_elements) == {"markdown", "note"}
    expected = {e["tag"]: e for e in _as_this_branch_renders(entry, sent)["elements"]}["markdown"]
    assert rebuilt_elements["markdown"] == expected

    # The button that is gone was relative, which is the whole reason it is gone.
    sent_url = sent_elements["action"]["actions"][0]["url"]
    assert urlsplit(sent_url).scheme == "" and sent_url.startswith("/news/market/")
    item_id = sent_url.rsplit("/", 1)[-1]

    # A card with no button says the item id instead; the rest of the note is unchanged.
    sent_note = sent_elements["note"]["elements"][0]["content"]
    rebuilt_note = rebuilt_elements["note"]["elements"][0]["content"]
    assert rebuilt_note == f"{sent_note} · {item_id}"


@pytest.mark.parametrize("entry", BRANCH_CARDS["entries"], ids=lambda entry: entry["id"])
def test_a_card_branch_production_did_not_exercise_renders_as_it_did_before(entry: dict[str, Any]) -> None:
    """The same claim as the production corpus, for the branches that corpus never reached.

    Every sent market card is a single-observation `first` or `raw`, so the smart-money account line,
    its action timeline, the last-four bound on that line, a spanned clock range, the escalation
    qualifier and both degraded News shapes had no byte coverage at all — and #562 PR-C edits exactly
    those. Each expected card here was written by the renderers on `main` at the commit this branch is
    built on, before the reader card existed; see the fixture's own note.
    """

    assert _canonical(_render(entry)) == _canonical(_as_this_branch_renders(entry, entry["card"]))


def test_the_branch_corpus_names_the_base_it_was_generated_from_and_covers_what_it_claims() -> None:
    """Not vacuous: the fixture is a record of the old renderers, and the branches are named."""

    assert BRANCH_CARDS["generated_from"] == "7b9628ca0"
    # What this repository writes for those inputs, which the test above ties to the record plus the
    # named changes; asserting the record here would only restate the fixture to itself.
    lines = {entry["id"]: _body(_render(entry)) for entry in BRANCH_CARDS["entries"]}
    assert len(lines) == len(BRANCH_CARDS["entries"]) == 17
    # The action line is bounded at four even though the card covers six reports (#553 §5.2).
    six = lines["market-smart-money-action-change-six-reports"]
    assert "动作变化 3 次 · 首 平空 → 末 开空" in six
    assert "开多 $160,180.00 · 开多 · 平多 $7,500.25 · 开空 $1,000,000.00" in six
    assert "10:00–10:42" in six and "（6 条报道）" in six
    assert "js-2（来源标签，非已核实地址）" in six
    # A verified address carries no caveat, and an unlabelled one is named as unlabelled.
    assert "（来源标签，非已核实地址）" not in lines["market-smart-money-verified-address"]
    assert fmt.UNKNOWN_ACCOUNT in lines["market-smart-money-unlabelled-account"]
    assert fmt.UNKNOWN_VENUE in lines["market-smart-money-unlabelled-account"]
    assert fmt.UNKNOWN_MEASUREMENT in lines["market-oi-followup-unknown-venue-and-measurement"]
    assert f"OI ${fmt.UNKNOWN_FIGURE}" in lines["market-oi-missing-change"]
    # An escalated card is marked, a degraded one names no judgment, and both headers are bounded.
    escalated = next(e for e in BRANCH_CARDS["entries"] if e["id"] == "news-escalate-two-assets")
    assert escalated["card"]["header"]["title"]["content"].startswith("⚡ ")
    degraded = lines["news-degraded-with-description"]
    assert degraded.startswith("Provider description kept as wire text\n") and "利多" not in degraded
    assert lines["news-degraded-without-description"].count("\n") == 0
    for name in ("news-header-bounded-at-one-hundred", "market-oi-subject-bounded-at-one-hundred"):
        title = next(e for e in BRANCH_CARDS["entries"] if e["id"] == name)["card"]["header"]["title"]["content"]
        assert len(title) == 100


@pytest.mark.parametrize("entry", QUOTED_CARDS["entries"], ids=lambda entry: entry["id"])
def test_a_quoted_market_card_renders_as_this_branch_wrote_it_down(entry: dict[str, Any]) -> None:
    """#562 PR-B's own corpus: the surfaces production has not sent yet, pinned character for character."""

    assert _canonical(_render(entry)) == _canonical(entry["card"])


def test_the_quoted_corpus_covers_the_three_families_and_the_lines_it_claims() -> None:
    """Not vacuous: each new line is asserted where it belongs, and absent where it does not."""

    lines = {entry["id"]: _body(entry["card"]) for entry in QUOTED_CARDS["entries"]}
    assert len(lines) == len(QUOTED_CARDS["entries"]) == 7

    oi = lines["market-oi-quoted-with-whale-columns"]
    assert "行情 WIF $0.5432 24h +7.91%" in oi
    assert "鲸鱼多头盈利 88.4% · 鲸鱼持仓/OI 143.9%" in oi
    # A stale quote costs its line; the whale columns are facts of the frame and stay.
    stale = lines["market-oi-quote-stale-whale-still-shown"]
    assert "行情" not in stale and "鲸鱼多头盈利 88.4%" in stale
    # A reference too old to date the window costs the percentage, never the price (#88).
    assert "行情 WIF $0.5432\n" in lines["market-oi-quoted-without-a-fresh-reference"]

    liquidation = lines["market-liquidation-reported-price-and-quote"]
    # Below a dollar the money rule keeps six places, so the report's own figure is exact here too.
    assert "来源报告价 $0.2181" in liquidation
    assert "行情 DOGE $0.1998 24h -3.20%" in liquidation

    smart_money = lines["market-smart-money-reported-price-pnl-and-day-basis-quote"]
    assert "来源报告价 $3,120.50 · 已实现 PNL -$412.75" in smart_money
    # Hyperliquid publishes the venue's own day, so the window is named `日内` rather than assumed.
    assert "行情 ETH $3,125.40 日内 +0.42%" in smart_money
    assert "行情" not in lines["market-smart-money-reported-price-with-no-quote"]
    # The sign goes outside the currency mark, and a profit is not written as a negative loss.
    assert "来源报告价 $3,180.25 · 已实现 PNL $128,500.40" in lines["market-smart-money-positive-pnl"]


def test_the_two_older_corpora_are_untouched_by_the_market_quote() -> None:
    """The claim that keeps this a hard cut rather than a rewrite of what was already sent.

    Every market observation in the production and branch corpora predates the quote line, the
    reported price and the whale columns: none of those fields exists in their inputs, so no card in
    either file may print one. Their expected cards are asserted byte-for-byte above; this states
    *why* they could stay byte-identical rather than leaving it to look like luck.
    """

    market = [
        entry
        for corpus in (PRODUCTION_CARDS, BRANCH_CARDS["entries"])
        for entry in corpus
        if entry["source"] == "market"
    ]
    assert len(market) == 46
    for entry in market:
        assert "quotes" not in entry["inputs"]
        for row in entry["inputs"]["observations"]:
            assert not {"price", "pnl_usd", "whale_long_profit_bps", "whale_oi_ratio_bps"} & set(row)
        body = _body(entry["card"])
        assert "行情" not in body and "来源报告价" not in body and "鲸鱼" not in body


def test_every_named_change_belongs_to_a_card_and_is_the_only_one_this_branch_makes() -> None:
    """Not vacuous: `DELIBERATE_CHANGES` names real cards, every pair fires, and nothing else moved.

    A table nothing matches would let the byte tests above pass by doing nothing, and a spare entry
    would excuse a difference this branch never made. `_as_this_branch_renders` raises on a line it
    cannot find, so building each entry's expected card is itself the second half of that claim.
    """

    recorded = {entry["id"]: entry["sent_card"] for entry in PRODUCTION_CARDS}
    recorded |= {entry["id"]: entry["card"] for entry in BRANCH_CARDS["entries"]}
    assert set(DELIBERATE_CHANGES) <= set(recorded)
    assert len(DELIBERATE_CHANGES) == 6
    for entry_id, changes in DELIBERATE_CHANGES.items():
        before = _body(recorded[entry_id])
        after = _body(_as_this_branch_renders({"id": entry_id}, recorded[entry_id]))
        assert after != before
        assert len(after.split("\n")) == len(before.split("\n")) - sum(1 for _, line in changes if not line)
    # And the money invariant reaches the three families that carry a formatted figure at all.
    assert len(FORMATTED_MARKET_CARDS) == 50
    assert {entry["inputs"]["track"]["family"] for entry in FORMATTED_MARKET_CARDS} == {
        "oi",
        "liquidation",
        "smart_money",
    }


@pytest.mark.parametrize("entry", FORMATTED_MARKET_CARDS, ids=lambda entry: entry["id"][:24])
def test_a_market_card_writes_every_dollar_figure_the_one_way(entry: dict[str, Any]) -> None:
    """One card, one number system: the notional, the reported price, the PNL and the quote agree.

    The first structured smart-money card production sent read `开多 $200840` three lines above
    `行情 ARB $0.1938` (#562). Rather than pin those two lines, this re-derives every dollar figure
    on every market card in all three corpora through `card_format.money` and requires the card to
    already say what that function says. The OI value line is the one exception and is named as such.
    """

    for line in _body(_render(entry)).split("\n"):
        if line.startswith(COMPACT_LINE_PREFIX):
            continue
        for token in DOLLAR_FIGURE.findall(line):
            sign, digits = ("-", token[2:]) if token.startswith("-") else ("", token[1:])
            assert fmt.money(f"{sign}{digits.replace(',', '')}") == token


def test_the_close_caveat_is_printed_by_a_card_that_printed_a_close() -> None:
    """#553 §4.4: the note says what a `平` on *this* card means, so an open-only card omits it.

    Both branches come from the corpus rather than a constructed card: an account that has only
    opened, and one whose action line and timeline both carry a close.
    """

    lines = {entry["id"]: _body(_render(entry)) for entry in BRANCH_CARDS["entries"]}
    open_only = lines["market-smart-money-unlabelled-account"]
    assert "开多 $500" in open_only and "平" not in open_only
    assert "Close" not in open_only
    closed = lines["market-smart-money-verified-address"]
    assert "平空 $1,250,000.00" in closed
    assert closed.endswith("opennews smart_money（2 条报道） · 18:30")
    assert "Close 只表示来源报告的平仓/减仓动作，不代表账户已全部清仓。" in closed


@pytest.mark.parametrize("case", MONEY_FORMAT["prices"], ids=lambda case: case["value"])
def test_card_money_agrees_with_the_console(case: dict[str, str]) -> None:
    """`newsPriceAlignment.test.ts` asserts the same table; neither surface can be edited alone."""

    assert fmt.price(case["value"]) == case["price"]


@pytest.mark.parametrize("case", MONEY_FORMAT["changes"], ids=lambda case: str(case["value"]))
def test_card_change_agrees_with_the_console(case: dict[str, Any]) -> None:
    assert fmt.change(case["value"], MONEY_FORMAT["change_basis"]) == f"{MONEY_FORMAT['change_label']} {case['change']}"


def test_a_card_the_channel_cannot_price_says_nothing_rather_than_zero() -> None:
    """The two surfaces answer an absent number differently on purpose, so the table excludes it.

    The console prints `—` in a table cell that must keep its column; a card drops the entry, because
    a line reading `行情 BTC $—` is worse than no line at all (#88).
    """

    assert fmt.price(None) == fmt.price("0") == fmt.price("not-a-price") == fmt.price("1e40") == ""
    assert fmt.change(7.9, "who_knows") == fmt.change(None, "rolling_24h") == ""
    assert fmt.percent_from_bps(None) == fmt.usd_compact(None) == fmt.UNKNOWN_FIGURE
    # `money` inherits that answer, and puts a sign where a sign goes rather than after the `$`.
    assert fmt.money(None) == fmt.money("") == fmt.money("0") == fmt.money("not-a-price") == ""
    assert fmt.money("-412.75") == "-$412.75" and fmt.money("200840") == "$200,840.00"
