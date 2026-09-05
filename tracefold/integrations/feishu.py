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

from tracefold.news import COMMIT_PHASE_NOT_SENT, COMMIT_PHASE_UNKNOWN, ReaderCard, ReaderDeliveryPresentation

FEISHU_WEBHOOK_REQUEST_MAX_BYTES = 20 * 1024
FEISHU_WEBHOOK_RATE_LIMIT_CODE = 11232
_FEISHU_WEBHOOK_PATH_PREFIX = "/open-apis/bot/v2/hook/"
_FEISHU_TIMEOUT_SECONDS = 6.5


class FeishuDeliveryError(RuntimeError):
    """A sanitized expected Feishu webhook failure, and what it proves about the message.

    `code` is unchanged and is still what the News Deliverer records. `commit_phase` is additive and
    answers the one question a code cannot: did this message provably not reach Feishu? A connect
    failure and an explicit refusal did not; a read timeout and a 5xx from Feishu's own tier tell us
    nothing either way, and calling those "not sent" is how a retry double-notifies a reader (#553).
    """

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        commit_phase: str = COMMIT_PHASE_UNKNOWN,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.commit_phase = commit_phase
        self.retryable = retryable


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
                raise FeishuDeliveryError("feishu_timestamp_invalid", commit_phase=COMMIT_PHASE_NOT_SENT)
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
            raise FeishuDeliveryError("feishu_card_invalid", commit_phase=COMMIT_PHASE_NOT_SENT) from None
        if len(body) > FEISHU_WEBHOOK_REQUEST_MAX_BYTES:
            raise FeishuDeliveryError("feishu_card_too_large", commit_phase=COMMIT_PHASE_NOT_SENT)

        try:
            response = self._client.post(self._webhook_url, content=body)
        except _PRE_CONNECT_FAILURES:
            # The connection was never established, so no byte of this card left the process. That is
            # the one transport failure a retry cannot duplicate.
            raise FeishuDeliveryError(
                "feishu_transport_failed", commit_phase=COMMIT_PHASE_NOT_SENT, retryable=True
            ) from None
        except (httpx.TimeoutException, httpx.TransportError):
            # Written, and the answer was not read. Feishu may have the card.
            raise FeishuDeliveryError("feishu_transport_failed") from None

        status_code = int(response.status_code)
        if status_code == 429:
            raise FeishuDeliveryError(
                "feishu_http_failed",
                status_code=status_code,
                commit_phase=COMMIT_PHASE_NOT_SENT,
                retryable=True,
            )
        if status_code >= 500:
            # Deliberately not "not sent". A 5xx is Feishu's own tier answering, and it can answer
            # that way after having accepted the card.
            raise FeishuDeliveryError("feishu_http_failed", status_code=status_code)
        if status_code < 200 or status_code >= 300:
            raise FeishuDeliveryError(
                "feishu_http_rejected", status_code=status_code, commit_phase=COMMIT_PHASE_NOT_SENT
            )
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
            raise FeishuDeliveryError(
                "feishu_business_rate_limited",
                status_code=status_code,
                commit_phase=COMMIT_PHASE_NOT_SENT,
                retryable=True,
            )
        if code != 0:
            raise FeishuDeliveryError(
                "feishu_business_rejected", status_code=status_code, commit_phase=COMMIT_PHASE_NOT_SENT
            )
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


# httpx raises these before any request byte is written: no connection, no proxy, no route. Every
# other transport failure happened at or after the write, so it cannot claim the card did not arrive.
_PRE_CONNECT_FAILURES = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError, httpx.UnsupportedProtocol)


class NewsPushExternalError(RuntimeError):
    """The sender's own error. `code` is what News records; the rest is what the adapter proved."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        commit_phase: str = COMMIT_PHASE_UNKNOWN,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.commit_phase = commit_phase
        self.retryable = retryable
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

    def prepare(self) -> None:
        """Feishu has no separate target preflight; keep the delivery lifecycle uniform."""

    def send_card(
        self,
        card: ReaderCard,
        *,
        channel_payload: Mapping[str, Any],
        presentation: ReaderDeliveryPresentation | None = None,
    ) -> dict[str, Any]:
        # `channel_payload` is this channel's own wire shape, serialized from `card` and frozen in the
        # delivery ledger before the first attempt. Feishu posts exactly that snapshot, so a retry
        # sends the bytes the ledger holds rather than a re-rendering of the same facts.
        del card, presentation  # The card model is the model-rendering channels'; rich context is adapter-only.
        try:
            receipt = self._client.send(dict(channel_payload), timestamp_seconds=self._timestamp_seconds())
        except FeishuDeliveryError as exc:
            raise NewsPushExternalError(
                f"news_delivery_{exc.code}",
                status_code=exc.status_code,
                commit_phase=exc.commit_phase,
                retryable=exc.retryable,
            ) from None
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
