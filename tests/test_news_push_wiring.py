"""Worker wiring binds Telegram delivery to one secure target, and confines what fails building it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import PoolTimeout

from tracefold.app.workers.runtime import (
    NEWS_DELIVERY,
    NEWS_EDITORIAL,
    NEWS_INGESTION,
    CapabilityStates,
)
from tracefold.app.workers.task_contract import worker_business_tasks
from tracefold.app.workers.wiring import news as news_wiring
from tracefold.app.workers.wiring.components import _wire_components
from tracefold.platform.config.models import Settings

CHANNEL_ID = -1001234567890
BOT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345"


def _settings(tmp_path: Path) -> Settings:
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_id": CHANNEL_ID,
                },
            }
        }
    )
    settings.set_config_dir(tmp_path)
    return settings


def test_worker_reads_the_secure_token_and_binds_the_configured_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    captured: dict[str, Any] = {}
    sender = object()

    def build_sender(*, bot_token: str, chat_id: int | str) -> object:
        captured.update(bot_token=bot_token, chat_id=chat_id)
        return sender

    monkeypatch.setattr(news_wiring, "TelegramNewsPushSender", build_sender)

    composed = news_wiring._news_push_sender(_settings(tmp_path))
    assert composed.sender is sender and composed.reason is None
    assert captured == {"bot_token": BOT_TOKEN, "chat_id": CHANNEL_ID}


def test_worker_does_not_construct_a_sender_from_an_insecure_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o644)
    constructed = False

    def build_sender(**_kwargs: object) -> object:
        nonlocal constructed
        constructed = True
        return object()

    monkeypatch.setattr(news_wiring, "TelegramNewsPushSender", build_sender)

    composed = news_wiring._news_push_sender(_settings(tmp_path))
    assert composed.sender is None
    assert composed.reason == "news_item_push_telegram_bot_token_unavailable"
    assert constructed is False


def test_worker_leaves_delivery_off_when_push_is_not_requested(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.news.push.enabled = False

    assert news_wiring._news_push_sender(settings) == news_wiring._ComposedPushSender()


def test_a_chat_id_that_is_not_a_private_channel_is_a_delivery_reason_not_a_dead_process(
    tmp_path: Path,
) -> None:
    """#562 §5 rows 1 and 8. A mistyped chat id used to stop `Settings` itself from validating.

    The private-channel shape was written down twice -- in `NewsPushSettings` and in the adapter that
    talks to Telegram -- and the config copy was the expensive one: one wrong digit and the process
    could not start at all, so no `/readyz` and no `tracefold config` could say why reception, triage
    and the market loop were down. The adapter still refuses the target; what it costs now is one
    capability marked `unavailable` beside a running process.
    """

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    settings = _settings(tmp_path)
    settings.news.push.telegram_chat_id = -100123  # too short to be a channel id

    composed = news_wiring._news_push_sender(settings)

    assert composed.sender is None
    assert composed.reason == "news_item_push_telegram_sender_invalid"


def test_a_public_channel_name_is_the_target_the_adapter_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#562 §5 rows 1 and 11: an operator who publishes their channel configures its `@name`."""

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    captured: dict[str, Any] = {}
    sender = object()

    def build_sender(*, bot_token: str, chat_id: int | str) -> object:
        captured.update(bot_token=bot_token, chat_id=chat_id)
        return sender

    monkeypatch.setattr(news_wiring, "TelegramNewsPushSender", build_sender)
    settings = _settings(tmp_path)
    settings.news.push.telegram_chat_id = "@tracefold_feed"

    composed = news_wiring._news_push_sender(settings)

    assert composed.sender is sender and composed.reason is None
    assert captured == {"bot_token": BOT_TOKEN, "chat_id": "@tracefold_feed"}


def test_a_chat_target_of_no_known_shape_is_a_delivery_reason_not_a_dead_process(tmp_path: Path) -> None:
    """A value that is neither an id nor an `@name` loads, and costs exactly the delivery capability.

    #562 §5 rows 1 and 8. `Settings` used to refuse `"@feed"` outright, which is the same outage as a
    mistyped digit: the process could not start, so nothing was left running to report the fault.
    """

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_id": "not-a-channel",
                },
            }
        }
    )
    settings.set_config_dir(tmp_path)

    composed = news_wiring._news_push_sender(settings)

    assert composed.sender is None
    assert composed.reason == "news_item_push_telegram_sender_invalid"


