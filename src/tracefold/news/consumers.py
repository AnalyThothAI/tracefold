"""News V3 consumers: Receiver, Recovery, Deduper, Triage, Analyst, Deliverer, Janitor.

Each consumer is one asyncio task; the broker is the only coordination plane; PostgreSQL holds
facts/decisions/audit; every write is idempotent by key. Consumers coordinate only through the
broker and database keys, so any of them can be scaled out without code changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

from .agents.analyst import Analyst
from .agents.prompts import TRIAGE_PROMPT_SHA256
from .agents.triage_model import TriageModel, TriageModelError, build_triage_input
from .analyst_evidence import build_evidence_bundle
from .bus import (
    Q_DEEP,
    Q_DELIVER,
    Q_RAW,
    Q_TRIAGE,
    RK_EVENT,
    RK_RAW_LIVE,
    RK_RAW_RECOVERY,
    RK_VERDICT_DEEP,
    RK_VERDICT_ESCALATE,
    RK_VERDICT_PUSH,
    BusMessage,
    DeferError,
    PermanentError,
    TransientError,
    new_trace_id,
    now_ms,
)
from .control import is_muted
from .delivery import render_first_card, render_followup_card
from .events import admit_item
from .models import (
    ANALYST_POLICY_VERSION,
    ANALYST_PROMPT_VERSION,
    TRIAGE_POLICY_VERSION,
    TRIAGE_PROMPT_VERSION,
    json_ready,
)
from .opennews import (
    OpenNewsExpectedError,
    OpenNewsHistoryError,
    parse_opennews_message,
    parse_opennews_strategy_hits,
)
from .storyline import final_storyline_key
from .triage_rules import DEFAULT_POLICY, DecidePolicy, GateFacts, decide, fallback_verdict, storyline_status_from_row

log = logging.getLogger("tracefold.news")

_HISTORY_PAGE_SIZE = 100
_HISTORY_PAGE_CAP = 60
_RECOVERY_OVERLAP_MS = 30_000
_WS_RECONNECT_SECONDS = 3.0
_OUTBOX_MIN_AGE_MS = 15_000
_JANITOR_PERIOD_SECONDS = 60.0
_RETENTION_MS = 30 * 24 * 3600_000

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


class _Db:
    """Thin adapter over WorkerDatabase's News lane: run a sync repository function inside one session."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        def _run() -> Any:
            with self._db.worker_session(name, timeout_seconds) as repos, repos.transaction():
                return fn(repos)

        return await self._run(name, _run, timeout_seconds)

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        def _run() -> Any:
            with self._db.worker_session(name, timeout_seconds) as repos:
                return fn(repos)

        return await self._run(name, _run, timeout_seconds)

    async def _run(self, name: str, fn: Callable[[], Any], timeout_seconds: float) -> Any:
        try:
            return await self._db.run_news(name, fn, operation_timeout_seconds=timeout_seconds)
        except ResourceAdmissionTimeout as exc:
            raise DeferError(f"db_admission_timeout:{name}") from exc
        except ResourceOperationOverrun as exc:
            raise TransientError(f"db_overrun:{name}") from exc


