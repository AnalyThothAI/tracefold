from __future__ import annotations

import functools
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

import tracefold.news.program.resources.candidates as candidate_programs
from tracefold.app.learning_runtime import (
    NewsProgramRuntimeComposition,
    active_arm_manifest,
    compose_news_program_runtime,
    runtime_manifest_sha,
)
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.wiring.database import (
    WorkerNewsColdDatabase,
    WorkerNewsDatabase,
    WorkerQuoteDatabase,
    WorkerReactionDatabase,
)
from tracefold.app.workers.wiring.market_review import (
    _delivery_price_fetcher_for,
    _event_reaction_loop,
    _instrument_snapshot_loop,
    _quote_snapshot_loop,
)
from tracefold.integrations.feishu import FeishuNewsPushSender
from tracefold.integrations.opennews import OpenNewsStrategyHistoryClient, OpenNewsWebSocketClient
from tracefold.integrations.telegram import TelegramNewsPushSender
from tracefold.integrations.venues import VenueCatalogTradabilityVerifier
from tracefold.news import ProgressionVerifier
from tracefold.news.learning.contracts import ArmManifest, CandidateManifest
from tracefold.news.market_review.loops import QuoteDatabasePort, ReactionDatabasePort
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.delivery import DelivererConsumer
from tracefold.news.pipeline.maintenance import JanitorLoop
from tracefold.news.pipeline.receiver import OpenNewsReceiver
from tracefold.news.pipeline.recovery import RecoveryRunner
from tracefold.news.pipeline.root import NewsPipeline
from tracefold.news.pipeline.runtime import NewsDatabasePort
from tracefold.news.pipeline.triage import TriageConsumer
from tracefold.news.program.artifact import (
    ProgramStrategyArtifactV1,
    load_stable_program_artifact,
)
from tracefold.news.program.contracts import SemanticJudge
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.release.canary import CanaryRuntimeArm
from tracefold.news.release.runtime import (
    CandidateArtifactUnavailable,
    CandidateRuntimeFact,
    candidate_program_artifact,
    reconcile_canary_startup,
)
from tracefold.news.triage_rules import DecidePolicy
from tracefold.platform.config.models import Settings, news_push_availability
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.runtime_identity import runtime_identity

if TYPE_CHECKING:
    from tracefold.integrations.rabbitmq import RabbitMQBus


@dataclass(frozen=True, slots=True)
class _ProgramArms:
    """What one Workers process may execute this deployment: the stable arm, plus any runnable candidate."""

    judge: SemanticJudge | None
    progression_verifier: ProgressionVerifier | None
    stable_artifact: ProgramStrategyArtifactV1
    stable_bundle_sha: str
    canary_arms: dict[str, CanaryRuntimeArm]
    runtime_manifest: dict[str, Any]


def configured_runtime_manifest_sha(
    settings: Settings,
    *,
    runtime_composition: NewsProgramRuntimeComposition | None = None,
    stable_arm: ArmManifest | None = None,
    candidate_shas: list[str] | None = None,
    identity: Any = None,
) -> str:
    """Hash the exact image/config Program set Workers will register."""

    composition = runtime_composition or compose_news_program_runtime(settings)
    stable = stable_arm or active_arm_manifest(settings, runtime_composition=composition)
    candidates = sorted(_compiled_candidate_manifests()) if candidate_shas is None else sorted(candidate_shas)
    process_identity = identity or runtime_identity()
    return runtime_manifest_sha(
        stable_bundle_sha=stable.bundle_sha,
        candidate_shas=candidates,
        image_digest=process_identity.image_digest,
        runtime_revision=process_identity.runtime_revision,
    )


