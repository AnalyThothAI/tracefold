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
    symbol_in_text,
)
from tracefold.news.learning.objective import (
    asset_claims_match,
    known_wrong_markets,
    typed_asset_claims,
    ungrounded_primaries,
)
from tracefold.news.market_review.pricing import QuoteRequest
from tracefold.news.models import MarketAsset, TriageVerdict, market_type_of
from tracefold.news.program.contracts import TriageContext, unambiguous_catalog_class
from tracefold.news.told_context import ToldLedgerSnapshot
from tracefold.news.updates.contracts import Asset

_FIXTURE = Path(__file__).parents[1] / "fixtures/news/issue_651_raw_cases.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]

# What the instrument catalogue holds for the symbols these Events carry. `SEI` is the whole point: the
# catalogue lists a Binance token and a `us.listed` NYSE ticker under it, and `instrument_classes()`
# collapses that to `crypto` before anything can see the ambiguity.
_CATALOG: dict[str, tuple[str, ...]] = {
    "SEI": ("crypto", "equity"),
    "AMP": ("crypto", "equity"),
    "XPT": ("commodity",),
    "INGM": ("equity",),
    "V": ("crypto", "equity"),
    "CRCL": ("equity",),
    "CP": ("crypto",),
}


def _snapshot_card(name: str) -> dict[str, Any]:
    case = _CASES[name]
    return dict(case["evidence_snapshots"][0]["snapshot"]["card"])


def _context(name: str, **overrides: Any) -> TriageContext:
    card = _snapshot_card(name) | overrides
    return TriageContext.from_card(
        card,
        watchlist=(),
        told_rows=[],
        now_ms=int(card["opened_at_ms"]) + 1,
        queue_lag_ms=0,
        catalog_candidates=_CATALOG,
    )


def _verdict(name: str) -> dict[str, Any]:
    return dict(_CASES[name]["verdicts"][0]["verdict"])


def _asset(symbol: str, market_type: str, role: str = "primary") -> dict[str, str]:
    return {"symbol": symbol, "market_type": market_type, "role": role}


# ------------------------------------------------------------------ (a) SEI: the contaminated snapshot
def test_the_sei_snapshot_shows_the_model_both_markets_the_catalogue_holds() -> None:
    """The catalogue's own ambiguity reaches the Program instead of being collapsed away (#651 §A)."""

    gate = _context("sei_contaminated").gate

    assert [candidate.symbol for candidate in gate.catalog_candidates] == ["SEI", "AMP"]
    assert dict(gate.catalog_candidates[0]) == {"symbol": "SEI", "classes": ("crypto", "equity")}
    # Two classes prove nothing on their own, and code says so rather than picking the first.
    assert unambiguous_catalog_class(gate.catalog_candidates, "SEI") == "unknown"
    assert unambiguous_catalog_class(gate.catalog_candidates, "XPT") == "unknown"


def test_a_wrong_market_and_a_role_swap_both_miss_the_accepted_sei_answer() -> None:
    """Two candidates that compared *equal* to the reviewer's answer before #651, and why each is wrong.

    The stored `sei_contaminated` verdict named `SEI/crypto` as the primary of a guidance release. The
    accepted answer is the NYSE-listed insurer. Under the untyped comparison both sides projected to
    `frozenset({"SEI"})` and the candidate scored a clean hit; so did a candidate that demoted the
    subject to a mention, because role was not part of the projection either.
    """

    gold = typed_asset_claims([_asset("SEI", "equity")])
    observed = typed_asset_claims(_verdict("sei_contaminated")["assets"])

    assert observed == frozenset({("primary", "SEI", "crypto")})
    assert not asset_claims_match(observed, gold)
    # Named separately: "you named the wrong company" and "you read this company as a coin" are different
    # defects with different repairs.
    assert known_wrong_markets(observed, gold) == ("SEI/crypto",)

    swapped = typed_asset_claims([_asset("SEI", "equity", "mentioned")])
    assert not asset_claims_match(swapped, gold)
    assert known_wrong_markets(swapped, gold) == ()

    assert asset_claims_match(typed_asset_claims([_asset("SEI", "equity")]), gold)


def test_an_unknown_market_on_either_side_cannot_contradict_a_known_one() -> None:
    """The honest half of the rule: `unknown` is what every asset written before #651 carries."""

    gold = typed_asset_claims([_asset("SEI", "equity")])
    untyped = typed_asset_claims([{"symbol": "SEI", "role": "primary"}])

    assert untyped == frozenset({("primary", "SEI", "unknown")})
    assert asset_claims_match(untyped, gold)
    assert asset_claims_match(gold, untyped)
    assert known_wrong_markets(untyped, gold) == ()
    # A pre-#651 free string is outside the vocabulary and means the same thing: nothing established.
    assert market_type_of("token") == market_type_of("cex") == market_type_of(None) == "unknown"


# ------------------------------------------------------------- (b) Platinum / Ingram: two subjects
def test_a_commodity_primary_never_takes_an_equity_card_and_is_reported_as_a_wrong_market() -> None:
    gold = typed_asset_claims([_asset("INGM", "equity")])
    platinum = typed_asset_claims([_asset("XPT", "commodity")])

    assert not asset_claims_match(platinum, gold)
    # Different symbols: an ordinary grounding miss, not a market contradiction.
    assert known_wrong_markets(platinum, gold) == ()
    assert known_wrong_markets(typed_asset_claims([_asset("INGM", "commodity")]), gold) == ("INGM/commodity",)

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


