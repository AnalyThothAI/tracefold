from __future__ import annotations

import time
from typing import Any

from loguru import logger

import tracefold.news.agents.programs.candidates as candidate_programs
from tracefold.app.learning_runtime import (
    active_arm_manifest,
    candidate_program_artifact,
    compose_news_program_runtime,
    runtime_manifest_sha,
)
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.wiring.market_review import (
    _event_reaction_loop,
    _instrument_snapshot_loop,
    _quote_snapshot_loop,
)
from tracefold.integrations.feishu import FeishuNewsPushSender
from tracefold.integrations.opennews import OpenNewsStrategyHistoryClient, OpenNewsWebSocketClient
from tracefold.news import CandidateManifest, DecidePolicy, OiPolicy
from tracefold.news.agents.semantic_program import load_stable_program_artifact
from tracefold.news.canary import CanaryRuntimeArm
from tracefold.news.consumers import (
    DeduperConsumer,
    DelivererConsumer,
    JanitorLoop,
    NewsPipeline,
    OpenNewsReceiver,
    RecoveryRunner,
    TriageConsumer,
)
from tracefold.platform.config.models import Settings, news_push_availability
from tracefold.platform.runtime_identity import runtime_identity


async def _wire_news_pipeline(
    *, settings: Settings, db: WorkerDatabase, finite: FiniteOperations
) -> tuple[Any, NewsPipeline]:
    """Broker-driven News V3: one RabbitMQ bus + consumers; models/providers are optional capabilities."""

    from tracefold.integrations.rabbitmq import RabbitMQBus

    broker_url = settings.news.broker.url
    if not broker_url:
        raise RuntimeError("news_broker_url_missing")
    bus = RabbitMQBus(
        url=broker_url,
        name_prefix=settings.news.broker.name_prefix,
        connect_timeout_seconds=settings.news.broker.connect_timeout_seconds,
    )
    await bus.connect()

    watchlist_symbols = settings.news.watchlist_symbols
    ws_client = OpenNewsWebSocketClient(token=settings.news.opennews_token) if settings.news.opennews_token else None
    history_client = (
        OpenNewsStrategyHistoryClient(token=settings.news.opennews_token) if settings.news.opennews_token else None
    )

    recovery = RecoveryRunner(bus=bus, db=db, history_client=history_client) if ws_client else None
    receiver = (
        OpenNewsReceiver(
            bus=bus,
            db=db,
            ws_client=ws_client,
            history_client=history_client,
            recovery=recovery,
        )
        if ws_client
        else None
    )

    runtime_composition = compose_news_program_runtime(settings)
    identity = runtime_identity()
    stable_arm = active_arm_manifest(settings, runtime_composition=runtime_composition)
    compiled_candidates: dict[str, CandidateManifest] = {}
    candidate_failures: dict[str, str] = {}
    for index, document in enumerate(candidate_programs.COMPILED_CANDIDATE_DOCUMENTS):
        try:
            candidate = CandidateManifest.model_validate(document)
        except (TypeError, ValueError) as exc:
            logger.error(
                "candidate manifest rejected index={} error={}",
                index,
                type(exc).__name__,
            )
            continue
        compiled_candidates[candidate.candidate_sha] = candidate
    canary_arms: dict[str, CanaryRuntimeArm] = {}
    stable_artifact = load_stable_program_artifact()
    if (
        stable_artifact.program_version != stable_arm.program_version
        or stable_artifact.program_sha256 != stable_arm.program_sha256
    ):
        raise RuntimeError("news_stable_program_manifest_mismatch")
    semantic_judge = runtime_composition.semantic_judge(stable_artifact)
    if semantic_judge is not None:
        for candidate in compiled_candidates.values():
            if candidate.parent_stable_sha != stable_arm.bundle_sha:
                candidate_failures[candidate.candidate_sha] = "candidate_parent_stale"
                logger.warning(
                    "ignoring canary candidate with stale parent candidate={} parent={} active={}",
                    candidate.candidate_sha,
                    candidate.parent_stable_sha,
                    stable_arm.bundle_sha,
                )
                continue
            arm = candidate.candidate_arm
            try:
                candidate_artifact = candidate_program_artifact(candidate, stable_artifact)
            except (OSError, ValueError) as exc:
                candidate_failures[candidate.candidate_sha] = "candidate_artifact_invalid"
                logger.error("candidate Program artifact rejected program={} error={}", arm.program_sha256, exc)
                continue
            try:
                candidate_program = runtime_composition.semantic_judge(candidate_artifact)
            except (TypeError, ValueError) as exc:
                candidate_failures[candidate.candidate_sha] = "candidate_runtime_invalid"
                logger.error("candidate Program composition rejected program={} error={}", arm.program_sha256, exc)
                continue
            if candidate_program is None:
                candidate_failures[candidate.candidate_sha] = "candidate_runtime_unavailable"
                continue
            canary_arms[arm.bundle_sha] = CanaryRuntimeArm(
                bundle_sha=arm.bundle_sha,
                program=candidate_program,
                policy=DecidePolicy(**arm.policy),
                program_version=arm.program_version,
                program_sha256=arm.program_sha256,
            )

    await db.run_news(
        "news_canary_startup_validation",
        _trip_unavailable_active_canary,
        db,
        {candidate_sha: candidate.candidate_arm.bundle_sha for candidate_sha, candidate in compiled_candidates.items()},
        frozenset(canary_arms),
        dict(candidate_failures),
        operation_timeout_seconds=3.0,
    )

    push = news_push_availability(settings)
    sender = (
        FeishuNewsPushSender(
            webhook_url=str(settings.news.push.feishu_webhook_url),
            signing_secret=settings.news.push.feishu_signing_secret,
        )
        if push.delivery_available
        else None
    )
    oi_policy = OiPolicy(**settings.news.oi.model_dump())
    pipeline = NewsPipeline(
        receiver=receiver,
        recovery=recovery,
        deduper=DeduperConsumer(
            bus=bus,
            db=db,
            watchlist_symbols=watchlist_symbols,
            suppress_low_signal=settings.news.gate.suppress_low_signal,
        ),
        triage=TriageConsumer(
            bus=bus,
            db=db,
            judge=semantic_judge,
            program_version=stable_artifact.program_version,
            program_sha256=stable_artifact.program_sha256,
            watchlist_symbols=watchlist_symbols,
            watchlist=sorted(watchlist_symbols),
            concurrency=settings.news.triage.concurrency,
            circuit_failures=settings.news.triage.circuit_failures,
            circuit_open_seconds=settings.news.triage.circuit_open_seconds,
            policy=DecidePolicy(**settings.news.policy.model_dump()),
            oi_policy=oi_policy,
            stable_bundle_sha=stable_arm.bundle_sha,
            canary_arms=canary_arms,
            runtime_manifest={
                "manifest_sha": runtime_manifest_sha(
                    stable_bundle_sha=stable_arm.bundle_sha,
                    candidate_shas=sorted(compiled_candidates),
                    image_digest=identity.image_digest,
                    runtime_revision=identity.runtime_revision,
                ),
                "stable_bundle_sha": stable_arm.bundle_sha,
                "candidate_shas": sorted(compiled_candidates),
                "image_digest": identity.image_digest,
                "runtime_revision": identity.runtime_revision,
                "now_ms": int(time.time() * 1000),
            },
        ),
        deliverer=DelivererConsumer(
            bus=bus,
            db=db,
            sender=sender,
            finite_operations=finite,
            min_interval_seconds=settings.news.push.min_interval_seconds,
            oi_policy=oi_policy,
        ),
        janitor=JanitorLoop(
            db=db,
            bus=bus,
            retention_raw_days=settings.news.retention.raw_days,
            retention_judged_days=settings.news.retention.judged_days,
        ),
        instruments=_instrument_snapshot_loop(settings, db=db),
        quotes=_quote_snapshot_loop(settings, db=db, watchlist=sorted(watchlist_symbols)),
        reactions=_event_reaction_loop(settings, db=db),
    )
    return bus, pipeline


