"""Pure-module tests for News V3: titles, gate, storyline, rules, minhash, delivery, bus."""

from __future__ import annotations

import json
import random
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.support.news_update_cards import adopted, asset, copy_for, draft, frozen_card, nvda_update, plan_for
from tests.support.news_update_cards import source as update_source
from tracefold.news.bus import BusDecodeError, BusMessage, decode_body
from tracefold.news.card_format import CHANGE_BASIS_LABEL
from tracefold.news.delivery import (
    news_update_card,
    reader_market_movements,
    reader_trade_targets,
    update_card_assets,
    update_change_label,
)
from tracefold.news.eval.replay import replay_hits
from tracefold.news.events.facts import FactUnit, extract_fact_units
from tracefold.news.events.gate import GateInput, evaluate_gate, gate_lexicon_flags, grounded_assets
from tracefold.news.events.minhash import BANDS, band_keys, minhash_signature
from tracefold.news.events.storyline import (
    NO_STORYLINE_KEY,
    STORYLINE_REGISTRY_SHA256,
    STORYLINE_REGISTRY_VERSION,
    StorylineRegistry,
    final_storyline_key,
    load_storyline_registry,
    match_storyline,
    preliminary_storyline_key,
    registry_storyline_key,
    symbol_in_text,
)
from tracefold.news.events.titles import extract_title
from tracefold.news.events.tokens import comparison_tokens, jaccard
from tracefold.news.feishu_card import feishu_card
from tracefold.news.market_review.pricing import CHANGE_BASIS_ZH
from tracefold.news.models import (
    MarketAsset,
    ReaderMarketMovement,
    ReaderReceipt,
    ReaderTradeTarget,
    TelegramDeliveryReceipt,
)
from tracefold.news.opennews import source_artifact_identity
from tracefold.news.outcome import storyline_key_zh
from tracefold.news.pipeline.admission import _event_identity
from tracefold.news.reader_card import quote_line, reader_quotes
from tracefold.news.updates.notification import freeze_card
from tracefold.news.updates.price_basis import price_move_basis

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"


def _hits() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_current_event_identity_never_reuses_the_pre_cut_item_primary_key() -> None:
    item_id = "a" * 64
    fact = FactUnit(
        fact_id="b" * 64,
        ordinal=0,
        text="current fact",
        context="",
        span_start=0,
        span_end=12,
        method="whole_item",
    )

    news_id = _event_identity(item_id=item_id, fact=fact, kind="news")
    assert news_id != item_id
    assert news_id == _event_identity(item_id=item_id, fact=fact, kind="news")
    # The OI ledger keeps deriving its published source identity from the same formula under its own
    # kind (#553 §3.3), so an Item that produced both never collides with itself.
    assert news_id != _event_identity(item_id=item_id, fact=fact, kind="oi")


# ---------------------------------------------------------------- titles
def test_extract_title_skips_url_only_and_label_lines() -> None:
    t = extract_title(
        "reply https://www.theblock.co/news/defi/2026-08-17-tokenized-equities-triple-market-share-411996"
    )
    assert t.url_slug is True
    assert "tokenized equities" in t.title
    t2 = extract_title(
        "Binance Announcement:<br/>Binance Will Support the Conflux Network (CFX) Network Upgrade"
        " &amp; Hard Fork - 2026-08-24<br/>$CFX"
    )
    assert t2.title.startswith("Binance Will Support the Conflux")
    t3 = extract_title(
        "国内新闻：<br/>1. 李强主持召开国务院第十二次全体会议强调，努力完成全年经济社会发展目标任务。"
        "<br/>2. 宇树科技：将于8月19日在科创板上市。"
    )
    assert t3.title.startswith("1. 李强")


def test_extract_title_strips_corpus_prefixes_and_suffixes() -> None:
    assert (
        extract_title("THE BLOCK: Robinhood Chain TVL surges 45% in August").title
        == "Robinhood Chain TVL surges 45% in August"
    )
    assert (
        extract_title("$NVDA - NVIDIA TO INVEST $100BN FOR OPENAI DATA CENTRE IN OHIO - FT").title
        == "NVIDIA TO INVEST $100BN FOR OPENAI DATA CENTRE IN OHIO"
    )
    assert extract_title("quote: Aave V4 active loans are ATH. Still early.").title.startswith("Aave V4 active loans")
    a = extract_title("Binance: Binance Will Support the Conflux Network (CFX) Network Upgrade")
    b = extract_title("Binance Will Support the Conflux Network (CFX) Network Upgrade")
    assert a.comparison == b.comparison


def test_extract_title_keeps_exchange_and_handle_subjects() -> None:
    """Exchange names and @handles are subjects, not source labels: v1 turned Kraken's launch into 'launches ...'."""

    kraken = extract_title(
        "JUST IN: @Krakenfx launches commission-free trading of 7,000+ U.S. stocks for eligible customers in Europe"
    )
    assert kraken.title.startswith("Krakenfx launches commission-free trading")
    assert extract_title(".@binance launches bStocks").title == "binance launches bStocks"
    assert extract_title("Binance: Notice on the Delisting of XYZ").title == "Binance: Notice on the Delisting of XYZ"
    assert extract_title("OKX: OKX Wallet launches new feature").title == "OKX Wallet launches new feature"
    assert (
        extract_title("Coinbase - Coinbase to acquire Deribit for $2.9B").title
        == "Coinbase to acquire Deribit for $2.9B"
    )
    assert (
        extract_title("The $DLUSD volume from @deel has exceeded $115M").title
        == "The $DLUSD volume from deel has exceeded $115M"
    )


def test_fact_units_split_only_explicit_sequential_numbered_digests() -> None:
    raw = (
        "市场快讯：<br/>1. 商务部反对欧方打压中国企业。<br/>"
        "2. Moderna 盘前下跌 13%，公司下调全年指引。<br/>"
        "3. 沃尔玛上调全年销售预期至 4.8%。"
    )
    units = extract_fact_units(item_id="item-1", raw_text=raw, fallback_title="市场快讯")
    assert [u.ordinal for u in units] == [0, 1, 2]
    assert [u.method for u in units] == ["explicit_numbered"] * 3
    assert units[0].text.startswith("商务部") and units[1].text.startswith("Moderna")
    assert all(u.context == "市场快讯：" for u in units)
    assert len({u.fact_id for u in units}) == 3

    # Two bullets are not enough evidence to manufacture two Events; neither
    # are broken numbers or prose that merely contains a number.
    for uncertain in (
        "1. 第一条足够长的事实。<br/>2. 第二条足够长的事实。",
        "1. 第一条足够长的事实。<br/>3. 第三条足够长的事实。<br/>4. 第四条足够长的事实。",
        "Revenue rose 3.2% while costs fell 1.1%.",
    ):
        whole = extract_fact_units(item_id="item-2", raw_text=uncertain, fallback_title="原标题")
        assert len(whole) == 1 and whole[0].method == "whole_item" and whole[0].text == "原标题"


def test_fact_unit_context_is_the_lead_above_the_list_not_the_first_block() -> None:
    """#152: the block directly above the list is what gives every bullet its subject.

    The shape is a real one: a quote tweet whose own slogan is the first block, the provider's bare ``|``
    separator, and only then the quoted wire lead.  Taking the *first* unnumbered block handed the model
    "The AI race is moving down the stack." and dropped Nvidia, OpenAI and Ohio entirely.
    """

    raw = (
        "quote: The AI race is moving down the stack.\r\n"
        "Machine-native capital markets are coming.\r\n"
        "|\r\n"
        "BREAKING: Nvidia, $NVDA, has agreed to provide a more than $100 billion backstop for a massive new "
        "OpenAI data center in Ohio, per FT. Details include:\r\n"
        '1. Nvidia will provide credit support for the "land, power and shell" capped at $105 billion\r\n'
        "2. The data center is being developed alongside a SoftBank-led energy company\r\n"
        "3. Nvidia will also invest $1.5 billion into SB Energy\r\n"
        "4. OpenAI plans to lease as much as 8 gigawatts at the data center in Pike County, Ohio"
    )
    units = extract_fact_units(item_id="item-3", raw_text=raw, fallback_title="fallback")
    assert len(units) == 4
    context = units[0].context
    assert all(u.context == context for u in units)
    assert "OpenAI data center in Ohio" in context and "per FT" in context
    assert context.startswith("quote: The AI race")
    assert " | " not in context and not context.endswith("|")


_BULLETS = (
    "1. 第一条内容足够长的具体事实描述。\r\n2. 第二条内容足够长的具体事实描述。\r\n3. 第三条内容足够长的具体事实描述。"
)


def test_fact_unit_context_keeps_the_lead_when_the_preamble_overflows() -> None:
    """A long preamble is budgeted from the bottom up: the poster's framing is what gets dropped."""

    filler = "x" * 400
    raw = f"{filler}\r\n{filler}\r\nWire lead that names the subject:\r\n{_BULLETS}"
    units = extract_fact_units(item_id="item-4", raw_text=raw, fallback_title="fallback")
    assert len(units) == 3
    assert units[0].context.endswith("Wire lead that names the subject:")
    assert len(units[0].context) <= 600
    assert units[0].context.count(filler) == 1


