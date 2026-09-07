"""Push configuration selects exactly one provider without exposing credentials."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from tracefold.app.cli.commands.config import handle_config
from tracefold.integrations.feishu import FeishuWebhookClient
from tracefold.platform.config import models
from tracefold.platform.config.loader import write_default_config
from tracefold.platform.config.models import Settings, news_push_availability

CHANNEL_ID = -1001234567890


def _telegram_settings(
    tmp_path: Path,
    *,
    chat_id: object = CHANNEL_ID,
    proxy_url: object = None,
    min_interval_seconds: float | None = None,
) -> Settings:
    push: dict[str, object] = {
        "enabled": True,
        "telegram_bot_token_file": "telegram_bot_token",
        "telegram_chat_id": chat_id,
        "telegram_proxy_url": proxy_url,
    }
    if min_interval_seconds is not None:
        push["min_interval_seconds"] = min_interval_seconds
    settings = Settings.model_validate({"news": {"enabled": True, "push": push}})
    settings.set_config_dir(tmp_path)
    return settings


def _readable_token(tmp_path: Path) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("123456:abcdefghijklmnopqrstuvwxyzABCDE_12345\n", encoding="utf-8")
    token_file.chmod(0o600)


def test_telegram_push_requires_a_secure_token_file_and_private_channel(tmp_path: Path) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("123456:abcdefghijklmnopqrstuvwxyzABCDE_12345\n", encoding="utf-8")
    token_file.chmod(0o600)
    availability = news_push_availability(_telegram_settings(tmp_path))

    assert availability.provider == "telegram"
    assert availability.delivery_available is True
    assert availability.telegram_bot_token_file_configured is True
    assert availability.telegram_chat_id_configured is True


def test_a_telegram_target_paced_for_feishu_is_reported_and_still_delivers(tmp_path: Path) -> None:
    """#604 N3: provider-aware pacing is advice printed beside a working configuration, not a gate.

    Telegram admits about 20 messages a minute to one chat. The 0.6 s default was chosen against
    Feishu's custom bot, which admits about 100, so a deployment that switches provider and keeps the
    number is refused by Telegram roughly four times in five. That is worth telling an operator and
    it is not worth refusing their configuration over: the number stays theirs, delivery stays
    available, and nothing in the process reads this field to decide anything.
    """

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("123456:abcdefghijklmnopqrstuvwxyzABCDE_12345\n", encoding="utf-8")
    token_file.chmod(0o600)

    default_paced = news_push_availability(_telegram_settings(tmp_path))
    advised = news_push_availability(_telegram_settings(tmp_path, min_interval_seconds=3.0))

    assert default_paced.pacing_warning == "news_item_push_telegram_interval_below_provider_rate"
    assert default_paced.delivery_available is True
    assert default_paced.reason is None
    assert advised.pacing_warning is None
    assert advised.delivery_available is True


def test_a_feishu_target_at_its_own_safe_interval_is_never_advised_about_telegrams(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                },
            }
        }
    )
    settings.set_config_dir(tmp_path)

    availability = news_push_availability(settings)

    assert availability.provider == "feishu"
    assert availability.pacing_warning is None
    assert settings.news.push.min_interval_seconds == 0.6


def test_telegram_push_fails_closed_when_token_file_permissions_are_open(tmp_path: Path) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("not-returned-in-diagnostics\n", encoding="utf-8")
    token_file.chmod(0o644)

    availability = news_push_availability(_telegram_settings(tmp_path))

    assert availability.delivery_available is False
    assert availability.reason == "news_item_push_telegram_bot_token_unavailable"
    assert availability.telegram_bot_token_file_configured is False


def test_telegram_push_fails_closed_when_token_file_content_is_not_a_bot_token(tmp_path: Path) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("not-a-telegram-bot-token\n", encoding="utf-8")
    token_file.chmod(0o600)

    availability = news_push_availability(_telegram_settings(tmp_path))

    assert availability.delivery_available is False
    assert availability.reason == "news_item_push_telegram_bot_token_unavailable"
    assert availability.telegram_bot_token_file_configured is False


def test_serve_can_report_configured_push_without_reading_the_workers_only_secret(tmp_path: Path) -> None:
    availability = news_push_availability(_telegram_settings(tmp_path), inspect_secret_file=False)

    assert availability.provider == "telegram"
    assert availability.delivery_available is True
    assert availability.telegram_bot_token_file_configured is True


@pytest.mark.parametrize(
    ("chat_id", "stored"),
    [
        # A public channel is named by its `@name`; both of Telegram's own ways of naming one channel
        # reach the adapter as the operator wrote them (#562 §5 rows 1 and 11).
        ("@channel", "@channel"),
        ("-1001234567890", -1_001_234_567_890),
        # And a shape neither the adapter nor Telegram can address still loads: the process starts and
        # the delivery capability is what carries the fault.
        ("not-a-number", "not-a-number"),
        (True, "True"),
    ],
)
def test_telegram_push_reads_the_chat_target_the_operator_wrote(
    tmp_path: Path, chat_id: object, stored: object
) -> None:
    """`Settings` reads the operator's target; the adapter owns what a valid one looks like.

    #562 §5 rows 1 and 8: the private-channel shape used to be enforced here as well as in
    `TelegramNewsPushSender`, and this copy refused the whole configuration -- so a mistyped digit took
    reception, triage and the market loop down with the process, with nothing left running to say so.
    """

    settings = _telegram_settings(tmp_path, chat_id=chat_id)

    assert settings.news.push.telegram_chat_id == stored
    assert news_push_availability(settings).telegram_chat_id_configured is True


@pytest.mark.parametrize("chat_id", [0, 123456789, -123456789])
def test_a_chat_id_that_is_not_a_private_channel_still_loads_and_reports_itself(tmp_path: Path, chat_id: int) -> None:
    settings = _telegram_settings(tmp_path, chat_id=chat_id)

    assert settings.news.push.telegram_chat_id == chat_id
    # The configuration is loadable, so the process starts and the capability can name the fault --
    # `tests/test_news_push_wiring.py` proves the sender refuses it and delivery reads `unavailable`.
    assert news_push_availability(settings).telegram_chat_id_configured is True


def test_push_rejects_ambiguous_feishu_and_telegram_provider_configuration(tmp_path: Path) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("123456:abcdefghijklmnopqrstuvwxyzABCDE_12345\n", encoding="utf-8")
    token_file.chmod(0o600)
    settings = _telegram_settings(tmp_path)
    settings.news.push.feishu_webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/example"

    availability = news_push_availability(settings)

    assert availability.provider is None
    assert availability.delivery_available is False
    assert availability.reason == "news_item_push_provider_conflict"


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://127.0.0.1:7890",
        "https://proxy.internal:8443",
        "socks5://127.0.0.1:1080",
        "socks5h://user:secret@127.0.0.1:1080",
    ],
)
def test_a_routable_proxy_leaves_telegram_delivery_available(tmp_path: Path, proxy_url: str) -> None:
    """#604 N2: the one key that gives this channel a way out of a host that cannot reach it directly."""

    _readable_token(tmp_path)

    availability = news_push_availability(_telegram_settings(tmp_path, proxy_url=proxy_url))

    assert availability.provider == "telegram"
    assert availability.delivery_available is True
    assert availability.telegram_proxy_configured is True


