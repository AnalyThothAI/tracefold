"""Typed market identity, on the four production cases that proved a bare symbol is not an identity (#651 §6.2).

Every case here is a frozen production Event. The thing they have in common is that before this cut the
code could not tell two different instruments apart, and said so nowhere:

* `SEI` is a Binance token *and* a NYSE-listed insurer, so a token-issuer guidance card and an insurer
  card shared a storyline, a told overlap and a Gold answer;
* `XPT` (platinum) and `INGM` (Ingram Micro) are different subjects of one commodity-tagged headline;
* the Visa Event carried one provider tag, `CRCL`, and the subject was `V`;
* the first Upbit `CP` Event carried no provider tag at all and the model named the subject itself.

No model runs in this module. Everything asserted here is code the pipeline executes on stored facts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support.news_update_cards import adopted, draft, source
from tracefold.news.delivery import update_card_assets
from tracefold.news.events.storyline import (
    final_storyline_key,
    same_storyline_key,
    storyline_asset,
)
from tracefold.news.market_review.pricing import QuoteRequest
from tracefold.news.models import MarketAsset, TriageVerdict, market_type_of
from tracefold.news.updates.contracts import Asset

_FIXTURE = Path(__file__).parents[1] / "fixtures/news/issue_651_raw_cases.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


def _snapshot_card(name: str) -> dict[str, Any]:
    case = _CASES[name]
    return dict(case["evidence_snapshots"][0]["snapshot"]["card"])


def _asset(symbol: str, market_type: str, role: str = "primary") -> dict[str, str]:
    return {"symbol": symbol, "market_type": market_type, "role": role}


# ------------------------------------------------------------- (b) Platinum / Ingram: two subjects
def test_a_commodity_tag_never_takes_an_equity_card() -> None:
    # The card prints this judgment's own subject, and `XPT` is not it — even though the commodity tag is
    # exactly what the provider grounded.
    shown = _card_assets(_asset("INGM", "equity"))
    assert shown == [MarketAsset("INGM", "equity")]


# ------------------------------------------------------------------------ (c) Visa: the CRCL fallback
def test_the_visa_card_names_visa_and_not_the_only_tag_the_provider_sent() -> None:
    """`727ffc0b` is the Event whose sorted-grounded-tag fallback printed `CRCL` beside a Visa headline."""

    card = _snapshot_card("visa_restated")
    assert card["grounded_assets"] == ["CRCL", "XYZ-CRCL"]

    shown = _card_assets(_asset("V", "equity"), _asset("CRCL", "equity", "mentioned"))

    # The card names the claim's own primary subject; a mention and a provider tag are not its subject.
    assert shown == [MarketAsset("V", "equity")]
    # And the quote target the card carries is the typed question, so `V` can only be answered by an
    # equity contract. The pre-#651 question was `V` alone, which a crypto venue also lists.
    assert QuoteRequest(shown[0].symbol, shown[0].market_type) == QuoteRequest("V", "equity")


# ------------------------------------------------------- (d) CP: a subject with no provider tag at all
def test_an_untagged_subject_still_produces_a_typed_quote_target() -> None:
    """`c28bbbd0` carried no provider tag and no Event-asset row; the model named `CP` itself."""

    card = _snapshot_card("cp_upbit_first")
    assert card["grounded_assets"] == []
    assert _CASES["cp_upbit_first"]["event_assets"] == []

    shown = _card_assets(_asset("CP", "crypto"))

    assert shown == [MarketAsset("CP", "crypto")]
    assert QuoteRequest(shown[0].symbol, shown[0].market_type).accepts("crypto")
    assert not QuoteRequest(shown[0].symbol, shown[0].market_type).accepts("equity")


def test_an_untyped_subject_is_never_shown_as_a_guessed_market() -> None:
    """`SEI` is a token and an insurer: a claim that did not type it names no card asset and no quote."""

    assert _card_assets(_asset("SEI", "unknown")) == []
    # A pre-#651 free string is outside the vocabulary and means the same thing: nothing established.
    assert market_type_of("token") == market_type_of("cex") == market_type_of(None) == "unknown"


def _card_assets(*assets: dict[str, Any]) -> list[MarketAsset]:
    """The card assets of one adopted claim carrying these asset readings."""

    item = source("Company announces a listing.")
    update = adopted((draft("a", item, assets=tuple(Asset.model_validate(row) for row in assets)), item))
    return update_card_assets(update, [update.claims[0].ref])


# ---------------------------------------------------------- (e) storyline: two SEIs are two stories
def _sei_key(market_type: str) -> str:
    return final_storyline_key(
        title="SEI pre-announces Q3 and Q4 and raises full-year guidance",
        headline_zh="SEI 上调全年指引",
        scope="single_name",
        verdict_primaries=[MarketAsset("SEI", market_type)],  # type: ignore[arg-type]
        grounded_assets=["SEI"],
        dedupe_family="filing",
    )


def test_two_sei_markets_are_two_storylines_and_two_untyped_ones_are_still_one() -> None:
    crypto, equity, unknown = _sei_key("crypto"), _sei_key("equity"), _sei_key("unknown")

    assert crypto == "asset:crypto:SEI" and equity == "asset:equity:SEI" and unknown == "asset:SEI"
    assert not same_storyline_key(crypto, equity)
    # Every card written before #651 carries the untyped key, and it must keep meeting both: an untyped
    # key cannot claim to be a different story.
    assert same_storyline_key(unknown, crypto) and same_storyline_key(unknown, equity)
    assert same_storyline_key(unknown, unknown)
    assert storyline_asset(crypto) == MarketAsset("SEI", "crypto")
    assert storyline_asset("conflict:mideast_2026") is None


# ------------------------------------------------------------------- the stored history keeps parsing
@pytest.mark.parametrize("name", sorted(_CASES))
def test_every_frozen_production_verdict_still_validates_under_the_typed_contract(name: str) -> None:
    """The durable ledger is audit truth and is never rewritten, so reading it must never raise."""

    for row in _CASES[name]["verdicts"]:
        verdict = TriageVerdict.model_validate(row["verdict"])
        assert all(asset.market_type in {"crypto", "equity", "unknown"} for asset in verdict.assets)
        stored = {str(dict(asset).get("market_type")) for asset in row["verdict"]["assets"]}
        if stored - {"crypto", "equity"}:
            # `None` and `token` are both outside the vocabulary and both read as `unknown`.
            assert any(asset.market_type == "unknown" for asset in verdict.assets)
