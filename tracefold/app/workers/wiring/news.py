from __future__ import annotations

import asyncio
import contextlib
import functools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from tracefold.app.learning_runtime import NewsModelRoute, compose_news_models, news_runtime_manifest_sha
from tracefold.app.news_updates import NewsUpdateRuntime, compose_news_updates
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.runtime import (
    MARKET_NOTIFICATIONS,
    NEWS_DELIVERY,
    NEWS_EDITORIAL,
    NEWS_INGESTION,
    NEWS_INSTRUMENTS,
    NEWS_QUOTES,
    NEWS_REACTIONS,
    SHARED_RESOURCE_FAILURES,
    CapabilityStates,
)
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
from tracefold.news.chain_tape.rules import WalletRules
from tracefold.news.market_notifications import TICK_SECONDS, MarketNotificationLoop
from tracefold.news.market_review.loops import QuoteDatabasePort, ReactionDatabasePort
from tracefold.news.market_review.pricing import QuoteRequest
from tracefold.news.pipeline.admission import DeduperConsumer
from tracefold.news.pipeline.delivery import DelivererLoop, read_display_quotes, read_pushed_news
from tracefold.news.pipeline.maintenance import JanitorLoop
from tracefold.news.pipeline.receiver import OpenNewsReceiver
from tracefold.news.pipeline.recovery import RecoveryRunner
from tracefold.news.pipeline.root import NewsPipeline
from tracefold.news.pipeline.runtime import NewsDatabasePort
from tracefold.news.pipeline.semantic import SemanticWorker
from tracefold.news.storage.event_update_store import PgJudgmentCache, PgNewsStore, PgSourceReader
from tracefold.platform.config.models import Settings, news_push_availability
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.runtime_identity import runtime_identity

if TYPE_CHECKING:
    from tracefold.integrations.rabbitmq import RabbitMQBus


@dataclass(frozen=True, slots=True)
class _MarketNotificationDatabase:
    """`MarketNotificationDatabasePort`: the News lane, plus the one read a market card needs.

    The market loop may not name a repository, a table or a price module, so "quote these symbols"
    is a port and this is the composition that satisfies it -- with the News first card's own read:
    the same session, the same 1.5 s budget, the same degradation. There is no market-specific quote
    rule anywhere, which is the point (#562). "What has this reader already been told about this
    instrument" is the second such port, satisfied out of the same lane and the same delivered-card
    ledger the reader-history bands read (#582 §3.3).
    """

    lane: WorkerNewsDatabase

    async def read[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float = 3.0) -> T:
        return await self.lane.read(name, fn, timeout_seconds=timeout_seconds)

    async def tx[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float = 3.0) -> T:
        return await self.lane.tx(name, fn, timeout_seconds=timeout_seconds)

    async def quotes_for_symbols(self, requests: Sequence[QuoteRequest], *, now_ms: int) -> list[dict[str, Any]]:
        return await read_display_quotes(self.lane, requests, now_ms=now_ms, name="news_market_quotes")

    async def pushed_news_for_symbol(self, symbol: str, *, now_ms: int) -> dict[str, Any]:
        """`MarketNotificationDatabasePort.pushed_news_for_symbol`: the delivered-card ledger, read once.

        One named read on the same News lane, through the News-owned read that carries the budget and
        the degradation -- exactly as the quote beside it is composed. Neither the statement nor the
        budget is App's: this is the composition and nothing else (#582 §3.3).
        """

        return await read_pushed_news(self.lane, symbol, now_ms=now_ms, name="news_market_news")


@dataclass(frozen=True, slots=True)
class NewsWiring:
    """What News composition hands the Workers root."""

    bus: RabbitMQBus
    pipeline: NewsPipeline
    market_notifications: MarketNotificationLoop
    # The composed EventUpdate runtime, owned by the root for its shutdown (the optional Jev
    # connection), or None when the editorial capability is not running.
    news_updates: NewsUpdateRuntime | None
    # The manifest Workers reports on /readyz; None when the configured program could not be composed.
    runtime_manifest_sha: str | None


def configured_runtime_manifest_sha(settings: Settings, *, identity: Any = None) -> str:
    """The runtime manifest this image and configuration will report, computed without composing it."""

    process_identity = identity or runtime_identity()
    return news_runtime_manifest_sha(
        settings,
        image_digest=process_identity.image_digest,
        runtime_revision=process_identity.runtime_revision,
    )