# ---------------------------------------------------------------------------- Receiver
class OpenNewsReceiver:
    """WSS -> broker. Publishes each accepted frame with confirms; overflow/unavailability become incidents."""

    def __init__(
        self,
        *,
        bus: Any,
        db: Any,
        ws_client: Any | None,
        history_client: Any | None,
        strategy_ids: Sequence[str],
        recovery: RecoveryRunner | None,
    ) -> None:
        self.bus = bus
        self.db = _Db(db)
        self.ws_client = ws_client
        self.history_client = history_client
        self.strategy_ids = frozenset(strategy_ids)
        self.recovery = recovery
        self._backpressure_open = False

    async def validate_strategies(self) -> None:
        """Compare the configured allowlist with provider-enabled strategies; warn, never fail."""

        warnings: list[str] = []
        enabled: list[str] | None = None
        if self.history_client is not None and self.strategy_ids:
            try:
                payload = await self.history_client.get_strategy_list(limit=100, page=1)
                data = payload.get("data") if isinstance(payload, Mapping) else None
                if not isinstance(data, list):
                    raise OpenNewsHistoryError("opennews_history_payload_invalid")
                enabled = sorted(
                    str(row.get("id")).strip()
                    for row in data
                    if isinstance(row, Mapping) and row.get("enabled") is True and row.get("id") is not None
                )
                configured_disabled = sorted(self.strategy_ids - set(enabled))
                enabled_unconfigured = sorted(set(enabled) - self.strategy_ids)
                if configured_disabled:
                    warnings.append("configured_but_provider_disabled:" + ",".join(configured_disabled))
                if enabled_unconfigured:
                    warnings.append("provider_enabled_but_not_configured:" + ",".join(enabled_unconfigured))
            except (OpenNewsHistoryError, Exception) as exc:
                warnings.append(f"strategy_list_unavailable:{type(exc).__name__}")
        stamp = now_ms()
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_ingest_validate",
                lambda repos: repos.news.update_ingest_state(
                    now_ms=stamp,
                    configured_strategy_ids=sorted(self.strategy_ids),
                    provider_enabled_strategy_ids=enabled,
                    strategy_warnings=warnings,
                ),
            )
        for warning in warnings:
            log.warning("news strategy allowlist: %s", warning)

    async def run(self, *, stop_event: asyncio.Event) -> None:
        if self.ws_client is None:
            await stop_event.wait()
            return
        await self.validate_strategies()
        while not stop_event.is_set():
            try:
                await self.ws_client.connect()
                await self._connected()
                while not stop_event.is_set():
                    message = await _receive_or_stop(self.ws_client, stop_event=stop_event)
                    if message is None:
                        break
                    event = parse_opennews_message(message, strategy_ids=self.strategy_ids)
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


# ---------------------------------------------------------------------------- Recovery
class RecoveryRunner:
    """Closed incidents -> official Strategy hits -> raw.recovery.* messages (never delivered)."""

    def __init__(self, *, bus: Any, db: Any, history_client: Any | None, strategy_ids: Sequence[str]) -> None:
        self.bus = bus
        self.db = _Db(db)
        self.history_client = history_client
        self.strategy_ids = frozenset(strategy_ids)
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

    async def _recover_pending(self) -> None:
        incidents = await self.db.read("news_recovery_pending", lambda repos: repos.news.pending_recovery_incidents())
        for incident in incidents:
            incident_id = int(incident["incident_id"])
            stamp = now_ms()
            if self.history_client is None or not self.strategy_ids:

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
                for strategy_id in sorted(self.strategy_ids):
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
            page = parse_opennews_strategy_hits(payload, strategy_ids=self.strategy_ids)
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


# ---------------------------------------------------------------------------- Deduper
async def publish_event(bus: Any, db: _Db, *, event_id: str, family: str, priority: str, trace_id: str) -> None:
    """Publish one candidate Event to Triage and mark it published (commit-then-publish outbox step)."""

    stamp = now_ms()
    await bus.publish(
        BusMessage(
            kind="event",
            message_id=f"event:{event_id}",
            routing_key=RK_EVENT.format(family=family, priority=priority),
            payload={"event_id": event_id},
            trace_id=trace_id,
            occurred_at_ms=stamp,
            priority=5 if priority == "high" else 0,
        )
    )
    with contextlib.suppress(TransientError, DeferError):
        await db.tx(
            "news_event_mark_published",
            lambda repos: repos.news.mark_event_published(event_id=event_id, now_ms=stamp),
            timeout_seconds=1.0,
        )


