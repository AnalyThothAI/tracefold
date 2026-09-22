"""Replay the recorded 24 h delivered ledger against the #675 PR-3 grounding rules.

`tests/fixtures/news/grounding_replay_24h.jsonl` is one row per card the reader received between
2026-09-21 07:10 and 2026-09-22 07:10 UTC: 417 of them, carrying the leader frame the Gate saw (title,
first line, provider coins), the frozen evidence text, the assets the model named, the catalogue candidate
rows it was shown, and the label an independent reviewer gave the card. `audit_case` names the 27 rows the
Issue's grounding audit called out by hand.

Two rules run against it and the third column of this file is what they *do not* do. A rule that demoted
every primary the text does not spell would take 50 of 338 primaries with it, 16 of them on cards a
reviewer wanted kept -- `LMT` for Lockheed Martin, `ACN` for Accenture, `HK1810` for 小米. That measurement
is asserted here too, because it is the reason the support reading is published as a signal instead of
being wired into a decision.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.news.test_news_policy_v16_replay import proxy_fact_kind
from tests.support.news_judgment import news_taxonomy, scored_judgment
from tracefold.news.events import grounding as grounding_module
from tracefold.news.events.gate import grounded_assets
from tracefold.news.events.grounding import (
    COMMODITY_CONTEXT,
    AssetGrounding,
    asset_grounding,
    commodity_context_present,
    symbol_support,
    verdict_grounding,
)
from tracefold.news.market_review.instruments import ALIAS_SEEDS, COMMODITY_SYMBOLS
from tracefold.news.models import TriageVerdict
from tracefold.news.program.assembly import contradicted_primary_symbols
from tracefold.news.program.module import _demote_contradicted_primaries
from tracefold.news.program.signatures import EventSemantics
from tracefold.news.timeline import event_timeline
from tracefold.news.triage_rules import GateFacts, StorylineStatus, decide

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "grounding_replay_24h.jsonl"
# The same delivered day recorded for the policy replay (#675 PR-1), which carries the relevance and
# taxonomy columns `decide()` reads. Joined by event id so the delivery consequence of a demotion is
# measured through the production decision rather than asserted from the rule's own point of view.
POLICY_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "policy_v16_replay_24h.jsonl"
NOW = 1_800_000_000_000
# The 上期所 margin notice: the provider tagged gold and silver on a frame that adjusts copper, aluminium,
# zinc and lead, and the model made both its primaries. It is the one delivered card whose own primary
# loses its grounding to the commodity condition.
SHFE_MARGIN_NOTICE = "铜、铝、锌、铅、氧化铝期货"


def _rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def rows() -> list[dict[str, Any]]:
    return _rows()


def _text(row: dict[str, Any]) -> str:
    return " \n".join([row["title"], row["raw_first_line"], *row["evidence_text"]])


def _readings(row: dict[str, Any]) -> tuple[AssetGrounding, ...]:
    return verdict_grounding(
        row["assets"],
        text=_text(row),
        grounded=row["grounded_assets"],
        candidates=row["catalog_candidates"],
    )


def _primaries(row: dict[str, Any]) -> list[AssetGrounding]:
    return [reading for reading in _readings(row) if reading.role == "primary"]


def test_the_recording_is_the_delivered_day_and_carries_its_audit_labels(rows: list[dict[str, Any]]) -> None:
    assert len(rows) == 417
    assert all(row["reviewer_verdict"] in {"keep", "borderline", "demote"} for row in rows)
    assert sum(1 for row in rows if row["audit_case"]) == 28
    assert sum(1 for row in rows if any(asset["role"] == "primary" for asset in row["assets"])) == 338


# ------------------------------------------------------------------ the Gate condition


def test_the_commodity_table_only_narrows_the_catalogue_vocabulary() -> None:
    """Every gated key is a commodity the catalogue already calls one, and oil is deliberately not here."""

    for symbol in COMMODITY_CONTEXT:
        resolved = ALIAS_SEEDS.get(symbol, symbol)
        assert resolved in COMMODITY_SYMBOLS, symbol
    assert not {"CL", "WTI", "OIL", "USOIL", "BRENTOIL"} & set(COMMODITY_CONTEXT)
    # A symbol the table says nothing about is grounded exactly as before.
    assert commodity_context_present("BTC", "anything at all")
    assert commodity_context_present("COPPER", "Copper surges toward record on LME")
    assert commodity_context_present("COPPER", "韦丹塔旗下的孔科拉铜业已完成检修")
    assert not commodity_context_present("XAU", "央行：9月23日将在香港发行600亿元中央银行票据。")


def test_a_commodity_tag_without_its_commodity_stops_grounding(
    rows: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured diff of the condition over the delivered day, card by card.

    The baseline is the same Gate with the table emptied, so the diff is this rule and nothing else --
    not a comparison with what production stored, which was grounded over every member of the Event and
    against an older storyline registry.
    """

    after = [tuple(grounded_assets(row["title"], row["coins"], raw_first_line=row["raw_first_line"])) for row in rows]
    monkeypatch.setattr(grounding_module, "COMMODITY_CONTEXT", {})
    before = [tuple(grounded_assets(row["title"], row["coins"], raw_first_line=row["raw_first_line"])) for row in rows]

    changed = [(row, sorted(set(b) - set(a))) for row, b, a in zip(rows, before, after, strict=True) if set(b) - set(a)]
    assert [set(a) - set(b) for _, b, a in zip(rows, before, after, strict=True)] == [set()] * len(rows)
    assert len(changed) == 10
    assert sum(len(lost) for _, lost in changed) == 20
    assert {symbol.removeprefix("XYZ-") for _, lost in changed for symbol in lost} == {
        "XAU",
        "GOLD",
        "XAG",
        "SILVER",
        "COPPER",
    }
    # Every one of them is a frame that never names the metal: 央行票据, SoftBank's bond sale, four Hong
    # Kong filings, the 上期所 notice, a diesel export story tagged COPPER.
    monkeypatch.undo()
    for row, lost in changed:
        for symbol in lost:
            assert not commodity_context_present(symbol, f"{row['title']} {row['raw_first_line']}")
    own = [row for row, lost in changed if {a["symbol"].removeprefix("XYZ-") for a in row["assets"]} & set(lost)]
    assert [row["title"][:12] for row in own] == [SHFE_MARGIN_NOTICE[:12]]
    assert own[0]["reviewer_verdict"] == "demote"


