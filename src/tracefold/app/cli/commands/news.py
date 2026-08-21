from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from argparse import Namespace
from collections.abc import Mapping
from typing import Any

from tracefold.platform.config.settings import load_settings


def handle_news(args: Namespace) -> tuple[int, dict[str, Any]]:
    if args.news_command == "bus-check":
        return _handle_bus_check()
    if args.news_command == "control":
        return _handle_control(args)
    if args.news_command == "instruments":
        return _handle_instruments(args)
    if args.news_command == "review":
        return _handle_review(args)
    if args.news_command == "learning":
        return _handle_learning(args)
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
        from tracefold.integrations.venues import (
            fetch_binance_instruments,
            fetch_hyperliquid_instruments,
            fetch_us_reference_instruments,
        )

        venues = settings.news.venues
        fetchers = []
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
            dangling = list(repos.instruments.dangling_seed_aliases())
            return 0, {"ok": True, "data": {"days": days, "tags": rows, "dangling_aliases": dangling}}
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


def _handle_review(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import postgres_connection
    from tracefold.news import (
        BlindPairwiseSubmission,
        DeskQuery,
        EventRubricSubmission,
        ExternalMissSubmission,
        Principal,
        ReviewDesk,
        TaskRef,
    )
    from tracefold.platform.postgres.postgres_client import transaction

    settings = load_settings(require_ws_token=False)
    principal = Principal(subject="operator")
    action = str(args.review_command)
    try:
        if action == "queue":
            query = DeskQuery(
                view=args.view,
                mode=args.mode,
                cohort=args.cohort,
                stratum=args.stratum,
                proposal=args.proposal,
                task=args.task,
                event=args.event,
                status=args.status,
                hours=int(args.hours),
                limit=min(100, int(args.limit)),
                cursor=args.cursor,
            )
            with postgres_connection(settings, role="serve") as conn:
                data = ReviewDesk(conn).open(query, principal=principal)
            return 0, {"ok": True, "data": data}
        if action == "evidence":
            task = TaskRef(task_id=str(args.task), task_version=str(args.version))
            with postgres_connection(settings, role="serve") as conn:
                data = ReviewDesk(conn).evidence(task, principal=principal)
            return 0, {"ok": True, "data": data}

        payload = _read_json_or_yaml(str(args.file))
        kind = str(payload.get("kind") or "")
        key = str(args.idempotency_key or uuid.uuid4())
        with postgres_connection(settings, role="review") as conn, transaction(conn):
            desk = ReviewDesk(conn)
            if action == "external-miss":
                submission = ExternalMissSubmission.model_validate(payload)
                data = desk.submit(None, submission, principal=principal, idempotency_key=key)
            else:
                submission = (
                    EventRubricSubmission.model_validate(payload)
                    if kind == "event_rubric"
                    else BlindPairwiseSubmission.model_validate(payload)
                )
                task = TaskRef(task_id=str(args.task), task_version=str(args.version))
                data = desk.submit(task, submission, principal=principal, idempotency_key=key)
        return 0, {"ok": True, "data": data}
    except (ValueError, PermissionError) as exc:
        return 2, {"ok": False, "error": str(exc)}


def _handle_learning(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import postgres_connection
    from tracefold.news import (
        CandidateEvaluator,
        CandidateManifest,
        ClosedWindow,
        DatasetSpec,
        EvaluationRequest,
        ProposalReceipt,
        RecordReplayModelAdapter,
        canonical_sha,
    )

    settings = load_settings(require_ws_token=False)
    action = str(args.learning_command)
    from tracefold.app.learning_runtime import active_arm_manifest

    stable = active_arm_manifest(settings)
    try:
        if action == "canary":
            from tracefold.app.repositories import repositories
            from tracefold.news import apply_canary_control, parse_canary_control
            from tracefold.news.agents.prompts.candidates import compiled_canary_candidates

            subcommand = str(args.canary_command)
            payload = {
                "action": subcommand,
                "candidate_sha": getattr(args, "candidate", None),
                "activation_id": getattr(args, "activation", None),
                "reason": getattr(args, "reason", None),
            }
            command = parse_canary_control(payload)
            compiled = compiled_canary_candidates()
            shipped = {
                sha: candidate.candidate_arm.bundle_sha
                for sha, candidate in compiled.items()
                if candidate.parent_stable_sha == stable.bundle_sha
            }
            stamp = int(time.time() * 1000)
            with repositories(settings) as repos, repos.transaction():
                result = apply_canary_control(
                    repos,
                    command,
                    stable_bundle_sha=stable.bundle_sha,
                    shipped_candidates=shipped,
                    now_ms=stamp,
                )
            return 0, {"ok": True, "data": result}

        if action == "propose":
            spec = _read_json_or_yaml(str(args.file))
            target = str(spec.get("target") or "")
            candidate_arm = stable
            if target == "prompt":
                prompt_text = str(spec.get("prompt_text") or "")
                candidate_arm = stable.model_copy(
                    update={
                        "prompt_version": str(spec.get("prompt_version") or "candidate"),
                        "prompt_text": prompt_text,
                        "prompt_sha256": hashlib.sha256(prompt_text.encode()).hexdigest(),
                    }
                )
                candidate_arm = type(stable).model_validate(candidate_arm.model_dump(mode="json"))
                patch_payload: Mapping[str, Any] = {
                    "prompt_version": candidate_arm.prompt_version,
                    "prompt_sha256": candidate_arm.prompt_sha256,
                }
            elif target == "policy":
                policy = dict(stable.policy)
                policy.update(dict(spec.get("policy") or {}))
                arm_payload = stable.model_dump(mode="json")
                arm_payload.update(policy=policy, policy_sha256=canonical_sha(policy))
                candidate_arm = type(stable).model_validate(arm_payload)
                patch_payload = {"policy": dict(spec.get("policy") or {})}
            else:
                raise ValueError("candidate_kind_unsupported")
            dimensions = tuple(str(value) for value in spec.get("target_dimensions") or ())
            with postgres_connection(settings, role="workers") as conn, conn.transaction():
                development = conn.execute(
                    "SELECT artifact_sha FROM news_learning_artifacts "
                    "WHERE artifact_sha = %s AND kind = 'dataset' AND payload->>'role' = 'development'",
                    (str(args.development),),
                ).fetchone()
                if development is None:
                    raise ValueError("news_learning_development_dataset_not_found")
                registered_at_ms = int(
                    conn.execute(
                        "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms"
                    ).fetchone()["now_ms"]
                )
                receipt = ProposalReceipt.issue(
                    development_dataset_sha=str(args.development),
                    failure_cluster_ids=tuple(str(value) for value in spec.get("failure_cluster_ids") or ()),
                    generator_kind=str(spec.get("generator_kind") or "human"),
                    generator_prompt_sha=spec.get("generator_prompt_sha"),
                    generator_model_sha=spec.get("generator_model_sha"),
                    generator_execution_sha=spec.get("generator_execution_sha"),
                    registered_at_ms=registered_at_ms,
                    candidate_patch_sha=canonical_sha(patch_payload),
                    declared_target_dimensions=dimensions,
                    guardrails=tuple(str(value) for value in spec.get("guardrails") or ()),
                )
                candidate = CandidateManifest(
                    target=target,
                    parent_stable_sha=stable.bundle_sha,
                    candidate_arm=candidate_arm,
                    hypothesis=str(spec.get("hypothesis") or ""),
                    target_dimensions=dimensions,
                    development_dataset_sha=str(args.development),
                    proposal_receipt=receipt,
                )
                proposal_payload = receipt.model_dump(mode="json")
                proposal_sha = _insert_learning_artifact(
                    conn,
                    kind="candidate_registration",
                    payload=receipt.registration_payload,
                    parent_sha=str(args.development),
                    created_at_ms=registered_at_ms,
                )
                if proposal_sha != receipt.registration_receipt_sha:
                    raise ValueError("news_learning_candidate_registration_hash_mismatch")
                sealed_proposal_sha = _insert_learning_artifact(
                    conn,
                    kind="proposal",
                    payload=proposal_payload,
                    parent_sha=str(args.development),
                    created_at_ms=registered_at_ms,
                )
                _insert_learning_artifact(
                    conn,
                    kind="candidate",
                    payload={
                        "candidate_sha": candidate.candidate_sha,
                        "proposal_sha": sealed_proposal_sha,
                        "manifest": candidate.model_dump(mode="json"),
                    },
                    parent_sha=stable.bundle_sha,
                    created_at_ms=registered_at_ms,
                )
            payload = {"candidate_sha": candidate.candidate_sha, "candidate": candidate.model_dump(mode="json")}
            _write_json(str(args.out), payload)
            return 0, {"ok": True, "data": {"path": args.out, **payload}}

        candidate = _load_candidate(str(getattr(args, "candidate", "") or ""))
        catalog = () if candidate is None else (candidate,)
        with postgres_connection(settings, role="workers") as conn:
            if action == "freeze":
                if args.role == "validation" and candidate is None:
                    raise ValueError("news_learning_validation_candidate_required")
                evaluator = CandidateEvaluator(
                    conn,
                    stable=stable,
                    model_adapter=RecordReplayModelAdapter({}),
                    candidate_catalog=catalog,
                )
                manifest = asyncio.run(
                    evaluator.freeze_dataset(
                        DatasetSpec(
                            role=str(args.role),
                            window=ClosedWindow(from_ms=int(args.from_ms), to_ms=int(args.to_ms)),
                            observation_ref=candidate.candidate_sha if candidate is not None else None,
                        )
                    )
                )
                payload = manifest.model_dump(mode="json")
                _write_json(str(args.out), payload)
                return 0, {"ok": True, "data": {"path": args.out, **payload}}

            if candidate is None:
                raise ValueError("news_learning_candidate_required")
            observation_manifest = str(getattr(args, "observation_manifest", "") or "") or None
            if action == "shadow" and observation_manifest is None and not bool(args.live_model):
                raise ValueError("news_learning_shadow_live_model_confirmation_required")
            adapter = _learning_model_adapter(conn, settings=settings, live=bool(getattr(args, "live_model", False)))
            evaluator = CandidateEvaluator(
                conn,
                stable=stable,
                model_adapter=adapter,
                candidate_catalog=(candidate,),
            )
            stage = str(args.stage) if action == "evaluate" else action
            report = asyncio.run(
                evaluator.evaluate(
                    EvaluationRequest(
                        development_dataset_sha=str(args.development),
                        validation_dataset_sha=str(args.validation) or None,
                        candidate_sha=candidate.candidate_sha,
                        stage=stage,
                        observation_manifest_sha=observation_manifest,
                    )
                )
            )
            payload = report.model_dump(mode="json")
            _write_json(str(args.out), payload)
            code = 0 if report.gate_outcome == "pass" else 1
            return code, {"ok": report.gate_outcome == "pass", "data": {"path": args.out, **payload}}
    except (ValueError, PermissionError) as exc:
        return 2, {"ok": False, "error": str(exc)}


def _load_candidate(path: str) -> Any | None:
    if not path:
        return None
    from tracefold.news import CandidateManifest

    document = _read_json_or_yaml(path)
    return CandidateManifest.model_validate(document.get("candidate") or document)


def _learning_model_adapter(conn: Any, *, settings: Any, live: bool) -> Any:
    from tracefold.news import LiveTriageModelAdapter, RecordReplayModelAdapter

    if live:
        from tracefold.app.llm import configured_chat_model
        from tracefold.platform.config.settings import news_model_availability

        availability = news_model_availability(settings)
        if not availability.triage_configured or not availability.triage_model:
            raise ValueError("news_learning_live_model_not_configured")
        chat, _ = configured_chat_model(
            settings,
            model_name=availability.triage_model,
            request_timeout_seconds=settings.news.triage.deadline_seconds + 2.0,
            max_tokens=700,
        )
        fallback_chat = None
        fallback_effective = None
        if availability.triage_fallback_model:
            fallback_endpoint = settings.llm.news_triage_fallback
            fallback_chat, fallback_effective = configured_chat_model(
                settings,
                model_name=availability.triage_fallback_model,
                request_timeout_seconds=settings.news.triage.deadline_seconds + 2.0,
                max_tokens=700,
                api_key=fallback_endpoint.api_key,
                base_url=fallback_endpoint.base_url,
            )
        return LiveTriageModelAdapter(
            chat_model=chat,
            deadline_seconds=settings.news.triage.deadline_seconds,
            fallback_chat_model=fallback_chat,
            fallback_model_name=fallback_effective,
            primary_breaker_failures=settings.news.triage.circuit_failures,
            primary_breaker_open_seconds=settings.news.triage.circuit_open_seconds,
        )
    rows = conn.execute(
        "SELECT DISTINCT ON (request_sha256) request_sha256, response "
        "FROM news_model_recordings WHERE response IS NOT NULL "
        "ORDER BY request_sha256, created_at_ms DESC"
    ).fetchall()
    return RecordReplayModelAdapter({str(row["request_sha256"]): row["response"] for row in rows})


def _insert_learning_artifact(
    conn: Any,
    *,
    kind: str,
    payload: Mapping[str, Any],
    parent_sha: str | None,
    created_at_ms: int,
) -> str:
    public = json.loads(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str))
    from tracefold.news import canonical_sha

    artifact_sha = canonical_sha({"kind": kind, "payload": public})
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, %s, %s, %s::jsonb, 'learning_propose', %s) "
        "ON CONFLICT (artifact_sha) DO NOTHING",
        (artifact_sha, kind, parent_sha, json.dumps(public, ensure_ascii=False, sort_keys=True), created_at_ms),
    )
    row = conn.execute(
        "SELECT kind, payload FROM news_learning_artifacts WHERE artifact_sha = %s",
        (artifact_sha,),
    ).fetchone()
    if row is None or str(row["kind"]) != kind or dict(row["payload"] or {}) != public:
        raise ValueError("news_learning_artifact_collision")
    return artifact_sha


