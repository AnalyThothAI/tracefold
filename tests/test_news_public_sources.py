from __future__ import annotations

import hashlib
import json
import os
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

from tracefold.news.sources import (
    WORLDMONITOR_PUBLIC_SOURCE_CATALOG_SHA256,
    WORLDMONITOR_PUBLIC_SOURCE_COMMIT,
    public_rss_membership_sources,
    public_rss_sources,
    reporting_origin_tier,
)

_CATALOG_NODE_DRIVER = r"""
import {
  INTEL_SOURCES,
  VARIANT_FEEDS,
  isServerFeedReachableForLanguage,
} from './server/worldmonitor/news/v1/_feeds.ts';

const memberships = [];
for (const [category, feeds] of Object.entries(VARIANT_FEEDS.full)) {
  for (const feed of feeds) {
    if (isServerFeedReachableForLanguage(feed, 'en')) memberships.push({ category, ...feed });
  }
}
for (const feed of INTEL_SOURCES) {
  if (isServerFeedReachableForLanguage(feed, 'en')) memberships.push({ category: 'intel', ...feed });
}

const physical = new Map();
for (const feed of memberships) {
  const key = JSON.stringify([feed.name, feed.url]);
  let row = physical.get(key);
  if (!row) {
    row = {
      name: feed.name,
      url: feed.url,
      lang: feed.lang || 'en',
      strategic_default: Boolean(feed.strategicDefault),
      memberships: [],
    };
    physical.set(key, row);
  }
  row.memberships.push(feed.category);
}

process.stdout.write(JSON.stringify({
  authority_commit: '0e8785c43e6a693990a14181ae0a16066c15fc8c',
  variant: 'full',
  language: 'en',
  counts: {
    physical_feeds: physical.size,
    category_memberships: memberships.length,
    reporting_source_names: new Set(memberships.map((row) => row.name)).size,
    categories: new Set(memberships.map((row) => row.category)).size,
  },
  sources: [...physical.values()],
}));
"""

_MEMBERSHIP_NODE_DRIVER = r"""
import {
  INTEL_SOURCES,
  VARIANT_FEEDS,
  isServerFeedReachableForLanguage,
} from './server/worldmonitor/news/v1/_feeds.ts';

const memberships = [];
for (const [category, feeds] of Object.entries(VARIANT_FEEDS.full)) {
  for (const feed of feeds) {
    if (isServerFeedReachableForLanguage(feed, 'en')) {
      memberships.push([category, feed.name, feed.url]);
    }
  }
}
for (const feed of INTEL_SOURCES) {
  if (isServerFeedReachableForLanguage(feed, 'en')) {
    memberships.push(['intel', feed.name, feed.url]);
  }
}
process.stdout.write(JSON.stringify(memberships));
"""


def test_public_source_tiers_are_the_exact_pinned_worldmonitor_registry() -> None:
    encoded = files("tracefold.news").joinpath("source_tiers.json").read_bytes()
    tiers = json.loads(encoded)

    assert len(tiers) == 343
    assert hashlib.sha256(encoded).hexdigest() == "c7c295f0e4edb55c21c91bf8c7b28847138d2e175805156c697fc7896e94bb2e"
    assert reporting_origin_tier(" Reuters ", fallback_tier=4) == 1
    assert reporting_origin_tier("CBC News", fallback_tier=4) == 1
    assert reporting_origin_tier("Arctic Today", fallback_tier=4) == 2
    assert reporting_origin_tier("unknown outlet", fallback_tier=3) == 3


def test_public_rss_catalog_is_the_frozen_full_english_plus_intel_population() -> None:
    encoded = files("tracefold.news").joinpath("worldmonitor_public_sources.json").read_bytes()
    catalog = json.loads(encoded)
    sources = public_rss_sources()

    assert hashlib.sha256(encoded).hexdigest() == WORLDMONITOR_PUBLIC_SOURCE_CATALOG_SHA256
    assert catalog["authority_commit"] == WORLDMONITOR_PUBLIC_SOURCE_COMMIT
    assert catalog["variant"] == "full"
    assert catalog["language"] == "en"
    assert catalog["counts"] == {
        "physical_feeds": 179,
        "category_memberships": 183,
        "reporting_source_names": 178,
        "categories": 17,
    }
    assert len(sources) == 179
    assert sum(len(source.memberships) for source in sources) == 183
    assert len({source.name for source in sources}) == 178
    assert len({category for source in sources for category in source.memberships}) == 17
    assert all(source.source_kind == "rss" for source in sources)
    assert all(source.feed_url and source.feed_url.startswith("https://") for source in sources)
    assert len({(source.name, source.feed_url) for source in sources}) == 179
    assert len({source.source_id for source in sources}) == 179
    assert all(
        source.source_id == f"news-rss-{hashlib.sha256(str(source.feed_url).encode()).hexdigest()[:24]}"
        for source in sources
    )


def test_public_rss_catalog_matches_the_pinned_worldmonitor_module() -> None:
    default_repo = Path(__file__).resolve().parents[2] / "worldmonitor"
    repo = Path(os.environ.get("TRACEFOLD_WORLDMONITOR_REPO", default_repo)).expanduser().resolve()
    if not repo.is_dir():
        pytest.skip("pinned WorldMonitor sibling is not available; frozen catalog test remains authoritative")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    assert head == WORLDMONITOR_PUBLIC_SOURCE_COMMIT
    completed = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "--eval", _CATALOG_NODE_DRIVER],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    actual = json.loads(completed.stdout)
    frozen = json.loads(files("tracefold.news").joinpath("worldmonitor_public_sources.json").read_text())
    assert actual == frozen

    membership_run = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "--eval", _MEMBERSHIP_NODE_DRIVER],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    expected_memberships = json.loads(membership_run.stdout)
    actual_memberships = [
        [category, source.name, source.feed_url] for category, source in public_rss_membership_sources()
    ]
    assert actual_memberships == expected_memberships
