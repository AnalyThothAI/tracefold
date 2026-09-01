"""At-least-once Signal and OperatorIntent reader with bounded in-process admission."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Sequence
from threading import Event, Lock
from typing import Any

from tracefold.trading import OperatorIntentV1, TradeSignalV1

_DEFAULT_MAX_COUNT = 256
_DEFAULT_MAX_BYTES = 1_048_576
_POSTGRES_CHANNEL = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

type SignalReader = Callable[[str, str, int], Sequence[TradeSignalV1]]
type CommandReader = Callable[[str, str, int], Sequence[OperatorIntentV1]]


class ExecutionSignalClient:
    """Keep unresolved Signals and Commands in one bounded callback input."""

    def __init__(
        self,
        *,
        runtime_profile_id: str,
        execution_strategy: str,
        max_count: int = _DEFAULT_MAX_COUNT,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        if max_count <= 0 or max_bytes <= 0:
            raise ValueError("oi_runtime_signal_bounds_invalid")
        self.runtime_profile_id = runtime_profile_id
        self.execution_strategy = execution_strategy
        self._max_count = max_count
        self._max_bytes = max_bytes
        self._values: deque[tuple[TradeSignalV1, int]] = deque()
        self._commands: deque[tuple[OperatorIntentV1, int]] = deque()
        self._pending_ids: set[str] = set()
        self._pending_command_ids: set[str] = set()
        self._command_priority_enabled = False
        self._command_scan_complete = True
        self._bytes = 0
        self._lock = Lock()

    @property
    def queued_count(self) -> int:
        with self._lock:
            return len(self._values) + len(self._commands)

    @property
    def queued_command_count(self) -> int:
        with self._lock:
            return len(self._commands)

    @property
    def command_scan_complete(self) -> bool:
        with self._lock:
            return self._command_scan_complete

    @property
    def queued_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def pending_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._pending_ids)

    @property
    def pending_command_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._pending_command_ids)

    def poll_once(self, reader: SignalReader) -> int:
        with self._lock:
            if self._command_priority_enabled and not self._command_scan_complete:
                return 0
            free_count = self._max_count - len(self._values) - len(self._commands)
        if free_count <= 0:
            return 0
        values = reader(
            self.runtime_profile_id,
            self.execution_strategy,
            free_count,
        )
        admitted = 0
        for value in values:
            size = len(value.model_dump_json().encode())
            with self._lock:
                if value.signal_id in self._pending_ids:
                    continue
                if len(self._values) + len(self._commands) >= self._max_count or self._bytes + size > self._max_bytes:
                    break
                self._values.append((value, size))
                self._pending_ids.add(value.signal_id)
                self._bytes += size
                admitted += 1
        return admitted

    def poll_commands_once(self, reader: CommandReader) -> int:
        with self._lock:
            self._command_priority_enabled = True
            self._command_scan_complete = False
            if len(self._values) + len(self._commands) >= self._max_count and self._values:
                self._evict_latest_signal_unlocked()
            free_count = self._max_count - len(self._values) - len(self._commands)
        if free_count <= 0:
            return 0
        values = reader(self.runtime_profile_id, self.execution_strategy, free_count)
        scan_complete = len(values) < free_count
        admitted = 0
        for value in values:
            size = len(value.model_dump_json().encode())
            with self._lock:
                if value.command_id in self._pending_command_ids:
                    continue
                while self._values and (
                    len(self._values) + len(self._commands) >= self._max_count or self._bytes + size > self._max_bytes
                ):
                    self._evict_latest_signal_unlocked()
                if len(self._values) + len(self._commands) >= self._max_count or self._bytes + size > self._max_bytes:
                    scan_complete = False
                    break
                self._commands.append((value, size))
                self._pending_command_ids.add(value.command_id)
                self._bytes += size
                admitted += 1
        with self._lock:
            self._command_scan_complete = scan_complete
        return admitted

    def _evict_latest_signal_unlocked(self) -> None:
        value, size = self._values.pop()
        self._pending_ids.remove(value.signal_id)
        self._bytes -= size

    def next_nowait(self) -> TradeSignalV1 | None:
        with self._lock:
            if not self._values:
                return None
            value, size = self._values.popleft()
            self._bytes -= size
            return value

    def next_command_nowait(self) -> OperatorIntentV1 | None:
        with self._lock:
            if not self._commands:
                return None
            value, size = self._commands.popleft()
            self._bytes -= size
            return value

    def mark_durable(self, signal_id: str) -> None:
        with self._lock:
            if signal_id not in self._pending_ids:
                raise RuntimeError("oi_runtime_signal_not_pending")
            self._pending_ids.remove(signal_id)

    def mark_command_durable(self, command_id: str) -> None:
        with self._lock:
            if command_id not in self._pending_command_ids:
                raise RuntimeError("oi_runtime_command_not_pending")
            self._pending_command_ids.remove(command_id)

    def retry(self, value: TradeSignalV1) -> None:
        """Return a popped Signal when its final audit fact could not be buffered."""

        size = len(value.model_dump_json().encode())
        with self._lock:
            if value.signal_id not in self._pending_ids:
                raise RuntimeError("oi_runtime_signal_not_pending")
            if any(queued.signal_id == value.signal_id for queued, _ in self._values):
                return
            if len(self._values) + len(self._commands) >= self._max_count or self._bytes + size > self._max_bytes:
                raise RuntimeError("oi_runtime_signal_retry_overflow")
            self._values.appendleft((value, size))
            self._bytes += size

    def retry_command(self, value: OperatorIntentV1) -> None:
        """Return a popped Command when its final audit fact could not be buffered."""

        size = len(value.model_dump_json().encode())
        with self._lock:
            if value.command_id not in self._pending_command_ids:
                raise RuntimeError("oi_runtime_command_not_pending")
            if any(queued.command_id == value.command_id for queued, _ in self._commands):
                return
            if len(self._values) + len(self._commands) >= self._max_count or self._bytes + size > self._max_bytes:
                raise RuntimeError("oi_runtime_command_retry_overflow")
            self._commands.appendleft((value, size))
            self._bytes += size


def install_execution_stream_listener(conn: Any, *, channel: str) -> None:
    """LISTEN is a wake hint; every wake and timeout is followed by an indexed poll."""

    if not bool(getattr(conn, "autocommit", False)):
        raise RuntimeError("oi_runtime_listener_requires_autocommit")
    if _POSTGRES_CHANNEL.fullmatch(channel) is None:
        raise ValueError("oi_runtime_listener_channel_invalid")
    conn.execute(f"LISTEN {channel}")


def wait_for_execution_stream_wake(conn: Any, timeout_seconds: float) -> bool:
    if timeout_seconds <= 0:
        raise ValueError("oi_runtime_poll_interval_invalid")
    notification = next(conn.notifies(timeout=timeout_seconds, stop_after=1), None)
    return notification is not None


def run_signal_poll_loop(
    *,
    client: ExecutionSignalClient,
    reader: SignalReader,
    command_reader: CommandReader | None = None,
    listener_conn: Any,
    channel: str,
    stop: Event,
    poll_interval_seconds: float,
    on_failure: Callable[[BaseException], None],
) -> None:
    """Own blocking PostgreSQL work outside the TradingNode callback thread."""

    try:
        install_execution_stream_listener(listener_conn, channel=channel)
        while not stop.is_set():
            poll_execution_inputs_once(client=client, reader=reader, command_reader=command_reader)
            if stop.is_set():
                break
            wait_for_execution_stream_wake(listener_conn, poll_interval_seconds)
    except BaseException as exc:
        on_failure(exc)


def poll_execution_inputs_once(
    *,
    client: ExecutionSignalClient,
    reader: SignalReader,
    command_reader: CommandReader | None = None,
) -> tuple[int, int]:
    """Admit Commands before Signals into their one shared bounded queue."""

    commands = 0 if command_reader is None else client.poll_commands_once(command_reader)
    signals = client.poll_once(reader)
    return commands, signals


__all__ = [
    "ExecutionSignalClient",
    "install_execution_stream_listener",
    "poll_execution_inputs_once",
    "run_signal_poll_loop",
    "wait_for_execution_stream_wake",
]
