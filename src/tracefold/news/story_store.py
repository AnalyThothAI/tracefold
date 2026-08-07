from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from psycopg.types.json import Jsonb

from .brief import selection_fingerprint
from .models import CLASSIFIER_VERSION, IMPORTANCE_VERSION, STORY_IDENTITY_VERSION

# OpenNews is a high-volume aggregate, unlike WorldMonitor's per-feed input
# (which is capped before clustering).  Keep the live Story closure within a
# fixed, complete window that the 25-second CPU budget can actually sustain;
# the acquisition repository still retains valid Article facts for 96 hours.
ACTIVE_WINDOW_MS = 12 * 60 * 60 * 1000
SCORING_EPOCH_MS = 60 * 60 * 1000
NEWS_STORY_INPUT_ROW_CAP = 10_000
NEWS_STORY_INPUT_BYTES_CAP = 8 * 1024 * 1024
STORY_PROJECTION_VERSION = f"{STORY_IDENTITY_VERSION}:{CLASSIFIER_VERSION}:{IMPORTANCE_VERSION}:12h-full-window-v3"
_PIPELINE_LOCK_KEY = 727_301_984


class NewsProjectionInputExceeded(RuntimeError):
    pass


def _require_bounded_story_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    if len(rows) > NEWS_STORY_INPUT_ROW_CAP:
        raise NewsProjectionInputExceeded("news_story_input_row_cap")
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode()
    if len(encoded) > NEWS_STORY_INPUT_BYTES_CAP:
        raise NewsProjectionInputExceeded("news_story_input_byte_cap")


def load_story_projection(repository: Any, *, now_ms: int) -> dict[str, Any]:
    cutoff_ms = int(now_ms) - ACTIVE_WINDOW_MS
    scoring_epoch_ms = int(now_ms) - (int(now_ms) % SCORING_EPOCH_MS)
    # This is the publish CAS baseline. Read it before the moving fact window;
    # the reverse order can pair old facts with a newer published fingerprint.
    summary = repository.conn.execute(
        "SELECT input_fingerprint FROM news_projection_summary WHERE singleton_key='current'"
    ).fetchone()
    bounds = repository.conn.execute(
        """
        SELECT count(*) AS item_count,
               coalesce(sum(
                 octet_length(item.item_id)
                 + octet_length(item.source_id)
                 + coalesce(octet_length(item.canonical_url), 0)
                 + octet_length(item.reporting_origin)
                 + octet_length(item.title)
                 + octet_length(item.description)
                 + octet_length(item.content_fingerprint)
                 + 12
               ), 0) AS minimum_input_bytes
          FROM news_items item
          JOIN news_sources source ON source.source_id = item.source_id
         WHERE source.enabled
           AND item.published_at_ms >= %s
        """,
        (cutoff_ms,),
    ).fetchone()
    item_count = int(bounds["item_count"] or 0)
    if item_count > NEWS_STORY_INPUT_ROW_CAP:
        raise NewsProjectionInputExceeded("news_story_input_row_cap")
    if int(bounds["minimum_input_bytes"] or 0) > NEWS_STORY_INPUT_BYTES_CAP:
        raise NewsProjectionInputExceeded("news_story_input_byte_cap")
    loaded_rows = [
        dict(row)
        for row in repository.conn.execute(
            """
            SELECT item.item_id, item.source_id, item.canonical_url,
                   item.reporting_origin, item.title, item.description,
                   item.published_at_ms, item.content_fingerprint,
                   source.tier
              FROM news_items item
              JOIN news_sources source ON source.source_id = item.source_id
             WHERE source.enabled
               AND item.published_at_ms >= %s
             ORDER BY item.published_at_ms, item.item_id
             LIMIT %s
            """,
            (cutoff_ms, NEWS_STORY_INPUT_ROW_CAP + 1),
        ).fetchall()
    ]
    rows = [
        {
            key: row[key]
            for key in (
                "item_id",
                "source_id",
                "canonical_url",
                "reporting_origin",
                "title",
                "description",
                "published_at_ms",
                "tier",
            )
        }
        for row in loaded_rows
    ]
    _require_bounded_story_rows(rows)
    input_fingerprint = repository.stable_json_hash(
        {
            "projection_version": STORY_PROJECTION_VERSION,
            "scoring_epoch_ms": scoring_epoch_ms,
            "items": [
                [
                    str(row["item_id"]),
                    str(row["content_fingerprint"]),
                    int(row["published_at_ms"]),
                    str(row["reporting_origin"]),
                    int(row["tier"]),
                ]
                for row in loaded_rows
            ],
        }
    )
    return {
        "input_fingerprint": input_fingerprint,
        "cutoff_ms": cutoff_ms,
        "scoring_epoch_ms": scoring_epoch_ms,
        "current_input_fingerprint": (summary["input_fingerprint"] if summary is not None else None),
        "rows": rows,
    }


