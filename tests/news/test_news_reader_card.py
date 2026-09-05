"""The reader card is one value object with one formatter, and it still sends the cards it sent (#562 PR-A).

Two claims, both about characters rather than structure:

* **Nothing a reader sees moved.** Every card in `reader_card_production_cards.json` is a card
  production actually sent, with the inputs that produced it. The News first card and all four market
  families are rebuilt through `ReaderCard` and the Feishu serializer and compared as JSON values --
  key order is PostgreSQL's, since the frozen snapshot is `jsonb`, so a canonical dump is the byte
  comparison that means anything here.
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
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import pytest

from tracefold.news import card_format as fmt
from tracefold.news.delivery import render_first_card
from tracefold.news.market_notifications import MarketObservation, MarketTrack, render_market_card

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "news"
PRODUCTION_CARDS: Final[list[dict[str, Any]]] = json.loads(
    (FIXTURES / "reader_card_production_cards.json").read_text(encoding="utf-8")
)
BRANCH_CARDS: Final[dict[str, Any]] = json.loads(
    (FIXTURES / "reader_card_branch_cards.json").read_text(encoding="utf-8")
)
MONEY_FORMAT: Final[dict[str, Any]] = json.loads((FIXTURES / "card_money_format.json").read_text(encoding="utf-8"))


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


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
    )


def _body(card: dict[str, Any]) -> str:
    return next(element["content"] for element in card["elements"] if element["tag"] == "markdown")


@pytest.mark.parametrize("entry", PRODUCTION_CARDS, ids=lambda entry: f"{entry['source']}-{entry['id'][:12]}")
def test_a_production_card_is_rebuilt_character_for_character(entry: dict[str, Any]) -> None:
    """The whole card: header, body, button and note, from the same facts through the new model."""

    assert _canonical(_render(entry)) == _canonical(entry["card"])


@pytest.mark.parametrize("entry", PRODUCTION_CARDS, ids=lambda entry: f"{entry['source']}-{entry['id'][:12]}")
def test_the_body_a_reader_read_is_the_body_this_repository_writes(entry: dict[str, Any]) -> None:
    """Every line of every sent card, including the 32 that predate the #553 header and link fix."""

    assert _body(_render(entry)) == _body(entry["sent_card"])


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
        assert _canonical(entry["card"]) == _canonical(entry["sent_card"])


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
    assert rebuilt_elements["markdown"] == sent_elements["markdown"]

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

    assert _canonical(_render(entry)) == _canonical(entry["card"])


def test_the_branch_corpus_names_the_base_it_was_generated_from_and_covers_what_it_claims() -> None:
    """Not vacuous: the fixture is a record of the old renderers, and the branches are named."""

    assert BRANCH_CARDS["generated_from"] == "7b9628ca0"
    lines = {entry["id"]: _body(entry["card"]) for entry in BRANCH_CARDS["entries"]}
    assert len(lines) == len(BRANCH_CARDS["entries"]) == 17
    # The action line is bounded at four even though the card covers six reports (#553 §5.2).
    six = lines["market-smart-money-action-change-six-reports"]
    assert "动作变化 3 次 · 首 平空 → 末 开空" in six
    assert "开多 $160180 · 开多 · 平多 $7500.25 · 开空 $1000000" in six
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