def test_fact_units_never_read_a_clock_time_as_a_numbered_item() -> None:
    """A 财经日程 lists consecutive hours, so `10:30 / 11:00 / 12:00` used to parse as items 10, 11, 12 —
    sequential, three of them — and split the calendar into Events whose titles had lost their hour."""

    for calendar in (
        "今日财经日程：\r\n10:30 中国8月社会消费品零售总额同比公布\r\n"
        "11:00 欧元区工业产出月率数据公布\r\n12:00 美国至9月API原油库存变动数据公布",
        # Leading zeros parse as 1, 2, 3 — a `numbers[0] == 1` guard would not have caught this one.
        "财经日历\r\n01:30 美联储主席鲍威尔在杰克逊霍尔发表主旨演讲\r\n"
        "02:00 美国至9月API原油库存变动数据公布\r\n03:00 新西兰联储公布利率决议与政策声明",
    ):
        units = extract_fact_units(item_id="item-6", raw_text=calendar, fallback_title="财经日程")
        assert len(units) == 1 and units[0].method == "whole_item"

    # A real numbered digest that merely mentions a time still splits.
    mixed = (
        "市场快讯：\r\n1. 商务部反对欧方打压中国企业并要求纠正。\r\n"
        "2. Moderna 将于 10:30 公布下调后的全年指引。\r\n3. 沃尔玛上调全年销售预期至 4.8%。"
    )
    assert len(extract_fact_units(item_id="item-7", raw_text=mixed, fallback_title="市场快讯")) == 3


def test_fact_unit_context_is_empty_when_the_digest_has_no_preamble() -> None:
    """A bare jin10 list has no lead, and the first bullet is *not* one: it is a different fact."""

    first_bullet = _BULLETS.split("\r\n", maxsplit=1)[0]
    units = extract_fact_units(item_id="item-5", raw_text=_BULLETS, fallback_title=first_bullet)
    assert len(units) == 3
    assert all(u.context == "" for u in units)


def test_a_delivery_receipt_has_no_delete_lifecycle_left_to_record() -> None:
    """#604 N3: `deleted_at_ms` was written by nothing and read by one dead predicate.

    #562 §5 row 5 removed the path that could delete a card -- `deleteMessage` is not even on the
    adapter's method allowlist -- and the receipt field outlived it. The model forbids extras, so a
    historical receipt carrying one would now be refused; none exists, because nothing ever wrote one.
    The `news_deliveries` delete columns, their CHECKs and the partial index stay where they are, and
    `ReaderReceipt` still answers a `delete_state` row, because those are storage an operator can
    still read.
    """

    live = TelegramDeliveryReceipt.model_validate(
        {"provider": "telegram", "message_id": 42, "pushed_at_ms": 1_700_000_000_000, "target_sha256": "a" * 64}
    )
    assert "deleted_at_ms" not in live.canonical()
    assert not hasattr(live, "deleted_at_ms")
    with pytest.raises(ValueError):
        TelegramDeliveryReceipt.model_validate({**live.canonical(), "deleted_at_ms": 1_700_000_001_000})


def test_reader_receipt_never_confuses_decision_or_ambiguous_send_with_received() -> None:
    assert ReaderReceipt.from_delivery(None).state == "not_received"
    assert ReaderReceipt.from_delivery({"state": "sending"}).state == "not_received"
    assert (
        ReaderReceipt.from_delivery({"state": "terminal", "error_code": "delivery_unavailable"}).state == "not_received"
    )
    ambiguous = ReaderReceipt.from_delivery(
        {"state": "terminal", "error_code": "ambiguous_after_crash", "card": {"header": {"x": 1}}}
    )
    assert ambiguous.state == "unknown" and ambiguous.rendered_card == {"header": {"x": 1}}
    sent = ReaderReceipt.from_delivery(
        {"state": "sent", "settled_at_ms": 123, "card": {"header": {"title": {"content": "实际卡片"}}}}
    )
    assert sent.state == "received" and sent.received_at_ms == 123 and sent.rendered_card is not None
    deleted = ReaderReceipt.from_delivery(
        {
            "state": "sent",
            "settled_at_ms": 123,
            "delete_state": "deleted",
            "card": {"header": {"title": {"content": "已删除卡片"}}},
        }
    )
    assert deleted.state == "not_received" and deleted.rendered_card is None


# ---------------------------------------------------------------- tokens / minhash
def test_minhash_bands_agree_for_near_duplicates_and_differ_for_unrelated() -> None:
    a = comparison_tokens(
        extract_title("Trump threatens to bomb Oman if it 'gets in the way' of US-Iran negotiations").comparison
    )
    b = comparison_tokens(
        extract_title("Trump threatens to bomb Oman if it gets in the way over Iran issue").comparison
    )
    c = comparison_tokens(extract_title("Copper surges toward record on LME as scramble for supply builds").comparison)
    assert jaccard(a, b) >= 0.5
    ka, kb, kc = band_keys(minhash_signature(a)), band_keys(minhash_signature(b)), band_keys(minhash_signature(c))
    assert len(ka) == BANDS
    assert any(x == y for x, y in zip(ka, kb, strict=True))
    assert not any(x == y for x, y in zip(ka, kc, strict=True))


# ---------------------------------------------------------------- gate
def test_gate_grounds_provider_grades_and_cashtags_without_a_name_table() -> None:
    coins = (
        {"symbol": "CL", "grade": "A+"},
        {"symbol": "XYZ-CL", "grade": "A+"},
        {"symbol": "NEAR", "grade": "A"},
        {"symbol": "OPENAI", "grade": "A"},
    )
    assert grounded_assets("China conducts suspected marine research within Japan's EEZ", coins) == ()
    assert grounded_assets("Vessel struck by unknown projectile in Strait of Hormuz", coins) == ("CL", "XYZ-CL")
    # The provider already resolved the name: Bitcoin -> BTC:A, Home Depot -> HD:A, SafePal -> SFP:A.
    assert grounded_assets(
        "Citi to launch digital asset custody, starting with Bitcoin", ({"symbol": "BTC", "grade": "A"},)
    ) == ("BTC",)
    assert grounded_assets(
        "Home Depot Shares Up 3% Premarket After Q2 Sales Beat", ({"symbol": "HD", "grade": "A"},)
    ) == ("HD",)
    # B+ counts; C and ungraded do not — unless the ticker is a literal cashtag.
    assert grounded_assets("Cardano Plans Two-Phase Dijkstra Upgrade", ({"symbol": "ADA", "grade": "B+"},)) == ("ADA",)
    assert grounded_assets(
        "Bitcoin scores a rare win", ({"symbol": "RARE", "grade": "C"}, {"symbol": "BTC", "grade": "A"})
    ) == ("BTC",)
    assert grounded_assets("$HD Home Depot reports Q2 adjusted EPS $4.92", ({"symbol": "HD", "grade": "C"},)) == ("HD",)
    # Cashtags stripped from the normalized title are still visible through the raw first line.
    assert grounded_assets(
        "NVIDIA TO INVEST $100BN FOR OPENAI DATA CENTRE",
        ({"symbol": "NVDA", "grade": "C"},),
        raw_first_line="$NVDA - NVIDIA TO INVEST $100BN FOR OPENAI DATA CENTRE - FT",
    ) == ("NVDA",)
    # English-word tags never ground.
    assert (
        grounded_assets(
            "Nvidia backs OpenAI data center near Ohio",
            ({"symbol": "OPENAI", "grade": "A+"}, {"symbol": "NEAR", "grade": "A"}),
        )
        == ()
    )


def test_market_telemetry_without_a_provider_score_is_held_back() -> None:
    """#126: a missing score is `0.0`, and the old `and score` guard read that as "skip this rule".

    It never mattered while an allowlist decided which Strategies reached the Gate. Without one, an unscored
    market frame would otherwise be admitted, cost a Triage call, and could reach a reader.
    """

    base = dict(coins=(), ingest_mode="live", watchlist_symbols=frozenset())
    unscored = evaluate_gate(
        GateInput(title="BTC open interest +3.4% in 3 minutes", engine_type="market", provider_score=None, **base)
    )
    assert unscored.admission == "suppressed_low_signal"
    assert "market_telemetry_below_min_score" in unscored.reasons

    # A market frame the provider does rate highly is still ordinary work.
    scored = evaluate_gate(
        GateInput(title="BTC open interest +3.4% in 3 minutes", engine_type="market", provider_score=85.0, **base)
    )
    assert scored.admission == "candidate"


