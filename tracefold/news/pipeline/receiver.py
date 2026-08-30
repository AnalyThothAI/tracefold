"""Authenticated OpenNews WebSocket receiver stage."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, ClassVar

from ..bus import RK_RAW_LIVE, BrokerBackpressure, BrokerUnavailable, BusMessage, new_trace_id, now_ms
from ..opennews import OpenNewsExpectedError, parse_opennews_message
from ..telemetry import NewsWorkSemantics
from .recovery import RecoveryRunner
from .runtime import NewsDatabasePort, _receive_or_stop, _sleep_or_stop

_WS_RECONNECT_SECONDS = 3.0
_WS_CAUSE = {
    "opennews_authentication_failed": "authentication",
    "opennews_connect_failed": "network_connect",
    "opennews_handshake_failed": "network_connect",
    "opennews_not_connected": "network_connect",
    "opennews_receive_failed": "provider_close",
    "opennews_protocol_error": "protocol_error",
    "opennews_idle_timeout": "idle_timeout",
}
_WS_INCIDENT_CAUSES = tuple(dict.fromkeys(_WS_CAUSE.values()))


def _cause_for(code: str) -> str | None:
    return _WS_CAUSE.get(code)


class OpenNewsReceiver:
    """WSS -> broker. Publishes each accepted frame with confirms; overflow/unavailability become incidents."""

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("durable_event",)

    def __init__(
        self,
        *,
        bus: Any,
        db: NewsDatabasePort,
        ws_client: Any | None,
        recovery: RecoveryRunner | None,
    ) -> None:
        self.bus = bus
        self.db = db
        self.ws_client = ws_client
        self.recovery = recovery

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
                cause = _cause_for(exc.code)
                if cause is None:
                    raise
                await self._disconnected(cause=cause, close_code=exc.status_code, error_code=exc.code)
            finally:
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
        except (BrokerBackpressure, BrokerUnavailable) as exc:
            cause = "broker_backpressure" if isinstance(exc, BrokerBackpressure) else "broker_unavailable"
            await self.db.tx(
                "news_ingest_backpressure",
                lambda repos: (
                    repos.news.open_incident(cause_class=cause, now_ms=stamp),
                    repos.news.update_ingest_state(now_ms=stamp, last_frame_at_ms=stamp, last_error_code=cause),
                ),
            )
            return

        def _published(repos: Any) -> int:
            closed = repos.news.close_open_incidents(
                cause_classes=["broker_backpressure", "broker_unavailable"], now_ms=stamp
            )
            repos.news.update_ingest_state(
                now_ms=stamp, last_frame_at_ms=stamp, last_publish_at_ms=stamp, clear_error=True
            )
            return int(closed)

        closed = await self.db.tx("news_ingest_frame", _published, timeout_seconds=1.0)
        if closed > 0 and self.recovery is not None:
            self.recovery.request()

    async def _connected(self) -> None:
        stamp = now_ms()

        def _fn(repos: Any) -> int:
            closed = repos.news.close_open_incidents(
                cause_classes=[*_WS_INCIDENT_CAUSES, "unknown", "process_outage", "planned_shutdown"],
                now_ms=stamp,
            )
            repos.news.update_ingest_state(now_ms=stamp, connected=True, clear_error=True)
            return int(closed)

        closed = await self.db.tx("news_ingest_connected", _fn)
        if closed > 0 and self.recovery is not None:
            self.recovery.request()

    async def _disconnected(
        self, *, cause: str, close_code: int | None, error_code: str | None, planned: bool = False
    ) -> None:
        stamp = now_ms()

        def _fn(repos: Any) -> None:
            repos.news.open_incident(cause_class=cause, now_ms=stamp, planned=planned, close_code=close_code)
            repos.news.update_ingest_state(now_ms=stamp, connected=False, last_error_code=error_code)

        await self.db.tx("news_ingest_disconnected", _fn)