# App owns the tick, as it owns the Signal lane's: the loop exposes one business action.
MARKET_NOTIFICATIONS_TASK_NAME = "market-notifications"
MARKET_NOTIFICATION_POLL_SECONDS = TICK_SECONDS


async def _wire_news_pipeline(
    *,
    settings: Settings,
    db: WorkerDatabase,
    finite: FiniteOperations,
    capabilities: CapabilityStates,
    telemetry: TelemetryRegistry | None = None,
) -> NewsWiring:
    """Broker-driven News V3: one RabbitMQ bus + consumers; models/providers are optional capabilities.

    The bus is foundational for News here: reception, admission and delivery all publish through it, so
    a broker that will not connect still refuses the process. What is *not* foundational is the
    semantic runtime and the push sender -- either can fail on its own and leave reception, fact
    writes and reads running (#553 PR-3).
    """

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

    news_updates = _news_updates_or_fault(settings, news_db=news_db, capabilities=capabilities)
    pipeline = _compose_news_pipeline(
        settings,
        bus=bus,
        news_db=news_db,
        cold_db=cold_db,
        quote_db=quote_db,
        reaction_db=reaction_db,
        finite=finite,
        news_updates=news_updates,
        sender=_push_sender_or_fault(settings, capabilities=capabilities),
        receiver=receiver,
        recovery=recovery,
        telemetry=telemetry,
    )
    # Reception and admission are the entry this whole PR exists to keep running; their tasks are
    # foundational and never fault this capability. The bounded market-review loops are optional, and
    # each owns its own key so a faulted one names itself instead of the other two.
    capabilities.running(NEWS_INGESTION)
    for capability, loop in (
        (NEWS_INSTRUMENTS, pipeline.instruments),
        (NEWS_QUOTES, pipeline.quotes),
        (NEWS_REACTIONS, pipeline.reactions),
    ):
        if loop is None:
            capabilities.disabled(capability, f"{capability}_not_configured")
        else:
            capabilities.running(capability)
    # The market loop shares the Deliverer's send entry rather than owning a sender: one initial-send
    # guard for both, which is what makes the operator's one pacing number mean one thing. It runs
    # whether or not that entry has a sender -- with none, observations keep arriving, keep merging
    # per group, and their cards say `unavailable` instead of consuming an attempt (#553 §5.3).
    # `api.public_url` is the console's public origin, and this is the only place it is read: with one
    # the card carries its 打开明细 button, without one the card carries the item id instead (#553).
    market_notifications = MarketNotificationLoop(
        db=_MarketNotificationDatabase(news_db),
        sender=pipeline.deliverer.send_entry,
        console_base_url=settings.api.public_url,
        wallet_notifications_enabled=settings.news.chain_tape.notifications_enabled,
        # One set of thresholds for the whole flow: the detector opens an episode with these, and the
        # sender re-evaluates the same evidence against the same numbers at the committed collection
        # cutoff (#649 §6.1).
        wallet_rules=WalletRules(
            net_buy_slow_n=settings.news.chain_tape.rules.net_buy_slow_n,
            min_net_buy_usd=settings.news.chain_tape.rules.min_net_buy_usd,
            trigger_max_age_s=settings.news.chain_tape.rules.trigger_max_age_s,
        ),
    )
    capabilities.running(MARKET_NOTIFICATIONS)
    manifest = (
        configured_runtime_manifest_sha(settings)
        if news_updates is not None or compose_news_models(settings) is None
        else None
    )
    return NewsWiring(
        bus=bus,
        pipeline=pipeline,
        market_notifications=market_notifications,
        news_updates=news_updates,
        runtime_manifest_sha=manifest,
    )


async def run_market_notifications(
    loop: MarketNotificationLoop,
    *,
    stop_event: asyncio.Event,
    poll_seconds: float = MARKET_NOTIFICATION_POLL_SECONDS,
) -> None:
    """Poll `advance()` until the process stops. The loop owns no clock and no timer of its own.

    The startup sweep runs once here, after the root has taken Workers ownership, because that is the
    only moment a row still reading `sending` can be read as "nobody is sending this".

    An exception out of `advance()` is an infrastructure fault by construction -- every business
    outcome of a send is a durable row, and a failed send never raises -- so it ends this run of the
    loop and is raised rather than swallowed. The Workers root records `market_notifications` as
    `faulted`, the task stops, and News reception, market fact writes and every read carry on beside
    it (#553 PR-3). Nothing restarts it: the to-do list is in PostgreSQL, so an operator restart after
    the fix resumes from exactly where this process stopped.

    No new external-data counter is emitted. What every turn did is already durable and queryable:
    one row per card in `news_market_deliveries`, with its attempts, its receipt and its error. A
    parallel counter would be a second, weaker answer to a question PostgreSQL already answers.
    """

    await loop.start()
    while not stop_event.is_set():
        try:
            await loop.advance()
        except Exception:
            logger.exception("market notification turn failed")
            raise
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, float(poll_seconds)))