def test_a_push_target_declared_against_disabled_news_is_a_capability_fault_not_a_startup_refusal() -> None:
    """#553 PR-3. A configuration error names the capability it breaks; it does not refuse the process."""

    settings = Settings.model_validate(
        {
            "news": {
                "enabled": False,
                "push": {
                    "enabled": True,
                    "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                },
            }
        }
    )

    components = asyncio.run(
        _wire_components(
            settings=settings,
            db=object(),  # type: ignore[arg-type]
            finite=object(),  # type: ignore[arg-type]
            telemetry=object(),  # type: ignore[arg-type]
        )
    )

    assert components.news_pipeline is None
    assert components.capabilities.payload()[NEWS_DELIVERY] == {
        "state": "unavailable",
        "reason": "news_item_push_news_disabled",
    }


def _composed_pipeline(settings: Settings, capabilities: CapabilityStates) -> Any:
    """Compose the real News pipeline against a bus stub.

    The broker is not the mechanism under test here and stays foundational: what these tests prove is
    that a sender that cannot be built, or a Program that cannot be assembled, leaves the reception
    and admission tasks composed and running (#553 PR-3).
    """

    # #553 PR-2 added the market notification loop to the wiring seam. It is composed here too --
    # it shares the Deliverer's send entry -- but the subject of these tests is the pipeline.
    _, pipeline, _ = asyncio.run(_wire_news_pipeline_with_stub_bus(settings=settings, capabilities=capabilities))
    return pipeline


class _UnusedDatabase:
    """Enough of `WorkerDatabase` to compose the pipeline; composition opens no transaction."""

    def heavy_business(self) -> object:
        return object()


async def _wire_news_pipeline_with_stub_bus(*, settings: Settings, capabilities: CapabilityStates) -> Any:
    async def connect(_settings: Settings, **_kwargs: Any) -> object:
        return object()

    original = news_wiring._connect_news_bus
    news_wiring._connect_news_bus = connect  # type: ignore[assignment]
    try:
        return await news_wiring._wire_news_pipeline(
            settings=settings,
            db=_UnusedDatabase(),  # type: ignore[arg-type]
            finite=object(),  # type: ignore[arg-type]
            capabilities=capabilities,
        )
    finally:
        news_wiring._connect_news_bus = original  # type: ignore[assignment]


def test_a_sender_that_cannot_be_constructed_leaves_the_fact_chain_composed_and_running(
    tmp_path: Path,
) -> None:
    """#553 PR-3 acceptance 2. An unreadable secret file is a delivery fact, not a dead process."""

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o644)
    capabilities = CapabilityStates()

    pipeline = _composed_pipeline(_settings(tmp_path), capabilities)

    assert pipeline.deliverer.sender is None
    assert capabilities.payload()[NEWS_DELIVERY] == {
        "state": "unavailable",
        "reason": "news_item_push_telegram_bot_token_unavailable",
    }
    assert capabilities.payload()[NEWS_INGESTION] == {"state": "running", "reason": None}
    # Reception, admission and retention are all still declared; only the send is missing.
    assert {name for name, _ in pipeline.runners()} >= {"news-deduper", "news-janitor", "news-deliverer"}

    # The Deliverer task still runs -- it settles those Events `delivery_unavailable` rather than
    # dropping them -- so "a task exists" must not be read back as "the capability works". Declaring
    # the task must leave the composition's `unavailable` exactly where composition put it.
    tasks = worker_business_tasks(news_pipeline=pipeline, signal_lane=None)
    assert ("news-deliverer", NEWS_DELIVERY) in {(task.name, task.capability) for task in tasks}
    assert capabilities.payload()[NEWS_DELIVERY]["state"] == "unavailable"


def test_a_postgresql_failure_during_program_assembly_is_not_an_editorial_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#553 PR-3. A shared PostgreSQL fault must not be reported as one capability's program error.

    `_compose_program_arms` runs the canary startup reconciliation, which is a control write. A pool
    timeout there says the database failed; confining it would leave a green readiness beside it.
    """

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise PoolTimeout("couldn't get a connection after 1.0 sec")

    monkeypatch.setattr(news_wiring, "_compose_program_arms", refuse)
    capabilities = CapabilityStates()

    with pytest.raises(PoolTimeout):
        _composed_pipeline(_settings(tmp_path), capabilities)
    assert NEWS_EDITORIAL not in capabilities.payload()


def test_the_market_loop_is_composed_with_the_console_origin_the_operator_named(tmp_path: Path) -> None:
    """#553. The wiring is the only place `api.public_url` is read; the card cannot invent one."""

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    settings = _settings(tmp_path)
    settings.api.public_url = "https://tracefold.example.com"

    _, _, market_loop = asyncio.run(
        _wire_news_pipeline_with_stub_bus(settings=settings, capabilities=CapabilityStates())
    )

    assert market_loop.console_base_url == "https://tracefold.example.com"


def test_a_deployment_that_named_no_console_composes_the_market_loop_without_one(tmp_path: Path) -> None:
    """Unset is not a guess. The loop is still composed and still sends -- without the detail button."""

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    settings = _settings(tmp_path)
    assert settings.api.public_url is None

    _, _, market_loop = asyncio.run(
        _wire_news_pipeline_with_stub_bus(settings=settings, capabilities=CapabilityStates())
    )

    assert market_loop.console_base_url is None


def test_a_program_that_cannot_be_assembled_faults_editorial_and_leaves_the_rest_composed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#553 PR-3 acceptance 3. The version check still refuses; it just no longer takes News with it."""

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("news_stable_program_manifest_mismatch")

    monkeypatch.setattr(news_wiring, "_compose_program_arms", refuse)
    capabilities = CapabilityStates()

    pipeline = _composed_pipeline(_settings(tmp_path), capabilities)

    assert pipeline.triage is None
    assert pipeline.runtime_manifest_sha is None
    assert capabilities.payload()[NEWS_EDITORIAL] == {
        "state": "faulted",
        "reason": "news_editorial_assembly_failed:RuntimeError",
    }
    assert capabilities.payload()[NEWS_INGESTION] == {"state": "running", "reason": None}
    task_names = {name for name, _ in pipeline.runners()}
    assert "news-triage" not in task_names
    assert {"news-deduper", "news-deliverer", "news-janitor"} <= task_names
