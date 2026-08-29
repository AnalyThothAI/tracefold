"""The canonical deny-list. One row per underlying, never per provider spelling.

The whole point of canonicalising first is that the operator writes `CL` once and it blocks `CL`,
`XYZ-CL` and any other prefix the provider invents, on both venues. Storing raw spellings would make
the list a guessing game that silently stops covering the case it was added for.

**A read failure is not a value here (#331).** `Blacklist.unavailable()` used to be a distinct
"block everything" snapshot the scanner reached for when the read raised, which turned a PostgreSQL
fault into a business refusal filed against every frame in the window. The read now runs inside the
lane's one bounded transaction and an exception propagates: the turn ends, nothing durable is written,
and the next turn re-reads. Fail-closed is still what happens — no Case, no Intent — but it is
recorded as infrastructure rather than as a deny-list decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import canonical_base_symbol, canonical_sha256, underlying_key


class CanonicalBlacklistEntryV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    underlying_key: str
    reason: str
    created_at_ms: int = Field(gt=0)
    expires_at_ms: int | None = None


class BlacklistSnapshotV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_version: Literal["blacklist_snapshot_v1"] = "blacklist_snapshot_v1"
    revision: int = Field(ge=0)
    active_rows: tuple[CanonicalBlacklistEntryV1, ...]

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        keys = [row.underlying_key for row in self.active_rows]
        if keys != sorted(set(keys)):
            raise ValueError("blacklist_snapshot_order_invalid")
        return self

    @property
    def snapshot_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class BlacklistEntry:
    base_symbol: str
    reason: str
    created_at_ms: int = 1
    expires_at_ms: int | None = None

    def active_at(self, now_ms: int) -> bool:
        return self.expires_at_ms is None or int(self.expires_at_ms) > int(now_ms)


@dataclass(frozen=True, slots=True)
class Blacklist:
    """An immutable snapshot of the deny-list as one statement saw it."""

    entries: Mapping[str, BlacklistEntry]

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> Blacklist:
        entries: dict[str, BlacklistEntry] = {}
        for row in rows:
            symbol = canonical_base_symbol(row.get("base_symbol"))
            if not symbol:
                continue
            expires = row.get("expires_at_ms")
            entries[symbol] = BlacklistEntry(
                base_symbol=symbol,
                reason=str(row.get("reason") or "unspecified"),
                created_at_ms=max(1, int(row.get("created_at_ms") or 1)),
                expires_at_ms=None if expires is None else int(expires),
            )
        return cls(entries=entries)

    def blocked(self, symbol: object, *, now_ms: int) -> BlacklistEntry | None:
        """The entry that blocks this symbol, or None."""

        canonical = canonical_base_symbol(symbol)
        if not canonical:
            return BlacklistEntry(base_symbol="", reason="symbol_not_canonicalisable")
        entry = self.entries.get(canonical)
        if entry is None or not entry.active_at(now_ms):
            return None
        return entry

    def snapshot(self, *, revision: int, now_ms: int) -> BlacklistSnapshotV1:
        rows = tuple(
            CanonicalBlacklistEntryV1(
                underlying_key=underlying_key(entry.base_symbol),
                reason=entry.reason,
                created_at_ms=entry.created_at_ms,
                expires_at_ms=entry.expires_at_ms,
            )
            for entry in sorted(self.entries.values(), key=lambda item: item.base_symbol)
            if entry.active_at(now_ms)
        )
        return BlacklistSnapshotV1(revision=revision, active_rows=rows)


__all__ = [
    "Blacklist",
    "BlacklistEntry",
    "BlacklistSnapshotV1",
    "CanonicalBlacklistEntryV1",
]