def test_no_proxy_is_the_default_and_reports_itself_as_absent(tmp_path: Path) -> None:
    _readable_token(tmp_path)

    availability = news_push_availability(_telegram_settings(tmp_path))

    assert availability.delivery_available is True
    assert availability.telegram_proxy_configured is False


@pytest.mark.parametrize(
    "proxy_url",
    [
        "127.0.0.1:1080",
        "ftp://127.0.0.1:1080",
        "http://",
        "socks4://127.0.0.1:1080",
        "http://127.0.0.1:7890/some/path",
    ],
)
def test_a_proxy_this_process_cannot_route_through_is_a_delivery_reason(tmp_path: Path, proxy_url: str) -> None:
    """The configuration loads and the process runs; one capability carries the fault (#562 §5 row 1)."""

    _readable_token(tmp_path)

    availability = news_push_availability(_telegram_settings(tmp_path, proxy_url=proxy_url))

    assert availability.delivery_available is False
    assert availability.reason == "news_item_push_telegram_proxy_invalid"
    assert availability.telegram_proxy_configured is True


def test_a_socks_proxy_without_the_socks_codec_is_a_delivery_reason_not_a_dead_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """httpx raises `ImportError` for a SOCKS proxy it has no codec for, and an `ImportError` out of a
    sender constructor is not a `ValueError`: it escapes the composition seam and takes reception,
    triage and the market loop down with the process (#562 §5 row 1, #604 N2)."""

    _readable_token(tmp_path)
    monkeypatch.setattr(models, "_socks_supported", lambda: False)

    availability = news_push_availability(_telegram_settings(tmp_path, proxy_url="socks5h://127.0.0.1:1080"))

    assert availability.delivery_available is False
    assert availability.reason == "news_item_push_telegram_proxy_socks_unsupported"


