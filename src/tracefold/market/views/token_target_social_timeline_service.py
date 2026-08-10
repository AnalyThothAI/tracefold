from __future__ import annotations

import time
from typing import Any

from tracefold.market.pricing.message_price_payload import message_price_payload
from tracefold.market.windows import PRODUCT_WINDOW_MS
from tracefold.platform.validation import require_nonnegative_int

from .token_target_cursor import decode_target_cursor, encode_target_cursor
from .token_target_post_serializer import token_target_post_payload
from .token_target_stage_builder import build_token_target_stages


class TokenTargetSocialTimelineWindowError(ValueError):
    pass


class TokenTargetSocialTimelineService:
    def __init__(self, *, targets: Any, market_candles: Any | None = None) -> None:
        self.targets = targets
        self.market_candles = market_candles

    def timeline(
        self,
        *,
        target_type: str,
        target_id: str,
        window: str,
        limit: int,
        cursor: str | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        row_limit = require_nonnegative_int(
            limit,
            error_code="token_target_social_timeline_limit_required",
        )
        resolved_now_ms = int(now_ms or time.time() * 1000)
        window_ms = _window_ms(window)
        fetch_limit = row_limit + 1
        rows = self.targets.timeline_rows(
            target_type=target_type,
            target_id=target_id,
            since_ms=resolved_now_ms - window_ms,
            limit=fetch_limit,
            cursor=decode_target_cursor(cursor),
        )
        page_rows = rows[:row_limit]
        bucket_ms, bucket_label = _bucket(window)
        has_more = len(rows) > len(page_rows)
        next_cursor = encode_target_cursor(page_rows[-1]) if has_more and page_rows else None
        stage_build = build_token_target_stages(page_rows)
        market_candles = _market_candles(page_rows)
        if self.market_candles is not None:
            market_candles = self.market_candles.enrich_market_candles(market_candles, window=window)
        return {
            "query": {
                "target_type": target_type,
                "target_id": target_id,
                "window": window,
                "bucket": bucket_label,
            },
            "summary": _summary(page_rows),
            "market_candles": market_candles,
            "stages": stage_build.stages,
            "buckets": _buckets(
                page_rows,
                bucket_ms=bucket_ms,
                since_ms=resolved_now_ms - window_ms,
                now_ms=resolved_now_ms,
            ),
            "authors": _authors(page_rows),
            "posts": [
                token_target_post_payload(
                    row,
                    stage=stage_build.annotations.get(str(row.get("event_id") or "")),
                    bucket_ms=bucket_ms,
                    since_ms=resolved_now_ms - window_ms,
                )
                for row in page_rows
            ],
            "cascade": {"edges": [], "unresolved_parents": []},
            "returned_count": len(page_rows),
            "has_more": has_more,
            "next_cursor": next_cursor,
        }


def _window_ms(window: str) -> int:
    try:
        return PRODUCT_WINDOW_MS[window]
    except KeyError as exc:
        raise TokenTargetSocialTimelineWindowError(window) from exc


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    authors = {str(row.get("author_handle") or "") for row in rows if row.get("author_handle")}
    times = [int(row.get("received_at_ms") or 0) for row in rows]
    return {
        "posts": len(rows),
        "authors": len(authors),
        "effective_authors": len(authors),
        "first_seen_ms": min(times) if times else None,
        "latest_seen_ms": max(times) if times else None,
        "phase": _phase(len(rows), len(authors)),
        "top_author_share": _top_author_share(rows),
        "duplicate_text_share": 0.0,
        "peak_posts_per_bucket": 0,
        "peak_new_authors_per_bucket": 0,
        "reproduction_rate": None,
    }


def _market_candles(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        target_type = row.get("target_type")
        if target_type == "Asset":
            return {
                "target_type": "Asset",
                "target_id": row.get("target_id"),
                "chain_id": row.get("chain_id"),
                "address": row.get("address"),
                "symbol": row.get("symbol"),
                "pricefeed_id": row.get("pricefeed_id"),
            }
        if target_type == "CexToken":
            return {
                "target_type": "CexToken",
                "target_id": row.get("target_id"),
                "provider": row.get("provider"),
                "native_market_id": row.get("native_market_id"),
                "symbol": row.get("symbol"),
                "quote_symbol": row.get("quote_symbol"),
                "feed_type": row.get("feed_type"),
                "pricefeed_id": row.get("pricefeed_id"),
            }
    return None


def _bucket(window: str) -> tuple[int, str]:
    if window == "5m":
        return 30 * 1000, "30s"
    if window == "1h":
        return 5 * 60 * 1000, "5m"
    if window == "4h":
        return 15 * 60 * 1000, "15m"
    if window == "24h":
        return 60 * 60 * 1000, "1h"
    raise TokenTargetSocialTimelineWindowError(window)


def _buckets(rows: list[dict[str, Any]], *, bucket_ms: int, since_ms: int, now_ms: int) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        received_at_ms = int(row.get("received_at_ms") or 0)
        start_ms = since_ms + ((received_at_ms - since_ms) // bucket_ms) * bucket_ms
        bucket = grouped.setdefault(
            start_ms,
            {
                "start_ms": start_ms,
                "end_ms": min(start_ms + bucket_ms, now_ms),
                "posts": 0,
                "authors": set(),
                "new_authors": 0,
                "duplicate_text_share": 0.0,
                "price": None,
                "price_change_from_start_pct": None,
            },
        )
        bucket["posts"] += 1
        if row.get("author_handle"):
            bucket["authors"].add(str(row["author_handle"]))
        price = message_price_payload(row)
        if price["observation_id"]:
            bucket["price"] = price
    public = []
    seen_authors: set[str] = set()
    start_price = None
    for start_ms in sorted(grouped):
        bucket = grouped[start_ms]
        authors = set(bucket["authors"])
        new_authors = len(authors - seen_authors)
        seen_authors.update(authors)
        bucket_price = bucket.get("price")
        if start_price is None and bucket_price and bucket_price.get("price_usd") is not None:
            start_price = float(bucket_price["price_usd"])
        if start_price and bucket_price and bucket_price.get("price_usd") is not None:
            bucket["price_change_from_start_pct"] = round(float(bucket_price["price_usd"]) / start_price - 1.0, 6)
        public.append({**bucket, "authors": len(authors), "new_authors": new_authors})
    return public


def _authors(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        handle = str(row.get("author_handle") or "")
        if not handle:
            continue
        received_at_ms = int(row.get("received_at_ms") or 0)
        item = grouped.setdefault(
            handle,
            {
                "handle": handle,
                "first_seen_ms": received_at_ms,
                "latest_seen_ms": received_at_ms,
                "posts": 0,
                "followers": None,
                "role": "amplifier",
                "quality_score": None,
            },
        )
        item["posts"] += 1
        item["first_seen_ms"] = min(int(item["first_seen_ms"]), received_at_ms)
        item["latest_seen_ms"] = max(int(item["latest_seen_ms"]), received_at_ms)
    return sorted(grouped.values(), key=lambda item: (-int(item["posts"]), int(item["first_seen_ms"])))


def _phase(posts: int, authors: int) -> str:
    if posts <= 1:
        return "seed"
    if authors <= 1:
        return "concentration"
    if posts >= 5 and authors >= 3:
        return "expansion"
    return "ignition"


def _top_author_share(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    counts: dict[str, int] = {}
    for row in rows:
        handle = str(row.get("author_handle") or "")
        if handle:
            counts[handle] = counts.get(handle, 0) + 1
    return (max(counts.values()) / len(rows)) if counts else 0.0
