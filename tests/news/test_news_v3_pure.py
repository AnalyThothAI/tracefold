"""Pure-module tests for News V3: titles, gate, storyline, rules, minhash, delivery, bus."""

from __future__ import annotations

import json
import random
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest

from tests.support.news_judgment import news_taxonomy, scored_judgment, trade_relevance
from tracefold.news.bus import BusDecodeError, BusMessage, decode_body
from tracefold.news.delivery import (
    _CHANGE_BASIS_LABEL,
    _quote_line,
    card_assets,
    reader_market_movements,
    reader_trade_targets,
    render_first_card,
    sanitize_ai_text,
)
from tracefold.news.eval.replay import replay_hits
from tracefold.news.events.facts import FactUnit, extract_fact_units
from tracefold.news.events.gate import GateInput, evaluate_gate, gate_lexicon_flags, grounded_assets
from tracefold.news.events.minhash import BANDS, band_keys, estimate_jaccard, minhash_signature
from tracefold.news.events.storyline import (
    NO_STORYLINE_KEY,
    STORYLINE_REGISTRY_SHA256,
    STORYLINE_REGISTRY_VERSION,
    StorylineRegistry,
    _symbol_in_text,
    final_storyline_key,
    load_storyline_registry,
    match_storyline,
    preliminary_storyline_key,
    registry_storyline_key,
)
from tracefold.news.events.titles import extract_title
from tracefold.news.events.tokens import comparison_tokens, jaccard
from tracefold.news.market_review.pricing import CHANGE_BASIS_ZH
from tracefold.news.models import ReaderMarketMovement, ReaderReceipt, ReaderTradeTarget, TriageAsset, TriageVerdict
from tracefold.news.opennews import source_artifact_identity
from tracefold.news.outcome import OVERRIDE_RULE_ZH, storyline_key_zh, throttled_by_zh
from tracefold.news.pipeline.admission import _event_identity
from tracefold.news.similarity import similarity
from tracefold.news.triage_rules import (
    DEFAULT_POLICY,
    STALE_SOURCE_KEY,
    DecidePolicy,
    GateFacts,
    StorylineStatus,
    fallback_verdict,
    rule_baseline,
    storyline_status,
)
from tracefold.news.triage_rules import (
    decide as production_decide,
)

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

    news_id = _event_identity(item_id=item_id, fact=fact, event_kind="news")
    assert news_id != item_id
    assert news_id == _event_identity(item_id=item_id, fact=fact, event_kind="news")
    assert news_id != _event_identity(item_id=item_id, fact=fact, event_kind="oi")


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
    assert estimate_jaccard(minhash_signature(a), minhash_signature(a)) == 1.0


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
            verdict_primaries=["BTC"],
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
            verdict_primaries=["NVDA"],
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
            verdict_primaries=["BTC"],
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
            verdict_primaries=["BTC"],
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
                verdict_primaries=[symbol],
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


# ---------------------------------------------------------------- triage rules
def _verdict(**kw) -> TriageVerdict:
    base = dict(
        novelty="new_fact",
        assets=[TriageAsset(symbol="NVDA", role="primary")],
        direction="bullish",
        scope="single_name",
        magnitude=2,
        confidence=0.8,
        headline_zh="英伟达投资",
        why_zh="",
    )
    base.update(kw)
    return TriageVerdict(**base)


_FACTS = GateFacts(
    grounded_assets=("NVDA", "XYZ-NVDA"),
    watchlist_symbols=frozenset({"NVDA"}),
    admission="candidate",
)


def decide(
    verdict: TriageVerdict,
    facts: GateFacts,
    status: StorylineStatus | None,
    *,
    policy: DecidePolicy = DEFAULT_POLICY,
    relevance: dict[str, Any] | None = None,
    now_ms: int | None = None,
) -> Any:
    """Exercise the current model-only seam without repeating envelope construction in every pure assertion."""

    judgment = scored_judgment(verdict, relevance=trade_relevance(**(relevance or {})))
    return production_decide(judgment, facts, status, policy=policy, now_ms=now_ms)


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


def test_stale_source_artifact_is_withheld_but_never_an_escalation() -> None:
    """#154: a 16-day-old tweet shipped as `Take-Two 股票 $TTWO 周四在 Solana 上线`.

    The artifact ledger cannot see that one — it was ingested exactly once — so age is the only signal. Escalate
    stays exempt for the same reason it is exempt from the similarity check.
    """

    fresh = replace(_FACTS, source_age_s=2)
    stale = replace(_FACTS, source_age_s=int(385.6 * 3600))
    status = storyline_status("asset:TTWO")

    assert decide(_verdict(magnitude=2), fresh, status).final == "push"
    withheld = decide(_verdict(magnitude=2), stale, status)
    assert withheld.final == "throttled"
    assert withheld.override_rule == "stale_source_artifact"
    # A constant key, not the age: `throttled_by` is counted into a top-10 map and a per-second key would
    # give every withhold its own bucket.
    assert withheld.throttled_by == STALE_SOURCE_KEY
    assert throttled_by_zh(STALE_SOURCE_KEY) == "旧闻：这条推文在 provider 推送时就已过时"
    assert OVERRIDE_RULE_ZH["stale_source_artifact"] == "来源推文本身已过时，按旧闻扣下"

    # (#504 D3: a corroborated escalate — two independent arrivals — is the one that keeps its exemption.)
    assert (
        decide(
            _verdict(magnitude=3),
            replace(stale, watchlist_symbols=frozenset(), member_count=2),
            status,
            relevance={"reader_value": "escalate"},
        ).final
        == "escalate"
    )
    # No artifact timestamp (every non-x/twitter frame) is not evidence of staleness.
    assert decide(_verdict(magnitude=2), replace(_FACTS, source_age_s=None), status).final == "push"
    # The knob turns it off without touching anything else.
    off = DecidePolicy(stale_source_max_age_s=0)
    assert decide(_verdict(magnitude=2), stale, status, policy=off).final == "push"


def test_policy_v12_has_six_safety_duplicate_and_budget_knobs() -> None:
    assert {item.name for item in fields(DecidePolicy)} == {
        "restatement_drop",
        "similarity_max",
        "stale_source_max_age_s",
        "listing_exempt_from_duplicate",
        "storyline_budget_window_s",
        "storyline_budget_max",
    }
    assert DEFAULT_POLICY.storyline_budget_window_s == 3600 and DEFAULT_POLICY.storyline_budget_max == 2
    assert len(DEFAULT_POLICY.as_dict()) == 6


def test_trade_relevance_is_the_only_model_owned_action_input() -> None:
    off_watchlist = GateFacts(
        grounded_assets=("SPY",),
        watchlist_symbols=frozenset(),
        admission="candidate",
        member_count=2,  # #504 D3: an escalate from an `unknown` source needs a second arrival
    )
    verdict = _verdict(
        magnitude=2,
        assets=[TriageAsset(symbol="SPY", role="primary")],
    )
    realtime = decide(verdict, off_watchlist, None)
    assert realtime.final == "push" and realtime.override_rule == "trade_relevance_realtime"

    escalated = decide(verdict, off_watchlist, None, relevance={"reader_value": "escalate"})
    assert escalated.final == "escalate" and escalated.override_rule == "trade_relevance_escalate"

    background = decide(
        verdict,
        off_watchlist,
        None,
        relevance={
            "reader_value": "background",
            "tradability": "contextual",
            "channels": [],
            "affected_markets": [],
        },
    )
    assert background.final == "drop" and background.override_rule == "reader_value_background"

    ineligible = decide(verdict.model_copy(update={"magnitude": 1}), off_watchlist, None)
    assert ineligible.final == "drop" and ineligible.override_rule == "trade_relevance_inconsistent"


def test_queue_priority_never_enters_policy_facts() -> None:
    assert "queue_priority" not in {item.name for item in fields(GateFacts)}
    assert "priority" not in {item.name for item in fields(GateFacts)}


