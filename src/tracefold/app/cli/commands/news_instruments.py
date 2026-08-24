from __future__ import annotations

import asyncio
import time
from argparse import Namespace
from collections.abc import Callable
from typing import Any

from tracefold.platform.config.loader import load_settings


def _handle_instruments(args: Namespace) -> tuple[int, dict[str, Any]]:
    """Tradeable instrument universe (#75). `snapshot` writes; the rest are read-only."""

    from tracefold.app.repository_session import repositories

    settings = load_settings(require_ws_token=False)
    stamp = int(time.time() * 1000)
    action = str(getattr(args, "action", "summary") or "summary")

    if action == "snapshot":
        from tracefold.integrations.venues import (
            fetch_binance_instruments,
            fetch_hyperliquid_instruments,
            fetch_us_reference_instruments,
        )

        venues = settings.news.venues
        # Each adapter takes its own venue-shaped keyword defaults; the loop below calls them with none.
        fetchers: list[tuple[str, Callable[[], Any]]] = []
        if venues.binance:
            fetchers.append(("binance", fetch_binance_instruments))
        if venues.hyperliquid:
            fetchers.append(("hyperliquid", fetch_hyperliquid_instruments))
        if venues.us_reference:
            fetchers.append(("us_reference", fetch_us_reference_instruments))
        if not fetchers:
            return 1, {"ok": False, "error": "news_venues_all_disabled"}
        instruments: list[Any] = []
        errors: list[str] = []
        for venue, fetch in fetchers:
            try:
                instruments.extend(asyncio.run(fetch()))
            except Exception as exc:
                errors.append(f"{venue}:{getattr(exc, 'code', None) or type(exc).__name__}")
        if not instruments:
            return 1, {"ok": False, "error": "news_venue_snapshot_empty", "venues": errors}
        with repositories(settings) as repos, repos.transaction():
            seeds = repos.instruments.reconcile_seed_aliases(now_ms=stamp)
            result = repos.instruments.apply_snapshot(instruments, now_ms=stamp)
            learned = repos.instruments.learn_aliases_from_universe(now_ms=stamp)
            dangling = repos.instruments.dangling_seed_aliases()
        return 0, {
            "ok": True,
            "data": {
                "total": result.total,
                "venues": list(result.venues),
                "delisted": result.delisted,
                "aliases_seeded": seeds,
                "aliases_learned": learned,
                "dangling_aliases": [f"{r['alias']}->{r['base_symbol']}" for r in dangling],
                "venue_errors": errors,
            },
        }

    # The workers role, like every other read-only News command: the CLI runs inside the workers container, which
    # is the only place the serve password file is absent.
    with repositories(settings) as repos:
        if action == "summary":
            return 0, {"ok": True, "data": repos.instruments.universe_summary()}
        if action == "unmatched":
            days = int(args.days)
            rows = repos.instruments.unmatched_provider_tags(since_ms=stamp - days * 86_400_000, limit=int(args.limit))
            unmatched_dangling = list(repos.instruments.dangling_seed_aliases())
            return 0, {"ok": True, "data": {"days": days, "tags": rows, "dangling_aliases": unmatched_dangling}}
        symbol = str(getattr(args, "symbol", "") or "").strip()
        if not symbol:
            return 1, {"ok": False, "error": "news_instruments_symbol_required"}
        base = repos.instruments.resolve(symbol)
        return 0, {
            "ok": True,
            "data": {
                "symbol": symbol,
                "base_symbol": base,
                "venues": list(repos.instruments.venues_for(base)),
                # `us.listed` is a reference row, not a venue: without this an operator reads
                # `{"venues": ["us.listed"]}` as "tradeable" (#91).
                "tradeable": repos.instruments.is_tradeable(base),
                "instrument_class": repos.instruments.instrument_classes().get(base),
            },
        }