class DeduperConsumer:
    def __init__(
        self,
        *,
        bus: Any,
        db: Any,
        strategy_ids: Sequence[str],
        watchlist_symbols: frozenset[str],
        suppress_low_signal: bool = False,
    ) -> None:
        self.bus = bus
        self.db = _Db(db)
        self.strategy_ids = frozenset(strategy_ids)
        self.watchlist_symbols = watchlist_symbols
        self.suppress_low_signal = bool(suppress_low_signal)

    async def run(self, *, stop_event: asyncio.Event) -> None:
        await self.bus.consume(Q_RAW, self.handle, prefetch=1, stop_event=stop_event)

    async def handle(self, message: BusMessage) -> None:
        params = message.payload.get("params")
        if not isinstance(params, Mapping):
            raise PermanentError("news_raw_params_missing")
        event = parse_opennews_message(
            {"method": "strategy.triggered", "params": dict(params)}, strategy_ids=self.strategy_ids
        )
        if event is None:
            return  # unconfigured strategy or malformed frame: settle silently
        ingest_mode = "recovery" if str(message.payload.get("ingest_mode")) == "recovery" else "live"
        observed = int(message.payload.get("observed_at_ms") or message.occurred_at_ms or now_ms())
        stamp = now_ms()
        result = await self.db.tx(
            "news_deduper_admit",
            lambda repos: admit_item(
                repos,
                event=event,
                ingest_mode=ingest_mode,
                observed_at_ms=observed,
                trace_id=message.trace_id,
                watchlist_symbols=self.watchlist_symbols,
                now_ms=stamp,
                suppress_low_signal=self.suppress_low_signal,
            ),
            timeout_seconds=5.0,
        )
        if result.event_created and result.admission == "candidate":
            await publish_event(
                self.bus,
                self.db,
                event_id=result.event_id,
                family=result.family,
                priority=result.gate.priority if result.gate else "normal",
                trace_id=message.trace_id,
            )


# ---------------------------------------------------------------------------- Triage
@dataclass
class _Circuit:
    failures: int = 0
    open_until_ms: int = 0
    threshold: int = 3
    open_seconds: float = 60.0

    def is_open(self, at_ms: int) -> bool:
        return at_ms < self.open_until_ms

    def record_failure(self, at_ms: int) -> bool:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until_ms = at_ms + int(self.open_seconds * 1000)
            self.failures = 0
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0


