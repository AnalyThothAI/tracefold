from __future__ import annotations

import asyncio
import json
import time
from argparse import Namespace
from collections.abc import Mapping
from typing import Any

from tracefold.platform.config.settings import load_settings

LABEL_VERSION = "news_label_v1"


def handle_news(args: Namespace) -> tuple[int, dict[str, Any]]:
    if args.news_command == "bus-check":
        return _handle_bus_check()
    if args.news_command == "control":
        return _handle_control(args)
    if args.news_command == "instruments":
        return _handle_instruments(args)
    if args.news_command == "label":
        return _handle_label(args)
    if args.news_command == "eval":
        return _handle_eval(args)
    if args.news_command == "replay-decisions":
        return _handle_replay_decisions(args)
    if args.news_command == "corpus":
        return _handle_corpus(args)
    if args.news_command == "validate-candidate":
        return _handle_validate_candidate(args)
    if args.news_command == "replay":
        return _handle_replay(args)
    if args.news_command == "dlq":
        return _handle_dlq(args)
    if args.news_command == "why":
        return _handle_why(args)
    return 2, {"ok": False, "error": f"unknown news command: {args.news_command}"}


def _bus(settings: Any) -> Any:
    from tracefold.integrations.rabbitmq import RabbitMQBus

    url = settings.news.broker.url
    if not url:
        raise ValueError("news_broker_url_missing")
    return RabbitMQBus(
        url=url,
        name_prefix=settings.news.broker.name_prefix,
        connect_timeout_seconds=settings.news.broker.connect_timeout_seconds,
    )


def _handle_bus_check() -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)

    async def _run() -> dict[str, Any]:
        bus = _bus(settings)
        try:
            await bus.connect()
            declared = await bus.declare_topology()
            depths = await bus.queue_depths()
        finally:
            await bus.close()
        return {"declared": declared, "queues": depths}

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        return 1, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
    return 0, {"ok": True, "data": result}


def _handle_control(args: Namespace) -> tuple[int, dict[str, Any]]:
    """Consumers read news_control_state on every message; the CLI writes it directly (no broker hop)."""

    from tracefold.app.repositories import repositories
    from tracefold.news import apply_control, parse_control

    settings = load_settings(require_ws_token=False)
    payload = {"action": args.action, "key": args.key or None, "ttl_ms": int(args.ttl_minutes) * 60_000}
    try:
        command = parse_control(payload)
    except ValueError as exc:
        return 1, {"ok": False, "error": str(exc)}
    stamp = int(time.time() * 1000)
    with repositories(settings) as repos, repos.transaction():
        state = repos.news.read_control(now_ms=stamp)
        new_state = apply_control(state, command, now_ms=stamp)
        repos.news.write_control(paused=new_state["paused"], mutes=new_state["mutes"], now_ms=stamp)
    return 0, {"ok": True, "data": {"command": payload, "control": new_state}}


def _handle_instruments(args: Namespace) -> tuple[int, dict[str, Any]]:
    """Tradeable instrument universe (#75). `snapshot` writes; the rest are read-only."""

    from tracefold.app.repositories import repositories

    settings = load_settings(require_ws_token=False)
    stamp = int(time.time() * 1000)
    action = str(getattr(args, "action", "summary") or "summary")

    if action == "snapshot":
        from tracefold.integrations.venues import fetch_binance_instruments, fetch_hyperliquid_instruments

        venues = settings.news.venues
        fetchers = []
        if venues.binance:
            fetchers.append(("binance", fetch_binance_instruments))
        if venues.hyperliquid:
            fetchers.append(("hyperliquid", fetch_hyperliquid_instruments))
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
            repos.instruments.seed_aliases(now_ms=stamp)
            result = repos.instruments.apply_snapshot(instruments, now_ms=stamp)
            aliases = repos.instruments.learn_aliases_from_universe(now_ms=stamp)
        reportable = result.reportable
        return 0, {
            "ok": True,
            "data": {
                "total": result.total,
                "seeded_venues": list(result.seeded_venues),
                "listed": [f"{i.venue}:{i.venue_symbol}" for i in reportable.listed[:100]],
                "delisted": [f"{i.venue}:{i.venue_symbol}" for i in reportable.delisted[:100]],
                "listed_count": len(reportable.listed),
                "delisted_count": len(reportable.delisted),
                "unchanged": reportable.unchanged,
                "aliases_learned": aliases,
                "venue_errors": errors,
            },
        }

    # The workers role, like every other read-only News command: the CLI runs inside the workers container, which
    # is the only place the serve password file is absent.
    with repositories(settings) as repos:
        if action == "summary":
            return 0, {"ok": True, "data": repos.instruments.universe_summary()}
        if action == "listings":
            since = stamp - int(args.hours) * 3600_000
            rows = repos.instruments.recent_listings(since_ms=since, limit=int(args.limit))
            return 0, {"ok": True, "data": {"hours": int(args.hours), "listings": rows}}
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
                "instrument_class": repos.instruments.instrument_classes().get(base),
            },
        }