def test_listing_frames_are_exempt_from_duplicate_evidence_only_across_instruments() -> None:
    """ "Coinbase adds ALIGN" and "Upbit adds BICO" are different instruments in one wire template, so
    restatement and bigram similarity both read the second as a repeat. #72 admits these frames
    deterministically; policy v8 stops that admission being undone one step later — but only for the
    different-instrument case, so a genuinely re-issued notice is still withheld.

    The comparison is between symbol sets. Reader headlines are Chinese with parenthesised tickers
    stripped by contract, so a ticker-in-headline test would be inert in production and would also
    fire by accident (``BASE`` inside "Coinbase").
    """

    # A ledger entry whose rendered headline names no ticker at all — the production shape.
    told = [{"direction": "bullish", "headline_zh": "Coinbase 将上线狗狗币", "symbols": ["DOGE"]}]
    status = storyline_status("asset:DOGE", told=told)
    admitted = replace(_FACTS, admission="listing_deterministic", grounded_assets=("BICO",))
    other_instrument = _verdict(
        novelty="restatement",
        restates=0,
        headline_zh="Upbit 将上线 BICO 等三个币种",
        direction="bullish",
        assets=[TriageAsset(symbol="BICO", role="primary")],
    )
    assert decide(other_instrument, admitted, status).final == "push"

    # A byte-identical re-send of the card the reader already received. The model itself called it a
    # restatement of the entry it was shown, and it must not reach the reader twice.
    same_instrument = other_instrument.model_copy(
        update={"headline_zh": "Coinbase 将上线狗狗币", "assets": [TriageAsset(symbol="DOGE", role="primary")]}
    )
    repeat = decide(same_instrument, replace(admitted, grounded_assets=("DOGE",)), status)
    assert repeat.final == "drop" and repeat.override_rule == "restatement"

    # A ledger row that carries no assets is not evidence of a different instrument.
    blind = storyline_status(
        "asset:DOGE", told=[{"direction": "bullish", "headline_zh": "Coinbase 将上线狗狗币", "symbols": []}]
    )
    assert decide(other_instrument, admitted, blind).override_rule == "restatement"

    # Only the Gate's objective admission is evidence of a listing frame.
    typed_only = decide(other_instrument, replace(_FACTS, grounded_assets=("BICO",)), status)
    assert typed_only.final == "drop" and typed_only.override_rule == "restatement"

    # The exemption stays operator-owned.
    kept = decide(
        other_instrument, admitted, status, policy=replace(DEFAULT_POLICY, listing_exempt_from_duplicate=False)
    )
    assert kept.final == "drop" and kept.override_rule == "restatement"


def test_policy_v13_listing_admission_yields_only_to_a_reader_value_none_frame() -> None:
    """#523 D1. `listing_deterministic` is the provider's `engine_type=listing` tag, not a content judgment.

    Over 24 h it admitted 56 frames: 20 real listings/delistings, 10 marketing/airdrop/rebate posts, 7
    operations notices and 19 market or company miscellany. The model scored 17 of them `reader_value=none`
    and 13 of those were pushed anyway (a Binance trading competition, a "Rug Pulls explained" explainer, a
    35% APR promotion). v13 lets exactly those fall through to the ordinary `reader_value_none` drop; nothing
    else about the branch moves.
    """

    listing = replace(_NO_WATCHLIST, admission="listing_deterministic", grounded_assets=("BICO",))
    frame = _verdict(assets=[TriageAsset(symbol="BICO", role="primary")], headline_zh="币安上线 BICO 交易竞赛")
    worthless: dict[str, Any] = {
        "reader_value": "none",
        "tradability": "contextual",
        "channels": [],
        "affected_markets": [],
    }

    dropped = decide(frame, listing, None, relevance=worthless)
    assert dropped.final == "drop" and dropped.override_rule == "reader_value_none"
    # The degraded lane has no `reader_value` at all, so a listing frame still pushes objectively there —
    # which is also what the dropped card reports as the baseline it was measured against.
    assert dropped.rule_baseline == "push"
    assert rule_baseline(listing) == "push"
    assert fallback_verdict(listing, error_code="news_program_route_deadline").decision.final == "push"

    # `background` keeps the objective guard: v13 yields to `none` only, because `none` is the one value that
    # says the model found nothing a reader could use. Moving the branch below `background` instead cost four
    # genuine listings in the same replay.
    background = decide(frame, listing, None, relevance={**worthless, "reader_value": "background"})
    assert background.final == "push" and background.override_rule == "listing_deterministic"
    # And a real listing notice is untouched, whichever ordinary rule would also have selected it.
    for relevance in ({}, {"reader_value": "escalate"}):
        admitted = decide(frame, listing, None, relevance=relevance)
        assert admitted.final == "push" and admitted.override_rule == "listing_deterministic", relevance
    # `reader_value` is the whole condition: a `none` frame with a full trade surface still yields.
    surfaced = decide(frame, listing, None, relevance={"reader_value": "none"})
    assert surfaced.final == "drop" and surfaced.override_rule == "reader_value_none"
    # The objective watchlist guard still runs after the listing branch, so a grounded watchlist asset the
    # model called worthless is pushed by the guard, not by the admission.
    guarded = decide(frame, replace(_FACTS, admission="listing_deterministic"), None, relevance=worthless)
    assert guarded.final == "push" and guarded.override_rule == "watchlist_objective_guard"


def test_decide_rules_and_throttle() -> None:
    # The grounded watchlist is an objective guard and wins before model relevance.
    guarded = decide(
        _verdict(magnitude=0),
        _FACTS,
        None,
        relevance={
            "reader_value": "background",
            "tradability": "contextual",
            "channels": [],
            "affected_markets": [],
        },
    )
    assert guarded.final == "push" and guarded.override_rule == "watchlist_objective_guard"

    # No objective guard: the exact realtime eligibility predicate applies.
    off_watchlist = GateFacts(
        grounded_assets=("AMD",),
        watchlist_symbols=frozenset(),
        admission="candidate",
    )
    assert decide(_verdict(assets=[TriageAsset(symbol="AMD", role="primary")]), off_watchlist, None).final == "push"
    assert decide(_verdict(magnitude=1), off_watchlist, None).override_rule == "trade_relevance_inconsistent"
    assert (
        decide(_verdict(), off_watchlist, None, relevance={"development_delta": "color_only"}).override_rule
        == "trade_relevance_inconsistent"
    )

    busy = StorylineStatus(key="conflict:mideast_2026")
    unbounded = decide(_verdict(magnitude=2, scope="sector"), _FACTS, busy)
    assert unbounded.final == "push" and unbounded.throttled_by is None


def test_decide_restatement_drop_is_grounded() -> None:
    """A grounded restatement never pushes (issue #61); an ungrounded one is neither dropped nor let through."""

    quiet = StorylineStatus(key="asset:NVDA", told_directions=("bullish", "bearish"))
    # Grounded restatement of entry 0 (same direction) -> drop, named.
    dropped = decide(_verdict(novelty="restatement", restates=0), _FACTS, quiet)
    assert dropped.final == "drop" and dropped.override_rule == "restatement"
    # Restatement of entry 1 whose direction was bearish while this one is bullish: a flip is never a restatement.
    assert decide(_verdict(novelty="restatement", restates=1), _FACTS, quiet).final == "push"
    # Out-of-range index or an empty ledger: the claim is ignored, so a hallucinated restatement cannot drop a card.
    assert decide(_verdict(novelty="restatement", restates=7), _FACTS, quiet).final == "push"
    assert (
        decide(_verdict(novelty="restatement", restates=0), _FACTS, StorylineStatus(key="asset:NVDA")).final == "push"
    )
    assert decide(_verdict(novelty="restatement", restates=0), _FACTS, None).final == "push"
    # An m3 restatement (the duplicated 4.75% yield escalate) drops too.
    assert decide(_verdict(novelty="restatement", restates=0, magnitude=3), _FACTS, quiet).final == "drop"
    # The switch.
    assert (
        decide(
            _verdict(novelty="restatement", restates=0), _FACTS, quiet, policy=DecidePolicy(restatement_drop=False)
        ).final
        == "push"
    )


