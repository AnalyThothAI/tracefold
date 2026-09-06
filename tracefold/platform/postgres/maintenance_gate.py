"""One PostgreSQL advisory gate shared by steady Workers and migrations.

The exclusive half of the pair is taken by `alembic/env.py` directly against its own migration
connection, so only the shared half a steady Workers runtime holds lives here (#589 P-F10).
"""

from __future__ import annotations

from typing import Any

MAINTENANCE_GATE_LOCK_KEYS = (0x54524644, 0)


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
    "acquire_steady_gate",
    "release_steady_gate",
]