class TriageConsumer:
    def __init__(
        self,
        *,
        bus: Any,
        db: Any,
        model: TriageModel | None,
        watchlist_symbols: frozenset[str],
        watchlist: Sequence[str],
        hourly_cap: int,
        concurrency: int,
        circuit_failures: int,
        circuit_open_seconds: float,
        policy: DecidePolicy = DEFAULT_POLICY,
    ) -> None:
        self.bus = bus
        self.db = _Db(db)
        self.model = model
        self.watchlist_symbols = watchlist_symbols
        self.watchlist = list(watchlist)
        self.hourly_cap = int(hourly_cap)
        self.concurrency = int(concurrency)
        self.circuit = _Circuit(threshold=circuit_failures, open_seconds=circuit_open_seconds)
        self.policy = policy

    async def run(self, *, stop_event: asyncio.Event) -> None:
        await self.bus.consume(Q_TRIAGE, self.handle, prefetch=self.concurrency, stop_event=stop_event)

    async def handle(self, message: BusMessage) -> None:
        event_id = str(message.payload.get("event_id") or "")
        if not event_id:
            raise PermanentError("news_event_id_missing")
        stamp = now_ms()
        existing = await self.db.read(
            "news_triage_existing",
            lambda repos: repos.news.get_verdict(
                event_id=event_id, stage="triage", policy_version=TRIAGE_POLICY_VERSION
            ),
        )
        if existing is not None:
            if existing.get("published_at_ms") is None and existing["final_decision"] in {"push", "escalate"}:
                await self._publish_decision(
                    event_id, existing["final_decision"], trace_id=message.trace_id, amqp_priority=message.priority
                )
            return
        bundle = await self.db.read("news_triage_load", lambda repos: self._load(repos, event_id, stamp))
        if bundle is None:
            raise PermanentError("news_event_missing")
        card, status_row, control, sent_last_hour = bundle
        facts = GateFacts(
            grounded_assets=tuple(card.get("grounded_assets") or []),
            watchlist_symbols=self.watchlist_symbols,
            provider_score=card.get("provider_score_max"),
            priority=str(card.get("priority") or "normal"),
            admission=str(card.get("admission") or ""),
        )
        status = storyline_status_from_row(status_row, str(card.get("storyline_key") or ""))
        muted = bool(control.get("paused")) or is_muted(
            control, storyline_key=status.key, grounded_assets=facts.grounded_assets, now_ms=stamp
        )
        cap_reached = sent_last_hour >= self.hourly_cap
        queue_lag_ms = max(0, stamp - int(message.occurred_at_ms or stamp))
        trace: dict[str, Any] = {
            "queue_lag_ms": queue_lag_ms,
            "attempt": message.attempt,
            "prompt_sha256": TRIAGE_PROMPT_SHA256,
            "status": json_ready(dict(status_row or {})),
        }
        model_name = self.model.model_name if self.model else None
        degraded = False
        error_code = None
        if self.model is None:
            verdict, decision = fallback_verdict(facts, error_code="news_triage_model_unconfigured")
            degraded, error_code = True, "news_triage_model_unconfigured"
        elif self.circuit.is_open(stamp):
            verdict, decision = fallback_verdict(facts, error_code="news_triage_circuit_open")
            degraded, error_code = True, "news_triage_circuit_open"
        else:
            status_payload = {
                **dict(status_row or {}),
                "storyline_key": status.key,
                "preliminary": True,
                "queue_lag_ms": queue_lag_ms,
            }
            human = build_triage_input(
                event=card,
                gate={
                    "asset_class": card.get("asset_class"),
                    "grounded_assets": facts.grounded_assets,
                    "macro_lexicon": card.get("macro_lexicon"),
                    "pr_template": False,
                },
                event_status=status_payload,
                watchlist=self.watchlist,
            )
            trace["input_sha256"] = hashlib.sha256(human.encode("utf-8")).hexdigest()
            try:
                call = await self.model.triage(human)
            except TriageModelError as exc:
                trace.update({"model_attempts": exc.attempts, "model_failure_retryable": exc.retryable})
                opened = self.circuit.record_failure(stamp)
                if opened:
                    with contextlib.suppress(TransientError, DeferError):
                        await self.db.tx(
                            "news_triage_circuit",
                            lambda repos: repos.news.open_incident(cause_class="triage_circuit_open", now_ms=stamp),
                        )
                verdict, decision = fallback_verdict(facts, error_code=exc.code)
                degraded, error_code = True, exc.code
            else:
                self.circuit.record_success()
                verdict = call.verdict
                trace.update(
                    {
                        "latency_ms": call.latency_ms,
                        "model_attempts": call.attempts,
                        "input_tokens": call.input_tokens,
                        "output_tokens": call.output_tokens,
                        "cached_tokens": call.cached_tokens,
                    }
                )
        # The final storyline key comes from the verdict (primaries/scope); the throttle windows must use it.
        final_key = final_storyline_key(
            title=str(card.get("leader_title") or ""),
            headline_zh=verdict.headline_zh,
            scope=verdict.scope,
            verdict_primaries=[a.symbol for a in verdict.assets if a.role == "primary"],
            grounded_assets=facts.grounded_assets,
            family=str(card.get("family") or "general"),
        )
        if final_key != status.key:
            final_row = await self.db.read(
                "news_triage_status_final",
                lambda repos: repos.news.event_status(storyline_key=final_key, now_ms=stamp),
            )
            status = storyline_status_from_row(final_row, final_key)
            muted = bool(control.get("paused")) or is_muted(
                control, storyline_key=final_key, grounded_assets=facts.grounded_assets, now_ms=stamp
            )
            trace["status_final"] = json_ready(dict(final_row or {}))
        trace["storyline_key"] = final_key
        decision = decide(verdict, facts, status, hourly_cap_reached=cap_reached, muted=muted, policy=self.policy)
        if degraded and decision.final in {"push", "escalate"} and decision.rule_baseline == "drop":
            decision = type(decision)(
                "drop", "fail_closed_fallback", None, decision.rule_baseline, decision.watchlist_hits
            )
        final = decision.final
        headline = verdict.headline_zh
        reason = decision.throttled_by or decision.override_rule or ""
        context_line = (
            f"[{verdict.audience}/{verdict.event_type}/{verdict.direction} m{verdict.magnitude}"
            f" → {final}·{reason}] {headline}"
        )

        def _persist(repos: Any) -> bool:
            inserted = repos.news.insert_verdict(
                event_id=event_id,
                stage="triage",
                policy_version=TRIAGE_POLICY_VERSION,
                model_decision=None
                if degraded and error_code == "news_triage_model_unconfigured"
                else verdict.decision,
                rule_baseline_decision=decision.rule_baseline,
                final_decision=final,
                override_rule=decision.override_rule,
                throttled_by=decision.throttled_by,
                verdict=json_ready(verdict),
                model=model_name,
                prompt_version=TRIAGE_PROMPT_VERSION,
                degraded=degraded,
                error_code=error_code,
                trace=trace,
                now_ms=stamp,
            )
            repos.news.set_storyline_key(event_id=event_id, storyline_key=final_key, now_ms=stamp)
            repos.news.set_context_line(event_id=event_id, context_line=context_line, followup_of=None, now_ms=stamp)
            return bool(inserted)

        await self.db.tx("news_triage_persist", _persist)
        if final in {"push", "escalate"}:
            await self._publish_decision(event_id, final, trace_id=message.trace_id, amqp_priority=message.priority)

    @staticmethod
    def _load(
        repos: Any, event_id: str, stamp: int
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], int] | None:
        card = repos.news.event_card(event_id)
        if card is None:
            return None
        status_row = repos.news.event_status(storyline_key=str(card.get("storyline_key") or ""), now_ms=stamp)
        control = repos.news.read_control(now_ms=stamp)
        sent = repos.news.sent_count_since(since_ms=stamp - 3600_000)
        return card, status_row, control, sent

    async def _publish_decision(self, event_id: str, final: str, *, trace_id: str, amqp_priority: int) -> None:
        stamp = now_ms()
        await self.bus.publish(
            BusMessage(
                kind="verdict",
                message_id=f"push:{event_id}",
                routing_key=RK_VERDICT_PUSH,
                payload={"event_id": event_id, "kind": "first"},
                trace_id=trace_id,
                occurred_at_ms=stamp,
                priority=amqp_priority,
            )
        )
        if final == "escalate":
            await self.bus.publish(
                BusMessage(
                    kind="verdict",
                    message_id=f"escalate:{event_id}",
                    routing_key=RK_VERDICT_ESCALATE,
                    payload={"event_id": event_id},
                    trace_id=trace_id,
                    occurred_at_ms=stamp,
                    priority=amqp_priority,
                )
            )
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_triage_mark_published",
                lambda repos: repos.news.mark_verdict_published(
                    event_id=event_id, stage="triage", policy_version=TRIAGE_POLICY_VERSION, now_ms=stamp
                ),
                timeout_seconds=1.0,
            )


