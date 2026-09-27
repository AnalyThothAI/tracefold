"""Replay the recorded 24 h delivered ledger against the #675 PR-3 grounding rules.

`tests/fixtures/news/grounding_replay_24h.jsonl` is one row per card the reader received between
2026-09-21 07:10 and 2026-09-22 07:10 UTC: 417 of them, carrying the leader frame the Gate saw (title,
first line, provider coins), the frozen evidence text, the assets the model named, the catalogue candidate
rows it was shown, and the label an independent reviewer gave the card. `audit_case` names the 27 rows the
Issue's grounding audit called out by hand.

The Gate's commodity-context rule runs against it (the contradicted-primary demotion left with the retired
Program in #706), and so does what the rules deliberately *do not* do. A rule that demoted
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
from tracefold.news.timeline import event_timeline

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "grounding_replay_24h.jsonl"
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
