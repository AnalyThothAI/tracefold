from __future__ import annotations

import hashlib
import json

import pytest

from tracefold.news import (
    STORY_SIMILARITY_THRESHOLD,
    candidate_tokens,
    classify_by_keyword,
    cluster_texts,
    default_sources,
    has_historical_marker,
    normalize_story_text,
    story_similarity,
    story_vector,
)
from tracefold.news.brief import validate_and_repair_brief
from tracefold.news.models import NewsBriefDraft, NewsBriefStory
from tracefold.news.ranking import importance_factors, select_top_stories

POSITIVE_PAIRS = (
    (
        "Fed holds interest rates steady amid inflation concerns",
        "Fed holds rates steady as inflation concerns persist",
    ),
    (
        "Magnitude 6.8 earthquake strikes northern Chile",
        "6.8-magnitude earthquake hits northern Chile",
    ),
    (
        "EU approves 12th sanctions package against Russia",
        "European Union approves 12th sanctions package on Russia",
    ),
    (
        "Ukraine drone strike hits Russian oil refinery in Ryazan region",
        "Ukraine drone strike hits Russian oil refinery",
    ),
    (
        "Iran threatens to close Strait of Hormuz if US blockade continues",
        "Iran threatens to close Strait of Hormuz — live updates",
    ),
    (
        "Apple unveils new AI features at WWDC keynote",
        "At WWDC keynote, Apple unveils new AI features",
    ),
    (
        "Iranian officials threaten Hormuz closure over sanctions",
        "Iran officials threaten Hormuz closure over sanctions",
    ),
    (
        "Nigeria fuel subsidy protests spread to Lagos as unions join",
        "Nigeria fuel subsidy protests spread to Lagos",
    ),
    (
        "Turkey hikes interest rates to 50% in surprise move",
        "Turkey hikes rates to 50% in surprise move",
    ),
    (
        "China exports fall 7.5% in June, worse than expected",
        "Chinese exports fell 7.5% in June, worse than expected",
    ),
    (
        "Nigeria fuel subsidy protests spread to Lagos as unions join nationwide strike over cost of living",
        "Nigeria fuel subsidy protests spread to Lagos",
    ),
    (
        "Turkey central bank hikes interest rates to 50% in surprise move to combat runaway inflation pressures",
        "Turkey central bank hikes interest rates",
    ),
    (
        "Iran threatens to close Strait of Hormuz - Reuters",
        "Iran threatens to close Strait of Hormuz",
    ),
)

NEGATIVE_PAIRS = (
    (
        "Iran seizes oil tanker in Strait of Hormuz",
        "Iran threatens to close Strait of Hormuz",
    ),
    (
        "Fed holds rates steady amid inflation concerns",
        "Fed cuts rates by 25 basis points amid slowing economy",
    ),
    (
        "Magnitude 6.8 earthquake strikes northern Chile",
        "Magnitude 5.9 earthquake strikes southern Peru",
    ),
    (
        "Ukraine drone strike hits Russian oil refinery",
        "Russian drone strike hits Ukrainian energy grid",
    ),
    (
        "Apple unveils new AI features at WWDC keynote",
        "Google unveils new AI features at I/O keynote",
    ),
    (
        "Turkey hikes interest rates to 50% in surprise move",
        "Argentina hikes interest rates to 50% in surprise move",
    ),
    (
        "Nigeria fuel subsidy protests spread to Lagos",
        "Kenya tax protests spread to Nairobi",
    ),
    (
        "US imposes new sanctions on Iranian oil exports",
        "US lifts sanctions on Venezuelan oil exports",
    ),
    (
        "Israel strikes Hezbollah targets in southern Lebanon",
        "Hezbollah strikes Israeli positions in northern Israel",
    ),
    (
        "Apple unveils new AI features at WWDC keynote - Reuters",
        "Google unveils new AI features at I/O keynote - Reuters",
    ),
)

