"""Persistence for the tradeable instrument universe (#75). Callers own the transaction, like NewsRepository.

The snapshot write is idempotent by ``(venue, venue_symbol)``: re-running it on an unchanged universe touches only
``last_seen_ms``. ``first_seen_ms`` is never moved once set — it is the listing time the diff reports.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .instruments import (
    ALIAS_SEEDS,
    Instrument,
    UniverseDiff,
    instruments_from_rows,
    resolve_base_symbol,
)


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """What one snapshot changed. ``seeded`` marks the first snapshot for a venue, whose "new" rows are not
    listings — there was simply nothing to compare against."""

    diff: UniverseDiff
    seeded_venues: tuple[str, ...]
    total: int

    @property
    def reportable(self) -> UniverseDiff:
        """The diff with seed venues removed: what a listing card may be built from."""

        if not self.seeded_venues:
            return self.diff
        seeded = frozenset(self.seeded_venues)
        return UniverseDiff(
            listed=tuple(i for i in self.diff.listed if i.venue not in seeded),
            delisted=tuple(i for i in self.diff.delisted if i.venue not in seeded),
            unchanged=self.diff.unchanged,
        )


class InstrumentsRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # ---------------------------------------------------------------- reads

    def tradeable_base_symbols(self) -> frozenset[str]:
        """Every base symbol currently trading somewhere, plus every alias that resolves into one.

        The Gate uses this as an existence check. It is deliberately *not* a false-positive filter: `NEAR`, `ACT`,
        `W`, `BILL` and `FLOCK` are real listed tokens that are also ordinary English words, so the word-collision
        stop-list in `gate.py` stays and both conditions apply.
        """

        rows = self.conn.execute(
            "SELECT DISTINCT base_symbol FROM news_market_instruments WHERE status = 'trading'"
        ).fetchall()
        symbols = {str(row["base_symbol"]).upper() for row in rows}
        alias_rows = self.conn.execute("SELECT alias, base_symbol FROM news_symbol_aliases").fetchall()
        for row in alias_rows:
            if str(row["base_symbol"]).upper() in symbols:
                symbols.add(str(row["alias"]).upper())
        return frozenset(symbols)

    def alias_map(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT alias, base_symbol FROM news_symbol_aliases").fetchall()
        return {str(row["alias"]).upper(): str(row["base_symbol"]).upper() for row in rows}

    def instrument_classes(self) -> dict[str, str]:
        """base_symbol -> instrument_class, preferring the most specific class when venues disagree.

        A symbol on both `binance.perp` (crypto by venue default) and `hl.xyz` (equity) is an equity: the specific
        classification wins over the venue default.
        """

        rows = self.conn.execute(
            "SELECT base_symbol, instrument_class FROM news_market_instruments WHERE status = 'trading'"
        ).fetchall()
        rank = {"unknown": 0, "crypto": 1, "fx": 2, "index": 3, "commodity": 4, "equity": 5, "pre_ipo": 6}
        out: dict[str, str] = {}
        for row in rows:
            symbol = str(row["base_symbol"]).upper()
            cls = str(row["instrument_class"])
            if rank.get(cls, 0) > rank.get(out.get(symbol, "unknown"), 0):
                out[symbol] = cls
        return out

    def venues_for(self, base_symbol: str) -> tuple[str, ...]:
        rows = self.conn.execute(
            "SELECT venue FROM news_market_instruments WHERE base_symbol = %s AND status = 'trading' ORDER BY venue",
            (str(base_symbol).upper(),),
        ).fetchall()
        return tuple(str(row["venue"]) for row in rows)

    def asset_refs(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Provider coin tags -> what each one actually names on a venue, for one bounded batch (#87).

        The console shows a grounded asset as `hl.perp:HYPE`, and shows a tag that resolves to nothing as struck
        through — that is the only place a reader sees the difference between "the provider tagged a token" and
        "the token exists". Resolution is the same two steps the Gate takes: alias first, then existence.

        A batch, never one query per symbol: the feed serves up to 100 Events on a three-second poll. Loading
        the whole universe (`tradeable_base_symbols`) instead would be ~1.3k rows per poll to answer at most a
        few dozen questions.

        `venue` is the *preferred* venue when a base trades on several — deepest first, HIP-3 builder DEXs last —
        so a chip is stable across polls rather than reshuffling with whatever the planner returned.
        """

        wanted = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
        if not wanted:
            return {}
        rows = self.conn.execute(
            """
            SELECT s.symbol,
                   COALESCE(a.base_symbol, s.symbol) AS base_symbol,
                   m.venue
              FROM unnest(%s::text[]) AS s(symbol)
              LEFT JOIN news_symbol_aliases a ON a.alias = s.symbol
              LEFT JOIN LATERAL (
                SELECT i.venue
                  FROM news_market_instruments i
                 WHERE i.base_symbol = COALESCE(a.base_symbol, s.symbol) AND i.status = 'trading'
                 ORDER BY CASE i.venue
                            WHEN 'binance.perp' THEN 0
                            WHEN 'binance.spot' THEN 1
                            WHEN 'hl.perp' THEN 2
                            WHEN 'hl.spot' THEN 3
                            ELSE 4
                          END, i.venue
                 LIMIT 1
              ) m ON true
            """,
            (wanted,),
        ).fetchall()
        return {
            str(row["symbol"]): {
                "symbol": str(row["symbol"]),
                "base_symbol": str(row["base_symbol"]),
                "venue": str(row["venue"]) if row["venue"] else None,
                "listed": row["venue"] is not None,
            }
            for row in rows
        }

    def aliases_by_base(self, base_symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        """base_symbol -> every name that resolves into it, for the detail page's normalization block (#87).

        This is what makes the storyline throttle legible: SKHY / SKHX / SKHYNIX are three real contracts for one
        issuer, and the reader needs to see that they share a bucket rather than wonder why one buyback shipped
        one card. Only bases that actually collapse something are worth a row, so the caller drops singletons.
        """

        wanted = sorted({str(symbol).upper() for symbol in base_symbols if str(symbol).strip()})
        if not wanted:
            return {}
        rows = self.conn.execute(
            "SELECT alias, base_symbol, source FROM news_symbol_aliases WHERE base_symbol = ANY(%s)"
            " ORDER BY base_symbol, alias",
            (wanted,),
        ).fetchall()
        # The base is one of the names, not something outside the group: the console renders the row as
        # `SKHY SKHX SKHYNIX -> SKHY`, and dropping the base would make one third of the collapse invisible.
        out: dict[str, dict[str, Any]] = {
            base: {"base_symbol": base, "aliases": [base], "sources": []} for base in wanted
        }
        for row in rows:
            entry = out[str(row["base_symbol"]).upper()]
            alias = str(row["alias"]).upper()
            if alias not in entry["aliases"]:
                entry["aliases"].append(alias)
            source = str(row["source"])
            if source not in entry["sources"]:
                entry["sources"].append(source)
        return out

    def universe_summary(self) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'trading')  AS trading,
                   count(*) FILTER (WHERE status = 'delisted') AS delisted,
                   count(DISTINCT base_symbol) FILTER (WHERE status = 'trading') AS base_symbols,
                   count(DISTINCT venue)       FILTER (WHERE status = 'trading') AS venues,
                   max(last_seen_ms) AS last_snapshot_ms
            FROM news_market_instruments
            """
        ).fetchone()
        summary = dict(row) if row else {}
        by_venue = self.conn.execute(
            "SELECT venue, count(*) AS n FROM news_market_instruments"
            " WHERE status = 'trading' GROUP BY venue ORDER BY n DESC"
        ).fetchall()
        summary["by_venue"] = {str(r["venue"]): int(r["n"]) for r in by_venue}
        return summary

    def recent_listings(self, *, since_ms: int, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT venue, venue_symbol, base_symbol, instrument_class, first_seen_ms
            FROM news_market_instruments
            WHERE status = 'trading' AND first_seen_ms >= %s
            ORDER BY first_seen_ms DESC, venue, venue_symbol
            LIMIT %s
            """,
            (int(since_ms), int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def all_instruments(self) -> tuple[Instrument, ...]:
        rows = self.conn.execute(
            "SELECT venue, venue_symbol, base_symbol, instrument_class, quote_asset, status"
            " FROM news_market_instruments"
        ).fetchall()
        return instruments_from_rows(dict(row) for row in rows)

    # --------------------------------------------------------------- writes

    def seed_aliases(self, *, now_ms: int, seeds: Mapping[str, str] = ALIAS_SEEDS) -> int:
        """Insert the operator seed aliases. Never overwrites an alias an operator already changed."""

        written = 0
        for alias, base in seeds.items():
            cursor = self.conn.execute(
                """
                INSERT INTO news_symbol_aliases (alias, base_symbol, source, updated_at_ms)
                VALUES (%s, %s, 'operator', %s)
                ON CONFLICT (alias) DO NOTHING
                """,
                (alias.upper(), str(base).upper(), int(now_ms)),
            )
            written += int(getattr(cursor, "rowcount", 0) or 0)
        return written

    def apply_snapshot(self, instruments: Sequence[Instrument], *, now_ms: int) -> SnapshotResult:
        """Upsert one snapshot and report what changed.

        Venues absent from ``instruments`` are left untouched — a venue that failed to answer must not read as a
        mass delisting. Only symbols missing from a venue that *did* answer are marked delisted.

        The diff compares against the rows that were *trading*, so an already-delisted contract is not re-reported
        as a fresh delisting on every snapshot, and one that comes back reads as a listing again. `seeded` still
        looks at every stored row, so a venue whose symbols have all been delisted is not mistaken for a new one.
        """

        answered = {i.venue for i in instruments}
        stored = tuple(i for i in self.all_instruments() if i.venue in answered)
        previous = tuple(i for i in stored if i.status == "trading")
        seeded = tuple(sorted(answered - {i.venue for i in stored}))

        for item in instruments:
            self.conn.execute(
                """
                INSERT INTO news_market_instruments (
                  venue, venue_symbol, base_symbol, instrument_class, quote_asset, status,
                  first_seen_ms, last_seen_ms
                ) VALUES (%s, %s, %s, %s, %s, 'trading', %s, %s)
                ON CONFLICT (venue, venue_symbol) DO UPDATE SET
                  base_symbol      = EXCLUDED.base_symbol,
                  instrument_class = EXCLUDED.instrument_class,
                  quote_asset      = EXCLUDED.quote_asset,
                  status           = 'trading',
                  last_seen_ms     = EXCLUDED.last_seen_ms
                """,
                (
                    item.venue,
                    item.venue_symbol,
                    item.base_symbol,
                    item.instrument_class,
                    item.quote_asset,
                    int(now_ms),
                    int(now_ms),
                ),
            )

        current_keys = {(i.venue, i.venue_symbol) for i in instruments}
        gone = [i for i in previous if (i.venue, i.venue_symbol) not in current_keys]
        for item in gone:
            self.conn.execute(
                "UPDATE news_market_instruments SET status = 'delisted', last_seen_ms = %s"
                " WHERE venue = %s AND venue_symbol = %s",
                (int(now_ms), item.venue, item.venue_symbol),
            )

        prev_keys = {(i.venue, i.venue_symbol) for i in previous}
        listed = tuple(
            sorted(
                (i for i in instruments if (i.venue, i.venue_symbol) not in prev_keys),
                key=lambda i: (i.venue, i.venue_symbol),
            )
        )
        delisted = tuple(sorted(gone, key=lambda i: (i.venue, i.venue_symbol)))
        diff = UniverseDiff(listed=listed, delisted=delisted, unchanged=len(current_keys & prev_keys))
        return SnapshotResult(diff=diff, seeded_venues=seeded, total=len(instruments))

    def learn_aliases_from_universe(self, *, now_ms: int) -> int:
        """Record the venue-derived aliases the pipeline will meet: the provider's ``XYZ-`` form and each
        ``dex:SYMBOL`` venue symbol, both pointing at their base. Cheap, and it keeps `alias_map()` self-contained."""

        rows = self.conn.execute(
            "SELECT DISTINCT venue, venue_symbol, base_symbol FROM news_market_instruments WHERE status = 'trading'"
        ).fetchall()
        written = 0
        for row in rows:
            venue_symbol = str(row["venue_symbol"])
            base = str(row["base_symbol"]).upper()
            aliases = {f"XYZ-{base}"} if str(row["venue"]) == "hl.xyz" else set()
            if ":" in venue_symbol:
                aliases.add(venue_symbol.upper())
            for alias in aliases:
                if alias == base:
                    continue
                cursor = self.conn.execute(
                    """
                    INSERT INTO news_symbol_aliases (alias, base_symbol, source, updated_at_ms)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (alias) DO NOTHING
                    """,
                    (alias, base, "venue" if ":" in alias else "opennews_prefix", int(now_ms)),
                )
                written += int(getattr(cursor, "rowcount", 0) or 0)
        return written

    def resolve(self, symbol: str, *, aliases: Mapping[str, str] | None = None) -> str:
        return resolve_base_symbol(symbol, aliases if aliases is not None else self.alias_map())


def instruments_from_iterable(rows: Iterable[Mapping[str, object]]) -> tuple[Instrument, ...]:
    return instruments_from_rows(rows)


__all__ = ["InstrumentsRepository", "SnapshotResult", "instruments_from_iterable"]
