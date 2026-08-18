"""Pure-module tests for News V3: titles, gate, storyline, rules, minhash, delivery, control, bus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracefold.news import bus
from tracefold.news.analyst_rules import verify_verdict
from tracefold.news.control import apply_control, is_muted, parse_control
from tracefold.news.delivery import render_first_card, sanitize_ai_text
from tracefold.news.eval.replay import replay_hits
from tracefold.news.gate import GateInput, evaluate_gate, grounded_assets
from tracefold.news.minhash import BANDS, band_keys, estimate_jaccard, minhash_signature
from tracefold.news.models import AnalystVerdict, TriageAsset, TriageVerdict
from tracefold.news.storyline import preliminary_storyline_key, storyline_key
from tracefold.news.titles import extract_title
from tracefold.news.tokens import comparison_tokens, jaccard
from tracefold.news.triage_rules import GateFacts, StorylineStatus, decide, fallback_verdict, rule_baseline

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
def test_gate_grounds_cl_only_in_energy_context_and_ignores_bad_tags() -> None:
    coins = (
        {"symbol": "CL", "grade": "A+"},
        {"symbol": "XYZ-CL", "grade": "A+"},
        {"symbol": "NEAR", "grade": "A"},
        {"symbol": "OPENAI", "grade": "A"},
    )
    assert grounded_assets("China conducts suspected marine research within Japan's EEZ", coins) == ()
    assert grounded_assets("Vessel struck by unknown projectile in Strait of Hormuz", coins) == ("CL", "XYZ-CL")
    assert (
        grounded_assets(
            "Nvidia backs OpenAI data center near Ohio",
            ({"symbol": "NVDA", "grade": "A"}, {"symbol": "OPENAI", "grade": "A"}, {"symbol": "NEAR", "grade": "A"}),
        )
        == ()
    )
    assert grounded_assets(
        "Nvidia backs OpenAI data center near Ohio",
        ({"symbol": "NVDA", "grade": "A+"}, {"symbol": "OPENAI", "grade": "A+"}),
    ) == ("NVDA",)
    assert grounded_assets("$NVDA jumps 5%", ({"symbol": "NVDA", "grade": "A"},)) == ("NVDA",)


def test_gate_admission_rules() -> None:
    base = dict(
        strategy_ids=("1018",), provider_score=75.0, coins=(), ingest_mode="live", watchlist_symbols=frozenset({"BTC"})
    )
    assert (
        evaluate_gate(GateInput(title="Imagine being this guy", engine_type="meme", **base)).admission
        == "suppressed_ungrounded_meme"
    )
    assert (
        evaluate_gate(
            GateInput(title="Russia downs 180 drones in Moscow region overnight", engine_type="news", **base)
        ).admission
        == "suppressed_ungrounded"
    )
    macro = evaluate_gate(
        GateInput(title="U.S. 30-Year Treasury Yield Climbs to 5.32%, Highest Since 2007", engine_type="news", **base)
    )
    assert macro.admission == "candidate" and macro.asset_class == "macro" and macro.priority == "high"
    pr = evaluate_gate(
        GateInput(
            title="Exelixis (EXEL) Securities Investigation Notice - Levi & Korsinsky", engine_type="news", **base
        )
    )
    assert pr.admission == "suppressed_pr_template"
    listing = evaluate_gate(GateInput(title="Bybit will list LYTE", engine_type="listing", **base))
    assert listing.admission == "listing_deterministic"
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
            title="BTC breaks $120k as ETF inflows surge",
            engine_type="news",
            **{**base, "coins": ({"symbol": "BTC", "grade": "A+"},)},
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
        rationale="",
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
    assert decide(_verdict(direction="unclear"), _FACTS, None).final == "drop"
    assert decide(_verdict(magnitude=3), _FACTS, None).final == "escalate"
    assert decide(_verdict(magnitude=1, actionable=False), _FACTS, None).final == "push"  # watchlist primary m1
    assert (
        decide(_verdict(magnitude=1, assets=[TriageAsset(symbol="AMD", role="primary")]), _FACTS, None).final == "drop"
    )
    high = GateFacts(
        grounded_assets=("NVDA",),
        watchlist_symbols=frozenset(),
        provider_score=92.0,
        priority="high",
        admission="candidate",
    )
    assert decide(_verdict(), high, None).final == "escalate"
    status = StorylineStatus(key="asset:NVDA", pushed_2h=1, max_magnitude_2h=2, directions_2h=("bullish",))
    throttled = decide(_verdict(), _FACTS, status)
    assert throttled.final == "throttled" and throttled.throttled_by == "storyline:asset:NVDA"
    assert decide(_verdict(magnitude=3), _FACTS, status).final == "escalate"  # magnitude exceeds window max
    assert decide(_verdict(direction="bearish"), _FACTS, status).final == "push"  # genuine flip
    assert decide(_verdict(), _FACTS, None, hourly_cap_reached=True).throttled_by == "hourly_cap"
    assert decide(_verdict(), _FACTS, None, muted=True).final == "drop"


def test_fallback_is_fail_closed() -> None:
    weak = GateFacts(
        grounded_assets=("AMD",),
        watchlist_symbols=frozenset(),
        provider_score=85.0,
        priority="normal",
        admission="candidate",
    )
    assert rule_baseline(weak) == "drop"
    verdict, decision = fallback_verdict(weak, error_code="news_triage_timeout")
    assert decision.final == "drop" and verdict.headline_zh
    strong = GateFacts(
        grounded_assets=("BTC",),
        watchlist_symbols=frozenset(),
        provider_score=90.0,
        priority="high",
        admission="candidate",
    )
    assert fallback_verdict(strong, error_code="x")[1].final == "push"


# ---------------------------------------------------------------- analyst verify
def test_verify_verdict_rejects_fabricated_evidence() -> None:
    evidence = {"history:abc": {"event_id": "e0", "final_decision": "push"}}
    ok = AnalystVerdict(
        agrees_with_triage=True,
        revised_direction="bullish",
        revised_magnitude=2,
        novelty_assessment="new",
        context_evidence=["history:abc"],
        thesis_zh="ok",
        risks_zh="",
        follow_up_needed=True,
        confidence=0.7,
    )
    assert verify_verdict(ok, tool_evidence=evidence, triage_direction="bullish").ok
    unknown = ok.model_copy(update={"context_evidence": ["history:nope"]})
    assert (
        verify_verdict(unknown, tool_evidence=evidence, triage_direction="bullish").reason == "context_evidence_unknown"
    )
    unsupported = ok.model_copy(update={"context_evidence": []})
    assert (
        verify_verdict(unsupported, tool_evidence=evidence, triage_direction="bullish").reason
        == "magnitude_without_evidence"
    )
    disagree = ok.model_copy(update={"agrees_with_triage": False})
    assert (
        verify_verdict(disagree, tool_evidence=evidence, triage_direction="bullish").reason
        == "disagreement_without_revision"
    )
    with pytest.raises(ValueError):
        AnalystVerdict.model_validate({**ok.model_dump(), "market_reaction": []})


# ---------------------------------------------------------------- delivery / control / bus
def test_card_uses_code_facts_and_sanitizes_ai_text() -> None:
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
        },
        verdict={
            "direction": "bullish",
            "magnitude": 2,
            "headline_zh": "英伟达投资 https://x.y",
            "title_zh": "英伟达将投资 1000 亿美元",
            "rationale": "利好",
            "event_type": "partnership",
            "scope": "single_name",
        },
        decision="push",
        grounded_assets=["NVDA"],
    )
    # URL in the AI headline -> fallback to the Triage title_zh, then to the original title.
    assert card["header"]["title"]["content"] == "英伟达将投资 1000 亿美元"
    body = json.dumps(card, ensure_ascii=False)
    assert "原标题" in body and "NVDA" in body and "打开来源" in body and "**标题**：英伟达将投资 1000 亿美元" in body
    bare = render_first_card(
        event={"event_id": "e1", "leader_title": "Nvidia to invest $100bn", "member_count": 1},
        verdict={"direction": "bullish", "magnitude": 2, "headline_zh": "x https://x.y"},
        decision="push",
        grounded_assets=[],
    )
    assert bare["header"]["title"]["content"] == "Nvidia to invest $100bn"


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
    # Levi & Korsinsky template PRs must not merge (ticker veto) and must be suppressed
    levi = [h for h in hits if "Levi & Korsinsky" in str(h.get("text"))]
    assert len(levi) >= 3
    assert counts.get("admission:suppressed_pr_template", 0) + counts.get("admission:suppressed_ungrounded", 0) >= len(
        levi
    )
    # 'reply <url>' items with distinct slugs must not collapse into one event
    replies = [h for h in hits if str(h.get("text", "")).lower().startswith("reply http")]
    assert len(replies) >= 2
    reply_report = replay_hits(replies, strategy_ids=("1018",), watchlist_symbols=frozenset())
    assert reply_report["counts"]["events"] == len(replies)
    # Binance CFX announcement burst collapses into one shared event
    cfx = [h for h in hits if "Conflux Network (CFX)" in str(h.get("text"))]
    cfx_report = replay_hits(cfx, strategy_ids=("1018",), watchlist_symbols=frozenset())
    assert cfx_report["counts"]["events"] == 1 and cfx_report["counts"]["exact_members"] == len(cfx) - 1
    # candidate share is bounded (Triage load)
    assert report["candidate_share_of_items"] <= 0.6