def _busy(**seen: object) -> StorylineStatus:
    """An asset storyline with prior delivery history and optional sent-ledger evidence."""

    return StorylineStatus(
        key="asset:NVDA",
        **seen,  # type: ignore[arg-type]
    )


_NOW = 1_800_000_000_000
_NO_WATCHLIST = replace(_FACTS, watchlist_symbols=frozenset())


def _sent(
    key: str, *directions: str, minutes_ago: int = 30, headline: str = "已推送过的另一件事"
) -> list[dict[str, Any]]:
    """Newest-first sent-ledger rows on one storyline key, as ``recent_seen_rows`` projects them."""

    return [
        {
            "event_id": f"{key}-{index}",
            "at_ms": _NOW - (minutes_ago + index) * 60_000,
            "storyline_key": key,
            "direction": direction,
            "headline_zh": f"{headline}{index}",
        }
        for index, direction in enumerate(directions)
    ]


def test_decide_withholds_the_third_card_on_a_storyline_inside_the_budget_window() -> None:
    """#504 D2. Prior *volume on the reader* never blocks a card, but prior delivered cards *on this storyline*
    do once the budget is spent: the third same-key card inside an hour is `storyline:<key>:budget`."""

    key = "conflict:mideast_2026"
    third = _verdict(scope="macro", assets=[], direction="bearish", headline_zh="伊朗宣布封锁霍尔木兹海峡")
    spent = storyline_status(key, seen=_sent(key, "bearish", "bearish"))

    withheld = decide(third, _NO_WATCHLIST, spent, now_ms=_NOW)
    assert withheld.final == "throttled" and withheld.throttled_by == f"storyline:{key}:budget"
    assert withheld.override_rule == "trade_relevance_realtime"  # the rule that would have pushed it
    assert withheld.seen_scope == "all" and withheld.seen_similarity is not None  # similarity still measured
    assert throttled_by_zh(withheld.throttled_by).startswith("同线索预算")

    # One delivered card is under budget; so are two delivered cards older than the window.
    assert decide(third, _NO_WATCHLIST, storyline_status(key, seen=_sent(key, "bearish")), now_ms=_NOW).final == "push"
    aged = storyline_status(key, seen=_sent(key, "bearish", "bearish", minutes_ago=90))
    assert decide(third, _NO_WATCHLIST, aged, now_ms=_NOW).final == "push"
    # Cards on other keys do not count against this one, however many.
    other = storyline_status(key, seen=_sent("conflict:ru_ua", "bearish", "bearish", "bearish"))
    assert decide(third, _NO_WATCHLIST, other, now_ms=_NOW).final == "push"

    # Exemption: a direction reversal against the newest delivered card on the key is new information.
    flip = decide(third.model_copy(update={"direction": "bullish"}), _NO_WATCHLIST, spent, now_ms=_NOW)
    assert flip.final == "push" and flip.throttled_by is None
    # ... but only against the *newest* one: newest bullish, older bearish, candidate bullish is not a flip.
    mixed = storyline_status(key, seen=_sent(key, "bullish", "bearish"))
    assert (
        decide(third.model_copy(update={"direction": "bullish"}), _NO_WATCHLIST, mixed, now_ms=_NOW).final
        == "throttled"
    )
    # Neutral on either side is not a reversal.
    assert (
        decide(third.model_copy(update={"direction": "neutral"}), _NO_WATCHLIST, spent, now_ms=_NOW).final
        == "throttled"
    )

    # Exemption: the `none` key is not a storyline (the registry matched nothing) and is never budgeted.
    fallback = storyline_status(NO_STORYLINE_KEY, seen=_sent(NO_STORYLINE_KEY, "bearish", "bearish", "bearish"))
    assert decide(third, _NO_WATCHLIST, fallback, now_ms=_NOW).final == "push"

    # Either knob at 0 switches the budget off; a caller without a clock has nothing to measure.
    for policy in (DecidePolicy(storyline_budget_max=0), DecidePolicy(storyline_budget_window_s=0)):
        assert decide(third, _NO_WATCHLIST, spent, policy=policy, now_ms=_NOW).final == "push"
    assert decide(third, _NO_WATCHLIST, spent).final == "push"
    # `max=3` is the replay's other candidate (#504 §9.1): the third card passes, the fourth does not.
    three = DecidePolicy(storyline_budget_max=3)
    assert decide(third, _NO_WATCHLIST, spent, policy=three, now_ms=_NOW).final == "push"
    fourth = storyline_status(key, seen=_sent(key, "bearish", "bearish", "bearish"))
    assert decide(third, _NO_WATCHLIST, fourth, policy=three, now_ms=_NOW).throttled_by == f"storyline:{key}:budget"

    # A watchlist push is still a push: the budget is the last throttle whatever rule selected the action.
    guarded = decide(
        _verdict(headline_zh="英伟达宣布回购"),
        _FACTS,
        storyline_status("asset:NVDA", seen=_sent("asset:NVDA", "bullish", "bullish")),
        now_ms=_NOW,
    )
    assert guarded.final == "throttled" and guarded.override_rule == "watchlist_objective_guard"


def test_policy_v13_budget_reversal_exemption_reads_past_a_non_directional_card() -> None:
    """#523 D2. The reversal exemption compares against the newest *directional* delivered card.

    v12 compared against the newest delivered card whatever its direction, so one neutral card landing on a
    key hid a real reversal behind it: "Russia will raise output" was withheld against a "will cut output"
    card 55 minutes earlier. Reading past non-directional cards released 5 more cards over the 2888-judgment
    replay, all genuine reversals, at most one per key per hour. Only the newest directional card is
    consulted; "against any delivered card" would have released 101 and let 10 escape on one key in an hour.
    """

    key = "conflict:mideast_2026"
    bullish = _verdict(scope="macro", assets=[], direction="bullish", headline_zh="俄罗斯宣布增产")

    # [neutral newest, bearish older] + bullish candidate: the budget is spent, and the newest card the
    # reader could read a direction from is the bearish one this contradicts.
    behind_neutral = storyline_status(key, seen=_sent(key, "neutral", "bearish"))
    freed = decide(bullish, _NO_WATCHLIST, behind_neutral, now_ms=_NOW)
    assert freed.final == "push" and freed.throttled_by is None
    # Still only the newest *directional* one: newest bullish, older bearish, bullish candidate is no flip.
    same_direction = storyline_status(key, seen=_sent(key, "bullish", "bearish"))
    assert decide(bullish, _NO_WATCHLIST, same_direction, now_ms=_NOW).throttled_by == f"storyline:{key}:budget"
    # `unclear` and a direction-less row are not directions either, and are read past the same way.
    for hidden in ("unclear", ""):
        blind = storyline_status(key, seen=_sent(key, hidden, "bearish"))
        assert decide(bullish, _NO_WATCHLIST, blind, now_ms=_NOW).final == "push", hidden

    # The count is unchanged: a neutral card is still a card the reader received, so two of them spend the
    # budget and, with no directional card to contradict, nothing is exempt.
    neutral_only = storyline_status(key, seen=_sent(key, "neutral", "neutral"))
    spent = decide(bullish, _NO_WATCHLIST, neutral_only, now_ms=_NOW)
    assert spent.final == "throttled" and spent.throttled_by == f"storyline:{key}:budget"
    # Two neutrals plus the bearish card is three delivered cards, over budget, and still exempt as a flip.
    over_budget = storyline_status(key, seen=_sent(key, "neutral", "neutral", "bearish"))
    assert decide(bullish, _NO_WATCHLIST, over_budget, now_ms=_NOW).final == "push"
    # A neutral candidate is not a reversal of anything, whatever the ledger holds.
    assert (
        decide(bullish.model_copy(update={"direction": "neutral"}), _NO_WATCHLIST, behind_neutral, now_ms=_NOW).final
        == "throttled"
    )
    # Only in-window rows on this key are read: the directional card behind the neutral one still has to be
    # inside the budget window to be reversed, and an out-of-window ledger is under budget anyway.
    aged = storyline_status(key, seen=_sent(key, "neutral", "bearish", minutes_ago=90))
    assert decide(bullish, _NO_WATCHLIST, aged, now_ms=_NOW).final == "push"