HISTORICAL_MARKER_TRUE = (
    "Science history: Chernobyl nuclear power plant melts down — April 26, 1986",
    "On this day in 1969: The moon landing",
    "This day in history: Berlin Wall falls",
    "Throwback Thursday: 9/11 reflections",
    "Flashback: 1986 Iran-Contra disclosure",
    "CBS News Radio flashback: D-Day, Invasion of Normandy in 1944",
    "BBC Throwback Thursday: the fall of Saigon",
    "NPR Flashback Friday: Watergate hearings",
    "Iraq invasion: 5 years ago today",
    "Cuban missile crisis 6 decades ago",
    "Vietnam war 50 years after withdrawal",
    "40th anniversary of the Chernobyl disaster",
    "Remembering 9/11 attacks",
    "Chernobyl meltdown - April 26, 1986",
    "Disaster on 1986-04-26 changed nuclear policy",
)

HISTORICAL_MARKER_FALSE = (
    "Today in Ukraine: Russian missile strikes Kyiv",
    "This day in: Iran fires missile at Tel Aviv",
    "On this day, Iran invasion begins",
    "Today in tech: Apple unveils new iPhone",
    "Markets see flashback to 2008 crisis as bonds tumble",
    "Stocks suffer flashback to March 2020 crash",
    "Tesla stock throwback after split",
    "AI flashback to 2023 boom: Nvidia earnings beat",
    "markets see flashback: bonds tumble",
    "Missile launch reported on April 26, 2026",
    "Court ruling on April 15, 2025 takes effect",
    "Election scheduled for November 3, 2027",
    "Brief published 2026-04-26 covers the day",
    "Russia warns of 2026 nuclear escalation",
    "Stock down to 1986 points after crash",
)

IMPORTANCE_CASES = (
    ("critical", 1, 5, 100),
    ("critical", 2, 3, 89),
    ("critical", 3, 1, 78),
    ("critical", 4, 1, 73),
    ("high", 1, 2, 77),
    ("high", 2, 4, 78),
    ("high", 4, 1, 59),
    ("medium", 2, 1, 56),
    ("medium", 3, 5, 63),
    ("low", 1, 1, 47),
)


@pytest.mark.parametrize(("left", "right"), POSITIVE_PAIRS)
def test_worldmonitor_edit_variants_merge(left: str, right: str) -> None:
    assert story_similarity(left, right) >= STORY_SIMILARITY_THRESHOLD


@pytest.mark.parametrize(("left", "right"), NEGATIVE_PAIRS)
def test_worldmonitor_distinct_events_split(left: str, right: str) -> None:
    assert story_similarity(left, right) < STORY_SIMILARITY_THRESHOLD


def test_known_ebola_pair_stays_split() -> None:
    left = "Ebola outbreak declared in Uganda after confirmed case"
    right = "WHO marks anniversary of West Africa Ebola outbreak"
    assert story_similarity(left, right) < STORY_SIMILARITY_THRESHOLD


def test_union_find_is_deterministic_and_uses_connected_components() -> None:
    titles = [
        "Iran threatens to close Strait of Hormuz if US blockade continues",
        "Iran threatens to close Strait of Hormuz — live updates",
        "Stock market rallies on tech earnings report",
        "Iran seizes oil tanker in Strait of Hormuz",
    ]
    assert cluster_texts(titles) == [[0, 1], [2], [3]]
    assert cluster_texts(titles) == cluster_texts(titles)


def test_worldmonitor_positive_and_negative_sets_keep_margin() -> None:
    minimum_positive = min(story_similarity(left, right) for left, right in POSITIVE_PAIRS)
    maximum_negative = max(story_similarity(left, right) for left, right in NEGATIVE_PAIRS)
    assert minimum_positive - STORY_SIMILARITY_THRESHOLD >= 0.015
    assert STORY_SIMILARITY_THRESHOLD - maximum_negative >= 0.015


def test_identical_titles_have_similarity_one() -> None:
    title = "Iran threatens to close Strait of Hormuz"
    score = story_similarity(title, title)
    assert score == pytest.approx(1.0, abs=1e-9)
    assert 0.0 <= score <= 1.0


def test_empty_titles_have_no_vector_and_zero_similarity() -> None:
    assert story_vector("") is None
    assert story_vector("   —— !!") is None
    assert story_similarity("", "Iran threatens Hormuz") == 0


