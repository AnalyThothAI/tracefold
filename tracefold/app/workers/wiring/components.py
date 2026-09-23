from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.runtime import (
    CHAIN_TAPE,
    MARKET_NOTIFICATIONS,
    NEWS_DELIVERY,
    NEWS_EDITORIAL,
    NEWS_INGESTION,
    NEWS_INSTRUMENTS,
    NEWS_QUOTES,
    NEWS_REACTIONS,
    SHARED_RESOURCE_FAILURES,
    WALLET_NET_BUY,
    WALLET_PRICES,
    WALLET_ROSTER,
    CapabilityStates,
)
from tracefold.app.workers.wiring.chain_tape import ChainTapeComposition, _wire_chain_tape
from tracefold.app.workers.wiring.news import _wire_news_pipeline
from tracefold.app.workers.wiring.watchdog import TradingWatchdog, wire_trading_watchdog
from tracefold.news.bus import BrokerBackpressure, BrokerUnavailable
from tracefold.news.market_notifications import MarketNotificationLoop
from tracefold.news.pipeline.root import NewsPipeline
from tracefold.platform.config.models import Settings, news_push_availability
from tracefold.platform.observability import TelemetryRegistry

if TYPE_CHECKING:
    from tracefold.integrations.rabbitmq import RabbitMQBus


@dataclass(slots=True)
class _Components:
    news_pipeline: NewsPipeline | None
    news_bus: RabbitMQBus | None
    # Composition is handed the process registry and never invents one, so this is not optional
    # either -- every capability wired below measures itself through it (#589 P-F14).
    telemetry: TelemetryRegistry
    runtime_manifest_sha: str | None = None
    market_notifications: MarketNotificationLoop | None = None
    chain_tape: ChainTapeComposition | None = None
    trading_watchdog: TradingWatchdog | None = None
    capabilities: CapabilityStates = field(default_factory=CapabilityStates)


def _capability_fault_reason(capability: str, exc: BaseException) -> str:
    """Name what stopped one capability, in the vocabulary a status reader publishes.

    Keyed on the capability rather than the task name so every reason an operator reads -- runtime
    fault, wiring failure, registration failure -- starts with the same word.
    """

    if any(isinstance(item, (BrokerBackpressure, BrokerUnavailable)) for item in _leaf_exceptions(exc)):
        return f"{capability}:news_broker_unavailable"
    return f"{capability}:{type(exc).__name__}"


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for item in exc.exceptions:
            leaves.extend(_leaf_exceptions(item))
        return leaves
    return [exc]


async def _wire_components(
    *,
    settings: Settings,
    db: WorkerDatabase,
    finite: FiniteOperations,
    telemetry: TelemetryRegistry,
) -> _Components:
    capabilities = CapabilityStates()
    news_pipeline: NewsPipeline | None = None
    news_bus: RabbitMQBus | None = None
    runtime_manifest_sha: str | None = None
    market_notifications: MarketNotificationLoop | None = None
    chain_tape: ChainTapeComposition | None = None
    if settings.news.enabled:
        news_bus, news_pipeline, market_notifications = await _wire_news_pipeline(
            settings=settings,
            db=db,
            finite=finite,
            telemetry=telemetry,
            capabilities=capabilities,
        )
        runtime_manifest_sha = await _register_runtime_manifest(news_pipeline, capabilities=capabilities)
        chain_tape = _wire_chain_tape(
            settings=settings,
            db=db,
            capabilities=capabilities,
            telemetry=telemetry,
        )
    else:
        for capability in (
            NEWS_INGESTION,
            NEWS_EDITORIAL,
            NEWS_INSTRUMENTS,
            NEWS_QUOTES,
            NEWS_REACTIONS,
            MARKET_NOTIFICATIONS,
            CHAIN_TAPE,
            WALLET_ROSTER,
            WALLET_NET_BUY,
            WALLET_PRICES,
        ):
            capabilities.disabled(capability, "news_disabled")
        # A push target declared against a disabled News is a configuration error, not a delivery.
        # It used to refuse the whole process; now it refuses only the capability it describes.
        push = news_push_availability(settings, inspect_secret_file=False)
        capabilities.declare(
            NEWS_DELIVERY,
            "unavailable" if push.requested else "disabled",
            reason="news_item_push_news_disabled" if push.requested else "news_disabled",
        )
    # The watchdog alerts through the Deliverer's one send entry, so it exists only beside a News
    # pipeline; which provider that entry reaches is composition's answer, not the watchdog's.
    trading_watchdog = wire_trading_watchdog(
        settings=settings, db=db, capabilities=capabilities, news_pipeline=news_pipeline
    )
    return _Components(
        news_pipeline=news_pipeline,
        news_bus=news_bus,
        runtime_manifest_sha=runtime_manifest_sha,
        market_notifications=market_notifications,
        chain_tape=chain_tape,
        trading_watchdog=trading_watchdog,
        telemetry=telemetry,
        capabilities=capabilities,
    )


async def _register_runtime_manifest(
    news_pipeline: NewsPipeline,
    *,
    capabilities: CapabilityStates,
) -> str | None:
    """Register the editorial Program manifest, or fault only the editorial capability.

    Reception, market facts and market notifications read no Program manifest, so a registration
    failure has nothing to say about them (#553 §7). The version check itself is unchanged: a
    manifest that cannot be registered leaves no Triage consumer to run an unproven Program.
    """

    try:
        await news_pipeline.register_runtime_manifest()
    except SHARED_RESOURCE_FAILURES:
        # PostgreSQL or a shared native permit, not this Program. Recording it as an editorial fault
        # would report a process-wide failure as one capability's, behind a green readiness.
        raise
    except Exception as exc:
        logger.opt(exception=exc).error("News Program manifest registration failed; editorial capability faulted")
        news_pipeline.disable_editorial()
        capabilities.faulted(
            NEWS_EDITORIAL,
            f"{NEWS_EDITORIAL}_manifest_registration_failed:{type(exc).__name__}",
        )
        return None
    return news_pipeline.runtime_manifest_sha
