"""Best-effort asynchronous delivery of durable execution observations.

Two passes over one ledger. The first tells the operator what the Signal lane decided, with the
Case's own frozen judgment beside it; the second, four hours later, tells them what happened to the
price after it. Neither invents a number: the judgment lines are `policy_checks` read back, the
outcome is public venue candles, and a provider figure is labelled as the provider's.

The outcome is a **second message**, not an edit of the first (#458 PR-B). The deployed channel is a
Feishu custom-bot webhook, which returns no message id and has no edit endpoint; the operator chose
that channel and dropped the earlier "one card per symbol per day" ceiling with it, which is what
makes a second message the simple answer rather than a workaround.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from loguru import logger

from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.integrations.feishu import FeishuDeliveryError
from tracefold.integrations.telegram import TelegramDeliveryError
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun
from tracefold.trading.notification_policy import is_notifiable

TRADING_NOTIFICATION_TASK_NAME = "trading-observation-notifier"
_POLL_SECONDS = 2.0
_RETRY_SECONDS = 5.0
_DB_TIMEOUT_SECONDS = 5.0
_SEND_TIMEOUT_SECONDS = 8.0
_BAR_TIMEOUT_SECONDS = 12.0

# The holding period the outcome reports, and the margin that keeps the last bar closed before it is
# read. Both are the Signal card's contract with the reader, not tunable operator policy.
RESULT_HOLD_MS = 4 * 3_600_000
RESULT_SETTLE_MS = 5 * 60_000
_HOUR_MS = 3_600_000
# The venue interval this worker reads. A bar opening at `t` closes at `t + BAR_INTERVAL_MS`.
BAR_INTERVAL_MS = 300_000

_DELIVERY_ERRORS = (TelegramDeliveryError, FeishuDeliveryError, ResourceAdmissionTimeout, ResourceOperationOverrun)


class TradingNotifier(Protocol):
    """What the worker needs from a channel. `send` answers a message id only where one exists."""

    @property
    def target_sha256(self) -> str: ...

    def prepare(self) -> None: ...

    def send(self, text: str) -> int | None: ...

    def close(self) -> None: ...


class ResultBarReader(Protocol):
    """Public venue closes for one market over a window; evidence, never an execution route."""

    async def __call__(self, market_key: str, venue: str, start_ms: int, end_ms: int) -> Sequence[tuple[int, str]]: ...


class TradingNotificationWorker:
    """Append a durable receipt only after a non-blocking notification turn succeeds."""

    def __init__(
        self,
        *,
        db: WorkerTradingDatabase,
        finite: FiniteOperations,
        sender: TradingNotifier,
        bars: ResultBarReader | None = None,
        clock_ns: Any = None,
    ) -> None:
        self._db = db
        self._finite = finite
        self._sender = sender
        self._bars = bars
        self._clock_ns = clock_ns or time.time_ns
        self._prepared = False

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            outcome = await self.advance_once()
            if outcome in {"idle", "sent"}:
                outcome = await self.advance_result_once()
            delay = _RETRY_SECONDS if outcome == "delivery_unavailable" else _POLL_SECONDS
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=delay)

    async def advance_once(self) -> str:
        selected_at_ns = int(self._clock_ns())
        try:
            row = await self._db.read(
                "trading_notification_observation",
                lambda repos: repos.trading.next_execution_notification(
                    self._sender.target_sha256, now_ns=selected_at_ns
                ),
                timeout_seconds=_DB_TIMEOUT_SECONDS,
            )
        except (ResourceAdmissionTimeout, ResourceOperationOverrun) as exc:
            logger.bind(error=type(exc).__name__).warning("Trading notification database unavailable")
            return "delivery_unavailable"
        if row is None:
            return "idle"
        text = trading_notification_text(row)
        if text is None:
            raise RuntimeError("trading_notification_projection_drift")
        message_id = await self._send(text)
        if message_id is _UNAVAILABLE:
            return "delivery_unavailable"
        try:
            await self._db.tx(
                "trading_notification_delivery_append",
                lambda repos: repos.trading.append_execution_notification_delivery(
                    target_sha256=self._sender.target_sha256,
                    observation_seq=int(row["seq"]),
                    message_id=message_id,
                    delivered_at_ns=self._clock_ns(),
                    selected_at_ns=selected_at_ns,
                ),
                timeout_seconds=_DB_TIMEOUT_SECONDS,
            )
        except (ResourceAdmissionTimeout, ResourceOperationOverrun) as exc:
            logger.bind(error=type(exc).__name__).warning("Trading notification database unavailable")
            return "delivery_unavailable"
        return "sent"

    async def advance_result_once(self) -> str:
        """Send the four-hour outcome for the oldest Signal card that is due one."""

        if self._bars is None:
            return "idle"
        due_at_or_before_ns = self._clock_ns() - (RESULT_HOLD_MS + RESULT_SETTLE_MS) * 1_000_000
        try:
            row = await self._db.read(
                "trading_notification_result_due",
                lambda repos: repos.trading.next_execution_notification_result(
                    self._sender.target_sha256, due_at_or_before_ns=due_at_or_before_ns
                ),
                timeout_seconds=_DB_TIMEOUT_SECONDS,
            )
        except (ResourceAdmissionTimeout, ResourceOperationOverrun) as exc:
            logger.bind(error=type(exc).__name__).warning("Trading notification database unavailable")
            return "delivery_unavailable"
        if row is None:
            return "idle"

        manifest = row.get("manifest")
        manifest = manifest if isinstance(manifest, Mapping) else {}
        venue = _manifest_venue(manifest)
        entry_at_ms = int(row["signal_observed_at_ns"]) // 1_000_000
        # The venue read is already async, so it is bounded by its own deadline rather than pushed
        # through `FiniteOperations`, which exists for *synchronous* external work and would occupy a
        # pool thread for the whole HTTP round trip.
        try:
            bars = await asyncio.wait_for(
                self._bars(
                    str(row["market_key"]),
                    venue,
                    entry_at_ms,
                    entry_at_ms + RESULT_HOLD_MS + _HOUR_MS,
                ),
                timeout=_BAR_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # a public venue read is best effort; it never faults the worker
            logger.bind(error=type(exc).__name__).warning("Trading notification bars unavailable")
            return "delivery_unavailable"

        text = trading_result_text(row, bars, entry_at_ms=entry_at_ms)
        if text is None:
            # No entry bar closed yet, or the venue served nothing. Leave the receipt unmarked so the
            # next turn tries again rather than reporting an outcome nobody could have traded.
            return "idle"
        message_id = await self._send(text)
        if message_id is _UNAVAILABLE:
            return "delivery_unavailable"
        try:
            await self._db.tx(
                "trading_notification_result_mark",
                lambda repos: repos.trading.mark_execution_notification_result(
                    target_sha256=self._sender.target_sha256,
                    observation_seq=int(row["observation_seq"]),
                    result_delivered_at_ns=self._clock_ns(),
                ),
                timeout_seconds=_DB_TIMEOUT_SECONDS,
            )
        except (ResourceAdmissionTimeout, ResourceOperationOverrun) as exc:
            logger.bind(error=type(exc).__name__).warning("Trading notification database unavailable")
            return "delivery_unavailable"
        return "sent"

    async def _send(self, text: str) -> Any:
        try:
            if not self._prepared:
                await self._finite.run(
                    "trading_notification_prepare",
                    self._sender.prepare,
                    timeout_seconds=_SEND_TIMEOUT_SECONDS,
                )
                self._prepared = True
            return await self._finite.run(
                "trading_notification_send",
                self._sender.send,
                text,
                timeout_seconds=_SEND_TIMEOUT_SECONDS,
            )
        except _DELIVERY_ERRORS as exc:
            self._prepared = False
            logger.bind(error=type(exc).__name__).warning("Trading notification channel unavailable")
            return _UNAVAILABLE

    def close(self) -> None:
        self._sender.close()


class _Unavailable:
    """A send outcome that is neither a message id nor the absence of one."""


_UNAVAILABLE = _Unavailable()


def trading_notification_text(row: dict[str, Any]) -> str | None:
    """Project only what the policy calls notable, in the Runtime's own summary vocabulary."""

    kind = str(row.get("normalized_kind") or "")
    summary = row.get("summary")
    values: dict[str, Any] = summary if isinstance(summary, dict) else {}
    if not is_notifiable(kind, values):
        return None
    stage = _stage(kind, values)
    if stage is None:
        return None
    event_id = str(row.get("event_id") or "")
    account_slot = str(row.get("account_slot") or "")
    command_id = str(row.get("command_id") or "")
    signal_id = str(row.get("signal_id") or "")
    correlation = command_id or signal_id
    lines = [
        f"Tracefold execution: {stage}",
        # The observation's own instant, not the send's. A coalesced kind can report a state observed
        # minutes before the card left, and a reader should never have to guess which (#472).
        f"at: {_utc_second(row.get('occurred_at_ns'))}",
        f"account: {account_slot}",
        f"correlation: {correlation[:16] or '-'}",
        f"event: {event_id[:16] or '-'}",
    ]
    lines.extend(_stage_detail_lines(kind, values))
    if kind == "signal_disposition":
        lines.extend(_signal_case_lines(row))
    return "\n".join(lines)