def test_gate_admission_rules() -> None:
    base = dict(provider_score=75.0, coins=(), ingest_mode="live", watchlist_symbols=frozenset({"BTC"}))
    # Ungrounded titles are candidates by default: the model, not a lexicon, decides relevance.
    meme = evaluate_gate(GateInput(title="Imagine being this guy", engine_type="meme", **base))
    assert meme.admission == "candidate" and meme.asset_class == "none"
    assert (
        evaluate_gate(
            GateInput(title="Russia downs 180 drones in Moscow region overnight", engine_type="news", **base)
        ).admission
        == "candidate"
    )
    eu = evaluate_gate(
        GateInput(
            title="European Union Rules Enable Regulatory Authorities to Block Third-Country Crypto Exchanges",
            engine_type="news",
            **base,
        )
    )
    assert eu.admission == "candidate"
    # #504 D7 deleted the Gate low-signal switch (never on, zero admissions in the whole retained history): a
    # low-score ungrounded social post is a candidate like any other; the model, not a score, decides relevance.
    low = evaluate_gate(
        GateInput(title="Imagine being this guy", engine_type="meme", **{**base, "provider_score": 60.0})
    )
    assert low.admission == "candidate" and "ungrounded_social_below_min_score" not in low.reasons
    macro = evaluate_gate(
        GateInput(title="U.S. 30-Year Treasury Yield Climbs to 5.32%, Highest Since 2007", engine_type="news", **base)
    )
    assert macro.admission == "candidate" and macro.asset_class == "macro" and macro.queue_priority == "high"
    housing = evaluate_gate(GateInput(title="TABLE-U.S. July housing starts fall 12.4%", engine_type="news", **base))
    assert housing.asset_class == "macro"
    # Law-firm templates are vetoed even when the provider grounded the ticker; real class-action news is not.
    pr = evaluate_gate(
        GateInput(
            title="Exelixis (EXEL) Securities Investigation Notice - Levi & Korsinsky",
            engine_type="news",
            **{**base, "coins": ({"symbol": "EXEL", "grade": "A"},)},
        )
    )
    assert pr.admission == "suppressed_pr_template" and pr.pr_template
    lawsuit = evaluate_gate(
        GateInput(
            title="Tesla faces class action over Autopilot claims",
            engine_type="news",
            **{**base, "coins": ({"symbol": "TSLA", "grade": "A"},)},
        )
    )
    assert lawsuit.admission == "candidate"
    listing = evaluate_gate(GateInput(title="Bybit will list LYTE", engine_type="listing", **base))
    assert listing.admission == "listing_deterministic" and listing.queue_priority == "high"
    recovery = evaluate_gate(
        GateInput(
            title="Bitcoin ETF inflows hit record",
            engine_type="news",
            **{**base, "ingest_mode": "recovery", "coins": ({"symbol": "BTC", "grade": "A+"},)},
        )
    )
    assert recovery.admission == "recovery"
    watch = evaluate_gate(
        GateInput(
            title="Bitcoin breaks $120k as ETF inflows surge",
            engine_type="news",
            **{**base, "coins": ({"symbol": "BTC", "grade": "A"},)},
        )
    )
    assert watch.admission == "candidate" and watch.watchlist_hits == ("BTC",) and watch.queue_priority == "high"
    # #509 PR-2: the energy context a bare `CL` tag needs is `gate.energy_context` on the storyline registry, so
    # a tanker attack in the Strait grounds crude through `tanker` / `hormuz` / `iran` rather than through a
    # regex kept next to this policy.
    hormuz = evaluate_gate(
        GateInput(
            title="Iran attacks tanker outside Strait of Hormuz",
            engine_type="news",
            **{**base, "coins": ({"symbol": "CL"}, {"symbol": "XYZ-CL"})},
        )
    )
    assert hormuz.energy_lexicon and hormuz.grounded_assets == ("CL", "XYZ-CL")
    assert hormuz.asset_class == "equity_or_commodity"
    # A hurricane over the Gulf is not energy context by itself: `gulf` and `mexico` are the v3 traps the
    # registry refuses, and v5's bare `energy` went with them. The subject has to be named.
    gulf = evaluate_gate(
        GateInput(
            title="Hurricane shuts Gulf of Mexico platforms",
            engine_type="news",
            **{**base, "coins": ({"symbol": "CL"},)},
        )
    )
    assert not gulf.energy_lexicon and gulf.grounded_assets == () and gulf.asset_class == "none"
    rigs = evaluate_gate(
        GateInput(
            title="Hurricane shuts Gulf of Mexico oil platforms",
            engine_type="news",
            **{**base, "coins": ({"symbol": "CL"},)},
        )
    )
    assert rigs.energy_lexicon and rigs.grounded_assets == ("CL",)
    # Central banks are `gate.macro` but only the Fed and the rates topic are `gate.queue_high`: v5 had no
    # entry for the RBNZ at all, and its un-bounded `rate|fed` high-priority pattern promoted anything.
    rbnz = evaluate_gate(
        GateInput(title="Reserve Bank of New Zealand Sets Official Cash Rate at 2.75%", engine_type="news", **base)
    )
    assert rbnz.macro_lexicon and rbnz.asset_class == "macro" and rbnz.queue_priority == "normal"
    powell = evaluate_gate(
        GateInput(title="Fed's Powell says policy is well positioned for now", engine_type="news", **base)
    )
    assert powell.macro_lexicon and powell.asset_class == "macro" and powell.queue_priority == "high"
    # v5 read `treasury` and the `rate` inside "accelerate" here, and filed a corporate raise as high-priority
    # macro. `bitcoin treasury` is the longer alias and it belongs to a crypto topic that carries no Gate flag.
    treasury_company = evaluate_gate(
        GateInput(
            title="Capital B raises $8.8M from Adam Back to accelerate its bitcoin treasury",
            engine_type="news",
            **base,
        )
    )
    assert not treasury_company.macro_lexicon and treasury_company.queue_priority == "normal"


def test_gate_lexicon_flags_are_registry_data_with_one_owner() -> None:
    """#509 D3: `gate.py` keeps no word list; `energy` / `macro` / `queue_high` are flags on registry rows.

    The v5 Gate and the v3 storyline lexicon were two vocabularies for the same words and they disagreed: the
    Gate knew `iran` but not `iranian` or 沙特, `pboc` but no other central bank outside a bare 央行, and its
    high-priority pattern had no word boundaries, so "accelerate" and "corporate" were rate news. One list now
    answers both questions, and the three things that could still go wrong are asserted here."""

    from tracefold.news.events import gate as gate_module

    assert not [
        name
        for name in ("ENERGY_LEXICON", "MACRO_LEXICON", "GATE_LEXICON_VERSION", "_HIGH_PRIORITY_MACRO")
        if hasattr(gate_module, name)
    ]
    flags = {entry.id: entry.gate for entry in load_storyline_registry().entries if entry.gate is not None}
    energy = {name for name, gate in flags.items() if gate.energy_context}
    macro = {name for name, gate in flags.items() if gate.macro}
    queue_high = {name for name, gate in flags.items() if gate.queue_high}
    # `evaluate_gate` drops v5's `macro and <high-priority pattern>` conjunction, which is only correct while
    # every queue_high row is also a macro row.
    assert queue_high <= macro and queue_high == {"fed", "rates"}
    assert energy == {"energy", "hormuz", "iran", "iraq", "kuwait", "oman", "qatar", "saudi", "uae", "yemen"}
    assert macro == {
        "boc",
        "boe",
        "boj",
        "bok",
        "cbr",
        "china_macro",
        "ecb",
        "fed",
        "fx",
        "macro_data",
        "pboc",
        "rba",
        "rbnz",
        "rates",
        "trade",
    }
    # Coverage v5 did not have (the #509 P1 words): every central bank, the Gulf states in Chinese, and the
    # inflected forms a word-boundary regex missed.
    assert gate_lexicon_flags("Bank of Canada mulls tariff shock as Macklem readies rate decision").macro
    assert gate_lexicon_flags("ADP employment change misses estimates").macro
    assert gate_lexicon_flags("ТАСС: ЦБ РФ снизил ключевую ставку").macro
    assert gate_lexicon_flags("沙特重返国际债市，发行美元计价伊斯兰债券。").energy
    assert gate_lexicon_flags("U.S. Military Attacked Two Iranian Government Tankers").energy
    assert gate_lexicon_flags("US can swap Venezuela barrels to refill SPR").energy
    # Background vocabulary v5 counted as a subject. A company called Energy is not the energy market, a
    # sales pipeline is not a pipeline, and a bare 央行 is not a central bank taking a decision (PR-1).
    assert not gate_lexicon_flags("Eos Energy Shares Up 14.8% Premarket").energy
    assert not gate_lexicon_flags("9% chance Trump renames the strait.").energy
    assert not gate_lexicon_flags("HPE's pipeline remains multiples of its backlog").energy
    assert gate_lexicon_flags("Ukrainian drones hit the Druzhba oil pipeline").energy
    assert not gate_lexicon_flags("施罗德投资上调黄金评级，认为央行强力购金构成结构性支撑").macro


# ---------------------------------------------------------------- storyline
def _prelim(title: str) -> str:
    return preliminary_storyline_key(title=title, strong_assets=(), asset_class="macro", dedupe_family="general")


