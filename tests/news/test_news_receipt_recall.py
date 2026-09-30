"""Deterministic claim-scoped selection of exact sent receipt bodies."""

from __future__ import annotations

import pytest

from tests.support.news_recall_window import (
    PROBE_NOISE,
    PROBE_RECEIPT,
    PROBE_STATEMENT,
    PROBE_STATEMENT_ZH,
    WINDOW_RECEIPTS,
    gold_fixture,
    gold_window_filler,
    probe_window,
)
from tests.support.news_update_semantic import draft, material
from tracefold.news.entities import asset_retrieval_symbols
from tracefold.news.notifications import recall as receipt_recall
from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt, ReaderNovelty, reader_novelty
from tracefold.news.notifications.recall import (
    RecallCandidate,
    RouteEvidence,
    lexical_evidence,
    query_for_claim,
    reader_context_revision,
    select_for_claim,
)
from tracefold.news.updates.assembly import assemble_update
from tracefold.news.updates.contracts import Asset, Claim, Extraction, FrozenInput, IdentityHint
from tracefold.news.updates.identity import digest

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
    assert asset_retrieval_symbols("$OKLO", "crypto") == asset_retrieval_symbols(" oklo ", "crypto") == {"OKLO"}
    assert asset_retrieval_symbols("xyz:GOLD", "commodity") >= {"GOLD"} and "GOLD" in asset_retrieval_symbols(
        "spot gold", "commodity"
    )
    assert "CL" in asset_retrieval_symbols("WTI", "commodity") & asset_retrieval_symbols("oil", "commodity")
    # A commodity word names the commodity only on a commodity asset.
    assert asset_retrieval_symbols("Silver", "equity") == {"SILVER"} and asset_retrieval_symbols("白银", "equity") == {
        "白银"
    }
    oklo = claim("Oklo shares jump", asset="OKLO", market="equity")
    listed = claim("Oklo token listed", asset="$OKLO", market="crypto")
    assert select(oklo, [receipt("listing", "代币上线", STAMP - 1, listed)]).intent_ids == ()


def test_sql_lexical_rank_is_preserved_by_final_fusion() -> None:
    current = claim("Agency approves project")
    candidates = (
        receipt("recent", "Agency approves project", STAMP - 1),
        receipt("ranked-first", "Agency approves project", STAMP - 2),
    )
    terms = ("agency", "approves", "project")
    selected = select_for_claim(
        query_for_claim(current),
        ReaderNovelty(novelty="unlinked"),
        candidates,
        as_of_ms=STAMP,
        routes={
            "recent": RouteEvidence(lexical_rank=2, lexical_terms=terms),
            "ranked-first": RouteEvidence(lexical_rank=1, lexical_terms=terms),
        },
    )
    assert selected.intent_ids == ("ranked-first", "recent")


def test_aster_pair_and_cross_language_issuer_features_recall_without_asserting_same_fact() -> None:
    # Frozen source case: Aster/SIUSDT vs Aster DEX/$SI. Neither text proves the same venue contract.
    current = claim("Aster DEX上线$SI，最大5倍杠杆", asset="$SI", market="crypto", subject="Aster DEX")
    previous = claim("Aster lists SIUSDT perpetual", asset="SIUSDT", market="crypto", subject="Aster")
    novelty = ReaderNovelty(novelty="unlinked")
    assert select(current, [receipt("aster", "此前永续上线报道", STAMP - 1, previous)], novelty=novelty).intent_ids == (
        "aster",
    )
    assert novelty.novelty == "unlinked"
    # A source/Claim asset tag is usable as a related query feature across languages; no actor dictionary or
    # stronger subject_id is fabricated from a mentioned asset.
    chinese = claim("英伟达发布下一代产品", asset="NVDA", market="equity", subject="英伟达")
    english = claim("Nvidia introduces a different chip", asset="$NVDA", market="equity", subject="Nvidia")
    assert select(chinese, [receipt("issuer", "芯片报道", STAMP - 2, english)]).intent_ids == ("issuer",)
    assert not chinese.known_identity and not english.known_identity


