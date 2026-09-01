from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tracefold.app.workers.wiring import components
from tracefold.platform.config.models import Settings

_CHAT_ID = -100433004
_USER_ID = 433004
_BOT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345"
_WEBHOOK_SECRET = "test-webhook-secret-433d"


def _settings(tmp_path: Path) -> Settings:
    settings = Settings.model_validate(
        {
            "trading": {
                "control": {
                    "enabled": True,
                    "allowed_chat_ids": [_CHAT_ID],
                    "allowed_user_ids": [_USER_ID],
                    "notification_chat_id": _CHAT_ID,
                }
            }
        }
    )
    settings.set_config_dir(tmp_path)
    return settings


def _write_secret(path: Path, value: str, *, mode: int = 0o600) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)


def test_workers_wires_control_only_from_two_secure_files_and_redacted_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_secret(tmp_path / "telegram_bot_token", _BOT_TOKEN)
    _write_secret(tmp_path / "telegram_webhook_secret", _WEBHOOK_SECRET)
    captured: dict[str, Any] = {}

    class Webhook:
        def __init__(self, **kwargs: object) -> None:
            captured["webhook"] = kwargs

    monkeypatch.setattr(components, "TelegramControlWebhook", Webhook)

    ingress = components._wire_telegram_control(
        settings=_settings(tmp_path),
        db=object(),  # type: ignore[arg-type]
    )

    # #458 PR-B: the command ingress no longer builds the notification channel, and no longer builds
    # an HTTP client to read a number out of the token. The bot id it authenticates against is the
    # token's left half, read directly.
    assert ingress is not None
    assert captured == {
        "webhook": {
            "webhook_secret": _WEBHOOK_SECRET,
            "bot_id": int(_BOT_TOKEN.partition(":")[0]),
            "allowed_chat_ids": frozenset({_CHAT_ID}),
            "allowed_user_ids": frozenset({_USER_ID}),
            "target_profile_id": "binance_usdm_primary",
        },
    }


def test_workers_rejects_insecure_control_secret_before_constructing_an_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_secret(tmp_path / "telegram_bot_token", _BOT_TOKEN)
    _write_secret(tmp_path / "telegram_webhook_secret", _WEBHOOK_SECRET, mode=0o644)
    constructed = False

    def construct(**_kwargs: object) -> object:
        nonlocal constructed
        constructed = True
        return object()

    monkeypatch.setattr(components, "TelegramControlWebhook", construct)

    with pytest.raises(RuntimeError, match="trading_control_secret_unavailable"):
        components._wire_telegram_control(
            settings=_settings(tmp_path),
            db=object(),  # type: ignore[arg-type]
        )
    assert constructed is False