def test_storyline_registry_is_literal_data_with_one_owner_per_alias() -> None:
    """#509 D1/五: the registry is data, so the three things that make it data are asserted, not reviewed.

    An alias belongs to exactly one entry (there is no priority rule to get wrong), carries no regex syntax (a
    row cannot smuggle in `.*`), and is already NFKC-case-folded (matching normalizes the *text*, so an alias
    that is not in that form would silently never match). `members` name entries that exist."""

    registry = load_storyline_registry()
    assert registry.version == STORYLINE_REGISTRY_VERSION == "news_storyline_registry_v1"
    assert len(STORYLINE_REGISTRY_SHA256) == 64 and set(STORYLINE_REGISTRY_SHA256) <= set("0123456789abcdef")

    owner: dict[str, str] = {}
    ids = {entry.id for entry in registry.entries}
    for entry in registry.entries:
        assert entry.label_zh.strip()
        for _script, alias in entry.aliases.all():
            assert alias not in owner, f"{alias!r} is claimed by both {owner[alias]} and {entry.id}"
            owner[alias] = entry.id
            assert not set(alias) & set("[]()|*+?{}\\^$"), alias
            assert unicodedata.normalize("NFKC", alias).casefold() == alias, alias
        assert set(entry.members) <= ids
        assert entry.kind == "conflict" or not (entry.members or entry.active)
    assert {entry.id for entry in registry.entries if entry.kind == "conflict" and entry.active} == {
        "mideast_2026",
        "ru_ua",
    }
    # `standalone` is opt-out and rare enough to name: an entry that may never be a key on its own.
    assert {entry.id for entry in registry.entries if not entry.standalone} == {"us"}
    # A conflict is a grouping over participants that exist on their own, never a matcher: `hormuz`,
    # `lebanon` and `mideast` are `geo` rows, so setting a war inactive stops the merge without deleting
    # the coverage underneath it.
    assert not [entry.id for entry in registry.entries if entry.kind == "conflict" and entry.aliases.all()]
    mideast = next(entry for entry in registry.entries if entry.id == "mideast_2026")
    assert {"hormuz", "lebanon", "mideast", "iran", "yemen"} <= set(mideast.members)
    # The single-word traps the v3 regexes fell into: none of them may become an alias again.
    for trap in ("联储", "央行", "gulf", "strait", "期货", "mexico"):
        assert trap not in owner
    # Two more substring traps the 09-01/09-02 titles found. A bare `国务院` read 美国国务院 (the US State
    # Department) as China, and Russian `газ` matched Газета — a newspaper, not natural gas.
    assert owner["中国国务院"] == "china" and "国务院" not in owner
    assert {"газа", "газо", "газпром", "природного газа"} <= set(owner) and "газ" not in owner


def test_storyline_registry_rejects_a_row_that_is_not_data() -> None:
    """Structure is enforced at load, not by review: a shared alias, a pattern, or a dangling member fails."""

    base = {
        "version": "news_storyline_registry_v1",
        "entries": [
            {"id": "iran", "kind": "geo", "label_zh": "伊朗", "aliases": {"latin": ["iran"]}},
            {"id": "war", "kind": "conflict", "label_zh": "战争", "active": True, "members": ["iran"]},
        ],
    }
    assert StorylineRegistry.model_validate(base).entries[0].id == "iran"
    for broken in (
        {"entries": [base["entries"][0], {**base["entries"][1], "members": ["nowhere"]}]},
        {"entries": [base["entries"][0], {**base["entries"][0], "id": "iran2"}]},
        {"entries": [{**base["entries"][0], "aliases": {"latin": ["ira.*"]}}]},
        {"entries": [{**base["entries"][0], "aliases": {"latin": ["Iran"]}}]},
        {"entries": [{**base["entries"][0], "kind": "topic", "members": ["iran"]}]},
        {"entries": [{**base["entries"][0], "surprise": 1}]},
        # A conflict owns no aliases, so it is never a hit and a Gate flag on it is data nothing can read.
        {"entries": [base["entries"][0], {**base["entries"][1], "gate": {"macro": True}}]},
        # `evaluate_gate` reads `queue_high` alone, which is only the v5 rule while queue_high implies macro.
        {"entries": [{**base["entries"][0], "gate": {"queue_high": True}}]},
    ):
        with pytest.raises(ValueError):
            StorylineRegistry.model_validate(base | broken)


def test_storyline_key_is_composed_by_rank_not_by_the_order_of_the_file() -> None:
    """#509 D2. The v3 lexicon decided 96 of 1036 pushed cards by which regex sat higher in a tuple. The rank
    is now fixed — asset, conflict, actor, geo, topic — and the tie-break inside a rank is the earliest
    mention, so the storyline is a property of the headline instead of a property of the file."""

    # A conflict collects its participants: on a war day the product wants one line for the war.
    assert _prelim("Iran attacks Kuwait") == "conflict:mideast_2026"
    assert _prelim("Iran attacked another ship outside the Strait of Hormuz") == "conflict:mideast_2026"
    # Two active conflicts in one headline: the one named first wins, whatever order the file is in.
    assert _prelim("Russia helps Iran build missiles") == "conflict:ru_ua"
    assert _prelim("Iran receives Russian missile parts") == "conflict:mideast_2026"
    # An institution outranks the country it sits in, and the instrument it sets.
    assert _prelim("Bank of Canada holds policy rate at 2.75%") == "actor:boc"
    assert _prelim("Fed's Powell says the committee is in no hurry to cut") == "actor:fed"
    assert _prelim("RBNZ Sets Official Cash Rate at 3.25%, Signals Further Easing") == "actor:rbnz"
    assert _prelim("新西兰联储加息") == "actor:rbnz"  # a bare `联储` is not the Fed
    assert _prelim("澳洲联储主席布洛克：不排除再次加息") == "actor:rba"
    assert _prelim("中国央行开展 3000 亿元 MLF 操作") == "actor:pboc"  # not `geo:china`
    # A place outranks a subject (#509 D2 step 4 before step 5), and a subject is the last resort.
    assert _prelim("Canada's tariff retaliation takes effect") == "geo:canada"
    assert _prelim("US 30-year yield hits 5.32%") == "topic:rates"
    assert _prelim("Chevron restarts Venezuela joint venture output") == "geo:venezuela"
    # The v3 false positives are gone: `\bstrait\b` took the Taiwan Strait to the Middle East, a bare `gulf`
    # took the Gulf of Mexico there, and `期货` filed Chinese methanol futures under US equities.
    assert _prelim("Taiwan Strait transit draws PLA response") == "geo:taiwan"
    assert _prelim("Hurricane shuts Gulf of Mexico platforms") == NO_STORYLINE_KEY
    assert _prelim("【期货热点追踪】甲醇涨停") == NO_STORYLINE_KEY
    assert _prelim("Fedex raises guidance") == NO_STORYLINE_KEY  # word boundaries, not substrings
    assert _prelim("Tanker traffic in the Persian Gulf halts") == "topic:energy"


def test_a_us_dateline_is_matched_but_never_becomes_the_key_on_its_own() -> None:
    """#509: `standalone: false`. "The United States" is not a storyline for this reader.

    Giving `美国` / `washington` / `u.s.` their own key put CPI, jobless claims and housing starts — unrelated
    prints that merely share a dateline — into one hourly budget, which is the coarse bucket of #509 P3 under a
    new name. The entry still matches, so it still owns its aliases and still counts toward a conflict's members;
    it just cannot be the answer by itself."""

    # The subject wins over the dateline, whichever surface form the dateline takes.
    assert _prelim("US housing starts fall 12.4%") == "topic:macro_data"
    assert _prelim("U.S. housing starts fall 12.4%") == "topic:macro_data"
    assert _prelim("美国7月营建许可总数 144.3万户") == "topic:macro_data"
    assert _prelim("U.S. crude production hits a record") == "topic:energy"
    assert _prelim("White House announces new tariffs on Brazil") == "geo:brazil"
    # A headline whose only registry hit is the dateline has no storyline.
    assert _prelim("Washington shutdown enters day 3") == NO_STORYLINE_KEY
    assert [hit.entry_id for hit in match_storyline("Washington shutdown enters day 3")] == ["us"]
    # ... but the hit is still evidence for a conflict that names the country, and a war still wins.
    assert _prelim("US strikes Iran nuclear site") == "conflict:mideast_2026"


def test_preliminary_key_does_not_let_an_unverified_provider_tag_take_a_geopolitical_headline() -> None:
    """#509: the preliminary rank drops the final key's first step, and this is why.

    A provider tag is an *affected* asset until Triage names a primary, and every Middle East headline in the
    recall corpus carries a BTC tag. Letting the tag win before Triage keyed a war headline `asset:BTC`, and the
    told ledger's exact-storyline tier then answered a war card with Bitcoin cards. After Triage the model has
    named its primary against the Gate's grounding, so the asset goes back on top."""

    title = "Iran attacked another ship outside the Strait of Hormuz this morning"
    assert (
        preliminary_storyline_key(
            title=title,
            strong_assets=("BTC", "CL", "XYZ-CL"),
            asset_class="equity_or_commodity",
            dedupe_family="general",
        )
        == "conflict:mideast_2026"
    )
    assert (
        final_storyline_key(
            title=title,
            headline_zh="伊朗在霍尔木兹海峡外袭击另一艘船只",
            scope="single_name",
            verdict_primaries=[MarketAsset("BTC")],
            grounded_assets=["BTC", "CL", "XYZ-CL"],
            dedupe_family="general",
        )
        == "asset:BTC"
    )
    # A strong tag still opens a preliminary storyline when the registry has nothing to say about the title.
    assert (
        preliminary_storyline_key(
            title="Home Depot Shares Up 3% Premarket",
            strong_assets=("HD",),
            asset_class="equity_or_commodity",
            dedupe_family="general",
        )
        == "asset:HD"
    )
    # ... and `CL` is never its own storyline, preliminary or final.
    assert (
        preliminary_storyline_key(
            title="Refinery outage in Rotterdam",
            strong_assets=("CL",),
            asset_class="equity_or_commodity",
            dedupe_family="general",
        )
        == "topic:energy"
    )