def test_decide_escalate_needs_corroboration_and_a_corroborated_escalate_ignores_the_budget() -> None:
    """#504 D3. 92 of 126 escalates on 2026-09-02 were a single Item from a source of unknown authority."""

    big = _verdict(magnitude=3, scope="macro", assets=[], direction="bearish", headline_zh="伊朗议员称将报复美军")
    escalate = trade_relevance(reader_value="escalate")
    lone = replace(_NO_WATCHLIST, member_count=1)

    claim = production_decide(scored_judgment(big, relevance=escalate), lone, None)
    assert claim.final == "push" and claim.override_rule == "trade_relevance_escalate_uncorroborated"
    assert OVERRIDE_RULE_ZH["trade_relevance_escalate_uncorroborated"]
    # Either corroboration keeps the escalate: a source of known authority, or a second independent arrival.
    wire = scored_judgment(big, relevance=escalate, taxonomy=news_taxonomy(source_authority="reputable_secondary"))
    assert production_decide(wire, lone, None).final == "escalate"
    merged = production_decide(scored_judgment(big, relevance=escalate), replace(lone, member_count=2), None)
    assert merged.final == "escalate" and merged.override_rule == "trade_relevance_escalate"
    # A grounded asset is not corroboration: a provider tag says which instrument, not that anyone confirmed it.
    tagged = replace(lone, grounded_assets=("CL", "XYZ-CL"))
    assert production_decide(scored_judgment(big, relevance=escalate), tagged, None).final == "push"

    # The downgraded card is an ordinary push from here on: the budget and similarity apply to it.
    key = "conflict:mideast_2026"
    spent = storyline_status(key, seen=_sent(key, "bearish", "bearish"))
    budgeted = production_decide(scored_judgment(big, relevance=escalate), lone, spent, now_ms=_NOW)
    assert budgeted.final == "throttled" and budgeted.throttled_by == f"storyline:{key}:budget"
    assert budgeted.override_rule == "trade_relevance_escalate_uncorroborated"
    # A corroborated escalate is the card the budget makes room for.
    assert production_decide(wire, lone, spent, now_ms=_NOW).final == "escalate"


def test_decide_drops_a_single_name_fact_that_names_no_instrument() -> None:
    """#504 PR-A: 197 single-name pushes a day named no primary at all. The check is only "is there a primary";
    the instrument universe is never consulted, so a Hong Kong ticker the universe cannot list still passes."""

    nameless = _verdict(assets=[], headline_zh="某初创公司完成种子轮融资")
    dropped = decide(nameless, _NO_WATCHLIST, None)
    assert dropped.final == "drop" and dropped.override_rule == "single_name_without_instrument"
    assert OVERRIDE_RULE_ZH["single_name_without_instrument"]
    # Any primary passes, grounded or not, on a venue or not.
    for symbol in ("GPRO", "02015.HK"):
        named = decide(
            nameless.model_copy(update={"assets": [TriageAsset(symbol=symbol, role="primary")]}), _NO_WATCHLIST, None
        )
        assert named.final == "push" and named.override_rule == "trade_relevance_realtime", symbol
    # A merely mentioned asset is not a primary.
    affected = decide(
        nameless.model_copy(update={"assets": [TriageAsset(symbol="SPY", role="mentioned")]}), _NO_WATCHLIST, None
    )
    assert affected.final == "drop" and affected.override_rule == "single_name_without_instrument"
    # Only a realtime single-name verdict: macro/sector scope, a corroborated escalate, a watchlist or listing
    # guard are untouched.
    assert decide(nameless.model_copy(update={"scope": "macro"}), _NO_WATCHLIST, None).final == "push"
    assert decide(nameless.model_copy(update={"scope": "sector"}), _NO_WATCHLIST, None).final == "push"
    corroborated = scored_judgment(
        nameless.model_copy(update={"magnitude": 3}),
        relevance=trade_relevance(reader_value="escalate"),
        taxonomy=news_taxonomy(source_authority="reputable_secondary"),
    )
    assert production_decide(corroborated, _NO_WATCHLIST, None).final == "escalate"
    assert decide(nameless, _FACTS, None).override_rule == "watchlist_objective_guard"
    assert decide(nameless, replace(_NO_WATCHLIST, admission="listing_deterministic"), None).final == "push"


def test_decide_uses_content_duplicate_evidence_without_a_reader_quota() -> None:
    """Prior volume on the *reader* never blocks a card; only evidence that the reader already got this fact
    (or, since v12, this storyline's budget) can."""

    # Nothing in the window to compare against: the reader received nothing, so nothing can be a repeat.
    empty = decide(_verdict(), _FACTS, _busy())
    assert empty.final == "push" and empty.override_rule == "watchlist_objective_guard" and empty.throttled_by is None
    assert empty.seen_similarity == 0.0 and empty.seen_against == -1

    seen = _busy(seen_headlines=("英伟达发布 Blackwell Ultra，单卡算力翻倍",), seen_event_ids=("evt-a",))
    # A card about something else goes out regardless of prior volume.
    released = decide(_verdict(headline_zh="美联储会议纪要显示多数官员倾向 9 月降息"), _FACTS, seen)
    assert released.final == "push" and released.override_rule == "watchlist_objective_guard"
    assert released.seen_similarity is not None and released.seen_similarity < 0.25

    # A near-repeat of that card does not, whatever the model called its novelty.
    repeat = decide(_verdict(headline_zh="英伟达发布 Blackwell Ultra 芯片，单卡算力翻倍"), _FACTS, seen)
    assert repeat.final == "throttled" and repeat.throttled_by == "storyline:asset:NVDA:seen"
    assert repeat.seen_similarity is not None and repeat.seen_similarity >= 0.25
    assert repeat.seen_against == 0
    for novelty in ("new_fact", "progression"):
        claimed = decide(
            _verdict(novelty=novelty, headline_zh="英伟达发布 Blackwell Ultra 芯片，单卡算力翻倍"), _FACTS, seen
        )
        assert claimed.final == "throttled", novelty

    # Disabling similarity does not restore a hidden count cap.
    duplicate_off = decide(
        _verdict(headline_zh="英伟达发布 Blackwell Ultra 芯片，单卡算力翻倍"),
        _FACTS,
        seen,
        policy=DecidePolicy(similarity_max=0.0),
    )
    assert duplicate_off.final == "push" and duplicate_off.throttled_by is None


def test_storyline_status_carries_only_content_evidence() -> None:
    """Every field is receipt evidence about delivered cards. `seen_at_ms`/`seen_keys` (#504) are when and under
    which storyline key each delivered card settled — the budget ledger — never a count or capacity field."""

    assert {item.name for item in fields(StorylineStatus)} == {
        "key",
        "told_directions",
        "told_assets",
        "seen_headlines",
        "seen_event_ids",
        "seen_directions",
        "seen_assets",
        "seen_at_ms",
        "seen_keys",
    }
    status = storyline_status("asset:BTC", seen=[_told_row("a", 5, storyline_key="topic:rates"), _told_row("b", 3)])
    assert status.seen_at_ms == (5, 3) and status.seen_keys == ("topic:rates", "asset:BTC")


