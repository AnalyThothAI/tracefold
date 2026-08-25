from __future__ import annotations

import json
from argparse import Namespace
from collections.abc import Mapping
from typing import Any

from tracefold.platform.config.loader import load_settings


def handle_news(args: Namespace) -> tuple[int, dict[str, Any]]:
    if args.news_command == "bus-check":
        from .news_bus import _handle_bus_check

        return _handle_bus_check()
    if args.news_command == "instruments":
        from .news_instruments import _handle_instruments

        return _handle_instruments(args)
    if args.news_command == "review":
        from .news_review import _handle_review

        return _handle_review(args)
    if args.news_command in {"learning", "release"}:
        # One handler, two groups (#202 §11 PR-E). The split is what an operator reads off `--help` and
        # what the packages enforce; routing them separately here would only add a second place to keep
        # in step with the parser.
        from .news_learning import _handle_learning

        return _handle_learning(args)
    if args.news_command == "replay":
        return _handle_replay(args)
    if args.news_command == "dlq":
        from .news_bus import _handle_dlq

        return _handle_dlq(args)
    if args.news_command == "why":
        return _handle_why(args)
    return 2, {"ok": False, "error": f"unknown news command: {args.news_command}"}


def _handle_why(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import repositories
    from tracefold.news.eval.why import explain_event

    settings = load_settings(require_ws_token=False)
    with repositories(settings) as repos:
        report = explain_event(repos, str(args.event_id))
    if report is None:
        return 1, {"ok": False, "error": "news_event_not_found"}
    return 0, {"ok": True, "data": report}


def _handle_replay(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import repositories
    from tracefold.news.eval.replay import replay_hits

    settings = load_settings(require_ws_token=False)
    # The Gate reads the instrument universe (#89), so a replay without it measures the fallback, not the deployed
    # behaviour. The database stays optional — this command is also the offline tuning tool — but never silently:
    # `instruments_error` says why the map is missing.
    classes: Mapping[str, str] | None = None
    instruments_error: str | None = None
    if not args.no_instruments:
        try:
            with repositories(settings) as repos:
                classes = repos.instruments.instrument_classes() or None
        except Exception as exc:  # a replay must not need a database to run
            instruments_error = type(exc).__name__
    with open(args.path, encoding="utf-8") as fh:
        raw = json.load(fh)
    hits: list[Mapping[str, Any]] = []
    if isinstance(raw, Mapping):
        for value in raw.values():
            hits.extend(h for h in value if isinstance(h, Mapping))
    elif isinstance(raw, list):
        hits.extend(h for h in raw if isinstance(h, Mapping))
    report = replay_hits(
        hits,
        watchlist_symbols=settings.news.watchlist_symbols,
        suppress_low_signal=(
            settings.news.gate.suppress_low_signal if args.gate_policy == "config" else args.gate_policy == "strict"
        ),
        instrument_classes=classes,
    )
    if instruments_error:
        report["instruments_error"] = instruments_error
    return 0, {"ok": True, "data": report}


__all__ = ["handle_news"]