def _stage(kind: str, values: Mapping[str, Any]) -> str | None:
    """The card's first line. `None` means the policy admitted a kind this renderer cannot state."""

    if kind == "signal_disposition":
        return f"Signal {values.get('disposition', 'disposed')}"
    if kind == "control_disposition":
        return f"Command {values.get('disposition', 'disposed')}"
    if kind == "order":
        return f"Order {values.get('status', 'observed')}"
    if kind == "fill":
        return "Fill observed"
    if kind == "audit_gap":
        return f"Audit gap: {values.get('cause', 'unknown')}"
    if kind == "readiness":
        if values.get("control_stage") == "runtime_accepted":
            return f"Runtime accepted {values.get('action', 'command')}"
        return f"Runtime started in {values.get('mode', 'unknown')}"
    if kind == "reconciliation":
        return "Account not flat"
    return None


def _stage_detail_lines(kind: str, values: Mapping[str, Any]) -> list[str]:
    """The one fact that makes each non-Signal stage actionable, and nothing beyond it."""

    if kind == "readiness" and values.get("lifecycle") == "started":
        return [f"revision: {str(values.get('runtime_revision') or '-')[:12]}"]
    if kind == "reconciliation":
        return [f"exposure: {values.get('positions', '?')} positions, {values.get('orders', '?')} orders"]
    if kind == "fill":
        return [f"leg: {values.get('leg', '?')} {values.get('last_quantity', '?')} @ {values.get('last_price', '?')}"]
    return []