async def _wire_news_pipeline(
    *,
    settings: Settings,
    db: WorkerDatabase,
    finite: FiniteOperations,
    telemetry: TelemetryRegistry | None = None,
) -> tuple[RabbitMQBus, NewsPipeline]:
    """Broker-driven News V3: one RabbitMQ bus + consumers; models/providers are optional capabilities."""

    bus = await _connect_news_bus(settings, telemetry=telemetry)
    news_db = WorkerNewsDatabase(db)
    cold_db = WorkerNewsColdDatabase(db)
    quote_db = WorkerQuoteDatabase(db)
    reaction_db = WorkerReactionDatabase(db)

    ws_client = OpenNewsWebSocketClient(token=settings.news.opennews_token) if settings.news.opennews_token else None
    history_client = (
        OpenNewsStrategyHistoryClient(token=settings.news.opennews_token) if settings.news.opennews_token else None
    )
    recovery = (
        RecoveryRunner(bus=bus, db=news_db, history_client=history_client, telemetry=telemetry)
        if history_client
        else None
    )
    receiver = (
        OpenNewsReceiver(
            bus=bus,
            db=news_db,
            ws_client=ws_client,
            recovery=recovery,
        )
        if ws_client
        else None
    )

    arms = await _compose_program_arms(settings, db=db)
    pipeline = _compose_news_pipeline(
        settings,
        bus=bus,
        news_db=news_db,
        cold_db=cold_db,
        quote_db=quote_db,
        reaction_db=reaction_db,
        finite=finite,
        arms=arms,
        receiver=receiver,
        recovery=recovery,
        telemetry=telemetry,
    )
    return bus, pipeline


async def _connect_news_bus(
    settings: Settings,
    *,
    telemetry: TelemetryRegistry | None = None,
) -> RabbitMQBus:
    from tracefold.integrations.rabbitmq import POLICY_EFFECTIVE_TIMEOUT_SECONDS, RabbitMQBus

    broker_url = settings.news.broker.url
    if not broker_url:
        raise RuntimeError("news_broker_url_missing")
    bus = RabbitMQBus(
        url=broker_url,
        name_prefix=settings.news.broker.name_prefix,
        connect_timeout_seconds=settings.news.broker.connect_timeout_seconds,
        management_url=settings.news.broker.management_url,
        telemetry=telemetry,
    )
    await bus.connect()
    # Retry now lives in the broker policy (#400). Workers refuses to consume against a topology whose
    # effective policy is not the checked-in contract, because a missing policy is not a degraded mode:
    # it is immediate redelivery, the quorum default delivery limit and at-most-once dead lettering.
    # The settle bound covers the first boot against a fresh broker: connect() has just declared the
    # queues, and the management API only publishes their effective policy on its statistics interval,
    # so an unbounded-truth one-shot read here would kill Workers on every fresh volume.
    await bus.verify_policies(settle_timeout_seconds=POLICY_EFFECTIVE_TIMEOUT_SECONDS)
    return bus


async def _compose_program_arms(settings: Settings, *, db: WorkerDatabase) -> _ProgramArms:
    """Resolve every Program this image can actually run, then fail closed on any armed candidate it cannot."""

    runtime_composition = compose_news_program_runtime(settings)
    identity = runtime_identity()
    stable_arm = active_arm_manifest(settings, runtime_composition=runtime_composition)
    compiled_candidates = _compiled_candidate_manifests()
    stable_artifact = load_stable_program_artifact()
    if stable_arm.program_version != PROGRAM_VERSION or stable_artifact.program_sha256 != stable_arm.program_sha256:
        raise RuntimeError("news_stable_program_manifest_mismatch")
    semantic_judge = runtime_composition.semantic_judge(stable_artifact)
    progression_verifier = runtime_composition.progression_verifier()
    canary_arms: dict[str, CanaryRuntimeArm] = {}
    candidate_facts = {
        candidate_sha: CandidateRuntimeFact(
            candidate_manifest_sha=candidate_sha,
            compiled_bundle_sha=candidate.candidate_arm.bundle_sha,
            runnable_bundle_sha=None,
            failure_kind="runtime_unavailable",
        )
        for candidate_sha, candidate in compiled_candidates.items()
    }
    if semantic_judge is not None:
        canary_arms, candidate_facts = _candidate_runtime_arms(
            compiled_candidates,
            runtime_composition=runtime_composition,
            stable_artifact=stable_artifact,
            stable_arm=stable_arm,
        )
    await db.run_news(
        "news_canary_startup_validation",
        _reconcile_news_canary_startup,
        db,
        candidate_facts,
        operation_timeout_seconds=3.0,
    )
    return _ProgramArms(
        judge=semantic_judge,
        progression_verifier=progression_verifier,
        stable_artifact=stable_artifact,
        stable_bundle_sha=stable_arm.bundle_sha,
        canary_arms=canary_arms,
        runtime_manifest={
            "manifest_sha": configured_runtime_manifest_sha(
                settings,
                runtime_composition=runtime_composition,
                stable_arm=stable_arm,
                candidate_shas=list(compiled_candidates),
                identity=identity,
            ),
            "stable_bundle_sha": stable_arm.bundle_sha,
            # What this bundle *is*, carried down so the startup barrier can open its evidence epoch
            # without re-deriving an identity the composition root already holds (#314).
            "envelope_sha256": stable_arm.envelope_sha256,
            "artifact_schema_version": stable_artifact.schema_version,
            "program_version": stable_arm.program_version,
            "program_sha256": stable_arm.program_sha256,
            "candidate_shas": sorted(compiled_candidates),
            "image_digest": identity.image_digest,
            "runtime_revision": identity.runtime_revision,
            "now_ms": int(time.time() * 1000),
        },
    )


