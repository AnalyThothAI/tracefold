from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tracefold.news.brief import (
    compose_l1_brief,
    parse_brief_synthesis,
    synthesis_system_prompt,
    synthesis_user_prompt,
)
from tracefold.news.identity import cluster_texts, normalize_story_text, story_similarity
from tracefold.news.models import NewsBriefStory
from tracefold.news.ranking import select_top_stories

PINNED_WORLDMONITOR_HEAD = "0e8785c43e6a693990a14181ae0a16066c15fc8c"
NOW_MS = 1_785_600_000_000

_EMOJI_PREFIX = "😀" * 151
IDENTITY_NORMALIZATION_TEXTS = (
    "  Fed — holds,  rates!  ",
    "İ",
    "Alpha-Beta!!! - Reuters",
    "日本銀行が金利を引き上げ、市場に衝撃",
)
IDENTITY_SIMILARITY_PAIRS = (
    (
        "Iran threatens to close Strait of Hormuz if US blockade continues",
        "Iran threatens to close Strait of Hormuz — live updates",
    ),
    (
        "Iran seizes oil tanker in Strait of Hormuz",
        "Iran threatens to close Strait of Hormuz",
    ),
    (
        _EMOJI_PREFIX + " Iran threatens to close Strait of Hormuz",
        _EMOJI_PREFIX + " Iran threatens to close the Strait of Hormuz",
    ),
)
IDENTITY_CLUSTER_GROUPS = (
    ("İ", "i"),
    (
        _EMOJI_PREFIX + " Iran threatens to close Strait of Hormuz",
        _EMOJI_PREFIX + " Iran threatens to close the Strait of Hormuz",
    ),
    (
        "Iran threatens to close Strait of Hormuz if US blockade continues",
        "Iran threatens to close Strait of Hormuz — live updates",
        "Iran seizes oil tanker in Strait of Hormuz",
    ),
)

SELECTOR_STORIES: tuple[dict[str, Any], ...] = (
    tuple(
        {
            "story_id": f"shared-{index}",
            "primary_title": f"Iran war missile attack killed troops in airstrike on base {index}",
            "primary_source": "Shared Wire",
            "primary_published_at_ms": NOW_MS,
            "last_updated_ms": NOW_MS,
            "sources": ["Shared Wire"],
            "upstream_importance_score": 100,
            "is_alert": True,
        }
        for index in range(4)
    )
    + tuple(
        {
            "story_id": f"wire-{index}",
            "primary_title": f"Iran war missile attack killed troops in airstrike on outpost {index}",
            "primary_source": f"Wire {index}",
            "primary_published_at_ms": NOW_MS,
            "last_updated_ms": NOW_MS,
            "sources": [f"Wire {index}"],
            "upstream_importance_score": 100,
            "is_alert": True,
        }
        for index in range(4)
    )
    + (
        {
            "story_id": "corroborated",
            "primary_title": "Routine scheduled cabinet meeting",
            "primary_source": "BBC World",
            "primary_published_at_ms": NOW_MS - 6 * 60 * 60_000,
            "last_updated_ms": NOW_MS - 6 * 60 * 60_000,
            "sources": ["BBC World", "Reuters"],
            "is_alert": False,
        },
    )
)

SYNTHESIS_DATE = "2026-08-07"
SYNTHESIS_RAW = (
    "```json\n"
    + json.dumps(
        {
            "lead": "Iran raises the stakes around Hormuz [1] while Turkey delivers a dramatic rate hike [2].",
            "lines": [
                {"n": 1, "text": "Iran threatens to close the Strait of Hormuz [1]."},
                {"n": 2, "text": "Turkey raises interest rates to 50% [1]."},
            ],
        }
    )
    + "\n``` trailing prose with a stray }"
)

