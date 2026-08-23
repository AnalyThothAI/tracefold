"""Official OpenNews history recovery stage."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ..bus import RK_RAW_RECOVERY, BusMessage, DeferError, TransientError, new_trace_id, now_ms
from ..opennews import OpenNewsHistoryError, enabled_strategy_ids, parse_opennews_strategy_hits
from .runtime import _Db, _sleep_or_stop

_HISTORY_PAGE_SIZE = 100
_HISTORY_PAGE_CAP = 60
_RECOVERY_OVERLAP_MS = 30_000


class RecoveryRunner:
    """Closed incidents -> official Strategy hits -> raw.recovery.* messages (never delivered)."""

    def __init__(self, *, bus: Any, db: Any, history_client: Any | None) -> None:
        self.bus = bus
        self.db = _Db(db)
        self.history_client = history_client
        self._requested = asyncio.Event()

    def request(self) -> None:
        self._requested.set()

    async def run(self, *, stop_event: asyncio.Event) -> None:
        self._requested.set()  # startup pass
        while not stop_event.is_set():
            waiter = asyncio.create_task(self._requested.wait())
            stopper = asyncio.create_task(stop_event.wait())
            try:
                await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (waiter, stopper):
                    task.cancel()
                await asyncio.gather(waiter, stopper, return_exceptions=True)
            if stop_event.is_set():
                return
            self._requested.clear()
            try:
                await self._recover_pending()
            except (TransientError, DeferError):
                await _sleep_or_stop(stop_event, 5.0)
                self._requested.set()

    async def _provider_strategy_ids(self) -> tuple[str, ...]:
        """Which Strategies to pull history for: the provider's own enabled list, read fresh.

        Live ingestion needs no list at all — the socket pushes what the account enabled. Recovery does, only
        because the provider's hits endpoint is per-strategy. Reading it per pass rather than caching it at
        startup means a Strategy enabled mid-run is recovered too.

        A failed read raises rather than returning nothing, and that distinction is the whole point:
        `complete_recovery` is terminal, `pending_recovery_incidents` only ever selects `pending`, so settling
        an incident `unavailable` throws its outage window away for good. One 429 on this call would otherwise
        discard the entire backlog. `TransientError` leaves every incident pending for `run()` to retry; an
        empty tuple means the account really has no enabled Strategies and there is nothing to recover.
        """

        if self.history_client is None:
            return ()
        try:
            payload = await self.history_client.get_strategy_list(limit=100, page=1)
        except Exception as exc:
            raise TransientError(f"opennews_strategy_list_unavailable:{type(exc).__name__}") from exc
        return tuple(sorted(enabled_strategy_ids(payload)))

    async def _recover_pending(self) -> None:
        incidents = await self.db.read("news_recovery_pending", lambda repos: repos.news.pending_recovery_incidents())
        if not incidents:
            return
        strategy_ids = await self._provider_strategy_ids()
        for incident in incidents:
            incident_id = int(incident["incident_id"])
            stamp = now_ms()
            if not strategy_ids:

                def _unavailable(repos: Any, i: int = incident_id, s: int = stamp) -> None:
                    repos.news.complete_recovery(
                        incident_id=i,
                        status="unavailable",
                        recovered_count=0,
                        error_code="opennews_history_unavailable",
                        recovery_from_at_ms=None,
                        recovery_to_at_ms=None,
                        now_ms=s,
                    )

                await self.db.tx("news_recovery_unavailable", _unavailable)
                continue
            from_ms = max(
                0, int(incident.get("recovery_from_at_ms") or incident["opened_at_ms"]) - _RECOVERY_OVERLAP_MS
            )
            to_ms = int(incident.get("recovery_to_at_ms") or incident.get("closed_at_ms") or stamp)
            complete, count, error = True, 0, None
            try:
                for strategy_id in strategy_ids:
                    strategy_complete, strategy_count = await self._recover_strategy(
                        strategy_id, from_ms=from_ms, to_ms=to_ms
                    )
                    complete = complete and strategy_complete
                    count += strategy_count
            except OpenNewsHistoryError as exc:
                error = exc.code
            except Exception as exc:
                error = f"opennews_history_failed:{type(exc).__name__}"
            status = "unavailable" if error else ("recovered" if complete else "partial")

            def _complete(
                repos: Any,
                i: int = incident_id,
                s: str = status,
                c: int = count,
                e: str | None = error,
                f: int = from_ms,
                u: int = to_ms,
            ) -> None:
                repos.news.complete_recovery(
                    incident_id=i,
                    status=s,
                    recovered_count=c,
                    error_code=e or (None if s == "recovered" else "opennews_history_retention_partial"),
                    recovery_from_at_ms=f,
                    recovery_to_at_ms=u,
                    now_ms=now_ms(),
                )

            await self.db.tx("news_recovery_complete", _complete)

    async def _recover_strategy(self, strategy_id: str, *, from_ms: int, to_ms: int) -> tuple[bool, int]:
        client = self.history_client
        if client is None:
            raise OpenNewsHistoryError("opennews_history_unavailable")
        recovered = 0
        for page_number in range(1, _HISTORY_PAGE_CAP + 1):
            payload = await client.get_strategy_hits(
                strategy_id=strategy_id, limit=_HISTORY_PAGE_SIZE, page=page_number
            )
            page = parse_opennews_strategy_hits(payload)
            for event in page.events:
                published = event.entry.published_at_ms
                if published is None or not (from_ms <= int(published) < to_ms):
                    continue
                raw_params = _raw_params_from_history(payload, event.provider_record_id)
                if raw_params is None:
                    continue
                stamp = now_ms()
                await self.bus.publish(
                    BusMessage(
                        kind="raw",
                        message_id=f"raw:{event.provider_record_id}",
                        routing_key=RK_RAW_RECOVERY.format(strategy_id=strategy_id),
                        payload={
                            "params": raw_params,
                            "strategy_id": strategy_id,
                            "ingest_mode": "recovery",
                            "observed_at_ms": stamp,
                        },
                        trace_id=new_trace_id(),
                        occurred_at_ms=stamp,
                    )
                )
                recovered += 1
            oldest = min(
                (int(e.entry.published_at_ms) for e in page.events if e.entry.published_at_ms is not None), default=None
            )
            if oldest is not None and oldest <= from_ms:
                return True, recovered
            if not page.has_more:
                return page.total == 0 or (oldest is not None and oldest <= from_ms), recovered
        return False, recovered


def _raw_params_from_history(payload: Mapping[str, Any], provider_record_id: str) -> dict[str, Any] | None:
    for value in payload.get("data") or []:
        if isinstance(value, Mapping) and str(value.get("id")) == provider_record_id:
            return dict(value)
    return None