# ---------------------------------------------------------------------------- Analyst
class AnalystConsumer:
    """One code-prefetched evidence bundle -> one structured call -> verify -> staleness check -> follow-up."""

    def __init__(self, *, bus: Any, db: Any, analyst: Analyst | None, concurrency: int) -> None:
        self.bus = bus
        self.db = _Db(db)
        self.analyst = analyst
        self.concurrency = int(concurrency)

    async def run(self, *, stop_event: asyncio.Event) -> None:
        await self.bus.consume(Q_DEEP, self.handle, prefetch=self.concurrency, stop_event=stop_event)

    async def handle(self, message: BusMessage) -> None:
        event_id = str(message.payload.get("event_id") or "")
        if not event_id:
            raise PermanentError("news_event_id_missing")
        analyst = self.analyst
        if analyst is None:
            return
        stamp = now_ms()
        existing = await self.db.read(
            "news_analyst_existing",
            lambda repos: repos.news.get_verdict(
                event_id=event_id, stage="deep", policy_version=ANALYST_POLICY_VERSION
            ),
        )
        if existing is not None:
            if existing.get("published_at_ms") is None and existing["final_decision"] == "push":
                await self._publish_followup(event_id, trace_id=message.trace_id)
            return
        queue_lag_ms = max(0, stamp - int(message.occurred_at_ms or stamp))
        bundle = await self.db.read(
            "news_analyst_bundle",
            lambda repos: build_evidence_bundle(repos, event_id=event_id, now_ms=stamp, queue_lag_ms=queue_lag_ms),
            timeout_seconds=5.0,
        )
        if bundle is None:
            raise PermanentError("news_event_or_triage_missing")
        triage_direction = str((bundle.payload["event"].get("triage") or {}).get("direction") or "unclear")
        result = await analyst.analyze(bundle=bundle, triage_direction=triage_direction)
        verdict_payload = json_ready(result.verdict) if result.verdict is not None else {}
        wants_push = bool(result.verdict is not None and result.verify.ok and result.verdict.follow_up_needed)
        final = "push" if wants_push else ("degraded" if not result.verify.ok else "drop")
        throttled_by: str | None = None
        if wants_push:
            # Safe-point staleness check: a newer push in the same storyline supersedes this follow-up.
            latest = await self.db.read(
                "news_analyst_staleness",
                lambda repos: repos.news.event_status(storyline_key=bundle.storyline_key, now_ms=now_ms()),
            )
            last_push_ago = latest.get("last_push_ago_ms") if latest else None
            if last_push_ago is not None and int(last_push_ago) <= (now_ms() - stamp):
                final, throttled_by = "throttled", f"storyline:{bundle.storyline_key}:superseded"
        trace = {
            "queue_lag_ms": queue_lag_ms,
            "latency_ms": result.latency_ms,
            "attempts": result.attempts,
            "evidence_count": result.evidence_count,
            "verify": result.verify.reason,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cached_tokens": result.cached_tokens,
            "prompt_sha256": analyst.prompt_sha256,
            "status": json_ready(bundle.status_row),
        }
        await self.db.tx(
            "news_analyst_persist",
            lambda repos: repos.news.insert_verdict(
                event_id=event_id,
                stage="deep",
                policy_version=ANALYST_POLICY_VERSION,
                model_decision=("push" if result.verdict and result.verdict.follow_up_needed else "drop")
                if result.verdict
                else None,
                rule_baseline_decision="drop",
                final_decision=final,
                override_rule=None if result.verify.ok else "verify_failed",
                throttled_by=throttled_by,
                verdict=verdict_payload,
                model=analyst.model_name,
                prompt_version=ANALYST_PROMPT_VERSION,
                degraded=not result.verify.ok,
                error_code=result.error_code,
                trace=trace,
                now_ms=stamp,
            ),
        )
        if final == "push":
            await self._publish_followup(event_id, trace_id=message.trace_id)

    async def _publish_followup(self, event_id: str, *, trace_id: str) -> None:
        stamp = now_ms()
        first = await self.db.read(
            "news_analyst_first_delivery", lambda repos: repos.news.delivery(event_id=event_id, kind="first")
        )
        if first is None or first.get("state") != "sent":
            return  # follow-up only after the first card was actually sent
        await self.bus.publish(
            BusMessage(
                kind="verdict",
                message_id=f"deep:{event_id}",
                routing_key=RK_VERDICT_DEEP,
                payload={"event_id": event_id, "kind": "followup"},
                trace_id=trace_id,
                occurred_at_ms=stamp,
            )
        )
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_analyst_mark_published",
                lambda repos: repos.news.mark_verdict_published(
                    event_id=event_id, stage="deep", policy_version=ANALYST_POLICY_VERSION, now_ms=stamp
                ),
                timeout_seconds=1.0,
            )


