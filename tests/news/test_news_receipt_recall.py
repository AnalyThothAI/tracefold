"""Deterministic claim-scoped selection of exact sent receipt bodies."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support.news_update_semantic import draft, material
from tracefold.news.updates.contracts import Asset, Claim, Extraction, FrozenInput, IdentityHint
from tracefold.news.updates.identity import digest
from tracefold.news.updates.reader_judgments import ClaimLink, LinkedReceipt, ReaderNovelty, reader_novelty
from tracefold.news.updates.receipt_recall import (
    RecallCandidate,
    asset_symbols,
    query_for_claim,
    reader_context_revision,
    select_for_claim,
)
from tracefold.news.updates.semantics import assemble_update

STAMP = 1_790_405_000_000


def claim(statement: str, *, asset: str | None = None, market: str = "commodity", subject: str | None = None):
    evidence = material(statement)
    original = draft(evidence)
    fields = original.fields.model_copy(
        update={
            "subject": subject or asset or statement,
            "object": "",
            "assets": () if asset is None else (Asset(symbol=asset, market_type=market, role="primary"),),
        }
    )
    update = assemble_update(
        FrozenInput(event_id=digest(statement), revision=1, lineage_id=digest(statement), evidence=(evidence,)),
        Extraction(claims=(original.model_copy(update={"statement": statement, "fields": fields}),)),
        None,
        adopted_at_ms=STAMP,
    )
    assert update is not None
    return update.claims[0]


def receipt(intent: str, body: str, at: int, *claims) -> RecallCandidate:
    return RecallCandidate(intent, digest(body), body, at, claims)


def select(current, candidates, *, novelty=None):
    return select_for_claim(
        query_for_claim(current), novelty or ReaderNovelty(novelty="unlinked"), tuple(candidates), as_of_ms=STAMP
    )


def test_gold_history_including_chinese_copy_survives_unrelated_trigram_noise() -> None:
    gold = claim("Gold rebounds from a seven-week low", asset="XAU")
    old = claim("Gold fell sharply to 4113.79", asset="XAU")
    noise = claim("OKLO listed on HTX", asset="OKLO", market="crypto")
    candidates = [
        receipt("gold-1", "黄金跌至4113.79", STAMP - 2000, old),
        receipt("gold-2", "国际现货黄金站上4140", STAMP - 1000, old),
        receipt("htx", "OKLO上线HTX，new found in htx", STAMP - 500, noise),
        receipt("politics", "政治新闻提及价格", STAMP - 100, noise),
    ]
    chosen = select(gold, candidates)
    assert chosen.intent_ids == ("gold-2", "gold-1")


def test_sibling_claim_and_short_query_do_not_inherit_gold_history() -> None:
    gold = claim("Gold rebounds from a seven-week low", asset="XAU")
    data = claim("US data in focus")
    old = claim("Gold fell to a seven-week low", asset="XAU")
    candidates = [receipt("gold", "黄金跌至七周低点", STAMP - 1, old), receipt("htx", "new found in htx", STAMP - 2)]
    assert select(gold, candidates).intent_ids == ("gold",)
    assert select(data, candidates).intent_ids == ()


def test_silver_history_is_recalled_across_spellings_and_languages() -> None:
    silver = claim("Silver climbs back above 50 dollars", asset="Silver")
    candidates = [
        receipt("xag", "银价回升", STAMP - 1000, claim("XAG rebounds", asset="XAG")),
        receipt("baiyin", "白银跌至一个月低点", STAMP - 2000, claim("白银跌至一个月低点", asset="白银")),
        receipt("spot", "现货白银下跌", STAMP - 3000, claim("现货白银下跌", asset="现货白银")),
        receipt("gold", "国际现货黄金站上4140", STAMP - 500, claim("国际现货黄金站上4140", asset="国际现货黄金")),
        receipt(
            "miner",
            "银矿股下跌",
            STAMP - 400,
            claim("银矿股下跌", asset="Silver", market="equity", subject="Silver miners"),
        ),
    ]
    chosen = select(silver, candidates)
    assert chosen.intent_ids == ("xag", "baiyin", "spot")
    assert all("primary_asset:commodity:SILVER" in reasons for _, reasons in chosen.reasons)


def test_asset_spelling_is_canonical_but_market_type_still_separates() -> None:
    assert asset_symbols("$OKLO", "crypto") == asset_symbols(" oklo ", "crypto") == {"OKLO"}
    assert asset_symbols("xyz:GOLD", "commodity") >= {"GOLD"} and "GOLD" in asset_symbols("spot gold", "commodity")
    assert asset_symbols("WTI", "commodity") == asset_symbols("oil", "commodity") == {"CL"}
    # A commodity word names the commodity only on a commodity asset.
    assert asset_symbols("Silver", "equity") == {"SILVER"} and asset_symbols("白银", "equity") == {"白银"}
    oklo = claim("Oklo shares jump", asset="OKLO", market="equity")
    listed = claim("Oklo token listed", asset="$OKLO", market="crypto")
    assert select(oklo, [receipt("listing", "代币上线", STAMP - 1, listed)]).intent_ids == ()


def test_sql_lexical_rank_is_preserved_by_final_fusion() -> None:
    current = claim("Agency approves project")
    candidates = (
        receipt("recent", "Agency approves project", STAMP - 1),
        receipt("ranked-first", "Agency approves project", STAMP - 2),
    )
    selected = select_for_claim(
        query_for_claim(current),
        ReaderNovelty(novelty="unlinked"),
        candidates,
        as_of_ms=STAMP,
        route_ranks={"recent": (None, 2), "ranked-first": (None, 1)},
    )
    assert selected.intent_ids == ("ranked-first", "recent")


def test_sql_stemming_alone_cannot_bypass_shared_content_rule() -> None:
    current = claim("Agency approves project")
    selected = select_for_claim(
        query_for_claim(current),
        ReaderNovelty(novelty="unlinked"),
        (receipt("noise", "unrelated notice", STAMP - 1),),
        as_of_ms=STAMP,
        route_ranks={"noise": (None, 1)},
    )
    assert selected.intent_ids == ()


def test_grounded_subject_identity_recalls_without_topic_or_text_collision() -> None:
    current = claim("Issuer announces an acquisition")
    previous = claim("Business sets a later closing date")
    hint = IdentityHint(key="subject_id", value="lei:123", evidence_ref="ev:fixture", surface="issuer")
    current = current.model_copy(update={"known_identity": (hint,)})
    previous = previous.model_copy(update={"known_identity": (hint,)})
    selected = select(current, [receipt("identity", "收购交割时间更新", STAMP - 1, previous)])
    assert selected.intent_ids == ("identity",)
    assert "identity:subject_id:lei:123" in selected.reasons[0][1]


def test_linked_representative_precedes_routes_and_old_window_is_not_general_recall() -> None:
    current = claim("Agency approves project")
    old = claim("Agency approves project")
    candidates = [
        receipt("linked", "真实已送正文", STAMP - 60 * 60 * 1000 * 50, old),
        receipt("recent", "Agency approves project", STAMP - 1, old),
        receipt("future", "Agency approves project", STAMP + 1, old),
    ]
    novelty = ReaderNovelty(novelty="increment", linked_intents=("linked",))
    assert select(current, candidates, novelty=novelty).intent_ids == ("linked", "recent")


def test_context_revision_tracks_order_body_and_semantic_state() -> None:
    current = claim("Agency approves project")
    a = receipt("a", "first body", STAMP - 1)
    b = receipt("b", "second body", STAMP - 2)
    novelty = ReaderNovelty(novelty="increment", linked_intents=("a", "b"))
    selection = select(current, (a, b), novelty=novelty)

    def revision(candidates, selected=selection, state="sent"):
        return reader_context_revision(
            "update:1",
            {current.ref: selected},
            {current.ref: novelty},
            tuple(candidates),
            (LinkedReceipt(intent_id="a", state=state, claim_refs=(current.ref,), settled_at_ms=STAMP - 1),),
            blocked=(),
            ambiguous=(),
            invalidated=(),
            watch_symbols=(),
        )

    assert revision((a, b)) == revision((a, b))
    assert revision((a, b)) != revision((b, a), selected=selection.__class__(("b", "a"), ()))
    assert revision((a, b)) != revision((receipt("a", "changed", STAMP - 1), b))
    assert revision((a, b)) != revision((a, b), state="ambiguous")


def test_issue_750_gold_frozen_production_recall() -> None:
    """The frozen production candidates of the #750 gold case through the pure routes.

    `tests/integration/test_news_event_update_store.py` seeds the same receipts into PostgreSQL and selects
    through the real SQL routes; both keep the same direct antecedents.
    """

    fixture = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/news/issue_750_gold_recall.json").read_text("utf-8")
    )
    candidates = tuple(
        RecallCandidate(
            intent_id=row["intent_id"],
            body=row["body"],
            payload_sha256=row["payload_sha256"],
            settled_at_ms=row["settled_at_ms"],
            claims=tuple(Claim.model_validate(item) for item in row["claims"]),
        )
        for row in fixture["candidates"]
    )
    links = tuple(ClaimLink.model_validate(item) for item in fixture["links"])
    receipts = tuple(LinkedReceipt.model_validate(item) for item in fixture["link_receipts"])
    gold, data = (Claim.model_validate(item) for item in fixture["claims"])
    gold_selection = select_for_claim(
        query_for_claim(gold), reader_novelty(gold.ref, links, receipts), candidates, as_of_ms=fixture["as_of_ms"]
    )
    data_selection = select_for_claim(
        query_for_claim(data), reader_novelty(data.ref, links, receipts), candidates, as_of_ms=fixture["as_of_ms"]
    )
    selected = {intent[7:13] for intent in gold_selection.intent_ids}
    assert len(gold_selection.intent_ids) == 16
    # With function words as the only stop words, copper receipts sharing "prices" and "low" take the last
    # place from 585497, the oldest-ranked structural gold match (see the PostgreSQL test).
    assert {"235d95", "191d10", "585497", "eca8e3"} - selected == {"585497"}
    assert not selected & {"4e5846", "96059b", "077119", "96b2f0", "ce1b55", "cdae0a"}
    assert data_selection.intent_ids == ()
    assert len(fixture["baseline_message_intents"][gold.ref]) == 16
    assert len(fixture["baseline_message_intents"][data.ref]) == 16