def _route_factory(route: NewsModelRoute) -> Callable[[], Any]:
    """One role's configured generative route, built once and borrowed by every call."""

    lms = route.lms()
    return lambda: lms


def _news_updates_or_fault(
    settings: Settings,
    *,
    news_db: NewsDatabasePort,
    capabilities: CapabilityStates,
) -> NewsUpdateRuntime | None:
    """Compose the EventUpdate runtime, or confine the failure to the editorial capability.

    Unconfigured News models leave no semantic worker: admitted evidence keeps committing its
    semantic work, which waits durably for a configured process. Construction performs no I/O, so
    a failure here is a configuration or program fact about this capability only (#553 PR-3).
    """

    try:
        models = compose_news_models(settings)
        if models is None:
            capabilities.disabled(NEWS_EDITORIAL, "news_models_not_configured")
            return None
        runtime = compose_news_updates(
            store=PgNewsStore(news_db, watch_symbols=settings.news.watchlist_symbols),
            relation_cache=PgJudgmentCache(news_db),
            extraction_lm_factory=_route_factory(models.extraction),
            card_lm_factory=_route_factory(models.card),
            judgment_lm_factory=_route_factory(models.judgment),
            extraction_model_identity=models.extraction.identity,
            card_model_identity=models.card.identity,
            judgment_model_identity=models.judgment.identity,
            news_judgment=models.news_judgment,
            source_reader=PgSourceReader(news_db),
        )
    except SHARED_RESOURCE_FAILURES:
        raise
    except Exception as exc:
        logger.opt(exception=exc).error("News semantic runtime assembly failed; editorial capability faulted")
        capabilities.faulted(NEWS_EDITORIAL, f"{NEWS_EDITORIAL}_assembly_failed:{type(exc).__name__}")
        return None
    capabilities.running(NEWS_EDITORIAL)
    return runtime


def _push_sender_or_fault(
    settings: Settings,
    *,
    capabilities: CapabilityStates,
) -> FeishuNewsPushSender | TelegramNewsPushSender | None:
    """Construct the push sender, or mark delivery unavailable and keep the fact chain running.

    A missing webhook, an unreadable secret file or a malformed token is a configuration fact, not a
    delivery: the Deliverer settles those Events `delivery_unavailable` rather than presenting them as
    sent. Recovery is a corrected config plus a restart; there is no hot reload console (#553 PR-3).
    """

    composed = _news_push_sender(settings)
    if composed.reason is not None:
        logger.error("News push sender unavailable reason={}", composed.reason)
        capabilities.unavailable(NEWS_DELIVERY, composed.reason)
        return None
    if composed.sender is None:
        capabilities.disabled(NEWS_DELIVERY, "news_item_push_not_requested")
    else:
        capabilities.running(NEWS_DELIVERY)
    return composed.sender


async def _connect_news_bus(
    settings: Settings,
    *,
    telemetry: TelemetryRegistry | None = None,
) -> RabbitMQBus:
    from tracefold.integrations.rabbitmq import (
        POLICY_EFFECTIVE_TIMEOUT_SECONDS,
        BrokerPolicyMismatch,
        RabbitMQBus,
    )

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
    # Retry lives in the broker policy (#400), so drift here is real: no policy means immediate
    # redelivery, the quorum default delivery limit and at-most-once dead lettering. It is not,
    # however, a reason for News to be down. Refusing to attach consumers stopped ingestion,
    # triage, delivery and every push, and left the operator a dead process to read the reason out
    # of; reporting it and consuming leaves a degraded retry contract and a running product, which
    # is the smaller failure and the one somebody can see (#598 D5-e). `tracefold news bus verify`
    # is still fail-closed, because a diagnostic that answers "yes" while drifted is worthless.
    # The settle bound covers the first boot against a fresh broker: connect() has just declared the
    # queues, and the management API only publishes their effective policy on its statistics interval,
    # so an unbounded-truth one-shot read here would report drift on every fresh volume.
    drifted = False
    try:
        await bus.verify_policies(settle_timeout_seconds=POLICY_EFFECTIVE_TIMEOUT_SECONDS)
    except BrokerPolicyMismatch as exc:
        drifted = True
        logger.error(
            "News broker effective policy is not the checked-in contract; attaching consumers anyway. "
            "Retry, delivery limit and dead lettering are the broker defaults until "
            "`tracefold news bus-policy apply` is rerun. mismatch={}",
            exc,
        )
    if telemetry is not None:
        telemetry.set_news_broker_policy_drift(drifted=drifted)
    return bus


