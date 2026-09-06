"""The reader card is one value object with one formatter, and it still sends the cards it sent (#562 PR-A).

Two claims, both about characters rather than structure:

* **Nothing a reader sees moved, except where this branch says it does.** Every card in
  `reader_card_production_cards.json` is a card production actually sent, with the inputs that
  produced it. The News first card and all three market families are rebuilt through `ReaderCard` and
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
those inputs and is asserted for all 46. `sent_card` is the JSON the provider received; it matches
`card` for the 16 cards sent by today's code, and differs for 30 older market cards in exactly two
already-shipped ways -- the #553 header-separator fix, and the same change's rule that a relative
`/news/market/<id>` is not a link a Feishu client can follow, so no button is offered. Their markdown
body, which is everything the reader reads, is asserted identical to what was sent.

The two unstructured cards production sent are no longer in the corpus. #582 §3.2 deleted the branch
that prepared them -- an unstructured record is stored, readable and never a card -- so there is no
renderer left to rebuild them with, and a fixture asserting a card this repository cannot produce
would be asserting the fixture to itself. Their delivery rows stay in production as receipts.
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
from tracefold.news.reader_card import NEWS_HEADLINE_MAX, ReaderCardHeadline, ReaderCardQuote, reader_news

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


# Every line this repository now changes about a card already written down, by the entry that carries
# it. The two corpora keyed here are records -- `sent_card` is what a provider received, and every
# branch `card` is what the renderers on 7b9628ca0 wrote -- so neither is edited; what this branch
# renders differently is named here instead, one whole line at a time, and any other difference still
# fails. The header title is one such line. An empty replacement is a line this branch no longer
# prints.
#
# Four reasons, and no fifth: the money rule (a market card's dollar figures are the quote line's
# formatter, so a reader is not asked to read `开多 $200840` three lines above `行情 ARB $0.1938`),
# the Close caveat (printed by a card that printed a Close), the largest reported amount (chosen
# as a number: the three-report liquidation also reported `1000000`, and `max` over the text
# answered `980000` because `"9" > "1"`), and the closing card's own qualifier -- `action_change` is
# smart money's second card of a round and it has exactly one meaning, so it is headed `平仓` rather
# than by the mechanism that noticed (#582 §3.1).
DELIBERATE_CHANGES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "26f40a0bf365ddeb483e7b5c34dd7aef": (("最大单笔来源报告金额 $1000000", "最大单笔来源报告金额 $1,000,000.00"),),
    "21ba1086b3635b00637114f76d8d79ce": (("最大单笔来源报告金额 $743120", "最大单笔来源报告金额 $743,120.00"),),
    "market-smart-money-action-change-six-reports": (
        (
            "开多 $160180 · 开多 · 平多 $7500.25 · 开空 $1000000",
            "开多 $160,180.00 · 开多 · 平多 $7,500.25 · 开空 $1,000,000.00",
        ),
        ("聪明钱 · 动作变化", "聪明钱 · 平仓"),
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


# Every market card whose figures this repository writes, which is every market card there is.
FORMATTED_MARKET_CARDS: Final[list[dict[str, Any]]] = [
    entry
    for corpus in (PRODUCTION_CARDS, BRANCH_CARDS["entries"], QUOTED_CARDS["entries"])
    for entry in corpus
    if entry["source"] == "market"
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
    title = card["header"]["title"]
    for before, after in changes:
        if title["content"] == before:
            # A header is rewritten, never dropped: a card with no header is not a card.
            assert after, entry["id"]
            title["content"] = after
            continue
        for element in card["elements"]:
            if element["tag"] != "markdown":
                continue
            lines = element["content"].split("\n")
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
    }
    whole = [entry for entry in PRODUCTION_CARDS if entry["reproduces_sent_card"]]
    assert len(PRODUCTION_CARDS) == 46
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
    """The 30 older market cards, held to the two named differences and byte-equal everywhere else.

    #553 changed exactly two things about a market card. The header gained its separator: the old join
    wrote `持仓异动 FLOCK`, a family and an instrument with no separator between them. And a relative
    `/news/market/<id>` stopped being offered as a button, because no Feishu or Telegram client can
    follow one — the note line carries the item id instead, which is what an operator needs to reach
    the same page.

    Asserting "the template matches and the tags differ" would have passed for a card that had lost a
    line. This states each difference and requires everything else to be identical.
    """

    rebuilt, sent = _render(entry), entry["sent_card"]
    assert entry["source"] == "market"

    rebuilt_title = rebuilt["header"]["title"]["content"]
    sent_title = sent["header"]["title"]["content"]
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

    Every sent market card is a single-observation `first`, so the smart-money account line,
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
    assert len(lines) == len(BRANCH_CARDS["entries"]) == 16
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
    assert len(market) == 43
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
        rebuilt = _as_this_branch_renders({"id": entry_id}, recorded[entry_id])
        assert _canonical(rebuilt) != _canonical(recorded[entry_id])
        before, after = _body(recorded[entry_id]), _body(rebuilt)
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


# --- #582 §3.3: the News an OI card's own instrument already has ----------------------------------
#
# One recorded card carries both claims below. `market-oi-quoted-with-whale-columns` is a real quoted
# OI card with the whale line these lines follow, so "where do they go" and "what do they cost" are
# measured against a card this repository already renders rather than a constructed one.

_OI_ENTRY_ID: Final = "market-oi-quoted-with-whale-columns"
_LIQUIDATION_ENTRY_ID: Final = "market-liquidation-three-reports"
# A headline at the clip bound in the script production writes them in. 40 characters of Chinese is
# 120 UTF-8 bytes, not 40, which is the whole reason the byte bound is asserted separately.
_LONGEST_HEADLINE: Final = "特" * (NEWS_HEADLINE_MAX + 10)
_WIDEST_NEWS: Final = {
    "news_pushed": tuple(ReaderCardHeadline(headline=_LONGEST_HEADLINE, at_ms=1_788_549_480_000) for _ in range(3)),
    "news_total": 99,
}
# What the widest possible news block costs, measured rather than claimed. #582 §3.3 estimated "at
# most +4 lines, about +250 bytes"; the line bound is exact, and the byte figure is the *typical*
# headline. A headline clipped at `NEWS_HEADLINE_MAX` is 40 Chinese characters -- 120 UTF-8 bytes, not
# 40 -- so the bound the code can actually be held to is the one below, and the typical case is
# asserted beside it so both numbers stay honest.
_NEWS_LINES_MAX: Final = 4
_NEWS_BODY_BYTES_MAX: Final = 429
_NEWS_CARD_BYTES_MAX: Final = 433
_TYPICAL_HEADLINE_CHARS: Final = 22
_NEWS_BODY_BYTES_TYPICAL: Final = 266


def _corpus_entry(entry_id: str) -> dict[str, Any]:
    return next(
        entry
        for corpus in (PRODUCTION_CARDS, BRANCH_CARDS["entries"], QUOTED_CARDS["entries"])
        for entry in corpus
        if entry["id"] == entry_id
    )


def _recorded_card(entry_id: str, **news: Any) -> dict[str, Any]:
    """One corpus card re-rendered, optionally carrying the News its instrument already had."""

    inputs = _corpus_entry(entry_id)["inputs"]
    return render_market_card(
        track=MarketTrack(**inputs["track"]),
        reason=inputs["reason"],
        observations=[MarketObservation(**row) for row in inputs["observations"]],
        detail_url=inputs["detail_url"],
        action_changes=inputs["action_changes"],
        quotes=[ReaderCardQuote(**quote) for quote in inputs.get("quotes", ())],
        **news,
    )


def test_an_oi_card_names_the_news_its_instrument_already_has() -> None:
    """The counts, then the titles, after the whale line and before the facts line the card ends on."""

    body = _body(
        _recorded_card(
            _OI_ENTRY_ID,
            news_pushed=(
                ReaderCardHeadline(headline="美国对进口芯片加征关税", at_ms=1_788_549_000_000),
                ReaderCardHeadline(headline="某交易所宣布下架三个永续合约", at_ms=1_788_500_000_000),
            ),
            news_total=5,
        )
    )

    assert body.split("\n") == [
        "上升 6.12% · 03:18",
        "OI $11.03M · binance · oi_signal_v1|opennews_oi_source_v1|300000",
        "行情 WIF $0.5432 24h +7.91%",
        "鲸鱼多头盈利 88.4% · 鲸鱼持仓/OI 143.9%",
        "相关新闻 48h · 已推 2 · 共 5",
        "· 美国对进口芯片加征关税 03:10",
        "· 某交易所宣布下架三个永续合约 13:33",
        "WIF · opennews oi（1 条报道） · 03:18",
    ]


def test_an_oi_card_whose_instrument_had_no_news_prints_no_news_line() -> None:
    """`已推 0 · 共 0` is four bytes to say a token had no news, which is the ordinary case (#582 §1)."""

    plain = _body(_recorded_card(_OI_ENTRY_ID))

    assert "相关新闻" not in plain
    # And it is the *total* that decides, not the absence of headlines: a card handed pushed titles
    # for a window that held no Event still prints nothing, because the two numbers are one claim.
    assert _body(_recorded_card(_OI_ENTRY_ID, news_pushed=_WIDEST_NEWS["news_pushed"], news_total=0)) == plain


def test_a_window_of_news_nobody_was_told_about_still_reaches_the_reader() -> None:
    """Five Events and no card is exactly what an operator reading an OI number wants to know."""

    lines = _body(_recorded_card(_OI_ENTRY_ID, news_total=5)).split("\n")

    assert "相关新闻 48h · 已推 0 · 共 5" in lines
    assert not any(line.startswith("· ") for line in lines)


def test_a_headline_wider_than_the_card_is_clipped_rather_than_dropped() -> None:
    """An overflow costs the tail of one line; the console holds the rest."""

    lines = _body(_recorded_card(_OI_ENTRY_ID, **_WIDEST_NEWS)).split("\n")
    headline = next(line for line in lines if line.startswith("· "))

    assert headline == f"· {'特' * (NEWS_HEADLINE_MAX - 1)}… 03:18"


def test_the_count_is_the_headlines_the_card_actually_printed() -> None:
    """`已推 n` and the lines under it are one list, so a titleless row cannot inflate the number.

    The row is dropped where the read is composed rather than where it is rendered, so this goes
    through `reader_news` -- the path the loop uses -- instead of handing the card model a value the
    read can no longer produce.
    """

    pushed, total = reader_news(
        {
            "pushed": [
                {"event_id": "e1", "headline_zh": "有标题的一条", "at_ms": 1_788_549_480_000},
                {"event_id": "e2", "headline_zh": "   ", "at_ms": 1_788_549_480_000},
            ],
            "total": 2,
        }
    )
    lines = _body(_recorded_card(_OI_ENTRY_ID, news_pushed=pushed, news_total=total)).split("\n")

    assert "相关新闻 48h · 已推 1 · 共 2" in lines
    assert [line for line in lines if line.startswith("· ")] == ["· 有标题的一条 03:18"]


def test_the_news_lines_cost_at_most_four_lines_and_the_bytes_they_claim() -> None:
    """#582 §3.3's volume bound, measured on a recorded card at the widest input the model allows."""

    plain, widest = _recorded_card(_OI_ENTRY_ID), _recorded_card(_OI_ENTRY_ID, **_WIDEST_NEWS)
    plain_body, widest_body = _body(plain), _body(widest)

    # The card this is measured against is the one #582 §1 measured: 5 lines, 600-700 bytes on the wire.
    assert len(plain_body.split("\n")) == 5
    assert 600 <= len(_canonical(plain).encode("utf-8")) <= 700

    assert len(widest_body.split("\n")) - len(plain_body.split("\n")) == _NEWS_LINES_MAX
    assert len(widest_body.encode("utf-8")) - len(plain_body.encode("utf-8")) == _NEWS_BODY_BYTES_MAX
    assert len(_canonical(widest).encode("utf-8")) - len(_canonical(plain).encode("utf-8")) == _NEWS_CARD_BYTES_MAX

    # And what the estimate in the Issue describes: three headlines of the length production writes.
    typical = _recorded_card(
        _OI_ENTRY_ID,
        news_pushed=tuple(
            ReaderCardHeadline(headline="美" * _TYPICAL_HEADLINE_CHARS, at_ms=1_788_549_480_000) for _ in range(3)
        ),
        news_total=5,
    )
    assert len(_body(typical).encode("utf-8")) - len(plain_body.encode("utf-8")) == _NEWS_BODY_BYTES_TYPICAL


def test_only_the_oi_card_prints_the_news_lines() -> None:
    """§3.3 leaves liquidation and smart money out deliberately; the render is where that is visible."""

    body = _body(_recorded_card(_LIQUIDATION_ENTRY_ID, **_WIDEST_NEWS))

    assert "相关新闻" not in body and "特特" not in body


def test_the_news_read_becomes_card_facts_bounded_to_what_a_card_prints() -> None:
    """`reader_news` is `reader_quotes`' twin: the port's mapping, bounded, never trusted."""

    payload = {
        "pushed": [
            {"event_id": f"e{index}", "headline_zh": f"标题{index}", "at_ms": 1_788_549_000_000 + index}
            for index in range(5)
        ],
        "total": 9,
    }
    pushed, total = reader_news(payload)

    assert [item.headline for item in pushed] == ["标题0", "标题1", "标题2"]
    assert total == 9
    # Everything it cannot read is absent rather than wrong: this line is display, and a broken read
    # costs it and nothing else.
    assert reader_news({}) == ((), 0)
    assert reader_news({"pushed": "not-a-list", "total": "not-a-number"}) == ((), 0)
    assert reader_news({"pushed": [{"headline_zh": "  "}, {"headline_zh": "有"}], "total": -3}) == (
        (ReaderCardHeadline(headline="有", at_ms=0),),
        0,
    )