def publish_story_projection(
    repository: Any,
    *,
    snapshot: Any,
    projection: Mapping[str, Any],
    now_ms: int,
) -> dict[str, Any]:
    _require_bounded_story_rows(snapshot.rows)
    conn = repository.conn
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PIPELINE_LOCK_KEY,))
    snapshot_item_ids = sorted({str(row["item_id"]) for row in snapshot.rows})
    summary = conn.execute(
        """
        SELECT input_fingerprint FROM news_projection_summary
         WHERE singleton_key='current' FOR UPDATE
        """
    ).fetchone()
    published_fingerprint = summary["input_fingerprint"] if summary is not None else None
    if published_fingerprint == snapshot.input_fingerprint:
        return {
            "projection_status": "unchanged_input",
            "items": len(snapshot.rows),
            "stories": len(projection.get("stories", [])),
            "rows_written": 0,
        }
    if published_fingerprint != snapshot.current_input_fingerprint:
        return {
            "projection_status": "superseded_snapshot",
            "items": len(snapshot.rows),
            "stories": 0,
            "rows_written": 0,
        }

    stories = [dict(row) for row in projection.get("stories", [])]
    memberships = [dict(row) for row in projection.get("memberships", [])]
    item_updates = [dict(row) for row in projection.get("item_updates", [])]
    item_writes = _publish_items(
        conn,
        cutoff_ms=int(snapshot.cutoff_ms),
        item_updates=item_updates,
        now_ms=now_ms,
    )
    story_writes = _upsert_stories(conn, stories=stories, now_ms=now_ms)
    membership_writes = _replace_memberships(conn, memberships=memberships)
    story_writes += _delete_absent_stories(conn, stories=stories)
    invariants = repository._story_invariant_counts(
        item_ids=snapshot_item_ids,
    )
    if invariants["total"]:
        raise RuntimeError(
            "news_story_invariant_failed:" + json.dumps(invariants, sort_keys=True, separators=(",", ":"))
        )
    bounded_writes = _replace_facets(
        conn,
        stories=stories,
        memberships=memberships,
        now_ms=now_ms,
    )
    bounded_writes += _replace_brief_selection(
        conn,
        selection=projection["selection_snapshot"],
        now_ms=now_ms,
    )
    material_writes = item_writes + story_writes + membership_writes + bounded_writes
    newest_item = max((int(row["published_at_ms"]) for row in snapshot.rows), default=None)
    newest_story = max(
        (int(row["last_published_at_ms"]) for row in stories),
        default=None,
    )
    summary_writes = int(
        conn.execute(
            """
            UPDATE news_projection_summary
               SET active_item_count=%s, active_story_count=%s,
                   unmaterialized_item_count=0, invalid_owner_count=0,
                   invalid_story_aggregate_count=0,
                   newest_item_at_ms=%s, newest_story_at_ms=%s,
                   last_material_change_at_ms=(
                     CASE WHEN %s THEN %s ELSE last_material_change_at_ms END
                   ), input_fingerprint=%s,
                   projection_version=%s, last_attempt_at_ms=%s,
                   last_success_at_ms=%s,
                   last_error=NULL, updated_at_ms=%s
             WHERE singleton_key='current'
            """,
            (
                len(snapshot.rows),
                len(stories),
                newest_item,
                newest_story,
                material_writes > 0,
                now_ms,
                snapshot.input_fingerprint,
                STORY_PROJECTION_VERSION,
                now_ms,
                now_ms,
                now_ms,
            ),
        ).rowcount
        or 0
    )
    return {
        "projection_status": "rebuilt",
        "items": len(snapshot.rows),
        "temporary_clusters": int(projection.get("temporary_clusters", 0)),
        "stories": len(stories),
        "story_writes": story_writes,
        "membership_writes": membership_writes,
        "item_writes": item_writes,
        "bounded_read_model_writes": bounded_writes,
        "rows_written": material_writes + summary_writes,
    }


def record_story_projection_failure(
    repository: Any,
    *,
    now_ms: int,
    error_code: str,
) -> None:
    repository.conn.execute(
        """
        UPDATE news_projection_summary
           SET last_attempt_at_ms=%s, last_error=%s, updated_at_ms=%s
         WHERE singleton_key='current'
        """,
        (now_ms, str(error_code)[:500], now_ms),
    )