# ---------------------------------------------------------------------------- Deliverer
class DelivererConsumer:
    """SAC consumer: one Feishu attempt per (event, kind); crash between send and ack never resends."""

    def __init__(
        self,
        *,
        bus: Any,
        db: Any,
        sender: Any | None,
        finite_operations: Any,
        min_interval_seconds: float,
        hourly_cap: int,
    ) -> None:
        self.bus = bus
        self.db = _Db(db)
        self.sender = sender
        self.finite = finite_operations
        self.min_interval = float(min_interval_seconds)
        self.hourly_cap = int(hourly_cap)
        self._last_send_at = 0.0

    async def run(self, *, stop_event: asyncio.Event) -> None:
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_delivery_reconcile", lambda repos: repos.news.terminalize_interrupted_deliveries(now_ms=now_ms())
            )
        await self.bus.consume(Q_DELIVER, self.handle, prefetch=1, stop_event=stop_event)

    async def handle(self, message: BusMessage) -> None:
        event_id = str(message.payload.get("event_id") or "")
        kind = (
            "followup"
            if message.routing_key == RK_VERDICT_DEEP or message.payload.get("kind") == "followup"
            else "first"
        )
        if not event_id:
            raise PermanentError("news_event_id_missing")
        stamp = now_ms()
        bundle = await self.db.read("news_delivery_load", lambda repos: self._load(repos, event_id, kind, stamp))
        if bundle is None:
            raise PermanentError("news_delivery_inputs_missing")
        card, triage_row, deep_row, control, sent_last_hour = bundle
        tv = dict(triage_row.get("verdict") or {})
        if kind == "first":
            if triage_row["final_decision"] not in {"push", "escalate"}:
                return
            if sent_last_hour >= self.hourly_cap and triage_row["final_decision"] != "escalate":
                await self._settle_direct(event_id, kind, "hourly_cap_reached", stamp)
                return
            card_payload = render_first_card(
                event=card,
                verdict=tv,
                decision=str(triage_row["final_decision"]),
                grounded_assets=list(card.get("grounded_assets") or []),
            )
        else:
            if deep_row is None or deep_row["final_decision"] != "push":
                return
            card_payload = render_followup_card(
                event=card, triage_verdict=tv, analyst_verdict=dict(deep_row.get("verdict") or {})
            )
        if control.get("paused"):
            # News is perishable: a paused lane drops instead of holding an unacked message.
            await self._settle_direct(event_id, kind, "delivery_paused", stamp)
            return
        if self.sender is None:
            await self._settle_direct(event_id, kind, "delivery_unavailable", stamp)
            return
        state = await self.db.tx(
            "news_delivery_begin",
            lambda repos: repos.news.begin_delivery(event_id=event_id, kind=kind, card=card_payload, now_ms=stamp),
        )
        if state != "new":
            if state == "sending":
                await self.db.tx(
                    "news_delivery_ambiguous",
                    lambda repos: repos.news.settle_delivery(
                        event_id=event_id,
                        kind=kind,
                        state="terminal",
                        receipt=None,
                        error_code="ambiguous_after_crash",
                        now_ms=now_ms(),
                    ),
                )
            return
        wait = self.min_interval - (time.monotonic() - self._last_send_at)
        if wait > 0:
            await asyncio.sleep(wait)
        error_code: str | None = None
        receipt: dict[str, Any] | None = None
        try:
            result = await self.finite.run(
                "news_delivery_feishu_send", self.sender.send_card, card_payload, timeout_seconds=8.0
            )
            receipt = dict(result)
        except Exception as exc:
            error_code = getattr(exc, "code", None) or f"news_delivery_failed:{type(exc).__name__}"
        finally:
            self._last_send_at = time.monotonic()
        settled_state = "sent" if error_code is None else "terminal"
        try:
            await self.db.tx(
                "news_delivery_settle",
                lambda repos: repos.news.settle_delivery(
                    event_id=event_id,
                    kind=kind,
                    state=settled_state,
                    receipt=receipt,
                    error_code=error_code,
                    now_ms=now_ms(),
                ),
            )
        except (TransientError, DeferError) as exc:
            raise RuntimeError("news_delivery_settlement_unavailable") from exc

    async def _settle_direct(self, event_id: str, kind: str, error_code: str, stamp: int) -> None:
        def _fn(repos: Any) -> None:
            state = repos.news.begin_delivery(event_id=event_id, kind=kind, card={}, now_ms=stamp)
            if state == "new":
                repos.news.settle_delivery(
                    event_id=event_id, kind=kind, state="terminal", receipt=None, error_code=error_code, now_ms=stamp
                )

        await self.db.tx("news_delivery_settle_direct", _fn)

    @staticmethod
    def _load(repos: Any, event_id: str, kind: str, stamp: int) -> tuple[Any, ...] | None:
        card = repos.news.event_card(event_id)
        triage = repos.news.latest_verdict(event_id=event_id, stage="triage")
        if card is None or triage is None:
            return None
        deep = repos.news.latest_verdict(event_id=event_id, stage="deep") if kind == "followup" else None
        control = repos.news.read_control(now_ms=stamp)
        sent = repos.news.sent_count_since(since_ms=stamp - 3600_000)
        return card, triage, deep, control, sent

    async def close(self) -> None:
        if self.sender is not None:
            with contextlib.suppress(Exception):
                await self.finite.run(
                    "news_delivery_sender_close", self.sender.close, timeout_seconds=5.0, allow_shutdown=True
                )


