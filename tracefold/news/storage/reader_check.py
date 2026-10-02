"""A News-only transactional generation witnesses a repeatable-read permission snapshot.

The short writer locks the singleton after its Event/job or intent. Relevant fact triggers
advance it under the same lock, so a reader either sees their commit or rejects its old proof.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ReaderCheck:
    event_id: str
    revision: str | None
    generation: int


def reader_unchanged(conn: Any, check: ReaderCheck) -> bool:
    row = conn.execute("SELECT revision FROM news_reader_clock WHERE singleton FOR UPDATE").fetchone()
    if row is None:
        raise RuntimeError("news_reader_clock_missing")
    return int(row["revision"]) == check.generation
