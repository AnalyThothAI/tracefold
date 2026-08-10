from __future__ import annotations

from typing import Any

from .token_target_posts_service import TokenTargetPostsService
from .token_target_social_timeline_service import TokenTargetSocialTimelineService


class TokenCaseTargetNotFound(Exception):
    pass


class TokenCaseService:
    def __init__(
        self,
        *,
        targets: Any,
        profiles: Any,
        market_candles: Any | None = None,
    ) -> None:
        self.targets = targets
        self.profiles = profiles
        self.market_candles = market_candles

    def dossier(
        self,
        *,
        target_type: str,
        target_id: str,
        window: str,
        posts_limit: int,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        target = self.targets.target_identity(target_type=target_type, target_id=target_id)
        if target is None:
            raise TokenCaseTargetNotFound(target_id)
        timeline = TokenTargetSocialTimelineService(
            targets=self.targets,
            market_candles=self.market_candles,
        ).timeline(
            target_type=target_type,
            target_id=target_id,
            window=window,
            limit=max(posts_limit, 24),
            now_ms=now_ms,
        )
        posts = TokenTargetPostsService(targets=self.targets).target_posts(
            target_type=target_type,
            target_id=target_id,
            window=window,
            post_range="current_window",
            limit=posts_limit,
            now_ms=now_ms,
        )
        profile = self.profiles.profile_for_target(target_type=target_type, target_id=target_id)
        market_live = self._market_live(target=target, now_ms=now_ms)
        return {
            "target": target,
            "profile": profile,
            "timeline": timeline,
            "posts": posts,
            "market_live": market_live,
        }

    def _market_live(self, *, target: dict[str, Any], now_ms: int | None) -> dict[str, Any]:
        target_type = str(target.get("target_type") or "")
        target_id = str(target.get("target_id") or "")
        latest_tick = _latest_market_tick(self.targets, target_type=target_type, target_id=target_id)
        if latest_tick is not None:
            return _market_snapshot_from_tick(
                target_type=target_type,
                target_id=target_id,
                row=latest_tick,
                now_ms=now_ms,
            )
        return _market_snapshot(target_type=target_type, target_id=target_id, status="missing")


def _latest_market_tick(targets: Any, *, target_type: str, target_id: str) -> dict[str, Any] | None:
    row = targets.latest_market_tick(target_type=target_type, target_id=target_id)
    return dict(row) if row is not None else None


def _market_snapshot_from_tick(
    *,
    target_type: str,
    target_id: str,
    row: dict[str, Any],
    now_ms: int | None,
) -> dict[str, Any]:
    received_at_ms = _int_or_none(row.get("received_at_ms"))
    age_ms = None
    if received_at_ms is not None and now_ms is not None:
        age_ms = max(0, int(now_ms) - received_at_ms)
    return {
        "target_type": target_type,
        "target_id": target_id,
        "status": "ready",
        "price_usd": _float_or_none(row.get("price_usd")),
        "price_quote": None,
        "quote_symbol": "USD",
        "price_basis": "usd",
        "market_cap_usd": _float_or_none(row.get("market_cap_usd")),
        "liquidity_usd": _float_or_none(row.get("liquidity_usd")),
        "holders": _int_or_none(row.get("holders")),
        "volume_24h_usd": _float_or_none(row.get("volume_24h_usd")),
        "open_interest_usd": _float_or_none(row.get("open_interest_usd")),
        "observed_at_ms": _int_or_none(row.get("observed_at_ms")),
        "received_at_ms": received_at_ms,
        "age_ms": age_ms,
        "provider": row.get("source_provider"),
    }


def _market_snapshot(*, target_type: str, target_id: str, status: str) -> dict[str, Any]:
    return {
        "target_type": target_type,
        "target_id": target_id,
        "status": status,
        "price_usd": None,
        "price_quote": None,
        "quote_symbol": None,
        "price_basis": "unavailable",
        "market_cap_usd": None,
        "liquidity_usd": None,
        "holders": None,
        "volume_24h_usd": None,
        "open_interest_usd": None,
        "observed_at_ms": None,
        "received_at_ms": None,
        "age_ms": None,
        "provider": None,
    }


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
