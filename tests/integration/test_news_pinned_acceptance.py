from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_settings_storage,
    prepare_postgres_database,
    reset_postgres_schema,
)
from tests.support.news import compute_news_public_clusters, rebuild_news_projection
from tracefold.app.http.app import create_app
from tracefold.integrations.news_ai import ProviderChainNewsBriefPublisher
from tracefold.news.models import NewsBriefStory
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.projection import NewsProjectionSnapshot
from tracefold.news.ranking import score_public_cluster
from tracefold.news.repository import NewsRepository
from tracefold.news.sources import _PINNED_SOURCE_TIERS, opennews_source
from tracefold.platform.config.settings import NewsPushSettings, NewsSettings, Settings

PINNED_WORLDMONITOR_HEAD = "0e8785c43e6a693990a14181ae0a16066c15fc8c"
NOW_MS = 1_786_082_400_000
OPENNEWS_STRATEGY_IDS = frozenset({"1018", "1019"})
_PINNED_GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures/worldmonitor_news_acceptance_0e8785c43e6a.json"
_PINNED_SOURCE_NAMES = {source.strip().lower(): source for source in _PINNED_SOURCE_TIERS}


def _frame(
    record_id: str,
    text: str,
    origin: str,
    offset_minutes: int,
    *,
    description: str = "",
    link: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    wire_text = f"{text}<br>{description}" if description else text
    params: dict[str, Any] = {
        "id": record_id,
        "text": wire_text,
        "description": description,
        "newsType": origin,
        "source": author or origin,
        "engineType": "news",
        "ts": NOW_MS - offset_minutes * 60_000,
        "strategy": {
            "id": "1018",
            "name": "News Score > 70",
            "sourceType": "news",
        },
    }
    if link is not None:
        params["link"] = link
    return {"method": "strategy.triggered", "params": params}


# One frozen provider corpus owns the acceptance seam. It deliberately has a
# rank-nine corroborated candidate, a fourth item from one primary source,
# overflow and inadmissible candidates, two cross-cluster Iran/talks entity
# signals, a real two-member Story, Twitter authors, HTML/body pollution, and
# linkless evidence.
OPENNEWS_FRAMES = (
    _frame(
        "cap-1",
        "Deadly missile airstrike attack kills troops at northern airbase during combat"
        "<br><b>Officials</b> assess damage &amp; emergency response.",
        "Cap Wire",
        1,
        link="https://example.test/cap-1?utm_source=opennews",
    ),
    _frame(
        "cap-2",
        "Coastal earthquake disaster follows military attack and kills dozens with casualties after emergency collapse",
        "Cap Wire",
        2,
        link=None,
    ),
    _frame("cap-3", "Coup sparks violent clashes in capital district", "Cap Wire", 3),
    _frame(
        "cap-4",
        "Wildfire catastrophe follows military attack, kills troops, and triggers emergency evacuation after collapse",
        "Cap Wire",
        4,
    ),
    _frame(
        "alert-1",
        "Navy warship missile strike destroys hostile drone fleet during sea combat attack",
        "Alert One",
        5,
    ),
    _frame("alert-2", "Bomb attack leaves casualties near central station", "Alert Two", 6),
    _frame(
        "alert-3",
        "Violent protest clashes follow military crackdown shooting with civilian casualties",
        "Alert Three",
        7,
    ),
    _frame(
        "alert-4",
        "Tsunami catastrophe follows military attack, kills residents, and leaves casualties in coastal emergency",
        "Alert Four",
        8,
    ),
    _frame(
        "alert-5",
        "Artillery offensive attack leaves wounded troops and casualties near border crossing",
        "Alert Five",
        9,
    ),
    _frame(
        "alert-6",
        "Deadly shooting massacre attack kills civilians with casualties in city market",
        "Alert Six",
        10,
    ),
    _frame(
        "alert-7",
        "Hurricane catastrophe sparks violent clashes, kills residents, and leaves thousands injured after collapse",
        "Alert Seven",
        11,
    ),
    _frame(
        "entity-1",
        "Iran talks resume over bilateral agreement",
        "Twitter",
        12,
        author="Diplomat_A",
        link="https://example.test/entity-1",
    ),
    _frame(
        "entity-2",
        "China delegation observes Iran talks on energy cooperation",
        "Twitter",
        13,
        author="Diplomat_B",
        link="https://example.test/entity-2",
    ),
    _frame(
        "routine-1",
        "Central bank schedules routine policy meeting next week",
        "Routine Alpha",
        14,
        description="Independent reporting confirms the schedule and the policy meeting agenda.",
    ),
    _frame(
        "routine-2",
        "Central bank schedules routine policy meeting next week",
        "Routine Beta",
        15,
    ),
    _frame("drop-1", "Local museum opens seasonal art exhibition", "Local Daily", 16),
    _frame("drop-2", "University publishes annual library schedule", "Campus Wire", 17),
    _frame(
        "tier-fallback-newer",
        "Central bank releases routine monthly statistical bulletin",
        "Field Wire",
        18,
    ),
    _frame(
        "tier-reuters-older",
        "Central bank releases routine monthly statistical bulletin",
        "Reuters",
        19,
    ),
)
NEXT_TURN_FRAME = _frame(
    "next-turn",
    "Major explosion kills troops and triggers emergency response",
    "Next Wire",
    -60,
    link="https://example.test/next-turn",
)

CANONICAL_OPENNEWS_GOLDEN = {
    "cap-1": {
        "title": "Deadly missile airstrike attack kills troops at northern airbase during combat",
        "description": "Officials assess damage & emergency response.",
        "reporting_origin": "cap wire",
        "canonical_url": "https://example.test/cap-1",
    },
    "cap-2": {
        "title": (
            "Coastal earthquake disaster follows military attack and kills dozens with casualties "
            "after emergency collapse"
        ),
        "description": "",
        "reporting_origin": "cap wire",
        "canonical_url": None,
    },
    "entity-1": {
        "title": "Iran talks resume over bilateral agreement",
        "description": "",
        "reporting_origin": "diplomat_a",
        "canonical_url": "https://example.test/entity-1",
    },
    "entity-2": {
        "title": "China delegation observes Iran talks on energy cooperation",
        "description": "",
        "reporting_origin": "diplomat_b",
        "canonical_url": "https://example.test/entity-2",
    },
}

_PINNED_DRIVER = r"""
import fs from 'node:fs';
import os from 'node:os';
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

const source = fs.readFileSync(path.join(root, 'server/worldmonitor/news/v1/list-feed-digest.ts'), 'utf8');
const seedSource = fs.readFileSync(path.join(root, 'scripts/seed-insights.mjs'), 'utf8');
const diplomacyKeywordsData = JSON.parse(fs.readFileSync(path.join(root, 'shared/diplomacy-keywords.json'), 'utf8'));
const sourceTiers = JSON.parse(fs.readFileSync(path.join(root, 'shared/source-tiers.json'), 'utf8'));
const extractFunctionBody = (text, signature) => {
  const index = text.indexOf(signature);
  if (index < 0) throw new Error(`missing ${signature}`);
  const parametersStart = text.indexOf('(', index);
  let parametersDepth = 1;
  let cursor = parametersStart + 1;
  while (cursor < text.length && parametersDepth > 0) {
    if (text[cursor] === '(') parametersDepth++;
    else if (text[cursor] === ')') parametersDepth--;
    cursor++;
  }
  const bodyStart = text.indexOf('{', cursor);
  let bodyDepth = 1;
  cursor = bodyStart + 1;
  while (cursor < text.length && bodyDepth > 0) {
    if (text[cursor] === '{') bodyDepth++;
    else if (text[cursor] === '}') bodyDepth--;
    cursor++;
  }
  return text.slice(bodyStart + 1, cursor - 1);
};
const extractFunctionDeclaration = (text, signature) => {
  const index = text.indexOf(signature);
  if (index < 0) throw new Error(`missing ${signature}`);
  const start = text.lastIndexOf('\n', index) + 1;
  const parametersStart = text.indexOf('(', index);
  let parametersDepth = 1;
  let cursor = parametersStart + 1;
  while (cursor < text.length && parametersDepth > 0) {
    if (text[cursor] === '(') parametersDepth++;
    else if (text[cursor] === ')') parametersDepth--;
    cursor++;
  }
  const bodyStart = text.indexOf('{', cursor);
  let bodyDepth = 1;
  cursor = bodyStart + 1;
  while (cursor < text.length && bodyDepth > 0) {
    if (text[cursor] === '{') bodyDepth++;
    else if (text[cursor] === '}') bodyDepth--;
    cursor++;
  }
  return text.slice(start, cursor).replace(/^export /, '');
};
const seedHelpers = new Function(`
  const INSIGHTS_RUN_META = Symbol('worldmonitor.insightsRunMeta');
  const INSIGHTS_RUN_OUTCOMES = Object.freeze({ LKG_PRESERVED: 'lkg_preserved' });
  ${extractFunctionDeclaration(seedSource, 'function attachInsightsRunMeta(')}
  ${extractFunctionDeclaration(seedSource, 'function insightsRunMeta(')}
  ${extractFunctionDeclaration(seedSource, 'function decorateInsightsRun(')}
  ${extractFunctionDeclaration(seedSource, 'function publishInsightsPayload(')}
  ${extractFunctionDeclaration(seedSource, 'function declareRecords(')}
  ${extractFunctionDeclaration(seedSource, 'function validateInsightsPayload(')}
  return { decorateInsightsRun, publishInsightsPayload, validateInsightsPayload };
`)();
const resolveInsightsFallbackStatus = new Function(
  'synthesisFailureCode',
  'legacyStatus',
  extractFunctionBody(seedSource, 'function resolveInsightsFallbackStatus('),
);
const digestModuleSource = `
  const diplomacyKeywordsData = ${JSON.stringify(diplomacyKeywordsData)};
  const sourceTiers = ${JSON.stringify(sourceTiers)};
  const DIPLOMACY_KEYWORDS = diplomacyKeywordsData.diplomacyKeywords;
  const FLASHPOINT_SCORING_KEYWORDS = diplomacyKeywordsData.flashpointKeywords;
  const DIPLOMACY_FLASHPOINT_PAIRS = diplomacyKeywordsData.diplomacyFlashpointPairs;
  const DIPLOMACY_FLASHPOINT_BOOST = 18;
  const ENTITY_CORROBORATION_SCORE_PER_SOURCE = 4;
  const ENTITY_CORROBORATION_WINDOW_MS = 24 * 60 * 60 * 1000;
  const DIPLOMACY_SEVERITY_PROMOTION_MIN_TIER12_SOURCES = 3;
  const SEVERITY_SCORES = { critical: 100, high: 75, medium: 50, low: 25, info: 0 };
  const SCORE_WEIGHTS = { severity: 0.55, sourceTier: 0.2, corroboration: 0.15, recency: 0.1 };
  const getSourceTier = (name) => sourceTiers[name] ?? 4;
  const hasHistoricalMarker = () => false;
  ${extractFunctionDeclaration(source, 'function normalizeScoringText(')}
  ${extractFunctionDeclaration(source, 'function containsKeywordToken(')}
  ${extractFunctionDeclaration(source, 'function hasAnySignal(')}
  ${extractFunctionDeclaration(source, 'function hasDiplomacyFlashpointSignal(')}
  ${extractFunctionDeclaration(source, 'function promoteDiplomacySeverity(')}
  ${extractFunctionDeclaration(source, 'function diplomacyFlashpointBoost(')}
  ${extractFunctionDeclaration(source, 'function entityCorroborationScore(')}
  ${extractFunctionDeclaration(source, 'function computeImportanceScore(')}
  ${extractFunctionDeclaration(source, 'function entityKeysForTitle(')}
  ${extractFunctionDeclaration(source, 'function computeEntityCorroborationSignals(')}
  export { computeEntityCorroborationSignals, promoteDiplomacySeverity, computeImportanceScore };
`;
const digestTempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'tracefold-pinned-digest-'));
const digestTempPath = path.join(digestTempRoot, 'helpers.ts');
fs.writeFileSync(digestTempPath, digestModuleSource);
const digestHelpers = await import(pathToFileURL(digestTempPath).href);
fs.rmSync(digestTempRoot, { recursive: true, force: true });
const normalizeCanonicalTitle = new Function('title', extractFunctionBody(source, 'function normalizeTitle('));
const categorizeStory = new Function('title', extractFunctionBody(seedSource, 'function categorizeStory('));
const sha256Hex = async (text) => createHash('sha256').update(new TextEncoder().encode(text)).digest('hex');
const sha256Sync = (text) => createHash('sha256').update(new TextEncoder().encode(text)).digest('hex');

Date.now = () => input.nowMs;
const items = structuredClone(input.items);
const assignments = await dedup.assignStoryIdentity(items, normalizeCanonicalTitle, sha256Hex);
for (const item of items) {
  const assigned = assignments.get(item);
  item.titleHash = assigned.titleHash;
  item.corroborationCount = assigned.corroborationCount;
  const classified = classifier.classifyByKeyword(item.title);
  item.level = classified.level;
  item.category = classified.category;
  item.classSource = classified.source;
  item.confidence = classified.confidence;
}
const entitySignals = digestHelpers.computeEntityCorroborationSignals(items, input.nowMs);
for (const item of items) {
  const entity = entitySignals.get(item.titleHash) ?? { sourceCount: 0, tier12SourceCount: 0 };
  item.entityCorroborationCount = entity.sourceCount;
  item.level = digestHelpers.promoteDiplomacySeverity(item.level, item.title, entity.tier12SourceCount);
  item.isAlert = item.level === 'critical' || item.level === 'high';
  item.threat = { level: item.level, category: item.category, source: item.classSource };
  item.importanceScore = digestHelpers.computeImportanceScore(
    item.level,
    item.source,
    Math.max(item.corroborationCount, item.entityCorroborationCount),
    item.publishedAt,
    { title: item.title, classSource: item.classSource, entityCorroborationCount: item.entityCorroborationCount },
  );
}

const componentIndices = identity.clusterTexts(items.map((item) => item.title));
const components = componentIndices.map((indices) => {
  const members = indices.map((index) => items[index]);
  const trackable = members
    .map((item) => ({ item, canonical: normalizeCanonicalTitle(item.title) }))
    .filter((entry) => entry.canonical);
  trackable.sort((a, b) => a.item.publishedAt - b.item.publishedAt
    || (a.canonical < b.canonical ? -1 : a.canonical > b.canonical ? 1 : 0));
  const anchor = trackable[0] ?? null;
  const anchorItem = anchor?.item ?? members[0];
  const storyId = anchor
    ? sha256Sync(identity.normalizeStoryText(anchorItem.title))
    : assignments.get(anchorItem).titleHash;
  return {
    storyId,
    canonicalKey: assignments.get(anchorItem).titleHash,
    memberIds: members.map((item) => item.id).sort(),
    memberTitles: members.map((item) => item.title),
    sources: [...new Set(members.map((item) => item.source))].sort(),
    memberCount: members.length,
    sourceCount: new Set(members.map((item) => item.source)).size,
  };
});
const storyIdByTitle = new Map();
for (const component of components) {
  for (const title of component.memberTitles) storyIdByTitle.set(title, component.storyId);
}

// seed-insights.mjs drops titles with at most ten JavaScript UTF-16 code units
// before clustering. The acceptance input is already canonical OpenNews text.
const seedItems = items.filter((item) => item.title.length > 10);
const clusters = clustering.clusterItems(seedItems);
for (const cluster of clusters) cluster.storyId = storyIdByTitle.get(cluster.primaryTitle);
const stats = {};
const selected = clustering.selectTopStories(clusters, 8, stats);
const briefCluster = brief.pickBriefCluster(selected);
const l1Raw = JSON.stringify({
  lead: `${selected[0].primaryTitle} [1]. ${selected[1].primaryTitle} [2].`,
  lines: selected.map((story, index) => ({ n: index + 1, text: `${story.primaryTitle} [${index + 1}].` })),
});
const composed = brief.composeSynthesizedBrief(l1Raw, selected, {
  sanitizeTitle: (title) => title,
  sourceFromStory: (story) => story.primaryLink
    ? { title: story.primaryTitle, source: story.primarySource, url: story.primaryLink }
    : null,
  briefCluster,
});
const l2Raw = `${briefCluster.primaryTitle}.`;
const l2Validation = briefCore.validateNoHallucinatedProperNouns(l2Raw, briefCluster.primaryTitle);
const degradedStatus = resolveInsightsFallbackStatus('INSIGHTS_SYNTHESIS_PARSE', 'ok');

let lkg = null;
if (input.previousPublication) {
  const existing = {
    status: 'ok',
    topStories: input.previousPublication.top_stories,
    publication: input.previousPublication,
  };
  const candidate = { status: degradedStatus, topStories: selected };
  const decided = candidate.status === 'degraded' && existing.status === 'ok'
    ? seedHelpers.decorateInsightsRun(existing, {
        outcome: 'lkg_preserved',
        failureCode: 'INSIGHTS_SYNTHESIS_PARSE',
      })
    : seedHelpers.decorateInsightsRun(candidate, { outcome: 'degraded' });
  lkg = {
    servedPublication: seedHelpers.publishInsightsPayload(decided).publication ?? null,
    shouldPublish: seedHelpers.validateInsightsPayload(decided),
  };
}

process.stdout.write(JSON.stringify({
  components,
  firstStage: items.map((item) => ({
    id: item.id,
    titleHash: item.titleHash,
    corroborationCount: item.corroborationCount,
    entityCorroborationCount: item.entityCorroborationCount,
    level: item.level,
    category: item.category,
    classSource: item.classSource,
    confidence: item.confidence,
    importanceScore: item.importanceScore,
  })),
  clusters: clusters.map((cluster) => {
    const presentation = categorizeStory(cluster.primaryTitle);
    return {
      storyId: cluster.storyId,
      primaryTitle: cluster.primaryTitle,
      primarySource: cluster.primarySource,
      primaryLink: cluster.primaryLink ?? null,
      primaryPublishedAtMs: Number(cluster.pubDate),
      sourceCount: cluster.sourceCount,
      uniqueSourceCount: cluster.sources.length,
      sources: cluster.sources,
      lastUpdatedMs: Date.parse(cluster.lastUpdated),
      memberTitles: cluster.memberTitles,
      sourceTier: cluster.sourceTier,
      upstreamImportanceScore: cluster.upstreamImportanceScore,
      entityCorroboration: cluster.entityCorroboration === true,
      corroborationSourceCount: cluster.corroborationSourceCount ?? 0,
      importanceScore: clustering.scoreImportance(cluster),
      isAlert: cluster.isAlert === true,
      threatLevel: presentation.threatLevel,
      category: presentation.category,
      threat: cluster.threat ?? null,
    };
  }),
  selector: {
    stats,
    selected: selected.map((story) => ({
      storyId: story.storyId,
      primaryTitle: story.primaryTitle,
      primarySource: story.primarySource,
      importanceScore: story.importanceScore,
      effectiveImportanceScore: story.effectiveImportanceScore,
      entityCorroboration: story.entityCorroboration === true,
      uniqueSourceCount: story.sources.length,
    })),
    briefClusterStoryId: briefCluster?.storyId ?? null,
  },
  synthesis: {
    systemPrompt: brief.synthesisSystemPrompt(input.date),
    userPrompt: brief.synthesisUserPrompt(selected),
    l1Raw,
    composed,
    l2: {
      systemPrompt: brief.briefSystemPrompt(input.date),
      userPrompt: brief.briefUserPrompt(briefCluster.primaryTitle),
      text: l2Validation.ok ? l2Raw : briefCluster.primaryTitle,
      headlineFallback: !l2Validation.ok,
      status: degradedStatus,
    },
  },
  lkg,
}));
"""


def _canonical_items(events: tuple[Any, ...]) -> list[dict[str, Any]]:
    items = [
        {
            "id": event.provider_record_id,
            "title": str(event.entry.title),
            "source": _PINNED_SOURCE_NAMES.get(
                str(event.entry.reporting_origin).strip().lower(),
                str(event.entry.reporting_origin),
            ),
            "link": event.entry.link,
            "publishedAt": int(event.entry.published_at_ms),
            "pubDate": int(event.entry.published_at_ms),
        }
        for event in events
        if event.entry is not None
    ]
    return sorted(items, key=lambda item: (item["publishedAt"], item["id"]))


def _run_pinned_worldmonitor(
    items: list[dict[str, Any]],
    *,
    now_ms: int = NOW_MS,
    previous_publication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "nowMs": now_ms,
        "date": "2026-08-07",
        "items": items,
        "previousPublication": previous_publication,
    }
    base_payload = {**payload, "previousPublication": None}
    payload_sha256 = hashlib.sha256(
        json.dumps(base_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    golden = json.loads(_PINNED_GOLDEN_PATH.read_text(encoding="utf-8"))
    assert golden["head"] == PINNED_WORLDMONITOR_HEAD
    assert golden["driver_sha256"] == hashlib.sha256(_PINNED_DRIVER.encode()).hexdigest()
    expected = copy.deepcopy(golden["corpora"][payload_sha256])

    default_repo = Path(__file__).resolve().parents[3] / "worldmonitor"
    repo = Path(os.environ.get("TRACEFOLD_WORLDMONITOR_REPO", default_repo)).expanduser().resolve()
    if not repo.is_dir():
        if previous_publication is not None:
            assert golden["lkg_policy"] == {"degraded_with_healthy_existing": "serve_existing_whole_and_do_not_publish"}
            expected["lkg"] = {
                "servedPublication": previous_publication,
                "shouldPublish": False,
            }
        return expected
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    assert head == PINNED_WORLDMONITOR_HEAD, (
        f"WorldMonitor acceptance requires {PINNED_WORLDMONITOR_HEAD}, found {head} at {repo}"
    )
    completed = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "--eval", _PINNED_DRIVER],
        cwd=repo,
        input=json.dumps(payload),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = json.loads(completed.stdout)
    assert isinstance(result, dict)
    assert {**result, "lkg": None} == expected
    return result


def _assert_canonical_opennews(events: tuple[Any, ...]) -> None:
    by_id = {event.provider_record_id: event for event in events}
    for record_id, expected in CANONICAL_OPENNEWS_GOLDEN.items():
        entry = by_id[record_id].entry
        assert entry is not None
        assert {
            "title": entry.title,
            "description": entry.description,
            "reporting_origin": entry.reporting_origin,
            "canonical_url": entry.link,
        } == expected


def _assert_story_and_selector_parity(
    conn: Any,
    pinned: dict[str, Any],
    actual_clusters: list[dict[str, Any]],
) -> None:
    actual_components = [
        {
            "storyId": row["story_id"],
            "canonicalKey": row["canonical_key"],
            "memberIds": sorted(row["member_ids"]),
            "memberCount": row["member_count"],
            "sourceCount": row["source_count"],
        }
        for row in conn.execute(
            """
            SELECT story.story_id, story.canonical_key, story.item_count AS member_count,
                   story.source_count,
                   array_agg(item.provider_record_id ORDER BY item.provider_record_id) AS member_ids
              FROM news_stories story
              JOIN news_story_members member ON member.story_id = story.story_id
              JOIN news_items item ON item.item_id = member.item_id
             GROUP BY story.story_id, story.canonical_key, story.item_count, story.source_count
             ORDER BY story.story_id
            """
        ).fetchall()
    ]
    expected_components = sorted(
        (
            {key: component[key] for key in ("storyId", "canonicalKey", "memberIds", "memberCount", "sourceCount")}
            for component in pinned["components"]
        ),
        key=lambda component: component["storyId"],
    )
    assert actual_components == expected_components

    actual_items = {
        row["provider_record_id"]: dict(row)
        for row in conn.execute(
            """
            SELECT provider_record_id, level, category, classification_source,
                   classification_confidence, importance_score, importance_factors
              FROM news_items
             ORDER BY provider_record_id
            """
        ).fetchall()
    }
    for expected in pinned["firstStage"]:
        actual = actual_items[expected["id"]]
        assert actual["level"] == expected["level"]
        assert actual["category"] == expected["category"]
        assert actual["classification_source"] == expected["classSource"]
        assert actual["classification_confidence"] == pytest.approx(expected["confidence"])
        assert actual["importance_score"] == expected["importanceScore"]
        factors = actual["importance_factors"]
        assert factors["severity_level"] == expected["level"]
        assert factors["reporting_origin_count"] == expected["corroborationCount"]
        assert factors["scoring_corroboration_count"] == max(
            expected["corroborationCount"], expected["entityCorroborationCount"]
        )
        assert factors["entity_corroboration_boost"] == min(expected["entityCorroborationCount"], 5) * 4
        assert factors["total"] == expected["importanceScore"]

    expected_clusters = pinned["clusters"]
    assert len(actual_clusters) == len(expected_clusters)
    for actual, expected in zip(actual_clusters, expected_clusters, strict=True):
        assert {
            "storyId": actual["story_id"],
            "primaryTitle": actual["primary_title"],
            "primarySource": actual["primary_source"].strip().lower(),
            "primaryLink": actual["primary_link"],
            "primaryPublishedAtMs": actual["primary_published_at_ms"],
            "sourceCount": actual["source_count"],
            "uniqueSourceCount": actual["unique_source_count"],
            "sources": [source.strip().lower() for source in actual["sources"]],
            "lastUpdatedMs": actual["last_updated_ms"],
            "memberTitles": actual["member_titles"],
            "sourceTier": actual["source_tier"],
            "upstreamImportanceScore": actual["upstream_importance_score"],
            "entityCorroboration": actual["entity_corroboration"],
            "corroborationSourceCount": actual["corroboration_source_count"],
            "isAlert": actual["is_alert"],
            "threatLevel": actual["threat_level"],
            "category": actual["category"],
            "threat": actual["threat"],
        } == {
            **{key: expected[key] for key in expected if key not in {"importanceScore", "primarySource", "sources"}},
            "primarySource": expected["primarySource"].strip().lower(),
            "sources": [source.strip().lower() for source in expected["sources"]],
        }
        assert score_public_cluster(actual) == pytest.approx(expected["importanceScore"], abs=1e-12)

    selection = conn.execute("SELECT * FROM news_brief_selection_current WHERE singleton_key = true").fetchone()
    assert selection is not None
    expected_stats = pinned["selector"]["stats"]
    assert selection["selection_stats"] == {
        "considered": expected_stats["considered"],
        "admissibility_dropped": expected_stats["admissibilityDropped"],
        "source_cap_dropped": expected_stats["sourceCapDropped"],
        "overflow_dropped": expected_stats["overflowDropped"],
        "brief_eligible_considered": expected_stats["briefEligibleConsidered"],
        "brief_eligible_promoted": expected_stats["briefEligiblePromoted"],
    }
    assert expected_stats["briefEligiblePromoted"] is True
    actual_top = list(selection["top_stories"])
    expected_top = pinned["selector"]["selected"]
    assert [story["story_id"] for story in actual_top] == [story["storyId"] for story in expected_top]
    for actual, expected in zip(actual_top, expected_top, strict=True):
        cluster = next(
            cluster
            for cluster in expected_clusters
            if cluster["storyId"] == expected["storyId"] and cluster["primaryTitle"] == expected["primaryTitle"]
        )
        assert {
            "primary_title": actual["primary_title"],
            "primary_source": actual["primary_source"].strip().lower(),
            "primary_link": actual["primary_link"],
            "primary_published_at_ms": actual["primary_published_at_ms"],
            "source_count": actual["source_count"],
            "unique_source_count": actual["unique_source_count"],
            "sources": [source.strip().lower() for source in actual["sources"]],
            "last_updated_ms": actual["last_updated_ms"],
            "member_titles": actual["member_titles"],
            "source_tier": actual["source_tier"],
            "upstream_importance_score": actual["upstream_importance_score"],
            "entity_corroboration": actual["entity_corroboration"],
            "corroboration_source_count": actual["corroboration_source_count"],
            "is_alert": actual["is_alert"],
            "threat_level": actual["threat_level"],
            "category": actual["category"],
        } == {
            "primary_title": cluster["primaryTitle"],
            "primary_source": cluster["primarySource"].strip().lower(),
            "primary_link": cluster["primaryLink"],
            "primary_published_at_ms": cluster["primaryPublishedAtMs"],
            "source_count": cluster["sourceCount"],
            "unique_source_count": cluster["uniqueSourceCount"],
            "sources": [source.strip().lower() for source in cluster["sources"]],
            "last_updated_ms": cluster["lastUpdatedMs"],
            "member_titles": cluster["memberTitles"],
            "source_tier": cluster["sourceTier"],
            "upstream_importance_score": cluster["upstreamImportanceScore"],
            "entity_corroboration": cluster["entityCorroboration"],
            "corroboration_source_count": cluster["corroborationSourceCount"],
            "is_alert": cluster["isAlert"],
            "threat_level": cluster["threatLevel"],
            "category": cluster["category"],
        }
        assert actual["importance_score"] == pytest.approx(expected["importanceScore"], abs=1e-12)
        assert actual["effective_importance_score"] == pytest.approx(expected["effectiveImportanceScore"], abs=1e-12)
    assert pinned["selector"]["briefClusterStoryId"] in {story["story_id"] for story in actual_top}


def _compute_current_public_clusters(repository: NewsRepository, *, now_ms: int) -> list[dict[str, Any]]:
    payload = repository.load_story_projection(now_ms=now_ms)
    public_clusters = compute_news_public_clusters(
        NewsProjectionSnapshot(
            input_fingerprint=str(payload["input_fingerprint"]),
            scoring_epoch_ms=int(payload["scoring_epoch_ms"]),
            current_input_fingerprint=(
                str(payload["current_input_fingerprint"]) if payload.get("current_input_fingerprint") else None
            ),
            rows=tuple(dict(row) for row in payload["rows"]),
        )
    )
    return public_clusters


def test_scoreless_1019_market_strategy_reaches_brief_and_http_but_not_push(
    tmp_path: Path,
) -> None:
    prepare_postgres_database()
    title = "BTC open interest surges 3.4% in 3 minutes across major exchanges"
    frames: list[dict[str, Any]] = []
    for record_id, source, offset_minutes in (
        ("oi-1019-binance", "binance", 0),
        ("oi-1019-okx", "okx", 1),
    ):
        frame = _frame(
            record_id,
            title,
            "strategy",
            offset_minutes,
            link=None,
            author=source,
        )
        frame["params"].update(
            {
                "engineType": "market",
                "description": '{"open_interest_change":{"value":3.4,"unit":"%"}}',
                "coins": [{"symbol": "BTC", "market_type": "cex"}],
                "strategy": {
                    "id": "1019",
                    "name": "OI Event Monitor",
                    "sourceType": "market",
                },
            }
        )
        frames.append(frame)

    events = tuple(parse_opennews_message(frame, strategy_ids=OPENNEWS_STRATEGY_IDS) for frame in frames)
    assert all(event is not None and event.entry is not None for event in events)
    typed_events = tuple(event for event in events if event is not None)
    assert [event.provider_record_id for event in typed_events] == ["oi-1019-binance", "oi-1019-okx"]
    assert all(event.observation_kind == "report" for event in typed_events)
    assert all(event.entry.link is None for event in typed_events)
    assert [event.entry.reporting_origin for event in typed_events] == ["binance", "okx"]
    assert all("score" not in event.provider_metadata for event in typed_events)
    assert all(
        [strategy["id"] for strategy in event.provider_metadata["strategies"]] == ["1019"] for event in typed_events
    )

    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS - 2)
            assert repository.initialize_push_baseline(now_ms=NOW_MS - 1) == (NOW_MS - 1, True)
            written = repository.record_opennews_events(
                source=source,
                events=typed_events,
                observed_at_ms=NOW_MS,
            )
            projection = rebuild_news_projection(repository, now_ms=NOW_MS)

        assert written["items_inserted"] == 2
        assert projection["projection_status"] == "rebuilt"
        items = conn.execute(
            """
            SELECT provider_record_id, canonical_url, provider_metadata
              FROM news_items
             ORDER BY provider_record_id
            """
        ).fetchall()
        assert [item["provider_record_id"] for item in items] == ["oi-1019-binance", "oi-1019-okx"]
        assert all(item["canonical_url"] is None for item in items)
        assert all("score" not in item["provider_metadata"] for item in items)
        assert all(item["provider_metadata"]["coins"] == [{"symbol": "BTC", "market_type": "cex"}] for item in items)
        assert all(
            [strategy["id"] for strategy in item["provider_metadata"]["strategies"]] == ["1019"] for item in items
        )
        story = conn.execute(
            """
            SELECT story_id, item_count, source_count
              FROM news_stories
            """
        ).fetchone()
        assert story is not None
        assert story["item_count"] == 2
        assert story["source_count"] == 2

        with conn.transaction():
            candidate = repository.peek_brief_candidate(now_ms=NOW_MS)
            assert candidate is not None
            prepared = repository.prepare_brief_run(
                slot_at_ms=int(candidate["slot_at_ms"]),
                lease_owner="oi-1019-acceptance",
                lease_token="healthy",
                now_ms=NOW_MS,
            )
        assert prepared is not None and not prepared["completed_without_model"]
        stories = tuple(NewsBriefStory.model_validate(value) for value in prepared["top_stories"])
        assert len(stories) == 1
        assert stories[0].primary_title == title
        assert stories[0].unique_source_count == 2

        raw_brief = json.dumps(
            {
                "lead": f"{title} [1].",
                "lines": [{"n": 1, "text": f"{title} [1]."}],
            }
        )
        requests: list[dict[str, Any]] = []

        def healthy_handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"model": "fixture-model", "choices": [{"message": {"content": raw_brief}}]},
            )

        publisher = ProviderChainNewsBriefPublisher(
            ollama_base_url="https://ollama.test/v1",
            groq_api_key=None,
            transport=httpx.MockTransport(healthy_handler),
        )
        try:
            result = publisher.publish(stories, date_iso="2026-08-07")
        finally:
            publisher.close()
        assert len(requests) == 1
        assert result.brief_kind == "l1"
        assert result.quality == "ok"

        with conn.transaction():
            assert repository.start_brief_model(
                slot_at_ms=int(prepared["claim"]["slot_at_ms"]),
                lease_owner="oi-1019-acceptance",
                lease_token="healthy",
                now_ms=NOW_MS + 1,
            )
            publication_id = repository.publish_brief(
                claim=prepared["claim"],
                result=result,
                now_ms=NOW_MS + 2,
            )
            push_candidates, next_cursor = repository.story_push_reconcile_page()
        assert publication_id is not None
        assert push_candidates == {}
        assert next_cursor is None
        assert conn.execute("SELECT count(*) AS count FROM news_push_deliveries").fetchone()["count"] == 0

        settings = Settings(
            ws_token="secret",
            news=NewsSettings(
                opennews_strategy_ids=("1018", "1019"),
                push=NewsPushSettings(
                    enabled=True,
                    feishu_webhook_url=("https://open.feishu.cn/open-apis/bot/v2/hook/oi-1019-acceptance"),
                ),
            ),
            storage=postgres_settings_storage(),
        )
        settings.set_config_dir(tmp_path / "app-home")
        app = create_app(settings=settings)
        headers = {"Authorization": "Bearer secret"}
        with TestClient(app) as client:
            unauthorized = client.get("/api/news/brief")
            feed = client.get("/api/news/feed", headers=headers)
            priority_feed = client.get(
                "/api/news/feed",
                params={"provider_score_gt": 70},
                headers=headers,
            )
            detail = client.get(
                f"/api/news/stories/{story['story_id']}",
                headers=headers,
            )
            brief = client.get("/api/news/brief", headers=headers)

        assert unauthorized.status_code == 401
        assert feed.status_code == 200
        assert priority_feed.status_code == 200
        assert detail.status_code == 200
        assert brief.status_code == 200
        feed_stories = feed.json()["data"]["stories"]
        assert len(feed_stories) == 1
        assert feed_stories[0]["title"] == title
        assert feed_stories[0]["notification"] == {
            "eligible": False,
            "ineligible_reason": "score_threshold",
            "delivery_state": "not_created",
        }
        assert priority_feed.json()["data"]["stories"] == []
        detail_data = detail.json()["data"]
        assert detail_data["story_id"] == story["story_id"]
        assert len(detail_data["members"]) == 2
        assert {member["provider_record_id"] for member in detail_data["members"]} == {
            "oi-1019-binance",
            "oi-1019-okx",
        }
        brief_data = brief.json()["data"]
        assert brief_data["state"] == "current"
        assert brief_data["publication"]["publication_id"] == publication_id
        assert brief_data["publication"]["brief_kind"] == "l1"
        assert brief_data["publication"]["quality"] == "ok"
        assert brief_data["publication"]["top_stories"][0]["primary_title"] == title
    finally:
        conn.close()