def test_an_http_proxy_needs_no_socks_codec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _readable_token(tmp_path)
    monkeypatch.setattr(models, "_socks_supported", lambda: False)

    availability = news_push_availability(_telegram_settings(tmp_path, proxy_url="http://127.0.0.1:7890"))

    assert availability.delivery_available is True


def test_the_operator_report_says_whether_a_proxy_is_configured_and_never_which(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy URL commonly carries credentials, and this report is what an operator pastes into an issue."""

    monkeypatch.setenv("HOME", str(tmp_path))
    path = write_default_config()
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("  push:\n    enabled: false\n", "  push:\n    enabled: true\n")
        .replace("    telegram_bot_token_file:\n", "    telegram_bot_token_file: telegram_bot_token\n")
        .replace("    telegram_chat_id:\n", f"    telegram_chat_id: {CHANNEL_ID}\n")
        .replace("    telegram_proxy_url:\n", '    telegram_proxy_url: "socks5h://tracefold:hunter2@127.0.0.1:1080"\n'),
        encoding="utf-8",
    )
    _readable_token(path.parent)

    _code, payload = handle_config(Namespace())

    push = payload["data"]["news"]["push"]
    assert push["provider"] == "telegram"
    assert push["telegram_proxy_configured"] is True
    assert "telegram_proxy_url" not in push
    assert "hunter2" not in json.dumps(payload, default=str)


def _feishu_settings(webhook_url: str) -> Settings:
    return Settings.model_validate(
        {"news": {"enabled": True, "push": {"enabled": True, "feishu_webhook_url": webhook_url}}}
    )


@pytest.mark.parametrize(
    "webhook_url",
    [
        "http://open.feishu.cn/open-apis/bot/v2/hook/abc123",
        "https://open.feishu.cn:8443/open-apis/bot/v2/hook/abc123",
        "https://evil.example/open-apis/bot/v2/hook/abc123",
        "https://user:pw@open.feishu.cn/open-apis/bot/v2/hook/abc123",
        "https://open.feishu.cn/open-apis/bot/v2/hook/abc123?x=1",
        "https://open.feishu.cn/open-apis/bot/v2/hook/abc123#f",
        "https://open.feishu.cn/open-apis/bot/v2/hook/",
        "https://open.feishu.cn/open-apis/bot/v2/hook/abc123/extra",
        "https://open.feishu.cn/some/other/path",
    ],
)
def test_one_webhook_rule_answers_for_both_the_operator_report_and_the_client(webhook_url: str) -> None:
    """#604 N2: the shape was written down twice, and two copies of one rule can disagree.

    The report an operator reads before any sender exists and the client that refuses to post
    anywhere else are now the same sentence, so a URL cannot be sayable in one and unsayable in the
    other.
    """

    assert news_push_availability(_feishu_settings(webhook_url)).reason == "news_item_push_feishu_webhook_invalid"
    with pytest.raises(ValueError, match="news_push_feishu_webhook_url_invalid"):
        FeishuWebhookClient(webhook_url=webhook_url)


def test_the_one_shape_both_accept_is_one_hook_id_on_the_exact_origin() -> None:
    webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/abc123"

    availability = news_push_availability(_feishu_settings(webhook_url))

    assert availability.provider == "feishu"
    assert availability.delivery_available is True
    FeishuWebhookClient(webhook_url=webhook_url).close()
