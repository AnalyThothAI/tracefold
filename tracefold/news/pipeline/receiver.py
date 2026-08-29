"""Authenticated OpenNews WebSocket receiver stage."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from typing import Any, ClassVar

from ..bus import RK_RAW_LIVE, BusMessage, DeferError, TransientError, new_trace_id, now_ms
from ..opennews import OpenNewsExpectedError, parse_opennews_message
from ..telemetry import NewsWorkSemantics
from .recovery import RecoveryRunner
from .runtime import NewsDatabasePort, _receive_or_stop, _sleep_or_stop

log = logging.getLogger("tracefold.news")

_WS_RECONNECT_SECONDS = 3.0
_WS_CAUSE = {
    "opennews_network_connect": "network_connect",
    "opennews_authentication": "authentication",
    "opennews_provider_close": "provider_close",
    "opennews_protocol_error": "protocol_error",
    "opennews_idle_timeout": "idle_timeout",
}


def _cause_for(code: str | None) -> str:
    if not code:
        return "unknown"
    for prefix, cause in _WS_CAUSE.items():
        if code.startswith(prefix):
            return cause
    return "unknown"


class OpenNewsReceiver:
    """WSS -> broker. Publishes each accepted frame with confirms; overflow/unavailability become incidents."""

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("durable_event",)

    def __init__(
        self,
        *,
        bus: Any,
        db: NewsDatabasePort,
        ws_client: Any | None,
        history_client: Any | None,
        recovery: RecoveryRunner | None,
    ) -> None:
        self.bus = bus
        self.db = db
        self.ws_client = ws_client
        self.history_client = history_client
        self.recovery = recovery
        self._backpressure_open = False

    async def run(self, *, stop_event: asyncio.Event) -> None:
        if self.ws_client is None:
            await stop_event.wait()
            return
        while not stop_event.is_set():
            try:
                await self.ws_client.connect()
                await self._connected()
                while not stop_event.is_set():
                    message = await _receive_or_stop(self.ws_client, stop_event=stop_event)
                    if message is None:
                        break
                    event = parse_opennews_message(message)
                    if event is None:
                        continue
                    await self._publish_frame(message, strategy_id=str(event.provider_metadata["strategies"][0]["id"]))
            except asyncio.CancelledError:
                raise
            except OpenNewsExpectedError as exc:
                await self._disconnected(cause=_cause_for(exc.code), close_code=exc.status_code, error_code=exc.code)
            except Exception as exc:
                log.exception("news receiver failed")
                await self._disconnected(cause="unknown", close_code=None, error_code=type(exc).__name__)
            finally:
                with contextlib.suppress(Exception):
                    await self.ws_client.close()
            if not stop_event.is_set():
                await _sleep_or_stop(stop_event, _WS_RECONNECT_SECONDS)
        await self._disconnected(cause="planned_shutdown", close_code=None, error_code=None, planned=True)

    async def _publish_frame(self, message: Mapping[str, Any], *, strategy_id: str) -> None:
        params = message.get("params") if isinstance(message, Mapping) else None
        if not isinstance(params, Mapping):
            return
        stamp = now_ms()
        payload = {"params": dict(params), "strategy_id": strategy_id, "ingest_mode": "live", "observed_at_ms": stamp}
        msg = BusMessage(
            kind="raw",
            message_id=f"raw:{params.get('id')}",
            routing_key=RK_RAW_LIVE.format(strategy_id=strategy_id),
            payload=payload,
            trace_id=new_trace_id(),
            occurred_at_ms=stamp,
        )
        try:
            await self.bus.publish(msg)
        except Exception as exc:  # BrokerBackpressure / BrokerUnavailable
            cause = "broker_backpressure" if type(exc).__name__ == "BrokerBackpressure" else "broker_unavailable"
            if not self._backpressure_open:
                self._backpressure_open = True
                with contextlib.suppress(TransientError, DeferError):
                    await self.db.tx(
                        "news_ingest_backpressure",
                        lambda repos: (
                            repos.news.open_incident(cause_class=cause, now_ms=stamp),
                            repos.news.update_ingest_state(now_ms=stamp, last_error_code=cause),
                        ),
                    )
            return
        if self._backpressure_open:
            self._backpressure_open = False
            with contextlib.suppress(TransientError, DeferError):
                await self.db.tx(
                    "news_ingest_backpressure_close",
                    lambda repos: repos.news.close_open_incidents(
                        cause_classes=["broker_backpressure", "broker_unavailable"], now_ms=stamp
                    ),
                )
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_ingest_frame",
                lambda repos: repos.news.update_ingest_state(
                    now_ms=stamp, last_frame_at_ms=stamp, last_publish_at_ms=stamp, clear_error=True
                ),
                timeout_seconds=1.0,
            )

    async def _connected(self) -> None:
        stamp = now_ms()

        def _fn(repos: Any) -> None:
            repos.news.close_open_incidents(
                cause_classes=[*_WS_CAUSE.values(), "unknown", "process_outage", "planned_shutdown"],
                now_ms=stamp,
            )
            repos.news.update_ingest_state(now_ms=stamp, connected=True, clear_error=True)

        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx("news_ingest_connected", _fn)
        if self.recovery is not None:
            self.recovery.request()

    async def _disconnected(
        self, *, cause: str, close_code: int | None, error_code: str | None, planned: bool = False
    ) -> None:
        stamp = now_ms()

        def _fn(repos: Any) -> None:
            repos.news.open_incident(cause_class=cause, now_ms=stamp, planned=planned, close_code=close_code)
            repos.news.update_ingest_state(now_ms=stamp, connected=False, last_error_code=error_code)

        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx("news_ingest_disconnected", _fn)