def test_storyline_key_reads_the_scripts_the_desk_actually_receives() -> None:
    """#509 D1. TASS, Fars and Israeli channels contributed 109 pushes a day that all fell to one fallback
    bucket. Non-Latin aliases match as substrings, so an inflected form still lands on its entry."""

    assert _prelim("Минобороны России: ВСУ потеряли за сутки до 1200 военнослужащих") == "conflict:ru_ua"
    assert _prelim("Poland scrambles jets in response to Russian strikes on Ukraine") == "conflict:ru_ua"
    assert _prelim("ایران: حمله به پایگاه آمریکا در قطر") == "conflict:mideast_2026"
    assert _prelim("ישראל תקפה מטרות בתימן") == "conflict:mideast_2026"
    assert _prelim("Иран нанёс удар по базе США") == "conflict:mideast_2026"
    # The longest alias at a position wins, so the central bank is not read as the state at war.
    assert _prelim("ТАСС: ЦБ РФ повысил ключевую ставку") == "actor:cbr"
    assert _prelim("日本央行维持利率不变") == "actor:boj"


def test_storyline_key_does_not_depend_on_the_order_of_the_registry_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """#509 五: shuffling the file must not move a key. Composition is the only thing that reads the registry,
    so re-deriving every key above against eight shuffles is the whole order-independence proof."""

    from tracefold.news.events import storyline as module

    registry = load_storyline_registry()
    cases: dict[str, str | None] = {
        "Iran attacks Kuwait": "conflict:mideast_2026",
        "Russia helps Iran build missiles": "conflict:ru_ua",
        "Bank of Canada holds policy rate at 2.75%": "actor:boc",
        "Canada's tariff retaliation takes effect": "geo:canada",
        "US 30-year yield hits 5.32%": "topic:rates",
        "ТАСС: ЦБ РФ повысил ключевую ставку": "actor:cbr",
        "Hurricane shuts Gulf of Mexico platforms": None,
    }
    assert {title: registry_storyline_key(title) for title in cases} == cases

    try:
        for seed in range(8):
            shuffled = list(registry.entries)
            random.Random(seed).shuffle(shuffled)
            reordered = StorylineRegistry.model_validate(
                {"version": registry.version, "entries": [entry.model_dump(mode="json") for entry in shuffled]}
            )
            monkeypatch.setattr(module, "load_storyline_registry", lambda bound=reordered: bound)
            module._matchers.cache_clear()
            module._entry_index.cache_clear()
            assert {title: registry_storyline_key(title) for title in cases} == cases
    finally:
        monkeypatch.undo()
        module._matchers.cache_clear()
        module._entry_index.cache_clear()
    assert {title: registry_storyline_key(title) for title in cases} == cases


def test_storyline_keys_follow_the_verdict_before_the_registry() -> None:
    """#509 D2 steps 1, 6, 7 and 8: a grounded primary is the storyline, then the registry, then the model's
    own symbol-shaped primary (#100), then a grounded tag the text actually names, then `none`."""

    assert (
        final_storyline_key(
            title="Nvidia to invest $100bn",
            headline_zh="",
            scope="single_name",
            verdict_primaries=[MarketAsset("NVDA")],
            grounded_assets=["NVDA"],
            dedupe_family="general",
        )
        == "asset:NVDA"
    )
    # A BTC market wrap that mentions oil is BTC's storyline once Triage names BTC as primary.
    assert (
        final_storyline_key(
            title="Bitcoin pauses at $64,000 as rising yields, oil drag equities lower",
            headline_zh="",
            scope="sector",
            verdict_primaries=[MarketAsset("BTC")],
            grounded_assets=["BTC", "CL", "XYZ-CL"],
            dedupe_family="general",
        )
        == "asset:BTC"
    )
    # A primary the Gate did not ground cannot open its own storyline (verify only trusts code facts).
    assert (
        final_storyline_key(
            title="FOMC minutes tomorrow",
            headline_zh="",
            scope="macro",
            verdict_primaries=[MarketAsset("BTC")],
            grounded_assets=[],
            dedupe_family="general",
        )
        == "actor:fed"
    )
    # Bitcoin treasury companies are their own subject; the composed key still follows the verdict first.
    assert (
        final_storyline_key(
            title="Hyperscale Data Bitcoin Treasury at 276 Bitcoin",
            headline_zh="",
            scope="macro",
            verdict_primaries=[],
            grounded_assets=[],
            dedupe_family="general",
        )
        == "topic:crypto_treasury"
    )
    # #509 P4: an exchange-qualified primary is exactly as groupable as `NVDA`. It used to fail the symbol
    # shape and send every Hong Kong and German single name to the fallback bucket.
    for symbol in ("02015.HK", "DTE.DE"):
        assert (
            final_storyline_key(
                title="Company reports half-year results",
                headline_zh="",
                scope="single_name",
                verdict_primaries=[MarketAsset(symbol)],
                grounded_assets=[],
                dedupe_family="general",
            )
            == f"asset:{symbol}"
        )
    # Nothing anywhere: the key is `none`, and the dedupe family stays a column instead of becoming a bucket.
    assert (
        final_storyline_key(
            title="Local official visits a factory",
            headline_zh="",
            scope="macro",
            verdict_primaries=[],
            grounded_assets=[],
            dedupe_family="general",
        )
        == NO_STORYLINE_KEY
    )


def test_storyline_labels_come_from_the_registry() -> None:
    """#509 D4: one table of Chinese storyline names, and it is the registry."""

    assert storyline_key_zh("conflict:mideast_2026") == "美伊冲突"
    assert storyline_key_zh("actor:rbnz") == "新西兰联储" and storyline_key_zh("topic:rates") == "利率与通胀数据"
    assert storyline_key_zh(NO_STORYLINE_KEY) == "无线索"
    assert storyline_key_zh("asset:02015.HK") == "02015.HK"
    # A key whose entry the registry no longer has renders as itself rather than as a wrong label.
    assert storyline_key_zh("geo:atlantis") == "geo:atlantis"


def test_match_storyline_reports_every_hit_with_its_position() -> None:
    """The composition above is the only consumer, but the hits are the auditable primitive underneath it."""

    hits = match_storyline("Fed's Powell on oil: Iran and Kuwait")
    assert [(hit.entry_id, hit.kind) for hit in hits] == [
        ("fed", "actor"),
        ("fed", "actor"),
        ("energy", "topic"),
        ("iran", "geo"),
        ("kuwait", "geo"),
    ]
    assert [hit.start for hit in hits] == sorted(hit.start for hit in hits)
    assert match_storyline("") == ()


def test_source_artifact_identity_survives_the_provider_url_spellings() -> None:
    """#154: 17 of 29 repeat ingests in a 30-day window differed only in URL spelling.

    `_article_url` lowercases the host but not the path, so `x.com/CoinDesk/...` and `twitter.com/coindesk/...`
    are different strings for the same tweet. The status id is the platform's own primary key.
    """

    spellings = (
        "https://x.com/soon_svm/status/2089994673804939740",
        "https://twitter.com/soon_svm/status/2089994673804939740",
        "https://X.com/SOON_SVM/status/2089994673804939740",
        "https://www.twitter.com/soon_svm/statuses/2089994673804939740",
        "https://x.com/soon_svm/status/2089994673804939740?s=20",
    )
    identities = {source_artifact_identity(url) for url in spellings}
    assert len(identities) == 1
    artifact_id, published_at_ms = identities.pop()
    assert artifact_id == "x:2089994673804939740"
    # Snowflake: the tweet itself, 1.7 s before the provider pushed it at 1787128536804.
    assert published_at_ms == 1787128535115

    for other in ("https://www.zerohedge.com/markets/story", "https://x.com/soon_svm", "", "not a url"):
        assert source_artifact_identity(other) == ("", None)


_NOW = 1_800_000_000_000


def test_price_move_basis_reads_both_languages_of_every_shape_the_audit_named() -> None:
    """#675 §3. The first cut of this vocabulary was Chinese-only and wrongly dropped 11 of 14 English or
    variant cards, so the shapes are asserted on the exact strings the 24 h audit produced."""

    for admissible in (
        "比特币站上 85000 美元",
        "美国原油跌回每桶 100 美元下方",
        "英伟达失守 100 美元关口",
        "Bitcoin rises above $82,000",
        "Gold reclaims $4,000 an ounce",
        "Silver falls below $50",
        "黄金创三个月以来最大单日涨幅",
        "创 7 月 30 日以来最大盘中涨幅",
        "Meta shares hit a seven-month high",
        "Copper at its highest since January",
        "S&P 500 posts its biggest daily gain of the year",
        "四小时内超 10 亿美元空头被清算",
        "增持 7400 万美元 ETH",
        "提取 10,000 枚 ETH",
        "SOL 持仓升至 816 万枚",
        "Bitcoin ETFs saw $999 million of inflows",
        "USDe 跌至 0.92 美元",
        "Tether depegs to $0.97",
        "VLCC 日租金升至 103.5 万美元",
    ):
        assert price_move_basis(admissible), admissible

    for quote_only in (
        "Spot Palladium Rises Nearly 3% to $1,328.68/Oz",
        "Shares of Samsung Electronics Rise Over 3%",
        "Meta 股价涨幅扩大，最新上涨 10.4%",
        "腾讯港股盘中涨超 7%",
        "费城半导体指数涨幅扩大至 4%",
    ):
        assert not price_move_basis(quote_only), quote_only
    assert not price_move_basis("")