def test_unresolved_chain_addresses_remain_case_sensitive_retrieval_features() -> None:
    first = claim("Current update", asset="solana:AbCdEFGh123456789", market="crypto", subject="Current actor")
    other = claim("Earlier account", asset="solana:abcdefgh123456789", market="crypto", subject="Earlier actor")
    assert select(first, [receipt("different-address", "Earlier", STAMP - 1, other)]).intent_ids == ()


def test_a_lexical_rank_without_two_qualifying_terms_is_not_evidence() -> None:
    current = claim("Agency approves project")
    selected = select_for_claim(
        query_for_claim(current),
        ReaderNovelty(novelty="unlinked"),
        (receipt("noise", "Agency notice", STAMP - 1),),
        as_of_ms=STAMP,
        routes={"noise": RouteEvidence(lexical_rank=1, lexical_terms=("agency",))},
    )
    assert selected.intent_ids == ()


def test_only_terms_rare_in_the_window_are_lexical_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A House probe claim shares "market" and "trading" (市场, 交易) with unrelated receipts and rare terms only
    with the receipt about the probe; the rare ones decide, in English and in Han bigrams."""

    window = tuple(receipt(key, text, at) for key, _, text, at in probe_window(STAMP))
    for statement in (PROBE_STATEMENT, PROBE_STATEMENT_ZH):
        query = query_for_claim(claim(statement, subject="House Oversight Committee"))
        assert set(lexical_evidence(query, window)) == {PROBE_RECEIPT}
        selected = select_for_claim(query, ReaderNovelty(novelty="unlinked"), window, as_of_ms=STAMP)
        assert selected.intent_ids == (PROBE_RECEIPT,)
    english = lexical_evidence(query_for_claim(claim(PROBE_STATEMENT)), window)[PROBE_RECEIPT][1]
    assert {"hyperliquid", "oversight", "probe"} <= set(english) and not {"market", "trading", "the"} & set(english)
    # Without the document-frequency rule the common words alone would have let every noise receipt in.
    monkeypatch.setattr(receipt_recall, "LEXICAL_DF_MAX", 1.0)
    everything = lexical_evidence(query_for_claim(claim(PROBE_STATEMENT)), window)
    assert set(PROBE_NOISE) - {"kyiv-zh", "iran-zh"} <= set(everything)


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
        )

    assert revision((a, b)) == revision((a, b))
    assert revision((a, b)) != revision((b, a), selected=selection.__class__(("b", "a"), ()))
    assert revision((a, b)) != revision((receipt("a", "changed", STAMP - 1), b))
    assert revision((a, b)) != revision((a, b), state="ambiguous")


def test_issue_750_gold_frozen_production_recall() -> None:
    """The frozen production receipts of the #750 gold case inside a production-shaped window, pure routes.

    Filler receipts complete the 1,321-receipt window so "prices", "week", "gold" and "low" are as common as they
    were in production; none of them is then rare enough to be evidence, so copper and bitcoin receipts that
    share them stay out and the gold history comes from the asset route and the semantic links.
    `tests/integration/test_news_event_update_store.py` runs the same window through PostgreSQL.
    """

    fixture = gold_fixture()
    candidates = tuple(
        RecallCandidate(
            intent_id=row["intent_id"],
            body=row["body"],
            payload_sha256=row["payload_sha256"],
            settled_at_ms=row["settled_at_ms"],
            claims=tuple(Claim.model_validate(item) for item in row["claims"]),
        )
        for row in fixture["candidates"]
    ) + tuple(receipt(intent, body, at) for intent, body, at in gold_window_filler(fixture))
    assert len(candidates) == WINDOW_RECEIPTS
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
    labels = fixture["labels"]
    assert len(gold_selection.intent_ids) <= 16
    assert {key for key, label in labels.items() if label == 2} <= selected
    assert not selected & {key for key, label in labels.items() if label == 0}
    assert not selected & {"0b4d11", "177218", "641052", "d903d3"}
    assert data_selection.intent_ids == ()
    assert len(fixture["baseline_message_intents"][gold.ref]) == 16
    assert len(fixture["baseline_message_intents"][data.ref]) == 16
