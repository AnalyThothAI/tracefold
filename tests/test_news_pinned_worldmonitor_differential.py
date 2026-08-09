from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import tracefold.news.brief as brief_module
from tracefold.news.brief import (
    compose_l1_brief,
    parse_brief_synthesis,
    synthesis_system_prompt,
    synthesis_user_prompt,
)
from tracefold.news.classification import classify_by_keyword
from tracefold.news.identity import (
    cluster_texts,
    normalize_story_canonical_title,
    normalize_story_text,
    public_story_title_hash,
    story_similarity,
    utf16_length,
    utf16_slice,
    web_usv_string,
)
from tracefold.news.models import NewsBriefStory
from tracefold.news.projection import NewsProjectionSnapshot, compute_news_story_projection
from tracefold.news.ranking import importance_factors, select_top_stories

PINNED_WORLDMONITOR_HEAD = "0e8785c43e6a693990a14181ae0a16066c15fc8c"
PINNED_WORLDMONITOR_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "worldmonitor_news_0e8785c43e6a.json"
NOW_MS = 1_785_600_000_000

_EMOJI_PREFIX = "😀" * 151
IDENTITY_NORMALIZATION_TEXTS = (
    "  Fed — holds,  rates!  ",
    "İ",
    "Alpha-Beta!!! - Reuters",
    "日本銀行が金利を引き上げ、市場に衝撃",
    "Alpha\ufeffBeta launches satellite today",
    "A\U0006139c\U00017237\U00011db7\U00036a54\U000353a7 İ \ua7ce \U00016ea0 War 12",
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
    ("Russia border talks-İ.com", "Russia border talks"),
    ("Alpha\ufeffBeta launches satellite today", "Alpha Beta launches satellite today now"),
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
    ("Alpha\ufeffBeta launches satellite today", "Alpha Beta launches satellite today now"),
)
CANONICAL_HASH_TITLES = (
    "a" * 119 + "𝔸" + "z",
    "Alpha-Beta!!!",
    "AlphaBeta???",
    "Alpha\ufeffBeta launches satellite today",
    "A\U0006139c\U00017237\U00011db7\U00036a54\U000353a7 İ \ua7ce \U00016ea0 War 12",
)
CLASSIFIER_TITLES = (
    "中国war升级",
    "Invasion 5 years ago纪念",
    "Invasion 5\ufeffyears ago",
    "Routine central bank update",
)
IMPORTANCE_CASES = (
    {
        "level": "info",
        "source": "unknown-source",
        "tier": 4,
        "corroboration_count": 1,
        "published_at_ms": NOW_MS + 12 * 60 * 60_000,
        "title": "Scheduled central bank update",
    },
)
SEED_STAGE_TITLES = (
    "War",
    "12345678𝔸",
    "123456789𝔸",
)
SOURCE_ORDER_ORIGINS = ("@z", "_a", "zulu", "éclair", "AP", "ap")
NUMERIC_GROUNDING_CASES = (
    ("Rate reaches 12.34561%.", "Rate reaches 12.34562%."),
    ("死亡two人", "死亡one人"),
    ("Digits ١٢ here.", "Digits 12 here."),
    ("Rate is 12%.", "Rate is \ua7ce12%."),
)
PROPER_NOUN_GROUNDING_CASES = (
    ("Strasse halted talks.", "Straße halted talks."),
    ("Alpha moved.", "\U00011db7Alpha moved."),
    ("Foo\ua7ceBar announced.", "Foo Bar announced."),
    ("Beirut'ſ announces talks.", "Beirut announces talks."),
)
NUMERIC_FACT_TEXTS = ("Values 0.0000001, 0.000001, 100000000000000000000 and 1000000000 trillion.",)
PARSER_COERCION_INDEXES = (True, [1], "0x1")
CODE_FENCE_TEXTS = ("```json\n{}", "```JSON\n{}", "```jſon\n{}")

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
DOTTED_SYNTHESIS_RAW = json.dumps(
    {
        "lead": (
            "Iran met 中U.S. officials during regional security talks [1]. Iran held talks with 中U.S. officials [1]."
        ),
        "lines": [{"n": 1, "text": "Iran met 中U.S. officials during regional security talks [1]."}],
    }
)

