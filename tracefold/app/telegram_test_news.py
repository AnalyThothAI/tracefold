"""Private-chat-only Telegram development news fixtures for Trading."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.integrations.telegram import TelegramTradingClient, TelegramTradingUpdate
from tracefold.platform.config.models import Settings

_FUTURES_TARGET_RE = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,19}")
_ONCHAIN_TARGET_RE = re.compile(r"(?:[A-Z0-9][A-Z0-9._-]{0,19}|0x[0-9a-f]{40})")
_TEST_SOURCE_TTL_MS = 2 * 60 * 60 * 1_000


class TelegramDevelopmentTestNewsController:
    def __init__(
        self,
        *,
        settings: Settings,
        database: WorkerTradingDatabase,
        client: TelegramTradingClient,
        finite: FiniteOperations,
        clock_ms: Any | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._client = client
        self._finite = finite
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def handle(self, update: TelegramTradingUpdate, *, kind: Literal["futures", "onchain"]) -> str:
        profile = self._settings.trading.telegram_profile(update.actor_user_id)
        if (
            profile is None
            or update.update_kind != "message"
            or update.chat_type != "private"
            or update.chat_id != update.actor_user_id
            or not update.authorized
        ):
            return "telegram_test_news_private_profile_required"
        if kind == "futures" and not profile.manual.enabled:
            return "telegram_test_news_futures_unavailable"
        if kind == "onchain" and not profile.onchain.enabled:
            return "telegram_test_news_onchain_unavailable"

        now_ms = int(self._clock_ms())
        targets = test_targets(kind, "")
        headline = test_headline(kind, "")
        source_id = str(uuid4())
        receipt: dict[str, Any] | None = None
        try:
            receipt = await self._finite.run(
                "telegram_test_news_send",
                self._client.send_development_test_card,
                chat_id=profile.user_id,
                card=test_card(
                    kind=kind,
                    headline=headline,
                    targets=targets,
                    direction="bullish",
                    source_id=source_id,
                    now_ms=now_ms,
                ),
                kind=kind,
                now_ms=now_ms,
                timeout_seconds=8.0,
            )
            await self._database.tx(
                "telegram_test_news_persist",
                lambda repos: repos.trading.insert_telegram_development_test_news(
                    source_id=source_id,
                    delivery_message_id=int(receipt["message_id"]),
                    delivery_target_sha256=str(receipt["target_sha256"]),
                    test_kind=kind,
                    headline_zh=headline,
                    direction="bullish",
                    displayed_targets=targets,
                    source_observed_at_ms=now_ms,
                    expires_at_ms=now_ms + _TEST_SOURCE_TTL_MS,
                    now_ms=now_ms,
                ),
                timeout_seconds=3.0,
            )
        except Exception as exc:
            if receipt is not None:
                try:
                    await self._finite.run(
                        "telegram_test_news_cleanup",
                        self._client.delete_interaction,
                        chat_id=profile.user_id,
                        message_id=int(receipt["message_id"]),
                        timeout_seconds=8.0,
                    )
                except Exception:
                    return "telegram_test_news_persist_cleanup_ambiguous"
            return getattr(exc, "code", None) or "telegram_test_news_failed"
        return f"telegram_test_news_{kind}_sent"


def test_targets(kind: Literal["futures", "onchain"], value: str) -> tuple[str, ...]:
    raw_targets = value.split(",") if value.strip() else (["HYPE"] if kind == "futures" else ["BLUECHIP", "COPPERINU"])
    targets: list[str] = []
    pattern = _FUTURES_TARGET_RE if kind == "futures" else _ONCHAIN_TARGET_RE
    for raw in raw_targets:
        normalized = raw.strip()
        normalized = normalized.lower() if normalized.lower().startswith("0x") else normalized.upper()
        if pattern.fullmatch(normalized) is None:
            raise ValueError("telegram_test_news_target_invalid")
        if normalized not in targets:
            targets.append(normalized)
    if not 1 <= len(targets) <= 4:
        raise ValueError("telegram_test_news_targets_invalid")
    return tuple(targets)


def test_headline(kind: Literal["futures", "onchain"], value: str) -> str:
    headline = (
        value.strip()
        or {
            "futures": "[开发测试] HYPE 合约交易交互",
            "onchain": "[开发测试] BLUECHIP / COPPERINU 链上路由交互",
        }[kind]
    )
    if not 1 <= len(headline) <= 240:
        raise ValueError("telegram_test_news_headline_invalid")
    return headline


def test_card(
    *,
    kind: Literal["futures", "onchain"],
    headline: str,
    targets: tuple[str, ...],
    direction: Literal["bullish", "bearish"],
    source_id: str,
    now_ms: int,
) -> dict[str, Any]:
    direction_label = "利多" if direction == "bullish" else "利空"
    lane_label = "合约交易" if kind == "futures" else "链上路由"
    observed = datetime.fromtimestamp(now_ms / 1_000, tz=UTC).strftime("%H:%M")
    return {
        "header": {
            "title": {"content": headline},
            "template": "green" if direction == "bullish" else "red",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"这是可点击的 {lane_label} 开发测试消息，不代表真实新闻或交易建议。\n"
                    f"{direction_label} · 新事实 · 影响有限 · {' '.join(targets)} · "
                    f"Tracefold 开发测试 · {observed}"
                ),
            },
            {"tag": "note", "elements": [{"tag": "plain_text", "content": f"DEV TEST · {source_id[:8]}"}]},
        ],
    }


__all__ = [
    "TelegramDevelopmentTestNewsController",
    "test_card",
    "test_headline",
    "test_targets",
]