def test_a_commodity_tag_still_grounds_wherever_the_text_names_the_commodity(rows: list[dict[str, Any]]) -> None:
    kept = [
        (row, symbol)
        for row in rows
        for symbol in grounded_assets(row["title"], row["coins"], raw_first_line=row["raw_first_line"])
        if symbol.removeprefix("XYZ-") in COMMODITY_CONTEXT
    ]
    assert kept, "the day had commodity tags; a rule that removed all of them would pass every other check"
    for row, symbol in kept:
        assert commodity_context_present(symbol, f"{row['title']} {row['raw_first_line']}")


# ------------------------------------------------------------------ the post-model rule


def test_only_a_primary_the_shown_catalogue_contradicts_is_demoted(rows: list[dict[str, Any]]) -> None:
    hits = [(row, contradicted_primary_symbols(row["assets"], row["catalog_candidates"])) for row in rows]
    changed = [(row, symbols) for row, symbols in hits if symbols]
    assert [symbols for _, symbols in changed] == [("SILVER",), ("XYZ-COPPER",)]
    assert [row["reviewer_verdict"] for row, _ in changed] == ["demote", "demote"]
    assert [row["audit_case"] for row, _ in changed] == ["catalogue_class_contradiction"] * 2
    # Both are `single_name`, so the demotion reaches the reader as `single_name_without_instrument`.
    assert [row["scope"] for row, _ in changed] == ["single_name", "single_name"]
    # 135 primaries had exactly one candidate class on the day; 133 of them agreed with it.
    unambiguous = [
        row
        for row in rows
        for asset in row["assets"]
        if asset["role"] == "primary"
        and len(row["catalog_candidates"].get(asset["symbol"].removeprefix("XYZ-"), ())) == 1
    ]
    assert len(unambiguous) == 135


