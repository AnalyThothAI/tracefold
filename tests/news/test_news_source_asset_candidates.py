"""Derived candidate facts affect extraction, never the identity of already read sources."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.support.news_update_semantic import generated
from tracefold.news.adapters.extraction import DspyExtractor
from tracefold.news.storage.semantic_input import frozen_input
from tracefold.news.updates.projection import extraction_input, reading_view, reading_views


def material(text, symbols=("CL", "XYZ-CL")):
    return {
        "item_ids": ["source"],
        "items": [
            {
                "item_id": "source",
                "source_id": "opennews",
                "source_item_key": "source",
                "observed_at_ms": 100,
                "evidence_text": text,
                "provider_metadata": {
                    "coins": [{"symbol": symbol, "market_type": "cex", "grade": "C"} for symbol in symbols]
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "text",
    [
        "US 30-year Treasury yields reach their highest since 2002.",
        "Turkey's central bank cuts reserve requirements.",
        "Iran faces new sanctions amid turmoil in the Gulf.",
        "全球农产品价格创最大季度涨幅。",
        "威廉姆斯称美联储加息后不急于再动。",
        "韩国拟在美国投资 1200 亿美元建设核电站。",
        "标普500ETF国泰溢价超过 7%。",
    ],
)
def test_unrelated_oil_tags_are_not_candidates(text) -> None:
    source = frozen_input("event", material(text))
    assert source.asset_candidates[source.evidence[0].ref] == ()
    assert [tag.symbol for tag in source.source_asset_tags[source.evidence[0].ref]] == ["CL", "XYZ-CL"]


@pytest.mark.parametrize(
    "text",
    [
        "布伦特原油日内涨超 4%，现报 101.53 美元/桶。",
        "Oil rises.",
        "Crude rises.",
        "Brent jumps.",
        "WTI trades higher.",
        "OPEC cuts output.",
        "Prices per barrel rise.",
        "Two tankers enter the port.",
        "石油油价上涨。",
        "布油美油上涨。",
        "油轮穿越海峡。",
    ],
)
def test_named_oil_candidates_survive_without_a_grade_gate(text) -> None:
    source = frozen_input("event", material(text))
    assert [row.symbol for row in source.asset_candidates[source.evidence[0].ref]] == ["CL", "XYZ-CL"]


@pytest.mark.parametrize(
    ("symbol", "named"), [("XYZ-GOLD", "黄金"), ("XAG", "白银"), ("NATGAS", "天然气"), ("COPPER", "铜")]
)
def test_other_commodity_candidates_require_their_own_underlying(symbol, named) -> None:
    unrelated = frozen_input("event", material("央行宣布新的准备金率。", (symbol,)))
    assert unrelated.asset_candidates[unrelated.evidence[0].ref] == ()
    related = frozen_input("event", material(f"{named}价格上涨。", (symbol,)))
    assert related.asset_candidates[related.evidence[0].ref][0].symbol == symbol


def test_unknown_source_market_is_absent_from_the_actual_model_input(monkeypatch) -> None:
    data = material("NEAR network and Oracle report updates.", ("NEAR", "ORCL"))
    data["items"][0]["provider_metadata"]["coins"][1]["market_type"] = "equity"
    data["listed_markets"] = {"NEAR": ("crypto",), "ORCL": ("crypto", "equity")}
    source = frozen_input("event", data)
    calls = generated(monkeypatch, {"claims": []})
    asyncio.run(DspyExtractor(lambda: None, model_identity="test", topics={}).extract(source))
    sent = json.loads(calls[0]["evidence_json"])
    assert sent["asset_candidates"]["e1"] == [
        {"symbol": "NEAR", "grade": "C", "listed_markets": ["crypto"]},
        {"symbol": "ORCL", "grade": "C", "market_type": "equity", "listed_markets": ["crypto", "equity"]},
    ]
    assert "source_asset_tags" not in sent
    assert source.asset_candidates[source.evidence[0].ref][0].market_type == "unknown"


def test_filter_and_catalogue_changes_keep_old_read_refs_and_only_new_member_is_read(monkeypatch) -> None:
    data = material("US Treasury yields rise.", ("CL", "NEAR"))
    source = frozen_input("event", data)
    evidence = source.evidence[0]
    read_ref = reading_views(source)[0].read_ref
    # The pre-788 material has exactly the raw three-field tags and the same projection version.
    assert read_ref == reading_view("event", evidence, (), source.source_asset_tags[evidence.ref]).read_ref
    data["listed_markets"] = {"CL": ("commodity",), "NEAR": ("crypto",)}
    listed = frozen_input("event", data)
    assert listed.input_sha != source.input_sha
    assert reading_views(listed)[0].read_ref == read_ref
    monkeypatch.setattr("tracefold.news.storage.semantic_input.commodity_context_present", lambda *_: True)
    unfiltered = frozen_input("event", data)
    assert unfiltered.input_sha != listed.input_sha
    assert reading_views(unfiltered)[0].read_ref == read_ref
    assert len(extraction_input(unfiltered)["asset_candidates"][evidence.ref]) == 2
    data["work"] = {"wanted_revision": 2, "lineage_id": "lineage", "processed_read_refs": [read_ref]}
    assert frozen_input("event", data).evidence == ()
    data["item_ids"].append("new")
    data["items"].append(
        {
            "item_id": "new",
            "source_id": "opennews",
            "source_item_key": "new",
            "observed_at_ms": 200,
            "evidence_text": "NEAR withdrawals resume.",
        }
    )
    pending = frozen_input("event", data)
    assert [row.source.record_id for row in pending.evidence] == ["new"]


def test_candidate_filtering_uses_each_source_body_and_not_its_sibling() -> None:
    data = material("Treasury yields rise.")
    oil = material("Brent crude rises.")["items"][0]
    oil.update(item_id="oil", source_item_key="oil")
    data["item_ids"].append("oil")
    data["items"].append(oil)
    source = frozen_input("event", data)
    treasury, crude = source.evidence
    assert source.asset_candidates[treasury.ref] == ()
    assert [row.symbol for row in source.asset_candidates[crude.ref]] == ["CL", "XYZ-CL"]


def test_all_filtered_tags_still_preserve_the_completed_read_identity(monkeypatch) -> None:
    data = material("Treasury yields rise.")
    filtered = frozen_input("event", data)
    monkeypatch.setattr("tracefold.news.storage.semantic_input.commodity_context_present", lambda *_: True)
    original = frozen_input("event", data)
    assert filtered.input_sha != original.input_sha
    read_ref = reading_views(original)[0].read_ref
    assert reading_views(filtered)[0].read_ref == read_ref
    monkeypatch.undo()
    data["work"] = {"wanted_revision": 2, "lineage_id": "lineage", "processed_read_refs": [read_ref]}
    assert frozen_input("event", data).evidence == ()


def test_ordinary_word_collisions_remain_candidates_for_model_relevance_reading() -> None:
    symbols = ("ACT", "BRIDGE", "GPU", "OPENAI", "AAVE", "ONDO", "SPACEX", "POLYMARKET")
    source = frozen_input("event", material('OpenAI discusses a "bridge round" and GPU rentals.', symbols))
    assert [row.symbol for row in source.asset_candidates[source.evidence[0].ref]] == list(symbols)