def _told_row(event_id: str, at_ms: int, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_id": event_id,
        "at_ms": at_ms,
        "storyline_key": "asset:BTC",
        "comparison_title": "",
        "comparison_fingerprint": "",
        "dedupe_family": "general",
        "magnitude": 2,
        "direction": "bullish",
        "headline_zh": event_id,
        "why_zh": "",
        "grounded_assets": [],
        "assets": [],
    }
    row.update(overrides)
    return row


def _select(rows: Sequence[Mapping[str, Any]], **overrides: Any) -> Any:
    from tracefold.news.told_context import ToldLedgerSnapshot

    kwargs: dict[str, Any] = {
        "now_ms": _NOW,
        "storyline_key": "topic:rates",
        "symbols": (),
        "comparison_title": "",
        "exclude_event_id": "self",
    }
    kwargs.update(overrides)
    return ToldLedgerSnapshot.select(rows, **kwargs)


_NOW = 1_800_000_000_000


def test_compacting_the_model_context_never_narrows_the_rule_side_duplicate_evidence() -> None:
    """The Program sees <= 12 selected rows; `decide()` measures duplicates against the whole bounded window.

    #81 widened that comparison on purpose. #138 changes only which rows the model reads, so a card that
    resembles the 40th-newest sent card must still be withheld even though the model never saw it.
    """

    seen = [
        {"headline_zh": f"无关卡片 {i}", "direction": "neutral", "event_id": f"e{i}", "grounded_assets": []}
        for i in range(40)
    ] + [
        {
            "headline_zh": "英伟达投资 OpenAI 数据中心",
            "direction": "bullish",
            "event_id": "old",
            "grounded_assets": ["NVDA"],
        }
    ]
    told = seen[:12]

    status = storyline_status("asset:NVDA", told=told, seen=seen)
    repeat = _verdict(headline_zh="英伟达投资 OpenAI 数据中心", direction="bullish", novelty="new_fact")
    decision = decide(repeat, _FACTS, status)

    assert decision.final == "throttled" and decision.throttled_by.endswith(":seen")
    assert decision.seen_against == 40 and len(status.seen_headlines) == 41
    # The model-visible slice is unchanged by that: it stays the selector's twelve.
    assert len(status.told_directions) == 12


def test_told_selector_ranks_the_candidates_own_storyline_above_every_unrelated_recent_card() -> None:
    """The old selector reserved six same-key slots and filled the rest by recency. In production that reserve
    was saturated on 61% of judgments: the model was shown unrelated cards while a same-storyline card it
    needed for novelty stayed outside the window. Tiers are an ordered union, not a quota."""

    from tracefold.news.told_context import TOLD_MAX, TOLD_STORYLINE_TIER_MAX

    same = [_told_row(f"s{i}", _NOW - (30 + i) * 60_000, storyline_key="topic:rates") for i in range(10)]
    unrelated = [_told_row(f"o{i}", _NOW - i * 60_000, storyline_key=NO_STORYLINE_KEY) for i in range(10)]

    entries = _select(unrelated + same).entries
    assert len(entries) == TOLD_MAX
    # The storyline tier wins the top slots even though every unrelated card is newer, but it is capped so the
    # tiers below it stay reachable. Its overflow is then offered the remaining slots *before* any recency
    # filler: filler must never displace evidence, or an unrelated delivery would change what the model saw.
    assert [entry.event_id for entry in entries[:TOLD_STORYLINE_TIER_MAX]] == [
        f"s{i}" for i in range(TOLD_STORYLINE_TIER_MAX)
    ]
    assert [entry.event_id for entry in entries[TOLD_STORYLINE_TIER_MAX:10]] == ["s8", "s9"]
    assert [entry.event_id for entry in entries[10:]] == [f"o{i}" for i in range(TOLD_MAX - 10)]
    assert [entry.i for entry in entries] == list(range(TOLD_MAX))


def test_recency_filler_never_displaces_evidence_the_model_needs() -> None:
    """A dense storyline plus a trickle of unrelated cards: every slot goes to evidence.

    This is what keeps an unrelated delivery from buying a second paid execution — if filler could take a slot
    an evidence row wanted, a new unrelated card would change the evidence set and force a re-ask.
    """

    from tracefold.news.told_context import TOLD_MAX

    dense = [_told_row(f"s{i}", _NOW - (30 + i) * 60_000, storyline_key="topic:rates") for i in range(TOLD_MAX + 4)]
    filler = [_told_row(f"o{i}", _NOW - i * 60_000, storyline_key=NO_STORYLINE_KEY) for i in range(3)]
    entries = _select(filler + dense).entries
    assert [entry.tier for entry in entries] == ["storyline"] * TOLD_MAX
    # Adding one more unrelated card changes nothing the model sees.
    grew = _select([_told_row("new", _NOW, storyline_key=NO_STORYLINE_KEY), *filler, *dense]).entries
    assert [entry.event_id for entry in grew] == [entry.event_id for entry in entries]


def test_told_selector_overflow_from_a_capped_tier_still_fills_leftover_slots() -> None:
    """The cap yields to other tiers; it does not throw rows away."""

    from tracefold.news.told_context import TOLD_MAX, TOLD_STORYLINE_TIER_MAX

    same = [_told_row(f"s{i}", _NOW - i * 60_000, storyline_key="topic:rates") for i in range(TOLD_MAX + 4)]
    entries = _select(same).entries
    assert len(entries) == TOLD_MAX
    assert [entry.event_id for entry in entries] == [f"s{i}" for i in range(TOLD_MAX)]
    assert all(entry.tier == "storyline" for entry in entries)
    assert TOLD_STORYLINE_TIER_MAX < TOLD_MAX


def test_told_selector_finds_the_same_instrument_under_a_different_storyline_key() -> None:
    """16% of judgments end on a different final storyline key than the preliminary one they were selected
    with, and a prior card about the same instrument can sit under any of them. Symbol sets answer that;
    storyline keys alone do not."""

    rows = [_told_row(f"noise{i}", _NOW - i * 60_000, storyline_key="topic:trade") for i in range(11)] + [
        _told_row("oil", _NOW - 60 * 60_000, storyline_key="conflict:mideast_2026", grounded_assets=["CL"])
    ]

    entries = _select(rows, storyline_key=NO_STORYLINE_KEY, symbols=("CL", "XYZ-CL")).entries
    matched = next(entry for entry in entries if entry.event_id == "oil")
    assert matched.tier == "asset_overlap"
    # An hour-old card about this instrument outranks every fresher unrelated one.
    assert entries[0].event_id == "oil"
    assert matched.symbols == ("CL",)


def test_told_selector_uses_normalized_comparison_titles_not_reader_headlines() -> None:
    rows = [_told_row(f"noise{i}", _NOW - i * 60_000, storyline_key="topic:trade") for i in range(12)] + [
        _told_row(
            "same-fact",
            _NOW - 90 * 60_000,
            storyline_key="asset:NVDA",
            comparison_title="nvidia to invest usd_100000000000 in openai data centre",
            headline_zh="英伟达投资 OpenAI",
        )
    ]
    entries = _select(
        rows,
        storyline_key=NO_STORYLINE_KEY,
        comparison_title="nvidia to invest usd_100000000000 in openai data centre",
    ).entries
    assert entries[0].event_id == "same-fact" and entries[0].tier == "fact_similarity"
    assert entries[0].similarity == 1.0
    # Below the retrieval threshold nothing is promoted out of the recency tail.
    weak = _select(rows, storyline_key=NO_STORYLINE_KEY, comparison_title="an entirely unrelated sentence").entries
    assert all(entry.tier == "recency" for entry in weak)


def test_told_selector_trusts_upstream_bounds_and_drops_duplicate_and_self_rows_before_ranking() -> None:
    rows = [
        _told_row("keep", _NOW - 60_000),
        _told_row("keep", _NOW - 120_000),  # same Event twice in the ledger
        _told_row("expired", _NOW - 5 * 3_600_000),
        _told_row("self", _NOW - 30_000),
        _told_row("", _NOW - 30_000),
    ]
    snapshot = _select(rows)
    assert [entry.event_id for entry in snapshot.entries] == ["keep", "expired"]
    assert snapshot.source_count == 2