def _replace_brief_selection(
    conn: Any,
    *,
    selection: Mapping[str, Any],
    now_ms: int,
) -> int:
    snapshot = dict(selection)
    fingerprint = str(snapshot.pop("selection_fingerprint"))
    if selection_fingerprint(snapshot) != fingerprint:
        raise RuntimeError("news_brief_selection_fingerprint_invalid")
    cursor = conn.execute(
        """
        INSERT INTO news_brief_selection_current (
          singleton_key, selection_fingerprint, projection_revision,
          selector_evaluated_at_ms, top_stories, selection_stats,
          selector_version, identity_version, updated_at_ms
        ) VALUES (
          true, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (singleton_key) DO UPDATE SET
          selection_fingerprint = EXCLUDED.selection_fingerprint,
          projection_revision = EXCLUDED.projection_revision,
          selector_evaluated_at_ms = EXCLUDED.selector_evaluated_at_ms,
          top_stories = EXCLUDED.top_stories,
          selection_stats = EXCLUDED.selection_stats,
          selector_version = EXCLUDED.selector_version,
          identity_version = EXCLUDED.identity_version,
          updated_at_ms = EXCLUDED.updated_at_ms
        WHERE news_brief_selection_current.selection_fingerprint
              IS DISTINCT FROM EXCLUDED.selection_fingerprint
        """,
        (
            fingerprint,
            str(snapshot["projection_revision"]),
            int(snapshot["selector_evaluated_at_ms"]),
            Jsonb(snapshot["top_stories"]),
            Jsonb(snapshot["selection_stats"]),
            str(snapshot["selector_version"]),
            str(snapshot["identity_version"]),
            int(now_ms),
        ),
    )
    return int(cursor.rowcount or 0)


def _publish_items(
    conn: Any,
    *,
    cutoff_ms: int,
    item_updates: Sequence[Mapping[str, Any]],
    now_ms: int,
) -> int:
    writes = int(
        conn.execute(
            """
            UPDATE news_items item
               SET active=(
                 item.published_at_ms >= %s AND EXISTS (
                   SELECT 1 FROM news_sources source
                    WHERE source.source_id=item.source_id AND source.enabled
                 )
               )
             WHERE item.active IS DISTINCT FROM (
               item.published_at_ms >= %s AND EXISTS (
                 SELECT 1 FROM news_sources source
                  WHERE source.source_id=item.source_id AND source.enabled
               )
             )
            """,
            (cutoff_ms, cutoff_ms),
        ).rowcount
        or 0
    )
    for item in item_updates:
        writes += int(
            conn.execute(
                """
                UPDATE news_items
                   SET importance_score=%(importance_score)s,
                       importance_factors=%(importance_factors)s,
                       level=%(level)s, category=%(category)s,
                       classification_source=%(classification_source)s,
                       classification_confidence=%(classification_confidence)s,
                       updated_at_ms=%(now_ms)s
                 WHERE item_id=%(item_id)s AND (
                   importance_score IS DISTINCT FROM %(importance_score)s OR
                   importance_factors IS DISTINCT FROM %(importance_factors)s OR
                   level IS DISTINCT FROM %(level)s OR
                   category IS DISTINCT FROM %(category)s OR
                   classification_source IS DISTINCT FROM %(classification_source)s OR
                   classification_confidence IS DISTINCT FROM %(classification_confidence)s
                 )
                """,
                {
                    **item,
                    "importance_factors": Jsonb(item["importance_factors"]),
                    "now_ms": now_ms,
                },
            ).rowcount
            or 0
        )
    return writes


def _upsert_stories(conn: Any, *, stories: Sequence[Mapping[str, Any]], now_ms: int) -> int:
    writes = 0
    for story in stories:
        writes += int(
            conn.execute(
                """
                INSERT INTO news_stories(
                  story_id, canonical_key, canonical_title,
                  representative_item_id, representative_source_id,
                  representative_title, representative_url,
                  representative_description, scoring_item_id, level, category,
                  importance_score, importance_factors, item_count, source_count,
                  first_published_at_ms, last_published_at_ms, state_fingerprint,
                  created_at_ms, updated_at_ms
                ) VALUES (
                  %(story_id)s, %(canonical_key)s, %(canonical_title)s,
                  %(representative_item_id)s, %(representative_source_id)s,
                  %(representative_title)s, %(representative_url)s,
                  %(representative_description)s, %(scoring_item_id)s,
                  %(level)s, %(category)s, %(importance_score)s,
                  %(importance_factors)s, %(item_count)s, %(source_count)s,
                  %(first_published_at_ms)s, %(last_published_at_ms)s,
                  %(state_fingerprint)s, %(now_ms)s, %(now_ms)s
                ) ON CONFLICT (story_id) DO UPDATE SET
                  canonical_key=EXCLUDED.canonical_key,
                  canonical_title=EXCLUDED.canonical_title,
                  representative_item_id=EXCLUDED.representative_item_id,
                  representative_source_id=EXCLUDED.representative_source_id,
                  representative_title=EXCLUDED.representative_title,
                  representative_url=EXCLUDED.representative_url,
                  representative_description=EXCLUDED.representative_description,
                  scoring_item_id=EXCLUDED.scoring_item_id,
                  level=EXCLUDED.level, category=EXCLUDED.category,
                  importance_score=EXCLUDED.importance_score,
                  importance_factors=EXCLUDED.importance_factors,
                  item_count=EXCLUDED.item_count, source_count=EXCLUDED.source_count,
                  first_published_at_ms=EXCLUDED.first_published_at_ms,
                  last_published_at_ms=EXCLUDED.last_published_at_ms,
                  state_fingerprint=EXCLUDED.state_fingerprint,
                  updated_at_ms=EXCLUDED.updated_at_ms
                WHERE news_stories.state_fingerprint IS DISTINCT FROM
                      EXCLUDED.state_fingerprint
                """,
                {
                    **story,
                    "importance_factors": Jsonb(story["importance_factors"]),
                    "now_ms": now_ms,
                },
            ).rowcount
            or 0
        )
    return writes


