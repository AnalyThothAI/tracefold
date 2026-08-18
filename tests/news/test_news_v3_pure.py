"""Pure-module tests for News V3: titles, gate, storyline, rules, minhash, delivery, control, bus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracefold.news import bus
from tracefold.news.control import apply_control, is_muted, parse_control
from tracefold.news.delivery import card_assets, render_first_card, sanitize_ai_text
from tracefold.news.eval.replay import replay_hits
from tracefold.news.gate import GateInput, evaluate_gate, grounded_assets
from tracefold.news.minhash import BANDS, band_keys, estimate_jaccard, minhash_signature
from tracefold.news.models import TriageAsset, TriageVerdict
from tracefold.news.storyline import final_storyline_key, preliminary_storyline_key, storyline_key
from tracefold.news.titles import extract_title
from tracefold.news.tokens import comparison_tokens, jaccard
from tracefold.news.triage_rules import (
    DecidePolicy,
    GateFacts,
    StorylineStatus,
    decide,
    fallback_verdict,
    rule_baseline,
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


def test_decide_rules_and_throttle() -> None:
    assert decide(_verdict(), _FACTS, None).final == "push"
    assert decide(_verdict(event_type="noise"), _FACTS, None).final == "drop"
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
    assert decide(_verdict(), high, None).final == "escalate"
    # Asset storylines: window-max plus direction flip.
    status = StorylineStatus(key="asset:NVDA", pushed_2h=1, max_magnitude_2h=2, directions_2h=("bullish",))
    throttled = decide(_verdict(), _FACTS, status)
    assert throttled.final == "throttled" and throttled.throttled_by == "storyline:asset:NVDA"
    assert decide(_verdict(magnitude=3), _FACTS, status).final == "escalate"  # magnitude exceeds window max
    assert decide(_verdict(direction="bearish"), _FACTS, status).final == "push"  # genuine flip
    # Theme storylines: a cap per 4 h instead of "only ever higher magnitude".
    theme = StorylineStatus(
        key="theme:mideast_energy",
        pushed_2h=2,
        pushed_4h=2,
        max_magnitude_2h=3,
        max_magnitude_4h=3,
        directions_2h=("bullish",),
        directions_4h=("bullish",),
    )
    assert decide(_verdict(magnitude=2, scope="sector"), _FACTS, theme).final == "push"  # 2 < cap 3
    capped = StorylineStatus(
        key="theme:mideast_energy",
        pushed_2h=3,
        pushed_4h=3,
        max_magnitude_2h=3,
        max_magnitude_4h=3,
        directions_2h=("bullish",),
        directions_4h=("bullish",),
    )
    assert (
        decide(_verdict(magnitude=2, scope="sector"), _FACTS, capped).throttled_by
        == "storyline:theme:mideast_energy:cap3"
    )
    assert decide(_verdict(magnitude=2, scope="sector", direction="bearish"), _FACTS, capped).final == "push"  # flip
    # Switches.
    assert decide(_verdict(), _FACTS, status, policy=DecidePolicy(storyline_throttle=False)).final == "push"
    assert decide(_verdict(), _FACTS, None, hourly_cap_reached=True).throttled_by == "hourly_cap"
    assert (
        decide(_verdict(), _FACTS, None, hourly_cap_reached=True, policy=DecidePolicy(hourly_cap_enabled=False)).final
        == "push"
    )
    assert decide(_verdict(), _FACTS, None, muted=True).final == "drop"
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
    ungrounded_90 = GateFacts(
        grounded_assets=(),
        watchlist_symbols=frozenset(),
        provider_score=95.0,
        priority="high",
        admission="candidate",
    )
    assert rule_baseline(ungrounded_90) == "drop"
    strong = GateFacts(
        grounded_assets=("BTC",),
        watchlist_symbols=frozenset(),
        provider_score=90.0,
        priority="high",
        admission="candidate",
    )
    assert fallback_verdict(strong, error_code="x")[1].final == "push"


# ---------------------------------------------------------------- delivery / control / bus
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
    # Card assets are the verdict primaries the Gate grounded; a small grounded set shows when the model named none.
    assert card_assets({"assets": [{"symbol": "CC", "role": "primary"}]}, ["CC"]) == ["CC"]
    assert card_assets({"assets": []}, ["A", "B", "C", "D", "E"]) == []
    assert card_assets({"assets": [{"symbol": "BTC", "role": "primary"}]}, ["BTC", "CL", "XYZ-CL"]) == ["BTC"]


def test_control_commands() -> None:
    state = {"paused": False, "mutes": []}
    state = apply_control(
        state, parse_control({"action": "mute_theme", "key": "mideast_energy", "ttl_ms": 120000}), now_ms=1000
    )
    assert is_muted(state, storyline_key="theme:mideast_energy", grounded_assets=[], now_ms=2000)
    assert not is_muted(state, storyline_key="theme:mideast_energy", grounded_assets=[], now_ms=200000)
    state = apply_control(state, parse_control({"action": "mute_symbol", "key": "cl"}), now_ms=1000)
    assert is_muted(state, storyline_key="asset:CL", grounded_assets=["XYZ-CL"], now_ms=2000)
    state = apply_control(state, parse_control({"action": "pause_delivery"}), now_ms=1000)
    assert state["paused"]
    with pytest.raises(ValueError):
        parse_control({"action": "nuke"})


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
    report = replay_hits(
        hits, strategy_ids=("1018", "1352", "1353"), watchlist_symbols=frozenset({"BTC", "ETH", "NVDA"})
    )
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
    reply_report = replay_hits(replies, strategy_ids=("1018",), watchlist_symbols=frozenset())
    assert reply_report["counts"]["events"] == len(replies)
    # Binance CFX announcement burst collapses into one shared event
    cfx = [h for h in hits if "Conflux Network (CFX)" in str(h.get("text"))]
    cfx_report = replay_hits(cfx, strategy_ids=("1018",), watchlist_symbols=frozenset())
    assert cfx_report["counts"]["events"] == 1 and cfx_report["counts"]["exact_members"] == len(cfx) - 1
    # The Gate no longer decides relevance: most items reach Triage (the model is the semantic filter)
    assert report["candidate_share_of_items"] >= 0.65


# ---------------------------------------------------------------- recall regression (issue #53)
RECALL_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_hits_recall_sample.json"
EXPECTATIONS = Path(__file__).resolve().parents[1] / "fixtures" / "news_v3_expectations.json"


def test_gate_expectations_over_the_recall_corpus() -> None:
    """Trajectory-prefix regression: every case names a real headline and the acceptable Gate outcome set."""

    hits = _hits() + json.loads(RECALL_FIXTURE.read_text(encoding="utf-8"))
    report = replay_hits(hits, strategy_ids=("1018", "1352", "1353"), watchlist_symbols=frozenset())
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
