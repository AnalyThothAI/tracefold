from __future__ import annotations

from typing import Any

from tracefold.app.repositories import repositories
from tracefold.market import (
    TOKEN_RADAR_DEFAULT_VENUE,
    AssetFlowService,
    SearchCursorError,
    SearchEventsQuery,
    SearchService,
    TokenProfileReadModel,
)
from tracefold.platform.config.settings import load_settings

READ_MODEL_COMMANDS = frozenset({"recent", "search", "asset-flow"})


def handle_read_model(args: object) -> tuple[int, dict[str, Any]]:
    command = args.command
    settings = load_settings(require_ws_token=False)
    with repositories(settings) as repos:
        evidence = repos.evidence

        if command == "recent":
            handles = _handle_set(args.handles)
            events = evidence.recent_events(
                limit=args.limit,
                handles=handles,
                ca=args.ca or None,
                chain=args.chain or None,
                symbol=args.symbol or None,
            )
            return 0, {"ok": True, "data": {"events": events}}

        if command == "search":
            try:
                results = SearchService(search_query=SearchEventsQuery(repos.conn)).search(
                    args.query,
                    limit=args.limit,
                    window=args.window,
                    cursor=args.cursor or None,
                )
            except SearchCursorError:
                return 1, {"ok": False, "error": "invalid_cursor"}
            return (
                0 if results.ok else 1,
                {
                    "ok": results.ok,
                    "data": {
                        "query": results.query,
                        "page": results.page,
                        "target_candidates": results.target_candidates,
                        "items": results.items,
                    },
                    "error": results.error,
                },
            )

        if command == "asset-flow":
            data = AssetFlowService(
                token_radar=repos.token_radar,
                profiles=TokenProfileReadModel(token_profiles=repos.token_profiles),
            ).asset_flow(
                window=args.window,
                limit=args.limit,
                venue=TOKEN_RADAR_DEFAULT_VENUE,
                now_ms=_now_ms(),
            )
            return 0, {"ok": True, "data": {"window": args.window, **data}}

    return 2, {"ok": False, "error": f"unknown read model command: {command}"}


def _handle_set(raw: str) -> set[str]:
    return {item.strip().lstrip("@").lower() for item in raw.split(",") if item.strip()}


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