def test_told_selector_trusts_bounded_history_and_prioritizes_targeted_exact_fact() -> None:
    from tracefold.news.pipeline.triage_audit import _told_from_context, _told_trace
    from tracefold.news.program.contracts import TriageContext

    targeted = _told_row(
        "overnight-exact",
        _NOW - 24 * 3_600_000,
        history_scope="targeted",
        retrieval_reason="exact_fingerprint",
    )
    recent = _told_row("recent", _NOW - 60_000, storyline_key="topic:rates")

    snapshot = _select([recent, targeted])

    assert [entry.event_id for entry in snapshot.entries] == ["overnight-exact", "recent"]
    assert snapshot.entries[0].tier == "exact_fact"
    assert snapshot.entries[0].history_scope == "targeted"
    assert snapshot.entries[0].retrieval_reason == "exact_fingerprint"
    visible = {
        "i",
        "ago_min",
        "storyline_key",
        "comparison_title",
        "symbols",
        "magnitude",
        "direction",
        "headline_zh",
        "why_zh",
    }
    context = TriageContext.from_card(
        {
            "event_id": "self",
            "evidence_version": 3,
            "evidence_sha256": "e" * 64,
            "focus_fact_id": "fact",
            "leader_title": "current",
            "opened_at_ms": _NOW,
            "storyline_key": "topic:rates",
            "dedupe_family": "general",
        },
        watchlist=(),
        told_rows=[recent, targeted],
        now_ms=_NOW,
        queue_lag_ms=0,
    )
    assert set(context.event_semantics_payload()["event_status"]["told"][0]) == visible
    audit_told = _told_from_context(context)
    assert audit_told == [entry.model_dump(mode="json") for entry in context.told.entries]
    assert _told_trace(audit_told) == audit_told
    assert audit_told[0]["ago_min"] == 1_440


def test_told_source_contract_rejects_unowned_taxonomy() -> None:
    from tracefold.news.program.contracts import TriageContext

    card = {
        "event_id": "self",
        "evidence_version": 3,
        "evidence_sha256": "e" * 64,
        "focus_fact_id": "fact",
        "leader_title": "current",
        "opened_at_ms": _NOW,
        "storyline_key": "topic:rates",
        "dedupe_family": "general",
    }
    prior = _told_row(
        "prior",
        _NOW - 60_000,
        storyline_key="topic:rates",
    )

    with pytest.raises(ValueError, match="news_told_context_fields_unexpected:taxonomy"):
        TriageContext.from_card(
            card,
            watchlist=(),
            told_rows=[{**prior, "taxonomy": {"event_family": "regulatory_legal"}}],
            now_ms=_NOW,
            queue_lag_ms=0,
        )


def test_told_selector_keeps_a_targeted_canonical_alias_inside_a_dense_pool() -> None:
    alias_target = _told_row(
        "alias-target",
        _NOW - 24 * 3_600_000,
        storyline_key="asset:9988",
        grounded_assets=["9988"],
        canonical_assets=["BABA"],
        history_scope="targeted",
        retrieval_reason="canonical_asset_overlap",
    )
    recent = [
        _told_row(f"recent-{index:02d}", _NOW - index * 60_000, storyline_key="topic:unrelated") for index in range(16)
    ]

    entries = _select([*recent, alias_target], storyline_key="asset:BABA", symbols=("BABA",)).entries

    assert entries[0].event_id == "alias-target"
    assert entries[0].tier == "asset_overlap"
    assert entries[0].retrieval_reason == "canonical_asset_overlap"


def test_told_selector_is_deterministic_under_equal_timestamps_and_input_order() -> None:
    rows = [_told_row(f"e{i}", _NOW - 60_000) for i in range(20)]
    first = _select(rows).entries
    second = _select(list(reversed(rows))).entries
    assert [entry.event_id for entry in first] == [entry.event_id for entry in second]
    # Equal tier, equal similarity, equal time: the stable Event identity breaks the tie.
    assert [entry.event_id for entry in first] == sorted(entry.event_id for entry in first)


def test_composite_retrieval_identity_binds_reader_history_and_selector_behaviour() -> None:

    from tracefold.news import told_context as contract
    from tracefold.news.artifact_identity import canonical_sha

    assert len(contract.TOLD_SELECTOR_SHA256) == 64
    assert len(contract.NEWS_RETRIEVAL_SHA256) == 64
    assert contract.NEWS_RETRIEVAL_SHA256 != contract.TOLD_SELECTOR_SHA256
    payload = {
        "tier_order": list(contract.TOLD_TIER_ORDER),
        "source_max": contract.TOLD_SOURCE_MAX,
        "visible_cap": contract.TOLD_MAX,
        "similarity_min": contract.TOLD_FACT_SIMILARITY_MIN,
    }
    # Every one of these is inside the hashed payload, so changing any of them moves the bundle identity.
    assert all(canonical_sha({**payload, key: "changed"}) != canonical_sha(payload) for key in payload)


def test_storyline_status_carries_told_directions() -> None:
    told = [
        {"i": 0, "direction": "bullish", "symbols": ["BTC"], "headline_zh": "a"},
        {"i": 1, "direction": "neutral", "symbols": [], "headline_zh": "b"},
    ]
    status = storyline_status("asset:BTC", told=told)
    assert status.told_directions == ("bullish", "neutral") and status.told_count == 2
    assert status.told_assets == (frozenset({"BTC"}), frozenset())
    assert storyline_status("asset:BTC").told_count == 0


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


def test_fallback_is_not_silent() -> None:
    weak = GateFacts(
        grounded_assets=("AMD",),
        watchlist_symbols=frozenset(),
        admission="candidate",
    )
    assert rule_baseline(weak) == "drop"
    judgment = fallback_verdict(weak, error_code="news_program_route_deadline")
    assert judgment.decision.final == "drop" and judgment.verdict.headline_zh
    assert judgment.error_code == "news_program_route_deadline"
    assert set(judgment.verdict.model_dump(mode="json")) == {
        "novelty",
        "restates",
        "assets",
        "direction",
        "scope",
        "magnitude",
        "confidence",
        "audience",
        "headline_zh",
        "why_zh",
    }
    assert judgment.judgment_sha256 == fallback_verdict(weak, error_code="news_program_route_deadline").judgment_sha256

    # v10 degraded handling fails open only for objective facts. Queue priority and provider score are not
    # editorial evidence and cannot enter GateFacts at all.
    listing = GateFacts(
        grounded_assets=(),
        watchlist_symbols=frozenset(),
        admission="listing_deterministic",
    )
    assert rule_baseline(listing) == "push"
    assert fallback_verdict(listing, error_code="x").decision.override_rule == "degraded_listing_objective"
    strong = GateFacts(
        grounded_assets=("BTC",),
        watchlist_symbols=frozenset({"BTC"}),
        admission="candidate",
    )
    assert fallback_verdict(strong, error_code="x").decision.final == "push"
    assert (
        fallback_verdict(strong, error_code="x", title="  Fed  holds rates\nsteady ").verdict.headline_zh
        == "Fed holds rates steady"
    )
    assert fallback_verdict(strong, error_code="x").verdict.headline_zh == "模型不可用（规则兜底）"