def test_story_similarity_is_symmetric() -> None:
    left = "Fed holds rates steady amid inflation concerns"
    right = "Fed holds interest rates steady"
    assert story_similarity(left, right) == pytest.approx(story_similarity(right, left), abs=1e-12)


def test_cjk_titles_vectorize_and_near_duplicates_are_closer() -> None:
    title = "日本銀行が金利を引き上げ、市場に衝撃"
    near = "日本銀行が金利を引き上げ"
    distant = "米国大統領がメキシコ国境を視察"
    assert story_vector(title) is not None
    assert story_similarity(title, near) > story_similarity(title, distant)


def test_case_only_differences_merge() -> None:
    assert (
        story_similarity(
            "IRAN THREATENS TO CLOSE STRAIT OF HORMUZ",
            "Iran threatens to close Strait of Hormuz",
        )
        >= STORY_SIMILARITY_THRESHOLD
    )


def test_candidate_tokens_and_normalization_match_worldmonitor() -> None:
    tokens = candidate_tokens("US to cut rates 日本")
    assert "to" not in tokens
    assert "rates" in tokens
    assert "日本" in tokens
    assert normalize_story_text("  Fed — holds,  rates!  ") == "fed holds rates"


def test_cluster_membership_is_order_independent() -> None:
    titles = [
        "Turkey central bank hikes interest rates to 50% in surprise move",
        "Turkey central bank hikes interest rates to 50%",
        "Turkey central bank hikes rates",
        "Kenya tax protests spread to Nairobi",
    ]
    permutations = (
        (0, 1, 2, 3),
        (3, 2, 1, 0),
        (1, 3, 0, 2),
        (2, 0, 3, 1),
    )
    signatures = [
        sorted(len(cluster) for cluster in cluster_texts([titles[index] for index in permutation]))
        for permutation in permutations
    ]
    assert all(signature == signatures[0] for signature in signatures)


def test_hot_bucket_identical_titles_still_form_one_cluster() -> None:
    titles = ["Iran threatens to close Strait of Hormuz"] * 251
    titles.append("Kenya tax protests spread to Nairobi")
    assert sorted((len(cluster) for cluster in cluster_texts(titles)), reverse=True) == [251, 1]


def test_worldmonitor_classifier_historical_guard_and_exact_categories() -> None:
    assert classify_by_keyword("Iran launches a missile").model_dump() == {
        "level": "high",
        "category": "military",
        "confidence": 0.8,
        "source": "keyword",
    }
    historical = classify_by_keyword(
        "Science history: Chernobyl nuclear meltdown April 26, 1986",
        now_ms=1_779_000_000_000,
    )
    assert historical.level == "info"
    assert historical.source == "keyword-historical-downgrade"


@pytest.mark.parametrize("title", HISTORICAL_MARKER_TRUE)
def test_worldmonitor_historical_marker_true_matrix(title: str) -> None:
    assert has_historical_marker(title, now_ms=1_776_211_200_000) is True


@pytest.mark.parametrize("title", HISTORICAL_MARKER_FALSE)
def test_worldmonitor_historical_marker_false_matrix(title: str) -> None:
    assert has_historical_marker(title, now_ms=1_776_211_200_000) is False


@pytest.mark.parametrize(("level", "tier", "corroboration", "expected"), IMPORTANCE_CASES)
def test_worldmonitor_importance_score_matrix(
    level: str,
    tier: int,
    corroboration: int,
    expected: int,
) -> None:
    factors = importance_factors(
        level=level,  # type: ignore[arg-type]
        tier=tier,
        corroboration_count=corroboration,
        published_at_ms=1_779_000_000_000,
        now_ms=1_779_000_000_000,
        title="Routine central bank update",
    )
    assert factors["total"] == expected


def test_importance_is_worldmonitor_55_20_15_10_and_reporting_origin_based() -> None:
    factors = importance_factors(
        level="high",
        tier=1,
        corroboration_count=3,
        published_at_ms=1_779_000_000_000,
        now_ms=1_779_000_000_000,
        title="Central bank statement",
    )
    assert factors == {
        "severity_level": "high",
        "severity_points": 41.25,
        "source_tier": 1,
        "source_points": 20.0,
        "reporting_origin_count": 3,
        "scoring_corroboration_count": 3,
        "corroboration_points": 9.0,
        "recency_points": 10.0,
        "diplomacy_flashpoint_boost": 0,
        "entity_corroboration_boost": 0,
        "total": 80,
    }


