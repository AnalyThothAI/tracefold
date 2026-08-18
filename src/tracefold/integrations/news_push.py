"""Feishu webhook sender for News V3 cards: one attempt, structured receipt, no retries."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from tracefold.integrations.feishu import FeishuDeliveryError, FeishuWebhookClient


class NewsPushExternalError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FeishuNewsPushSender:
    """Send one interactive card; the caller owns idempotency and settlement."""

    def __init__(
        self,
        *,
        webhook_url: str,
        signing_secret: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timestamp_seconds: Callable[[], int] | None = None,
    ) -> None:
        self._client = FeishuWebhookClient(webhook_url=webhook_url, signing_secret=signing_secret, transport=transport)
        self._timestamp_seconds = timestamp_seconds or (lambda: int(time.time()))

    def send_card(self, card: Mapping[str, Any]) -> dict[str, Any]:
        try:
            receipt = self._client.send(dict(card), timestamp_seconds=self._timestamp_seconds())
        except FeishuDeliveryError as exc:
            raise NewsPushExternalError(f"news_delivery_{exc.code}") from None
        return {"provider": "feishu", "code": receipt.code, "status_code": receipt.status_code}

    def close(self) -> None:
        self._client.close()


__all__ = ["FeishuNewsPushSender", "NewsPushExternalError"]
