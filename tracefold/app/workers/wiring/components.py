from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.operator_control import WorkersTelegramControl
from tracefold.app.workers.trading_notifications import TradingNotificationWorker
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.app.workers.wiring.news import _wire_news_pipeline
from tracefold.app.workers.wiring.trading import (
    _wire_signal_lane,
)
from tracefold.integrations.telegram import TelegramTradingNotifier
from tracefold.integrations.telegram_control import TelegramControlWebhook
from tracefold.news.bus import BrokerBackpressure, BrokerUnavailable
from tracefold.news.pipeline.root import NewsPipeline
from tracefold.platform.config.models import Settings
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.platform.observability import TelemetryRegistry
from tracefold.trading.signal_lane import SignalLane

if TYPE_CHECKING:
    from tracefold.integrations.rabbitmq import RabbitMQBus


@dataclass(slots=True)
class _Components:
    news_pipeline: NewsPipeline | None
    news_bus: RabbitMQBus | None
    runtime_manifest_sha: str | None = None
    signal_lane: SignalLane | None = None
    telemetry: TelemetryRegistry | None = None
    telegram_control: WorkersTelegramControl | None = None
    trading_notifications: TradingNotificationWorker | None = None


def _task_unavailable_reason(task_name: str | None, exc: BaseException) -> str:
    """Map a composed News task failure onto the process readiness vocabulary."""

    if task_name is None or not task_name.startswith("news-"):
        return "runtime_failed"
    if any(isinstance(item, (BrokerBackpressure, BrokerUnavailable)) for item in _leaf_exceptions(exc)):
        return "news_broker_unavailable"
    if task_name in {"news-deduper", "news-triage", "news-deliverer"}:
        return "news_consumer_fatal"
    if task_name == "news-receiver":
        return "news_receiver_fatal"
    if task_name == "news-recovery":
        return "news_recovery_fatal"
    return "runtime_failed"


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
    if settings.news.push.enabled and not settings.news.enabled:
        raise RuntimeError("news_push_unavailable:news_item_push_news_disabled")
    news_pipeline: NewsPipeline | None = None
    news_bus: RabbitMQBus | None = None
    runtime_manifest_sha: str | None = None
    if settings.news.enabled:
        news_bus, news_pipeline = await _wire_news_pipeline(
            settings=settings,
            db=db,
            finite=finite,
            telemetry=telemetry,
        )
        await news_pipeline.register_runtime_manifest()
        runtime_manifest_sha = news_pipeline.runtime_manifest_sha
    trading_db = WorkerTradingDatabase(db)
    telegram_control, trading_notifications = _wire_telegram_control(
        settings=settings,
        db=trading_db,
        finite=finite,
    )
    signal_lane = _wire_signal_lane(settings=settings, db=db, telemetry=telemetry)
    if signal_lane is None:
        now_ms = int(time.time() * 1_000)
        updated = await trading_db.tx(
            "trading_decision_disabled",
            lambda repos: repos.trading.set_decision_runtime(
                state="DISABLED",
                heartbeat_at_ms=None,
                reason="trading_disabled",
                now_ms=now_ms,
            ),
            timeout_seconds=10.0,
        )
        if not updated:
            raise RuntimeError("trading_decision_runtime_missing")
    return _Components(
        news_pipeline=news_pipeline,
        news_bus=news_bus,
        runtime_manifest_sha=runtime_manifest_sha,
        signal_lane=signal_lane,
        telemetry=telemetry,
        telegram_control=telegram_control,
        trading_notifications=trading_notifications,
    )


def _wire_telegram_control(
    *,
    settings: Settings,
    db: WorkerTradingDatabase,
    finite: FiniteOperations,
) -> tuple[WorkersTelegramControl | None, TradingNotificationWorker | None]:
    control = settings.trading.control
    if not control.enabled:
        return None, None
    secret_path = settings.trading_telegram_webhook_secret_file()
    bot_token_path = settings.trading_telegram_bot_token_file()
    if secret_path is None or bot_token_path is None:
        raise RuntimeError("trading_control_secret_unavailable")
    try:
        webhook_secret = read_secure_secret_text(secret_path)
        bot_token = read_secure_secret_text(bot_token_path)
    except SecretFileError:
        raise RuntimeError("trading_control_secret_unavailable") from None
    chat_id = control.notification_chat_id
    if chat_id is None:
        raise RuntimeError("trading_control_notification_chat_unavailable")
    notifier: TelegramTradingNotifier | None = None
    try:
        notifier = TelegramTradingNotifier(bot_token=bot_token, chat_id=chat_id)
        webhook = TelegramControlWebhook(
            webhook_secret=webhook_secret,
            bot_id=notifier.bot_id,
            allowed_chat_ids=frozenset(control.allowed_chat_ids),
            allowed_user_ids=frozenset(control.allowed_user_ids),
            target_profile_id=settings.trading.execution.profile_id,
        )
    except ValueError as exc:
        if notifier is not None:
            notifier.close()
        raise RuntimeError("trading_control_configuration_invalid") from exc
    return (
        WorkersTelegramControl(webhook=webhook, db=db),
        TradingNotificationWorker(db=db, finite=finite, sender=notifier),
    )