def _replace_memberships(conn: Any, *, memberships: Sequence[Mapping[str, Any]]) -> int:
    desired = Jsonb([dict(row) for row in memberships])
    writes = int(
        conn.execute(
            """
            DELETE FROM news_story_members existing
             WHERE NOT EXISTS (
               SELECT 1 FROM jsonb_to_recordset(%s::jsonb)
                 AS desired(story_id text, item_id text)
                WHERE desired.story_id=existing.story_id
                  AND desired.item_id=existing.item_id
             )
            """,
            (desired,),
        ).rowcount
        or 0
    )
    if memberships:
        writes += int(
            conn.execute(
                """
                INSERT INTO news_story_members(story_id, item_id)
                SELECT story_id, item_id FROM jsonb_to_recordset(%s::jsonb)
                  AS desired(story_id text, item_id text)
                ON CONFLICT (story_id, item_id) DO NOTHING
                """,
                (desired,),
            ).rowcount
            or 0
        )
    return writes


def _delete_absent_stories(conn: Any, *, stories: Sequence[Mapping[str, Any]]) -> int:
    story_ids = [str(row["story_id"]) for row in stories]
    if not story_ids:
        return int(conn.execute("DELETE FROM news_stories").rowcount or 0)
    return int(
        conn.execute(
            "DELETE FROM news_stories WHERE NOT (story_id=ANY(%s))",
            (story_ids,),
        ).rowcount
        or 0
    )


def _replace_facets(
    conn: Any,
    *,
    stories: Sequence[Mapping[str, Any]],
    memberships: Sequence[Mapping[str, Any]],
    now_ms: int,
) -> int:
    story_counts: Counter[tuple[str, str]] = Counter()
    for story in stories:
        story_counts[("category", str(story["category"]))] += 1
        story_counts[("level", str(story["level"]))] += 1
    source_by_item = {
        str(row["item_id"]): str(row["source_id"])
        for row in conn.execute("SELECT item_id, source_id FROM news_items WHERE active").fetchall()
    }
    story_sources: dict[str, set[str]] = {}
    for member in memberships:
        source_id = source_by_item.get(str(member["item_id"]))
        if source_id is not None:
            story_sources.setdefault(str(member["story_id"]), set()).add(source_id)
    source_counts: Counter[str] = Counter()
    for source_ids in story_sources.values():
        source_counts.update(source_ids)
    existing_story = {
        (str(row["facet_type"]), str(row["facet_value"])): int(row["story_count"])
        for row in conn.execute("SELECT facet_type, facet_value, story_count FROM news_story_facet_counts").fetchall()
    }
    existing_source = {
        str(row["source_id"]): int(row["story_count"])
        for row in conn.execute("SELECT source_id, story_count FROM news_source_facet_counts").fetchall()
    }
    writes = 0
    if dict(story_counts) != existing_story:
        writes += int(conn.execute("DELETE FROM news_story_facet_counts").rowcount or 0)
        for (facet_type, facet_value), count in sorted(story_counts.items()):
            writes += int(
                conn.execute(
                    """
                    INSERT INTO news_story_facet_counts(
                      facet_type, facet_value, story_count, updated_at_ms
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (facet_type, facet_value, count, now_ms),
                ).rowcount
                or 0
            )
    if dict(source_counts) != existing_source:
        writes += int(conn.execute("DELETE FROM news_source_facet_counts").rowcount or 0)
        for source_id, count in sorted(source_counts.items()):
            writes += int(
                conn.execute(
                    """
                    INSERT INTO news_source_facet_counts(
                      source_id, story_count, updated_at_ms
                    ) VALUES (%s, %s, %s)
                    """,
                    (source_id, count, now_ms),
                ).rowcount
                or 0
            )
    return writes
