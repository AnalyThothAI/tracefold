from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.operator_control import WorkersTelegramControl
from tracefold.app.workers.trading_notifications import TradingNotificationWorker
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.app.workers.wiring.news import _wire_news_pipeline
from tracefold.app.workers.wiring.trading import (
    _source_native_result_bars,
    _wire_signal_lane,
)
from tracefold.integrations.feishu import FeishuTradingNotifier
from tracefold.integrations.telegram import TelegramTradingNotifier, telegram_bot_id
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
    telegram_control = _wire_telegram_control(settings=settings, db=trading_db)
    trading_notifications = _wire_trading_notifications(settings=settings, db=trading_db, finite=finite)
    signal_lane = _wire_signal_lane(settings=settings, db=db, telemetry=telemetry)
    return _Components(
        news_pipeline=news_pipeline,
        news_bus=news_bus,
        runtime_manifest_sha=runtime_manifest_sha,
        signal_lane=signal_lane,
        telemetry=telemetry,
        telegram_control=telegram_control,
        trading_notifications=trading_notifications,
    )


def _wire_telegram_control(*, settings: Settings, db: WorkerTradingDatabase) -> WorkersTelegramControl | None:
    """The authenticated operator command ingress (#433-D). It no longer owns the notification channel.

    Until #458 PR-B this function also built the observation notifier, so being *told* what the Signal
    lane decided required standing up an authenticated *command* channel first. In production nobody
    had, and the notification worker had therefore never run at all.
    """

    control = settings.trading.control
    if not control.enabled:
        return None
    secret_path = settings.trading_telegram_webhook_secret_file()
    bot_token_path = settings.trading_telegram_bot_token_file()
    if secret_path is None or bot_token_path is None:
        raise RuntimeError("trading_control_secret_unavailable")
    try:
        webhook_secret = read_secure_secret_text(secret_path)
        bot_token = read_secure_secret_text(bot_token_path)
    except SecretFileError:
        raise RuntimeError("trading_control_secret_unavailable") from None
    if control.notification_chat_id is None:
        raise RuntimeError("trading_control_notification_chat_unavailable")
    try:
        webhook = TelegramControlWebhook(
            webhook_secret=webhook_secret,
            bot_id=telegram_bot_id(bot_token),
            allowed_chat_ids=frozenset(control.allowed_chat_ids),
            allowed_user_ids=frozenset(control.allowed_user_ids),
            account_slot=settings.trading.execution.account_slot,
        )
    except ValueError as exc:
        raise RuntimeError("trading_control_configuration_invalid") from exc
    return WorkersTelegramControl(webhook=webhook, db=db)


def _wire_trading_notifications(
    *,
    settings: Settings,
    db: WorkerTradingDatabase,
    finite: FiniteOperations,
) -> TradingNotificationWorker | None:
    """Assemble the observation notifier for the operator-selected channel (#458 PR-B).

    Feishu reuses the `news.push` webhook target. That reuse lives here, at the composition seam,
    which is the one place allowed to read both capabilities' configuration -- the News sender and the
    Trading notifier still never import each other and share only `FeishuWebhookClient`'s transport.
    """

    notifications = settings.trading.notifications
    if not notifications.enabled:
        return None
    sender: FeishuTradingNotifier | TelegramTradingNotifier
    if notifications.channel == "feishu":
        push = settings.news.push
        if not push.feishu_webhook_url:
            raise RuntimeError("trading_notification_feishu_target_unavailable")
        try:
            sender = FeishuTradingNotifier(
                webhook_url=push.feishu_webhook_url,
                signing_secret=push.feishu_signing_secret,
            )
        except ValueError as exc:
            raise RuntimeError("trading_notification_configuration_invalid") from exc
    else:
        bot_token_path = settings.trading_telegram_bot_token_file()
        chat_id = settings.trading.control.notification_chat_id
        if bot_token_path is None or chat_id is None:
            raise RuntimeError("trading_notification_telegram_target_unavailable")
        try:
            bot_token = read_secure_secret_text(bot_token_path)
        except SecretFileError:
            raise RuntimeError("trading_notification_telegram_target_unavailable") from None
        try:
            sender = TelegramTradingNotifier(bot_token=bot_token, chat_id=chat_id)
        except ValueError as exc:
            raise RuntimeError("trading_notification_configuration_invalid") from exc
    return TradingNotificationWorker(db=db, finite=finite, sender=sender, bars=_source_native_result_bars)
