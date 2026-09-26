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

    def build_sender(*, bot_token: str, chat_id: int | str, proxy_url: str | None) -> object:
        captured.update(bot_token=bot_token, chat_id=chat_id, proxy_url=proxy_url)
        return sender

    monkeypatch.setattr(news_wiring, "TelegramNewsPushSender", build_sender)

    composed = news_wiring._news_push_sender(_settings(tmp_path))
    assert composed.sender is sender and composed.reason is None
    assert captured == {"bot_token": BOT_TOKEN, "chat_id": CHANNEL_ID, "proxy_url": None}


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

    def build_sender(*, bot_token: str, chat_id: int | str, proxy_url: str | None) -> object:
        captured.update(bot_token=bot_token, chat_id=chat_id, proxy_url=proxy_url)
        return sender

    monkeypatch.setattr(news_wiring, "TelegramNewsPushSender", build_sender)
    settings = _settings(tmp_path)
    settings.news.push.telegram_chat_id = "@tracefold_feed"

    composed = news_wiring._news_push_sender(settings)

    assert composed.sender is sender and composed.reason is None
    assert captured == {"bot_token": BOT_TOKEN, "chat_id": "@tracefold_feed", "proxy_url": None}


def test_the_configured_proxy_is_handed_to_the_adapter_that_makes_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#604 N2: a host that cannot reach `api.telegram.org` directly configures the route that can.

    Nothing between the operator and the socket reads the environment for this: the sender always
    builds its own transport, and httpx only consults `HTTPS_PROXY` for a client that builds one for
    itself. The proxy therefore has to travel this way or not at all.
    """

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    captured: dict[str, Any] = {}
    sender = object()

    def build_sender(*, bot_token: str, chat_id: int | str, proxy_url: str | None) -> object:
        captured.update(bot_token=bot_token, chat_id=chat_id, proxy_url=proxy_url)
        return sender

    monkeypatch.setattr(news_wiring, "TelegramNewsPushSender", build_sender)
    settings = _settings(tmp_path)
    settings.news.push.telegram_proxy_url = "socks5h://127.0.0.1:1080"

    composed = news_wiring._news_push_sender(settings)

    assert composed.sender is sender and composed.reason is None
    assert captured["proxy_url"] == "socks5h://127.0.0.1:1080"


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


@pytest.mark.parametrize("enabled", [True, False])
def test_wallet_notification_setting_reaches_the_market_loop(tmp_path: Path, enabled: bool) -> None:
    settings = _settings(tmp_path)
    settings.news.chain_tape.notifications_enabled = enabled
    market = asyncio.run(
        _wire_news_pipeline_with_stub_bus(settings=settings, capabilities=CapabilityStates())
    ).market_notifications
    assert market.wallet_notifications_enabled is enabled


def _composed_pipeline(settings: Settings, capabilities: CapabilityStates) -> Any:
    """Compose the real News pipeline against a bus stub.

    The broker is not the mechanism under test here and stays foundational: what these tests prove is
    that a sender that cannot be built, or a semantic runtime that cannot be assembled, leaves the
    reception and admission tasks composed and running (#553 PR-3).
    """

    # #553 PR-2 added the market notification loop to the wiring seam. It is composed here too --
    # it shares the Deliverer's send entry -- but the subject of these tests is the pipeline.
    return asyncio.run(_wire_news_pipeline_with_stub_bus(settings=settings, capabilities=capabilities)).pipeline


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
    tasks = worker_business_tasks(news_pipeline=pipeline)
    assert ("news-deliverer", NEWS_DELIVERY) in {(task.name, task.capability) for task in tasks}
    assert capabilities.payload()[NEWS_DELIVERY]["state"] == "unavailable"


def test_a_postgresql_failure_during_semantic_assembly_is_not_an_editorial_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#553 PR-3. A shared PostgreSQL fault must not be reported as one capability's program error."""

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)

    def refuse(**_kwargs: Any) -> Any:
        raise PoolTimeout("couldn't get a connection after 1.0 sec")

    monkeypatch.setattr(news_wiring, "compose_news_updates", refuse)
    capabilities = CapabilityStates()

    with pytest.raises(PoolTimeout):
        _composed_pipeline(_with_models(tmp_path), capabilities)
    assert NEWS_EDITORIAL not in capabilities.payload()


def test_the_market_loop_is_composed_with_the_console_origin_the_operator_named(tmp_path: Path) -> None:
    """#553. The wiring is the only place `api.public_url` is read; the card cannot invent one."""

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    settings = _settings(tmp_path)
    settings.api.public_url = "https://tracefold.example.com"

    market_loop = asyncio.run(
        _wire_news_pipeline_with_stub_bus(settings=settings, capabilities=CapabilityStates())
    ).market_notifications

    assert market_loop.console_base_url == "https://tracefold.example.com"