def _utc_second(occurred_at_ns: Any) -> str:
    """Whole-second UTC; a card claiming nanosecond precision would be claiming a clock it lacks."""

    try:
        seconds = int(occurred_at_ns) // 1_000_000_000
    except (TypeError, ValueError):
        return "-"
    if seconds <= 0:
        return "-"
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _signal_case_lines(row: Mapping[str, Any]) -> list[str]:
    """Why this Signal existed, from the Case's own frozen evidence and nothing else.

    Absent when the Case cannot be read: a card that silently dropped its reasons would look like a
    Signal with none, which is the reading this whole surface exists to prevent.
    """

    manifest = row.get("manifest")
    manifest = manifest if isinstance(manifest, Mapping) else None
    checks = row.get("policy_checks")
    checks = checks if isinstance(checks, Mapping) else None
    if manifest is None and checks is None:
        return ["case: unavailable"]

    lines: list[str] = []
    market_key = str(row.get("market_key") or "")
    direction = str(row.get("direction") or "")
    if market_key:
        lines.append(f"market: {market_key} {direction}".rstrip())
    if checks is not None:
        decision = str(checks.get("decision") or "")
        rule = str(checks.get("rule") or "")
        policy = str(checks.get("policy_version") or checks.get("policy_id") or "")
        lines.append(f"policy: {policy} {decision} {rule}".rstrip())
        for check in checks.get("checks") or ():
            if not isinstance(check, Mapping):
                continue
            mark = "PASS" if check.get("passed") else "FAIL"
            measured = check.get("measured")
            lines.append(
                f"  {mark} {check.get('check', '?')} {check.get('operator', '?')} "
                f"{check.get('threshold', '?')} · measured {measured if measured is not None else '-'}"
            )
    if manifest is not None:
        lines.extend(_vendor_measurement_lines(manifest))
    return lines


