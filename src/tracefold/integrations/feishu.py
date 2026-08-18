"""Feishu custom-bot webhook: one interactive-card send per call; the News Deliverer owns idempotency."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

FEISHU_WEBHOOK_REQUEST_MAX_BYTES = 20 * 1024
FEISHU_WEBHOOK_RATE_LIMIT_CODE = 11232
_FEISHU_WEBHOOK_PATH_PREFIX = "/open-apis/bot/v2/hook/"
_FEISHU_TIMEOUT_SECONDS = 6.5


class FeishuDeliveryError(RuntimeError):
    """A sanitized expected Feishu webhook failure."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class FeishuDeliveryReceipt:
    status_code: int
    code: int


class FeishuWebhookClient:
    """One V2 custom-bot webhook call; Item Push owns the terminal outcome."""

    def __init__(
        self,
        *,
        webhook_url: str,
        signing_secret: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized_url = str(webhook_url or "").strip()
        normalized_secret = str(signing_secret or "").strip()
        if not _is_feishu_webhook_url(normalized_url):
            raise ValueError("news_push_feishu_webhook_url_invalid")
        self._webhook_url = normalized_url
        self._signing_secret = normalized_secret or None
        self._client = httpx.Client(
            timeout=httpx.Timeout(_FEISHU_TIMEOUT_SECONDS),
            headers={"Content-Type": "application/json; charset=utf-8"},
            follow_redirects=False,
            transport=transport,
        )

    def send(
        self,
        card: Mapping[str, Any],
        *,
        timestamp_seconds: int | None = None,
    ) -> FeishuDeliveryReceipt:
        payload = {
            "msg_type": "interactive",
            "card": dict(card),
        }
        if self._signing_secret is not None:
            timestamp = int(time.time()) if timestamp_seconds is None else int(timestamp_seconds)
            if timestamp <= 0:
                raise FeishuDeliveryError("feishu_timestamp_invalid")
            payload = {
                "timestamp": str(timestamp),
                "sign": generate_feishu_signature(
                    timestamp_seconds=timestamp,
                    signing_secret=self._signing_secret,
                ),
                **payload,
            }
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise FeishuDeliveryError("feishu_card_invalid") from None
        if len(body) > FEISHU_WEBHOOK_REQUEST_MAX_BYTES:
            raise FeishuDeliveryError("feishu_card_too_large")

        try:
            response = self._client.post(self._webhook_url, content=body)
        except (httpx.TimeoutException, httpx.TransportError):
            raise FeishuDeliveryError("feishu_transport_failed") from None

        status_code = int(response.status_code)
        if status_code == 429 or status_code >= 500:
            raise FeishuDeliveryError("feishu_http_failed", status_code=status_code)
        if status_code < 200 or status_code >= 300:
            raise FeishuDeliveryError("feishu_http_rejected", status_code=status_code)
        try:
            response_payload = response.json()
        except ValueError:
            raise FeishuDeliveryError("feishu_response_invalid", status_code=status_code) from None
        if not isinstance(response_payload, Mapping):
            raise FeishuDeliveryError("feishu_response_invalid", status_code=status_code)
        code = response_payload.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise FeishuDeliveryError("feishu_response_invalid", status_code=status_code)
        if code == FEISHU_WEBHOOK_RATE_LIMIT_CODE:
            raise FeishuDeliveryError("feishu_business_rate_limited", status_code=status_code)
        if code != 0:
            raise FeishuDeliveryError("feishu_business_rejected", status_code=status_code)
        return FeishuDeliveryReceipt(status_code=status_code, code=code)

    def close(self) -> None:
        self._client.close()


def generate_feishu_signature(*, timestamp_seconds: int, signing_secret: str) -> str:
    timestamp = int(timestamp_seconds)
    secret = str(signing_secret or "").strip()
    if timestamp <= 0:
        raise ValueError("feishu_timestamp_invalid")
    if not secret:
        raise ValueError("news_push_feishu_signing_secret_required")
    string_to_sign = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _is_feishu_webhook_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    hook_id = parsed.path.removeprefix(_FEISHU_WEBHOOK_PATH_PREFIX)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "open.feishu.cn"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path.startswith(_FEISHU_WEBHOOK_PATH_PREFIX)
        and hook_id
        and "/" not in hook_id
    )


class NewsPushExternalError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FeishuNewsPushSender:
    """Send one News card; the caller owns idempotency and settlement, there are no retries here."""

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


__all__ = [
    "FEISHU_WEBHOOK_RATE_LIMIT_CODE",
    "FEISHU_WEBHOOK_REQUEST_MAX_BYTES",
    "FeishuDeliveryError",
    "FeishuDeliveryReceipt",
    "FeishuNewsPushSender",
    "FeishuWebhookClient",
    "NewsPushExternalError",
    "generate_feishu_signature",
]