def _trip_unavailable_active_canary(
    db: WorkerDatabase,
    compiled_candidate_bundles: dict[str, str],
    runnable_candidate_bundles: frozenset[str],
    candidate_failures: dict[str, str],
) -> bool:
    """Fail closed a nonterminal candidate that this image cannot execute."""

    with db.worker_session("news_canary_startup_validation", 3.0) as repos, repos.transaction():
        from tracefold.news.canary import (
            CANARY_ELIGIBILITY_PROFILE_SHA,
            CANARY_ROLLING_PROFILE_SHA,
            CANARY_SELECTOR_VERSION,
        )

        status = repos.news.canary_status()
        activation = status.get("activation")
        if activation is None or str(activation["state"]) not in {"armed", "active"}:
            return False
        for field, expected, reason in (
            ("selector_version", CANARY_SELECTOR_VERSION, "selector_version_mismatch"),
            ("eligibility_profile_sha", CANARY_ELIGIBILITY_PROFILE_SHA, "eligibility_profile_hash_mismatch"),
            ("rolling_profile_sha", CANARY_ROLLING_PROFILE_SHA, "rolling_profile_hash_mismatch"),
        ):
            if str(activation.get(field) or "") != expected:
                return bool(
                    repos.news.transition_canary(
                        activation_id=str(activation["activation_id"]),
                        target_state="tripped",
                        reason=reason,
                        now_ms=_now_ms(),
                    )
                )
        candidate_manifest_sha = str(activation["candidate_manifest_sha"])
        candidate_bundle_sha = str(activation["candidate_bundle_sha"])
        expected_bundle_sha = compiled_candidate_bundles.get(candidate_manifest_sha)
        if candidate_bundle_sha == expected_bundle_sha and candidate_bundle_sha in runnable_candidate_bundles:
            return False
        if expected_bundle_sha is None:
            reason = "candidate_manifest_missing_or_invalid"
        elif candidate_bundle_sha != expected_bundle_sha:
            reason = "candidate_bundle_mismatch"
        else:
            reason = candidate_failures.get(candidate_manifest_sha, "candidate_runtime_unavailable")
        return bool(
            repos.news.transition_canary(
                activation_id=str(activation["activation_id"]),
                target_state="tripped",
                reason=reason,
                now_ms=_now_ms(),
            )
        )


def _now_ms() -> int:
    return int(time.time() * 1_000)