_PINNED_NODE_DRIVER = r"""
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const root = process.cwd();
const identity = await import(pathToFileURL(path.join(root, 'shared/story-identity.js')).href);
const clustering = await import(pathToFileURL(path.join(root, 'scripts/_clustering.mjs')).href);
const brief = await import(pathToFileURL(path.join(root, 'scripts/_insights-brief.mjs')).href);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

Date.now = () => input.nowMs;
const selectorStats = {};
const selected = clustering.selectTopStories(
  structuredClone(input.selectorStories),
  input.selectorLimit,
  selectorStats,
);
const parsed = brief.parseBriefSynthesis(input.synthesis.raw, input.synthesis.stories.length);
const composed = brief.composeSynthesizedBrief(
  input.synthesis.raw,
  input.synthesis.stories,
  {
    sanitizeTitle: (title) => title,
    sourceFromStory: (story) => story.primaryLink
      ? { title: story.primaryTitle, source: story.primarySource, url: story.primaryLink }
      : null,
  },
);

process.stdout.write(JSON.stringify({
  identity: {
    normalizations: input.identity.normalizationTexts.map(identity.normalizeStoryText),
    similarities: input.identity.similarityPairs.map(([left, right]) => identity.storySimilarity(left, right)),
    clusters: input.identity.clusterGroups.map((titles) => identity.clusterTexts(titles)),
  },
  selector: {
    selected: selected.map((story) => ({
      storyId: story.storyId,
      importanceScore: story.importanceScore,
      effectiveImportanceScore: story.effectiveImportanceScore,
    })),
    stats: selectorStats,
  },
  synthesis: {
    systemPrompt: brief.synthesisSystemPrompt(input.synthesis.date),
    userPrompt: brief.synthesisUserPrompt(input.synthesis.stories),
    parsed,
    composed,
  },
}));
"""


def _news_brief_stories() -> tuple[NewsBriefStory, ...]:
    return (
        NewsBriefStory(
            story_id="story-1",
            primary_title="Iran threatens to close Strait of Hormuz",
            primary_source="Reuters",
            primary_link=None,
            primary_published_at_ms=1_786_928_400_000,
            source_count=2,
            unique_source_count=2,
            sources=("Reuters", "BBC"),
            last_updated_ms=1_786_928_400_000,
            member_titles=(),
            source_tier=1,
            upstream_importance_score=90,
            entity_corroboration=False,
            corroboration_source_count=0,
            importance_score=240,
            effective_importance_score=230,
            is_alert=True,
            threat_level="high",
            category="economic",
        ),
        NewsBriefStory(
            story_id="story-2",
            primary_title="Turkey hikes interest rates to 50%",
            primary_source="Bloomberg",
            primary_link="https://example.test/turkey",
            primary_published_at_ms=1_786_928_400_000,
            source_count=1,
            unique_source_count=1,
            sources=("Bloomberg",),
            last_updated_ms=1_786_928_400_000,
            member_titles=(),
            source_tier=1,
            upstream_importance_score=90,
            entity_corroboration=False,
            corroboration_source_count=0,
            importance_score=240,
            effective_importance_score=230,
            is_alert=True,
            threat_level="high",
            category="economic",
        ),
    )


def _camel_story(story: dict[str, Any]) -> dict[str, Any]:
    return {
        "storyId": story["story_id"],
        "primaryTitle": story["primary_title"],
        "primarySource": story["primary_source"],
        "primaryPublishedAtMs": story["primary_published_at_ms"],
        "lastUpdated": story["last_updated_ms"],
        "sources": story["sources"],
        "upstreamImportanceScore": story.get("upstream_importance_score", 0),
        "isAlert": story.get("is_alert", False),
    }


def _camel_brief_story(story: NewsBriefStory) -> dict[str, Any]:
    return {
        "storyId": story.story_id,
        "primaryTitle": story.primary_title,
        "primarySource": story.primary_source,
        "primaryLink": story.primary_link,
        "primaryPublishedAtMs": story.primary_published_at_ms,
        "sourceCount": story.source_count,
        "sources": list(story.sources),
        "memberTitles": list(story.member_titles),
        "entityCorroboration": story.entity_corroboration,
    }