# ---------------------------------------------------------------- delivery / bus
def test_card_is_the_reader_contract() -> None:
    assert sanitize_ai_text("看 https://evil.example 这里", limit=60, fallback="原标题") == "原标题"
    assert sanitize_ai_text("**加粗** @user 文本\x00", limit=60) == "加粗 文本"
    card = render_first_card(
        event={
            "event_id": "e1",
            "leader_title": "Nvidia to invest $100bn",
            "leader_url": "https://ft.com/x",
            "reporting_origin": "ft",
            "member_count": 3,
            "provider_score_max": 80,
            "leader_published_at_ms": 1787064000000,  # 2026-08-18 14:40 UTC -> 22:40 in the reader's zone
        },
        verdict={
            "direction": "bullish",
            "magnitude": 2,
            "headline_zh": "英伟达千亿美元投资 OpenAI 数据中心",
            "why_zh": "英伟达把千亿美元投进 OpenAI 的俄亥俄数据中心，算力供给链再加码",
            "scope": "single_name",
            "assets": [{"symbol": "NVDA", "role": "primary"}, {"symbol": "OPENAI", "role": "mentioned"}],
        },
        decision="push",
        grounded_assets=["NVDA", "XYZ-NVDA"],
    )
    # header = the model's factual headline; body = why sentence + facts in words; nothing else.
    assert card["header"]["title"]["content"] == "英伟达千亿美元投资 OpenAI 数据中心"
    assert card["elements"][0]["content"].splitlines() == [
        "英伟达把千亿美元投进 OpenAI 的俄亥俄数据中心，算力供给链再加码",
        "利多 · 影响明显 · NVDA · ft（3 条报道） · 22:40",
    ]
    text = json.dumps(card, ensure_ascii=False)
    for machine_word in (
        "AI 初判",
        "类型：",
        "范围：",
        "成员：",
        "Provider",
        "single_name",
        "原标题",
        "个别标的",
    ):
        assert machine_word not in text
    assert "Nvidia to invest $100bn" not in text
    assert "打开来源" in text and "news_delivery_card" not in text
    escalated = render_first_card(
        event={"event_id": "e1", "leader_title": "Nvidia to invest $100bn", "member_count": 1},
        verdict={"direction": "bullish", "magnitude": 3, "headline_zh": "x https://x.y"},
        decision="escalate",
        grounded_assets=[],
    )
    assert escalated["header"]["title"]["content"] == "⚡ Nvidia to invest $100bn"  # URL in AI copy -> code fallback
    assert escalated["elements"][0]["content"] == "利多 · 影响重大 · -"
    # Degraded (model chain failed, rule baseline pushes): the wire text itself, no verdict words the model never gave.
    degraded = render_first_card(
        event={
            "event_id": "e2",
            "leader_title": "BREAKING: SEC approves spot **ETH** ETF options https://x.y/z",
            "leader_description": "The SEC approved options on spot ether ETFs on Thursday.\nMore to follow.",
            "leader_url": "https://x.y/z",
            "reporting_origin": "wire",
            "member_count": 1,
            "leader_published_at_ms": 1787064000000,
        },
        verdict={"direction": "neutral", "magnitude": 2, "headline_zh": "BREAKING: SEC approves spot ETH ETF options"},
        decision="escalate",
        grounded_assets=["ETH"],
        degraded=True,
    )
    assert degraded["header"]["title"]["content"] == "⚡ BREAKING: SEC approves spot ETH ETF options"
    assert degraded["header"]["template"] == "grey"
    assert degraded["elements"][0]["content"].splitlines() == [
        "The SEC approved options on spot ether ETFs on Thursday. More to follow.",
        "ETH · wire · 22:40",
    ]
    assert "模型" not in json.dumps(degraded, ensure_ascii=False) and "中性" not in json.dumps(
        degraded, ensure_ascii=False
    )
    # Card assets are the verdict primaries the Gate grounded; a small grounded set shows when the model named none.
    assert card_assets({"assets": [{"symbol": "CC", "role": "primary"}]}, ["CC"]) == ["CC"]
    assert card_assets({"assets": []}, ["A", "B", "C", "D", "E"]) == []
    assert card_assets({"assets": [{"symbol": "BTC", "role": "primary"}]}, ["BTC", "CL", "XYZ-CL"]) == ["BTC"]


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
    card = render_first_card(
        event={"event_id": "e1", "leader_title": "t", "reporting_origin": "jin10", "member_count": 1},
        verdict={"direction": "bearish", "magnitude": 3, "headline_zh": "标题"},
        decision="push",
        grounded_assets=["CL"],
        **overrides,
    )
    return card["elements"][0]["content"].splitlines()


def test_card_market_line_is_display_only() -> None:
    # The market's own number, on its own line, for the assets the facts line already named (#113).
    assert _market_lines(quotes=[_quote("CL", "86.43", 2.296, instrument_class="commodity")]) == [
        "利空 · 影响重大 · CL · jin10",
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
        assert _market_lines(quotes=[_quote("CL", "86.43", 2.3, state=absent)]) == ["利空 · 影响重大 · CL · jin10"]
    assert _quote_line([_quote("X", "0", 1.0)]) == "" and _quote_line([_quote("X", "not-a-price", 1.0)]) == ""
    # `parse_price` bounds a price to finite-and-positive, not to a magnitude, and quantizing 1e40 raises.
    # `_quote_line` runs in the renderer, outside the consumer's guard, so it must lose the entry, not the card.
    assert _quote_line([_quote("HUGE", "1e40", 1.0)]) == ""
    assert (
        _quote_line([_quote("HUGE", "1e40", 1.0), _quote("BTC", "74757.60", 7.914)]) == "行情 BTC $74,757.60 24h +7.91%"
    )
    assert _quote_line([]) == "" and _market_lines() == ["利空 · 影响重大 · CL · jin10"]
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
    # A degraded card keeps its price: it is our fact, not the model's. The facts line still names no judgment.
    degraded = render_first_card(
        event={"event_id": "e2", "leader_title": "wire", "reporting_origin": "wire", "member_count": 1},
        verdict={"direction": "neutral", "magnitude": 2, "novelty": "progression", "headline_zh": "x"},
        decision="push",
        grounded_assets=["ETH"],
        degraded=True,
        quotes=[_quote("ETH", "2348.14", 4.252)],
    )
    assert degraded["elements"][0]["content"].splitlines()[-1] == "行情 ETH $2,348.14 24h +4.25%"
    assert "新进展" not in json.dumps(degraded, ensure_ascii=False)


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
        "利空 · 影响重大 · LRCX · jin10",
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

    assert set(_CHANGE_BASIS_LABEL) == set(CHANGE_BASIS_ZH)


def test_card_marks_a_progression() -> None:
    # 28.8% of a week's cards advanced a story the reader already had one for and the card said nothing (#113).
    card = render_first_card(
        event={"event_id": "e1", "leader_title": "t", "reporting_origin": "jin10", "member_count": 1},
        verdict={"direction": "bearish", "magnitude": 3, "novelty": "progression", "headline_zh": "标题"},
        decision="push",
        grounded_assets=["CL"],
    )
    assert card["elements"][0]["content"].splitlines() == ["利空 · 新进展 · 影响重大 · CL · jin10"]
    for quiet in ("new_fact", "restatement", "", None):
        verdict = {"direction": "bearish", "magnitude": 3, "novelty": quiet, "headline_zh": "标题"}
        card = render_first_card(
            event={"event_id": "e1", "leader_title": "t", "reporting_origin": "jin10", "member_count": 1},
            verdict=verdict,
            decision="push",
            grounded_assets=["CL"],
        )
        assert card["elements"][0]["content"].splitlines() == ["利空 · 影响重大 · CL · jin10"]


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
    report = replay_hits(hits, watchlist_symbols=frozenset({"BTC", "ETH", "NVDA"}))
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
    reply_report = replay_hits(replies, watchlist_symbols=frozenset())
    assert reply_report["counts"]["events"] == len(replies)
    # Binance CFX announcement burst collapses within each kind; news and listing never merge.
    cfx = [h for h in hits if "Conflux Network (CFX)" in str(h.get("text"))]
    cfx_report = replay_hits(cfx, watchlist_symbols=frozenset())
    assert cfx_report["counts"]["events"] == 2 and cfx_report["counts"]["exact_members"] == len(cfx) - 2
    # The Gate no longer decides relevance: most items reach Triage (the model is the semantic filter)
    assert report["candidate_share_of_items"] >= 0.65


# ---------------------------------------------------------------- recall regression (issue #53)
RECALL_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_recall_sample.json"
EXPECTATIONS = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_expectations.json"


def test_gate_expectations_over_the_recall_corpus() -> None:
    """Trajectory-prefix regression: every case names a real headline and the acceptable Gate outcome set."""

    hits = _hits() + json.loads(RECALL_FIXTURE.read_text(encoding="utf-8"))
    report = replay_hits(hits, watchlist_symbols=frozenset())
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
            verdict_primaries=["JNJ"],
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
            verdict_primaries=["XOM"],
            grounded_assets=["XOM"],
            dedupe_family="general",
        )
        == "conflict:mideast_2026"
    )


