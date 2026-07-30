from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from tracefold.platform.postgres.projection_frontier import (
    MODEL_FRONTIER,
    NEWS_FRONTIER,
)

from .brief import brief_fingerprint
from .classification import SEVERITY_VALUES, classify_by_keyword
from .identity import (
    MAX_CANDIDATE_BUCKET,
    STORY_SIMILARITY_THRESHOLD,
    candidate_tokens,
    normalize_story_text,
    story_similarity,
)
from .models import (
    BRIEF_WORKFLOW_VERSION,
    CLASSIFIER_VERSION,
    IMPORTANCE_VERSION,
    STORY_IDENTITY_VERSION,
    EventCategory,
    ThreatLevel,
)
from .ranking import (
    diplomacy_entity_keys,
    importance_factors,
    promote_diplomacy_severity,
)
from .repository import deterministic_id

_ACTIVE_WINDOW_MS = 96 * 60 * 60 * 1000
_ALIAS_TTL_MS = 7 * 24 * 60 * 60 * 1000
_SCORING_EPOCH_MS = 60 * 60 * 1000
_CLAIM_LEASE_MS = 5_000
_CLAIM_TRANSACTION_TIMEOUT_SECONDS = 0.5
_PUBLISH_TRANSACTION_TIMEOUT_SECONDS = 1.0
_STEADY_STATEMENT_TIMEOUT_SECONDS = 3.0
_MAINTENANCE_STATEMENT_TIMEOUT_SECONDS = 120.0
_INPUT_ROW_CAP = 10_000
_INPUT_BYTE_CAP = 4 * 1024 * 1024
_OUTPUT_BYTE_CAP = 1 * 1024 * 1024
_PAIR_BLOCK_CAP = 4_096
_ENTITY_PREFIX = "__entity__:"
NEWS_PROJECTION_VERSION = f"{STORY_IDENTITY_VERSION}:{CLASSIFIER_VERSION}:{IMPORTANCE_VERSION}:incremental-v1"

_CATEGORY_ORDER: tuple[EventCategory, ...] = (
    "conflict",
    "protest",
    "disaster",
    "diplomatic",
    "economic",
    "terrorism",
    "cyber",
    "health",
    "environmental",
    "military",
    "crime",
    "infrastructure",
    "tech",
    "general",
)