_NOW = 1_800_000_000_000


def test_mideast_storyline_requires_real_strait_or_mideast_context() -> None:
    # "STRAITS" was matching the unbounded substring ``strait``, so a crypto liquidation wrap was Middle East
    # news; the registry only knows the two straits that are storylines. Guyana's oil is `topic:energy`, which
    # is what it is — the point is that it is not the Middle East.
    assert (
        preliminary_storyline_key(
            title="STRAITS: Crypto surge causes $2.7bn liquidations",
            strong_assets=("BTC",),
            asset_class="crypto",
            dedupe_family="market_telemetry",
        )
        == "asset:BTC"
    )
    assert (
        preliminary_storyline_key(
            title="Exxon starts production at new Guyana oil FPSO",
            strong_assets=("XOM",),
            asset_class="equity_or_commodity",
            dedupe_family="general",
        )
        == "topic:energy"
    )
    assert (
        preliminary_storyline_key(
            title="Tanker struck in Strait of Hormuz, Brent supply risk rises",
            strong_assets=("CL",),
            asset_class="equity_or_commodity",
            dedupe_family="general",
        )
        == "conflict:mideast_2026"
    )


# ---------------------------------------------------------------- delivery / bus
def _update_card(update: Any = None, *, key: bool = False, **kwargs: Any) -> dict[str, Any]:
    update = update or nvda_update()
    plan = plan_for(update, key=key)
    return feishu_card(news_update_card(frozen_card(plan, update), plan=plan, update=update, **kwargs))


def _body(card: dict[str, Any]) -> str:
    return str(card["elements"][0]["text"]["content"])


def test_card_is_the_reader_contract() -> None:
    card = _update_card()
    # header = the frozen headline; body = the frozen claim lines + the code-owned facts line; nothing else.
    assert card["header"]["title"]["content"] == "英伟达向数据中心投资千亿美元"
    assert card["header"]["template"] == "grey"
    # The frozen copy is shown literally, never as Feishu markdown.
    assert card["elements"][0]["tag"] == "div" and card["elements"][0]["text"]["tag"] == "plain_text"
    assert _body(card).splitlines() == ["第1条：英伟达宣布投资", "新增 · NVDA · Reuters · 22:40"]
    text = json.dumps(card, ensure_ascii=False)
    for machine_word in (
        "AI 初判",
        "类型：",
        "范围：",
        "成员：",
        "Provider",
        "single_name",
        "利多",
        "新进展",
        "状态变化",
    ):
        assert machine_word not in text
    assert "Nvidia to invest $100bn" not in text
    assert "打开来源" in text and "news_delivery_card" not in text
    # The key marker is the plan's, printed as the header qualifier.
    assert _update_card(key=True)["header"]["title"]["content"] == "⚡ 英伟达向数据中心投资千亿美元"
    # Card assets are the selected claims' own typed primaries: a mention or an untyped name is not shown.
    assert update_card_assets(nvda_update(), [claim.ref for claim in nvda_update().claims]) == [
        MarketAsset("NVDA", "equity")
    ]
    untyped = update_source("SEI launches a staking product.")
    sei = adopted((draft("a", untyped, assets=(asset("SEI", "unknown"),)), untyped))
    assert update_card_assets(sei, [sei.claims[0].ref]) == []
    many = update_source("Five tokens listed.")
    listed = adopted(
        (draft("a", many, assets=tuple(asset(f"T{index}", "crypto") for index in range(6))), many),
    )
    assert [shown.symbol for shown in update_card_assets(listed, [listed.claims[0].ref])] == ["T0", "T1", "T2", "T3"]


def test_card_names_the_change_label_from_the_updates_own_change_kinds() -> None:
    first = nvda_update()
    assert update_change_label(first, [first.claims[0].ref]) == "new"
    for kind, label, word in (
        ("parameter_change", "update", "更新"),
        ("phase_change", "update", "更新"),
        ("correction", "correction", "更正"),
        ("new_fact", "new", "新增"),
    ):
        update = nvda_update(changes=(("a", kind),))
        assert update_change_label(update, [update.claims[0].ref]) == label
        assert _body(_update_card(update)).splitlines()[-1].startswith(f"{word} · NVDA")
    # A correction among the selected claims outranks an update beside it.
    one, two = update_source("First claim."), update_source("Second claim.")
    both = adopted((draft("a", one), one), (draft("b", two), two), changes=(("a", "phase_change"), ("b", "correction")))
    assert update_change_label(both, [claim.ref for claim in both.claims]) == "correction"


def test_the_frozen_body_reaches_the_card_whole_and_unsafe_copy_is_refused_before_freezing() -> None:
    update = nvda_update()
    plan = plan_for(update)
    card = frozen_card(plan, update)
    rendered = news_update_card(card, plan=plan, update=update)
    assert f"{rendered.header.subject}\n\n{rendered.lead}" == card.body
    # Model copy carrying a link or a control character is refused, not cleaned: a channel may never
    # strip what the ledger records as sent.
    for unsafe in ("看 https://evil.example", "看 www.evil.example", "标题\x00", "两行\n标题"):
        with pytest.raises(ValueError, match="news_card_copy_unsafe"):
            frozen_card(plan, update, headline=unsafe)
    long_copy = copy_for(plan, "英伟达投资", **{update.claims[0].ref: "长" * 3_000})
    assert len(freeze_card(plan, update, long_copy).body) > 3_000


def _quote_line(quotes: Sequence[Mapping[str, Any]]) -> str:
    """The quote line as the renderer builds it: read-model rows to card facts to one line."""

    return quote_line(reader_quotes(quotes))


def _quote(symbol: str, price: str, change: float | None, **overrides: Any) -> dict[str, Any]:
    quote = {
        "symbol": symbol,
        "price": price,
        "change_pct": change,
        "change_basis": "rolling_24h",
        "instrument_class": "crypto",
        "state": "fresh",
    }
    quote.update(overrides)
    return quote


def _market_lines(**overrides: Any) -> list[str]:
    item = update_source(
        "WTI crude futures rise on the export ban.", origin="jin10", published_at_ms=None, available_at_ms=0
    )
    update = adopted((draft("a", item, assets=(asset("CL", "commodity"),), kind="official_measure"), item))
    return _body(_update_card(update, **overrides)).splitlines()[1:]


def test_card_market_line_is_display_only() -> None:
    # The market's own number, on its own line, for the assets the facts line already named (#113).
    assert _market_lines(quotes=[_quote("CL", "86.43", 2.296, instrument_class="commodity")]) == [
        "新增 · CL · jin10",
        "行情 CL $86.43 24h +2.30%（永续）",
    ]
    # Formatting is the console's `formatPrice`/`formatChangePct` character for character: thousands and two
    # decimals from 1000 up, up to four below it, up to six below one, trailing zeros dropped.
    assert _quote_line([_quote("BTC", "74757.60", 7.914)]) == "行情 BTC $74,757.60 24h +7.91%"
    assert _quote_line([_quote("SAMSUNG", "201.70000", 3.916)]) == "行情 SAMSUNG $201.7 24h +3.92%"
    assert _quote_line([_quote("MANTRA", "0.0043290", -10.516)]) == "行情 MANTRA $0.004329 24h -10.52%"
    # The window is named from `change_basis`, never assumed: Hyperliquid publishes the venue's day, not 24 h.
    assert (
        _quote_line([_quote("GOLD", "4538.55", 0.9239, change_basis="provider_day")])
        == "行情 GOLD $4,538.55 日内 +0.92%"
    )
    # A basis we cannot name costs the percentage, not the price.
    assert _quote_line([_quote("XX", "12.5", 1.0, change_basis="who_knows")]) == "行情 XX $12.5"
    assert _quote_line([_quote("XX", "12.5", None)]) == "行情 XX $12.5"
    # An issuer alias prices on another contract; the line keeps the ticker the facts line printed.
    alias = _quote("HK1810", "40.5", 1.0, requested_symbol="XIAOMI", instrument_class="equity")
    assert _quote_line([alias]) == "行情 XIAOMI $40.5 24h +1.00%（永续）"
    # Only `fresh` renders. Everything else leaves no line at all — never a placeholder, never a zero.
    for absent in ("stale", "unavailable", "unlisted"):
        assert _quote_line([_quote("BTC", "74757.60", 7.914, state=absent)]) == ""
        assert _market_lines(quotes=[_quote("CL", "86.43", 2.3, state=absent)]) == ["新增 · CL · jin10"]
    assert _quote_line([_quote("X", "0", 1.0)]) == "" and _quote_line([_quote("X", "not-a-price", 1.0)]) == ""
    # `parse_price` bounds a price to finite-and-positive, not to a magnitude, and quantizing 1e40 raises.
    # `_quote_line` runs in the renderer, outside the consumer's guard, so it must lose the entry, not the card.
    assert _quote_line([_quote("HUGE", "1e40", 1.0)]) == ""
    assert (
        _quote_line([_quote("HUGE", "1e40", 1.0), _quote("BTC", "74757.60", 7.914)]) == "行情 BTC $74,757.60 24h +7.91%"
    )
    assert _quote_line([]) == "" and _market_lines() == ["新增 · CL · jin10"]
    # The mark is attached per asset, never once for the line: a trailing mark on a mixed line cannot say
    # whether it covers the last asset or all of them.
    equities = [
        _quote(s, p, c, instrument_class="equity")
        for s, p, c in (
            ("AAPL", "312.56", -1.248),
            ("AMZN", "260.77", 1.1),
            ("META", "547.11", 0.4),
            ("MSFT", "481.85", -0.8),
        )
    ]
    assert _quote_line(equities) == (
        "行情 AAPL $312.56 24h -1.25%（永续） · AMZN $260.77 24h +1.10%（永续）"
        " · META $547.11 24h +0.40%（永续） · MSFT $481.85 24h -0.80%（永续）"
    )
    mixed = _quote_line(
        [_quote("BTC", "74757.60", 7.914), _quote("SAMSUNG", "201.70", 3.916, instrument_class="equity")]
    )
    assert mixed == "行情 BTC $74,757.60 24h +7.91% · SAMSUNG $201.7 24h +3.92%（永续）"
    assert _quote_line([*equities, _quote("BTC", "1", 1.0)]) == _quote_line(equities)  # bounded at four