def _compiled_candidate_manifests() -> dict[str, CandidateManifest]:
    """Image-carried candidate documents, keyed by candidate SHA. A malformed one is logged, never fatal."""

    compiled: dict[str, CandidateManifest] = {}
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
        compiled[candidate.candidate_sha] = candidate
    return compiled


def _candidate_runtime_arms(
    compiled_candidates: dict[str, CandidateManifest],
    *,
    runtime_composition: NewsProgramRuntimeComposition,
    stable_artifact: ProgramStrategyArtifactV1,
    stable_arm: ArmManifest,
) -> tuple[dict[str, CanaryRuntimeArm], dict[str, CandidateRuntimeFact]]:
    """Compose candidate Programs and report neutral runtime-stage facts."""

    canary_arms: dict[str, CanaryRuntimeArm] = {}
    candidate_facts: dict[str, CandidateRuntimeFact] = {}
    for candidate in compiled_candidates.values():
        arm = candidate.candidate_arm
        try:
            candidate_artifact = candidate_program_artifact(
                candidate,
                stable_arm,
                stable_artifact=stable_artifact,
            )
        except CandidateArtifactUnavailable as exc:
            candidate_facts[candidate.candidate_sha] = CandidateRuntimeFact(
                candidate_manifest_sha=candidate.candidate_sha,
                compiled_bundle_sha=arm.bundle_sha,
                runnable_bundle_sha=None,
                failure_kind=exc.failure_kind,
            )
            logger.warning(
                "candidate Program artifact unavailable candidate={} stage={} error={}",
                candidate.candidate_sha,
                exc.failure_kind,
                exc,
            )
            continue
        try:
            candidate_program = runtime_composition.semantic_judge(candidate_artifact)
        except (TypeError, ValueError) as exc:
            candidate_facts[candidate.candidate_sha] = CandidateRuntimeFact(
                candidate_manifest_sha=candidate.candidate_sha,
                compiled_bundle_sha=arm.bundle_sha,
                runnable_bundle_sha=None,
                failure_kind="runtime_invalid",
            )
            logger.error("candidate Program composition rejected program={} error={}", arm.program_sha256, exc)
            continue
        if candidate_program is None:
            candidate_facts[candidate.candidate_sha] = CandidateRuntimeFact(
                candidate_manifest_sha=candidate.candidate_sha,
                compiled_bundle_sha=arm.bundle_sha,
                runnable_bundle_sha=None,
                failure_kind="runtime_unavailable",
            )
            continue
        canary_arms[arm.bundle_sha] = CanaryRuntimeArm(
            bundle_sha=arm.bundle_sha,
            program=candidate_program,
            policy=DecidePolicy(**arm.policy),
            program_version=arm.program_version,
            program_sha256=arm.program_sha256,
        )
        candidate_facts[candidate.candidate_sha] = CandidateRuntimeFact(
            candidate_manifest_sha=candidate.candidate_sha,
            compiled_bundle_sha=arm.bundle_sha,
            runnable_bundle_sha=arm.bundle_sha,
            failure_kind=None,
        )
    return canary_arms, candidate_facts


