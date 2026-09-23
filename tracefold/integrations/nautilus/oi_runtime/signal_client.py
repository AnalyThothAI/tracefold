"""At-least-once Signal and OperatorIntent reader with bounded in-process admission."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from threading import Lock

from tracefold.trading.execution_contracts import OperatorIntentV1, TradeSignalV1, TradeSignalV2

_DEFAULT_MAX_COUNT = 256
_DEFAULT_MAX_BYTES = 1_048_576

type SignalReader = Callable[[str, str, int], Sequence[TradeSignalV1 | TradeSignalV2]]
type CommandReader = Callable[[str, str, int], Sequence[OperatorIntentV1]]


class ExecutionSignalClient:
    """Keep unresolved Signals and Commands in one bounded callback input."""

    def __init__(
        self,
        *,
        account_slot: str,
        execution_strategy: str,
        max_count: int = _DEFAULT_MAX_COUNT,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        if max_count <= 0 or max_bytes <= 0:
            raise ValueError("oi_runtime_signal_bounds_invalid")
        self.account_slot = account_slot
        self.execution_strategy = execution_strategy
        self._max_count = max_count
        self._max_bytes = max_bytes
        self._values: deque[tuple[TradeSignalV1 | TradeSignalV2, int]] = deque()
        self._commands: deque[tuple[OperatorIntentV1, int]] = deque()
        self._pending_ids: set[str] = set()
        self._pending_command_ids: set[str] = set()
        self._command_priority_enabled = False
        self._command_scan_complete = True
        self._bytes = 0
        self._lock = Lock()

    # The two the Runtime itself reads: `_pump` drains Commands before it looks at a single Signal, so
    # it has to know both that the Command queue is empty and that the last scan of it was complete.
    # `queued_count`, `queued_bytes`, `pending_ids` and `pending_command_ids` were four more public
    # properties beside them that only assertions ever read; they are test-side readers of this
    # object's private state now (`tests/helpers/nautilus_oi_runtime_process.py`, #589 PR-2).

    @property
    def queued_command_count(self) -> int:
        with self._lock:
            return len(self._commands)

    @property
    def command_scan_complete(self) -> bool:
        with self._lock:
            return self._command_scan_complete

    def poll_once(self, reader: SignalReader) -> int:
        with self._lock:
            if self._command_priority_enabled and not self._command_scan_complete:
                return 0
            free_count = self._max_count - len(self._values) - len(self._commands)
        if free_count <= 0:
            return 0
        values = reader(
            self.account_slot,
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
        values = reader(self.account_slot, self.execution_strategy, free_count)
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

    def next_nowait(self) -> TradeSignalV1 | TradeSignalV2 | None:
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
        """A disposition for this Signal is in the ledger, so the indexed read will never offer it again.

        A Signal whose plan was committed by an earlier generation was never polled by this one, so a
        verdict written for it settles nothing here; that is not an error.
        """

        with self._lock:
            self._pending_ids.discard(signal_id)

    def mark_command_durable(self, command_id: str) -> None:
        with self._lock:
            self._pending_command_ids.discard(command_id)

    def release(self, signal_id: str) -> None:
        """Give up the in-process claim on a Signal whose verdict could not be queued.

        `mark_durable` says "a disposition for this Signal is in the ledger"; this says "no disposition
        exists and none is coming from this attempt". The Signal stays unresolved, so the next indexed
        poll offers it again.
        """

        with self._lock:
            self._pending_ids.discard(signal_id)

    def release_command(self, command_id: str) -> None:
        """The Command half of `release`."""

        with self._lock:
            self._pending_command_ids.discard(command_id)


__all__ = ["ExecutionSignalClient"]
