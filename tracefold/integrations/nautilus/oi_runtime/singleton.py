"""Dedicated PostgreSQL session owner for one Binance account-slot writer."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, Lock


class AccountSlotSingleton:
    """Fail closed when the session owning the advisory lock is lost."""

    def __init__(
        self,
        *,
        account_slot: str,
        try_acquire: Callable[[str], bool],
        release: Callable[[str], bool],
        heartbeat: Callable[[], bool],
    ) -> None:
        self.account_slot = account_slot
        self._try_acquire = try_acquire
        self._release = release
        self._heartbeat = heartbeat
        self._acquired = False
        self._lost = False
        self._lock = Lock()

    @property
    def acquired(self) -> bool:
        with self._lock:
            return self._acquired and not self._lost

    @property
    def lost(self) -> bool:
        with self._lock:
            return self._lost

    def acquire(self) -> bool:
        with self._lock:
            if self._acquired:
                raise RuntimeError("oi_runtime_singleton_already_acquired")
        acquired = self._try_acquire(self.account_slot)
        with self._lock:
            self._acquired = acquired
            self._lost = False
        return acquired

    def check(self) -> bool:
        with self._lock:
            if not self._acquired or self._lost:
                return False
        try:
            alive = self._heartbeat()
        except Exception:
            alive = False
        if not alive:
            with self._lock:
                self._lost = True
                self._acquired = False
        return alive

    def release(self) -> bool:
        with self._lock:
            if not self._acquired or self._lost:
                self._acquired = False
                return False
        released = self._release(self.account_slot)
        with self._lock:
            self._acquired = False
        return released


def run_singleton_monitor(
    singleton: AccountSlotSingleton,
    *,
    stop: Event,
    interval_seconds: float = 1.0,
) -> None:
    if interval_seconds <= 0:
        raise ValueError("oi_runtime_singleton_interval_invalid")
    while not stop.wait(interval_seconds):
        if not singleton.check():
            return


__all__ = ["AccountSlotSingleton", "run_singleton_monitor"]