class NewsShardOversized(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NewsProjectionClaim:
    bucket_id: str
    kind: str
    item_id: str
    story_id: str
    runtime_id: str
    input_fingerprint: str
    projection_version: str
    deadline_at_ms: int


class NewsProjectionService:
    """Short claim/load/publish transactions for one News identity shard."""

    def __init__(
        self,
        *,
        db: Any,
        worker_name: str = "steady_projection_coordinator",
    ) -> None:
        self.db = db
        self.worker_name = worker_name

    def next_due(self, *, now_ms: int) -> dict[str, Any] | None:
        with self._session() as repos:
            return cast(
                dict[str, Any] | None,
                repos.projection_frontiers.next_due(
                    NEWS_FRONTIER,
                    now_ms=now_ms,
                ),
            )

    def claim(
        self,
        *,
        bucket_id: str,
        runtime_id: str,
        now_ms: int,
    ) -> NewsProjectionClaim | None:
        kind, entity_id = _parse_bucket(bucket_id)
        with self._session(
            transaction_timeout_seconds=_CLAIM_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            row = repos.projection_frontiers.claim(
                NEWS_FRONTIER,
                key={"bucket_id": bucket_id},
                runtime_id=runtime_id,
                now_ms=now_ms,
                lease_ms=_CLAIM_LEASE_MS,
            )
        if row is None:
            return None
        return NewsProjectionClaim(
            bucket_id=bucket_id,
            kind=kind,
            item_id=entity_id if kind == "identity" else "",
            story_id=entity_id if kind == "score" else "",
            runtime_id=str(UUID(str(runtime_id))),
            input_fingerprint=str(row["input_fingerprint"]),
            projection_version=str(row["projection_version"]),
            deadline_at_ms=int(row["deadline_at_ms"]),
        )

    def load_target(
        self,
        claim: NewsProjectionClaim,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        with self._session() as repos:
            row = repos.conn.execute(
                """
                SELECT item.*, source.name AS source_name, source.tier,
                       source.enabled AS source_enabled,
                       feature.normalized_title AS old_normalized_title,
                       feature.candidate_tokens AS old_candidate_tokens,
                       feature.feature_fingerprint AS old_feature_fingerprint,
                       feature.published_at_ms AS old_feature_published_at_ms,
                       feature.expires_at_ms AS old_expires_at_ms,
                       feature.active AS old_feature_active
                  FROM news_items item
                  JOIN news_sources source ON source.source_id = item.source_id
                  LEFT JOIN news_identity_features feature
                    ON feature.item_id = item.item_id
                 WHERE item.item_id = %s
                """,
                (claim.item_id,),
            ).fetchone()
        if row is None:
            return {
                "status": "stale_snapshot",
                "reason": "news_item_missing",
            }
        payload = {
            "status": "loaded",
            "now_ms": int(now_ms),
            "item": dict(row),
        }
        _require_bounded_input(payload)
        return payload

    def load_score(
        self,
        claim: NewsProjectionClaim,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        if claim.kind != "score":
            raise ValueError("news_score_claim_required")
        with self._session() as repos:
            conn = repos.conn
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT item.*, source.name AS source_name, source.tier,
                           source.enabled AS source_enabled,
                           feature.normalized_title,
                           feature.candidate_tokens,
                           feature.feature_fingerprint,
                           feature.published_at_ms AS feature_published_at_ms,
                           feature.expires_at_ms,
                           feature.active AS feature_active
                      FROM news_story_members membership
                      JOIN news_items item ON item.item_id = membership.item_id
                      JOIN news_sources source ON source.source_id = item.source_id
                      JOIN news_identity_features feature
                        ON feature.item_id = item.item_id
                     WHERE membership.story_id = %(story_id)s
                       AND membership.current
                       AND feature.active
                       AND feature.expires_at_ms > %(now_ms)s
                       AND source.enabled
                     ORDER BY item.item_id
                     LIMIT %(limit)s
                    """,
                    {
                        "story_id": claim.story_id,
                        "now_ms": int(now_ms),
                        "limit": _INPUT_ROW_CAP + 1,
                    },
                ).fetchall()
            ]
            if len(rows) > _INPUT_ROW_CAP:
                raise NewsShardOversized("news_score_component_rows_overflow")
            if not rows:
                return {"status": "obsolete"}
            item_ids = [str(row["item_id"]) for row in rows]
            previous_memberships = [{"item_id": item_id, "story_id": claim.story_id} for item_id in item_ids]
            alias_keys = sorted(
                {_alias_key(str(row["normalized_title"])) for row in rows if str(row["normalized_title"])}
            )
            aliases = (
                [
                    dict(row)
                    for row in conn.execute(
                        """
                    SELECT alias_key, story_id
                      FROM news_story_aliases
                     WHERE expires_at_ms > %(now_ms)s
                       AND alias_key = ANY(%(alias_keys)s)
                     ORDER BY alias_key
                    """,
                        {
                            "now_ms": int(now_ms),
                            "alias_keys": alias_keys,
                        },
                    ).fetchall()
                ]
                if alias_keys
                else []
            )
            entity_tokens = sorted(
                {
                    str(token)
                    for row in rows
                    for token in row["candidate_tokens"]
                    if str(token).startswith(_ENTITY_PREFIX)
                }
            )
            entity_rows = (
                [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT token, feature.item_id, item.source_id, source.tier
                          FROM news_identity_features feature
                          JOIN news_items item ON item.item_id = feature.item_id
                          JOIN news_sources source ON source.source_id = item.source_id
                          CROSS JOIN LATERAL unnest(feature.candidate_tokens) token
                         WHERE feature.active
                           AND feature.expires_at_ms > %(now_ms)s
                           AND item.published_at_ms >= %(entity_cutoff_ms)s
                           AND source.enabled
                           AND token = ANY(%(tokens)s)
                         ORDER BY token, feature.item_id
                         LIMIT %(limit)s
                        """,
                        {
                            "now_ms": int(now_ms),
                            "entity_cutoff_ms": _scoring_epoch(now_ms) - 86_400_000,
                            "tokens": entity_tokens,
                            "limit": _INPUT_ROW_CAP + 1,
                        },
                    ).fetchall()
                ]
                if entity_tokens
                else []
            )
            if len(entity_rows) > _INPUT_ROW_CAP:
                raise NewsShardOversized("news_score_entity_rows_overflow")
        first = rows[0]
        feature = {
            "item_id": str(first["item_id"]),
            "normalized_title": str(first["normalized_title"]),
            "lexical_tokens": [
                str(token) for token in first["candidate_tokens"] if not str(token).startswith(_ENTITY_PREFIX)
            ],
            "candidate_tokens": [str(token) for token in first["candidate_tokens"]],
            "feature_fingerprint": str(first["feature_fingerprint"]),
            "published_at_ms": int(first["feature_published_at_ms"]),
            "expires_at_ms": int(first["expires_at_ms"]),
            "active": True,
        }
        context = {
            "status": "loaded",
            "now_ms": int(now_ms),
            "target_item_id": feature["item_id"],
            "target_feature": feature,
            "target_old_feature_fingerprint": feature["feature_fingerprint"],
            "target_old_tokens": feature["lexical_tokens"],
            "target_old_active": True,
            "crossing_tokens": [],
            "token_counts": {},
            "rows": rows,
            "existing_edges": [],
            "previous_memberships": previous_memberships,
            "aliases": aliases,
            "entity_rows": entity_rows,
            "snapshot_fingerprint": _context_fingerprint(rows),
        }
        _require_bounded_input(context)
        return {
            "status": "loaded",
            "feature": feature,
            "context": context,
        }

    def load_context(
        self,
        claim: NewsProjectionClaim,
        feature: dict[str, Any],
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        lexical_tokens = [str(token) for token in feature["lexical_tokens"]]
        normalized_title = str(feature["normalized_title"])
        with self._session() as repos:
            conn = repos.conn
            candidate_rows = conn.execute(
                """
                SELECT feature.*, item.title, item.content_fingerprint,
                       item.source_id, source.name AS source_name, source.tier,
                       source.enabled AS source_enabled
                  FROM news_identity_features feature
                  JOIN news_items item ON item.item_id = feature.item_id
                  JOIN news_sources source ON source.source_id = item.source_id
                 WHERE feature.active
                   AND feature.expires_at_ms > %(now_ms)s
                   AND source.enabled
                   AND (
                     feature.normalized_title = %(normalized_title)s
                     OR feature.candidate_tokens && %(tokens)s
                   )
                 ORDER BY feature.item_id
                 LIMIT %(limit)s
                """,
                {
                    "now_ms": int(now_ms),
                    "normalized_title": normalized_title,
                    "tokens": lexical_tokens,
                    "limit": _INPUT_ROW_CAP + 1,
                },
            ).fetchall()
            if len(candidate_rows) > _INPUT_ROW_CAP:
                raise NewsShardOversized("news_candidate_rows_overflow")

            target_row = conn.execute(
                """
                SELECT item.*, source.name AS source_name, source.tier,
                       source.enabled AS source_enabled,
                       old.normalized_title AS old_normalized_title,
                       old.candidate_tokens AS old_candidate_tokens,
                       old.feature_fingerprint AS old_feature_fingerprint,
                       old.published_at_ms AS old_feature_published_at_ms,
                       old.expires_at_ms AS old_expires_at_ms,
                       old.active AS old_feature_active
                  FROM news_items item
                  JOIN news_sources source ON source.source_id = item.source_id
                  LEFT JOIN news_identity_features old ON old.item_id = item.item_id
                 WHERE item.item_id = %s
                """,
                (claim.item_id,),
            ).fetchone()
            if target_row is None:
                return {"status": "stale_snapshot", "reason": "news_item_missing"}

            old_tokens = [
                str(token)
                for token in (target_row.get("old_candidate_tokens") or [])
                if not str(token).startswith(_ENTITY_PREFIX)
            ]
            count_tokens = sorted(set(old_tokens) | set(lexical_tokens))
            token_counts = _token_counts(conn, tokens=count_tokens, now_ms=now_ms)
            old_active = (
                bool(target_row.get("old_feature_active")) and int(target_row.get("old_expires_at_ms") or 0) > now_ms
            )
            projected_counts = dict(token_counts)
            for token in count_tokens:
                if old_active and token in old_tokens:
                    projected_counts[token] = projected_counts.get(token, 0) - 1
                if bool(feature["active"]) and token in lexical_tokens:
                    projected_counts[token] = projected_counts.get(token, 0) + 1
            crossing_tokens = sorted(
                token
                for token in count_tokens
                if (token_counts.get(token, 0) <= MAX_CANDIDATE_BUCKET)
                != (projected_counts.get(token, 0) <= MAX_CANDIDATE_BUCKET)
            )

            seed_ids = {
                claim.item_id,
                *(str(row["item_id"]) for row in candidate_rows),
            }
            if crossing_tokens:
                crossing_rows = conn.execute(
                    """
                    SELECT feature.item_id
                      FROM news_identity_features feature
                      JOIN news_items item ON item.item_id = feature.item_id
                      JOIN news_sources source ON source.source_id = item.source_id
                     WHERE feature.active
                       AND feature.expires_at_ms > %(now_ms)s
                       AND source.enabled
                       AND feature.candidate_tokens && %(tokens)s
                     ORDER BY feature.item_id
                     LIMIT %(limit)s
                    """,
                    {
                        "now_ms": int(now_ms),
                        "tokens": crossing_tokens,
                        "limit": _INPUT_ROW_CAP + 1,
                    },
                ).fetchall()
                if len(crossing_rows) > _INPUT_ROW_CAP:
                    raise NewsShardOversized("news_crossing_bucket_rows_overflow")
                seed_ids.update(str(row["item_id"]) for row in crossing_rows)

            closure_ids = _edge_closure(
                conn,
                seed_ids=sorted(seed_ids),
                now_ms=now_ms,
                row_cap=_INPUT_ROW_CAP,
            )
            closure_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT item.*, source.name AS source_name, source.tier,
                           source.enabled AS source_enabled,
                           feature.normalized_title,
                           feature.candidate_tokens,
                           feature.feature_fingerprint,
                           feature.published_at_ms AS feature_published_at_ms,
                           feature.expires_at_ms,
                           feature.active AS feature_active
                      FROM news_items item
                      JOIN news_sources source ON source.source_id = item.source_id
                      LEFT JOIN news_identity_features feature
                        ON feature.item_id = item.item_id
                     WHERE item.item_id = ANY(%s)
                     ORDER BY item.item_id
                    """,
                    (closure_ids,),
                ).fetchall()
            ]
            all_tokens = sorted(
                {
                    str(token)
                    for row in closure_rows
                    for token in (row.get("candidate_tokens") or [])
                    if not str(token).startswith(_ENTITY_PREFIX)
                }
                | set(lexical_tokens)
            )
            all_token_counts = _token_counts(
                conn,
                tokens=all_tokens,
                now_ms=now_ms,
            )
            for token in all_tokens:
                if old_active and token in old_tokens:
                    all_token_counts[token] = all_token_counts.get(token, 0) - 1
                if bool(feature["active"]) and token in lexical_tokens:
                    all_token_counts[token] = all_token_counts.get(token, 0) + 1

            existing_edges = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT left_item_id, right_item_id, similarity, expires_at_ms
                      FROM news_similarity_edges
                     WHERE left_item_id = ANY(%(ids)s)
                       AND right_item_id = ANY(%(ids)s)
                       AND expires_at_ms > %(now_ms)s
                     ORDER BY left_item_id, right_item_id
                     LIMIT %(limit)s
                    """,
                    {
                        "ids": closure_ids,
                        "now_ms": int(now_ms),
                        "limit": _INPUT_ROW_CAP + 1,
                    },
                ).fetchall()
            ]
            if len(existing_edges) > _INPUT_ROW_CAP:
                raise NewsShardOversized("news_component_edges_overflow")
            previous_memberships = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT item_id, story_id
                      FROM news_story_members
                     WHERE current AND item_id = ANY(%s)
                     ORDER BY item_id
                    """,
                    (closure_ids,),
                ).fetchall()
            ]
            alias_keys = sorted(
                {
                    _alias_key(str(row.get("normalized_title") or ""))
                    for row in closure_rows
                    if str(row.get("normalized_title") or "")
                }
                | ({_alias_key(str(feature["normalized_title"]))} if str(feature["normalized_title"]) else set())
            )
            alias_rows = (
                [
                    dict(row)
                    for row in conn.execute(
                        """
                    SELECT alias_key, story_id
                      FROM news_story_aliases
                     WHERE expires_at_ms > %(now_ms)s
                       AND alias_key = ANY(%(alias_keys)s)
                     ORDER BY alias_key
                    """,
                        {
                            "now_ms": int(now_ms),
                            "alias_keys": alias_keys,
                        },
                    ).fetchall()
                ]
                if alias_keys
                else []
            )
            entity_tokens = sorted(
                {
                    str(token)
                    for row in closure_rows
                    for token in (row.get("candidate_tokens") or [])
                    if str(token).startswith(_ENTITY_PREFIX)
                }
                | {str(token) for token in feature["candidate_tokens"] if str(token).startswith(_ENTITY_PREFIX)}
            )
            entity_rows = (
                [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT token, feature.item_id, item.source_id, source.tier
                          FROM news_identity_features feature
                          JOIN news_items item ON item.item_id = feature.item_id
                          JOIN news_sources source ON source.source_id = item.source_id
                          CROSS JOIN LATERAL unnest(feature.candidate_tokens) token
                         WHERE feature.active
                           AND feature.expires_at_ms > %(now_ms)s
                           AND item.published_at_ms >= %(entity_cutoff_ms)s
                           AND source.enabled
                           AND token = ANY(%(tokens)s)
                         ORDER BY token, feature.item_id
                         LIMIT %(limit)s
                        """,
                        {
                            "now_ms": int(now_ms),
                            "entity_cutoff_ms": _scoring_epoch(now_ms) - 86_400_000,
                            "tokens": entity_tokens,
                            "limit": _INPUT_ROW_CAP + 1,
                        },
                    ).fetchall()
                ]
                if entity_tokens
                else []
            )
            if len(entity_rows) > _INPUT_ROW_CAP:
                raise NewsShardOversized("news_entity_rows_overflow")

        payload = {
            "status": "loaded",
            "now_ms": int(now_ms),
            "target_item_id": claim.item_id,
            "target_feature": feature,
            "target_old_feature_fingerprint": target_row.get("old_feature_fingerprint"),
            "target_old_tokens": old_tokens,
            "target_old_active": old_active,
            "crossing_tokens": crossing_tokens,
            "token_counts": all_token_counts,
            "rows": closure_rows,
            "existing_edges": existing_edges,
            "previous_memberships": previous_memberships,
            "aliases": alias_rows,
            "entity_rows": entity_rows,
            "snapshot_fingerprint": _context_fingerprint(closure_rows),
        }
        _require_bounded_input(payload)
        return payload

    def publish(
        self,
        claim: NewsProjectionClaim,
        *,
        feature: dict[str, Any],
        context: dict[str, Any],
        edge_plan: dict[str, Any],
        projection: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        _require_bounded_output(projection)
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            conn = repos.conn
            current_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT item.*, source.name AS source_name, source.tier,
                           source.enabled AS source_enabled,
                           current.normalized_title,
                           current.candidate_tokens,
                           current.feature_fingerprint,
                           current.published_at_ms AS feature_published_at_ms,
                           current.expires_at_ms,
                           current.active AS feature_active
                      FROM news_items item
                      JOIN news_sources source ON source.source_id = item.source_id
                      LEFT JOIN news_identity_features current
                        ON current.item_id = item.item_id
                     WHERE item.item_id = ANY(%s)
                     ORDER BY item.item_id
                    """,
                    ([str(row["item_id"]) for row in context["rows"]],),
                ).fetchall()
            ]
            if _context_fingerprint(current_rows) != context["snapshot_fingerprint"]:
                repos.projection_frontiers.release_stale(
                    NEWS_FRONTIER,
                    key={"bucket_id": claim.bucket_id},
                    runtime_id=claim.runtime_id,
                    now_ms=now_ms,
                )
                return {"projection_status": "stale_snapshot", "rows_written": 0}

            feature_writes = int(
                conn.execute(
                    """
                    INSERT INTO news_identity_features (
                      item_id, normalized_title, candidate_tokens,
                      feature_fingerprint, published_at_ms, expires_at_ms,
                      active, updated_at_ms
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (item_id) DO UPDATE SET
                      normalized_title = EXCLUDED.normalized_title,
                      candidate_tokens = EXCLUDED.candidate_tokens,
                      feature_fingerprint = EXCLUDED.feature_fingerprint,
                      published_at_ms = EXCLUDED.published_at_ms,
                      expires_at_ms = EXCLUDED.expires_at_ms,
                      active = EXCLUDED.active,
                      updated_at_ms = EXCLUDED.updated_at_ms
                    WHERE (
                      news_identity_features.normalized_title,
                      news_identity_features.candidate_tokens,
                      news_identity_features.feature_fingerprint,
                      news_identity_features.published_at_ms,
                      news_identity_features.expires_at_ms,
                      news_identity_features.active
                    ) IS DISTINCT FROM (
                      EXCLUDED.normalized_title,
                      EXCLUDED.candidate_tokens,
                      EXCLUDED.feature_fingerprint,
                      EXCLUDED.published_at_ms,
                      EXCLUDED.expires_at_ms,
                      EXCLUDED.active
                    )
                    """,
                    (
                        context["target_item_id"],
                        feature["normalized_title"],
                        feature["candidate_tokens"],
                        feature["feature_fingerprint"],
                        feature["published_at_ms"],
                        feature["expires_at_ms"],
                        feature["active"],
                        now_ms,
                    ),
                ).rowcount
                or 0
            )
            item_active_writes = int(
                conn.execute(
                    """
                    UPDATE news_items
                       SET active = %s, updated_at_ms = %s
                     WHERE item_id = %s AND active IS DISTINCT FROM %s
                    """,
                    (
                        feature["active"],
                        now_ms,
                        context["target_item_id"],
                        feature["active"],
                    ),
                ).rowcount
                or 0
            )

            affected_pairs = [tuple(pair) for pair in edge_plan["affected_pairs"]]
            edge_deletes = 0
            if affected_pairs:
                edge_deletes = int(
                    conn.execute(
                        """
                        WITH keys(left_item_id, right_item_id) AS (
                          SELECT *
                            FROM unnest(%s::text[], %s::text[])
                        )
                        DELETE FROM news_similarity_edges edge
                         USING keys
                         WHERE edge.left_item_id = keys.left_item_id
                           AND edge.right_item_id = keys.right_item_id
                        """,
                        (
                            [pair[0] for pair in affected_pairs],
                            [pair[1] for pair in affected_pairs],
                        ),
                    ).rowcount
                    or 0
                )
            edge_writes = 0
            for edge in edge_plan["new_edges"]:
                edge_writes += int(
                    conn.execute(
                        """
                        INSERT INTO news_similarity_edges (
                          left_item_id, right_item_id, similarity,
                          identity_version, expires_at_ms, updated_at_ms
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (left_item_id, right_item_id) DO UPDATE SET
                          similarity = EXCLUDED.similarity,
                          identity_version = EXCLUDED.identity_version,
                          expires_at_ms = EXCLUDED.expires_at_ms,
                          updated_at_ms = EXCLUDED.updated_at_ms
                        WHERE (
                          news_similarity_edges.similarity,
                          news_similarity_edges.identity_version,
                          news_similarity_edges.expires_at_ms
                        ) IS DISTINCT FROM (
                          EXCLUDED.similarity,
                          EXCLUDED.identity_version,
                          EXCLUDED.expires_at_ms
                        )
                        """,
                        (
                            edge["left_item_id"],
                            edge["right_item_id"],
                            edge["similarity"],
                            STORY_IDENTITY_VERSION,
                            edge["expires_at_ms"],
                            now_ms,
                        ),
                    ).rowcount
                    or 0
                )

            item_score_writes = 0
            for item in projection["item_updates"]:
                item_score_writes += int(
                    conn.execute(
                        """
                        UPDATE news_items
                           SET importance_score = %s,
                               importance_factors = %s,
                               level = %s,
                               category = %s,
                               classification_source = %s,
                               classification_confidence = %s,
                               updated_at_ms = %s
                         WHERE item_id = %s
                           AND (
                             importance_score IS DISTINCT FROM %s
                             OR importance_factors IS DISTINCT FROM %s
                             OR level IS DISTINCT FROM %s
                             OR category IS DISTINCT FROM %s
                             OR classification_source IS DISTINCT FROM %s
                             OR classification_confidence IS DISTINCT FROM %s
                           )
                        """,
                        (
                            item["importance_score"],
                            Jsonb(item["importance_factors"]),
                            item["level"],
                            item["category"],
                            item["classification_source"],
                            item["classification_confidence"],
                            now_ms,
                            item["item_id"],
                            item["importance_score"],
                            Jsonb(item["importance_factors"]),
                            item["level"],
                            item["category"],
                            item["classification_source"],
                            item["classification_confidence"],
                        ),
                    ).rowcount
                    or 0
                )

            story_writes = 0
            for story in projection["stories"]:
                story_writes += _upsert_story(conn, story=story, now_ms=now_ms)
            published_story_ids = [str(row["story_id"]) for row in projection["stories"]]
            old_story_ids = [str(value) for value in projection["old_story_ids"]]
            if old_story_ids:
                story_writes += int(
                    conn.execute(
                        """
                        UPDATE news_stories
                           SET active = false, updated_at_ms = %s
                         WHERE story_id = ANY(%s)
                           AND NOT (story_id = ANY(%s))
                           AND active
                        """,
                        (now_ms, old_story_ids, published_story_ids or [""]),
                    ).rowcount
                    or 0
                )

            desired_memberships = {str(row["item_id"]): str(row["story_id"]) for row in projection["memberships"]}
            closure_ids = [str(value) for value in projection["closure_item_ids"]]
            membership_writes = int(
                conn.execute(
                    """
                    UPDATE news_story_members
                       SET current = false
                     WHERE current
                       AND item_id = ANY(%s)
                       AND (
                         NOT (item_id = ANY(%s))
                         OR (item_id, story_id) NOT IN (
                           SELECT *
                             FROM unnest(%s::text[], %s::text[])
                         )
                       )
                    """,
                    (
                        closure_ids,
                        list(desired_memberships) or [""],
                        list(desired_memberships) or [""],
                        list(desired_memberships.values()) or [""],
                    ),
                ).rowcount
                or 0
            )
            for item_id, story_id in sorted(desired_memberships.items()):
                membership_writes += int(
                    conn.execute(
                        """
                        INSERT INTO news_story_members (
                          story_id, item_id, current,
                          first_joined_at_ms, last_confirmed_at_ms
                        )
                        VALUES (%s, %s, true, %s, %s)
                        ON CONFLICT (story_id, item_id) DO UPDATE SET
                          current = true,
                          last_confirmed_at_ms = EXCLUDED.last_confirmed_at_ms
                        WHERE NOT news_story_members.current
                        """,
                        (story_id, item_id, now_ms, now_ms),
                    ).rowcount
                    or 0
                )
            for alias in projection["aliases"]:
                conn.execute(
                    """
                    INSERT INTO news_story_aliases (
                      alias_key, story_id, expires_at_ms, created_at_ms
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (alias_key) DO UPDATE SET
                      story_id = EXCLUDED.story_id,
                      expires_at_ms = EXCLUDED.expires_at_ms
                    WHERE news_story_aliases.story_id IS DISTINCT FROM EXCLUDED.story_id
                       OR news_story_aliases.expires_at_ms < EXCLUDED.expires_at_ms
                    """,
                    (
                        alias["alias_key"],
                        alias["story_id"],
                        now_ms + _ALIAS_TTL_MS,
                        now_ms,
                    ),
                )

            serving_rows = item_active_writes + item_score_writes + story_writes + membership_writes
            if serving_rows:
                brief_candidates = repos.news.brief_candidates()
                repos.projection_frontiers.mark_dirty(
                    MODEL_FRONTIER,
                    key={
                        "candidate_kind": "news_brief",
                        "shard_key": "current",
                    },
                    dirty_at_ms=now_ms,
                    deadline_at_ms=now_ms + 10 * 60 * 1000,
                    input_fingerprint=brief_fingerprint(brief_candidates),
                    version=BRIEF_WORKFLOW_VERSION,
                )
            if not repos.projection_frontiers.complete(
                NEWS_FRONTIER,
                key={"bucket_id": claim.bucket_id},
                runtime_id=claim.runtime_id,
                input_fingerprint=claim.input_fingerprint,
                version=claim.projection_version,
                now_ms=now_ms,
            ):
                raise RuntimeError("news_projection_publish_frontier_cas_failed")
            if claim.kind == "identity" and bool(feature["active"]) and int(feature["expires_at_ms"]) > now_ms:
                repos.projection_frontiers.mark_dirty(
                    NEWS_FRONTIER,
                    key={"bucket_id": claim.bucket_id},
                    dirty_at_ms=now_ms,
                    deadline_at_ms=int(feature["expires_at_ms"]),
                    input_fingerprint=_stable_hash(
                        {
                            "kind": "expiry",
                            "item_id": context["target_item_id"],
                            "feature_fingerprint": feature["feature_fingerprint"],
                            "expires_at_ms": feature["expires_at_ms"],
                        }
                    ),
                    version=claim.projection_version,
                    extra_insert={"active_item_count": 1},
                )
            published_story_id_set = {str(story["story_id"]) for story in projection["stories"]}
            retired_story_ids = {str(value) for value in projection["old_story_ids"]} - published_story_id_set
            if retired_story_ids:
                conn.execute(
                    """
                    DELETE FROM news_projection_frontiers
                     WHERE bucket_id = ANY(%s)
                    """,
                    ([f"score:{story_id}" for story_id in sorted(retired_story_ids)],),
                )
            next_score_at_ms = _scoring_epoch(now_ms) + _SCORING_EPOCH_MS
            for story in projection["stories"]:
                story_id = str(story["story_id"])
                repos.projection_frontiers.mark_dirty(
                    NEWS_FRONTIER,
                    key={"bucket_id": f"score:{story_id}"},
                    dirty_at_ms=now_ms,
                    deadline_at_ms=next_score_at_ms,
                    input_fingerprint=_stable_hash(
                        {
                            "kind": "score",
                            "story_id": story_id,
                            "story_fingerprint": story["state_fingerprint"],
                            "scoring_epoch_ms": next_score_at_ms,
                        }
                    ),
                    version=claim.projection_version,
                    extra_insert={"active_item_count": int(story["item_count"])},
                )

        return {
            "projection_status": "published",
            "rows_written": serving_rows + feature_writes + edge_deletes + edge_writes,
            "serving_rows_written": serving_rows,
            "feature_rows_written": feature_writes,
            "edge_rows_deleted": edge_deletes,
            "edge_rows_written": edge_writes,
            "story_rows_written": story_writes,
            "membership_rows_written": membership_writes,
            "candidate_pairs": len(edge_plan["recompute_pairs"]),
            "pair_blocks": int(edge_plan["pair_blocks"]),
            "closure_items": len(projection["closure_item_ids"]),
        }

    def release_stale(self, claim: NewsProjectionClaim, *, now_ms: int) -> bool:
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            return bool(
                repos.projection_frontiers.release_stale(
                    NEWS_FRONTIER,
                    key={"bucket_id": claim.bucket_id},
                    runtime_id=claim.runtime_id,
                    now_ms=now_ms,
                )
            )

    def complete_obsolete(self, claim: NewsProjectionClaim, *, now_ms: int) -> bool:
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            return bool(
                repos.projection_frontiers.complete(
                    NEWS_FRONTIER,
                    key={"bucket_id": claim.bucket_id},
                    runtime_id=claim.runtime_id,
                    input_fingerprint=claim.input_fingerprint,
                    version=claim.projection_version,
                    now_ms=now_ms,
                )
            )

    def fail_deterministic(
        self,
        claim: NewsProjectionClaim,
        *,
        error_code: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            return cast(
                dict[str, Any] | None,
                repos.projection_frontiers.fail_deterministic(
                    NEWS_FRONTIER,
                    key={"bucket_id": claim.bucket_id},
                    runtime_id=claim.runtime_id,
                    error_code=error_code,
                    now_ms=now_ms,
                ),
            )

    def fail_transient(
        self,
        claim: NewsProjectionClaim,
        *,
        error_code: str,
        now_ms: int,
    ) -> bool:
        with self._session(
            transaction_timeout_seconds=_PUBLISH_TRANSACTION_TIMEOUT_SECONDS,
        ) as repos, repos.transaction():
            return bool(
                repos.projection_frontiers.fail_transient(
                    NEWS_FRONTIER,
                    key={"bucket_id": claim.bucket_id},
                    runtime_id=claim.runtime_id,
                    error_code=error_code,
                    now_ms=now_ms,
                )
            )

    def _session(
        self,
        *,
        transaction_timeout_seconds: float | None = None,
    ) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=(
                _MAINTENANCE_STATEMENT_TIMEOUT_SECONDS
                if self.worker_name == "news_maintenance_rebuild"
                else _STEADY_STATEMENT_TIMEOUT_SECONDS
            ),
            transaction_timeout_seconds=transaction_timeout_seconds,
        )


def rebuild_all_news_for_maintenance(
    *,
    db: Any,
    now_ms: int,
) -> dict[str, Any]:
    """Seed incremental News identity state from the active material window."""

    service = NewsProjectionService(
        db=db,
        worker_name="news_maintenance_rebuild",
    )
    cutoff_ms = int(now_ms) - _ACTIVE_WINDOW_MS
    with service._session() as repos, repos.transaction():
        reset = {
            "frontiers": int(repos.conn.execute("DELETE FROM news_projection_frontiers").rowcount or 0),
            "similarity_edges": int(repos.conn.execute("DELETE FROM news_similarity_edges").rowcount or 0),
            "identity_features": int(repos.conn.execute("DELETE FROM news_identity_features").rowcount or 0),
        }
        items = [
            dict(row)
            for row in repos.conn.execute(
                """
                SELECT item.item_id, item.content_fingerprint,
                       item.published_at_ms, item.active,
                       source.enabled AS source_enabled
                FROM news_items item
                JOIN news_sources source ON source.source_id = item.source_id
                WHERE item.active OR item.published_at_ms >= %s
                ORDER BY item.item_id
                """,
                (cutoff_ms,),
            ).fetchall()
        ]
        for item in items:
            active = bool(item["source_enabled"]) and (int(item["published_at_ms"]) + _ACTIVE_WINDOW_MS > int(now_ms))
            input_fingerprint = _stable_hash(
                {
                    "item_id": str(item["item_id"]),
                    "content_fingerprint": str(item["content_fingerprint"]),
                    "published_at_ms": int(item["published_at_ms"]),
                    "active": active,
                }
            )
            repos.projection_frontiers.mark_dirty(
                NEWS_FRONTIER,
                key={
                    "bucket_id": f"identity:{item['item_id']}",
                },
                dirty_at_ms=int(now_ms),
                deadline_at_ms=int(now_ms),
                input_fingerprint=input_fingerprint,
                version=NEWS_PROJECTION_VERSION,
                extra_insert={"active_item_count": int(active)},
            )

    runtime_id = str(uuid4())
    results: list[dict[str, Any]] = []
    while True:
        due = service.next_due(now_ms=int(now_ms))
        if due is None:
            break
        if int(due["deadline_at_ms"]) > int(now_ms):
            break
        bucket_id = str(due["bucket_id"])
        if not bucket_id.startswith("identity:"):
            raise RuntimeError(f"news_maintenance_unexpected_due_frontier:{bucket_id}")
        claim = service.claim(
            bucket_id=bucket_id,
            runtime_id=runtime_id,
            now_ms=int(now_ms),
        )
        if claim is None:
            raise RuntimeError("news_maintenance_claim_missing")
        loaded = service.load_target(claim, now_ms=int(now_ms))
        if loaded["status"] != "loaded":
            raise RuntimeError(f"news_maintenance_load_failed:{loaded['status']}")
        feature = compute_news_identity_feature(loaded)
        context = service.load_context(
            claim,
            feature,
            now_ms=int(now_ms),
        )
        if context["status"] != "loaded":
            raise RuntimeError(f"news_maintenance_context_failed:{context['status']}")
        edge_plan = plan_news_edge_pairs(context)
        new_edges: list[dict[str, Any]] = []
        pairs = list(edge_plan["recompute_pairs"])
        for offset in range(0, len(pairs), _PAIR_BLOCK_CAP):
            new_edges.extend(compute_news_edge_block(pairs[offset : offset + _PAIR_BLOCK_CAP]))
        edge_plan["new_edges"] = new_edges
        final_edges = merge_final_edges(
            existing_edges=context["existing_edges"],
            affected_pairs=edge_plan["affected_pairs"],
            new_edges=new_edges,
        )
        projection = compute_news_component_projection(
            {
                **context,
                "final_edges": final_edges,
            }
        )
        result = service.publish(
            claim,
            feature=feature,
            context=context,
            edge_plan=edge_plan,
            projection=projection,
            now_ms=int(now_ms),
        )
        if result["projection_status"] != "published":
            raise RuntimeError(f"news_maintenance_publish_failed:{result['projection_status']}")
        results.append(result)

    with service._session() as repos:
        counts = dict(
            repos.conn.execute(
                """
                SELECT
                  (SELECT count(*) FROM news_identity_features
                    WHERE active) AS active_features,
                  (SELECT count(*) FROM news_similarity_edges) AS edges,
                  (SELECT count(*) FROM news_stories
                    WHERE active) AS active_stories,
                  (SELECT count(*) FROM news_projection_frontiers
                    WHERE status = 'quarantined') AS quarantined
                """
            ).fetchone()
        )
    if int(counts["quarantined"]):
        raise RuntimeError(f"news_maintenance_quarantine_unresolved:{counts['quarantined']}")
    return {
        "projection_status": "rebuilt",
        "items_seeded": len(items),
        "shards_computed": len(results),
        "rows_written": sum(int(row["rows_written"]) for row in results),
        "active_features": int(counts["active_features"]),
        "similarity_edges": int(counts["edges"]),
        "active_stories": int(counts["active_stories"]),
        "reset": reset,
    }


def compute_news_identity_feature(payload: dict[str, Any]) -> dict[str, Any]:
    """Pure feature extraction for one material News item."""

    item = dict(payload["item"])
    now_ms = int(payload["now_ms"])
    title = str(item["title"])
    normalized = normalize_story_text(title)
    lexical = sorted(candidate_tokens(title))
    entity = sorted(f"{_ENTITY_PREFIX}{key}" for key in diplomacy_entity_keys(title))
    expires_at_ms = int(item["published_at_ms"]) + _ACTIVE_WINDOW_MS
    active = bool(item["source_enabled"]) and expires_at_ms > now_ms
    feature = {
        "item_id": str(item["item_id"]),
        "normalized_title": normalized,
        "lexical_tokens": lexical,
        "candidate_tokens": sorted(set(lexical) | set(entity)),
        "published_at_ms": int(item["published_at_ms"]),
        "expires_at_ms": expires_at_ms,
        "active": active,
    }
    feature["feature_fingerprint"] = _stable_hash(feature)
    return feature


def plan_news_edge_pairs(context: dict[str, Any]) -> dict[str, Any]:
    """Pure deterministic candidate planning; no pair block exceeds 4,096."""

    target_id = str(context["target_item_id"])
    target_feature = dict(context["target_feature"])
    rows_by_id = {str(row["item_id"]): dict(row) for row in context["rows"]}
    if target_id not in rows_by_id:
        raise RuntimeError("news_target_missing_from_closure")
    features: dict[str, dict[str, Any]] = {}
    for item_id, row in rows_by_id.items():
        features[item_id] = {
            "item_id": item_id,
            "normalized_title": str(row.get("normalized_title") or ""),
            "candidate_tokens": [str(token) for token in (row.get("candidate_tokens") or [])],
            "expires_at_ms": int(row.get("expires_at_ms") or 0),
            "active": bool(row.get("feature_active")),
        }
    features[target_id] = target_feature
    active_ids = {
        item_id
        for item_id, feature in features.items()
        if bool(feature["active"])
        and int(feature["expires_at_ms"]) > int(context["now_ms"])
        and bool(rows_by_id[item_id]["source_enabled"])
    }
    lexical_by_id = {
        item_id: {token for token in feature["candidate_tokens"] if not str(token).startswith(_ENTITY_PREFIX)}
        for item_id, feature in features.items()
    }
    counts = {str(key): int(value) for key, value in context["token_counts"].items()}

    def is_candidate(left: str, right: str) -> bool:
        if left not in active_ids or right not in active_ids:
            return False
        if features[left]["normalized_title"] == features[right]["normalized_title"]:
            return bool(features[left]["normalized_title"])
        return any(counts.get(token, 0) <= MAX_CANDIDATE_BUCKET for token in lexical_by_id[left] & lexical_by_id[right])

    affected: set[tuple[str, str]] = {
        _ordered_pair(str(edge["left_item_id"]), str(edge["right_item_id"]))
        for edge in context["existing_edges"]
        if target_id in {str(edge["left_item_id"]), str(edge["right_item_id"])}
    }
    recompute: set[tuple[str, str]] = {
        _ordered_pair(target_id, other) for other in active_ids if other != target_id and is_candidate(target_id, other)
    }
    crossing_tokens = {str(token) for token in context["crossing_tokens"]}
    for token in crossing_tokens:
        members = sorted(item_id for item_id in active_ids if token in lexical_by_id[item_id])
        for left, right in combinations(members, 2):
            pair = _ordered_pair(left, right)
            affected.add(pair)
            if is_candidate(left, right):
                recompute.add(pair)

    pair_rows = [
        {
            "left_item_id": left,
            "right_item_id": right,
            "left_title": str(rows_by_id[left]["title"]),
            "right_title": str(rows_by_id[right]["title"]),
            "expires_at_ms": min(
                int(features[left]["expires_at_ms"]),
                int(features[right]["expires_at_ms"]),
            ),
        }
        for left, right in sorted(recompute)
    ]
    return {
        "affected_pairs": [list(pair) for pair in sorted(affected | recompute)],
        "recompute_pairs": pair_rows,
        "pair_blocks": (len(pair_rows) + _PAIR_BLOCK_CAP - 1) // _PAIR_BLOCK_CAP,
    }


def compute_news_edge_block(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(pairs) > _PAIR_BLOCK_CAP:
        raise NewsShardOversized("news_candidate_pair_block_overflow")
    edges: list[dict[str, Any]] = []
    for pair in pairs:
        similarity = story_similarity(
            str(pair["left_title"]),
            str(pair["right_title"]),
        )
        if similarity >= STORY_SIMILARITY_THRESHOLD:
            edges.append(
                {
                    "left_item_id": str(pair["left_item_id"]),
                    "right_item_id": str(pair["right_item_id"]),
                    "similarity": float(similarity),
                    "expires_at_ms": int(pair["expires_at_ms"]),
                }
            )
    return edges


def compute_news_component_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Pure Story closure calculation over the bounded affected graph."""

    now_ms = int(payload["now_ms"])
    scoring_now_ms = _scoring_epoch(now_ms)
    target_id = str(payload["target_item_id"])
    target_feature = dict(payload["target_feature"])
    rows_by_id = {str(row["item_id"]): dict(row) for row in payload["rows"]}
    features: dict[str, dict[str, Any]] = {}
    for item_id, row in rows_by_id.items():
        features[item_id] = {
            "normalized_title": str(row.get("normalized_title") or ""),
            "candidate_tokens": [str(token) for token in (row.get("candidate_tokens") or [])],
            "expires_at_ms": int(row.get("expires_at_ms") or 0),
            "active": bool(row.get("feature_active")),
        }
    features[target_id] = target_feature
    active_ids = sorted(
        item_id
        for item_id, feature in features.items()
        if bool(feature["active"])
        and int(feature["expires_at_ms"]) > now_ms
        and bool(rows_by_id[item_id]["source_enabled"])
    )
    parent = {item_id: item_id for item_id in active_ids}

    def find(item_id: str) -> str:
        while parent[item_id] != item_id:
            parent[item_id] = parent[parent[item_id]]
            item_id = parent[item_id]
        return item_id

    def union(left: str, right: str) -> None:
        if left not in parent or right not in parent:
            return
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for edge in payload["final_edges"]:
        union(str(edge["left_item_id"]), str(edge["right_item_id"]))
    base_components: dict[str, list[str]] = {}
    for item_id in active_ids:
        base_components.setdefault(find(item_id), []).append(item_id)
    components = [sorted(members) for _, members in sorted(base_components.items())]

    previous = {str(row["item_id"]): str(row["story_id"]) for row in payload["previous_memberships"]}
    aliases = {str(row["alias_key"]): str(row["story_id"]) for row in payload["aliases"]}
    component_candidates: list[Counter[str]] = []
    component_parent = list(range(len(components)))

    def component_find(index: int) -> int:
        while component_parent[index] != index:
            component_parent[index] = component_parent[component_parent[index]]
            index = component_parent[index]
        return index

    first_component_by_story: dict[str, int] = {}
    for index, members in enumerate(components):
        candidates: Counter[str] = Counter()
        for item_id in members:
            if item_id in previous:
                candidates[previous[item_id]] += 1
            alias_story = aliases.get(_alias_key(features[item_id]["normalized_title"]))
            if alias_story:
                candidates[alias_story] += 1
        component_candidates.append(candidates)
        for story_id in candidates:
            prior = first_component_by_story.setdefault(story_id, index)
            left = component_find(prior)
            right = component_find(index)
            if left != right:
                component_parent[max(left, right)] = min(left, right)

    merged: dict[int, list[str]] = {}
    merged_candidates: dict[int, Counter[str]] = {}
    for index, members in enumerate(components):
        root = component_find(index)
        merged.setdefault(root, []).extend(members)
        merged_candidates.setdefault(root, Counter()).update(component_candidates[index])

    affected_ids = _affected_component_item_ids(
        target_id=target_id,
        rows_by_id=rows_by_id,
        features=features,
        previous=previous,
        aliases=aliases,
        merged=merged,
        merged_candidates=merged_candidates,
        existing_edges=[dict(edge) for edge in payload["existing_edges"]],
        final_edges=[dict(edge) for edge in payload["final_edges"]],
    )
    affected_roots = {
        root
        for root, member_ids in merged.items()
        if affected_ids.intersection(member_ids)
    }

    entity_sources: dict[str, dict[str, int]] = {}
    entity_members: dict[str, dict[str, int]] = {}
    for row in payload["entity_rows"]:
        item_id = str(row["item_id"])
        if item_id == target_id:
            continue
        token = str(row["token"])
        entity_members.setdefault(token, {})[str(row["source_id"])] = int(row["tier"])
    if bool(target_feature["active"]):
        target_source = str(rows_by_id[target_id]["source_id"])
        target_tier = int(rows_by_id[target_id]["tier"])
        for token in target_feature["candidate_tokens"]:
            if str(token).startswith(_ENTITY_PREFIX):
                entity_members.setdefault(str(token), {})[target_source] = target_tier
    for token, sources in entity_members.items():
        entity_sources[token] = {
            "source_count": len(sources),
            "tier12_source_count": sum(tier <= 2 for tier in sources.values()),
        }

    item_updates: list[dict[str, Any]] = []
    stories: list[dict[str, Any]] = []
    memberships: list[dict[str, str]] = []
    alias_updates: list[dict[str, str]] = []
    for root, member_ids in sorted(merged.items(), key=lambda pair: min(pair[1])):
        if root not in affected_roots:
            continue
        component_rows = [rows_by_id[item_id] for item_id in sorted(member_ids)]
        source_count = len({str(member["source_id"]) for member in component_rows})
        entity_source_count = 0
        tier12_entity_source_count = 0
        for item_id in member_ids:
            for token in features[item_id]["candidate_tokens"]:
                signal = entity_sources.get(str(token))
                if signal is not None:
                    entity_source_count = max(entity_source_count, signal["source_count"])
                    tier12_entity_source_count = max(
                        tier12_entity_source_count,
                        signal["tier12_source_count"],
                    )
        scored: list[dict[str, Any]] = []
        for raw_member in component_rows:
            member = dict(raw_member)
            classified = classify_by_keyword(
                str(member["title"]),
                now_ms=scoring_now_ms,
            )
            level = promote_diplomacy_severity(
                classified.level,
                title=str(member["title"]),
                tier12_origin_count=tier12_entity_source_count,
            )
            factors = importance_factors(
                level=level,
                tier=int(member["tier"]),
                corroboration_count=source_count,
                published_at_ms=int(member["published_at_ms"]),
                now_ms=scoring_now_ms,
                title=str(member["title"]),
                entity_corroboration_count=entity_source_count,
            )
            member.update(
                {
                    "level": level,
                    "category": classified.category,
                    "classification_source": classified.source,
                    "classification_confidence": classified.confidence,
                    "importance_score": int(factors["total"]),
                    "importance_factors": factors,
                }
            )
            scored.append(member)
            item_updates.append(
                {
                    key: member[key]
                    for key in (
                        "item_id",
                        "level",
                        "category",
                        "classification_source",
                        "classification_confidence",
                        "importance_score",
                        "importance_factors",
                    )
                }
            )

        earliest = min(
            scored,
            key=lambda member: (
                int(member["published_at_ms"]),
                str(member["normalized_title"]),
                str(member["item_id"]),
            ),
        )
        canonical_key = _alias_key(str(earliest["normalized_title"]))
        candidates = merged_candidates[root]
        if candidates:
            max_hits = max(candidates.values())
            story_id = min(story for story, hits in candidates.items() if hits == max_hits)
        else:
            story_id = deterministic_id("story", canonical_key)
        representative = min(
            scored,
            key=lambda member: (
                int(member["tier"]),
                -int(member["published_at_ms"]),
                str(member["normalized_title"]),
                str(member["item_id"]),
            ),
        )
        scoring_item = min(
            scored,
            key=lambda member: (
                -int(member["importance_score"]),
                int(member["tier"]),
                -int(member["published_at_ms"]),
                str(member["source_id"]),
                str(member["item_id"]),
            ),
        )
        level = cast(
            ThreatLevel,
            max(
                (str(member["level"]) for member in scored),
                key=lambda value: (
                    SEVERITY_VALUES[cast(ThreatLevel, value)],
                    value,
                ),
            ),
        )
        category = cast(
            EventCategory,
            _mode([str(member["category"]) for member in scored], _CATEGORY_ORDER),
        )
        fingerprint_payload = {
            "identity_version": STORY_IDENTITY_VERSION,
            "canonical_key": canonical_key,
            "representative_item_id": representative["item_id"],
            "scoring_item_id": scoring_item["item_id"],
            "members": sorted(str(member["item_id"]) for member in scored),
            "level": level,
            "category": category,
            "importance_score": scoring_item["importance_score"],
            "importance_factors": scoring_item["importance_factors"],
            "source_count": source_count,
            "first": min(int(member["published_at_ms"]) for member in scored),
            "last": max(int(member["published_at_ms"]) for member in scored),
        }
        stories.append(
            {
                "story_id": story_id,
                "canonical_key": canonical_key,
                "canonical_title": str(earliest["title"]),
                "representative_item_id": representative["item_id"],
                "representative_source_id": representative["source_id"],
                "representative_title": representative["title"],
                "representative_url": representative["canonical_url"],
                "representative_description": representative["description"],
                "scoring_item_id": scoring_item["item_id"],
                "level": level,
                "category": category,
                "importance_score": scoring_item["importance_score"],
                "importance_factors": scoring_item["importance_factors"],
                "item_count": len(scored),
                "source_count": source_count,
                "first_published_at_ms": fingerprint_payload["first"],
                "last_published_at_ms": fingerprint_payload["last"],
                "state_fingerprint": _stable_hash(fingerprint_payload),
            }
        )
        for member in scored:
            item_id = str(member["item_id"])
            memberships.append({"item_id": item_id, "story_id": story_id})
            alias_updates.append(
                {
                    "alias_key": _alias_key(features[item_id]["normalized_title"]),
                    "story_id": story_id,
                }
            )

    alias_rows = [
        {"alias_key": alias_key, "story_id": story_id}
        for alias_key, story_id in sorted({(str(row["alias_key"]), str(row["story_id"])) for row in alias_updates})
    ]
    output: dict[str, Any] = {
        "closure_item_ids": sorted(affected_ids),
        "old_story_ids": sorted(
            {
                previous[item_id]
                for item_id in affected_ids
                if item_id in previous
            }
        ),
        "item_updates": sorted(item_updates, key=lambda row: str(row["item_id"])),
        "stories": sorted(stories, key=lambda row: str(row["story_id"])),
        "memberships": sorted(memberships, key=lambda row: str(row["item_id"])),
        "aliases": alias_rows,
    }
    _require_bounded_output(output)
    return output


def _affected_component_item_ids(
    *,
    target_id: str,
    rows_by_id: dict[str, dict[str, Any]],
    features: dict[str, dict[str, Any]],
    previous: dict[str, str],
    aliases: dict[str, str],
    merged: dict[int, list[str]],
    merged_candidates: dict[int, Counter[str]],
    existing_edges: list[dict[str, Any]],
    final_edges: list[dict[str, Any]],
) -> set[str]:
    """Return the old/new Story identity closure touched by one identity change."""

    def pair(edge: dict[str, Any]) -> tuple[str, str]:
        return _ordered_pair(
            str(edge["left_item_id"]),
            str(edge["right_item_id"]),
        )

    existing_pairs = {pair(edge) for edge in existing_edges}
    final_pairs = {pair(edge) for edge in final_edges}
    changed_pairs = existing_pairs.symmetric_difference(final_pairs)
    seeds = {
        target_id,
        *(
            item_id
            for changed_pair in changed_pairs
            for item_id in changed_pair
        ),
    }

    adjacency: dict[str, set[str]] = {}
    for left, right in existing_pairs | final_pairs:
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
    topology_ids: set[str] = set()
    pending = sorted(item_id for item_id in seeds if item_id in rows_by_id)
    while pending:
        item_id = pending.pop()
        if item_id in topology_ids:
            continue
        topology_ids.add(item_id)
        pending.extend(
            neighbor
            for neighbor in sorted(adjacency.get(item_id, ()))
            if neighbor not in topology_ids
        )

    story_ids = {
        previous[item_id]
        for item_id in topology_ids
        if item_id in previous
    }
    story_ids.update(
        aliases[alias_key]
        for item_id in topology_ids
        if item_id in features
        if (alias_key := _alias_key(features[item_id]["normalized_title"])) in aliases
    )

    affected_ids = set(topology_ids)
    remaining = set(merged)
    while True:
        selected = {
            root
            for root in remaining
            if affected_ids.intersection(merged[root])
            or story_ids.intersection(merged_candidates[root])
        }
        if not selected:
            break
        for root in selected:
            affected_ids.update(merged[root])
            story_ids.update(merged_candidates[root])
        remaining.difference_update(selected)
    return affected_ids


def merge_final_edges(
    *,
    existing_edges: list[dict[str, Any]],
    affected_pairs: list[list[str]],
    new_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    affected = {tuple(pair) for pair in affected_pairs}
    merged = {
        (str(edge["left_item_id"]), str(edge["right_item_id"])): dict(edge)
        for edge in existing_edges
        if (str(edge["left_item_id"]), str(edge["right_item_id"])) not in affected
    }
    for edge in new_edges:
        merged[(str(edge["left_item_id"]), str(edge["right_item_id"]))] = dict(edge)
    return [merged[key] for key in sorted(merged)]


def _token_counts(conn: Any, *, tokens: list[str], now_ms: int) -> dict[str, int]:
    if not tokens:
        return {}
    return {
        str(row["token"]): int(row["active_count"])
        for row in conn.execute(
            """
            SELECT token, count(*) AS active_count
              FROM news_identity_features feature
              JOIN news_items item ON item.item_id = feature.item_id
              JOIN news_sources source ON source.source_id = item.source_id
              CROSS JOIN LATERAL unnest(feature.candidate_tokens) token
             WHERE feature.active
               AND feature.expires_at_ms > %(now_ms)s
               AND source.enabled
               AND token = ANY(%(tokens)s)
             GROUP BY token
             ORDER BY token
            """,
            {"now_ms": int(now_ms), "tokens": tokens},
        ).fetchall()
    }


def _edge_closure(
    conn: Any,
    *,
    seed_ids: list[str],
    now_ms: int,
    row_cap: int,
) -> list[str]:
    rows = conn.execute(
        """
        WITH RECURSIVE closure(item_id) AS (
          SELECT unnest(%(seed_ids)s::text[])
          UNION
          SELECT CASE
                   WHEN edge.left_item_id = closure.item_id
                   THEN edge.right_item_id
                   ELSE edge.left_item_id
                 END
            FROM closure
            JOIN news_similarity_edges edge
              ON edge.left_item_id = closure.item_id
              OR edge.right_item_id = closure.item_id
           WHERE edge.expires_at_ms > %(now_ms)s
        )
        SELECT item_id
          FROM closure
         ORDER BY item_id
         LIMIT %(limit)s
        """,
        {
            "seed_ids": seed_ids,
            "now_ms": int(now_ms),
            "limit": int(row_cap) + 1,
        },
    ).fetchall()
    if len(rows) > row_cap:
        raise NewsShardOversized("news_component_rows_overflow")
    return [str(row["item_id"]) for row in rows]


def _context_fingerprint(rows: list[dict[str, Any]]) -> str:
    return _stable_hash(
        [
            {
                "item_id": row["item_id"],
                "content_fingerprint": row["content_fingerprint"],
                "source_enabled": row["source_enabled"],
                "feature_fingerprint": row.get("feature_fingerprint"),
                "candidate_tokens": row.get("candidate_tokens") or [],
                "feature_active": row.get("feature_active"),
                "expires_at_ms": row.get("expires_at_ms"),
            }
            for row in sorted(rows, key=lambda value: str(value["item_id"]))
        ]
    )


def _upsert_story(conn: Any, *, story: dict[str, Any], now_ms: int) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO news_stories (
              story_id, canonical_key, canonical_title,
              representative_item_id, representative_source_id,
              representative_title, representative_url,
              representative_description, scoring_item_id,
              level, category, importance_score, importance_factors,
              item_count, source_count, first_published_at_ms,
              last_published_at_ms, active, state_fingerprint,
              created_at_ms, updated_at_ms
            )
            VALUES (
              %(story_id)s, %(canonical_key)s, %(canonical_title)s,
              %(representative_item_id)s, %(representative_source_id)s,
              %(representative_title)s, %(representative_url)s,
              %(representative_description)s, %(scoring_item_id)s,
              %(level)s, %(category)s, %(importance_score)s,
              %(importance_factors)s, %(item_count)s, %(source_count)s,
              %(first_published_at_ms)s, %(last_published_at_ms)s,
              true, %(state_fingerprint)s, %(now_ms)s, %(now_ms)s
            )
            ON CONFLICT (story_id) DO UPDATE SET
              canonical_key = EXCLUDED.canonical_key,
              canonical_title = EXCLUDED.canonical_title,
              representative_item_id = EXCLUDED.representative_item_id,
              representative_source_id = EXCLUDED.representative_source_id,
              representative_title = EXCLUDED.representative_title,
              representative_url = EXCLUDED.representative_url,
              representative_description = EXCLUDED.representative_description,
              scoring_item_id = EXCLUDED.scoring_item_id,
              level = EXCLUDED.level,
              category = EXCLUDED.category,
              importance_score = EXCLUDED.importance_score,
              importance_factors = EXCLUDED.importance_factors,
              item_count = EXCLUDED.item_count,
              source_count = EXCLUDED.source_count,
              first_published_at_ms = EXCLUDED.first_published_at_ms,
              last_published_at_ms = EXCLUDED.last_published_at_ms,
              active = true,
              state_fingerprint = EXCLUDED.state_fingerprint,
              updated_at_ms = EXCLUDED.updated_at_ms
            WHERE news_stories.state_fingerprint IS DISTINCT FROM
                  EXCLUDED.state_fingerprint
               OR NOT news_stories.active
            """,
            {
                **story,
                "importance_factors": Jsonb(story["importance_factors"]),
                "now_ms": int(now_ms),
            },
        ).rowcount
        or 0
    )


def _parse_bucket(bucket_id: str) -> tuple[str, str]:
    value = str(bucket_id)
    for kind in ("identity", "score"):
        prefix = f"{kind}:"
        if value.startswith(prefix) and len(value) > len(prefix):
            return kind, value[len(prefix) :]
    raise ValueError("news_projection_bucket_kind_invalid")


def _ordered_pair(left: str, right: str) -> tuple[str, str]:
    if left == right:
        raise ValueError("news_similarity_self_pair_invalid")
    return (left, right) if left < right else (right, left)


def _alias_key(normalized_title: str) -> str:
    return hashlib.sha256(normalized_title.encode()).hexdigest()


def _mode(values: list[str], order: tuple[str, ...]) -> str:
    counts = Counter(values)
    highest = max(counts.values())
    index = {value: position for position, value in enumerate(order)}
    return min(
        (value for value, count in counts.items() if count == highest),
        key=lambda value: (index.get(value, len(index)), value),
    )


def _stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _scoring_epoch(now_ms: int) -> int:
    return int(now_ms) // _SCORING_EPOCH_MS * _SCORING_EPOCH_MS


def _require_bounded_input(payload: object) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    if len(encoded) > _INPUT_BYTE_CAP:
        raise NewsShardOversized("news_shard_input_bytes_overflow")


def _require_bounded_output(payload: object) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    if len(encoded) > _OUTPUT_BYTE_CAP:
        raise NewsShardOversized("news_shard_output_bytes_overflow")


__all__ = [
    "NEWS_PROJECTION_VERSION",
    "NewsProjectionClaim",
    "NewsProjectionService",
    "NewsShardOversized",
    "compute_news_component_projection",
    "compute_news_edge_block",
    "compute_news_identity_feature",
    "merge_final_edges",
    "plan_news_edge_pairs",
    "rebuild_all_news_for_maintenance",
]
