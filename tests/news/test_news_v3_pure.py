"""Pure-module tests for News V3: titles, gate, storyline, rules, minhash, delivery, bus."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest

from tracefold.news import bus
from tracefold.news.delivery import _CHANGE_BASIS_LABEL, _quote_line, card_assets, render_first_card, sanitize_ai_text
from tracefold.news.eval.replay import replay_hits
from tracefold.news.facts import extract_fact_units
from tracefold.news.gate import GateInput, evaluate_gate, grounded_assets
from tracefold.news.minhash import BANDS, band_keys, estimate_jaccard, minhash_signature
from tracefold.news.models import ReaderReceipt, TriageAsset, TriageVerdict
from tracefold.news.opennews import source_artifact_identity
from tracefold.news.outcome import OVERRIDE_RULE_ZH, throttled_by_zh
from tracefold.news.pricing import CHANGE_BASIS_ZH
from tracefold.news.similarity import similarity
from tracefold.news.storyline import (
    _symbol_in_text,
    final_storyline_key,
    preliminary_storyline_key,
    storyline_key,
)
from tracefold.news.titles import extract_title
from tracefold.news.tokens import comparison_tokens, jaccard
from tracefold.news.triage_rules import (
    DEFAULT_POLICY,
    STALE_SOURCE_KEY,
    DecidePolicy,
    GateFacts,
    StorylineStatus,
    decide,
    fallback_verdict,
    rule_baseline,
    storyline_status,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_sample.json"


def _hits() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


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

    base = dict(strategy_ids=("9999",), coins=(), ingest_mode="live", watchlist_symbols=frozenset())
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
    base = dict(
        strategy_ids=("1018",), provider_score=75.0, coins=(), ingest_mode="live", watchlist_symbols=frozenset({"BTC"})
    )
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
    # The optional low-signal switch only affects ungrounded, non-macro social posts under 70.
    low = evaluate_gate(
        GateInput(
            title="Imagine being this guy",
            engine_type="meme",
            **{**base, "provider_score": 60.0, "suppress_low_signal": True},
        )
    )
    assert low.admission == "suppressed_low_signal" and "ungrounded_social_below_min_score" in low.reasons
    macro = evaluate_gate(
        GateInput(title="U.S. 30-Year Treasury Yield Climbs to 5.32%, Highest Since 2007", engine_type="news", **base)
    )
    assert macro.admission == "candidate" and macro.asset_class == "macro" and macro.priority == "high"
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
    assert listing.admission == "listing_deterministic" and listing.priority == "high"
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
    assert watch.admission == "candidate" and watch.watchlist_hits == ("BTC",) and watch.priority == "high"


# ---------------------------------------------------------------- storyline
def test_storyline_keys() -> None:
    assert (
        storyline_key(
            title="Trump threatens to bomb Oman", headline_zh="", scope="macro", primary_assets=["CL"], family="general"
        )
        == "theme:mideast_energy"
    )
    assert (
        storyline_key(
            title="Nvidia to invest $100bn",
            headline_zh="",
            scope="single_name",
            primary_assets=["NVDA"],
            family="general",
        )
        == "asset:NVDA"
    )
    assert (
        preliminary_storyline_key(
            title="US 30-year yield hits 5.32%", grounded_assets=(), asset_class="macro", family="general"
        )
        == "theme:rates"
    )
    # Bitcoin treasury companies are a crypto storyline, not a rates one; the final key follows the verdict.
    assert (
        final_storyline_key(
            title="Metaplanet to Invest 2,100 Bitcoin to Launch U.S. Bitcoin Treasury Platform",
            headline_zh="",
            scope="single_name",
            verdict_primaries=["BTC"],
            grounded_assets=["BTC"],
            family="general",
        )
        == "asset:BTC"
    )
    assert (
        final_storyline_key(
            title="Hyperscale Data Bitcoin Treasury at 276 Bitcoin",
            headline_zh="",
            scope="macro",
            verdict_primaries=[],
            grounded_assets=[],
            family="general",
        )
        == "theme:crypto_treasury"
    )
    # A BTC market wrap that mentions oil is BTC's storyline once Triage names BTC as primary.
    assert (
        final_storyline_key(
            title="Bitcoin pauses at $64,000 as rising yields, oil drag equities lower",
            headline_zh="",
            scope="sector",
            verdict_primaries=["BTC"],
            grounded_assets=["BTC", "CL", "XYZ-CL"],
            family="general",
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
            family="general",
        )
        == "theme:rates"
    )
    assert (
        preliminary_storyline_key(
            title="TABLE-U.S. July housing starts fall 12.4%", grounded_assets=(), asset_class="macro", family="general"
        )
        == "theme:us_macro_data"
    )


# ---------------------------------------------------------------- triage rules
def _verdict(**kw) -> TriageVerdict:
    base = dict(
        novelty="new_fact",
        event_type="partnership",
        assets=[TriageAsset(symbol="NVDA", role="primary")],
        direction="bullish",
        scope="single_name",
        magnitude=2,
        actionable=True,
        confidence=0.8,
        decision="push",
        headline_zh="英伟达投资",
        why_zh="",
    )
    base.update(kw)
    return TriageVerdict(**base)


_FACTS = GateFacts(
    grounded_assets=("NVDA", "XYZ-NVDA"),
    watchlist_symbols=frozenset({"NVDA"}),
    provider_score=80.0,
    priority="normal",
    admission="candidate",
)


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

    assert decide(_verdict(magnitude=3), stale, status).final == "escalate"
    # No artifact timestamp (every non-x/twitter frame) is not evidence of staleness.
    assert decide(_verdict(magnitude=2), replace(_FACTS, source_age_s=None), status).final == "push"
    # The knob turns it off without touching anything else.
    off = DecidePolicy(stale_source_max_age_s=0)
    assert decide(_verdict(magnitude=2), stale, status, policy=off).final == "push"


def test_high_priority_pushes_without_shouting() -> None:
    """#77: the Gate's `priority` is an AMQP transport hint, not a reader-facing importance judgment.

    The branch must stay a branch — it pushes without requiring `actionable` or min_push_magnitude, so deleting
    it would turn those Events into `below_threshold` drops rather than quieter pushes. Only loudness changes.
    """

    high = replace(_FACTS, priority="high")
    quiet = decide(_verdict(), high, None)
    assert quiet.final == "push" and quiet.override_rule == "high_priority_push"

    loud = decide(_verdict(), high, None, policy=replace(DEFAULT_POLICY, high_priority_escalates=True))
    assert loud.final == "escalate" and loud.override_rule == "high_priority_push"

    # Recall is preserved for the Events this branch exists to carry: not actionable, magnitude below the push
    # floor. Without the branch these fall through to `below_threshold`.
    weak = _verdict(actionable=False, magnitude=0)
    assert decide(weak, high, None).final == "push"
    assert decide(weak, _FACTS, None).final == "drop"

    # magnitude 3 still escalates on its own merit, independent of priority.
    assert decide(_verdict(magnitude=3), high, None).override_rule == "magnitude3"
    assert decide(_verdict(magnitude=3), _FACTS, None).final == "escalate"


def replace_magnitude(verdict: TriageVerdict, magnitude: int) -> TriageVerdict:
    return verdict.model_copy(update={"magnitude": magnitude})


def test_noise_only_vetoes_when_the_verdict_agrees_with_itself() -> None:
    """Policy v8: `noise` returned before every other rule, so one mislabelled enum outranked
    magnitude 3, Gate priority and the watchlist. The instruction defines noise as magnitude 0
    material, so a verdict that calls an Event noise and then gives it weight is contradicting
    itself and must fall through to the rules below instead of dropping on that label alone."""

    quiet_noise = _verdict(event_type="noise", magnitude=0, actionable=False, decision="drop")
    dropped = decide(quiet_noise, _FACTS, None)
    assert dropped.final == "drop" and dropped.override_rule == "noise"

    # Magnitude 1 is instruction-compliant for noise ("a routine update on one name that changes
    # nothing"), so it must still be vetoed. The ceiling is where the instruction says "clearly
    # tradable" instead.
    routine = _verdict(event_type="noise", magnitude=1, actionable=False, decision="drop")
    assert decide(routine, _FACTS, None).override_rule == "noise"

    # Weight the model gave the Event itself: magnitude above the ceiling, actionability, push intent.
    # Assert the outcome, not only the rule name: these fall through to real rules and the test has to
    # record which one, or a drop -> push change could pass unnoticed.
    for contradiction, expected in (
        ({"magnitude": 2}, ("push", "watchlist")),
        ({"magnitude": 2, "decision": "push", "actionable": True}, ("push", "model_push_actionable")),
        ({"actionable": True}, ("drop", "below_threshold")),
        ({"decision": "push"}, ("drop", "below_threshold")),
    ):
        fields = {"event_type": "noise", "magnitude": 0, "actionable": False, "decision": "drop", **contradiction}
        result = decide(_verdict(**fields), _FACTS, None)
        assert (result.final, result.override_rule) == expected, contradiction

    # Gate priority is upstream evidence the model did not produce; it survives a noise label.
    assert decide(quiet_noise, replace(_FACTS, priority="high"), None).override_rule != "noise"

    # Both halves stay operator-owned: raising the ceiling restores the pre-v8 veto.
    loud_noise = _verdict(event_type="noise", magnitude=2, actionable=False, decision="drop")
    assert decide(loud_noise, _FACTS, None).override_rule != "noise"
    restored = decide(loud_noise, _FACTS, None, policy=replace(DEFAULT_POLICY, noise_veto_max_magnitude=3))
    assert restored.final == "drop" and restored.override_rule == "noise"
    ignored = decide(
        quiet_noise,
        replace(_FACTS, priority="high"),
        None,
        policy=replace(DEFAULT_POLICY, noise_veto_respects_gate_priority=False),
    )
    assert ignored.override_rule == "noise"


def test_contested_high_priority_resolves_toward_the_reader() -> None:
    """Gate high priority plus the model's own magnitude >= 2, against the model's hold intent."""

    high = replace(_FACTS, priority="high")
    # No watchlist hit, so nothing else in decide() can carry this Event.
    off_watchlist = GateFacts(
        grounded_assets=("SPY",),
        watchlist_symbols=frozenset(),
        provider_score=40.0,
        priority="high",
        admission="candidate",
    )
    contested = _verdict(
        magnitude=2,
        decision="drop",
        actionable=False,
        event_type="macro",
        assets=[TriageAsset(symbol="SPY", role="primary")],
    )
    result = decide(contested, off_watchlist, None)
    assert result.final == "push" and result.override_rule == "contested_high_priority"

    # Never fires on a normal-priority Event, and never below the declared magnitude.
    assert decide(contested, replace(off_watchlist, priority="normal"), None).final == "drop"
    assert decide(replace_magnitude(contested, 1), off_watchlist, None).final == "drop"

    # Zero disables the rule without touching the older high_priority_push branch.
    off = decide(contested, off_watchlist, None, policy=replace(DEFAULT_POLICY, contested_push_min_magnitude=0))
    assert off.final == "drop"
    assert (
        decide(
            _verdict(decision="push"), high, None, policy=replace(DEFAULT_POLICY, contested_push_min_magnitude=0)
        ).override_rule
        == "high_priority_push"
    )


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
    told = [{"dir": "bullish", "headline_zh": "Coinbase 将上线狗狗币", "grounded_assets": ["DOGE"]}]
    status = storyline_status("asset:DOGE", told=told)
    admitted = replace(_FACTS, admission="listing_deterministic", grounded_assets=("BICO",))
    other_instrument = _verdict(
        event_type="listing",
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
    blind = storyline_status("asset:DOGE", told=[{"dir": "bullish", "headline_zh": "Coinbase 将上线狗狗币"}])
    assert decide(other_instrument, admitted, blind).override_rule == "restatement"

    # The model's own `event_type` is not evidence of a listing frame: only the Gate's admission is,
    # or any story the model keeps typing as `listing` escapes duplicate evidence on every repeat.
    typed_only = decide(other_instrument, replace(_FACTS, grounded_assets=("BICO",)), status)
    assert typed_only.final == "drop" and typed_only.override_rule == "restatement"

    # The exemption stays operator-owned.
    kept = decide(
        other_instrument, admitted, status, policy=replace(DEFAULT_POLICY, listing_exempt_from_duplicate=False)
    )
    assert kept.final == "drop" and kept.override_rule == "restatement"


def test_contested_high_priority_never_steals_a_model_push() -> None:
    """The rule is about a *hold* intent, so a model that asked to push or escalate keeps its own
    rule name — `pushed_by_rule` is the number this policy will be judged by."""

    high = replace(_FACTS, priority="high")
    assert decide(_verdict(decision="push"), high, None).override_rule == "high_priority_push"
    assert decide(_verdict(decision="escalate"), high, None).override_rule == "model_push_actionable"

    # And it does not sit above `unclear_direction`: an unclear event type outside
    # `unclear_push_event_types` still drops rather than riding Gate priority into the feed.
    unclear_rumor = _verdict(
        event_type="rumor",
        direction="unclear",
        magnitude=2,
        decision="drop",
        actionable=True,
        assets=[TriageAsset(symbol="SPY", role="primary")],
    )
    off_watchlist = replace(high, grounded_assets=("SPY",), watchlist_symbols=frozenset())
    result = decide(unclear_rumor, off_watchlist, None)
    assert result.final == "drop" and result.override_rule == "unclear_direction"


def test_decide_rules_and_throttle() -> None:
    assert decide(_verdict(), _FACTS, None).final == "push"
    # Policy v8: the default verdict is magnitude 2 / actionable / push, so a `noise` label on it is
    # self-contradicting and no longer vetoes. A noise verdict that agrees with itself still drops.
    assert decide(_verdict(event_type="noise"), _FACTS, None).final == "push"
    quiet_noise = decide(_verdict(event_type="noise", magnitude=0, actionable=False, decision="drop"), _FACTS, None)
    assert quiet_noise.final == "drop" and quiet_noise.override_rule == "noise"
    assert decide(_verdict(magnitude=3), _FACTS, None).final == "escalate"
    # Policy v2: the model's push intent on an actionable m1 single-name event is honoured.
    m1 = decide(_verdict(magnitude=1, assets=[TriageAsset(symbol="AMD", role="primary")]), _FACTS, None)
    assert m1.final == "push" and m1.override_rule == "model_push_actionable"
    assert decide(_verdict(magnitude=1, actionable=False), _FACTS, None).final == "push"  # watchlist primary m1
    assert (
        decide(
            _verdict(magnitude=1, actionable=False, assets=[TriageAsset(symbol="AMD", role="primary")]), _FACTS, None
        ).override_rule
        == "below_threshold"
    )
    assert decide(_verdict(magnitude=0), _FACTS, None).final == "drop"  # m0 never pushes, watchlist or not
    # Unclear direction: a clear event type at m>=2 pushes; otherwise it drops.
    unclear_product = decide(_verdict(direction="unclear", event_type="product"), _FACTS, None)
    assert unclear_product.final == "push" and unclear_product.override_rule == "unclear_but_clear_event"
    assert decide(_verdict(direction="unclear", event_type="rumor"), _FACTS, None).override_rule == "unclear_direction"
    assert decide(_verdict(direction="unclear", event_type="product", magnitude=1), _FACTS, None).final == "drop"
    high = GateFacts(
        grounded_assets=("NVDA",),
        watchlist_symbols=frozenset(),
        provider_score=92.0,
        priority="high",
        admission="candidate",
    )
    # #77: a high-priority push still pushes, it just no longer earns the ⚡ header.
    assert decide(_verdict(), high, None).final == "push"
    busy = StorylineStatus(key="theme:mideast_energy")
    unbounded = decide(_verdict(magnitude=2, scope="sector"), _FACTS, busy)
    assert unbounded.final == "push" and unbounded.throttled_by is None
    assert (
        decide(_verdict(magnitude=1), _FACTS, None, policy=DecidePolicy(min_push_magnitude=2)).final == "push"
    )  # watchlist
    assert (
        decide(
            _verdict(magnitude=1, assets=[TriageAsset(symbol="AMD", role="primary")]),
            _FACTS,
            None,
            policy=DecidePolicy(min_push_magnitude=2),
        ).final
        == "drop"
    )


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
    # Policy v8: `noise` no longer outranks restatement on a magnitude-2 actionable verdict. The card
    # still drops — the trace now names the rule that actually applies to it.
    noisy_repeat = decide(_verdict(novelty="restatement", restates=0, event_type="noise"), _FACTS, quiet)
    assert noisy_repeat.final == "drop" and noisy_repeat.override_rule == "restatement"
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


def test_decide_uses_content_duplicate_evidence_without_a_count_quota() -> None:
    """Prior volume never blocks a card; only evidence that the reader already got this fact can."""

    # Nothing in the window to compare against: the reader received nothing, so nothing can be a repeat.
    empty = decide(_verdict(), _FACTS, _busy())
    assert empty.final == "push" and empty.override_rule == "model_push_actionable" and empty.throttled_by is None
    assert empty.seen_similarity == 0.0 and empty.seen_against == -1

    seen = _busy(seen_headlines=("英伟达发布 Blackwell Ultra，单卡算力翻倍",), seen_event_ids=("evt-a",))
    # A card about something else goes out regardless of prior volume.
    released = decide(_verdict(headline_zh="美联储会议纪要显示多数官员倾向 9 月降息"), _FACTS, seen)
    assert released.final == "push" and released.override_rule == "model_push_actionable"
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

    # Degraded fallback has no semantic headline and therefore skips similarity.
    assert decide(_verdict(), _FACTS, seen, degraded=True).final == "push"
    # Disabling similarity does not restore a hidden count cap.
    duplicate_off = decide(
        _verdict(headline_zh="英伟达发布 Blackwell Ultra 芯片，单卡算力翻倍"),
        _FACTS,
        seen,
        policy=DecidePolicy(similarity_max=0.0),
    )
    assert duplicate_off.final == "push" and duplicate_off.throttled_by is None


def test_storyline_status_carries_only_content_evidence() -> None:
    """A count-based capacity field cannot quietly return to the decision interface."""

    assert {item.name for item in fields(StorylineStatus)} == {
        "key",
        "told_directions",
        "told_assets",
        "seen_headlines",
        "seen_event_ids",
        "seen_directions",
        "seen_assets",
    }


def _told_row(event_id: str, at_ms: int, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_id": event_id,
        "at_ms": at_ms,
        "storyline_key": "asset:BTC",
        "comparison_title": "",
        "event_type": "macro",
        "magnitude": 2,
        "direction": "bullish",
        "headline_zh": event_id,
        "grounded_assets": [],
        "assets": [],
    }
    row.update(overrides)
    return row


def _select(rows: Sequence[Mapping[str, Any]], **overrides: Any) -> Any:
    from tracefold.news.semantic_contract import ToldLedgerSnapshot

    kwargs: dict[str, Any] = {
        "now_ms": _NOW,
        "storyline_key": "theme:rates",
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

    from tracefold.news.semantic_contract import TOLD_MAX, TOLD_STORYLINE_TIER_MAX

    same = [_told_row(f"s{i}", _NOW - (30 + i) * 60_000, storyline_key="theme:rates") for i in range(10)]
    unrelated = [_told_row(f"o{i}", _NOW - i * 60_000, storyline_key="macro:general") for i in range(10)]

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

    from tracefold.news.semantic_contract import TOLD_MAX

    dense = [_told_row(f"s{i}", _NOW - (30 + i) * 60_000, storyline_key="theme:rates") for i in range(TOLD_MAX + 4)]
    filler = [_told_row(f"o{i}", _NOW - i * 60_000, storyline_key="macro:general") for i in range(3)]
    entries = _select(filler + dense).entries
    assert [entry.tier for entry in entries] == ["storyline"] * TOLD_MAX
    # Adding one more unrelated card changes nothing the model sees.
    grew = _select([_told_row("new", _NOW, storyline_key="macro:general"), *filler, *dense]).entries
    assert [entry.event_id for entry in grew] == [entry.event_id for entry in entries]


def test_told_selector_overflow_from_a_capped_tier_still_fills_leftover_slots() -> None:
    """The cap yields to other tiers; it does not throw rows away."""

    from tracefold.news.semantic_contract import TOLD_MAX, TOLD_STORYLINE_TIER_MAX

    same = [_told_row(f"s{i}", _NOW - i * 60_000, storyline_key="theme:rates") for i in range(TOLD_MAX + 4)]
    entries = _select(same).entries
    assert len(entries) == TOLD_MAX
    assert [entry.event_id for entry in entries] == [f"s{i}" for i in range(TOLD_MAX)]
    assert all(entry.tier == "storyline" for entry in entries)
    assert TOLD_STORYLINE_TIER_MAX < TOLD_MAX


def test_told_selector_finds_the_same_instrument_under_a_different_storyline_key() -> None:
    """16% of judgments end on a different final storyline key than the preliminary one they were selected
    with, and a prior card about the same instrument can sit under any of them. Symbol sets answer that;
    storyline keys alone do not."""

    rows = [_told_row(f"noise{i}", _NOW - i * 60_000, storyline_key="theme:trade") for i in range(11)] + [
        _told_row("oil", _NOW - 60 * 60_000, storyline_key="theme:mideast_energy", grounded_assets=["CL"])
    ]

    entries = _select(rows, storyline_key="macro:general", symbols=("CL", "XYZ-CL")).entries
    matched = next(entry for entry in entries if entry.event_id == "oil")
    assert matched.tier == "asset_overlap"
    # An hour-old card about this instrument outranks every fresher unrelated one.
    assert entries[0].event_id == "oil"
    assert matched.symbols == ("CL",)


def test_told_selector_uses_normalized_comparison_titles_not_reader_headlines() -> None:
    rows = [_told_row(f"noise{i}", _NOW - i * 60_000, storyline_key="theme:trade") for i in range(12)] + [
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
        storyline_key="macro:general",
        comparison_title="nvidia to invest usd_100000000000 in openai data centre",
    ).entries
    assert entries[0].event_id == "same-fact" and entries[0].tier == "fact_similarity"
    assert entries[0].similarity == 1.0
    # Below the retrieval threshold nothing is promoted out of the recency tail.
    weak = _select(rows, storyline_key="macro:general", comparison_title="an entirely unrelated sentence").entries
    assert all(entry.tier == "recency" for entry in weak)


def test_told_selector_drops_expired_duplicate_and_self_rows_before_ranking() -> None:
    rows = [
        _told_row("keep", _NOW - 60_000),
        _told_row("keep", _NOW - 120_000),  # same Event twice in the ledger
        _told_row("expired", _NOW - 5 * 3_600_000),
        _told_row("self", _NOW - 30_000),
        _told_row("", _NOW - 30_000),
    ]
    snapshot = _select(rows)
    assert [entry.event_id for entry in snapshot.entries] == ["keep"]
    assert snapshot.source_count == 1


def test_told_selector_is_deterministic_under_equal_timestamps_and_input_order() -> None:
    rows = [_told_row(f"e{i}", _NOW - 60_000) for i in range(20)]
    first = _select(rows).entries
    second = _select(list(reversed(rows))).entries
    assert [entry.event_id for entry in first] == [entry.event_id for entry in second]
    # Equal tier, equal similarity, equal time: the stable Event identity breaks the tie.
    assert [entry.event_id for entry in first] == sorted(entry.event_id for entry in first)


def test_told_selector_identity_binds_every_behaviour_that_changes_what_the_model_sees() -> None:
    """`retrieval_sha256` is this hash. A literal describing the selector could not tell a tier-order or
    projection edit from a no-op, and the arm would have shipped as the same bundle."""

    import tracefold.news.semantic_contract as contract
    from tracefold.news.artifact_identity import canonical_sha

    assert len(contract.TOLD_SELECTOR_SHA256) == 64
    payload = {
        "tier_order": list(contract.TOLD_TIER_ORDER),
        "source_max": contract.TOLD_SOURCE_MAX,
        "visible_cap": contract.TOLD_MAX,
        "similarity_min": contract.TOLD_FACT_SIMILARITY_MIN,
        "window_ms": contract.TOLD_WINDOW_MS,
    }
    # Every one of these is inside the hashed payload, so changing any of them moves the bundle identity.
    assert all(canonical_sha({**payload, key: "changed"}) != canonical_sha(payload) for key in payload)


def test_storyline_status_carries_told_directions() -> None:
    told = [{"i": 0, "dir": "bullish", "headline_zh": "a"}, {"i": 1, "dir": "neutral", "headline_zh": "b"}]
    status = storyline_status("asset:BTC", told=told)
    assert status.told_directions == ("bullish", "neutral") and status.told_count == 2
    assert storyline_status("asset:BTC").told_count == 0


def test_mideast_storyline_requires_real_strait_or_mideast_context() -> None:
    # "STRAITS" was matching the unbounded substring ``strait`` and any
    # unrelated use of "oil" was classified as Middle East energy.
    assert (
        preliminary_storyline_key(
            title="STRAITS: Crypto surge causes $2.7bn liquidations",
            grounded_assets=("BTC",),
            asset_class="crypto",
            family="market_telemetry",
        )
        == "asset:BTC"
    )
    assert (
        preliminary_storyline_key(
            title="Exxon starts production at new Guyana oil FPSO",
            grounded_assets=("XOM",),
            asset_class="equity_or_commodity",
            family="general",
        )
        == "asset:XOM"
    )
    assert (
        preliminary_storyline_key(
            title="Tanker struck in Strait of Hormuz, Brent supply risk rises",
            grounded_assets=("CL",),
            asset_class="equity_or_commodity",
            family="general",
        )
        == "theme:mideast_energy"
    )


def test_fallback_is_not_silent() -> None:
    weak = GateFacts(
        grounded_assets=("AMD",),
        watchlist_symbols=frozenset(),
        provider_score=75.0,
        priority="normal",
        admission="candidate",
    )
    assert rule_baseline(weak) == "drop"
    verdict, decision = fallback_verdict(weak, error_code="news_triage_timeout")
    assert decision.final == "drop" and verdict.headline_zh
    grounded_80 = GateFacts(
        grounded_assets=("AMD",),
        watchlist_symbols=frozenset(),
        provider_score=85.0,
        priority="normal",
        admission="candidate",
    )
    assert rule_baseline(grounded_80) == "push"
    # #81: a high-priority Event without a grounded asset used to drop silently whenever the model was down —
    # a missile strike or a rate decision has no ticker. It now fails open onto the wire headline, and so does a
    # deterministic exchange notice.
    ungrounded_high = GateFacts(
        grounded_assets=(),
        watchlist_symbols=frozenset(),
        provider_score=95.0,
        priority="high",
        admission="candidate",
    )
    assert rule_baseline(ungrounded_high) == "push"
    assert rule_baseline(ungrounded_high, fail_open_high_priority=False) == "drop"
    listing = GateFacts(
        grounded_assets=(),
        watchlist_symbols=frozenset(),
        provider_score=10.0,
        priority="normal",
        admission="listing_deterministic",
    )
    assert rule_baseline(listing) == "push"
    ungrounded_normal = GateFacts(
        grounded_assets=(),
        watchlist_symbols=frozenset(),
        provider_score=95.0,
        priority="normal",
        admission="candidate",
    )
    assert rule_baseline(ungrounded_normal) == "drop"
    strong = GateFacts(
        grounded_assets=("BTC",),
        watchlist_symbols=frozenset(),
        provider_score=90.0,
        priority="high",
        admission="candidate",
    )
    assert fallback_verdict(strong, error_code="x")[1].final == "push"
    assert (
        fallback_verdict(strong, error_code="x", title="  Fed  holds rates\nsteady ")[0].headline_zh
        == "Fed holds rates steady"
    )
    assert fallback_verdict(strong, error_code="x")[0].headline_zh == "模型不可用（规则兜底）"  # no wire title at all


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
            "title_zh": "英伟达将投资 1000 亿美元",
            "why_zh": "英伟达把千亿美元投进 OpenAI 的俄亥俄数据中心，算力供给链再加码",
            "event_type": "partnership",
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
        "partnership",
        "single_name",
        "原标题",
        "个别标的",
    ):
        assert machine_word not in text
    assert "Nvidia to invest $100bn" not in text and "英伟达将投资 1000 亿美元" not in text  # no title lines
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
    m = bus.BusMessage(
        kind="event",
        message_id="event:1",
        routing_key="event.general.high",
        payload={"event_id": "1"},
        trace_id="t",
        occurred_at_ms=5,
        priority=5,
    )
    back = bus.decode_body(m.body(), routing_key=m.routing_key, priority=5, headers={"x-news-attempt": 2})
    assert back.payload == {"event_id": "1"} and back.attempt == 2 and back.priority == 5
    with pytest.raises(bus.BusDecodeError):
        bus.decode_body(b"{}", routing_key="x", priority=0, headers=None)


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
    # Binance CFX announcement burst collapses into one shared event
    cfx = [h for h in hits if "Conflux Network (CFX)" in str(h.get("text"))]
    cfx_report = replay_hits(cfx, watchlist_symbols=frozenset())
    assert cfx_report["counts"]["events"] == 1 and cfx_report["counts"]["exact_members"] == len(cfx) - 1
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
            family="general",
        )
        == "asset:JNJ"
    )
    # The model named nothing and the tag is not what the text is about: the family bucket, not `asset:BTC`.
    assert (
        final_storyline_key(
            title="Poland scrambles jets in response to Russian strikes on Ukraine",
            headline_zh="波兰启动预防性军机行动",
            scope="macro",
            verdict_primaries=[],
            grounded_assets=["BTC"],
            family="general",
        )
        == "macro:general"
    )
    # The model named nothing but the text names the tag as its own token: still that asset's storyline.
    assert (
        final_storyline_key(
            title="OKB burn completed",
            headline_zh="OKB 完成销毁",
            scope="macro",
            verdict_primaries=[],
            grounded_assets=["OKB"],
            family="general",
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
            family="general",
        )
        == "macro:general"
    )
    # A degraded verdict has no `assets` by construction, so "named nothing" says nothing: keep the old fallback.
    assert (
        final_storyline_key(
            title="NVIDIA to invest $100bn in OpenAI data centre",
            headline_zh="NVIDIA 投资 OpenAI",
            scope="macro",
            verdict_primaries=[],
            grounded_assets=["NVDA"],
            family="general",
            degraded=True,
        )
        == "asset:NVDA"
    )
    # A grounded primary still wins outright, and a theme still beats both fallbacks.
    assert (
        final_storyline_key(
            title="Iran halts oil exports",
            headline_zh="伊朗停止石油出口",
            scope="macro",
            verdict_primaries=["XOM"],
            grounded_assets=["XOM"],
            family="general",
        )
        == "theme:mideast_energy"
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
    on_fresh = decide(big, _FACTS, _fresh(seen_headlines=told, seen_event_ids=("a",)))
    assert on_fresh.final == "escalate" and on_fresh.throttled_by is None and on_fresh.seen_scope == ""

    # Even identical text cannot make the content heuristic veto an escalation.
    hot = StorylineStatus(
        key="asset:NVDA",
        seen_headlines=("特朗普称美国正考虑购买大量比特币及其他加密资产",),
        seen_event_ids=("a",),
    )
    repeat = decide(big, _FACTS, hot)
    assert repeat.final == "escalate" and repeat.seen_scope == ""
    assert repeat.throttled_by is None


def test_decide_leaves_the_degraded_and_switched_off_paths_alone() -> None:
    """A rule-baseline card carries a placeholder headline, and `similarity_max = 0` is the operator switching the
    content judgment off. Neither is ever measured, on either path."""

    window = _fresh(seen_headlines=_OKX_LEDGER, seen_event_ids=("a", "b", "c"))
    duplicate = _verdict(headline_zh="KLA Corporation（$KLACx）出现在 OKX")

    degraded = decide(duplicate, _FACTS, window, degraded=True)
    assert degraded.final == "push" and degraded.seen_similarity is None and degraded.seen_scope == ""

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
            family="general",
        )

    assert key(["TSLA"]) == "asset:TSLA"
    # An exchange-qualified identifier we cannot group falls through instead of minting one.
    assert key(["0001.HK"]) == "macro:general"
    assert key(["a" * 11]) == "macro:general"


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
            family="general",
        )
        == "macro:general"
    )


def test_empty_title_zh_means_same_as_headline() -> None:
    """#101: `title_zh` repeated `headline_zh` verbatim in 85% of a live day's verdicts — ~13% of all output
    tokens — because the prompt only asks for a condensed header when the wire headline is long. Prompt v9 asks
    for the sentinel instead; every reader fills it in, so nothing downstream sees an empty title."""

    def _card(**verdict_over: object) -> dict:
        return render_first_card(
            event={"event_id": "e1", "leader_title": "Nvidia to invest $100bn", "reporting_origin": "ft"},
            verdict={
                "direction": "bullish",
                "magnitude": 2,
                "headline_zh": "英伟达千亿美元投资 OpenAI 数据中心",
                "why_zh": "算力供给链再加码",
                "assets": [],
                **verdict_over,
            },
            decision="push",
            grounded_assets=[],
        )

    # The sentinel never reaches the card: the header is headline_zh, exactly as when title_zh repeated it.
    assert _card(title_zh="")["header"]["title"]["content"] == "英伟达千亿美元投资 OpenAI 数据中心"
    assert (
        _card(title_zh="英伟达将投资 1000 亿美元")["header"]["title"]["content"] == "英伟达千亿美元投资 OpenAI 数据中心"
    )
    # title_zh is still the fallback for a headline that sanitises away (a URL in it), and the wire title after it.
    assert (
        _card(headline_zh="看 https://evil.example", title_zh="英伟达将投资 1000 亿美元")["header"]["title"]["content"]
        == "英伟达将投资 1000 亿美元"
    )
    assert (
        _card(headline_zh="看 https://evil.example", title_zh="")["header"]["title"]["content"]
        == "Nvidia to invest $100bn"
    )