def _fresh(**seen: object) -> StorylineStatus:
    """A fresh storyline with optional sent-ledger evidence."""

    return StorylineStatus(key="asset:KLAC", **seen)  # type: ignore[arg-type]


_OKX_LEDGER = (
    "Ciena Corporation（$CIENx）出现在 OKX",
    "Salesforce（$CRMx）出现在 OKX",
    "康宁（$GLWx）出现在 OKX",
)


def test_decide_measures_every_ordinary_push_for_duplicate_evidence() -> None:
    """A fresh storyline is still compared with the sent ledger; counts do not participate."""

    duplicate = _verdict(headline_zh="KLA Corporation（$KLACx）出现在 OKX")
    window = _fresh(seen_headlines=_OKX_LEDGER, seen_event_ids=("a", "b", "c"))

    stopped = decide(duplicate, _FACTS, window)
    assert stopped.final == "throttled" and stopped.throttled_by == "storyline:asset:KLAC:seen"
    assert stopped.seen_scope == "all" and stopped.seen_similarity is not None
    assert stopped.seen_similarity >= DEFAULT_POLICY.similarity_max

    # A card about something else on the same fresh key still goes out, and is traced as measured.
    fresh_fact = decide(_verdict(headline_zh="美联储会议纪要显示多数官员倾向 9 月降息"), _FACTS, window)
    assert fresh_fact.final == "push" and fresh_fact.throttled_by is None
    assert fresh_fact.seen_scope == "all" and fresh_fact.seen_similarity is not None

    # A busy storyline uses the exact same content-only path.
    busy = _busy(seen_headlines=("英伟达发布 Blackwell Ultra，单卡算力翻倍",), seen_event_ids=("evt-a",))
    repeat = decide(_verdict(headline_zh="英伟达发布 Blackwell Ultra 芯片，单卡算力翻倍"), _FACTS, busy)
    assert repeat.throttled_by == "storyline:asset:NVDA:seen" and repeat.seen_scope == "all"


def test_decide_never_withholds_an_escalate_as_a_similarity_match() -> None:
    """Character bigrams over a fixed topic vocabulary mistake one Trump headline for another. On a magnitude-3
    card that mistake is not affordable, so `escalate` skips deterministic similarity."""

    told = ("特朗普称其政府已结束对加密的战争",)
    big = _verdict(magnitude=3, headline_zh="特朗普称美国正考虑购买大量比特币及其他加密资产")
    facts = replace(_FACTS, watchlist_symbols=frozenset(), member_count=2)  # corroborated (#504 D3)
    on_fresh = decide(
        big,
        facts,
        _fresh(seen_headlines=told, seen_event_ids=("a",)),
        relevance={"reader_value": "escalate"},
    )
    assert on_fresh.final == "escalate" and on_fresh.throttled_by is None
    # #491: the comparison is still made and recorded, so the exemption is observable in the trace.
    assert on_fresh.seen_scope == "all" and on_fresh.seen_similarity is not None and on_fresh.seen_against == 0

    # Even identical text cannot make the content heuristic veto an escalation.
    hot = StorylineStatus(
        key="asset:NVDA",
        seen_headlines=("特朗普称美国正考虑购买大量比特币及其他加密资产",),
        seen_event_ids=("a",),
    )
    repeat = decide(big, facts, hot, relevance={"reader_value": "escalate"})
    assert repeat.final == "escalate" and repeat.seen_scope == "all" and repeat.seen_similarity == 1.0
    assert repeat.throttled_by is None


def test_decide_leaves_the_switched_off_similarity_path_alone() -> None:
    """`similarity_max = 0` is the operator switching the content judgment off."""

    window = _fresh(seen_headlines=_OKX_LEDGER, seen_event_ids=("a", "b", "c"))
    duplicate = _verdict(headline_zh="KLA Corporation（$KLACx）出现在 OKX")

    off = decide(duplicate, _FACTS, window, policy=DecidePolicy(similarity_max=0.0))
    assert off.final == "push" and off.seen_similarity is None and off.seen_scope == ""


def test_decide_never_withholds_a_reversal_as_a_duplicate() -> None:
    """Character bigrams are blind to negation: "SEC 批准…" and "SEC 拒绝…" score 0.60, well over
    `similarity_max`. The duplicate check must exempt a direction flip using the matched sent-ledger row."""

    approved = "SEC 批准以太坊现货 ETF"
    window = _fresh(seen_headlines=(approved,), seen_event_ids=("evt-a",), seen_directions=("bullish",))
    reversal = _verdict(headline_zh="SEC 拒绝以太坊现货 ETF", direction="bearish")
    assert similarity(reversal.headline_zh, approved) >= DEFAULT_POLICY.similarity_max  # it does look alike

    flipped = decide(reversal, _FACTS, window)
    assert flipped.final == "push" and flipped.throttled_by is None
    assert flipped.seen_similarity is not None  # measured, and then exempted — not skipped

    # Same wording, same direction: still a duplicate.
    repeat = decide(_verdict(headline_zh="SEC 批准以太坊现货 ETF 上市", direction="bullish"), _FACTS, window)
    assert repeat.final == "throttled" and repeat.throttled_by == "storyline:asset:KLAC:seen"

    # Neutral on either side is not a reversal; a ledger with no directions never exempts.
    neutral = decide(_verdict(headline_zh="SEC 拒绝以太坊现货 ETF", direction="neutral"), _FACTS, window)
    assert neutral.final == "throttled"
    blind = _fresh(seen_headlines=(approved,), seen_event_ids=("evt-a",))
    assert decide(reversal, _FACTS, blind).final == "throttled"

    # Prior volume changes nothing; the matched direction still exempts it.
    hot = _busy(seen_headlines=(approved,), seen_event_ids=("evt-a",), seen_directions=("bullish",))
    assert decide(reversal, _FACTS, hot).final == "push"


def test_final_storyline_key_only_accepts_symbol_shaped_primaries() -> None:
    """`TriageAsset.symbol` is free text and this fallback is reached when nothing grounded it, so it is the least
    validated string in the pipeline — and it becomes a duplicate-comparison group, an advisory-lock key and a
    console label."""

    def key(primaries: list[str], **over: object) -> str:
        return final_storyline_key(
            title=str(over.get("title", "Some exchange notice")),
            headline_zh="",
            scope="single_name",
            verdict_primaries=primaries,
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

    assert not _symbol_in_text("NOT", "Trump says he will not raise tariffs on Canada")
    assert not _symbol_in_text("ME", "show me the money")
    assert not _symbol_in_text("ID", "no id required")
    assert _symbol_in_text("NOT", "NOT holders vote on the treasury")
    assert _symbol_in_text("OKB", "强生（$OKB）出现在 OKX")
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


def test_invalid_model_headline_falls_back_to_the_wire_title() -> None:
    card = render_first_card(
        event={"event_id": "e1", "leader_title": "Nvidia to invest $100bn", "reporting_origin": "ft"},
        verdict={
            "direction": "bullish",
            "magnitude": 2,
            "headline_zh": "看 https://evil.example",
            "why_zh": "算力供给链再加码",
            "assets": [],
        },
        decision="push",
        grounded_assets=[],
    )

    assert card["header"]["title"]["content"] == "Nvidia to invest $100bn"
