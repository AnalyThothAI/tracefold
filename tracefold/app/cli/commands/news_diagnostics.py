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
    if args.news_command == "bus-policy":
        from .news_bus import _handle_bus_policy

        return _handle_bus_policy(args)
    if args.news_command == "instruments":
        from .news_instruments import _handle_instruments

        return _handle_instruments(args)
    if args.news_command == "review":
        from .news_review import _handle_review

        return _handle_review(args)
    if args.news_command == "learning":
        from .news_learning import _handle_learning

        return _handle_learning(args)
    if args.news_command == "replay":
        return _handle_replay(args)
    if args.news_command == "dlq":
        from .news_bus import _handle_dlq

        return _handle_dlq(args)
    if args.news_command == "retry-work":
        return _handle_retry_work(args)
    if args.news_command == "cancel-work":
        return _handle_cancel_work(args)
    if args.news_command == "reanalyze":
        return _handle_reanalyze(args)
    if args.news_command == "repair-head-scopes":
        return _handle_repair_head_scopes(args)
    if args.news_command == "why":
        return _handle_why(args)
    if args.news_command == "wallets":
        from .news_wallets import handle_wallets

        return handle_wallets(args)
    return 2, {"ok": False, "error": f"unknown news command: {args.news_command}"}


def _handle_retry_work(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import repositories
    from tracefold.news.bus import now_ms

    settings = load_settings(require_ws_token=False)
    try:
        with repositories(settings) as repos, repos.transaction():
            work = repos.news.semantic_work if args.kind == "semantic" else repos.news.notification_work
            reopened = work.retry_failed_revision(
                event_id=str(args.event),
                revision=str(args.revision),
                now_ms=now_ms(),
            )
    except ValueError as exc:
        return 2, {"ok": False, "error": str(exc)}
    return (0 if reopened else 1), {
        "ok": reopened,
        "event_id": args.event,
        "kind": args.kind,
        "revision": args.revision,
        "status": "reopened" if reopened else "not_failed_or_version_changed",
    }


def _handle_cancel_work(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import repositories
    from tracefold.news.bus import now_ms

    reason = str(args.reason).strip()
    if not reason:
        return 2, {"ok": False, "error": "news_cancel_reason_missing"}
    settings = load_settings(require_ws_token=False)
    stamp = now_ms()
    with repositories(settings) as repos, repos.transaction():
        work = repos.news.semantic_work
        current = work.semantic_work(str(args.event))
        cancellable = (
            current is not None
            and int(current["wanted_revision"]) == args.revision
            and int(current.get("done_revision") or 0) < args.revision
            and current.get("last_outcome") != "cancelled"
            and int(current.get("leased_until_ms") or 0) <= stamp
        )
        cancelled = (
            args.execute
            and cancellable
            and work.cancel_revision(
                event_id=str(args.event), expected_revision=args.revision, now_ms=stamp, input=repos.news.semantic_input
            )
        )
    ok = bool(cancelled if args.execute else cancellable)
    return (0 if ok else 1), {
        "ok": ok,
        "event_id": args.event,
        "revision": args.revision,
        "reason": reason,
        "execute": args.execute,
        "status": "cancelled"
        if cancelled
        else ("cancellable" if ok else "not_outstanding_or_version_changed_or_leased"),
    }


def _handle_reanalyze(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import repositories
    from tracefold.news.bus import now_ms
    from tracefold.news.storage.errors import EventUpdateConflict

    if args.execute and (not args.read or not args.reason):
        return 2, {"ok": False, "error": "news_reanalysis_target_or_reason_missing"}
    expected_head = None if args.head == "none" else str(args.head)
    settings = load_settings(require_ws_token=False)
    try:
        with repositories(settings) as repos:
            with repos.transaction():
                listing = repos.news.semantic_work.reanalysis_scope_list(
                    event_id=str(args.event), now_ms=now_ms(), input=repos.news.semantic_input
                )
            if listing["wanted_revision"] != args.wanted or listing["head_revision"] != expected_head:
                raise EventUpdateConflict("news_reanalysis_version_changed")
            if not args.execute:
                return 0, {"ok": True, **listing}
            with repos.transaction():
                revision = repos.news.semantic_work.request_reanalysis(
                    input=repos.news.semantic_input,
                    event_id=str(args.event),
                    expected_wanted_revision=args.wanted,
                    expected_head_revision=expected_head,
                    read_ref=str(args.read),
                    reason=str(args.reason),
                    now_ms=now_ms(),
                )
    except (EventUpdateConflict, LookupError, ValueError) as exc:
        return 1, {"ok": False, "error": str(exc)}
    return 0, {"ok": True, "event_id": args.event, "read_ref": args.read, "wanted_revision": revision}


def _handle_repair_head_scopes(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import repositories
    from tracefold.news.bus import now_ms
    from tracefold.news.storage.head_scope_repairs import audit_head_scope, audit_scope_rows
    from tracefold.news.updates.identity import digest

    settings = load_settings(require_ws_token=False)
    if args.execute:
        if not args.event or not args.head or not args.proof:
            return 2, {"ok": False, "error": "news_scope_repair_exact_target_required"}
        try:
            with repositories(settings) as repos, repos.transaction():
                row = repos.news.head_scope_repairs.head_scope_event(str(args.event))
                if row is None:
                    raise ValueError("news_scope_repair_event_not_found")
                proof = audit_head_scope(row)
                if proof["head_revision"] != args.head:
                    raise ValueError("news_scope_repair_head_changed")
                if digest(proof) != args.proof:
                    raise ValueError("news_scope_repair_proof_changed")
                revision = repos.news.head_scope_repairs.adopt_head_scope_repair(
                    expected_head=str(args.head), proof=proof, now_ms=now_ms()
                )
        except ValueError as exc:
            return 1, {"ok": False, "event_id": args.event, "error": str(exc)}
        return 0, {"ok": True, "event_id": args.event, "status": "applied", "content_revision": revision}

    if args.limit > 500:
        return 2, {"ok": False, "error": "news_scope_repair_page_limit_invalid"}
    with repositories(settings) as repos, repos.transaction():
        rows = repos.news.head_scope_repairs.head_scope_material(after=str(args.after), limit=args.limit + 1)
    page = rows[: args.limit]
    report = audit_scope_rows(page)
    return 0, {
        "ok": True,
        "projection_version": report["projection_version"],
        "heads": report["heads"],
        "affected_heads": report["affected_heads"],
        "outside_active_claims": report["outside_active_claims"],
        "unresolved_active_claims": report["unresolved_active_claims"],
        "next_cursor": str(page[-1]["event_id"]) if len(rows) > args.limit else None,
        "events": [
            {
                "event_id": event["event_id"],
                "head_revision": event["head_revision"],
                "proof": digest(event),
                "outside_claim_refs": [item["claim_ref"] for item in event["outside"]],
                "unresolved": event["unresolved"],
            }
            for event in report["events"]
            if event["outside"] or event["unresolved"]
        ],
    }


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
        instrument_classes=classes,
    )
    if instruments_error:
        report["instruments_error"] = instruments_error
    return 0, {"ok": True, "data": report}


__all__ = ["handle_news"]
