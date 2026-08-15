from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from psycopg.types.json import Jsonb

from . import brief_store, query_specs
from .exact_atom_identity import (
    EXACT_ATOM_IDENTITY_VERSION,
    NEWS_PUSH_ADMISSION_POLICY_VERSION,
    describe_exact_atom,
)
from .models import (
    CLASSIFIER_VERSION,
    IMPORTANCE_VERSION,
    NEWS_PUSH_PAYLOAD_SCHEMA_VERSION,
    STORY_IDENTITY_VERSION,
    NewsBriefSynthesisResult,
    NewsFeedFetch,
    NewsSourceDefinition,
)
from .opennews import OpenNewsEvent
from .sources import OPENNEWS_SOURCE_ID
from .title_presentation import (
    DEEPL_DEADLINE_SECONDS,
    DEEPSEEK_DEADLINE_SECONDS,
    TITLE_PRESENTATION_POLICY_VERSION,
)

_ACTIVE_WINDOW_MS = 96 * 60 * 60 * 1000
_STORY_ACTIVE_WINDOW_MS = 12 * 60 * 60 * 1000
_NEWS_PIPELINE_LOCK_KEY = 727_301_984
_MAX_STRATEGY_PROVENANCE = 32
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
    }
)


def deterministic_id(namespace: str, *parts: object) -> str:
    payload = "\x1f".join([namespace, *(str(part) for part in parts)])
    return f"{namespace}_{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _title_fingerprint(title: str) -> str:
    return hashlib.sha256(title.encode("utf-8")).hexdigest()


def _canonical_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
        )
    )
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, ""))


def _news_item_content_fingerprint(
    *,
    title: str,
    description: str,
    canonical_url: str | None,
    reporting_origin: str,
    published_at_ms: int,
    language: str,
) -> str:
    return _sha256_json(
        {
            "title": title,
            "description": description,
            "canonical_url": canonical_url,
            "reporting_origin": reporting_origin,
            "published_at_ms": published_at_ms,
            "language": language,
        }
    )


def _rss_source_item_key(
    *,
    guid: str | None,
    canonical_url: str | None,
    title: str,
    published_at_ms: int,
) -> str:
    normalized_guid = str(guid or "").strip()
    if normalized_guid:
        return f"guid:{normalized_guid}"
    if canonical_url:
        return f"url:{canonical_url}"
    return "entry:" + _sha256_json(
        {
            "title": title,
            "published_at_ms": int(published_at_ms),
        }
    )