def test_the_rule_is_silent_where_the_catalogue_is_ambiguous_or_absent(rows: list[dict[str, Any]]) -> None:
    """Two candidate classes is a question for the text, and no candidate row is not a contradiction."""

    assert (
        contradicted_primary_symbols(
            [{"role": "primary", "symbol": "GOLD", "market_type": "equity"}], {"GOLD": ("commodity", "equity")}
        )
        == ()
    )
    assert contradicted_primary_symbols([{"role": "primary", "symbol": "LMT", "market_type": "equity"}], {}) == ()
    assert (
        contradicted_primary_symbols(
            [{"role": "mentioned", "symbol": "SILVER", "market_type": "unknown"}], {"SILVER": ("commodity",)}
        )
        == ()
    )
    # 33 primaries on the day are neither tagged nor spelled in their own text and are simply right.
    absent = [
        reading
        for row in rows
        for reading in _primaries(row)
        if reading.support == "unsupported" and not reading.in_catalogue
    ]
    assert {reading.symbol for reading in absent} >= {"0700.HK", "WUXIY", "TCE"}


def test_the_program_demotes_a_contradicted_primary_and_keeps_the_name() -> None:
    """End to end through the Program's own normalization: role changes, order and the name do not."""

    semantics = EventSemantics.model_validate(
        {
            "novelty": "new_fact",
            "restates": -1,
            "assets": [
                {"symbol": "SILVER", "role": "primary", "market_type": "unknown"},
                {"symbol": "XAG", "role": "mentioned", "market_type": "commodity"},
            ],
            "direction": "neutral",
            "scope": "single_name",
            "fact_kind": "state_change",
            "evidence_ref": "c1",
            "confidence": 0.9,
        }
    )
    demoted = _demote_contradicted_primaries(semantics, {"SILVER": ("commodity",)})
    assert [(asset.symbol, asset.role) for asset in demoted.assets] == [("SILVER", "mentioned"), ("XAG", "mentioned")]
    assert _demote_contradicted_primaries(semantics, {}) is semantics


# ------------------------------------------------------------------ the published signal


def test_support_names_how_the_event_carries_a_symbol() -> None:
    assert symbol_support("HD", text="$HD Home Depot reports Q2 adjusted EPS $4.92") == "cashtag"
    assert symbol_support("NEAR", text="Binance Will List NEAR Protocol") == "text"
    assert symbol_support("BA", text="BAE Systems completes Critical Design Review") == "unsupported"
    assert symbol_support("FIRE", text="Kakao Pay signs an agreement with Fireblocks") == "unsupported"
    assert symbol_support("CRCL", text="Circle launches Bitcoin-backed USDC borrowing") == "unsupported"
    assert symbol_support("CRCL", text="Circle launches ...", grounded=("CRCL", "XYZ-CRCL")) == "provider_tag"
    # Hong Kong is not in the catalogue and has no provider tag, so its own code is the only evidence
    # there is; all three spellings of one listing resolve (#504 PR-A).
    assert symbol_support("0700.HK", text="腾讯控股(00700.HK)盘中一度涨超7%") == "alias"
    assert symbol_support("02015.HK", text="理想汽车(02015.HK)发布Q3交付量") == "text"
    assert symbol_support("HK1810", text="小米(01810.HK)发布 MiMo-V2.6") == "alias"
    assert symbol_support("0700.HK", text="腾讯港股盘中一度涨超7%") == "unsupported"


def test_the_catalogue_reading_separates_absence_from_contradiction() -> None:
    proved = asset_grounding(
        "SILVER", market_type="unknown", text="Sunshine Silver", candidates={"SILVER": ("commodity",)}
    )
    assert (proved.in_catalogue, proved.catalogue_classes, proved.class_conflict) == (True, ("commodity",), True)
    ambiguous = asset_grounding(
        "GOLD", market_type="equity", text="i-80 Gold", candidates={"GOLD": ("commodity", "equity")}
    )
    assert (ambiguous.in_catalogue, ambiguous.class_conflict) == (True, False)
    unknown = asset_grounding("0700.HK", market_type="equity", text="腾讯", candidates={"BTC": ("crypto",)})
    assert (unknown.in_catalogue, unknown.catalogue_classes, unknown.class_conflict) == (False, (), False)


def test_the_grounding_signal_over_the_delivered_day_is_pinned(rows: list[dict[str, Any]]) -> None:
    support = {name: 0 for name in ("cashtag", "text", "alias", "provider_tag", "unsupported")}
    in_catalogue = 0
    for row in rows:
        for reading in _primaries(row):
            support[reading.support] += 1
            in_catalogue += int(reading.in_catalogue)
    assert support == {"cashtag": 10, "text": 101, "alias": 0, "provider_tag": 189, "unsupported": 50}
    assert in_catalogue == 298