@dataclass(frozen=True, slots=True)
class _ComposedPushSender:
    """The sender the configuration describes, or the reason there is none. Never both."""

    sender: FeishuNewsPushSender | TelegramNewsPushSender | None = None
    reason: str | None = None


def _news_push_sender(settings: Settings) -> _ComposedPushSender:
    """Build the configured push sender, or name the configuration fact that stops one being built.

    Nothing here raises. A misspelled signing secret, an unreadable token file or a chat id that is not
    a private channel is a fact about one capability's configuration; it used to be thrown as a
    `RuntimeError` for the caller a few lines below to catch and translate straight back into that same
    fact. A raise that is always caught in its own module is one rule written twice, and the shapes that
    escaped it were not hypothetical: a `ValueError` out of a sender constructor is not a `RuntimeError`,
    and it took reception, triage and the market loop down with it (#562 §5 row 1).
    """

    push = news_push_availability(settings)
    if not push.requested:
        return _ComposedPushSender()
    if not push.delivery_available:
        return _ComposedPushSender(reason=push.reason or "news_item_push_configuration_invalid")
    if push.provider == "telegram":
        token_file = settings.news_telegram_bot_token_file()
        chat_id = settings.news.push.telegram_chat_id
        if token_file is None or chat_id is None:
            return _ComposedPushSender(reason="news_item_push_telegram_configuration_invalid")
        try:
            bot_token = read_secure_secret_text(token_file)
        except (SecretFileError, OSError):
            return _ComposedPushSender(reason="news_item_push_telegram_bot_token_unavailable")
        try:
            return _ComposedPushSender(
                sender=TelegramNewsPushSender(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    proxy_url=settings.news.push.telegram_proxy_url,
                )
            )
        except ValueError:
            # The private-channel shape now lives only here, where the code that talks to Telegram
            # keeps it. `Settings` reads the operator's number and stops there (#562 §5 row 8).
            return _ComposedPushSender(reason="news_item_push_telegram_sender_invalid")
    try:
        return _ComposedPushSender(
            sender=FeishuNewsPushSender(
                webhook_url=str(settings.news.push.feishu_webhook_url),
                signing_secret=settings.news.push.feishu_signing_secret,
            )
        )
    except ValueError:
        return _ComposedPushSender(reason="news_item_push_feishu_sender_invalid")


def _compose_news_pipeline(
    settings: Settings,
    *,
    bus: RabbitMQBus,
    news_db: NewsDatabasePort,
    cold_db: NewsDatabasePort,
    quote_db: QuoteDatabasePort,
    reaction_db: ReactionDatabasePort,
    finite: FiniteOperations,
    news_updates: NewsUpdateRuntime | None,
    sender: FeishuNewsPushSender | TelegramNewsPushSender | None,
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
        ),
        semantic=(
            None
            if news_updates is None
            else SemanticWorker(
                bus=bus,
                db=news_db,
                store=PgNewsStore(news_db, watch_symbols=watchlist_symbols),
                agent=news_updates.agent,
                concurrency=settings.news.triage.concurrency,
                circuit_failures=settings.news.triage.circuit_failures,
                circuit_open_seconds=settings.news.triage.circuit_open_seconds,
                program_identity=news_updates.program_identity,
            )
        ),
        deliverer=DelivererLoop(
            db=news_db,
            sender=sender,
            finite_operations=finite,
            min_interval_seconds=settings.news.push.min_interval_seconds,
            price_fetcher_for=functools.partial(_delivery_price_fetcher_for, settings),
            progression_verifier=None,
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
            retention_chain_tape_days=settings.news.chain_tape.retention_days,
            chain_tape_enabled=settings.news.chain_tape.enabled,
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
