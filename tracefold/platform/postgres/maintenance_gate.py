"""One PostgreSQL advisory gate shared by steady Workers and migrations."""

from __future__ import annotations

from typing import Any

MAINTENANCE_GATE_LOCK_KEYS = (0x54524644, 0)


def acquire_maintenance_gate(conn: Any) -> None:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
        MAINTENANCE_GATE_LOCK_KEYS,
    ).fetchone()
    if row is None or not bool(row["acquired"]):
        raise RuntimeError("steady_workers_runtime_active")


def release_maintenance_gate(conn: Any) -> None:
    row = conn.execute(
        "SELECT pg_advisory_unlock(%s, %s) AS released",
        MAINTENANCE_GATE_LOCK_KEYS,
    ).fetchone()
    if row is None or not bool(row["released"]):
        raise RuntimeError("maintenance_runtime_lock_not_owned")


def acquire_steady_gate(conn: Any) -> None:
    row = conn.execute(
        "SELECT pg_try_advisory_lock_shared(%s, %s) AS acquired",
        MAINTENANCE_GATE_LOCK_KEYS,
    ).fetchone()
    if row is None or not bool(row["acquired"]):
        raise RuntimeError("maintenance_runtime_active")


def release_steady_gate(conn: Any) -> None:
    row = conn.execute(
        "SELECT pg_advisory_unlock_shared(%s, %s) AS released",
        MAINTENANCE_GATE_LOCK_KEYS,
    ).fetchone()
    if row is None or not bool(row["released"]):
        raise RuntimeError("steady_runtime_lock_not_owned")


__all__ = [
    "MAINTENANCE_GATE_LOCK_KEYS",
    "acquire_maintenance_gate",
    "acquire_steady_gate",
    "release_maintenance_gate",
    "release_steady_gate",
]