_PINNED_NODE_DRIVER = r"""
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';

const root = process.cwd();
const identity = await import(pathToFileURL(path.join(root, 'shared/story-identity.js')).href);
const dedup = await import(pathToFileURL(path.join(root, 'server/worldmonitor/news/v1/dedup.mjs')).href);
const classifier = await import(pathToFileURL(path.join(root, 'server/worldmonitor/news/v1/_classifier.ts')).href);
const clustering = await import(pathToFileURL(path.join(root, 'scripts/_clustering.mjs')).href);
const brief = await import(pathToFileURL(path.join(root, 'scripts/_insights-brief.mjs')).href);
const briefCore = await import(pathToFileURL(path.join(root, 'scripts/shared/brief-llm-core.js')).href);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

const extractObjectLiteral = (source, name) => {
  const match = source.match(new RegExp(`(?:export\\s+)?const\\s+${name}\\b[^=]*=\\s*\\{`));
  if (!match) throw new Error(`missing ${name}`);
  const start = match.index + match[0].length - 1;
  let depth = 1;
  let cursor = start + 1;
  while (cursor < source.length && depth > 0) {
    if (source[cursor] === '{') depth++;
    else if (source[cursor] === '}') depth--;
    cursor++;
  }
  return new Function(`return (${source.slice(start, cursor)});`)();
};
const extractFunctionBody = (source, signature) => {
  const index = source.indexOf(signature);
  if (index < 0) throw new Error(`missing ${signature}`);
  const parametersStart = source.indexOf('(', index);
  let parametersDepth = 1;
  let cursor = parametersStart + 1;
  while (cursor < source.length && parametersDepth > 0) {
    if (source[cursor] === '(') parametersDepth++;
    else if (source[cursor] === ')') parametersDepth--;
    cursor++;
  }
  const bodyStart = source.indexOf('{', cursor);
  let bodyDepth = 1;
  cursor = bodyStart + 1;
  while (cursor < source.length && bodyDepth > 0) {
    if (source[cursor] === '{') bodyDepth++;
    else if (source[cursor] === '}') bodyDepth--;
    cursor++;
  }
  return source.slice(bodyStart + 1, cursor - 1);
};
const digestSource = fs.readFileSync(path.join(root, 'server/worldmonitor/news/v1/list-feed-digest.ts'), 'utf8');
const seedSource = fs.readFileSync(path.join(root, 'scripts/seed-insights.mjs'), 'utf8');
const sourceTiers = JSON.parse(fs.readFileSync(path.join(root, 'shared/source-tiers.json'), 'utf8'));
const digestImportance = new Function(
  'level', 'source', 'corroborationCount', 'publishedAt', 'context',
  'SEVERITY_SCORES', 'SCORE_WEIGHTS', 'SOURCE_TIERS',
  `
    function getSourceTier(name) { return SOURCE_TIERS[name] ?? 4; }
    function diplomacyFlashpointBoost() { return 0; }
    function entityCorroborationScore() { return 0; }
    ${extractFunctionBody(digestSource, 'function computeImportanceScore(')}
  `,
);
const digestSeverityScores = extractObjectLiteral(digestSource, 'SEVERITY_SCORES');
const digestScoreWeights = extractObjectLiteral(digestSource, 'SCORE_WEIGHTS');
const seedHeadlineLimit = Number(seedSource.match(/const MAX_HEADLINE_LEN\s*=\s*([0-9]+)/)[1]);
const seedSanitizeTitle = new Function(
  'title', 'MAX_HEADLINE_LEN',
  extractFunctionBody(seedSource, 'function sanitizeTitle('),
);
const seedClipText = new Function('value', 'maxLen', extractFunctionBody(seedSource, 'function clipText('));
const seedNormalizeBriefSourceUrl = new Function(
  'value',
  extractFunctionBody(seedSource, 'function normalizeBriefSourceUrl('),
);

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
const dottedComposed = brief.composeSynthesizedBrief(
  input.dottedSynthesis.raw,
  [input.dottedSynthesis.story],
  {
    sanitizeTitle: (title) => title,
    sourceFromStory: (story) => story.primaryLink
      ? { title: story.primaryTitle, source: story.primarySource, url: story.primaryLink }
      : null,
  },
);
const canonicalSourceSuffix = new RegExp(
  '\\s*[-\\u2013\\u2014]\\s*'
  + '(?:reuters|ap news|bbc|cnn|al jazeera|france 24|dw news|pbs newshour|cbs news|nbc|abc|'
  + 'associated press|the guardian|nos nieuws|tagesschau|cnbc|the national)\\s*$',
);
const normalizeCanonicalTitle = (title) => title
  .toLowerCase()
  .replace(/\s*[-\u2013\u2014]\s*[\w\s.]+\.(?:com|org|net|co\.uk)\s*$/, '')
  .replace(canonicalSourceSuffix, '')
  .replace(/[^\p{L}\p{N}\s]/gu, '')
  .replace(/\s+/g, ' ')
  .trim()
  .slice(0, 120);
const sha256Hex = async (text) => createHash('sha256')
  .update(new TextEncoder().encode(text))
  .digest('hex');
const canonicalItems = input.identity.canonicalHashTitles.map((title, index) => ({
  title,
  source: `source-${index}`,
  publishedAt: index,
}));
const canonicalAssignments = await dedup.assignStoryIdentity(
  canonicalItems,
  normalizeCanonicalTitle,
  sha256Hex,
);

process.stdout.write(JSON.stringify({
  identity: {
    normalizations: input.identity.normalizationTexts.map(identity.normalizeStoryText),
    similarities: input.identity.similarityPairs.map(([left, right]) => identity.storySimilarity(left, right)),
    clusters: input.identity.clusterGroups.map((titles) => identity.clusterTexts(titles)),
    canonicalHashes: canonicalItems.map((item) => canonicalAssignments.get(item).titleHash),
  },
  classifications: input.classifierTitles.map((title) => classifier.classifyByKeyword(title)),
  importance: input.importanceCases.map((item) => digestImportance(
    item.level,
    item.source,
    item.corroborationCount,
    item.publishedAtMs,
    {},
    digestSeverityScores,
    digestScoreWeights,
    sourceTiers,
  )),
  seedStageEligible: input.seedStageTitles.map((title) => (
    seedSanitizeTitle(title, seedHeadlineLimit).length > 10
  )),
  sourceOrdering: clustering.clusterItems(input.sourceOrderOrigins.map((source) => ({
    title: 'Central bank announces emergency policy meeting',
    source,
    tier: 4,
    pubDate: input.nowMs,
  })))[0].sources,
  grounding: {
    numeric: input.numericGroundingCases.map(([summary, ground]) => (
      briefCore.validateNoHallucinatedFacts(summary, ground)
    )),
    properNouns: input.properNounGroundingCases.map(([summary, ground]) => (
      briefCore.validateNoHallucinatedProperNouns(summary, ground)
    )),
    facts: input.numericFactTexts.map((value) => [...briefCore.extractNumericFacts(value)].sort()),
  },
  webHelpers: {
    clipped: seedClipText(input.webHelpers.clipText, input.webHelpers.clipLimit),
    sanitized: seedSanitizeTitle(input.webHelpers.sanitizeTitle, seedHeadlineLimit),
    urls: input.webHelpers.urls.map(seedNormalizeBriefSourceUrl),
  },
  parserEdges: {
    fenceCleanup: input.codeFenceTexts.map((value) => value.replace(/```(?:json)?/gi, '')),
    coercions: input.parserCoercionIndexes.map((n) => brief.parseBriefSynthesis(JSON.stringify({
      lead: '😀'.repeat(20),
      lines: [{ n, text: '😀'.repeat(8) }],
    }), 1)),
    nonfinite: brief.parseBriefSynthesis(
      `{"lead":"${'L'.repeat(40)}","lines":[],"extra":NaN}`,
      1,
    ),
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
    dottedComposed,
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


def _pinned_worldmonitor_payload() -> dict[str, Any]:
    stories = _news_brief_stories()
    return {
        "nowMs": NOW_MS,
        "identity": {
            "normalizationTexts": IDENTITY_NORMALIZATION_TEXTS,
            "similarityPairs": IDENTITY_SIMILARITY_PAIRS,
            "clusterGroups": IDENTITY_CLUSTER_GROUPS,
            "canonicalHashTitles": CANONICAL_HASH_TITLES,
        },
        "selectorStories": [_camel_story(story) for story in SELECTOR_STORIES],
        "classifierTitles": CLASSIFIER_TITLES,
        "importanceCases": [
            {
                "level": case["level"],
                "source": case["source"],
                "corroborationCount": case["corroboration_count"],
                "publishedAtMs": case["published_at_ms"],
            }
            for case in IMPORTANCE_CASES
        ],
        "seedStageTitles": SEED_STAGE_TITLES,
        "sourceOrderOrigins": SOURCE_ORDER_ORIGINS,
        "numericGroundingCases": NUMERIC_GROUNDING_CASES,
        "properNounGroundingCases": PROPER_NOUN_GROUNDING_CASES,
        "numericFactTexts": NUMERIC_FACT_TEXTS,
        "parserCoercionIndexes": PARSER_COERCION_INDEXES,
        "codeFenceTexts": CODE_FENCE_TEXTS,
        "webHelpers": {
            "clipText": "😀" * 80 + " title",
            "clipLimit": 160,
            "sanitizeTitle": "a" * 499 + "𝔸" + "z",
            "urls": [
                "https://EXAMPLE.com:443",
                "https://example.com:bad/path",
                r"http:\\example.com\a",
            ],
        },
        "selectorLimit": 5,
        "synthesis": {
            "date": SYNTHESIS_DATE,
            "raw": SYNTHESIS_RAW,
            "stories": [_camel_brief_story(story) for story in stories],
        },
        "dottedSynthesis": {
            "raw": DOTTED_SYNTHESIS_RAW,
            "story": {
                **_camel_brief_story(stories[0]),
                "primaryTitle": "Iran met 中U.S. officials during regional security talks",
                "primaryLink": "https://example.test/talks",
                "memberTitles": [],
            },
        },
    }


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _run_pinned_worldmonitor(repo: Path, payload: dict[str, Any]) -> dict[str, Any]:
    completed = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "--eval", _PINNED_NODE_DRIVER],
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


@pytest.fixture(scope="module")
def pinned_worldmonitor_output() -> dict[str, Any]:
    payload = _pinned_worldmonitor_payload()
    frozen = json.loads(PINNED_WORLDMONITOR_GOLDEN.read_text())
    assert frozen["fixture_format"] == "tracefold.worldmonitor-news-differential.v1"
    assert frozen["worldmonitor_head"] == PINNED_WORLDMONITOR_HEAD
    assert frozen["driver_sha256"] == hashlib.sha256(_PINNED_NODE_DRIVER.encode()).hexdigest()
    assert frozen["payload_sha256"] == _canonical_json_sha256(payload)
    output = frozen["output"]
    assert isinstance(output, dict)

    default_repo = Path(__file__).resolve().parents[2] / "worldmonitor"
    repo = Path(os.environ.get("TRACEFOLD_WORLDMONITOR_REPO", default_repo)).expanduser().resolve()
    if not repo.exists():
        return output
    assert repo.is_dir(), f"WorldMonitor differential path is not a directory: {repo}"

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
    actual = _run_pinned_worldmonitor(repo, payload)
    assert actual == output
    return actual


def test_story_identity_matches_pinned_normalization_similarity_and_clusters(
    pinned_worldmonitor_output: dict[str, Any],
) -> None:
    actual = pinned_worldmonitor_output["identity"]

    assert actual["normalizations"] == [normalize_story_text(text) for text in IDENTITY_NORMALIZATION_TEXTS]
    for pinned_score, pair in zip(actual["similarities"], IDENTITY_SIMILARITY_PAIRS, strict=True):
        assert story_similarity(*pair) == pytest.approx(pinned_score, abs=1e-12)
    assert actual["clusters"] == [cluster_texts(group) for group in IDENTITY_CLUSTER_GROUPS]
    assert actual["canonicalHashes"] == [
        public_story_title_hash(normalize_story_canonical_title(title)) for title in CANONICAL_HASH_TITLES
    ]
    assert pinned_worldmonitor_output["classifications"] == [
        classify_by_keyword(title, now_ms=NOW_MS).model_dump() for title in CLASSIFIER_TITLES
    ]
    assert pinned_worldmonitor_output["importance"] == [
        importance_factors(
            level=case["level"],
            tier=case["tier"],
            corroboration_count=case["corroboration_count"],
            published_at_ms=case["published_at_ms"],
            now_ms=NOW_MS,
            title=case["title"],
        )["total"]
        for case in IMPORTANCE_CASES
    ]
    assert pinned_worldmonitor_output["seedStageEligible"] == [
        utf16_length(web_usv_string(utf16_slice(title, 500)).strip()) > 10 for title in SEED_STAGE_TITLES
    ]


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


def test_public_cluster_and_grounding_edges_match_pinned_web_semantics(
    pinned_worldmonitor_output: dict[str, Any],
) -> None:
    source_rows = tuple(
        {
            "item_id": f"source-{index}",
            "source_id": "opennews",
            "canonical_url": None,
            "reporting_origin": origin,
            "title": "Central bank announces emergency policy meeting",
            "description": "",
            "published_at_ms": NOW_MS + index,
            "tier": 4,
            "source_kind": "opennews",
            "source_position": None,
            "memberships": (),
        }
        for index, origin in enumerate(SOURCE_ORDER_ORIGINS)
    )
    projection = compute_news_story_projection(
        NewsProjectionSnapshot(
            input_fingerprint="f" * 64,
            scoring_epoch_ms=NOW_MS,
            current_input_fingerprint=None,
            rows=source_rows,
        )
    )

    assert projection["selection_snapshot"]["top_stories"][0]["sources"] == pinned_worldmonitor_output["sourceOrdering"]
    local_numeric_results: list[dict[str, object]] = []
    for summary, ground in NUMERIC_GROUNDING_CASES:
        missing = brief_module._numeric_facts(summary) - brief_module._numeric_facts(ground)
        local_numeric_results.append(
            {"ok": True} if not missing else {"ok": False, "hallucinated": [sorted(missing)[0]]}
        )
    assert pinned_worldmonitor_output["grounding"]["numeric"] == local_numeric_results
    assert pinned_worldmonitor_output["grounding"]["properNouns"] == [
        (
            {"ok": True}
            if (local := brief_module.validate_no_hallucinated_proper_nouns(summary, ground))[0]
            else {"ok": False, "hallucinated": list(local[1])}
        )
        for summary, ground in PROPER_NOUN_GROUNDING_CASES
    ]
    assert pinned_worldmonitor_output["grounding"]["facts"] == [
        sorted(brief_module._numeric_facts(value)) for value in NUMERIC_FACT_TEXTS
    ]
    pinned_web_helpers = pinned_worldmonitor_output["webHelpers"]
    assert {
        "clipped": web_usv_string(pinned_web_helpers["clipped"]),
        "sanitized": web_usv_string(pinned_web_helpers["sanitized"]),
        "urls": pinned_web_helpers["urls"],
    } == {
        "clipped": brief_module._clip_text("😀" * 80 + " title", 160),
        "sanitized": brief_module._sanitize_title("a" * 499 + "𝔸" + "z"),
        "urls": [
            brief_module._valid_http_url("https://EXAMPLE.com:443"),
            brief_module._valid_http_url("https://example.com:bad/path"),
            brief_module._valid_http_url(r"http:\\example.com\a"),
        ],
    }
    lead = "😀" * 20
    line = "😀" * 8
    assert pinned_worldmonitor_output["parserEdges"] == {
        "fenceCleanup": [brief_module._CODE_FENCE_RE.sub("", value) for value in CODE_FENCE_TEXTS],
        "coercions": [
            {"lead": parsed[0], "lines": [{"n": n, "text": text} for n, text in parsed[1]]}
            if (parsed := parse_brief_synthesis(json.dumps({"lead": lead, "lines": [{"n": index, "text": line}]}), 1))
            else None
            for index in PARSER_COERCION_INDEXES
        ],
        "nonfinite": None,
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

    dotted_story = stories[0].model_copy(
        update={
            "primary_title": "Iran met 中U.S. officials during regional security talks",
            "primary_link": "https://example.test/talks",
            "member_titles": (),
        }
    )
    dotted = compose_l1_brief(DOTTED_SYNTHESIS_RAW, (dotted_story,), provider="ollama", model="llama3.1:8b")
    assert dotted is not None
    assert pinned["dottedComposed"] is not None
    assert dotted.world_brief == pinned["dottedComposed"]["lead"]
    assert [line.model_dump() for line in dotted.brief_story_lines] == pinned["dottedComposed"]["lines"]
