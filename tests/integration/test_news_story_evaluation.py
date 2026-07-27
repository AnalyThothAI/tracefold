from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.news import (
    NewsFeedEntry,
    NewsInterface,
    NewsRepository,
    NewsSourceDefinition,
    clustering_metrics,
)

NOW_MS = 1_779_000_000_000
CASE_GAP_MS = 31 * 24 * 60 * 60 * 1_000
CORPUS_PATH = Path(__file__).parents[1] / "fixtures" / "news_story_identity_golden.json"
WORLDMONITOR_CORPUS_PATH = Path(__file__).parents[1] / "fixtures" / "worldmonitor_story_identity_pairs.json"


def test_labeled_identity_corpus_replays_through_postgres_runtime_seam(tmp_path) -> None:
    corpus = json.loads(CORPUS_PATH.read_text())
    evaluated: list[dict[str, Any]] = []
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        for case_index, case in enumerate(corpus["cases"]):
            if case_index:
                _clear_identity_case(conn)
            repository = NewsRepository(conn)
            case_now_ms = NOW_MS + case_index * CASE_GAP_MS
            definitions = tuple(
                source_definition(source_id)
                for source_id in dict.fromkeys(report["source_id"] for report in case["reports"])
            )
            repository.sync_sources(definitions, now_ms=case_now_ms)
            expected_seen: dict[str, str] = {}
            for report_index, report in enumerate(case["reports"]):
                observed_at_ms = case_now_ms + report_index * 1_000
                source = next(item for item in definitions if item.source_id == report["source_id"])
                repository.record_fetch_success(
                    source=source,
                    entries=(
                        NewsFeedEntry(
                            guid=f"{case['case_id']}-{report_index}",
                            link=report["url"],
                            title=report["title"],
                            summary=report.get("summary", ""),
                            published_at_ms=observed_at_ms - 60_000,
                            language=report["language"],
                        ),
                    ),
                    started_at_ms=observed_at_ms,
                    finished_at_ms=observed_at_ms,
                    status_code=200,
                    etag=None,
                    last_modified=None,
                    not_modified=False,
                )
            repository.project_pending_revisions(
                now_ms=case_now_ms + 9_000,
                limit=100,
            )
            for report_index, report in enumerate(case["reports"]):
                row = conn.execute(
                    """
                    SELECT
                      revisions.revision_id,
                      memberships.story_id,
                      decisions.candidates
                    FROM news_articles AS articles
                    JOIN news_article_revisions AS revisions
                      ON revisions.article_id = articles.article_id
                    JOIN news_story_memberships AS memberships
                      ON memberships.article_id = articles.article_id
                     AND memberships.membership_kind = 'primary'
                    JOIN news_story_identity_decisions AS decisions
                      ON decisions.revision_id = revisions.revision_id
                    WHERE articles.canonical_url = %s
                    """,
                    (report["url"],),
                ).fetchone()
                assert row is not None
                expected_cluster = str(report["expected_cluster"])
                prior_story_id = expected_seen.get(expected_cluster)
                candidates = list(row["candidates"])
                evaluated.append(
                    {
                        "fact_id": f"{case['case_id']}:{report_index}",
                        "expected_cluster": expected_cluster,
                        "predicted_story_id": str(row["story_id"]),
                        "has_prior_true_member": prior_story_id is not None,
                        "true_story_recalled": (
                            prior_story_id is None
                            or any(str(candidate.get("story_id")) == prior_story_id for candidate in candidates)
                        ),
                        "hard_negative": bool(case["hard_negative"]),
                    }
                )
                expected_seen.setdefault(expected_cluster, str(row["story_id"]))
            expected_origin_count = case.get("expected_independent_origin_count")
            if expected_origin_count is not None:
                stories = NewsInterface(repository).list_stories(limit=20)["items"]
                matching = [story for story in stories if story["story_id"] in set(expected_seen.values())]
                assert len(matching) == 1
                assert matching[0]["independent_origin_count"] == expected_origin_count
            conn.commit()
    finally:
        conn.close()

    metrics = clustering_metrics(evaluated)
    expected_metrics = {
        "candidate_recall": 1.0,
        "pairwise_precision": 1.0,
        "pairwise_recall": 1.0,
        "false_merge_count": 0,
        "false_split_count": 0,
        "bcubed_precision": 1.0,
        "bcubed_recall": 1.0,
        "cluster_purity": 1.0,
        "hard_negative_false_merge_count": 0,
        "singleton_cluster_count": 32,
        "cluster_size_distribution": {"1": 32, "2": 19},
    }
    assert metrics == expected_metrics, {
        "metrics": metrics,
        "mismatches": _identity_mismatches(evaluated),
    }


