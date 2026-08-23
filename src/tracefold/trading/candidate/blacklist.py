"""The canonical deny-list. One row per underlying, never per provider spelling.

The whole point of canonicalising first is that the operator writes `CL` once and it blocks `CL`,
`XYZ-CL` and any other prefix the provider invents, on both venues. Storing raw spellings would make
the list a guessing game that silently stops covering the case it was added for.

A read failure is not an empty list. `Blacklist.unavailable()` is a distinct value and every caller
treats it as "block everything": the deny-list is the last thing that should fail open.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..contracts import canonical_base_symbol


@dataclass(frozen=True, slots=True)
class BlacklistEntry:
    base_symbol: str
    reason: str
    expires_at_ms: int | None = None

    def active_at(self, now_ms: int) -> bool:
        return self.expires_at_ms is None or int(self.expires_at_ms) > int(now_ms)


@dataclass(frozen=True, slots=True)
class Blacklist:
    """An immutable snapshot. `available=False` means the read failed, which blocks every symbol."""

    entries: Mapping[str, BlacklistEntry]
    available: bool = True

    @classmethod
    def unavailable(cls) -> Blacklist:
        return cls(entries={}, available=False)

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
                expires_at_ms=None if expires is None else int(expires),
            )
        return cls(entries=entries)

    def blocked(self, symbol: object, *, now_ms: int) -> BlacklistEntry | None:
        """The entry that blocks this symbol, or None. A failed read blocks everything."""

        if not self.available:
            return BlacklistEntry(base_symbol=canonical_base_symbol(symbol), reason="blacklist_unavailable")
        canonical = canonical_base_symbol(symbol)
        if not canonical:
            return BlacklistEntry(base_symbol="", reason="symbol_not_canonicalisable")
        entry = self.entries.get(canonical)
        if entry is None or not entry.active_at(now_ms):
            return None
        return entry


__all__ = ["Blacklist", "BlacklistEntry"]