def _news_push_sender(settings: Settings) -> FeishuNewsPushSender | TelegramNewsPushSender | None:
    push = news_push_availability(settings)
    if not push.requested:
        return None
    if not push.delivery_available:
        raise RuntimeError(f"news_push_unavailable:{push.reason or 'news_item_push_configuration_invalid'}")
    if push.provider == "telegram":
        token_file = settings.news_telegram_bot_token_file()
        chat_id = settings.news.push.telegram_chat_id
        if token_file is None or chat_id is None:
            raise RuntimeError("news_push_unavailable:news_item_push_telegram_configuration_invalid")
        try:
            bot_token = read_secure_secret_text(token_file)
        except SecretFileError:
            raise RuntimeError("news_push_unavailable:news_item_push_telegram_bot_token_unavailable") from None
        try:
            return TelegramNewsPushSender(bot_token=bot_token, chat_id=chat_id)
        except ValueError:
            raise RuntimeError("news_push_unavailable:news_item_push_telegram_sender_invalid") from None
    return FeishuNewsPushSender(
        webhook_url=str(settings.news.push.feishu_webhook_url),
        signing_secret=settings.news.push.feishu_signing_secret,
    )


def _compose_news_pipeline(
    settings: Settings,
    *,
    bus: RabbitMQBus,
    news_db: NewsDatabasePort,
    cold_db: NewsDatabasePort,
    quote_db: QuoteDatabasePort,
    reaction_db: ReactionDatabasePort,
    finite: FiniteOperations,
    arms: _ProgramArms,
    receiver: OpenNewsReceiver | None,
    recovery: RecoveryRunner | None,
    telemetry: TelemetryRegistry | None,
) -> NewsPipeline:
    watchlist_symbols = settings.news.watchlist_symbols
    return NewsPipeline(
        receiver=receiver,
        recovery=recovery,
        deduper=DeduperConsumer(
            bus=bus,
            db=news_db,
            watchlist_symbols=watchlist_symbols,
            suppress_low_signal=settings.news.gate.suppress_low_signal,
        ),
        triage=TriageConsumer(
            bus=bus,
            db=news_db,
            judge=arms.judge,
            program_version=PROGRAM_VERSION,
            program_sha256=arms.stable_artifact.program_sha256,
            watchlist_symbols=watchlist_symbols,
            watchlist=sorted(watchlist_symbols),
            concurrency=settings.news.triage.concurrency,
            circuit_failures=settings.news.triage.circuit_failures,
            circuit_open_seconds=settings.news.triage.circuit_open_seconds,
            policy=DecidePolicy(**settings.news.policy.model_dump()),
            stable_bundle_sha=arms.stable_bundle_sha,
            canary_arms=arms.canary_arms,
            runtime_manifest=arms.runtime_manifest,
        ),
        deliverer=DelivererConsumer(
            bus=bus,
            db=news_db,
            sender=_news_push_sender(settings),
            finite_operations=finite,
            min_interval_seconds=settings.news.push.min_interval_seconds,
            price_fetcher_for=functools.partial(_delivery_price_fetcher_for, settings),
            progression_verifier=arms.progression_verifier,
            tradability_verifier=(
                VenueCatalogTradabilityVerifier()
                if settings.news.venues.enabled
                and settings.news.venues.binance
                and settings.news.venues.hyperliquid
                and settings.news.venues.okx
                and settings.news.venues.lighter
                and settings.news.venues.bitget
                else None
            ),
        ),
        janitor=JanitorLoop(
            db=news_db,
            cold_db=cold_db,
            bus=bus,
            retention_raw_days=settings.news.retention.raw_days,
            retention_judged_days=settings.news.retention.judged_days,
            telemetry=telemetry,
        ),
        instruments=_instrument_snapshot_loop(settings, db=news_db, telemetry=telemetry),
        quotes=_quote_snapshot_loop(
            settings,
            db=quote_db,
            watchlist=sorted(watchlist_symbols),
            telemetry=telemetry,
        ),
        reactions=_event_reaction_loop(settings, db=reaction_db, telemetry=telemetry),
    )


def _reconcile_news_canary_startup(
    db: WorkerDatabase,
    candidate_facts: dict[str, CandidateRuntimeFact],
) -> bool:
    """Run the News-owned startup use case inside the one Worker transaction."""

    with db.worker_session("news_canary_startup_validation", 3.0) as repos:
        return reconcile_canary_startup(
            repos.news,
            candidate_facts=candidate_facts,
            now_ms=_now_ms(),
        )


def _now_ms() -> int:
    return int(time.time() * 1_000)