def _handle_why(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import repositories
    from tracefold.news.eval.why import explain_event

    settings = load_settings(require_ws_token=False)
    with repositories(settings) as repos:
        report = explain_event(repos, str(args.event_id))
    if report is None:
        return 1, {"ok": False, "error": "news_event_not_found"}
    return 0, {"ok": True, "data": report}


def _read_json_or_yaml(path: str) -> dict[str, Any]:
    """JSON first, YAML second.

    A frozen corpus is one line of JSON and can be megabytes; PyYAML is orders of magnitude slower on it, and
    YAML 1.1 does not resolve exponent-form floats without a decimal point — `1e-05` comes back as the *string*
    `"1e-05"`, which then fails the corpus hash check for no visible reason. A hand-written candidate file is
    still allowed to be YAML.
    """

    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    try:
        document = json.loads(text)
    except ValueError:
        import yaml

        document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise ValueError(f"news_document_not_a_mapping:{path}")
    return document


def _write_json(path: str, payload: Mapping[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _handle_replay(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import repositories
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
        strategy_ids=settings.news.opennews_strategy_ids or ("1018", "1352", "1353"),
        watchlist_symbols=settings.news.watchlist_symbols,
        suppress_low_signal=(
            settings.news.gate.suppress_low_signal if args.gate_policy == "config" else args.gate_policy == "strict"
        ),
        instrument_classes=classes,
    )
    if instruments_error:
        report["instruments_error"] = instruments_error
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