def _numeric_provider_score(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and isfinite(float(value))


def _strategy_provenance(value: object) -> list[dict[str, str]]:
    """Return one deterministic, bounded provenance row per opaque Strategy ID."""

    if not isinstance(value, list):
        return []
    by_id: dict[str, dict[str, str]] = {}
    for candidate in value:
        if not isinstance(candidate, Mapping):
            continue
        strategy_id = str(candidate.get("id") or "").strip()
        if not strategy_id or "\x00" in strategy_id or len(strategy_id) > 128:
            continue
        normalized = {"id": strategy_id}
        for key, limit in (("name", 128), ("source_type", 32), ("engine_type", 32)):
            field = str(candidate.get(key) or "").strip()
            if field and "\x00" not in field:
                normalized[key] = field[:limit]
        current = by_id.get(strategy_id)
        if current is None or _sha256_json(normalized) > _sha256_json(current):
            by_id[strategy_id] = normalized
    return [by_id[strategy_id] for strategy_id in sorted(by_id)[:_MAX_STRATEGY_PROVENANCE]]


def _merged_strategy_provenance(*values: object) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for value in values:
        candidates.extend(_strategy_provenance(value))
    return _strategy_provenance(candidates)


def _stable_payload_order(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _provider_payload_order(value: Mapping[str, Any]) -> tuple[float, bool, str, str]:
    metadata = value.get("provider_metadata")
    provider_metadata = metadata if isinstance(metadata, Mapping) else {}
    raw_score = provider_metadata.get("score")
    score = float(cast(int | float, raw_score)) if _numeric_provider_score(raw_score) else -1.0
    coins = provider_metadata.get("coins")
    bounded_coins = coins if isinstance(coins, list) else []
    return (
        score,
        bool(bounded_coins),
        json.dumps(bounded_coins, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        _stable_payload_order(value),
    )


def _news_item_push_source_payload(
    *,
    item_id: str,
    provider_event_id: str,
    provider_metadata: Mapping[str, Any],
    live_observed_at_ms: int,
    original_title: str,
    reporting_origin: str,
    provider_published_at_ms: int,
    source_url: str | None,
) -> dict[str, Any]:
    strategy_labels: list[str] = []
    seen_strategies: set[str] = set()
    for strategy in _strategy_provenance(provider_metadata.get("strategies")):
        strategy_id = str(strategy["id"]).strip()
        strategy_name = str(strategy.get("name") or "").strip()
        label = f"{strategy_id} {strategy_name}".strip()
        identity = label.casefold()
        if label and identity not in seen_strategies:
            seen_strategies.add(identity)
            strategy_labels.append(label)

    assets: list[dict[str, str]] = []
    seen_assets: set[tuple[str, str]] = set()
    coins = provider_metadata.get("coins")
    if isinstance(coins, list):
        for raw_asset in coins[:32]:
            if not isinstance(raw_asset, Mapping):
                continue
            symbol = str(raw_asset.get("symbol") or "").strip()[:32]
            market_type = str(raw_asset.get("market_type") or "").strip()[:32]
            asset_identity = (symbol.casefold(), market_type.casefold())
            if not symbol or not market_type or asset_identity in seen_assets:
                continue
            seen_assets.add(asset_identity)
            assets.append({"symbol": symbol, "market_type": market_type})

    payload: dict[str, Any] = {
        "schema_version": NEWS_PUSH_PAYLOAD_SCHEMA_VERSION,
        "item_id": str(item_id),
        "provider_event_id": str(provider_event_id),
        "live_observed_at_ms": int(live_observed_at_ms),
        "original_title": str(original_title),
        "reporting_origin": str(reporting_origin),
        "provider_published_at_ms": int(provider_published_at_ms),
        "strategy_labels": strategy_labels,
        "assets": assets,
    }
    if source_url:
        payload["source_url"] = str(source_url)
    score = provider_metadata.get("score")
    if _numeric_provider_score(score) and 0 <= float(cast(int | float, score)) <= 100:
        payload["score"] = score
    for key in ("signal", "grade"):
        value = str(provider_metadata.get(key) or "").strip()
        if value:
            payload[key] = value[:32]
    return payload


def _cursor_encode(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_decode(value: str) -> dict[str, Any]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("news_feed_cursor_invalid") from exc
    if not isinstance(decoded, dict):
        raise ValueError("news_feed_cursor_invalid")
    return decoded


def _percentile_cont_95_ms(values: Sequence[int]) -> int | None:
    """Match PostgreSQL percentile_cont(0.95), rounded up to milliseconds."""

    return _percentile_cont_ms(values, 0.95)


def _percentile_cont_ms(values: Sequence[int], percentile: float) -> int | None:
    """Return a deterministic continuous percentile rounded up to milliseconds."""

    if not values:
        return None
    ordered = sorted(int(value) for value in values)
    numerator_percent = round(float(percentile) * 100)
    scaled_position = (len(ordered) - 1) * numerator_percent
    lower, remainder = divmod(scaled_position, 100)
    upper = min(lower + 1, len(ordered) - 1)
    numerator = ordered[lower] * (100 - remainder) + ordered[upper] * remainder
    return (numerator + 99) // 100


def _incomplete_title_presentation_sample() -> dict[str, Any]:
    return {
        "total": 0,
        "attempted": 0,
        "translated": 0,
        "not_needed": 0,
        "fallback": 0,
        "provider_counts": {},
        "latency_p95_ms": None,
        "fallback_counts": {},
        "sample_complete": False,
    }


def _incomplete_delivery_sample() -> dict[str, Any]:
    return {
        "completed": 0,
        "sent": 0,
        "terminal": 0,
        "latency_p95_ms": None,
        "slo_met": None,
        "sample_complete": False,
    }


class NewsRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @staticmethod
    def stable_json_hash(value: object) -> str:
        return _sha256_json(value)

    def lock_story_inputs(self) -> None:
        """Fence fact mutation against final Story input validation/publication."""

        self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (_NEWS_PIPELINE_LOCK_KEY,))

    # Source inventory and acquisition -----------------------------------------

    def sync_sources(self, sources: Sequence[NewsSourceDefinition], *, now_ms: int) -> int:
        """Reconcile the complete code-owned physical source inventory."""

        self.lock_story_inputs()
        source_ids = [source.source_id for source in sources]
        writes = 0
        for source in sources:
            is_rss = source.source_kind == "rss"
            if not is_rss:
                self.conn.execute(
                    """
                    INSERT INTO news_opennews_incidents (
                      source_id, cause_class, opened_at_ms, planned,
                      recovery_status, recovery_from_at_ms, last_error_code,
                      created_at_ms, updated_at_ms
                    )
                    SELECT source_id, 'process_outage', updated_at_ms, false,
                           'pending', updated_at_ms, 'opennews_process_outage',
                           %s, %s
                      FROM news_sources
                     WHERE source_id = %s AND live_connected
                    ON CONFLICT DO NOTHING
                    """,
                    (int(now_ms), int(now_ms), source.source_id),
                )
            cursor = self.conn.execute(
                """
                INSERT INTO news_sources (
                  source_id, name, tier, lang, enabled, source_kind,
                  feed_url, refresh_interval_seconds, next_fetch_at_ms,
                  created_at_ms, updated_at_ms
                )
                VALUES (
                  %(source_id)s, %(name)s, %(tier)s, %(lang)s,
                  %(enabled)s, %(source_kind)s, %(feed_url)s,
                  %(refresh_interval_seconds)s, %(next_fetch_at_ms)s,
                  %(now_ms)s, %(now_ms)s
                )
                ON CONFLICT (source_id) DO UPDATE SET
                  name = EXCLUDED.name,
                  tier = EXCLUDED.tier,
                  lang = EXCLUDED.lang,
                  enabled = EXCLUDED.enabled,
                  source_kind = EXCLUDED.source_kind,
                  feed_url = EXCLUDED.feed_url,
                  refresh_interval_seconds = EXCLUDED.refresh_interval_seconds,
                  next_fetch_at_ms = CASE
                    WHEN news_sources.feed_url IS DISTINCT FROM EXCLUDED.feed_url
                      OR news_sources.enabled IS DISTINCT FROM EXCLUDED.enabled
                      THEN EXCLUDED.next_fetch_at_ms
                    ELSE news_sources.next_fetch_at_ms
                  END,
                  etag = CASE
                    WHEN news_sources.feed_url IS DISTINCT FROM EXCLUDED.feed_url
                      THEN NULL
                    ELSE news_sources.etag
                  END,
                  last_modified = CASE
                    WHEN news_sources.feed_url IS DISTINCT FROM EXCLUDED.feed_url
                      THEN NULL
                    ELSE news_sources.last_modified
                  END,
                  claim_token = CASE
                    WHEN news_sources.feed_url IS DISTINCT FROM EXCLUDED.feed_url
                      THEN NULL
                    ELSE news_sources.claim_token
                  END,
                  claim_lease_expires_at_ms = CASE
                    WHEN news_sources.feed_url IS DISTINCT FROM EXCLUDED.feed_url
                      THEN NULL
                    ELSE news_sources.claim_lease_expires_at_ms
                  END,
                  live_connected = CASE
                    WHEN EXCLUDED.source_kind = 'opennews'
                     AND news_sources.live_connected
                      THEN false
                    ELSE news_sources.live_connected
                  END,
                  last_disconnected_at_ms = CASE
                    WHEN EXCLUDED.source_kind = 'opennews'
                     AND news_sources.live_connected
                      THEN EXCLUDED.updated_at_ms
                    ELSE news_sources.last_disconnected_at_ms
                  END,
                  last_outcome = CASE
                    WHEN EXCLUDED.source_kind = 'opennews'
                     AND news_sources.live_connected
                      THEN 'strategy_process_outage'
                    ELSE news_sources.last_outcome
                  END,
                  last_error = CASE
                    WHEN EXCLUDED.source_kind = 'opennews'
                     AND news_sources.live_connected
                      THEN 'opennews_process_outage'
                    ELSE news_sources.last_error
                  END,
                  consecutive_failures = CASE
                    WHEN EXCLUDED.source_kind = 'opennews'
                     AND news_sources.live_connected
                      THEN news_sources.consecutive_failures + 1
                    ELSE news_sources.consecutive_failures
                  END,
                  updated_at_ms = EXCLUDED.updated_at_ms
                WHERE (
                  news_sources.name,
                  news_sources.tier,
                  news_sources.lang,
                  news_sources.enabled,
                  news_sources.source_kind,
                  news_sources.feed_url,
                  news_sources.refresh_interval_seconds
                ) IS DISTINCT FROM (
                  EXCLUDED.name,
                  EXCLUDED.tier,
                  EXCLUDED.lang,
                  EXCLUDED.enabled,
                  EXCLUDED.source_kind,
                  EXCLUDED.feed_url,
                  EXCLUDED.refresh_interval_seconds
                ) OR (
                  EXCLUDED.source_kind = 'opennews'
                  AND news_sources.live_connected
                )
                """,
                {
                    "source_id": source.source_id,
                    "name": source.name,
                    "tier": source.tier,
                    "lang": source.lang,
                    "enabled": source.enabled,
                    "source_kind": source.source_kind,
                    "feed_url": source.feed_url if is_rss else None,
                    "refresh_interval_seconds": source.refresh_interval_seconds if is_rss else None,
                    "next_fetch_at_ms": int(now_ms) if is_rss else None,
                    "now_ms": int(now_ms),
                },
            )
            writes += int(cursor.rowcount or 0)
        if source_ids:
            cursor = self.conn.execute(
                """
                UPDATE news_sources
                   SET enabled = false,
                       claim_token = NULL,
                       claim_lease_expires_at_ms = NULL,
                       updated_at_ms = %s
                 WHERE enabled AND NOT (source_id = ANY(%s))
                """,
                (int(now_ms), source_ids),
            )
        else:
            cursor = self.conn.execute(
                """
                UPDATE news_sources
                   SET enabled = false,
                       claim_token = NULL,
                       claim_lease_expires_at_ms = NULL,
                       updated_at_ms = %s
                 WHERE enabled
                """,
                (int(now_ms),),
            )
        return writes + int(cursor.rowcount or 0)

    def claim_due_rss_source(
        self,
        *,
        now_ms: int,
        claim_token: str,
        lease_expires_at_ms: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            WITH candidate AS (
              SELECT source_id
                FROM news_sources
               WHERE enabled
                 AND source_kind = 'rss'
                 AND next_fetch_at_ms <= %(now_ms)s
                 AND (
                   claim_token IS NULL
                   OR claim_lease_expires_at_ms <= %(now_ms)s
                 )
               ORDER BY next_fetch_at_ms, source_id
               LIMIT 1
               FOR UPDATE SKIP LOCKED
            )
            UPDATE news_sources AS source
               SET claim_token = %(claim_token)s,
                   claim_lease_expires_at_ms = %(lease_expires_at_ms)s,
                   last_fetch_started_at_ms = %(now_ms)s,
                   updated_at_ms = %(now_ms)s
              FROM candidate
             WHERE source.source_id = candidate.source_id
            RETURNING source.source_id, source.name, source.tier, source.lang,
                      source.feed_url, source.refresh_interval_seconds,
                      source.etag, source.last_modified,
                      source.claim_token::text AS claim_token,
                      source.claim_lease_expires_at_ms
            """,
            {
                "now_ms": int(now_ms),
                "claim_token": claim_token,
                "lease_expires_at_ms": int(lease_expires_at_ms),
            },
        ).fetchone()
        return dict(row) if row is not None else None

    def record_rss_fetch(
        self,
        *,
        source: NewsSourceDefinition,
        claim_token: str,
        fetch: NewsFeedFetch,
        finished_at_ms: int,
    ) -> dict[str, int] | None:
        """Publish one successful current feed snapshot behind its claim fence."""

        if source.source_kind != "rss":
            raise ValueError("rss_source_required")
        self.lock_story_inputs()
        claimed = self.conn.execute(
            """
            SELECT refresh_interval_seconds
              FROM news_sources
             WHERE source_id = %s
               AND source_kind = 'rss'
               AND claim_token = %s
               AND claim_lease_expires_at_ms > %s
             FOR UPDATE
            """,
            (source.source_id, claim_token, int(finished_at_ms)),
        ).fetchone()
        if claimed is None:
            return None

        gate_counts = Counter({str(key): int(value) for key, value in fetch.gate_counts.items()})
        refresh_interval_seconds = int(claimed["refresh_interval_seconds"])
        next_fetch_at_ms = int(finished_at_ms) + refresh_interval_seconds * 1_000
        if fetch.not_modified:
            self.conn.execute(
                """
                UPDATE news_sources
                   SET etag = COALESCE(%s, etag),
                       last_modified = COALESCE(%s, last_modified),
                       last_fetch_finished_at_ms = %s,
                       last_success_at_ms = %s,
                       last_http_status = %s,
                       consecutive_failures = 0,
                       last_error = NULL,
                       last_outcome = 'not_modified',
                       last_rejection_counts = %s,
                       last_items_seen = %s,
                       last_items_accepted = 0,
                       next_fetch_at_ms = %s,
                       claim_token = NULL,
                       claim_lease_expires_at_ms = NULL,
                       updated_at_ms = %s
                 WHERE source_id = %s AND claim_token = %s
                """,
                (
                    fetch.etag,
                    fetch.last_modified,
                    int(finished_at_ms),
                    int(finished_at_ms),
                    int(fetch.status_code),
                    Jsonb(dict(gate_counts)),
                    int(fetch.entries_seen),
                    next_fetch_at_ms,
                    int(finished_at_ms),
                    source.source_id,
                    claim_token,
                ),
            )
            return {"items_inserted": 0, "items_updated": 0, "items_deactivated": 0}

        accepted: dict[str, dict[str, Any]] = {}
        cutoff_ms = int(finished_at_ms) - _ACTIVE_WINDOW_MS
        for source_position, entry in enumerate(fetch.entries):
            title = str(entry.title or "").strip()
            published_at_ms = entry.published_at_ms
            if not title or published_at_ms is None:
                raise RuntimeError("rss_parser_acceptance_invariant")
            if int(published_at_ms) < cutoff_ms:
                gate_counts["stale_age"] += 1
                continue
            canonical_url = _canonical_url(str(entry.link or "")) or None
            source_item_key = _rss_source_item_key(
                guid=entry.guid,
                canonical_url=canonical_url,
                title=title,
                published_at_ms=int(published_at_ms),
            )
            if source_item_key in accepted:
                gate_counts["duplicate_item_key"] += 1
                continue
            description = str(entry.description or "").strip()
            language = str(entry.language or source.lang).strip() or source.lang
            accepted[source_item_key] = {
                "item_id": deterministic_id("news_item", source.source_id, source_item_key),
                "source_id": source.source_id,
                "source_item_key": source_item_key,
                "source_position": source_position,
                "canonical_url": canonical_url,
                "reporting_origin": source.name,
                "title": title,
                "source_title_fingerprint": _title_fingerprint(title),
                "description": description,
                "lang": language,
                "published_at_ms": int(published_at_ms),
                "observed_at_ms": int(finished_at_ms),
                "content_fingerprint": _news_item_content_fingerprint(
                    title=title,
                    description=description,
                    canonical_url=canonical_url,
                    reporting_origin=source.name,
                    published_at_ms=int(published_at_ms),
                    language=language,
                ),
            }

        inserted = 0
        updated = 0
        for values in accepted.values():
            outcome = self.conn.execute(
                """
                INSERT INTO news_items AS current_item (
                  item_id, source_id, source_item_key, source_position, canonical_url,
                  reporting_origin, title, description, lang,
                  published_at_ms, first_observed_at_ms, last_observed_at_ms,
                  content_fingerprint, active,
                  created_at_ms, updated_at_ms
                ) VALUES (
                  %(item_id)s, %(source_id)s, %(source_item_key)s,
                  %(source_position)s,
                  %(canonical_url)s, %(reporting_origin)s, %(title)s,
                  %(description)s, %(lang)s, %(published_at_ms)s,
                  %(observed_at_ms)s, %(observed_at_ms)s,
                  %(content_fingerprint)s, true, %(observed_at_ms)s,
                  %(observed_at_ms)s
                )
                ON CONFLICT (source_id, source_item_key) DO UPDATE SET
                  source_position = EXCLUDED.source_position,
                  canonical_url = EXCLUDED.canonical_url,
                  reporting_origin = EXCLUDED.reporting_origin,
                  title = EXCLUDED.title,
                  description = EXCLUDED.description,
                  lang = EXCLUDED.lang,
                  published_at_ms = EXCLUDED.published_at_ms,
                  last_observed_at_ms = EXCLUDED.last_observed_at_ms,
                  content_fingerprint = EXCLUDED.content_fingerprint,
                  level = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN NULL ELSE current_item.level END,
                  category = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN NULL ELSE current_item.category END,
                  classification_source = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN NULL ELSE current_item.classification_source END,
                  classification_confidence = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN NULL ELSE current_item.classification_confidence END,
                  importance_score = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN 0 ELSE current_item.importance_score END,
                  importance_factors = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN '{}'::jsonb ELSE current_item.importance_factors END,
                  active = true,
                  updated_at_ms = EXCLUDED.updated_at_ms
                WHERE current_item.content_fingerprint
                        IS DISTINCT FROM EXCLUDED.content_fingerprint
                   OR current_item.source_position
                        IS DISTINCT FROM EXCLUDED.source_position
                   OR NOT current_item.active
                RETURNING (xmax = 0) AS inserted
                """,
                values,
            ).fetchone()
            if outcome is None:
                continue
            self.conn.execute(
                """
                INSERT INTO news_item_title_presentations (
                  item_id, source_title_fingerprint, original_title, state,
                  created_at_ms, updated_at_ms
                ) VALUES (
                  %(item_id)s, %(source_title_fingerprint)s, %(title)s,
                  'pending', %(observed_at_ms)s, %(observed_at_ms)s
                )
                ON CONFLICT (item_id, source_title_fingerprint) DO NOTHING
                """,
                values,
            )
            if bool(outcome["inserted"]):
                inserted += 1
            else:
                updated += 1

        accepted_item_ids = [str(value["item_id"]) for value in accepted.values()]
        if accepted_item_ids:
            deactivated = self.conn.execute(
                """
                UPDATE news_items
                   SET active = false, updated_at_ms = %s
                 WHERE source_id = %s
                   AND active
                   AND NOT (item_id = ANY(%s))
                """,
                (int(finished_at_ms), source.source_id, accepted_item_ids),
            )
        else:
            deactivated = self.conn.execute(
                """
                UPDATE news_items
                   SET active = false, updated_at_ms = %s
                 WHERE source_id = %s AND active
                """,
                (int(finished_at_ms), source.source_id),
            )

        self.conn.execute(
            """
            UPDATE news_sources
               SET etag = COALESCE(%s, etag),
                   last_modified = COALESCE(%s, last_modified),
                   last_fetch_finished_at_ms = %s,
                   last_success_at_ms = %s,
                   last_http_status = %s,
                   consecutive_failures = 0,
                   last_error = NULL,
                   last_outcome = 'success',
                   last_rejection_counts = %s,
                   last_items_seen = %s,
                   last_items_accepted = %s,
                   next_fetch_at_ms = %s,
                   claim_token = NULL,
                   claim_lease_expires_at_ms = NULL,
                   updated_at_ms = %s
             WHERE source_id = %s AND claim_token = %s
            """,
            (
                fetch.etag,
                fetch.last_modified,
                int(finished_at_ms),
                int(finished_at_ms),
                int(fetch.status_code),
                Jsonb(dict(gate_counts)),
                int(fetch.entries_seen),
                len(accepted),
                next_fetch_at_ms,
                int(finished_at_ms),
                source.source_id,
                claim_token,
            ),
        )
        return {
            "items_inserted": inserted,
            "items_updated": updated,
            "items_deactivated": int(deactivated.rowcount or 0),
        }

    def record_rss_failure(
        self,
        *,
        source_id: str,
        claim_token: str,
        finished_at_ms: int,
        error_code: str,
        status_code: int | None,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_sources
               SET last_fetch_finished_at_ms = %s,
                   last_http_status = %s,
                   consecutive_failures = consecutive_failures + 1,
                   last_error = %s,
                   last_outcome = 'failed',
                   next_fetch_at_ms = %s + refresh_interval_seconds * 1000,
                   claim_token = NULL,
                   claim_lease_expires_at_ms = NULL,
                   updated_at_ms = %s
             WHERE source_id = %s
               AND source_kind = 'rss'
               AND claim_token = %s
            """,
            (
                int(finished_at_ms),
                status_code,
                str(error_code)[:500],
                int(finished_at_ms),
                int(finished_at_ms),
                source_id,
                claim_token,
            ),
        )
        return bool(cursor.rowcount)

    def expire_items(self, *, now_ms: int) -> int:
        self.lock_story_inputs()
        cursor = self.conn.execute(
            """
            UPDATE news_items AS item
               SET active = false, updated_at_ms = %s
              FROM news_sources AS source
             WHERE source.source_id = item.source_id
               AND item.active
               AND (
                 (
                   source.source_kind = 'rss'
                   AND item.published_at_ms < %s
                 )
                 OR (
                   source.source_kind = 'opennews'
                   AND item.published_at_ms < %s
                 )
               )
            """,
            (
                int(now_ms),
                int(now_ms) - _ACTIVE_WINDOW_MS,
                int(now_ms) - _STORY_ACTIVE_WINDOW_MS,
            ),
        )
        return int(cursor.rowcount or 0)

    def record_opennews_events(
        self,
        *,
        source: NewsSourceDefinition,
        events: Sequence[OpenNewsEvent],
        observed_at_ms: int,
        ingest_mode: str = "live",
    ) -> dict[str, int]:
        """Upsert bounded OpenNews facts through the sole idempotent Item writer."""

        if source.source_kind != "opennews" or source.source_id != OPENNEWS_SOURCE_ID:
            raise ValueError("opennews_source_required")
        if ingest_mode not in {"live", "recovery"}:
            raise ValueError("opennews_ingest_mode_invalid")
        rejections: Counter[str] = Counter()
        observed_strategy_provenance: list[dict[str, str]] = []
        candidates: dict[str, dict[str, Any]] = {}
        for event in events:
            incoming_metadata = {
                key: value for key, value in event.provider_metadata.items() if value not in (None, "", [], {})
            }
            incoming_strategies = _strategy_provenance(incoming_metadata.get("strategies"))
            if not incoming_strategies:
                rejections["strategy_provenance_missing"] += 1
                continue
            incoming_metadata["strategies"] = incoming_strategies

            entry = event.entry
            title = str(entry.title or "").strip() if entry is not None else ""
            canonical_url = _canonical_url(str(entry.link or "")) or None if entry is not None else None
            published_at_ms = entry.published_at_ms if entry is not None else None
            rejection = self._opennews_rejection_reason(
                title=title,
                published_at_ms=published_at_ms,
                now_ms=observed_at_ms,
            )
            if rejection is not None:
                rejections[rejection] += 1
                continue
            if entry is None or published_at_ms is None:
                raise RuntimeError("opennews_report_invariant")
            if published_at_ms < observed_at_ms - _STORY_ACTIVE_WINDOW_MS:
                rejections["stale_age"] += 1
                continue
            observed_strategy_provenance = _merged_strategy_provenance(
                observed_strategy_provenance,
                incoming_strategies,
            )
            reporting_origin = str(entry.reporting_origin or source.name).strip().lower()
            description = str(entry.description or "").strip()
            language = entry.language or source.lang
            content_fingerprint = _news_item_content_fingerprint(
                title=title,
                description=description,
                canonical_url=canonical_url,
                reporting_origin=reporting_origin,
                published_at_ms=published_at_ms,
                language=language,
            )
            incoming_payload: dict[str, Any] = {
                "provider_metadata": {key: value for key, value in incoming_metadata.items() if key != "strategies"},
                "canonical_url": canonical_url,
                "reporting_origin": reporting_origin,
                "title": title,
                "description": description,
                "lang": language,
                "published_at_ms": int(published_at_ms),
                "content_fingerprint": content_fingerprint,
            }
            current = candidates.get(event.provider_record_id)
            if current is None:
                candidates[event.provider_record_id] = {
                    "payload": incoming_payload,
                    "strategies": incoming_strategies,
                    "frame_count": 1,
                }
            else:
                current_payload = current["payload"]
                current["payload"] = (
                    current_payload
                    if _provider_payload_order(current_payload) > _provider_payload_order(incoming_payload)
                    else incoming_payload
                )
                current["strategies"] = _merged_strategy_provenance(
                    current["strategies"],
                    incoming_strategies,
                )
                current["frame_count"] += 1

        inserted = 0
        updated = 0
        source_provenance: object = []
        existing_by_record: dict[str, Mapping[str, Any]] = {}
        push_delivery_available = False
        push_enablement_epoch_at_ms: int | None = None
        if events:
            self.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"news-opennews-source:{source.source_id}",),
            )
            state_rows = self.conn.execute(
                """
                WITH source_state AS MATERIALIZED (
                  SELECT source.observed_strategy_provenance
                    FROM news_sources AS source
                   WHERE source.source_id = %s
                     AND source.source_kind = 'opennews'
                   FOR UPDATE
                ), push_state AS MATERIALIZED (
                  SELECT delivery_available, enablement_epoch_at_ms
                    FROM news_push_state
                   WHERE singleton_key = 'current'
                   FOR UPDATE
                ), item_state AS MATERIALIZED (
                  SELECT item.provider_record_id, item.provider_metadata,
                         item.first_ingest_mode,
                         item.canonical_url, item.reporting_origin,
                         item.title, item.description, item.lang,
                         item.published_at_ms, item.content_fingerprint,
                         item.active
                    FROM news_items AS item
                   WHERE item.source_id = %s
                     AND item.provider_record_id = ANY(%s)
                   ORDER BY item.provider_record_id
                   FOR UPDATE
                )
                SELECT source_state.observed_strategy_provenance AS source_provenance,
                       push_state.delivery_available AS push_delivery_available,
                       push_state.enablement_epoch_at_ms AS push_enablement_epoch_at_ms,
                       item_state.*
                  FROM source_state
                  CROSS JOIN push_state
                  LEFT JOIN item_state ON true
                 ORDER BY item_state.provider_record_id NULLS FIRST
                """,
                (
                    source.source_id,
                    source.source_id,
                    sorted(candidates),
                ),
            ).fetchall()
            if not state_rows:
                raise RuntimeError("opennews_source_not_synced")
            source_provenance = state_rows[0]["source_provenance"]
            push_delivery_available = bool(state_rows[0]["push_delivery_available"])
            push_enablement_epoch_at_ms = (
                int(state_rows[0]["push_enablement_epoch_at_ms"])
                if state_rows[0]["push_enablement_epoch_at_ms"] is not None
                else None
            )
            existing_by_record = {
                str(row["provider_record_id"]): row for row in state_rows if row["provider_record_id"] is not None
            }

        write_rows: list[dict[str, Any]] = []
        for provider_record_id in sorted(candidates):
            candidate = candidates[provider_record_id]
            incoming_payload = candidate["payload"]
            incoming_strategies = candidate["strategies"]
            existing = existing_by_record.get(provider_record_id)
            existing_metadata = dict(existing["provider_metadata"] or {}) if existing is not None else {}
            existing_payload: dict[str, Any] | None = None
            if (
                existing is not None
                and existing["active"]
                and _strategy_provenance(existing_metadata.get("strategies"))
            ):
                existing_payload = {
                    "provider_metadata": {
                        key: value for key, value in existing_metadata.items() if key != "strategies"
                    },
                    "canonical_url": existing["canonical_url"],
                    "reporting_origin": existing["reporting_origin"],
                    "title": existing["title"],
                    "description": existing["description"],
                    "lang": existing["lang"],
                    "published_at_ms": int(existing["published_at_ms"]),
                    "content_fingerprint": existing["content_fingerprint"],
                }
            existing_is_strategy_fact = existing_payload is not None
            winner = (
                existing_payload
                if existing_payload is not None
                and _provider_payload_order(existing_payload) > _provider_payload_order(incoming_payload)
                else incoming_payload
            )
            winner_metadata = dict(winner["provider_metadata"])
            winner_metadata["strategies"] = _merged_strategy_provenance(
                existing_metadata.get("strategies") if existing_is_strategy_fact else None,
                incoming_strategies,
            )
            material_changed = bool(
                existing is None
                or not existing["active"]
                or existing["content_fingerprint"] != winner["content_fingerprint"]
                or existing_metadata != winner_metadata
            )
            if not material_changed:
                rejections["duplicate"] += int(candidate["frame_count"])
                continue
            rejections["duplicate"] += max(0, int(candidate["frame_count"]) - 1)
            write_rows.append(
                {
                    "item_id": deterministic_id("news_item", source.source_id, provider_record_id),
                    "source_id": source.source_id,
                    "source_item_key": provider_record_id,
                    "provider_record_id": provider_record_id,
                    "provider_metadata": winner_metadata,
                    "first_ingest_mode": (
                        str(existing["first_ingest_mode"])
                        if existing is not None and existing["first_ingest_mode"] is not None
                        else ingest_mode
                    ),
                    "canonical_url": winner["canonical_url"],
                    "reporting_origin": winner["reporting_origin"],
                    "title": winner["title"],
                    "source_title_fingerprint": _title_fingerprint(str(winner["title"])),
                    "description": winner["description"],
                    "lang": winner["lang"],
                    "published_at_ms": winner["published_at_ms"],
                    "observed_at_ms": int(observed_at_ms),
                    "content_fingerprint": winner["content_fingerprint"],
                }
            )
            exact_atom = describe_exact_atom(str(winner["title"]))
            write_rows[-1].update(
                {
                    "notification_fingerprint": exact_atom.comparison_fingerprint,
                    "comparison_identity_version": exact_atom.identity_version,
                    "exact_atom_trackable": bool(exact_atom.comparison_title),
                    "duplicate_window_ms": exact_atom.duplicate_window_ms,
                    "push_status": None,
                    "admission_reason": None,
                    "suppressed_by_item_id": None,
                    "adjudicated_at_ms": None,
                    "push_eligible": bool(
                        existing is None
                        and ingest_mode == "live"
                        and push_delivery_available
                        and push_enablement_epoch_at_ms is not None
                        and int(observed_at_ms) >= push_enablement_epoch_at_ms
                    ),
                }
            )
            write_rows[-1]["push_payload"] = _news_item_push_source_payload(
                item_id=str(write_rows[-1]["item_id"]),
                provider_event_id=provider_record_id,
                provider_metadata=winner_metadata,
                live_observed_at_ms=int(observed_at_ms),
                original_title=str(winner["title"]),
                reporting_origin=str(winner["reporting_origin"]),
                provider_published_at_ms=int(winner["published_at_ms"]),
                source_url=(str(winner["canonical_url"]) if winner["canonical_url"] is not None else None),
            )

        eligible_push_rows = [row for row in write_rows if bool(row["push_eligible"])]
        if eligible_push_rows:
            fingerprints = sorted({str(row["notification_fingerprint"]) for row in eligible_push_rows})
            minimum_published_at_ms = min(int(row["published_at_ms"]) for row in eligible_push_rows)
            maximum_published_at_ms = max(int(row["published_at_ms"]) for row in eligible_push_rows)
            maximum_window_ms = max(int(row["duplicate_window_ms"]) for row in eligible_push_rows)
            existing_leader_rows = self.conn.execute(
                """
                SELECT item_id, notification_fingerprint, adjudicated_at_ms,
                       (source_payload ->> 'provider_published_at_ms')::bigint
                         AS provider_published_at_ms
                  FROM news_push_deliveries
                 WHERE admission_policy_version = %(policy_version)s
                   AND notification_fingerprint = ANY(%(fingerprints)s)
                   AND status IN ('pending', 'sending', 'sent', 'terminal')
                   AND source_payload ->> 'schema_version' = %(schema_version)s
                   AND (source_payload ->> 'provider_published_at_ms')::bigint
                         BETWEEN %(minimum_published_at_ms)s
                             AND %(maximum_published_at_ms)s
                 ORDER BY
                       (source_payload ->> 'provider_published_at_ms')::bigint,
                       item_id
                """,
                {
                    "policy_version": NEWS_PUSH_ADMISSION_POLICY_VERSION,
                    "schema_version": NEWS_PUSH_PAYLOAD_SCHEMA_VERSION,
                    "fingerprints": fingerprints,
                    "minimum_published_at_ms": minimum_published_at_ms - maximum_window_ms,
                    "maximum_published_at_ms": maximum_published_at_ms + maximum_window_ms,
                },
            ).fetchall()
            leaders_by_fingerprint: dict[str, list[dict[str, Any]]] = {}
            for leader in existing_leader_rows:
                leaders_by_fingerprint.setdefault(str(leader["notification_fingerprint"]), []).append(
                    {
                        "item_id": str(leader["item_id"]),
                        "provider_published_at_ms": int(leader["provider_published_at_ms"]),
                        "durable": True,
                    }
                )
            for row in sorted(
                eligible_push_rows,
                key=lambda value: (int(value["published_at_ms"]), str(value["item_id"])),
            ):
                fingerprint = str(row["notification_fingerprint"])
                published_at_ms = int(row["published_at_ms"])
                duplicate_window_ms = int(row["duplicate_window_ms"])
                compatible = (
                    [
                        leader
                        for leader in leaders_by_fingerprint.get(fingerprint, [])
                        if abs(published_at_ms - int(leader["provider_published_at_ms"])) <= duplicate_window_ms
                    ]
                    if bool(row["exact_atom_trackable"])
                    else []
                )
                leader = next((value for value in compatible if bool(value["durable"])), None)
                if leader is None and compatible:
                    leader = compatible[0]
                row["adjudicated_at_ms"] = int(observed_at_ms)
                if leader is None:
                    row["push_status"] = "pending"
                    row["admission_reason"] = "exact_atom_leader"
                    leaders_by_fingerprint.setdefault(fingerprint, []).append(
                        {
                            "item_id": str(row["item_id"]),
                            "provider_published_at_ms": published_at_ms,
                            "durable": False,
                        }
                    )
                else:
                    row["push_status"] = "suppressed"
                    row["admission_reason"] = "exact_atom_suppressed"
                    row["suppressed_by_item_id"] = str(leader["item_id"])

        push_outbox_writes = 0
        if write_rows:
            outcome = self.conn.execute(
                """
                WITH incoming AS MATERIALIZED (
                  SELECT *
                    FROM jsonb_to_recordset(%(json)s::jsonb) AS row(
                      item_id text,
                      source_id text,
                      source_item_key text,
                      provider_record_id text,
                      provider_metadata jsonb,
                      first_ingest_mode text,
                      canonical_url text,
                      reporting_origin text,
                      title text,
                      source_title_fingerprint text,
                      description text,
                      lang text,
                      published_at_ms bigint,
                      observed_at_ms bigint,
                      content_fingerprint text,
                      push_payload jsonb,
                      notification_fingerprint text,
                      comparison_identity_version text,
                      push_status text,
                      adjudicated_at_ms bigint,
                      admission_reason text,
                      suppressed_by_item_id text
                    )
                ), written AS (
                  INSERT INTO news_items AS current_item (
                    item_id, source_id, source_item_key, provider_record_id,
                    provider_metadata, first_ingest_mode,
                    canonical_url, reporting_origin, title,
                    description, lang, published_at_ms,
                    first_observed_at_ms, last_observed_at_ms,
                    content_fingerprint, active, created_at_ms, updated_at_ms
                  )
                  SELECT item_id, source_id, source_item_key, provider_record_id,
                         provider_metadata, first_ingest_mode,
                         canonical_url, reporting_origin, title,
                         description, lang, published_at_ms,
                         observed_at_ms, observed_at_ms,
                         content_fingerprint, true, observed_at_ms, observed_at_ms
                    FROM incoming
                   ORDER BY provider_record_id
                  ON CONFLICT (source_id, provider_record_id)
                    WHERE provider_record_id IS NOT NULL
                  DO UPDATE SET
                    source_item_key = EXCLUDED.source_item_key,
                    provider_metadata = EXCLUDED.provider_metadata,
                    first_ingest_mode = current_item.first_ingest_mode,
                    canonical_url = EXCLUDED.canonical_url,
                    reporting_origin = EXCLUDED.reporting_origin,
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    lang = EXCLUDED.lang,
                    published_at_ms = EXCLUDED.published_at_ms,
                    last_observed_at_ms = EXCLUDED.last_observed_at_ms,
                    content_fingerprint = EXCLUDED.content_fingerprint,
                    level = CASE
                      WHEN current_item.content_fingerprint
                             IS DISTINCT FROM EXCLUDED.content_fingerprint
                        THEN NULL ELSE current_item.level END,
                    category = CASE
                      WHEN current_item.content_fingerprint
                             IS DISTINCT FROM EXCLUDED.content_fingerprint
                        THEN NULL ELSE current_item.category END,
                    classification_source = CASE
                      WHEN current_item.content_fingerprint
                             IS DISTINCT FROM EXCLUDED.content_fingerprint
                        THEN NULL ELSE current_item.classification_source END,
                    classification_confidence = CASE
                      WHEN current_item.content_fingerprint
                             IS DISTINCT FROM EXCLUDED.content_fingerprint
                        THEN NULL ELSE current_item.classification_confidence END,
                    importance_score = CASE
                      WHEN current_item.content_fingerprint
                             IS DISTINCT FROM EXCLUDED.content_fingerprint
                        THEN 0 ELSE current_item.importance_score END,
                    importance_factors = CASE
                      WHEN current_item.content_fingerprint
                             IS DISTINCT FROM EXCLUDED.content_fingerprint
                        THEN '{}'::jsonb ELSE current_item.importance_factors END,
                    active = true,
                    updated_at_ms = EXCLUDED.updated_at_ms
                  RETURNING current_item.item_id,
                            current_item.first_ingest_mode,
                            (xmax = 0) AS inserted
                ), presentation_written AS (
                  INSERT INTO news_item_title_presentations (
                    item_id, source_title_fingerprint, original_title, state,
                    display_title, outcome, provider, policy_version,
                    fallback_code, created_at_ms, updated_at_ms,
                    attempted_at_ms, resolved_at_ms, duration_ms
                  )
                  SELECT written.item_id, incoming.source_title_fingerprint,
                         incoming.title, 'pending',
                         NULL, NULL, NULL, NULL, NULL,
                         incoming.observed_at_ms, incoming.observed_at_ms,
                         NULL, NULL, NULL
                    FROM written
                    JOIN incoming USING (item_id)
                   ORDER BY written.item_id, incoming.source_title_fingerprint
                  ON CONFLICT (item_id, source_title_fingerprint) DO NOTHING
                  RETURNING item_id, source_title_fingerprint
                ), push_written AS (
                  INSERT INTO news_push_deliveries (
                    item_id, source_title_fingerprint,
                    live_observed_at_ms, source_payload,
                    notification_fingerprint,
                    comparison_identity_version,
                    admission_policy_version,
                    adjudicated_at_ms, admission_reason,
                    suppressed_by_item_id,
                    legacy_delivery_payload, legacy_presentation_snapshot,
                    status, attempted_at_ms, receipt, last_error,
                    sent_at_ms, created_at_ms, updated_at_ms
                  )
                  SELECT written.item_id, incoming.source_title_fingerprint,
                         incoming.observed_at_ms,
                         incoming.push_payload,
                         incoming.notification_fingerprint,
                         incoming.comparison_identity_version,
                         %(admission_policy_version)s,
                         incoming.adjudicated_at_ms,
                         incoming.admission_reason,
                         incoming.suppressed_by_item_id,
                         NULL, NULL,
                         incoming.push_status, NULL, NULL, NULL, NULL,
                         incoming.observed_at_ms, incoming.observed_at_ms
                    FROM written
                    JOIN incoming USING (item_id)
                    JOIN presentation_written USING (
                      item_id, source_title_fingerprint
                    )
                   WHERE written.inserted
                     AND written.first_ingest_mode = 'live'
                     AND incoming.push_status IS NOT NULL
                  ON CONFLICT (item_id) DO NOTHING
                  RETURNING item_id, status
                ), push_count AS MATERIALIZED (
                  SELECT count(*)::bigint AS total,
                         count(*) FILTER (WHERE status = 'pending')::bigint AS pending,
                         count(*) FILTER (WHERE status = 'suppressed')::bigint AS suppressed
                    FROM push_written
                ), push_summary AS (
                  UPDATE news_push_state state
                     SET total_count = total_count + push_count.total,
                         pending_count = pending_count + push_count.pending,
                         suppressed_count = suppressed_count + push_count.suppressed,
                         updated_at_ms = greatest(
                           state.updated_at_ms,
                           %(observed_at_ms)s
                         )
                   FROM push_count
                   WHERE state.singleton_key = 'current'
                     AND push_count.total > 0
                  RETURNING state.singleton_key
                )
                SELECT count(*) FILTER (WHERE inserted) AS inserted,
                       count(*) FILTER (WHERE NOT inserted) AS updated,
                       (SELECT total FROM push_count) AS push_outbox_writes,
                       (SELECT count(*) FROM push_summary) AS push_summary_writes
                  FROM written
                """,
                {
                    "json": Jsonb(write_rows),
                    "observed_at_ms": int(observed_at_ms),
                    "admission_policy_version": NEWS_PUSH_ADMISSION_POLICY_VERSION,
                },
            ).fetchone()
            inserted = int(outcome["inserted"] or 0)
            updated = int(outcome["updated"] or 0)
            push_outbox_writes = int(outcome["push_outbox_writes"] or 0)

        if events:
            durable_strategy_provenance = _merged_strategy_provenance(
                source_provenance,
                observed_strategy_provenance,
            )
            self.conn.execute(
                """
                UPDATE news_sources
                   SET last_success_at_ms = CASE
                         WHEN %(accepted_trigger)s THEN %(observed_at_ms)s
                         ELSE last_success_at_ms
                       END,
                       last_accepted_strategy_trigger_at_ms = CASE
                         WHEN %(accepted_trigger)s THEN %(observed_at_ms)s
                         ELSE last_accepted_strategy_trigger_at_ms
                       END,
                       observed_strategy_provenance = %(strategy_provenance)s,
                       last_outcome = CASE
                         WHEN %(accepted_trigger)s THEN 'strategy_trigger_success'
                         ELSE 'strategy_trigger_rejected'
                       END,
                       last_rejection_counts = %(last_rejection_counts)s,
                       last_items_seen = %(last_items_seen)s,
                       last_items_accepted = %(last_items_accepted)s,
                       updated_at_ms = %(observed_at_ms)s
                 WHERE source_id = %(source_id)s
                """,
                {
                    "accepted_trigger": bool(observed_strategy_provenance),
                    "observed_at_ms": int(observed_at_ms),
                    "strategy_provenance": Jsonb(durable_strategy_provenance),
                    "last_rejection_counts": Jsonb(dict(rejections)),
                    "last_items_seen": len(events),
                    "last_items_accepted": inserted + updated,
                    "source_id": source.source_id,
                },
            )
        return {
            "events_seen": len(events),
            "items_inserted": inserted,
            "items_updated": updated,
            "push_outbox_writes": push_outbox_writes,
            "metadata_updated": 0,
            "rejected": sum(rejections.values()),
        }

    @staticmethod
    def _opennews_rejection_reason(
        *,
        title: str,
        published_at_ms: int | None,
        now_ms: int,
    ) -> str | None:
        if not title:
            return "missing_title"
        if published_at_ms is None:
            return "missing_date"
        if published_at_ms > now_ms + 3_600_000:
            return "future_date"
        return None

    def update_opennews_live_status(
        self,
        *,
        source_id: str,
        connected: bool,
        now_ms: int,
        error_code: str | None,
        coverage_gap: bool = False,
        planned: bool = False,
        close_code: int | None = None,
    ) -> bool:
        normalized_error = _public_opennews_error(error_code)
        overflow = bool(coverage_gap or normalized_error == "opennews_buffer_overflow")
        if planned:
            self.conn.execute(
                """
                UPDATE news_opennews_incidents
                   SET closed_at_ms = COALESCE(closed_at_ms, %s),
                       recovery_to_at_ms = COALESCE(recovery_to_at_ms, %s),
                       updated_at_ms = %s
                 WHERE source_id = %s
                   AND cause_class = 'buffer_overflow'
                   AND closed_at_ms IS NULL
                   AND NOT planned
                """,
                (int(now_ms), int(now_ms), int(now_ms), source_id),
            )
            self.conn.execute(
                """
                INSERT INTO news_opennews_incidents (
                  source_id, cause_class, opened_at_ms,
                  planned, close_code, recovery_status,
                  recovery_from_at_ms, recovery_to_at_ms, recovered_count,
                  last_error_code, created_at_ms, updated_at_ms
                ) VALUES (
                  %s, 'planned_shutdown', %s, true, %s,
                  'pending', %s, NULL, 0, NULL, %s, %s
                )
                """,
                (
                    source_id,
                    int(now_ms),
                    close_code,
                    int(now_ms),
                    int(now_ms),
                    int(now_ms),
                ),
            )
        elif overflow:
            self.conn.execute(
                """
                INSERT INTO news_opennews_incidents (
                  source_id, cause_class, opened_at_ms, planned,
                  recovery_status, recovery_from_at_ms,
                  recovered_count, last_error_code,
                  created_at_ms, updated_at_ms
                ) VALUES (
                  %s, 'buffer_overflow', %s, false, 'pending',
                  %s, 0, 'opennews_buffer_overflow', %s, %s
                )
                ON CONFLICT DO NOTHING
                """,
                (source_id, *(int(now_ms) for _ in range(4))),
            )
        elif not connected:
            self.conn.execute(
                """
                UPDATE news_opennews_incidents
                   SET closed_at_ms = COALESCE(closed_at_ms, %s),
                       recovery_to_at_ms = COALESCE(recovery_to_at_ms, %s),
                       updated_at_ms = %s
                 WHERE source_id = %s
                   AND cause_class = 'buffer_overflow'
                   AND closed_at_ms IS NULL
                   AND NOT planned
                """,
                (int(now_ms), int(now_ms), int(now_ms), source_id),
            )
            cause_class = _opennews_incident_cause(normalized_error)
            self.conn.execute(
                """
                INSERT INTO news_opennews_incidents (
                  source_id, cause_class, opened_at_ms, planned, close_code,
                  recovery_status, recovery_from_at_ms, recovered_count,
                  last_error_code, created_at_ms, updated_at_ms
                ) VALUES (%s, %s, %s, false, %s, 'pending', %s, 0, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    source_id,
                    cause_class,
                    int(now_ms),
                    close_code,
                    int(now_ms),
                    normalized_error,
                    int(now_ms),
                    int(now_ms),
                ),
            )
        else:
            self.conn.execute(
                """
                UPDATE news_opennews_incidents
                   SET reconnected_at_ms = COALESCE(reconnected_at_ms, %s),
                       closed_at_ms = COALESCE(closed_at_ms, %s),
                       recovery_to_at_ms = COALESCE(recovery_to_at_ms, %s),
                       updated_at_ms = %s
                 WHERE source_id = %s
                   AND closed_at_ms IS NULL
                """,
                (int(now_ms), int(now_ms), int(now_ms), int(now_ms), source_id),
            )
        cursor = self.conn.execute(
            """
            UPDATE news_sources
               SET live_connected = %(connected)s,
                   last_connected_at_ms = CASE
                     WHEN %(connected)s THEN %(now_ms)s
                     ELSE last_connected_at_ms
                   END,
                   last_disconnected_at_ms = CASE
                     WHEN NOT %(connected)s THEN %(now_ms)s
                     ELSE last_disconnected_at_ms
                   END,
                   last_outcome = CASE
                     WHEN %(planned)s THEN 'strategy_planned_shutdown'
                     WHEN %(overflow)s THEN 'strategy_connected_overflow'
                     WHEN %(error_code)s::text IS NOT NULL THEN 'strategy_disconnected_failed'
                     WHEN %(connected)s THEN 'strategy_connected'
                     ELSE 'strategy_disconnected'
                   END,
                   last_error = CASE
                     WHEN %(connected)s THEN NULL
                     WHEN %(error_code)s::text IS NOT NULL
                       THEN %(error_code)s::text
                     ELSE NULL
                   END,
                   consecutive_failures = CASE
                     WHEN %(error_code)s::text IS NOT NULL
                       THEN consecutive_failures + 1
                     WHEN %(connected)s THEN 0
                     ELSE consecutive_failures
                   END,
                   updated_at_ms = %(now_ms)s
             WHERE source_id = %(source_id)s
               AND source_kind = 'opennews'
            """,
            {
                "connected": connected,
                "now_ms": int(now_ms),
                "error_code": normalized_error,
                "overflow": overflow,
                "planned": planned,
                "source_id": source_id,
            },
        )
        return bool(cursor.rowcount)

    def claim_opennews_recovery(
        self,
        *,
        source_id: str,
        after_incident_id: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
              FROM news_opennews_incidents
             WHERE source_id = %s
               AND incident_id > %s
               AND closed_at_ms IS NOT NULL
               AND recovery_status IN ('pending', 'partial', 'unavailable')
             ORDER BY opened_at_ms, incident_id
             LIMIT 1
             FOR UPDATE SKIP LOCKED
            """,
            (source_id, int(after_incident_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    def complete_opennews_recovery(
        self,
        *,
        source_id: str,
        incident_id: int | None,
        status: str,
        recovered_count: int,
        error_code: str | None,
        now_ms: int,
    ) -> None:
        if status not in {"recovered", "partial", "unavailable"}:
            raise ValueError("opennews_recovery_status_invalid")
        public_error = _public_opennews_error(error_code)
        history_status = "available" if status == "recovered" else status
        if incident_id is not None:
            self.conn.execute(
                """
                UPDATE news_opennews_incidents
                   SET recovery_status = %s,
                       recovered_count = recovered_count + %s,
                       last_error_code = %s,
                       updated_at_ms = %s
                 WHERE incident_id = %s AND source_id = %s
                """,
                (status, int(recovered_count), public_error, int(now_ms), int(incident_id), source_id),
            )
        elif status == "unavailable":
            self.conn.execute(
                """
                UPDATE news_opennews_incidents
                   SET recovery_status = 'unavailable',
                       last_error_code = %s,
                       updated_at_ms = %s
                 WHERE source_id = %s
                   AND recovery_status IN ('pending', 'running', 'partial', 'unavailable')
                """,
                (public_error, int(now_ms), source_id),
            )
        self.conn.execute(
            """
            UPDATE news_sources
               SET strategy_history_status = %s,
                   last_history_check_at_ms = %s,
                   updated_at_ms = greatest(updated_at_ms, %s)
             WHERE source_id = %s AND source_kind = 'opennews'
            """,
            (history_status, int(now_ms), int(now_ms), source_id),
        )

    # Persistent Story projection ----------------------------------------------

    def load_story_projection(self, *, now_ms: int) -> dict[str, Any]:
        from .story_store import load_story_projection

        return load_story_projection(self, now_ms=now_ms)

    def publish_story_projection(
        self,
        *,
        snapshot: Any,
        projection: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        from .story_store import publish_story_projection

        return publish_story_projection(
            self,
            snapshot=snapshot,
            projection=projection,
            now_ms=now_ms,
        )

    def record_story_projection_failure(
        self,
        *,
        now_ms: int,
        error_code: str,
    ) -> None:
        from .story_store import record_story_projection_failure

        record_story_projection_failure(
            self,
            now_ms=now_ms,
            error_code=error_code,
        )

    def _story_invariant_counts(
        self,
        *,
        item_ids: Sequence[str] | None = None,
    ) -> dict[str, int]:
        """Check one published item snapshot while retaining global Story checks."""

        scoped_item_ids = sorted({str(item_id) for item_id in item_ids}) if item_ids is not None else None
        row = self.conn.execute(
            """
            WITH current_owners AS (
              SELECT i.item_id, count(m.story_id) AS owner_count
                FROM news_items i
                LEFT JOIN news_story_members m
                  ON m.item_id = i.item_id
               WHERE i.active
                 AND (
                   %(item_ids)s::text[] IS NULL
                   OR i.item_id = ANY(%(item_ids)s::text[])
                 )
               GROUP BY i.item_id
            ),
            story_aggregates AS (
              SELECT st.story_id,
                     st.item_count AS stored_item_count,
                     st.source_count AS stored_source_count,
                     st.first_published_at_ms AS stored_first_at_ms,
                     st.last_published_at_ms AS stored_last_at_ms,
                     st.facet_facts AS stored_facet_facts,
                     count(m.item_id) AS actual_item_count,
                     count(DISTINCT i.reporting_origin) AS actual_source_count,
                     min(i.published_at_ms) AS actual_first_at_ms,
                     max(i.published_at_ms) AS actual_last_at_ms,
                     jsonb_build_object(
                       'source_ids', coalesce(
                         jsonb_agg(
                           DISTINCT (i.source_id COLLATE "C")
                           ORDER BY (i.source_id COLLATE "C")
                         )
                           FILTER (WHERE i.source_id IS NOT NULL),
                         '[]'::jsonb
                       ),
                       'reporting_origins', coalesce(
                         jsonb_agg(
                           DISTINCT (btrim(i.reporting_origin) COLLATE "C")
                           ORDER BY (btrim(i.reporting_origin) COLLATE "C")
                         ) FILTER (
                           WHERE nullif(btrim(i.reporting_origin), '') IS NOT NULL
                         ),
                         '[]'::jsonb
                       )
                     ) AS actual_facet_facts,
                     bool_or(m.item_id = st.representative_item_id)
                       AS representative_is_member,
                     bool_or(m.item_id = st.scoring_item_id)
                       AS scoring_item_is_member
                FROM news_stories st
                LEFT JOIN news_story_members m
                  ON m.story_id = st.story_id
                LEFT JOIN news_items i ON i.item_id = m.item_id
               GROUP BY st.story_id
            ),
            invalid_stories AS (
              SELECT story_id
                FROM story_aggregates
               WHERE stored_item_count <> actual_item_count
                  OR stored_source_count <> actual_source_count
                  OR stored_first_at_ms IS DISTINCT FROM actual_first_at_ms
                  OR stored_last_at_ms IS DISTINCT FROM actual_last_at_ms
                  OR stored_facet_facts IS DISTINCT FROM actual_facet_facts
                  OR representative_is_member IS DISTINCT FROM true
                  OR scoring_item_is_member IS DISTINCT FROM true
            )
            SELECT count(*) FILTER (WHERE owner_count <> 1)
                     AS invalid_owner_count,
                   (SELECT count(*) FROM invalid_stories)
                     AS invalid_story_aggregate_count
              FROM current_owners
            """,
            {"item_ids": scoped_item_ids},
        ).fetchone()
        invalid_owners = int(row["invalid_owner_count"] or 0)
        invalid_aggregates = int(row["invalid_story_aggregate_count"] or 0)
        return {
            "invalid_owner_count": invalid_owners,
            "invalid_story_aggregate_count": invalid_aggregates,
            "total": invalid_owners + invalid_aggregates,
        }

    # Read contract ------------------------------------------------------------

    def list_feed(
        self,
        *,
        category: str | None = None,
        level: str | None = None,
        source_id: str | None = None,
        reporting_origin: str | None = None,
        provider_score_gt: float | None = None,
        q: str | None = None,
        sort: str = "importance",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if sort not in {"importance", "latest"}:
            raise ValueError("news_feed_sort_invalid")
        if limit < 1 or limit > 100:
            raise ValueError("news_feed_limit_invalid")
        normalized_category = str(category or "").strip().lower() or None
        normalized_level = str(level or "").strip().lower() or None
        normalized_source_id = str(source_id or "").strip() or None
        normalized_reporting_origin = str(reporting_origin or "").strip().lower() or None
        normalized_query = " ".join(str(q or "").split()).lower() or None
        if normalized_reporting_origin is not None and len(normalized_reporting_origin) > 128:
            raise ValueError("news_feed_reporting_origin_invalid")
        if normalized_query is not None and len(normalized_query) > 200:
            raise ValueError("news_feed_query_invalid")
        normalized_provider_score_gt = None
        if provider_score_gt is not None:
            if not _numeric_provider_score(provider_score_gt):
                raise ValueError("news_feed_provider_score_gt_invalid")
            normalized_provider_score_gt = float(provider_score_gt)
        filters: dict[str, str | float | None] = {
            "category": normalized_category,
            "level": normalized_level,
            "source_id": normalized_source_id,
            "reporting_origin": normalized_reporting_origin,
            "provider_score_gt": normalized_provider_score_gt,
            "q": normalized_query,
        }
        feed_cursor: tuple[int, int, str] | None = None
        if cursor:
            decoded = _cursor_decode(cursor)
            if decoded.get("v") != 2 or decoded.get("sort") != sort:
                raise ValueError("news_feed_cursor_invalid")
            cursor_filters = decoded.get("filters")
            if cursor_filters != filters:
                raise ValueError("news_feed_cursor_filter_mismatch")
            try:
                last_ms = int(decoded["last_published_at_ms"])
                score = int(decoded["importance_score"])
                story_id_value = str(decoded["story_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("news_feed_cursor_invalid") from exc
            feed_cursor = (last_ms, score, story_id_value)
        rows_query = query_specs.feed_rows_query(
            category=normalized_category,
            level=normalized_level,
            source_id=normalized_source_id,
            reporting_origin=normalized_reporting_origin,
            provider_score_gt=normalized_provider_score_gt,
            q=normalized_query,
            sort=sort,
            limit=limit,
            cursor=feed_cursor,
        )
        rows = self.conn.execute(
            rows_query.sql,
            rows_query.params,
        ).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        page_story_ids = [str(row["story_id"]) for row in page]
        provider_evidence = self.story_provider_evidence(story_ids=page_story_ids)
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _cursor_encode(
                {
                    "v": 2,
                    "sort": sort,
                    "filters": filters,
                    "last_published_at_ms": int(last["last_published_at_ms"]),
                    "importance_score": int(last["importance_score"]),
                    "story_id": str(last["story_id"]),
                }
            )
        stories = []
        for row in page:
            story = _story_summary(row)
            selected = provider_evidence.get(str(row["story_id"]))
            story["provider_evidence"] = _public_provider_evidence(selected)
            stories.append(story)
        return {
            "sort": sort,
            "filters": filters,
            "stories": stories,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "facets": self._feed_facets(
                category=normalized_category,
                level=normalized_level,
                source_id=normalized_source_id,
                reporting_origin=normalized_reporting_origin,
                provider_score_gt=normalized_provider_score_gt,
                q=normalized_query,
            ),
        }

    def _feed_facets(
        self,
        *,
        category: str | None,
        level: str | None,
        source_id: str | None,
        reporting_origin: str | None,
        provider_score_gt: float | None,
        q: str | None,
    ) -> dict[str, Any]:
        facets_query = query_specs.feed_facets_query(
            category=category,
            level=level,
            source_id=source_id,
            reporting_origin=reporting_origin,
            provider_score_gt=provider_score_gt,
            q=q,
        )
        rows = self.conn.execute(facets_query.sql, facets_query.params).fetchall()
        facets: dict[str, list[dict[str, Any]]] = {
            "category": [],
            "level": [],
            "source": [],
            "reporting_origin": [],
        }
        has_more = {facet_type: False for facet_type in facets}
        for row in rows:
            facet_type = str(row["facet_type"])
            if int(row["position"]) > query_specs.PUBLIC_LIST_LIMIT:
                has_more[facet_type] = True
                continue
            facet = {
                "value": str(row["value"]),
                "count": int(row["count"]),
            }
            if facet_type in {"source", "reporting_origin"}:
                facet["label"] = str(row["label"])
            facets[facet_type].append(facet)
        return {
            "categories": facets["category"],
            "levels": facets["level"],
            "sources": facets["source"],
            "reporting_origins": facets["reporting_origin"],
            "page": {
                "categories_has_more": has_more["category"],
                "levels_has_more": has_more["level"],
                "sources_has_more": has_more["source"],
                "reporting_origins_has_more": has_more["reporting_origin"],
            },
        }

    def get_story(
        self,
        *,
        story_id: str,
        members_limit: int = 100,
        members_cursor: str | None = None,
    ) -> dict[str, Any] | None:
        if members_limit < 1 or members_limit > 100:
            raise ValueError("news_story_members_limit_invalid")
        story_query = query_specs.story_query(story_id=story_id)
        row = self.conn.execute(story_query.sql, story_query.params).fetchone()
        if row is None:
            return None
        selected = self.story_provider_evidence(story_ids=(story_id,)).get(story_id)
        provider_evidence = _public_provider_evidence(selected)
        member_cursor: tuple[int, str] | None = None
        if members_cursor:
            decoded = _cursor_decode(members_cursor)
            if decoded.get("v") != 2 or decoded.get("kind") != "story_members":
                raise ValueError("news_story_members_cursor_invalid")
            if decoded.get("story_id") != story_id:
                raise ValueError("news_story_members_cursor_story_mismatch")
            try:
                published_at_ms = int(decoded["published_at_ms"])
                item_id = str(decoded["item_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("news_story_members_cursor_invalid") from exc
            member_cursor = (published_at_ms, item_id)
        members_query = query_specs.story_members_query(
            story_id=story_id,
            limit=members_limit,
            cursor=member_cursor,
        )
        members = self.conn.execute(
            members_query.sql,
            members_query.params,
        ).fetchall()
        has_more = len(members) > members_limit
        page = members[:members_limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _cursor_encode(
                {
                    "v": 2,
                    "kind": "story_members",
                    "story_id": story_id,
                    "published_at_ms": int(last["published_at_ms"]),
                    "item_id": str(last["item_id"]),
                }
            )
        story = _story_summary(row)
        return {
            **story,
            "provider_evidence": provider_evidence,
            "canonical_title": str(row["canonical_title"]),
            "identity_evidence": dict(row["identity_evidence"]),
            "members": [_item_payload(member) for member in page],
            "members_page": {
                "returned_count": len(page),
                "has_more": has_more,
                "next_cursor": next_cursor,
            },
        }

    def list_sources(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise ValueError("news_sources_limit_invalid")
        source_cursor: tuple[int, int, str, str] | None = None
        if cursor:
            decoded = _cursor_decode(cursor)
            if decoded.get("v") != 2 or decoded.get("kind") != "sources":
                raise ValueError("news_sources_cursor_invalid")
            try:
                role_order = int(decoded["role_order"])
                tier = int(decoded["tier"])
                name = str(decoded["name"])
                source_id = str(decoded["source_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("news_sources_cursor_invalid") from exc
            if role_order not in {0, 1}:
                raise ValueError("news_sources_cursor_invalid")
            source_cursor = (role_order, tier, name, source_id)
        sources_query = query_specs.sources_query(limit=limit, cursor=source_cursor)
        rows = self.conn.execute(
            sources_query.sql,
            sources_query.params,
        ).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _cursor_encode(
                {
                    "v": 2,
                    "kind": "sources",
                    "role_order": 0 if str(last["source_kind"]) == "opennews" else 1,
                    "tier": int(last["tier"]),
                    "name": str(last["name"]),
                    "source_id": str(last["source_id"]),
                }
            )
        items = [dict(row) for row in page]
        opennews_items = [item for item in items if str(item["source_kind"]) == "opennews"]
        incidents = (
            [
                dict(row)
                for row in self.conn.execute(
                    query_specs.status_opennews_incidents_query().sql,
                ).fetchall()
            ]
            if opennews_items
            else []
        )
        unresolved_incident_count = sum(
            incident["recovery_status"] not in {"recovered", "not_required"} for incident in incidents
        )
        for item in items:
            is_opennews = str(item["source_kind"]) == "opennews"
            item["incidents"] = incidents if is_opennews else []
            item["unresolved_incident_count"] = unresolved_incident_count if is_opennews else 0
        return {
            "items": items,
            "page": {
                "returned_count": len(page),
                "has_more": has_more,
                "next_cursor": next_cursor,
            },
        }

    # World Brief ---------------------------------------------------------------

    def peek_brief_candidate(self, *, now_ms: int) -> dict[str, Any] | None:
        return brief_store.peek_brief_candidate(self, now_ms=now_ms)

    def prepare_brief_run(
        self,
        *,
        slot_at_ms: int,
        lease_owner: str,
        lease_token: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        return brief_store.prepare_brief_run(
            self,
            slot_at_ms=slot_at_ms,
            lease_owner=lease_owner,
            lease_token=lease_token,
            now_ms=now_ms,
        )

    def start_brief_model(
        self,
        *,
        slot_at_ms: int,
        lease_owner: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        return brief_store.start_brief_model(
            self,
            slot_at_ms=slot_at_ms,
            lease_owner=lease_owner,
            lease_token=lease_token,
            now_ms=now_ms,
        )

    def release_brief_claim(
        self,
        *,
        slot_at_ms: int,
        lease_owner: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        return brief_store.release_brief_claim(
            self,
            slot_at_ms=slot_at_ms,
            lease_owner=lease_owner,
            lease_token=lease_token,
            now_ms=now_ms,
        )

    def publish_brief(
        self,
        *,
        claim: Mapping[str, Any],
        result: NewsBriefSynthesisResult,
        now_ms: int,
    ) -> str | None:
        return brief_store.publish_brief(
            self,
            claim=claim,
            result=result,
            now_ms=now_ms,
        )

    def get_brief(self, *, now_ms: int) -> dict[str, Any]:
        return brief_store.get_brief(self, now_ms=now_ms)

    # News Item push -----------------------------------------------------------

    def reconcile_item_push(
        self,
        *,
        delivery_available: bool,
        now_ms: int,
    ) -> dict[str, int | bool | None]:
        row = self.conn.execute(
            """
            WITH current_state AS MATERIALIZED (
              SELECT delivery_available, enablement_epoch_at_ms
                FROM news_push_state
               WHERE singleton_key = 'current'
               FOR UPDATE
            ), interrupted AS (
              UPDATE news_push_deliveries
                 SET status = 'terminal',
                     last_error = 'news_item_push_interrupted_unknown',
                     updated_at_ms = greatest(updated_at_ms, %(now_ms)s)
               WHERE status = 'sending'
                 AND source_title_fingerprint IS NOT NULL
                 AND source_payload ->> 'schema_version' = 'news_item_push_v2'
              RETURNING item_id
            ), interrupted_count AS MATERIALIZED (
              SELECT count(*)::bigint AS value FROM interrupted
            ), changed AS (
              UPDATE news_push_state state
                 SET delivery_available = %(delivery_available)s,
                     enablement_epoch_at_ms = CASE
                       WHEN NOT %(delivery_available)s THEN NULL
                       WHEN current_state.delivery_available
                         AND current_state.enablement_epoch_at_ms IS NOT NULL
                         THEN current_state.enablement_epoch_at_ms
                       ELSE %(now_ms)s
                     END,
                     sending_count = sending_count - interrupted_count.value,
                     terminal_count = terminal_count + interrupted_count.value,
                     latest_error = CASE
                       WHEN interrupted_count.value > 0
                         THEN 'news_item_push_interrupted_unknown'
                       ELSE latest_error
                     END,
                     latest_error_at_ms = CASE
                       WHEN interrupted_count.value > 0 THEN %(now_ms)s
                       ELSE latest_error_at_ms
                     END,
                     updated_at_ms = greatest(state.updated_at_ms, %(now_ms)s)
                FROM current_state, interrupted_count
               WHERE state.singleton_key = 'current'
              RETURNING state.delivery_available,
                        state.enablement_epoch_at_ms,
                        (
                          state.delivery_available
                            IS DISTINCT FROM current_state.delivery_available
                          OR state.enablement_epoch_at_ms
                            IS DISTINCT FROM current_state.enablement_epoch_at_ms
                        ) AS availability_changed,
                        interrupted_count.value AS terminalized
            )
            SELECT * FROM changed
            """,
            {
                "delivery_available": bool(delivery_available),
                "now_ms": int(now_ms),
            },
        ).fetchone()
        if row is None:
            raise RuntimeError("news_push_state_missing")
        return {
            "delivery_available": bool(row["delivery_available"]),
            "enablement_epoch_at_ms": (
                int(row["enablement_epoch_at_ms"]) if row["enablement_epoch_at_ms"] is not None else None
            ),
            "availability_changed": bool(row["availability_changed"]),
            "terminalized": int(row["terminalized"]),
        }

    def peek_item_push(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT delivery.item_id, delivery.source_payload,
                   delivery.source_title_fingerprint,
                   delivery.live_observed_at_ms, delivery.created_at_ms,
                   jsonb_build_object(
                     'display_title', presentation.display_title,
                     'original_title', presentation.original_title,
                     'outcome', presentation.outcome,
                     'provider', presentation.provider,
                     'policy_version', presentation.policy_version,
                     'fallback_code', presentation.fallback_code,
                     'duration_ms', presentation.duration_ms
                   ) AS presentation
              FROM news_push_deliveries delivery
              JOIN news_item_title_presentations presentation
                ON presentation.item_id = delivery.item_id
               AND presentation.source_title_fingerprint =
                   delivery.source_title_fingerprint
               AND presentation.state = 'resolved'
             WHERE delivery.status = 'pending'
               AND delivery.source_title_fingerprint IS NOT NULL
               AND delivery.source_payload ->> 'schema_version' = 'news_item_push_v2'
               AND delivery.admission_reason = 'exact_atom_leader'
             ORDER BY delivery.live_observed_at_ms, delivery.item_id
             LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def fence_item_push(
        self,
        *,
        item_id: str,
        attempted_at_ms: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            WITH changed AS (
              UPDATE news_push_deliveries
                 SET status = 'sending',
                     attempted_at_ms = %(attempted_at_ms)s,
                     updated_at_ms = greatest(updated_at_ms, %(attempted_at_ms)s)
               WHERE item_id = %(item_id)s
                 AND status = 'pending'
                 AND source_title_fingerprint IS NOT NULL
                 AND source_payload ->> 'schema_version' = 'news_item_push_v2'
                 AND admission_reason = 'exact_atom_leader'
              RETURNING item_id, source_payload, source_title_fingerprint
            ), summary AS (
              UPDATE news_push_state
                 SET pending_count = pending_count - 1,
                     sending_count = sending_count + 1,
                     updated_at_ms = greatest(updated_at_ms, %(attempted_at_ms)s)
               WHERE singleton_key = 'current'
                 AND EXISTS (SELECT 1 FROM changed)
              RETURNING singleton_key
            )
            SELECT changed.*,
                   (SELECT count(*) FROM summary) AS summary_writes
              FROM changed
            """,
            {
                "item_id": str(item_id),
                "attempted_at_ms": int(attempted_at_ms),
            },
        ).fetchone()
        return dict(row) if row is not None else None

    def complete_item_push(
        self,
        *,
        item_id: str,
        receipt: Mapping[str, Any],
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            WITH changed AS (
              UPDATE news_push_deliveries
                 SET status = 'sent',
                     receipt = %(receipt)s,
                     sent_at_ms = %(now_ms)s,
                     updated_at_ms = greatest(updated_at_ms, %(now_ms)s)
               WHERE item_id = %(item_id)s
                 AND status = 'sending'
                 AND source_title_fingerprint IS NOT NULL
                 AND source_payload ->> 'schema_version' = 'news_item_push_v2'
                 AND admission_reason = 'exact_atom_leader'
              RETURNING item_id
            ), summary AS (
              UPDATE news_push_state
                 SET sending_count = sending_count - 1,
                     sent_count = sent_count + 1,
                     latest_sent_at_ms = CASE
                       WHEN EXISTS (SELECT 1 FROM changed)
                         THEN greatest(coalesce(latest_sent_at_ms, 0), %(now_ms)s)
                       ELSE latest_sent_at_ms
                     END,
                     updated_at_ms = greatest(updated_at_ms, %(now_ms)s)
               WHERE singleton_key = 'current'
                 AND EXISTS (SELECT 1 FROM changed)
              RETURNING singleton_key
            )
            SELECT count(*) AS changed,
                   (SELECT count(*) FROM summary) AS summary_writes
              FROM changed
            """,
            {
                "item_id": str(item_id),
                "receipt": Jsonb(dict(receipt)),
                "now_ms": int(now_ms),
            },
        ).fetchone()
        return bool(row is not None and int(row["changed"]) == 1)

    def terminalize_item_push(
        self,
        *,
        item_id: str,
        error_code: str,
        now_ms: int,
    ) -> bool:
        normalized_error = str(error_code or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{1,120}", normalized_error):
            raise ValueError("news_item_push_error_invalid")
        row = self.conn.execute(
            """
            WITH changed AS (
              UPDATE news_push_deliveries
                 SET status = 'terminal',
                     last_error = %(error_code)s,
                     updated_at_ms = greatest(updated_at_ms, %(now_ms)s)
               WHERE item_id = %(item_id)s
                 AND status = 'sending'
                 AND source_title_fingerprint IS NOT NULL
                 AND source_payload ->> 'schema_version' = 'news_item_push_v2'
                 AND admission_reason = 'exact_atom_leader'
              RETURNING item_id
            ), summary AS (
              UPDATE news_push_state
                 SET sending_count = sending_count - 1,
                     terminal_count = terminal_count + 1,
                     latest_error = %(error_code)s,
                     latest_error_at_ms = %(now_ms)s,
                     updated_at_ms = greatest(updated_at_ms, %(now_ms)s)
               WHERE singleton_key = 'current'
                 AND EXISTS (SELECT 1 FROM changed)
              RETURNING singleton_key
            )
            SELECT count(*) AS changed,
                   (SELECT count(*) FROM summary) AS summary_writes
              FROM changed
            """,
            {
                "error_code": normalized_error,
                "item_id": str(item_id),
                "now_ms": int(now_ms),
            },
        ).fetchone()
        return bool(row is not None and int(row["changed"]) == 1)

    def story_provider_evidence(
        self,
        *,
        story_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        if not story_ids:
            return {}
        contexts_query = query_specs.story_provider_evidence_query(story_ids=story_ids)
        rows = self.conn.execute(
            contexts_query.sql,
            contexts_query.params,
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            story_id = str(row["story_id"])
            result[story_id] = {
                "provider_evidence": (
                    {
                        "item_id": str(row["item_id"]),
                        "url": (str(row["canonical_url"]) if row["canonical_url"] else None),
                        "provider_metadata": dict(row["provider_metadata"] or {}),
                    }
                    if row["item_id"] is not None
                    else None
                )
            }
        return result

    def push_health_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        state_query = query_specs.push_state_query()
        state = self.conn.execute(state_query.sql, state_query.params).fetchone()
        if state is None:
            raise RuntimeError("news_push_state_missing")
        oldest_query = query_specs.push_oldest_pending_query()
        oldest = self.conn.execute(
            oldest_query.sql,
            oldest_query.params,
        ).fetchone()
        suppression_query = query_specs.push_suppression_samples_query()
        suppression_rows = self.conn.execute(
            suppression_query.sql,
            suppression_query.params,
        ).fetchall()
        suppression_sample_complete = len(suppression_rows) <= query_specs.SUPPRESSION_SAMPLE_LIMIT
        recent_suppressions = [
            {
                "item_id": str(row["item_id"]),
                "suppressed_by_item_id": str(row["suppressed_by_item_id"]),
                "notification_fingerprint": str(row["notification_fingerprint"]),
                "provider_published_at_ms": int(row["provider_published_at_ms"]),
                "adjudicated_at_ms": int(row["adjudicated_at_ms"]),
                "admission_reason": str(row["admission_reason"]),
            }
            for row in suppression_rows[: query_specs.SUPPRESSION_SAMPLE_LIMIT]
        ]
        return {
            "payload_schema_version": NEWS_PUSH_PAYLOAD_SCHEMA_VERSION,
            "comparison_identity_version": EXACT_ATOM_IDENTITY_VERSION,
            "admission_policy_version": NEWS_PUSH_ADMISSION_POLICY_VERSION,
            "delivery_available": bool(state["delivery_available"]),
            "enablement_epoch_at_ms": (
                int(state["enablement_epoch_at_ms"]) if state["enablement_epoch_at_ms"] is not None else None
            ),
            "total_count": int(state["total_count"] or 0),
            "suppressed_count": int(state["suppressed_count"] or 0),
            "pending_count": int(state["pending_count"] or 0),
            "sending_count": int(state["sending_count"] or 0),
            "sent_count": int(state["sent_count"] or 0),
            "terminal_count": int(state["terminal_count"] or 0),
            "oldest_pending_at_ms": (int(oldest["live_observed_at_ms"]) if oldest is not None else None),
            "latest_sent_at_ms": (int(state["latest_sent_at_ms"]) if state["latest_sent_at_ms"] is not None else None),
            "latest_error": (
                _public_push_error(str(state["latest_error"])) if state["latest_error"] is not None else None
            ),
            "latest_error_at_ms": (
                int(state["latest_error_at_ms"]) if state["latest_error_at_ms"] is not None else None
            ),
            "delivery_24h": self._push_delivery_24h_snapshot(now_ms=now_ms),
            "recent_suppressions": recent_suppressions,
            "suppression_sample_complete": suppression_sample_complete,
            "measured_at_ms": int(now_ms),
        }

    def title_presentation_health_snapshot(
        self,
        *,
        now_ms: int,
        deepl_configured: bool,
        deepl_key_count: int,
        deepseek_configured: bool,
    ) -> dict[str, Any]:
        state_query = query_specs.title_presentation_state_query()
        state = self.conn.execute(state_query.sql, state_query.params).fetchone()
        if state is None:
            raise RuntimeError("news_title_presentation_state_missing")
        sample = self._title_presentation_24h_snapshot(now_ms=now_ms)
        oldest_push_blocking_at_ms = (
            int(state["oldest_push_blocking_at_ms"]) if state["oldest_push_blocking_at_ms"] is not None else None
        )
        oldest_resolving_at_ms = (
            int(state["oldest_resolving_at_ms"]) if state["oldest_resolving_at_ms"] is not None else None
        )
        reasons: list[str] = []
        if not deepl_configured and not deepseek_configured:
            reasons.append("news_title_presentation_provider_unconfigured")
        if oldest_push_blocking_at_ms is not None and now_ms - oldest_push_blocking_at_ms > 15_000:
            reasons.append("news_title_presentation_push_blocking_slo_breached")
        if oldest_resolving_at_ms is not None and now_ms - oldest_resolving_at_ms > 7_000:
            reasons.append("news_title_presentation_resolving_stale")
        if not bool(sample["sample_complete"]):
            reasons.append("news_title_presentation_sample_overflow")
        return {
            "status": "degraded" if reasons else "ready",
            "reasons": reasons,
            "deepl_configured": bool(deepl_configured),
            "deepl_key_count": int(deepl_key_count),
            "deepseek_configured": bool(deepseek_configured),
            "policy_version": TITLE_PRESENTATION_POLICY_VERSION,
            "deepl_deadline_ms": round(DEEPL_DEADLINE_SECONDS * 1_000),
            "deepseek_deadline_ms": round(DEEPSEEK_DEADLINE_SECONDS * 1_000),
            "pending_count": int(state["pending_count"] or 0),
            "resolving_count": int(state["resolving_count"] or 0),
            "oldest_pending_at_ms": (
                int(state["oldest_pending_at_ms"]) if state["oldest_pending_at_ms"] is not None else None
            ),
            "oldest_push_blocking_at_ms": oldest_push_blocking_at_ms,
            "oldest_resolving_at_ms": oldest_resolving_at_ms,
            "resolution_24h": sample,
            "measured_at_ms": int(now_ms),
        }

    def _title_presentation_24h_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        query = query_specs.title_presentation_samples_query(now_ms=now_ms)
        rows = self.conn.execute(query.sql, query.params).fetchall()
        if len(rows) > query_specs.SLO_SAMPLE_LIMIT:
            return _incomplete_title_presentation_sample()
        outcomes = Counter(str(row["outcome"] or "fallback") for row in rows)
        providers = Counter(str(row["provider"]) for row in rows if row["provider"] is not None)
        durations = [
            int(row["duration_ms"]) for row in rows if row["duration_ms"] is not None and int(row["duration_ms"]) >= 0
        ]
        fallback_counts: Counter[str] = Counter()
        for row in rows:
            if str(row["outcome"]) != "fallback":
                continue
            fallback_counts[
                _public_push_error(str(row["fallback_code"] or "news_title_presentation_provider_failed"))
            ] += 1
        return {
            "total": len(rows),
            "attempted": sum(row["attempted_at_ms"] is not None for row in rows),
            "translated": outcomes["translated"],
            "not_needed": outcomes["not_needed"],
            "fallback": outcomes["fallback"],
            "provider_counts": dict(sorted(providers.items())),
            "latency_p95_ms": _percentile_cont_95_ms(durations),
            "fallback_counts": dict(sorted(fallback_counts.items())),
            "sample_complete": True,
        }

    def _push_delivery_24h_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        query = query_specs.push_delivery_samples_query(now_ms=now_ms)
        rows = self.conn.execute(query.sql, query.params).fetchall()
        if len(rows) > query_specs.SLO_SAMPLE_LIMIT:
            return _incomplete_delivery_sample()
        latencies = [
            int(row["latency_ms"])
            for row in rows
            if row["status"] == "sent" and row["latency_ms"] is not None and int(row["latency_ms"]) >= 0
        ]
        latency_p95_ms = _percentile_cont_95_ms(latencies)
        return {
            "completed": len(rows),
            "sent": sum(str(row["status"]) == "sent" for row in rows),
            "terminal": sum(str(row["status"]) == "terminal" for row in rows),
            "latency_p95_ms": latency_p95_ms,
            "slo_met": (latency_p95_ms <= 15_000 if latency_p95_ms is not None else None),
            "sample_complete": True,
        }

    # Health -------------------------------------------------------------------

    def realtime_status_snapshot(
        self,
        *,
        now_ms: int,
        configured_strategy_count: int,
    ) -> dict[str, Any]:
        if configured_strategy_count < 0:
            raise ValueError("news_configured_strategy_count_invalid")
        opennews_query = query_specs.status_opennews_query()
        opennews = self.conn.execute(
            opennews_query.sql,
            opennews_query.params,
        ).fetchone()
        return self._realtime_status_from_opennews(
            opennews,
            now_ms=now_ms,
            configured_strategy_count=configured_strategy_count,
        )

    def health_snapshot(
        self,
        *,
        now_ms: int,
        rss_enabled: bool,
        configured_strategy_count: int,
        push_requested: bool = False,
        push_delivery_available: bool = False,
        push_unavailable_reason: str | None = None,
        feishu_webhook_url_configured: bool = False,
        feishu_signing_secret_configured: bool = False,
        title_deepl_configured: bool = False,
        title_deepl_key_count: int = 0,
        title_deepseek_configured: bool = False,
        workers_state: str | None = None,
        workers_reason: str | None = None,
    ) -> dict[str, Any]:
        if workers_state not in {None, "running", "recovering", "stalled"}:
            raise ValueError("news_workers_state_invalid")
        if configured_strategy_count < 0:
            raise ValueError("news_configured_strategy_count_invalid")
        opennews_query = query_specs.status_opennews_query()
        opennews = self.conn.execute(
            opennews_query.sql,
            opennews_query.params,
        ).fetchone()
        incidents_query = query_specs.status_opennews_incidents_query()
        incident_rows = self.conn.execute(
            incidents_query.sql,
            incidents_query.params,
        ).fetchall()
        realtime = self._realtime_status_from_opennews(
            opennews,
            now_ms=now_ms,
            configured_strategy_count=configured_strategy_count,
        )
        rss_query = query_specs.status_rss_query()
        rss_rows = self.conn.execute(rss_query.sql, rss_query.params).fetchall()
        projection_query = query_specs.status_projection_query()
        story = self.conn.execute(
            projection_query.sql,
            projection_query.params,
        ).fetchone()
        brief = self.get_brief(now_ms=now_ms)
        ingest_reasons: list[str] = []
        opennews_payload = (
            {
                **dict(opennews),
                "configured_strategy_count": int(configured_strategy_count),
                "unresolved_incident_count": sum(
                    not bool(row["planned"]) and str(row["recovery_status"]) not in {"recovered", "not_required"}
                    for row in incident_rows
                ),
                "incidents": [dict(row) for row in incident_rows],
            }
            if opennews is not None
            else None
        )
        next_due_values = [int(row["next_fetch_at_ms"]) for row in rss_rows if row["next_fetch_at_ms"] is not None]
        success_values = [int(row["last_success_at_ms"]) for row in rss_rows if row["last_success_at_ms"] is not None]
        rss_payload = {
            "enabled": bool(rss_enabled),
            "source_count": len(rss_rows),
            "successful_source_count": sum(row["last_success_at_ms"] is not None for row in rss_rows),
            "failed_source_count": sum(row["last_error"] is not None for row in rss_rows),
            "claimed_source_count": sum(row["claim_token"] is not None for row in rss_rows),
            "next_due_at_ms": min(next_due_values, default=None),
            "latest_success_at_ms": max(success_values, default=None),
        }
        if opennews_payload is None or configured_strategy_count == 0:
            ingest_status = "degraded"
            ingest_reasons.append("opennews_strategy_missing")
        else:
            if opennews_payload["last_error"] is not None:
                ingest_reasons.append("opennews_strategy_transport_error")
            if not bool(opennews_payload["live_connected"]):
                ingest_reasons.append("opennews_strategy_disconnected")
            if any(
                reason
                in {
                    "opennews_strategy_transport_error",
                    "opennews_strategy_disconnected",
                }
                for reason in ingest_reasons
            ):
                ingest_status = "degraded"
            elif opennews_payload["last_accepted_strategy_trigger_at_ms"] is None:
                ingest_status = "warming"
                ingest_reasons.append("opennews_strategy_no_trigger_yet")
            else:
                ingest_status = "ready"

        if rss_enabled:
            if rss_payload["source_count"] == 0:
                ingest_reasons.append("public_rss_corroboration_catalog_empty")
            elif rss_payload["successful_source_count"] == 0:
                ingest_reasons.append(
                    "public_rss_corroboration_unavailable"
                    if rss_payload["failed_source_count"]
                    else "public_rss_corroboration_warming"
                )
            elif rss_payload["failed_source_count"]:
                ingest_reasons.append("public_rss_corroboration_partial_failures")

        invalid_owners = int(story["invalid_owner_count"] or 0)
        invalid_aggregates = int(story["invalid_story_aggregate_count"] or 0)
        active_stories = int(story["active_count"] or 0)
        story_last_success_at_ms = int(story["last_success_at_ms"] or 0) or None
        story_reasons: list[str] = []
        if invalid_owners:
            story_reasons.append("current_item_owner_invalid")
        if invalid_aggregates:
            story_reasons.append("story_aggregate_invalid")
        if story["last_error"] is not None:
            story_reasons.append(str(story["last_error"]))

        runtime_stalled = workers_state == "stalled"
        runtime_recovering = workers_state == "recovering"
        if workers_reason is not None and workers_state in {"recovering", "stalled"}:
            story_reasons.append(str(workers_reason))

        if runtime_stalled or invalid_owners or invalid_aggregates or story["last_error"] is not None:
            story_status = "degraded"
        elif runtime_recovering or active_stories == 0:
            story_status = "warming"
            if active_stories == 0:
                story_reasons.append("no_active_stories_yet")
        else:
            story_status = "ready"

        brief_status = (
            "ready"
            if brief["state"] == "current"
            else "degraded"
            if brief["state"] in {"degraded", "last_known_good"}
            else "warming"
        )
        brief_reasons = [] if brief_status == "ready" else [f"public_brief_{brief['state']}"]
        push_snapshot = self.push_health_snapshot(now_ms=now_ms)
        delivery_24h = push_snapshot["delivery_24h"]
        push_reasons: list[str] = []
        state_synchronized = bool(push_snapshot["delivery_available"]) == bool(push_delivery_available)
        if push_requested:
            if push_unavailable_reason is not None:
                push_reasons.append(str(push_unavailable_reason))
            if not state_synchronized:
                push_reasons.append("news_item_push_availability_unsynchronized")
            if push_delivery_available and push_snapshot["enablement_epoch_at_ms"] is None:
                push_reasons.append("news_item_push_enablement_epoch_missing")
            if int(delivery_24h["terminal"] or 0) > 0:
                push_reasons.append("news_item_push_recent_terminal")
            if delivery_24h["slo_met"] is False:
                push_reasons.append("news_item_push_delivery_latency_slo_breached")
            if not bool(delivery_24h["sample_complete"]):
                push_reasons.append("news_item_push_delivery_sample_overflow")
            push_status = "degraded" if push_reasons else "ready"
        else:
            push_status = "disabled"
        push_payload = {
            **push_snapshot,
            "status": push_status,
            "reasons": push_reasons,
            "requested": bool(push_requested),
            "delivery_available": bool(push_delivery_available),
            "availability_reason": push_unavailable_reason,
            "state_synchronized": state_synchronized,
            "feishu_webhook_url_configured": (bool(feishu_webhook_url_configured)),
            "feishu_signing_secret_configured": (bool(feishu_signing_secret_configured)),
        }
        layers = {
            "ingest": {
                "status": ingest_status,
                "reasons": ingest_reasons,
                "rss": rss_payload,
                "opennews": opennews_payload,
            },
            "story": {
                "status": story_status,
                "reasons": story_reasons,
                "active_items": int(story["active_item_count"] or 0),
                "active_stories": active_stories,
                "newest_item_at_ms": story["newest_item_at_ms"],
                "newest_story_at_ms": story["newest_story_at_ms"],
                "last_material_change_at_ms": story["last_material_change_at_ms"],
                "invalid_owner_count": invalid_owners,
                "invalid_story_aggregate_count": invalid_aggregates,
                "invariant_error_count": invalid_owners + invalid_aggregates,
                "identity_version": STORY_IDENTITY_VERSION,
                "classifier_version": CLASSIFIER_VERSION,
                "importance_version": IMPORTANCE_VERSION,
                "last_attempt_at_ms": story["last_attempt_at_ms"],
                "last_success_at_ms": story_last_success_at_ms,
                "last_error": story["last_error"],
            },
            "brief": {
                "status": brief_status,
                "reasons": brief_reasons,
                "public_state": brief["state"],
                "slot_at_ms": brief["slot_at_ms"],
                "next_due_at_ms": brief["next_due_at_ms"],
                "publication_id": (brief["publication"]["publication_id"] if brief["publication"] else None),
                "latest_run": brief["latest_run"],
            },
            "push": push_payload,
        }
        statuses = [
            ingest_status,
            story_status,
            brief_status,
        ]
        if push_requested:
            statuses.append(push_status)
        overall = "degraded" if "degraded" in statuses else "warming" if "warming" in statuses else "ready"
        operating_state = "stalled" if runtime_stalled else "recovering" if overall != "ready" else "live"
        title_presentation = self.title_presentation_health_snapshot(
            now_ms=now_ms,
            deepl_configured=title_deepl_configured,
            deepl_key_count=title_deepl_key_count,
            deepseek_configured=title_deepseek_configured,
        )
        return {
            "status": overall,
            "operating_state": operating_state,
            "last_success_at_ms": story_last_success_at_ms,
            "reasons": [f"{name}:{reason}" for name, details in layers.items() for reason in details["reasons"]],
            "realtime": realtime,
            "layers": layers,
            "title_presentation": title_presentation,
            "measured_at_ms": now_ms,
        }

    def _realtime_status_from_opennews(
        self,
        opennews: Any,
        *,
        now_ms: int,
        configured_strategy_count: int,
    ) -> dict[str, Any]:
        inbound_latency = self._realtime_latency_snapshot(
            query_specs.status_inbound_latency_query(now_ms=now_ms),
            now_ms=now_ms,
        )
        story_visible_latency = self._realtime_latency_snapshot(
            query_specs.status_story_latency_query(now_ms=now_ms),
            now_ms=now_ms,
        )
        return {
            "wss_state": (
                "unavailable"
                if (
                    opennews is None
                    or configured_strategy_count == 0
                    or opennews["last_error"] in {"opennews_authentication_failed", "opennews_token_missing"}
                )
                else "connected"
                if bool(opennews["live_connected"])
                else "reconnecting"
            ),
            "connected_at_ms": opennews["last_connected_at_ms"] if opennews is not None else None,
            "disconnected_at_ms": opennews["last_disconnected_at_ms"] if opennews is not None else None,
            "inbound_latency": inbound_latency,
            "story_visible_latency": story_visible_latency,
        }

    def _realtime_latency_snapshot(
        self,
        query: Any,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        rows = self.conn.execute(query.sql, query.params).fetchall()
        values = sorted(
            int(row["latency_ms"])
            for row in rows[: query_specs.SLO_SAMPLE_LIMIT]
            if row["latency_ms"] is not None and int(row["latency_ms"]) >= 0
        )
        return {
            "p50_ms": _percentile_cont_ms(values, 0.50),
            "p95_ms": _percentile_cont_ms(values, 0.95),
            "sample_count": len(values),
            "window_started_at_ms": int(now_ms) - 60 * 60 * 1_000,
            "measured_at_ms": int(now_ms),
        }


def _story_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "story_id": str(row["story_id"]),
        "title": str(row["display_title"]),
        "original_title": str(row["representative_title"]),
        "description": str(row["representative_description"]),
        "url": str(row["representative_url"]) if row["representative_url"] else None,
        "source_id": str(row["representative_source_id"]),
        "source_name": str(row["representative_source_name"]),
        "representative_item_id": str(row["representative_item_id"]),
        "scoring_item_id": str(row["scoring_item_id"]),
        "level": str(row["level"]),
        "category": str(row["category"]),
        "importance_score": int(row["importance_score"]),
        "importance_factors": dict(row["importance_factors"]),
        "item_count": int(row["item_count"]),
        "source_count": int(row["source_count"]),
        "first_published_at_ms": int(row["first_published_at_ms"]),
        "last_published_at_ms": int(row["last_published_at_ms"]),
    }


def _public_provider_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    public = {key: value[key] for key in ("score", "source", "signal", "grade") if key in value}
    coins = value.get("coins")
    if isinstance(coins, list):
        assets = [
            {key: coin[key] for key in ("symbol", "market_type", "match", "score", "signal", "grade") if key in coin}
            for coin in coins
            if isinstance(coin, Mapping)
            and isinstance(coin.get("symbol"), str)
            and str(coin["symbol"]).strip()
            and isinstance(coin.get("market_type"), str)
            and str(coin["market_type"]).strip()
        ]
        if assets:
            public["assets"] = assets
    return public


def _public_provider_evidence(selected: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if selected is None:
        return None
    evidence = selected.get("provider_evidence")
    if not isinstance(evidence, Mapping):
        return None
    return {
        "item_id": str(evidence["item_id"]),
        "url": str(evidence["url"]) if evidence.get("url") else None,
        "provider_metadata": _public_provider_metadata(evidence.get("provider_metadata")),
    }


def _public_push_error(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9_]{1,120}", normalized):
        return normalized
    return "news_item_push_delivery_error"


def _public_opennews_error(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if re.fullmatch(r"[a-z0-9_]{1,120}", normalized):
        return normalized
    return "opennews_unknown"


def _opennews_incident_cause(error_code: str | None) -> str:
    return {
        "opennews_connect_failed": "network_connect",
        "opennews_authentication_failed": "authentication",
        "opennews_handshake_failed": "protocol_error",
        "opennews_receive_failed": "provider_close",
        "opennews_idle_timeout": "idle_timeout",
        "opennews_protocol_error": "protocol_error",
        "opennews_frame_invalid": "protocol_error",
        "opennews_buffer_overflow": "buffer_overflow",
        "opennews_process_outage": "process_outage",
    }.get(error_code or "", "unknown")


def _item_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_id": str(row["item_id"]),
        "provider_record_id": (str(row["provider_record_id"]) if row.get("provider_record_id") is not None else None),
        "provider_metadata": _public_provider_metadata(row.get("provider_metadata")),
        "source_id": str(row["source_id"]),
        "source_name": str(row["source_name"]),
        "reporting_origin": str(row["reporting_origin"]),
        "tier": int(row["tier"]),
        "title": str(row["display_title"]),
        "original_title": str(row["title"]),
        "description": str(row["description"]),
        "url": str(row["canonical_url"]) if row["canonical_url"] else None,
        "lang": str(row["lang"]),
        "published_at_ms": int(row["published_at_ms"]),
        "last_observed_at_ms": int(row["last_observed_at_ms"]),
        "level": str(row["level"]),
        "category": str(row["category"]),
        "importance_score": int(row["importance_score"]),
        "importance_factors": dict(row["importance_factors"]),
    }


__all__ = ["NewsRepository", "deterministic_id"]