def test_reader_market_movements_require_fresh_push_price_and_selected_anchors() -> None:
    quote = _quote(
        "BTC",
        "101.10",
        3.2,
        requested_symbol="BTC",
        base_symbol="BTC",
        venue="binance.perp",
        venue_symbol="BTCUSDT",
        quote_asset="USDT",
        price_at_news="100.00",
        price_one_hour_before_push="99.00",
    )
    assert reader_market_movements(["BTC"], [quote]) == (ReaderMarketMovement("BTC", 110, 212, 320, "available"),)
    assert reader_market_movements(["BTC"], [{**quote, "state": "stale"}]) == (
        ReaderMarketMovement("BTC", None, None, None, "unavailable"),
    )


def test_reader_market_movements_never_fabricate_missing_anchor_prices() -> None:
    quote = _quote(
        "BTC",
        "101.10",
        3.2,
        requested_symbol="BTC",
        base_symbol="BTC",
        venue="binance.perp",
        venue_symbol="BTCUSDT",
        quote_asset="USDT",
        price_at_news="not-a-price",
    )
    assert reader_market_movements(["BTC"], [quote]) == (ReaderMarketMovement("BTC", None, None, 320, "unavailable"),)


def test_delivery_returns_use_news_and_push_centered_price_windows() -> None:
    quote = _quote(
        "MSFT",
        "102.00",
        2.27,
        requested_symbol="MSFT",
        base_symbol="MSFT",
        venue="hl.xyz",
        venue_symbol="xyz:MSFT",
        price_at_news="100.00",
        price_one_hour_before_push="101.00",
    )

    assert reader_market_movements(["MSFT"], [quote]) == (ReaderMarketMovement("MSFT", 200, 99, 227, "available"),)


def test_reader_trade_targets_bind_ticker_to_exact_binance_contracts_without_changing_the_card() -> None:
    perpetual_quote = _quote(
        "LRCX",
        "317.53",
        1.12,
        requested_symbol="LRCX",
        base_symbol="LRCX",
        venue="binance.perp",
        venue_symbol="LRCXUSDT",
        quote_asset="USDT",
        instrument_class="equity",
    )
    spot_quote = _quote(
        "BTC",
        "74553.10",
        7.91,
        requested_symbol="BTC",
        base_symbol="BTC",
        venue="binance.spot",
        venue_symbol="BTCUSDT",
        quote_asset="USDT",
    )
    assert reader_trade_targets([perpetual_quote, spot_quote]) == (
        ReaderTradeTarget(
            ticker="LRCX",
            venue="binance.perp",
            venue_symbol="LRCXUSDT",
            base_symbol="LRCX",
            quote_asset="USDT",
        ),
        ReaderTradeTarget(
            ticker="BTC",
            venue="binance.spot",
            venue_symbol="BTCUSDT",
            base_symbol="BTC",
            quote_asset="USDT",
        ),
    )
    assert _market_lines(quotes=[perpetual_quote], assets=["LRCX"]) == [
        "新增 · LRCX · jin10",
        "行情 LRCX $317.53 24h +1.12%（永续）",
    ]

    assert reader_trade_targets(
        [_quote("ETH", "2300", 1.0, requested_symbol="ETH", base_symbol="ETH", venue="hl.perp", venue_symbol="ETH")]
    ) == (
        ReaderTradeTarget(
            ticker="ETH",
            venue="hl.perp",
            venue_symbol="ETH",
            base_symbol="ETH",
            quote_asset="",
        ),
    )

    # The adapter gets no target for malformed contracts or a Binance ticker/base/pair mismatch.
    unsafe = [
        _quote(
            "SOL",
            "200",
            1.0,
            requested_symbol="SOL",
            base_symbol="SOL",
            venue="binance.perp",
            venue_symbol="SOL/USDT",
            quote_asset="USDT",
        ),
        _quote(
            "BTC",
            "74553.10",
            7.91,
            requested_symbol="BTC",
            base_symbol="ETH",
            venue="binance.spot",
            venue_symbol="ETHUSDT",
            quote_asset="USDT",
        ),
        _quote(
            "ETH",
            "2300",
            1.0,
            requested_symbol="ETH",
            base_symbol="BTC",
            venue="binance.perp",
            venue_symbol="BTCUSDT",
            quote_asset="USDT",
        ),
        _quote(
            "BTC",
            "74553.10",
            7.91,
            requested_symbol="BTC",
            base_symbol="BTC",
            venue="binance.spot",
            venue_symbol="ETHUSDT",
            quote_asset="USDT",
        ),
        _quote(
            "BTC",
            "74553.10",
            7.91,
            base_symbol="BTC",
            venue="binance.perp",
            venue_symbol="BTCUSDT",
            quote_asset="USDT",
        ),
        _quote(
            "BTC",
            "74553.10",
            7.91,
            requested_symbol="BTC",
            venue="binance.perp",
            venue_symbol="BTCUSDT",
            quote_asset="USDT",
        ),
        _quote(
            "ETH",
            "2300",
            1.0,
            requested_symbol="BTC",
            base_symbol="BTC",
            venue="binance.perp",
            venue_symbol="BTCUSDT",
            quote_asset="USDT",
        ),
    ]
    assert all(reader_trade_targets([quote]) == () for quote in unsafe)


def test_card_change_basis_labels_cover_the_price_domain() -> None:
    """A basis `pricing` knows and the card cannot name would drop that venue's percentage in silence."""

    assert set(CHANGE_BASIS_LABEL) == set(CHANGE_BASIS_ZH)


def test_bus_envelope_roundtrip() -> None:
    m = BusMessage(
        kind="event",
        message_id="event:1",
        routing_key="event.general.high",
        payload={"event_id": "1"},
        trace_id="t",
        occurred_at_ms=5,
        priority=5,
    )
    back = decode_body(m.body(), routing_key=m.routing_key, priority=5, headers={"x-delivery-count": 1})
    assert back.payload == {"event_id": "1"} and back.attempt == 2 and back.priority == 5
    # A first delivery carries no broker counter at all; that is attempt 1, not a decode failure (#400).
    first = decode_body(m.body(), routing_key=m.routing_key, priority=5, headers={})
    assert first.attempt == 1
    # Anything else in that header means the delivery cannot be attributed, so it fails closed.
    for invalid in ("2", -1, 2.0, True, None):
        with pytest.raises(BusDecodeError, match="news_bus_delivery_count_invalid"):
            decode_body(m.body(), routing_key=m.routing_key, priority=5, headers={"x-delivery-count": invalid})
    with pytest.raises(BusDecodeError):
        decode_body(b"{}", routing_key="x", priority=0, headers=None)


# ---------------------------------------------------------------- golden replay
def test_golden_replay_on_real_sample() -> None:
    hits = _hits()
    report = replay_hits(hits, watchlist_symbols=frozenset({"BTC", "ETH", "NVDA"}), instrument_classes=None)
    counts = report["counts"]
    assert counts["items"] == len({h["id"] for h in hits})
    # Levi & Korsinsky template PRs must not merge (ticker veto) and are vetoed at the Gate
    levi = [h for h in hits if "Levi & Korsinsky" in str(h.get("text"))]
    assert len(levi) >= 3
    assert counts.get("admission:suppressed_pr_template", 0) >= len(levi)
    assert "admission:suppressed_ungrounded" not in counts and "admission:suppressed_ungrounded_meme" not in counts
    # 'reply <url>' items with distinct slugs must not collapse into one event
    replies = [h for h in hits if str(h.get("text", "")).lower().startswith("reply http")]
    assert len(replies) >= 2
    reply_report = replay_hits(replies, watchlist_symbols=frozenset(), instrument_classes=None)
    assert reply_report["counts"]["events"] == len(replies)
    # Binance CFX announcement burst collapses within each kind; news and listing never merge.
    cfx = [h for h in hits if "Conflux Network (CFX)" in str(h.get("text"))]
    cfx_report = replay_hits(cfx, watchlist_symbols=frozenset(), instrument_classes=None)
    assert cfx_report["counts"]["events"] == 2 and cfx_report["counts"]["exact_members"] == len(cfx) - 2
    # The Gate no longer decides relevance: most items reach Triage (the model is the semantic filter)
    assert report["candidate_share_of_items"] >= 0.65