def _handle_label(args: Namespace) -> tuple[int, dict[str, Any]]:
    """Record one operator label. Labels are the only ground truth this system has (#81), so a label must be
    correctable, attributable, and able to say "the reader should have got this and no Event exists for it"."""

    from tracefold.app.repositories import repositories

    event_id = str(args.event_id or "").strip() or None
    subject = " ".join(str(args.subject or "").split())[:200]
    if event_id is None and not subject:
        return 2, {"ok": False, "error": "news_label_subject_required"}
    settings = load_settings(require_ws_token=False)
    stamp = int(time.time() * 1000)
    labelled_by = " ".join(str(args.by or "operator").split())[:64] or "operator"
    label = {"label": args.label, "note": str(args.note or "")[:200]}
    with repositories(settings) as repos, repos.transaction():
        if event_id is not None:
            card = repos.news.event_card(event_id)
            if card is None:
                return 1, {"ok": False, "error": "news_event_not_found"}
            subject = subject or " ".join(str(card.get("leader_title") or "").split())[:200]
        repos.news.insert_label(
            event_id=event_id,
            label_version=LABEL_VERSION,
            source="human",
            label=label,
            now_ms=stamp,
            labeled_by=labelled_by,
            subject=subject,
        )
    return 0, {
        "ok": True,
        "data": {"event_id": event_id, "subject": subject, "labeled_by": labelled_by, "label": label},
    }