def _vendor_measurement_lines(manifest: Mapping[str, Any]) -> list[str]:
    """The four provider numbers, each labelled as the provider's.

    #459 measured what the vendor's "OI change" actually is: over the same window its five-minute move
    was substantially price rather than position. Printing it unlabelled beside a venue-truth price
    would invite exactly the reading that measurement disproved.
    """

    contexts = manifest.get("contexts")
    contexts = contexts if isinstance(contexts, Mapping) else {}
    oi = contexts.get("oi")
    oi = oi if isinstance(oi, Mapping) else {}
    market = contexts.get("market")
    market = market if isinstance(market, Mapping) else {}
    if not oi:
        return []
    lines = [
        "vendor (OpenNews 1019 caliber, not venue truth):"
        f" OI change {_bps_percent(oi.get('oi_change_bps'))}"
        f" · whale/OI {_bps_percent(oi.get('whale_oi_ratio_bps'))}"
        f" · whale profit {_bps_percent(oi.get('whale_long_profit_bps'))}"
        f" · OI value {_usd(oi.get('oi_value_usd'))}",
    ]
    pre_move = market.get("pre_move_bps")
    if pre_move is not None:
        lookback = market.get("pre_move_lookback_ms")
        window = f"{int(lookback) // 60_000}m" if isinstance(lookback, int) and lookback > 0 else "lookback"
        lines.append(f"venue price: {_bps_percent(pre_move)} over {window} before the trigger")
    return lines


def trading_result_text(row: Mapping[str, Any], bars: Sequence[tuple[int, str]], *, entry_at_ms: int) -> str | None:
    """The 1 h and 4 h outcome of one Signal, or `None` when no entry bar has closed yet.

    Entry is the first close at or after the Signal's own instant -- the bar the Signal falls inside,
    taken at its close. That is the first price a taker could have had, and it is the convention #459
    replayed under. The difference is not cosmetic: measuring the same sample from a close that
    *preceded* the trigger turned a negative result positive.
    """

    closes = sorted((int(open_ms), str(close)) for open_ms, close in bars)
    entry = _close_at_or_after(closes, entry_at_ms)
    if entry is None:
        return None
    entry_close_ms, entry_close = entry
    one_hour = _close_at_or_after(closes, entry_close_ms + _HOUR_MS)
    four_hour = _close_at_or_after(closes, entry_close_ms + RESULT_HOLD_MS)
    signal_id = str(row.get("signal_id") or "")
    market_key = str(row.get("market_key") or "")
    direction = str(row.get("direction") or "")
    return "\n".join(
        (
            f"Tracefold Signal result: {market_key} {direction}".rstrip(),
            f"entry: {entry_close} at the first close on or after the Signal",
            f"1H: {_return_percent(entry_close, one_hour)}   4H: {_return_percent(entry_close, four_hour)}",
            f"correlation: {signal_id[:16] or '-'}",
        )
    )


def _close_at_or_after(closes: Sequence[tuple[int, str]], at_ms: int) -> tuple[int, str] | None:
    """The first bar whose *close* lands at or after `at_ms`, returned as `(close instant, price)`.

    Indexed on the close rather than the open because the close is the observable event: a bar opening
    before the Signal still closes after it, and that close is a price the Signal could have been
    filled at.
    """

    for open_ms, close in closes:
        close_ms = open_ms + BAR_INTERVAL_MS
        if close_ms >= at_ms:
            return close_ms, close
    return None


def _return_percent(entry_close: str, later: tuple[int, str] | None) -> str:
    if later is None:
        return "pending"
    try:
        entry = float(entry_close)
        exit_price = float(later[1])
    except (TypeError, ValueError):
        return "-"
    if entry <= 0:
        return "-"
    return f"{(exit_price / entry - 1.0) * 100:+.2f}%"


def _bps_percent(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return "-"
    return f"{value / 100:+.2f}%"


def _usd(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return "-"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    return f"${value}"


def _manifest_venue(manifest: Mapping[str, Any]) -> str:
    trigger = manifest.get("primary_trigger")
    trigger = trigger if isinstance(trigger, Mapping) else {}
    return str(trigger.get("venue") or "")


__all__ = [
    "RESULT_HOLD_MS",
    "RESULT_SETTLE_MS",
    "TRADING_NOTIFICATION_TASK_NAME",
    "ResultBarReader",
    "TradingNotificationWorker",
    "TradingNotifier",
    "trading_notification_text",
    "trading_result_text",
]