def source_definition(source_id: str) -> NewsSourceDefinition:
    role = "trusted_aggregator" if source_id == "aggregator" else "original_publisher"
    return NewsSourceDefinition(
        source_id=source_id,
        name=source_id.title(),
        feed_url=f"https://{source_id}.example/feed.xml",
        source_domain=f"{source_id}.example",
        source_role=role,
        trust_tier="trusted",
        source_chain_id=source_id,
        publisher_organization_id=source_id,
        default_language="en",
    )


def _identity_mismatches(
    evaluated: list[dict[str, Any]],
) -> dict[str, object]:
    expected_to_predicted: dict[str, set[str]] = defaultdict(set)
    predicted_to_expected: dict[str, set[str]] = defaultdict(set)
    for row in evaluated:
        expected_to_predicted[str(row["expected_cluster"])].add(str(row["predicted_story_id"]))
        predicted_to_expected[str(row["predicted_story_id"])].add(str(row["expected_cluster"]))
    false_splits = sorted(expected for expected, predicted in expected_to_predicted.items() if len(predicted) > 1)
    false_merges = sorted(sorted(expected) for expected in predicted_to_expected.values() if len(expected) > 1)
    missed_candidates = sorted(
        str(row["fact_id"])
        for row in evaluated
        if bool(row["has_prior_true_member"]) and not bool(row["true_story_recalled"])
    )
    return {
        "false_splits": false_splits,
        "false_merges": false_merges,
        "missed_candidates": missed_candidates,
    }


def test_worldmonitor_labeled_title_pairs_pass_tracefold_postgres_identity_runtime(
    tmp_path,
) -> None:
    source_corpus = json.loads(WORLDMONITOR_CORPUS_PATH.read_text())
    cases: list[dict[str, Any]] = []
    for index, pair in enumerate(source_corpus["positive_pairs"]):
        cases.append(
            {
                "case_id": f"worldmonitor-positive-{index}",
                "expected_same": True,
                "titles": pair,
            }
        )
    for index, pair in enumerate(source_corpus["negative_pairs"]):
        cases.append(
            {
                "case_id": f"worldmonitor-negative-{index}",
                "expected_same": False,
                "titles": pair,
            }
        )

    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        repository = NewsRepository(conn)
        sources = (
            source_definition("worldmonitor-a"),
            source_definition("worldmonitor-b"),
        )
        for case_index, case in enumerate(cases):
            if case_index:
                _clear_identity_case(conn)
            case_now_ms = NOW_MS + case_index * CASE_GAP_MS
            repository.sync_sources(sources, now_ms=case_now_ms)
            for report_index, title in enumerate(case["titles"]):
                observed_at_ms = case_now_ms + report_index * 1_000
                repository.record_fetch_success(
                    source=sources[report_index],
                    entries=(
                        NewsFeedEntry(
                            guid=f"{case['case_id']}-{report_index}",
                            link=(f"https://worldmonitor-{report_index}.example/{case['case_id']}"),
                            title=title,
                            published_at_ms=observed_at_ms - 60_000,
                            language="en",
                        ),
                    ),
                    started_at_ms=observed_at_ms,
                    finished_at_ms=observed_at_ms,
                    status_code=200,
                    etag=None,
                    last_modified=None,
                    not_modified=False,
                )
            repository.project_pending_revisions(
                now_ms=case_now_ms + 9_000,
                limit=100,
            )
            stories = [
                story
                for story in NewsInterface(repository).list_stories(limit=20)["items"]
                if int(story["first_seen_at_ms"]) >= case_now_ms
            ]
            expected_count = 1 if case["expected_same"] else 2
            assert len(stories) == expected_count, case
            conn.commit()
    finally:
        conn.close()


def _clear_identity_case(conn: Any) -> None:
    conn.execute(
        """
        TRUNCATE TABLE
          news_story_projection_checkpoints,
          news_sources
        CASCADE
        """
    )
    conn.commit()