def test_demoting_every_unsupported_primary_would_take_sixteen_keep_cards(rows: list[dict[str, Any]]) -> None:
    """The rule this PR does not ship, measured on the day it would have run.

    `unsupported` means only that nothing on the Event spells the symbol. For a ticker the model read off
    a company name -- which is what the seed asks it to do -- that is the normal state, so the class is a
    signal for a later decision row to weigh, not a demotion.
    """

    unsupported = [(row, reading) for row in rows for reading in _primaries(row) if reading.support == "unsupported"]
    assert len(unsupported) == 50
    keeps = {reading.symbol for row, reading in unsupported if row["reviewer_verdict"] == "keep"}
    assert len([1 for row, _ in unsupported if row["reviewer_verdict"] == "keep"]) == 16
    assert {"LMT", "ACN", "ALKS", "HK1810", "SOFTBANK"} <= keeps


def test_hong_kong_primaries_are_untouched_and_resolvable(rows: list[dict[str, Any]]) -> None:
    hk = [
        (row, reading)
        for row in rows
        for reading in _primaries(row)
        if ".HK" in reading.symbol.upper() or reading.symbol.upper().startswith("HK")
    ]
    assert len(hk) == 9
    assert all(not contradicted_primary_symbols(row["assets"], row["catalog_candidates"]) for row, _ in hk)
    supported = [reading for _, reading in hk if reading.support in {"text", "alias", "cashtag"}]
    assert len(supported) == 6
    # The three that are not: two 小米 cards written as 小米 with no code, and the 腾讯 card that names the
    # company in Chinese only. Nothing demotes them; the signal says the code is not in the text.
    assert sorted(reading.symbol for _, reading in hk if reading.support == "unsupported") == [
        "0700.HK",
        "HK1810",
        "HK1810",
    ]


def test_the_audit_cases_this_change_corrects_and_the_ones_it_cannot(rows: list[dict[str, Any]]) -> None:
    """The 27 hand-labelled mis-groundings, and which mechanism each one needs.

    Only `catalogue_class_contradiction` is separable with the evidence the pipeline holds. The provider
    tag families (`Walrus Pump` -> PUMP, `Funding Circle` -> CRCL, `Anterix` -> AAPL) and the model's own
    (`BAE` -> BA, `Muse` -> ANTHROPIC) are name-resolution errors: the frame carries no matched name and
    the catalogue carries no issuer name, so no reading here can see them. That is the finding, and this
    assertion is what will fail when an issuer-name column makes them visible.
    """

    outcome: dict[str, set[str]] = {}
    for row in rows:
        case = row["audit_case"]
        if not case:
            continue
        acted = set()
        if contradicted_primary_symbols(row["assets"], row["catalog_candidates"]):
            acted.add("primary_demoted")
        grounded_now = grounded_assets(row["title"], row["coins"], raw_first_line=row["raw_first_line"])
        if any(
            symbol.removeprefix("XYZ-") in COMMODITY_CONTEXT and symbol not in grounded_now
            for symbol in row["grounded_assets"]
        ):
            acted.add("tag_ungrounded")
        outcome.setdefault(case, set()).update(acted or {"unchanged"})
    assert outcome == {
        "catalogue_class_contradiction": {"primary_demoted"},
        "commodity_tag_without_commodity": {"tag_ungrounded"},
        # The eight remaining families are name-resolution errors nothing here can see.
        "commodity_of_a_company_story": {"unchanged"},
        "commodity_tag_on_miner": {"unchanged"},
        "model_invented_ticker": {"unchanged"},
        "model_private_proxy": {"unchanged"},
        "provider_tag_other_party": {"unchanged"},
        "provider_tag_word_collision": {"unchanged"},
        "provider_tag_wrong_issuer": {"unchanged"},
    }