def _handle_eval(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import repositories
    from tracefold.news.eval.offline import evaluate_recent

    settings = load_settings(require_ws_token=False)
    now_ms = int(time.time() * 1000)
    with repositories(settings) as repos:
        report = evaluate_recent(
            repos, now_ms=now_ms, hours=int(args.hours), policy_version=str(args.policy_version or "") or None
        )
    return 0, {"ok": True, "data": report}


def _handle_why(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import repositories
    from tracefold.news.eval.why import explain_event

    settings = load_settings(require_ws_token=False)
    with repositories(settings) as repos:
        report = explain_event(repos, str(args.event_id))
    if report is None:
        return 1, {"ok": False, "error": "news_event_not_found"}
    return 0, {"ok": True, "data": report}


def _handle_replay_decisions(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import repositories
    from tracefold.news import DecidePolicy
    from tracefold.news.eval.harness import candidate_policy
    from tracefold.news.eval.offline import replay_decisions

    settings = load_settings(require_ws_token=False)
    now_ms = int(time.time() * 1000)
    live = DecidePolicy(**settings.news.policy.model_dump())
    overrides: dict[str, Any] = {
        name: getattr(args, name)
        for name in (
            "escalate_magnitude",
            "min_push_magnitude",
            "min_watchlist_magnitude",
            "theme_cap_4h",
            "distinct_hard_cap_4h",
            "distinct_asset_cap_2h",
            "similarity_max",
        )
        if getattr(args, name, None) is not None
    }
    if args.no_unclear_push:
        overrides["unclear_push_event_types"] = ()
    if args.no_storyline_throttle:
        overrides["storyline_throttle"] = False
    if args.no_restatement_drop:
        overrides["restatement_drop"] = False
    if args.high_priority_escalates:
        overrides["high_priority_escalates"] = True
    policy = candidate_policy(live, overrides)
    with repositories(settings) as repos:
        report = replay_decisions(
            repos,
            now_ms=now_ms,
            hours=int(args.hours),
            watchlist_symbols=settings.news.watchlist_symbols,
            policy=policy,
        )
    return 0, {"ok": True, "data": report}


def _handle_corpus(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import repositories
    from tracefold.news.eval.harness import freeze_corpus

    settings = load_settings(require_ws_token=False)
    with repositories(settings) as repos:
        payload = freeze_corpus(
            repos,
            now_ms=int(time.time() * 1000),
            hours=int(args.hours),
            watchlist_symbols=settings.news.watchlist_symbols,
        )
    if args.out:
        _write_json(args.out, payload)
    return 0, {
        "ok": True,
        "data": {
            "path": args.out or None,
            "corpus_version": payload["corpus_version"],
            "sha256": payload["sha256"],
            "cases": len(payload["cases"]),
            "skipped_unreplayable_verdicts": payload["skipped_unreplayable_verdicts"],
            "prompt_versions": payload["prompt_versions"],
            **({} if args.out else {"corpus": payload}),
        },
    }


def _handle_validate_candidate(args: Namespace) -> tuple[int, dict[str, Any]]:
    """The release gate. Exit code 1 means "do not ship this"; the evidence says exactly which check said so."""

    from tracefold.news import DecidePolicy
    from tracefold.news.eval.harness import candidate_policy, load_corpus, validate_candidate

    settings = load_settings(require_ws_token=False)
    payload = _read_json_or_yaml(args.corpus)
    expectations = _read_json_or_yaml(args.expectations) if args.expectations else {}
    overrides: dict[str, Any] = {}
    if args.candidate:
        document = _read_json_or_yaml(args.candidate)
        overrides.update(dict(document.get("policy") or {}))
    for pair in args.overrides:
        name, _, value = str(pair).partition("=")
        if not name or not _:
            return 2, {"ok": False, "error": f"news_policy_override_invalid:{pair}"}
        overrides[name.strip()] = value.strip()
    try:
        corpus = load_corpus(payload, expectations=expectations)
        stable = DecidePolicy(**settings.news.policy.model_dump())
        decision = validate_candidate(
            corpus,
            stable=stable,
            candidate=candidate_policy(stable, overrides),
            hourly_cap=settings.news.push.hourly_cap,
        )
    except ValueError as exc:
        return 2, {"ok": False, "error": str(exc)}
    if args.evidence:
        _write_json(args.evidence, dict(decision.evidence))
    return (0 if decision.accepted else 1), {
        "ok": decision.accepted,
        "data": {
            "decision": "release_to_canary" if decision.accepted else "reject_candidate",
            "failed_checks": list(decision.failed_checks),
            "evidence_path": args.evidence or None,
            "evidence": dict(decision.evidence),
        },
    }


def _read_json_or_yaml(path: str) -> dict[str, Any]:
    import yaml

    with open(path, encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"news_document_not_a_mapping:{path}")
    return document


def _write_json(path: str, payload: Mapping[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _handle_replay(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.news.eval.replay import replay_hits

    settings = load_settings(require_ws_token=False)
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
        strategy_ids=settings.news.opennews_strategy_ids or ("1018", "1352", "1353"),
        watchlist_symbols=settings.news.watchlist_symbols,
        suppress_low_signal=(
            settings.news.gate.suppress_low_signal if args.gate_policy == "config" else args.gate_policy == "strict"
        ),
    )
    return 0, {"ok": True, "data": report}


def _handle_dlq(args: Namespace) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)

    async def _run() -> dict[str, Any]:
        bus = _bus(settings)
        try:
            await bus.connect()
            if args.dlq_action == "inspect":
                return {"messages": await bus.dead_letters(limit=int(args.limit))}
            if args.dlq_action == "replay":
                return {"replayed": await bus.replay_dead_letters(limit=int(args.limit))}
            return {"purged": await bus.purge_dead_letters()}
        finally:
            await bus.close()

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        return 1, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
    return 0, {"ok": True, "data": result}


__all__ = ["handle_news"]
