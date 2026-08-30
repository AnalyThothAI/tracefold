"""Deprecated CLI boundary for Telegram development-test news."""

from __future__ import annotations

from typing import Any

from tracefold.app.telegram_test_news import test_card as _test_card
from tracefold.app.telegram_test_news import test_headline as _test_headline
from tracefold.app.telegram_test_news import test_targets as _test_targets
from tracefold.platform.config.models import Settings


def handle_trading_test_news(settings: Settings, args: Any, *, now_ms: int) -> tuple[int, dict[str, Any]]:
    del settings, args, now_ms
    return 2, {
        "ok": False,
        "error": "telegram_test_news_private_command_required",
        "commands": ["/test_futures", "/test_onchain"],
    }


__all__ = ["_test_card", "_test_headline", "_test_targets", "handle_trading_test_news"]