# ---------------------------------------------------------------- recall regression (issue #53)
RECALL_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_recall_sample.json"
EXPECTATIONS = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_expectations.json"


def test_gate_expectations_over_the_recall_corpus() -> None:
    """Trajectory-prefix regression: every case names a real headline and the acceptable Gate outcome set."""

    hits = _hits() + json.loads(RECALL_FIXTURE.read_text(encoding="utf-8"))
    report = replay_hits(hits, watchlist_symbols=frozenset(), instrument_classes=None)
    events = report["events"]
    failures: list[str] = []
    for case in json.loads(EXPECTATIONS.read_text(encoding="utf-8"))["cases"]:
        matched = [e for e in events if case["match"] in e["title"]]
        if not matched:
            # the normalized title may have lost a prefix; fall back to the raw hit text
            raw = [h for h in hits if case["match"] in str(h.get("text", ""))]
            if not raw:
                failures.append(f"no hit matches {case['match']!r}")
                continue
            matched = [
                e for e in events if e["title"] and e["title"] in str(raw[0].get("text", "")).replace("<br/>", " ")
            ]
            if not matched:
                failures.append(f"no event matches {case['match']!r}")
                continue
        for event in matched[:1]:
            if event["admission"] not in case["admission"]:
                failures.append(f"{case['match']!r}: admission {event['admission']} not in {case['admission']}")
            grounded = {g.replace("XYZ-", "") for g in event["grounded_assets"]}
            if case.get("grounded_any") and not grounded & set(case["grounded_any"]):
                failures.append(f"{case['match']!r}: grounded {sorted(grounded)} lacks {case['grounded_any']}")
            if case.get("grounded_none") and grounded & set(case["grounded_none"]):
                failures.append(f"{case['match']!r}: grounded {sorted(grounded)} contains {case['grounded_none']}")
            if case.get("storyline") and event["storyline_key"] not in case["storyline"]:
                failures.append(f"{case['match']!r}: storyline {event['storyline_key']} not in {case['storyline']}")
            if case.get("title_startswith") and not event["title"].startswith(case["title_startswith"]):
                failures.append(f"{case['match']!r}: title {event['title'][:60]!r}")
    assert failures == []
    # Head-line numbers the hard cut is accountable for: most items reach Triage, templates never do.
    assert report["candidate_share_of_items"] >= 0.7
    assert report["counts"].get("admission:suppressed_pr_template", 0) >= 8


def test_final_storyline_key_prefers_the_named_subject_over_an_arbitrary_tag() -> None:
    """#100: the fallback used to take *any* grounded tag, so OKX's listing notices (every one of them tagged
    OKB) all landed in `asset:OKB`, and a VeChain upgrade vote landed in `asset:SKHY`. 16% of a live day's
    asset-keyed cards sat in a bucket that was not about them (alias-resolved; 20% counting raw symbols)."""

    # The model named the subject; the provider only tagged the venue's own token.
    assert (
        final_storyline_key(
            title="Johnson & Johnson ($JNJx) Found in OKX",
            headline_zh="强生（$JNJx）出现在 OKX",
            scope="single_name",
            verdict_primaries=[MarketAsset("JNJ")],
            grounded_assets=["OKB"],
            dedupe_family="general",
        )
        == "asset:JNJ"
    )
    # The model named nothing and the tag is not what the text is about: the family bucket, not `asset:BTC`.
    assert (
        final_storyline_key(
            title="Poland scrambles jets after unidentified drones cross its border",
            headline_zh="波兰启动预防性军机行动",
            scope="macro",
            verdict_primaries=[],
            grounded_assets=["BTC"],
            dedupe_family="general",
        )
        == NO_STORYLINE_KEY
    )
    # The model named nothing but the text names the tag as its own token: still that asset's storyline.
    assert (
        final_storyline_key(
            title="OKB burn completed",
            headline_zh="OKB 完成销毁",
            scope="macro",
            verdict_primaries=[],
            grounded_assets=["OKB"],
            dedupe_family="general",
        )
        == "asset:OKB"
    )
    # A full-token match only: a tag that merely prefixes a longer word is not evidence.
    assert (
        final_storyline_key(
            title="Elon Musk sells another stake",
            headline_zh="马斯克再度减持",
            scope="macro",
            verdict_primaries=[],
            grounded_assets=["MU"],
            dedupe_family="general",
        )
        == NO_STORYLINE_KEY
    )
    # A degraded verdict has no `assets` by construction, so "named nothing" says nothing: keep the old fallback.
    assert (
        final_storyline_key(
            title="NVIDIA to invest $100bn in OpenAI data centre",
            headline_zh="NVIDIA 投资 OpenAI",
            scope="macro",
            verdict_primaries=[],
            grounded_assets=["NVDA"],
            dedupe_family="general",
            degraded=True,
        )
        == "asset:NVDA"
    )
    # A grounded primary still wins outright, and a registry hit still beats both fallbacks.
    assert (
        final_storyline_key(
            title="Iran halts oil exports",
            headline_zh="伊朗停止石油出口",
            scope="macro",
            verdict_primaries=[MarketAsset("XOM")],
            grounded_assets=["XOM"],
            dedupe_family="general",
        )
        == "conflict:mideast_2026"
    )


def test_final_storyline_key_only_accepts_symbol_shaped_primaries() -> None:
    """`TriageAsset.symbol` is free text and this fallback is reached when nothing grounded it, so it is the least
    validated string in the pipeline — and it becomes a duplicate-comparison group, an advisory-lock key and a
    console label."""

    def key(primaries: list[str], **over: object) -> str:
        return final_storyline_key(
            title=str(over.get("title", "Some exchange notice")),
            headline_zh="",
            scope="single_name",
            verdict_primaries=[MarketAsset(symbol) for symbol in primaries],
            grounded_assets=["OKB"],
            dedupe_family="general",
        )

    assert key(["TSLA"]) == "asset:TSLA"
    # #509 P4: an exchange-qualified identifier is groupable and now mints its own key. Anything else the
    # shape rejects still falls through rather than becoming an advisory-lock key.
    assert key(["0001.HK"]) == "asset:0001.HK"
    assert key(["0001.NASDAQ"]) == NO_STORYLINE_KEY
    assert key(["a" * 11]) == NO_STORYLINE_KEY


def test_symbol_in_text_does_not_match_ordinary_english_words() -> None:
    """`NOT`, `ME`, `ID`, `IO`, `ON` and `AI` are all real provider tags. A case-insensitive match turned "he will
    not sell his stake" into evidence for `asset:NOT` — the exact mis-bucketing this fallback exists to prevent."""

    assert not symbol_in_text("NOT", "Trump says he will not raise tariffs on Canada")
    assert not symbol_in_text("ME", "show me the money")
    assert not symbol_in_text("ID", "no id required")
    assert symbol_in_text("NOT", "NOT holders vote on the treasury")
    assert symbol_in_text("OKB", "强生（$OKB）出现在 OKX")
    assert (
        final_storyline_key(
            title="Musk says he will not sell his stake",
            headline_zh="马斯克称不会减持",
            scope="macro",
            verdict_primaries=[],
            grounded_assets=["NOT"],
            dedupe_family="general",
        )
        == NO_STORYLINE_KEY
    )


def test_the_verifier_reads_the_kind_the_policy_acted_on_not_the_one_the_model_wrote() -> None:
    """#679 review 5. `non_fact_delivered` is critical, and one legitimate path would have tripped it.

    `confirmed_fact_kind` re-reads a `market_flow_price` report against its own text, and the >= 5%
    commodity/index exception carries a card the model called a `statement` through as a `new_quantity`.
    The verdict still stores `statement`, so a verifier comparing the stored kind would raise a critical
    flag on a card the policy deliberately delivered. `decide()` already records which kind it acted on
    -- the `fact_kind_*` override rule names it -- so the ledger is read rather than a second copy
    persisted beside it.
    """

    from tracefold.news.review.desk import _acted_fact_kind, _verifier_flags

    admitted = {
        "verdict": {"fact_kind": "statement", "novelty": "new_fact"},
        "final_decision": "push",
        "override_rule": "fact_kind_new_quantity",
    }
    assert _acted_fact_kind("fact_kind_new_quantity", admitted["verdict"]) == "new_quantity"
    assert not [flag for flag in _verifier_flags(admitted) if flag["code"] == "non_fact_delivered"]

    # The case the flag exists for is untouched: a `statement` that reached a reader through an
    # objective guard has no `fact_kind_*` rule, so the stored kind is what the verifier reads.
    guarded = {**admitted, "override_rule": "listing_deterministic"}
    assert _acted_fact_kind("listing_deterministic", guarded["verdict"]) == "statement"
    flags = [flag for flag in _verifier_flags(guarded) if flag["code"] == "non_fact_delivered"]
    assert [flag["severity"] for flag in flags] == ["info"]

    # And a `promotion` delivered with no guard at all is still the critical finding.
    unguarded = {**admitted, "override_rule": "", "verdict": {"fact_kind": "promotion"}}
    flags = [flag for flag in _verifier_flags(unguarded) if flag["code"] == "non_fact_delivered"]
    assert [flag["severity"] for flag in flags] == ["critical"]