def test_top8_keeps_rank_and_caps_reporting_origin_at_three() -> None:
    stories = [
        {
            "story_id": f"story-{index}",
            "importance_score": 100 - index,
            "last_published_at_ms": 100 - index,
            "representative_source_name": "reuters" if index < 5 else f"feed-{index}",
        }
        for index in range(12)
    ]
    selected = select_top_stories(stories)
    assert len(selected) == 8
    assert [row["story_id"] for row in selected[:3]] == ["story-0", "story-1", "story-2"]
    assert "story-3" not in {row["story_id"] for row in selected}


def test_brief_index_lock_degrades_only_invalid_line_and_lead() -> None:
    stories = [
        NewsBriefStory(
            story_id="story-1",
            title="Title one",
            source="Reuters",
            url="https://example.test/1",
            source_count=2,
            importance_score=90,
            level="high",
            category="economic",
        ),
        NewsBriefStory(
            story_id="story-2",
            title="Title two",
            source="AP",
            url="https://example.test/2",
            source_count=2,
            importance_score=80,
            level="medium",
            category="diplomatic",
        ),
    ]
    repaired, validation, degraded = validate_and_repair_brief(
        NewsBriefDraft(
            lead="uncited lead",
            lines=("第一条有效 [1]", "wrong citation [1]"),
            provider="test",
            model="test",
            raw_response="{}",
        ),
        stories,
    )
    assert degraded is True
    assert repaired.lead == "今日重点：Title one [1]"
    assert repaired.lines == ("第一条有效 [1]", "第2条：Title two [2]")
    assert validation["line_fallbacks"] == [2]


def test_source_inventory_is_the_confirmed_us_finance_global_politics_hard_cut() -> None:
    sources = default_sources()
    assert len(sources) == 73
    assert sum(len(source.memberships) for source in sources) == 73
    assert sum("intel" in source.memberships for source in sources) == 12
    assert any(source.name == "6551NEWS" and source.lang == "zh" for source in sources)
    assert sum("crypto" in source.memberships for source in sources) == 16
    assert {source.name for source in sources if source.memberships[0] in {"asia", "middleeast"}} == {
        "Al Jazeera",
        "Nikkei Asia",
        "South China Morning Post",
        "Xinhua",
    }
    assert not {
        "Guardian World",
        "CNN World",
        "NPR News",
        "PBS NewsHour",
        "ABC News",
        "CBS News",
        "NBC News",
        "The Hill",
        "Yahoo Finance",
        "CNA",
        "NDTV",
        "The Hindu",
        "Foreign Policy",
        "Foreign Affairs",
        "Atlantic Council",
        "CSIS",
        "OCCRP",
    } & {source.name for source in sources}
    trump = next(source for source in sources if source.name == "Trump - Truth Social")
    assert trump.tier == 1
    assert trump.memberships == ("politics",)
    wallstengine = next(source for source in sources if source.name == "WallStEngine")
    assert wallstengine.feed_url == (
        "http://rsshub:1200/twitter/user/wallstengine/"
        "includeReplies=0&includeRts=0&showRetweetTextInTitle=1&showQuotedInTitle=0"
    )
    assert wallstengine.source_id == (
        f"news-wallstengine-{hashlib.sha256(wallstengine.feed_url.encode()).hexdigest()[:8]}"
    )
    assert wallstengine.tier == 4
    assert wallstengine.lang == "en"
    assert wallstengine.memberships == ("finance",)
    assert wallstengine.refresh_interval_seconds == 120
    assert not any(len(source.memberships) > 1 for source in sources)
    manifest = [
        {
            "source_id": source.source_id,
            "name": source.name,
            "feed_url": source.feed_url,
            "tier": source.tier,
            "lang": source.lang,
            "memberships": list(source.memberships),
        }
        for source in sources
    ]
    encoded = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == ("5ef085d74d7c9fb7019858c270e8fbe73e783f02533e9c4699f6fdc4d178f1be")
