"""Persistence for the instrument universe (#75, consolidated in #89). Callers own the transaction, like NewsRepository.

The snapshot write is idempotent by ``(venue, venue_symbol)``: re-running it on an unchanged catalogue only moves
``last_seen_ms``. The universe answers two questions and no others — what is this issuer's canonical symbol, and is
this a coin or a stock — so there is no listing-time column and no diff here: OpenNews pushes listing frames and the
pipeline admits them (#72), which is a first source, not a second one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .instruments import (
    ALIAS_SEEDS,
    REFERENCE_VENUES,
    Instrument,
    instruments_from_rows,
    normalize_symbol,
    resolve_base_symbol,
)


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """What one snapshot wrote. ``delisted`` counts contracts an answering venue no longer lists — an operational
    signal, not a card: a delisting the reader should hear about arrives as a provider frame."""

    total: int
    venues: tuple[str, ...]
    delisted: int


class InstrumentsRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # ---------------------------------------------------------------- reads

    def alias_map(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT alias, base_symbol FROM news_symbol_aliases").fetchall()
        return {str(row["alias"]).upper(): str(row["base_symbol"]).upper() for row in rows}

    def instrument_classes(self) -> dict[str, str]:
        """base_symbol -> instrument_class, in two tiers: venues we poll first, reference directories after.

        Within the traded tier the most specific class wins — a symbol on both `binance.perp` (crypto by venue
        default) and `hl.xyz` (equity) is an equity. This is what the Gate reads to decide whether a headline is
        about a coin or a stock (#89), replacing a guess based on the provider's `XYZ-` prefix.

        The reference tier (`us.listed`, #91) never overrides a traded symbol, and the ranking cannot express
        that: `equity` outranks `crypto`, so one shared dictionary would turn `ATOM` (Atomera on the NYSE, the
        Cosmos token on three exchanges) into a stock. A symbol anyone can actually trade is described by the
        venue that lists it; the directory only speaks for the thousands of tickers no venue lists at all.
        """

        rows = self.conn.execute(
            "SELECT base_symbol, instrument_class, venue FROM news_market_instruments WHERE status = 'trading'"
        ).fetchall()
        rank = {"unknown": 0, "crypto": 1, "fx": 2, "index": 3, "commodity": 4, "equity": 5, "pre_ipo": 6}
        out: dict[str, str] = {}
        reference: dict[str, str] = {}
        for row in rows:
            symbol = str(row["base_symbol"]).upper()
            cls = str(row["instrument_class"])
            tier = reference if str(row["venue"]) in REFERENCE_VENUES else out
            if rank.get(cls, 0) > rank.get(tier.get(symbol, "unknown"), 0):
                tier[symbol] = cls
        for symbol, cls in reference.items():
            out.setdefault(symbol, cls)
        # Aliases carry the class of what they resolve to, so the caller needs one lookup and no alias table:
        # `SKHYNIX` and `XYZ-SKHX` are the forms the provider actually sends. Resolution reads `bases`, never the
        # dict being written — otherwise a two-hop chain would resolve or not depending on the row order the
        # unordered SELECT happened to return, and the Gate's asset class would differ between restarts.
        bases = dict(out)
        alias_rows = self.conn.execute("SELECT alias, base_symbol FROM news_symbol_aliases").fetchall()
        for row in alias_rows:
            alias = str(row["alias"]).upper()
            resolved = bases.get(str(row["base_symbol"]).upper())
            if resolved and alias not in bases:
                out[alias] = resolved
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

        Input is the raw provider tag and the result is keyed by it, so a caller needs no normalization
        knowledge of its own; the returned ``symbol`` is the normalized form. Normalizing matters twice: the
        provider ships both `UNITREE` and `XYZ-UNITREE` for one instrument, and `news_event_assets` stores the
        stripped form — so resolving the raw tag would miss every builder-DEX symbol whose `XYZ-` alias row
        happens not to exist, and would print `hl.xyz:XYZ-UNITREE` on the chip (#87 review).

        Reference venues are excluded (#91). The chip and the funnel segment it feeds both mean "names something
        on a venue we poll"; letting a US-listed-only ticker light them up would quietly widen the console's
        `符号落表` count by ~95 Events a week and claim a tradeable instrument that does not exist. The reference
        tier answers a different question, and only `instrument_classes()` asks it.
        """

        normalized = {str(symbol): normalize_symbol(symbol) for symbol in symbols if str(symbol).strip()}
        wanted = sorted(set(normalized.values()))
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
                   AND NOT (i.venue = ANY(%s))
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
            (wanted, sorted(REFERENCE_VENUES)),
        ).fetchall()
        resolved = {
            str(row["symbol"]): {
                "symbol": str(row["symbol"]),
                "base_symbol": str(row["base_symbol"]),
                "venue": str(row["venue"]) if row["venue"] else None,
                "listed": row["venue"] is not None,
            }
            for row in rows
        }
        return {raw: resolved[norm] for raw, norm in normalized.items() if norm in resolved}

    def aliases_by_base(
        self, base_symbols: Iterable[str], *, sources: Sequence[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """base_symbol -> every name that resolves into it, for the detail page's normalization block (#87).

        This is what makes the storyline throttle legible: SKHY / SKHX / SKHYNIX are three real contracts for one
        issuer, and the reader needs to see that they share a bucket rather than wonder why one buyback shipped
        one card. Only bases that actually collapse something are worth a row, so the caller drops singletons.

        ``sources`` narrows to particular ``news_symbol_aliases.source`` values. The console passes
        ``("seed",)``: venue-derived rows are mechanical (`XYZ-{base}` exists for every builder-DEX base),
        so including them would fire the block on Events where nothing surprising happened.
        """

        wanted = sorted({str(symbol).upper() for symbol in base_symbols if str(symbol).strip()})
        if not wanted:
            return {}
        clause = "" if sources is None else " AND source = ANY(%s)"
        params: tuple[Any, ...] = (wanted,) if sources is None else (wanted, list(sources))
        rows = self.conn.execute(
            f"SELECT alias, base_symbol, source FROM news_symbol_aliases WHERE base_symbol = ANY(%s){clause}"
            " ORDER BY base_symbol, alias",
            params,
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

    def dangling_seed_aliases(self) -> tuple[dict[str, str], ...]:
        """Seed aliases whose target is not listed anywhere — the shape of the `1810.HK -> XIAOMI` bug (#89).

        One hop only: a seed is written by hand, and no seed chains today. An empty universe (no snapshot yet)
        reports nothing rather than flagging every seed at first boot.
        """

        rows = self.conn.execute(
            """
            SELECT a.alias, a.base_symbol
            FROM news_symbol_aliases a
            WHERE a.source = 'seed'
              AND EXISTS (SELECT 1 FROM news_market_instruments WHERE status = 'trading')
              AND NOT EXISTS (
                SELECT 1 FROM news_market_instruments m
                WHERE m.base_symbol = a.base_symbol AND m.status = 'trading'
              )
            ORDER BY a.alias
            """
        ).fetchall()
        return tuple({"alias": str(r["alias"]), "base_symbol": str(r["base_symbol"])} for r in rows)

    def universe_summary(self) -> dict[str, Any]:
        """Every figure here counts contracts on venues we poll; the reference tier gets its own count.

        Mixing them would make `trading` read 15k and the console's 「在交易合约」 figure a lie — a US ticker in
        the directory is not a contract anyone can take a position in (#91).
        """

        reference = sorted(REFERENCE_VENUES)
        row = self.conn.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'trading')  AS trading,
                   count(*) FILTER (WHERE status = 'delisted') AS delisted,
                   count(DISTINCT base_symbol) FILTER (WHERE status = 'trading') AS base_symbols,
                   count(DISTINCT venue)       FILTER (WHERE status = 'trading') AS venues
            FROM news_market_instruments WHERE NOT (venue = ANY(%s))
            """,
            (reference,),
        ).fetchone()
        summary = dict(row) if row else {}
        # The timestamp spans every venue: one stale tier is still a stale snapshot.
        stamp = self.conn.execute(
            "SELECT max(last_seen_ms) AS last_snapshot_ms FROM news_market_instruments"
        ).fetchone()
        summary["last_snapshot_ms"] = stamp["last_snapshot_ms"] if stamp else None
        summary["reference_symbols"] = int(
            self.conn.execute(
                "SELECT count(DISTINCT base_symbol) AS n FROM news_market_instruments"
                " WHERE status = 'trading' AND venue = ANY(%s)",
                (reference,),
            ).fetchone()["n"]
        )
        by_venue = self.conn.execute(
            "SELECT venue, count(*) AS n FROM news_market_instruments"
            " WHERE status = 'trading' AND NOT (venue = ANY(%s)) GROUP BY venue ORDER BY n DESC",
            (reference,),
        ).fetchall()
        summary["by_venue"] = {str(r["venue"]): int(r["n"]) for r in by_venue}
        by_class = self.conn.execute(
            "SELECT instrument_class, count(DISTINCT base_symbol) AS n FROM news_market_instruments"
            " WHERE status = 'trading' AND NOT (venue = ANY(%s)) GROUP BY instrument_class ORDER BY n DESC",
            (reference,),
        ).fetchall()
        summary["by_class"] = {str(r["instrument_class"]): int(r["n"]) for r in by_class}
        summary["dangling_aliases"] = len(self.dangling_seed_aliases())
        return summary

    def unmatched_provider_tags(self, *, since_ms: int, limit: int = 50) -> list[dict[str, Any]]:
        """Provider coin tags that resolve to nothing in the universe, by volume — the evidence an alias is missing.

        Reads `news_items` because that is where the tags are; both tables live in the News store and this is a
        read-only operator report, never part of the message path. Reference rows count as resolved here — a tag
        the US directory recognises is not a missing alias — so since #91 the residue is two things the report
        does not try to tell apart: word collisions (`PRIME`, `GPU`, `SPOT`) and coins on venues we do not
        snapshot (`OKB`, `HTX`).
        """

        rows = self.conn.execute(
            """
            WITH tags AS (
              SELECT upper(regexp_replace(c ->> 'symbol', '^XYZ-', '')) AS symbol,
                     CASE WHEN c ->> 'score' ~ '^[0-9]+(\\.[0-9]+)?$' THEN (c ->> 'score')::numeric END AS score
              FROM news_items i, jsonb_array_elements(i.provider_metadata -> 'coins') c
              WHERE i.published_at_ms >= %s AND c ->> 'symbol' <> ''
            ), resolved AS (
              SELECT t.symbol, t.score, coalesce(a.base_symbol, t.symbol) AS base
              FROM tags t LEFT JOIN news_symbol_aliases a ON a.alias = t.symbol
            )
            SELECT symbol, count(*) AS tags, max(score) AS max_score
            FROM resolved r
            WHERE NOT EXISTS (
              SELECT 1 FROM news_market_instruments m WHERE m.base_symbol = r.base AND m.status = 'trading'
            )
            GROUP BY symbol
            ORDER BY count(*) DESC, symbol
            LIMIT %s
            """,
            (int(since_ms), int(limit)),
        ).fetchall()
        return [
            {
                "symbol": str(r["symbol"]),
                "tags": int(r["tags"]),
                "max_score": float(r["max_score"]) if r["max_score"] is not None else None,
            }
            for r in rows
        ]

    def all_instruments(self) -> tuple[Instrument, ...]:
        rows = self.conn.execute(
            "SELECT venue, venue_symbol, base_symbol, instrument_class, quote_asset, status"
            " FROM news_market_instruments"
        ).fetchall()
        return instruments_from_rows(dict(row) for row in rows)

    # --------------------------------------------------------------- writes

    def reconcile_seed_aliases(self, *, now_ms: int, seeds: Mapping[str, str] = ALIAS_SEEDS) -> dict[str, int]:
        """Make the `source = 'seed'` rows equal to `ALIAS_SEEDS`: upsert what the code carries, delete what it
        dropped. Venue-derived rows are untouched.

        The previous `ON CONFLICT DO NOTHING` made the code seeds write-once: correcting `1810.HK -> XIAOMI` in the
        source would never have reached a deployed database (#89). Seeds are code-owned and rebuildable, so the
        code wins; a future operator-owned alias would carry its own `source` and survive this.
        """

        written = 0
        for alias, base in seeds.items():
            cursor = self.conn.execute(
                """
                INSERT INTO news_symbol_aliases (alias, base_symbol, source, updated_at_ms)
                VALUES (%s, %s, 'seed', %s)
                ON CONFLICT (alias) DO UPDATE
                   SET base_symbol = EXCLUDED.base_symbol, updated_at_ms = EXCLUDED.updated_at_ms
                 WHERE news_symbol_aliases.source = 'seed'
                   AND news_symbol_aliases.base_symbol IS DISTINCT FROM EXCLUDED.base_symbol
                """,
                (alias.upper(), str(base).upper(), int(now_ms)),
            )
            written += int(getattr(cursor, "rowcount", 0) or 0)
        cursor = self.conn.execute(
            "DELETE FROM news_symbol_aliases WHERE source = 'seed' AND NOT (alias = ANY(%s))",
            ([alias.upper() for alias in seeds],),
        )
        return {"written": written, "removed": int(getattr(cursor, "rowcount", 0) or 0)}

    def apply_snapshot(self, instruments: Sequence[Instrument], *, now_ms: int) -> SnapshotResult:
        """Upsert one snapshot and reconcile the venues that answered.

        Venues absent from ``instruments`` are left untouched — a venue that failed to answer must not read as a
        mass delisting. Only symbols missing from a venue that *did* answer are marked delisted.
        """

        answered = {i.venue for i in instruments}
        previous = tuple(i for i in self.all_instruments() if i.venue in answered and i.status == "trading")

        for item in instruments:
            self.conn.execute(
                """
                INSERT INTO news_market_instruments (
                  venue, venue_symbol, base_symbol, instrument_class, quote_asset, status, last_seen_ms
                ) VALUES (%s, %s, %s, %s, %s, 'trading', %s)
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
        return SnapshotResult(total=len(instruments), venues=tuple(sorted(answered)), delisted=len(gone))

    def learn_aliases_from_universe(self, *, now_ms: int) -> int:
        """Record the venue-derived aliases the pipeline will meet: the provider's ``XYZ-`` form and each
        ``dex:SYMBOL`` venue symbol, both pointing at their base. Cheap, and it keeps `alias_map()` self-contained."""

        rows = self.conn.execute(
            # Only rows that can produce an alias: the `XYZ-` form exists for hl.xyz bases and the `dex:SYMBOL`
            # form for dex-qualified venue symbols. Without the filter this walked all 13k reference rows too.
            "SELECT DISTINCT venue, venue_symbol, base_symbol FROM news_market_instruments"
            " WHERE status = 'trading' AND (venue = 'hl.xyz' OR venue_symbol LIKE '%:%')"
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


__all__ = ["InstrumentsRepository", "SnapshotResult"]