def _decide_row(row: dict[str, Any], assets: list[dict[str, Any]]) -> tuple[str, str | None]:
    """One recorded card through the production decision, with the assets this test supplies."""

    judgment = scored_judgment(
        TriageVerdict(
            novelty=row["novelty"],
            restates=-1,
            assets=assets,
            direction=row["direction"],
            scope=row["scope"],
            fact_kind=proxy_fact_kind(row),
            evidence_ref="c1",
            confidence=0.8,
            headline_zh=row["headline_zh"][:60],
            why_zh="",
        ),
        taxonomy=news_taxonomy(
            event_family=row["event_family"],
            change_state=row["change_state"],
            assertion_status=row["assertion_status"],
        ),
        source_authority=row["source_authority"],
    )
    facts = GateFacts(
        grounded_assets=tuple(asset["symbol"] for asset in assets),
        watchlist_symbols=frozenset(),
        admission="listing_deterministic" if row["v14_override_rule"] == "listing_deterministic" else "candidate",
        independent_text_count=row["independent_text_count"],
        title=row["title"],
    )
    told = row["told_same_key_4h"]
    status = StorylineStatus(
        key=row["storyline_key"],
        told_directions=("neutral",) * told,
        told_assets=(frozenset(),) * told,
        told_keys=(row["storyline_key"],) * told,
        told_at_ms=tuple(NOW - (index + 1) * 60_000 for index in range(told)),
    )
    result = decide(judgment, facts, status, now_ms=NOW)
    return result.final, result.override_rule


def test_a_demoted_primary_reaches_the_reader_as_a_dropped_card(rows: list[dict[str, Any]]) -> None:
    """What the reader actually sees: the two cards stop being delivered, under the existing rule.

    Nothing new decides this. A `single_name` fact with no primary instrument names nothing the reader
    can act on, which is `single_name_without_instrument` (#504 PR-A); the demotion only stops that
    question being answered with an instrument the catalogue says is something else.
    """

    policy = {
        json.loads(line)["event_id"]: json.loads(line)
        for line in POLICY_FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    outcomes = []
    for row in rows:
        demoted = contradicted_primary_symbols(row["assets"], row["catalog_candidates"])
        if not demoted:
            continue
        source = policy[row["event_id"]]
        after = [
            {**asset, "role": "mentioned"} if asset["symbol"] in demoted and asset["role"] == "primary" else asset
            for asset in source["assets"]
        ]
        outcomes.append((_decide_row(source, source["assets"]), _decide_row(source, after)))
    assert outcomes == [
        (("push", "fact_kind_state_change"), ("drop", "single_name_without_instrument")),
        (("push", "fact_kind_state_change"), ("drop", "single_name_without_instrument")),
    ]


def test_the_timeline_publishes_the_reading_beside_the_assets() -> None:
    """The console reads the Event chain, so the block travels with the assets step or not at all."""

    reading = asset_grounding("SILVER", market_type="unknown", text="Sunshine Silver", candidates={})
    verdict = {
        "novelty": "new_fact",
        "direction": "neutral",
        "scope": "single_name",
        "fact_kind": "state_change",
        "evidence_ref": "c1",
        "confidence": 0.9,
        "assets": [{"symbol": "SILVER", "role": "mentioned", "market_type": "unknown"}],
        "headline_zh": "标题",
        "why_zh": "说明",
    }
    block = {"policy": "news_gate_grounding_v1", "assets": [reading.as_trace()]}
    _, steps = event_timeline(
        event={
            "event_id": "ev-1",
            "leader_title": "Sunshine Silver",
            "reporting_origin": "opennews",
            "dedupe_family": "general",
            "event_kind": "news",
            "opened_at_ms": 1,
            "member_count": 1,
            "admission": "candidate",
            "asset_class": "none",
            "grounded_assets": [],
            "watchlist_hits": [],
            "macro_lexicon": False,
            "storyline_key": "none",
            "published_at_ms": 1,
            "ingest_mode": "live",
            "provenance": [],
        },
        members=[],
        verdicts=[
            {
                "final_decision": "drop",
                "override_rule": "single_name_without_instrument",
                "throttled_by": None,
                "degraded": False,
                "error_code": None,
                "created_at_ms": 2,
                "stage": "triage",
                "verdict": verdict,
                "trace": {"grounding": block},
            }
        ],
        deliveries=[],
    )
    triage = next(step for step in steps if step["stage"] == "triage")
    assert triage["facts"]["grounding"] == block
    assert triage["facts"]["grounding"]["assets"][0] == {
        "symbol": "SILVER",
        "role": "primary",
        "market_type": "unknown",
        "support": "text",
        "in_catalogue": False,
        "catalogue_classes": [],
        "class_conflict": False,
    }