# ---------------------------------------------------------------------------- Janitor
class JanitorLoop:
    """Outbox catch-up, band expiry, retention, broker snapshot — one bounded turn per period."""

    def __init__(self, *, db: Any, bus: Any | None = None, period_seconds: float = _JANITOR_PERIOD_SECONDS) -> None:
        self.db = _Db(db)
        self.bus = bus
        self.period = float(period_seconds)

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.turn()
            await _sleep_or_stop(stop_event, self.period)

    async def turn(self) -> None:
        stamp = now_ms()
        if self.bus is not None:
            with contextlib.suppress(TransientError, DeferError, Exception):
                await self.republish_unpublished()
        with contextlib.suppress(TransientError, DeferError):

            def _janitor(repos: Any, s: int = stamp) -> None:
                repos.news.expire_bands(now_ms=s)
                repos.news.purge_before(cutoff_ms=s - _RETENTION_MS)

            await self.db.tx("news_janitor", _janitor, timeout_seconds=10.0)
        if self.bus is not None:
            snapshot: dict[str, Any] = {"configured": True, "connected": False, "queues": {}, "error_code": None}
            try:
                depths = await asyncio.wait_for(self.bus.queue_depths(), timeout=5.0)
                prefix = f"{self.bus.prefix}." if getattr(self.bus, "prefix", "") else ""
                snapshot.update(
                    connected=True,
                    queues={name.removeprefix(prefix): value for name, value in depths.items()},
                )
            except Exception as exc:
                snapshot["error_code"] = f"broker_snapshot_failed:{type(exc).__name__}"
            with contextlib.suppress(TransientError, DeferError):

                def _snapshot(repos: Any, s: int = stamp, snap: dict[str, Any] = snapshot) -> None:
                    repos.news.update_broker_snapshot(snapshot=snap, now_ms=s)

                await self.db.tx("news_broker_snapshot", _snapshot, timeout_seconds=3.0)

    async def republish_unpublished(self) -> int:
        """Commit-then-crash (or publish failure) before publish: re-publish candidate Events that never left."""

        rows = await self.db.read(
            "news_outbox_unpublished",
            lambda repos: repos.news.unpublished_candidates(older_than_ms=now_ms() - _OUTBOX_MIN_AGE_MS),
        )
        republished = 0
        for row in rows:
            event_id = str(row["event_id"])

            def _card(repos: Any, e: str = event_id) -> Any:
                return repos.news.event_card(e)

            card = await self.db.read("news_outbox_card", _card)
            if card is None:
                continue
            await publish_event(
                self.bus,
                self.db,
                event_id=str(card["event_id"]),
                family=str(card["family"]),
                priority=str(card["priority"]),
                trace_id=str(card.get("trace_id") or new_trace_id()),
            )
            republished += 1
        return republished