def test_a_deployment_that_named_no_console_composes_the_market_loop_without_one(tmp_path: Path) -> None:
    """Unset is not a guess. The loop is still composed and still sends -- without the detail button."""

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    settings = _settings(tmp_path)
    assert settings.api.public_url is None

    market_loop = asyncio.run(
        _wire_news_pipeline_with_stub_bus(settings=settings, capabilities=CapabilityStates())
    ).market_notifications

    assert market_loop.console_base_url is None


def _with_models(tmp_path: Path, **llm: Any) -> Settings:
    settings = Settings.model_validate(
        {
            "llm": {
                "api_key": "news-key",
                "base_url": "https://news-llm.test/v1",
                "news_triage_model": "news-model",
                **llm,
            },
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_id": CHANNEL_ID,
                },
            },
        }
    )
    settings.set_config_dir(tmp_path)
    return settings


def test_a_semantic_runtime_that_cannot_be_assembled_faults_editorial_and_leaves_the_rest_composed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#553 PR-3 acceptance 3, for the #706 semantic worker: the fault is confined to editorial."""

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)

    def refuse(**_kwargs: Any) -> Any:
        raise RuntimeError("news_semantic_runtime_invalid")

    monkeypatch.setattr(news_wiring, "compose_news_updates", refuse)
    capabilities = CapabilityStates()

    wiring = asyncio.run(_wire_news_pipeline_with_stub_bus(settings=_with_models(tmp_path), capabilities=capabilities))

    assert wiring.news_updates is None
    # No agent: the worker only acknowledges wakes so the bounded queue keeps accepting admission's
    # publishes, and the semantic work waits durably in PostgreSQL.
    assert wiring.pipeline.semantic.agent is None
    # Workers cannot claim the configured program it does not run: the deployment check sees that.
    assert wiring.runtime_manifest_sha is None
    assert capabilities.payload()[NEWS_EDITORIAL] == {
        "state": "faulted",
        "reason": "news_editorial_assembly_failed:RuntimeError",
    }
    assert capabilities.payload()[NEWS_INGESTION] == {"state": "running", "reason": None}
    task_names = {name for name, _ in wiring.pipeline.runners()}
    assert {"news-deduper", "news-semantic", "news-deliverer", "news-janitor"} <= task_names


def test_unconfigured_news_models_leave_no_semantic_worker_and_a_disabled_editorial_capability(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    capabilities = CapabilityStates()
    settings = _settings(tmp_path)

    wiring = asyncio.run(_wire_news_pipeline_with_stub_bus(settings=settings, capabilities=capabilities))

    assert wiring.pipeline.semantic.agent is None
    assert capabilities.payload()[NEWS_EDITORIAL] == {"state": "disabled", "reason": "news_models_not_configured"}
    # Admission still commits semantic work; the manifest names the configuration Workers actually runs.
    assert wiring.runtime_manifest_sha == news_wiring.configured_runtime_manifest_sha(settings)


def test_configured_models_compose_the_semantic_worker_as_a_confined_editorial_task(tmp_path: Path) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    capabilities = CapabilityStates()
    settings = _with_models(
        tmp_path,
        trading_semantics={"api_key": "trading-key", "base_url": "https://openrouter.ai/api", "model": "jev-1.13"},
    )

    wiring = asyncio.run(_wire_news_pipeline_with_stub_bus(settings=settings, capabilities=capabilities))

    assert wiring.news_updates is not None
    assert wiring.pipeline.semantic.agent is wiring.news_updates.agent
    assert wiring.pipeline.semantic.program_identity == wiring.news_updates.program_identity
    # Trading's System One route never enables News Jev.
    assert wiring.news_updates.judgment_connection is None
    assert capabilities.payload()[NEWS_EDITORIAL] == {"state": "running", "reason": None}
    assert wiring.runtime_manifest_sha == news_wiring.configured_runtime_manifest_sha(settings)
    tasks = {task.name: task for task in worker_business_tasks(news_pipeline=wiring.pipeline)}
    assert tasks["news-semantic"].capability == NEWS_EDITORIAL
    assert tasks["news-semantic"].foundational is False


def test_a_configured_news_judgment_route_opens_its_own_connection_and_changes_the_program(tmp_path: Path) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    generated = _with_models(tmp_path)
    native = _with_models(
        tmp_path,
        news_judgment={"api_key": "news-jev-key", "base_url": "https://openrouter.ai/api", "model": "jev-1.13"},
    )

    plain = asyncio.run(_wire_news_pipeline_with_stub_bus(settings=generated, capabilities=CapabilityStates()))
    judged = asyncio.run(_wire_news_pipeline_with_stub_bus(settings=native, capabilities=CapabilityStates()))

    assert plain.news_updates is not None and judged.news_updates is not None
    assert plain.news_updates.judgment_connection is None
    assert judged.news_updates.judgment_connection is not None
    assert judged.news_updates.program_identity != plain.news_updates.program_identity
    asyncio.run(judged.news_updates.aclose())