@pytest.fixture(scope="module")
def pinned_worldmonitor_output() -> dict[str, Any]:
    default_repo = Path(__file__).resolve().parents[2] / "worldmonitor"
    repo = Path(os.environ.get("TRACEFOLD_WORLDMONITOR_REPO", default_repo)).expanduser().resolve()
    if not repo.is_dir():
        pytest.skip(f"pinned WorldMonitor sibling is unavailable: {repo}")

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    assert head == PINNED_WORLDMONITOR_HEAD, (
        f"WorldMonitor differential requires {PINNED_WORLDMONITOR_HEAD}, found {head} at {repo}"
    )

    stories = _news_brief_stories()
    payload = {
        "nowMs": NOW_MS,
        "identity": {
            "normalizationTexts": IDENTITY_NORMALIZATION_TEXTS,
            "similarityPairs": IDENTITY_SIMILARITY_PAIRS,
            "clusterGroups": IDENTITY_CLUSTER_GROUPS,
        },
        "selectorStories": [_camel_story(story) for story in SELECTOR_STORIES],
        "selectorLimit": 5,
        "synthesis": {
            "date": SYNTHESIS_DATE,
            "raw": SYNTHESIS_RAW,
            "stories": [_camel_brief_story(story) for story in stories],
        },
    }
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", _PINNED_NODE_DRIVER],
        cwd=repo,
        input=json.dumps(payload),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    parsed = json.loads(completed.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_story_identity_matches_pinned_normalization_similarity_and_clusters(
    pinned_worldmonitor_output: dict[str, Any],
) -> None:
    actual = pinned_worldmonitor_output["identity"]

    assert actual["normalizations"] == [normalize_story_text(text) for text in IDENTITY_NORMALIZATION_TEXTS]
    for pinned_score, pair in zip(actual["similarities"], IDENTITY_SIMILARITY_PAIRS, strict=True):
        assert story_similarity(*pair) == pytest.approx(pinned_score, abs=1e-12)
    assert actual["clusters"] == [cluster_texts(group) for group in IDENTITY_CLUSTER_GROUPS]


def test_public_selector_matches_pinned_order_scores_and_stats(
    pinned_worldmonitor_output: dict[str, Any],
) -> None:
    stats: dict[str, int | bool] = {}
    selected = select_top_stories(SELECTOR_STORIES, now_ms=NOW_MS, limit=5, stats=stats)
    pinned = pinned_worldmonitor_output["selector"]

    assert [story["story_id"] for story in selected] == [story["storyId"] for story in pinned["selected"]]
    for local, upstream in zip(selected, pinned["selected"], strict=True):
        assert local["importance_score"] == pytest.approx(upstream["importanceScore"], abs=1e-12)
        assert local["effective_importance_score"] == pytest.approx(upstream["effectiveImportanceScore"], abs=1e-12)
    assert stats == {
        "considered": pinned["stats"]["considered"],
        "admissibility_dropped": pinned["stats"]["admissibilityDropped"],
        "source_cap_dropped": pinned["stats"]["sourceCapDropped"],
        "overflow_dropped": pinned["stats"]["overflowDropped"],
        "brief_eligible_considered": pinned["stats"]["briefEligibleConsidered"],
        "brief_eligible_promoted": pinned["stats"]["briefEligiblePromoted"],
    }


def test_synthesis_prompt_parser_and_composer_match_pinned_helpers(
    pinned_worldmonitor_output: dict[str, Any],
) -> None:
    stories = _news_brief_stories()
    pinned = pinned_worldmonitor_output["synthesis"]

    assert synthesis_system_prompt(SYNTHESIS_DATE) == pinned["systemPrompt"]
    assert synthesis_user_prompt(stories) == pinned["userPrompt"]

    parsed = parse_brief_synthesis(SYNTHESIS_RAW, len(stories))
    assert parsed is not None
    assert {"lead": parsed[0], "lines": [{"n": n, "text": text} for n, text in parsed[1]]} == pinned["parsed"]

    composed = compose_l1_brief(SYNTHESIS_RAW, stories, provider="ollama", model="llama3.1:8b")
    assert composed is not None
    assert composed.world_brief == pinned["composed"]["lead"]
    assert [line.model_dump() for line in composed.brief_story_lines] == pinned["composed"]["lines"]
    assert [
        {"title": source.title, "source": source.source, "url": source.url} for source in composed.sources
    ] == pinned["composed"]["sources"]
    assert len(composed.validation["line_fallbacks"]) == pinned["composed"]["hallucinatedLines"]
    assert composed.validation["stripped_citations"] == pinned["composed"]["strippedCitations"]