# ---------------------------------------------------------------------------- helpers
async def _receive_or_stop(client: Any, *, stop_event: asyncio.Event) -> Any | None:
    receive_task = asyncio.create_task(client.receive())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait({receive_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done and stop_task.result():
            return None
        return await receive_task
    finally:
        for task in (receive_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(receive_task, stop_task, return_exceptions=True)


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.001, float(seconds)))


@dataclass
class NewsPipeline:
    """All consumers wired for one Workers process."""

    receiver: OpenNewsReceiver | None
    recovery: RecoveryRunner | None
    deduper: DeduperConsumer
    triage: TriageConsumer
    analyst: AnalystConsumer
    deliverer: DelivererConsumer
    janitor: JanitorLoop
    tasks: list[tuple[str, Callable[..., Any]]] = field(default_factory=list)

    def runners(self) -> list[tuple[str, Callable[[asyncio.Event], Any]]]:
        out: list[tuple[str, Callable[[asyncio.Event], Any]]] = []
        if self.receiver is not None:
            out.append(("news-receiver", lambda stop: self.receiver.run(stop_event=stop)))  # type: ignore[union-attr]
        if self.recovery is not None:
            out.append(("news-recovery", lambda stop: self.recovery.run(stop_event=stop)))  # type: ignore[union-attr]
        out.extend(
            [
                ("news-deduper", lambda stop: self.deduper.run(stop_event=stop)),
                ("news-triage", lambda stop: self.triage.run(stop_event=stop)),
                ("news-analyst", lambda stop: self.analyst.run(stop_event=stop)),
                ("news-deliverer", lambda stop: self.deliverer.run(stop_event=stop)),
                ("news-janitor", lambda stop: self.janitor.run(stop_event=stop)),
            ]
        )
        return out

    async def close(self) -> None:
        await self.deliverer.close()


__all__ = [
    "AnalystConsumer",
    "DeduperConsumer",
    "DelivererConsumer",
    "JanitorLoop",
    "NewsPipeline",
    "OpenNewsReceiver",
    "RecoveryRunner",
    "TriageConsumer",
    "publish_event",
]