def test_frozen_opennews_pinned_worldmonitor_provider_to_http_and_whole_lkg(tmp_path: Path) -> None:
    prepare_postgres_database()
    events = tuple(parse_opennews_message(frame, strategy_ids=OPENNEWS_STRATEGY_IDS) for frame in OPENNEWS_FRAMES)
    assert all(event is not None and event.entry is not None for event in events)
    typed_events = tuple(event for event in events if event is not None)
    _assert_canonical_opennews(typed_events)
    canonical_items = _canonical_items(typed_events)
    pinned = _run_pinned_worldmonitor(canonical_items)

    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
        repository = NewsRepository(conn)
        source = opennews_source()
        with conn.transaction():
            repository.sync_sources((source,), now_ms=NOW_MS)
            inserted = repository.record_opennews_events(source=source, events=typed_events, observed_at_ms=NOW_MS)
            replay = repository.record_opennews_events(source=source, events=typed_events, observed_at_ms=NOW_MS)
            projection = rebuild_news_projection(repository, now_ms=NOW_MS)
        assert inserted["items_inserted"] == len(typed_events)
        assert replay["items_inserted"] == 0
        assert replay["items_updated"] == 0
        assert projection["projection_status"] == "rebuilt"
        _assert_story_and_selector_parity(
            conn,
            pinned,
            _compute_current_public_clusters(repository, now_ms=NOW_MS),
        )

        with conn.transaction():
            candidate = repository.peek_brief_candidate(now_ms=NOW_MS)
            assert candidate is not None
            prepared = repository.prepare_brief_run(
                slot_at_ms=int(candidate["slot_at_ms"]),
                lease_owner="pinned-acceptance",
                lease_token="healthy",
                now_ms=NOW_MS,
            )
        assert prepared is not None and not prepared["completed_without_model"]
        stories = tuple(NewsBriefStory.model_validate(story) for story in prepared["top_stories"])
        requests: list[dict[str, Any]] = []

        def healthy_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(
                200,
                json={"model": "llama3.1:8b", "choices": [{"message": {"content": pinned["synthesis"]["l1Raw"]}}]},
            )

        publisher = ProviderChainNewsBriefPublisher(
            ollama_base_url="https://ollama.test/v1",
            groq_api_key=None,
            transport=httpx.MockTransport(healthy_handler),
        )
        try:
            healthy_result = publisher.publish(stories, date_iso="2026-08-07")
        finally:
            publisher.close()
        assert len(requests) == 1
        assert requests[0]["messages"] == [
            {"role": "system", "content": pinned["synthesis"]["systemPrompt"]},
            {"role": "user", "content": pinned["synthesis"]["userPrompt"]},
        ]
        assert requests[0]["max_tokens"] == 900
        assert healthy_result.world_brief == pinned["synthesis"]["composed"]["lead"]
        assert [line.model_dump() for line in healthy_result.brief_story_lines] == pinned["synthesis"]["composed"][
            "lines"
        ]
        assert [
            {key: value for key, value in source.model_dump().items() if key in {"title", "source", "url"}}
            for source in healthy_result.sources
        ] == pinned["synthesis"]["composed"]["sources"]

        with conn.transaction():
            assert repository.start_brief_model(
                slot_at_ms=int(prepared["claim"]["slot_at_ms"]),
                lease_owner="pinned-acceptance",
                lease_token="healthy",
                now_ms=NOW_MS + 1,
            )
            publication_id = repository.publish_brief(
                claim=prepared["claim"],
                result=healthy_result,
                now_ms=NOW_MS + 2,
            )
        assert publication_id is not None

        settings = Settings(
            ws_token="secret",
            news=NewsSettings(opennews_strategy_ids=("1018", "1019")),
            storage=postgres_settings_storage(),
        )
        settings.set_config_dir(tmp_path / "app-home")
        app = create_app(settings=settings)
        headers = {"Authorization": "Bearer secret"}
        with TestClient(app) as client:
            response = client.get("/api/news/brief", headers=headers)
            unchanged = client.get(
                "/api/news/brief",
                headers={**headers, "If-None-Match": response.headers["etag"]},
            )
        assert response.status_code == 200
        assert unchanged.status_code == 304
        healthy_http = response.json()["data"]
        assert healthy_http["state"] == "current"
        sealed_healthy_publication = healthy_http["publication"]

        next_ms = NOW_MS + 3_600_000
        changed = parse_opennews_message(
            NEXT_TURN_FRAME,
            strategy_ids=OPENNEWS_STRATEGY_IDS,
        )
        assert changed is not None
        changed_items = [*canonical_items, *_canonical_items((changed,))]
        degraded_pinned = _run_pinned_worldmonitor(changed_items, now_ms=next_ms)
        with conn.transaction():
            repository.record_opennews_events(source=source, events=(changed,), observed_at_ms=next_ms)
            rebuild_news_projection(repository, now_ms=next_ms)
        _assert_story_and_selector_parity(
            conn,
            degraded_pinned,
            _compute_current_public_clusters(repository, now_ms=next_ms),
        )
        with conn.transaction():
            degraded_candidate = repository.peek_brief_candidate(now_ms=next_ms)
            assert degraded_candidate is not None
            degraded_prepared = repository.prepare_brief_run(
                slot_at_ms=int(degraded_candidate["slot_at_ms"]),
                lease_owner="pinned-acceptance",
                lease_token="degraded",
                now_ms=next_ms,
            )
        assert degraded_prepared is not None and not degraded_prepared["completed_without_model"]
        degraded_stories = tuple(NewsBriefStory.model_validate(story) for story in degraded_prepared["top_stories"])
        degraded_requests: list[dict[str, Any]] = []

        def degraded_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            degraded_requests.append(body)
            content = (
                '{"lead":"bad","lines":[]}' if body["max_tokens"] == 900 else degraded_pinned["synthesis"]["l2"]["text"]
            )
            return httpx.Response(
                200,
                json={"model": "llama3.1:8b", "choices": [{"message": {"content": content}}]},
            )

        publisher = ProviderChainNewsBriefPublisher(
            ollama_base_url="https://ollama.test/v1",
            groq_api_key=None,
            transport=httpx.MockTransport(degraded_handler),
        )
        try:
            degraded_result = publisher.publish(degraded_stories, date_iso="2026-08-07")
        finally:
            publisher.close()
        assert [request["max_tokens"] for request in degraded_requests] == [900, 300]
        assert degraded_requests[1]["messages"] == [
            {"role": "system", "content": degraded_pinned["synthesis"]["l2"]["systemPrompt"]},
            {"role": "user", "content": degraded_pinned["synthesis"]["l2"]["userPrompt"]},
        ]
        assert degraded_result.brief_kind == "l2"
        assert degraded_result.quality == "degraded"
        assert degraded_result.world_brief == degraded_pinned["synthesis"]["l2"]["text"]
        assert degraded_result.brief_story_lines == ()

        with conn.transaction():
            assert repository.start_brief_model(
                slot_at_ms=int(degraded_prepared["claim"]["slot_at_ms"]),
                lease_owner="pinned-acceptance",
                lease_token="degraded",
                now_ms=next_ms + 1,
            )
            served_id = repository.publish_brief(
                claim=degraded_prepared["claim"],
                result=degraded_result,
                now_ms=next_ms + 2,
            )
        assert served_id == publication_id

        pinned_lkg = _run_pinned_worldmonitor(
            changed_items,
            now_ms=next_ms,
            previous_publication=sealed_healthy_publication,
        )["lkg"]
        assert pinned_lkg == {"servedPublication": sealed_healthy_publication, "shouldPublish": False}
        with TestClient(app) as client:
            preserved_response = client.get("/api/news/brief", headers=headers)
        preserved = preserved_response.json()["data"]
        assert preserved["state"] == "last_known_good"
        assert preserved["publication"] == pinned_lkg["servedPublication"]
        assert preserved["latest_run"]["model_outcome"] == "l2"
        assert preserved["latest_run"]["pointer_action"] == "preserve_lkg"
    finally:
        conn.close()