def test_a_primary_the_gate_did_not_ground_is_still_grounded_by_the_event() -> None:
    """The gate that zeroed the correct Visa answer for not matching the *grounded* tag (#651 §5).

    `727ffc0b` is exact about why the old rule was wrong. The provider sent four coin tags — `XPL`,
    `CRCL`, `XYZ-CRCL` and `V` — and the Gate grounded only the two `CRCL` spellings, because a B+ tag
    grounds only when the text spells it and this text says `Visa`, not `V`. So the Event names `V`,
    the catalogue holds it, the model read Visa out of the headline and answered `V` — and the metric
    zeroed the case for it. Grounding is now the whole question "does this Event name this symbol at
    all", which the Gate's grounding bar was never asking.
    """

    context = _context("visa_restated")
    evidence_text = " ".join((context.evidence.title, context.evidence.raw_first_line, context.evidence.content))

    # Neither `V` nor `CRCL` is spelled in the text, and the Gate grounded only `CRCL`.
    assert "Visa" in evidence_text and not symbol_in_text("V", evidence_text)
    assert context.evidence.provider_coins == ("XPL:C", "CRCL:A", "XYZ-CRCL:A", "V:B+")
    assert unambiguous_catalog_class(context.gate.catalog_candidates, "V") == "unknown"  # two markets

    assert (
        ungrounded_primaries(
            [_asset("V", "equity")],
            grounded={"CRCL"},
            evidence_text=evidence_text,
            catalog_candidates=context.gate.catalog_candidates,
        )
        == ()
    )
    # The other half of the rule, on an Event that names nothing at all in the catalogue: a symbol the
    # evidence text spells as its own token is grounded by the text alone.
    assert (
        ungrounded_primaries(
            [_asset("NVDA", "equity")],
            grounded={"CRCL"},
            evidence_text="NVDA to invest $100bn in OpenAI data centres",
            catalog_candidates=(),
        )
        == ()
    )
    # And the gate that stays: a primary the Event names nowhere is invented, not grounded.
    assert ungrounded_primaries(
        [_asset("ZZZZ", "equity")],
        grounded={"CRCL"},
        evidence_text=evidence_text,
        catalog_candidates=context.gate.catalog_candidates,
    ) == ("ZZZZ",)


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


def _card_assets(*assets: dict[str, Any]) -> list[MarketAsset]:
    """The card assets of one adopted claim carrying these asset readings."""

    item = source("Company announces a listing.")
    update = adopted((draft("a", item, assets=tuple(Asset.model_validate(row) for row in assets)), item))
    return update_card_assets(update, [update.claims[0].ref])


# --------------------------------------------------- (e) storyline and told: two SEIs are two stories
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


@pytest.mark.parametrize(
    ("candidate_market", "row_market", "tier"),
    [
        ("crypto", "crypto", "asset_overlap"),
        ("crypto", "equity", "recency"),
        ("unknown", "equity", "asset_overlap"),
        ("crypto", "unknown", "asset_overlap"),
    ],
)
def test_the_told_asset_tier_only_refuses_two_markets_that_both_say_something(
    candidate_market: str, row_market: str, tier: str
) -> None:
    """The retrieval half of the same rule: a delivered SEI-equity card is not evidence about SEI the coin."""

    row = {
        "event_id": "delivered",
        "at_ms": 1_000,
        "storyline_key": "",
        "comparison_title": "totally different phrasing",
        "comparison_fingerprint": "f",
        "dedupe_family": "general",
        "grounded_assets": [],
        "assets": [{"symbol": "SEI", "market_type": row_market}],
        "canonical_assets": [],
        "direction": "bullish",
        "headline_zh": "已推送的 SEI 卡片",
        "why_zh": "",
    }

    snapshot = ToldLedgerSnapshot.select(
        [row],
        now_ms=2_000,
        storyline_key="",
        symbols=[{"symbol": "SEI", "market_type": candidate_market}],
        comparison_title="wholly unrelated wording",
    )

    assert [entry.tier for entry in snapshot.entries] == [tier]
    # The model-visible field stays the bare symbol: the told trace's shape is a PostgreSQL CHECK.
    assert snapshot.entries[0].symbols == ("SEI",)


def test_a_provider_tag_in_the_told_ledger_never_claims_a_market() -> None:
    row = {
        "event_id": "delivered",
        "at_ms": 1_000,
        "storyline_key": "",
        "comparison_title": "x",
        "comparison_fingerprint": "f",
        "dedupe_family": "general",
        "grounded_assets": ["XYZ-SEI"],
        "assets": [],
        "canonical_assets": [],
        "direction": "bullish",
        "headline_zh": "标题",
        "why_zh": "",
    }

    snapshot = ToldLedgerSnapshot.select(
        [row],
        now_ms=2_000,
        storyline_key="",
        symbols=[{"symbol": "SEI", "market_type": "equity"}],
        comparison_title="y",
    )

    assert [entry.tier for entry in snapshot.entries] == ["asset_overlap"]


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
