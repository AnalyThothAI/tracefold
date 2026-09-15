"""Latest quote snapshots and deterministic Event Reaction persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

# S608 exemptions below interpolate only the module-owned venue-priority expression; all values stay bound.
from ..oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from ..row_values import optional_float, optional_int
from .instruments import REFERENCE_VENUES, normalize_symbol
from .pricing import (
    QUOTE_SOURCE_GROUP_MAX,
    QUOTE_TARGET_MAX,
    REACTION_METRIC_VERSION,
    PriceInstrument,
    Quote,
    QuoteRequest,
    change_basis_zh,
    price_kind_zh,
    quote_asset_rank_sql,
    quote_freshness,
    quote_state_zh,
    reference_freshness,
    source_rank_sql,
)
from .projections import (
    _aggregate_public,
    _directory_only_quote,
    _reaction_public,
    _unavailable_quote,
    _unlisted_quote,
)

# The due-work scan, named so the query audit plans the statement this method executes rather than a
# hand-copied look-alike of it (#589 L-F1).
DUE_REACTIONS_SQL: Final = """
            SELECT a.event_id, a.symbol, a.opened_at_ms AS anchor_at_ms,
                   r.state, r.venue, r.venue_symbol, r.instrument_class,
                   r.p0, r.p0_at_ms, r.p1, r.p1_at_ms,
                   -- Whether the model called this asset a primary, read once here and stored on the row:
                   -- the review's event-level sample is the median over primaries, and re-deriving it from
                   -- verdict JSONB per request does not fit the 720 h budget (#88 §14).
                   COALESCE((
                     SELECT bool_or(replace(upper(x ->> 'symbol'), 'XYZ-', '') = a.symbol)
                       FROM (
                         SELECT v.verdict FROM news_verdicts v
                         WHERE v.event_id = a.event_id AND v.stage = 'triage'
                           AND v.judgment_contract_version = 'news_judgment_v2'
                          ORDER BY v.created_at_ms DESC LIMIT 1
                       ) t, LATERAL jsonb_array_elements(COALESCE(t.verdict -> 'assets', '[]'::jsonb)) x
                      WHERE x ->> 'role' = 'primary'
                   ), false) AS is_primary,
                   -- Which market this Event says the symbol is (#651 §6.2). `news_event_assets` is the
                   -- Gate's tag ledger and carries no market; the judgment does, and it is the authority on
                   -- what the Event is about. A primary's market wins over a mention of the same symbol; a
                   -- verdict written before #651 says nothing here and reads as `unknown`, which resolves
                   -- exactly as it did before.
                   (
                     SELECT x ->> 'market_type'
                       FROM (
                         SELECT v.verdict FROM news_verdicts v
                         WHERE v.event_id = a.event_id AND v.stage = 'triage'
                           AND v.judgment_contract_version = 'news_judgment_v2'
                          ORDER BY v.created_at_ms DESC LIMIT 1
                       ) t, LATERAL jsonb_array_elements(COALESCE(t.verdict -> 'assets', '[]'::jsonb)) x
                      WHERE replace(upper(x ->> 'symbol'), 'XYZ-', '') = a.symbol
                        AND jsonb_typeof(x -> 'market_type') = 'string'
                      ORDER BY (x ->> 'role' = 'primary') DESC
                      LIMIT 1
                   ) AS market_type
              FROM news_event_assets a
              JOIN news_events e ON e.event_id = a.event_id AND e.ingest_mode = 'live'
              -- A lateral probe on the primary key, not a hash join: the scan walks Event-assets oldest
              -- first and stops at the limit, so it must never read the whole Reaction table to do it.
              LEFT JOIN LATERAL (
                SELECT r.state, r.venue, r.venue_symbol, r.instrument_class,
                       r.p0, r.p0_at_ms, r.p1, r.p1_at_ms, r.unavailable_reason
                  FROM news_event_reactions r
                 WHERE r.event_id = a.event_id AND r.symbol = a.symbol AND r.metric_version = %s
              ) r ON true
             WHERE a.opened_at_ms <= %s
               AND (r.state IS NULL OR r.state IN ('pending', 'partial'))
               AND (
                 r.state IS DISTINCT FROM 'partial'
                 OR (a.opened_at_ms <= %s AND r.unavailable_reason IS NULL)
               )
             ORDER BY a.opened_at_ms
             LIMIT %s
"""


class QuoteStorage:
    conn: Any

    def resolve_instruments(self, requests: Sequence[QuoteRequest]) -> dict[str, PriceInstrument]:
        """Raw tag -> the one contract its price comes from, for a bounded batch of typed questions.

        Exact-symbol-first: a symbol that is itself tradeable is never resolved through an issuer alias, so
        `SKHX` prices SKHX even though storyline identity normalizes it to `SKHY` (#88 §3). The alias is
        the fallback for tags that name nothing on their own. Reference-only tiers (`us.listed`) are never
        priceable — they answer "does this ticker exist", not "what does it cost".

        Typed since #651 §6.2: a request naming a market resolves only to contracts of that market, so an
        equity question can no longer be answered with a same-name coin. Keyed by the caller's raw symbol
        so no caller needs normalization knowledge of its own.
        """

        candidates, _ = self._priceable(requests)
        return {symbol: rows[0] for symbol, rows in candidates.items() if rows}

    def instruments_for_symbols(self, requests: Sequence[QuoteRequest]) -> dict[str, tuple[PriceInstrument, ...]]:
        """Every priceable contract that answers each typed question, in the code-owned venue order.

        Delivery uses this larger view to fail over an entire price calculation from Binance to Hyperliquid
        and then OKX. Exact tradeable tags still suppress issuer-alias candidates, preserving the same
        SKHX/SKHY identity rule as :meth:`resolve_instruments`.
        """

        return self._priceable(requests)[0]

    def _priceable(
        self, requests: Sequence[QuoteRequest]
    ) -> tuple[dict[str, tuple[PriceInstrument, ...]], frozenset[str]]:
        """Resolution, once: the priceable contracts per request, and the requests only a directory knows.

        The second half is what keeps `unlisted` and `unavailable` different answers for a typed question.
        `V/equity` is a real ticker the `us.listed` directory carries and no venue we poll prices, so the
        honest answer is "we cannot quote it", not "no such symbol" — and certainly not the `V` coin. A
        request whose market the catalogue carries nowhere stays `unlisted`.
        """

        normalized = {
            str(request.symbol): (normalize_symbol(request.symbol), request)
            for request in requests
            if str(request.symbol).strip()
        }
        wanted = sorted({symbol for symbol, _ in normalized.values() if symbol})
        if not wanted:
            return {}, frozenset()
        alias_rows = self.conn.execute(
            "SELECT alias, base_symbol FROM news_symbol_aliases WHERE alias = ANY(%s)", (wanted,)
        ).fetchall()
        aliases = {str(row["alias"]): str(row["base_symbol"]) for row in alias_rows}
        bases = sorted({*wanted, *(aliases.get(symbol, symbol) for symbol in wanted)})
        # Both tiers in one statement. Reference rows are never returned as price candidates; they are read
        # so that a typed miss can say which of the two misses it is.
        rows = self.conn.execute(
            f"""
            SELECT venue, venue_symbol, base_symbol, instrument_class, quote_asset
              FROM news_market_instruments i
             WHERE i.status = 'trading'
               AND i.base_symbol = ANY(%s)
             ORDER BY i.base_symbol, {source_rank_sql()}, {quote_asset_rank_sql()}, i.venue, i.venue_symbol
            """,  # noqa: S608
            (bases,),
        ).fetchall()
        grouped: dict[str, list[PriceInstrument]] = {}
        reference: dict[str, set[str]] = {}
        for row in rows:
            base = str(row["base_symbol"])
            if str(row["venue"]) in REFERENCE_VENUES:
                reference.setdefault(base, set()).add(str(row["instrument_class"]))
                continue
            grouped.setdefault(base, []).append(
                PriceInstrument(
                    venue=str(row["venue"]),
                    venue_symbol=str(row["venue_symbol"]),
                    base_symbol=base,
                    instrument_class=str(row["instrument_class"]),
                    quote_asset=str(row["quote_asset"]) if row["quote_asset"] else None,
                )
            )
        result: dict[str, tuple[PriceInstrument, ...]] = {}
        directory_only: set[str] = set()
        for raw, (symbol, request) in normalized.items():
            base = symbol if symbol in grouped or symbol in reference else aliases.get(symbol, symbol)
            candidates = tuple(
                instrument for instrument in grouped.get(base, ()) if request.accepts(instrument.instrument_class)
            )
            if candidates:
                result[raw] = candidates
                continue
            if any(request.accepts(name) for name in reference.get(base, ())):
                directory_only.add(raw)
        return result, frozenset(directory_only)

    def quote_target_symbols(self, *, since_ms: int, limit: int = 1000) -> list[str]:
        """Code-verified assets on recent live Events, most recently observed first.

        One symbol per row however many Events carried it: a hundred BTC Events are one Quote target, and the
        provider work that follows is `O(source groups)`, never `O(Events x assets)` (#88 §13). OI rows join
        the same bounded working set through the ledger and the Item that produced them -- they have no Event
        to reach for and never had a Gate-grounded asset tag (#553).
        """

        rows = self.conn.execute(
            """
            WITH candidates AS (
              SELECT a.symbol, a.opened_at_ms AS observed_at_ms
                FROM news_event_assets a
                JOIN news_events e ON e.event_id = a.event_id AND e.ingest_mode = 'live'
               WHERE a.opened_at_ms >= %s
              UNION ALL
              SELECT o.symbol, o.observed_at_ms
                FROM news_oi_signals o
                JOIN news_items i ON i.item_id = o.source_item_id AND i.first_ingest_mode = 'live'
               WHERE o.observed_at_ms >= %s AND NOT o.historical
                 AND o.metric_version = %s
            )
            SELECT symbol, max(observed_at_ms) AS last_ms
              FROM candidates
             GROUP BY symbol
             ORDER BY last_ms DESC
             LIMIT %s
            """,
            (int(since_ms), int(since_ms), OI_METRIC_VERSION, int(limit)),
        ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def plan_quote_targets(self, *, since_ms: int, watchlist: Sequence[str] = ()) -> dict[str, Any]:
        """The bounded working set for one Quote turn, plus the planner's own arithmetic as a result field.

        Watchlist symbols win, then the most recently opened Events. Deduplication happens three times over —
        by normalized symbol, by resolved instrument, and by source group — so repetition in the feed cannot
        multiply provider calls.
        """

        watch = [str(symbol).upper() for symbol in watchlist if str(symbol).strip()]
        recent = self.quote_target_symbols(since_ms=since_ms)
        ordered: list[str] = []
        seen_symbols: set[str] = set()
        for symbol in [*watch, *recent]:
            key = normalize_symbol(symbol)
            if key and key not in seen_symbols:
                seen_symbols.add(key)
                ordered.append(symbol)
        # The planner asks the untyped question on purpose: it decides which contracts to *poll*, and
        # polling a superset costs one provider call, while typing it here would starve a later typed
        # question of its snapshot. Typing happens where a price is claimed about an Event.
        resolved = self.resolve_instruments([QuoteRequest(symbol) for symbol in ordered])
        targets: list[PriceInstrument] = []
        seen_instruments: set[tuple[str, str, str]] = set()
        groups: list[str] = []
        for symbol in ordered:
            instrument = resolved.get(symbol)
            if instrument is None:
                continue
            instrument_key = (instrument.venue, instrument.venue_symbol, instrument.price_kind)
            if instrument_key in seen_instruments:
                continue
            if instrument.source_key not in groups:
                if len(groups) >= QUOTE_SOURCE_GROUP_MAX:
                    continue  # outside the bounded working set: truthfully unavailable until it enters
                groups.append(instrument.source_key)
            if len(targets) >= QUOTE_TARGET_MAX:
                break
            seen_instruments.add(instrument_key)
            targets.append(instrument)
        return {
            "targets": targets,
            "input_symbol_count": len(watch) + len(recent),
            "unique_symbol_count": len(seen_symbols),
            "unique_instrument_count": len(targets),
            "source_group_count": len(groups),
            "dedupe_ratio": round((len(watch) + len(recent)) / len(targets), 2) if targets else 0.0,
        }

    def replace_source_snapshot(
        self,
        *,
        source_key: str,
        quotes: Sequence[Quote],
        target_count: int,
        source_at_ms: int | None,
        received_at_ms: int,
        now_ms: int,
    ) -> None:
        """One successful source replaces its own row. A failed source is simply not called here."""

        payload = {f"{quote.venue_symbol}|{quote.price_kind}": quote.as_entry() for quote in quotes}
        encoded = _dumps(payload)
        self.conn.execute(
            """
            INSERT INTO news_quote_snapshots
              (source_key, quotes, target_count, payload_sha256, source_at_ms, received_at_ms, updated_at_ms)
            VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s)
            ON CONFLICT (source_key) DO UPDATE SET
              quotes = EXCLUDED.quotes,
              target_count = EXCLUDED.target_count,
              payload_sha256 = EXCLUDED.payload_sha256,
              source_at_ms = EXCLUDED.source_at_ms,
              received_at_ms = EXCLUDED.received_at_ms,
              updated_at_ms = EXCLUDED.updated_at_ms
            """,
            (
                str(source_key),
                encoded,
                int(target_count),
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                None if source_at_ms is None else int(source_at_ms),
                int(received_at_ms),
                int(now_ms),
            ),
        )

    def forget_sources_except(self, source_keys: Sequence[str]) -> int:
        """Drop snapshot rows for sources the planner no longer targets; keep every planned one."""

        if not source_keys:
            return 0
        cursor = self.conn.execute(
            "DELETE FROM news_quote_snapshots WHERE NOT (source_key = ANY(%s))", (list(source_keys),)
        )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def quote_snapshots(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT source_key, quotes, target_count, source_at_ms, received_at_ms, updated_at_ms"
            " FROM news_quote_snapshots"
        ).fetchall()
        return {str(row["source_key"]): dict(row) for row in rows}

    def quotes_for_symbols(self, requests: Sequence[QuoteRequest], *, now_ms: int) -> list[dict[str, Any]]:
        """One result per requested instrument, in request order, each naming its own state.

        `unlisted` and `unavailable` are different answers: the first says the catalogue holds nothing that
        answers this question, the second says we have not managed to quote it yet. A typed equity question
        the `us.listed` directory answers is `unavailable` — the ticker exists and we poll no price source
        for it — never the same-name coin, and never a price of zero (#651 §6.2).
        """

        requested: list[QuoteRequest] = []
        seen: set[tuple[str, str]] = set()
        for request in requests:
            text = str(request.symbol).strip()
            key = (text, request.market_type)
            if text and key not in seen:
                seen.add(key)
                requested.append(QuoteRequest(text, request.market_type))
        if not requested:
            return []
        candidates, directory_only = self._priceable(requested)
        instruments = {symbol: rows[0] for symbol, rows in candidates.items() if rows}
        snapshots = self.quote_snapshots() if instruments else {}
        out: list[dict[str, Any]] = []
        for request in requested:
            symbol = request.symbol
            instrument = instruments.get(symbol)
            if instrument is None:
                out.append(
                    _directory_only_quote(symbol, request.market_type)
                    if symbol in directory_only
                    else _unlisted_quote(symbol)
                )
                continue
            snapshot = snapshots.get(instrument.source_key)
            entry = (snapshot or {}).get("quotes", {}).get(f"{instrument.venue_symbol}|{instrument.price_kind}")
            if not snapshot or not isinstance(entry, Mapping):
                out.append(_unavailable_quote(symbol, instrument))
                continue
            received_at_ms = int(snapshot["received_at_ms"])
            source_at_ms = optional_int(entry.get("source_at_ms"))
            freshness = quote_freshness(
                measured_at_ms=now_ms,
                received_at_ms=received_at_ms,
                source_at_ms=source_at_ms,
            )
            reference_at_ms = optional_int(entry.get("reference_at_ms"))
            reference_age_ms, reference_is_fresh = reference_freshness(
                measured_at_ms=now_ms,
                reference_at_ms=reference_at_ms,
            )
            out.append(
                {
                    "requested_symbol": symbol,
                    "symbol": instrument.base_symbol,
                    "base_symbol": instrument.base_symbol,
                    "venue": instrument.venue,
                    "venue_symbol": instrument.venue_symbol,
                    "instrument_class": instrument.instrument_class,
                    "quote_asset": entry.get("quote_asset") or instrument.quote_asset,
                    "price": str(entry.get("price")),
                    "price_kind": str(entry.get("price_kind") or instrument.price_kind),
                    "price_kind_zh": price_kind_zh(entry.get("price_kind") or instrument.price_kind),
                    "change_pct": optional_float(entry.get("change_pct")) if reference_is_fresh else None,
                    "change_basis": entry.get("change_basis"),
                    "change_basis_zh": change_basis_zh(entry.get("change_basis")),
                    "source_at_ms": source_at_ms,
                    "received_at_ms": received_at_ms,
                    "received_age_ms": freshness.received_age_ms,
                    "source_age_ms": freshness.source_age_ms,
                    "effective_age_ms": freshness.effective_age_ms,
                    "freshness_basis": freshness.freshness_basis,
                    "reference_at_ms": reference_at_ms,
                    "reference_age_ms": reference_age_ms,
                    "state": freshness.state,
                    "state_zh": quote_state_zh(freshness.state),
                }
            )
        return out

    def due_reactions(self, *, now_ms: int, limit: int) -> list[dict[str, Any]]:
        """Event-assets whose next horizon is due, oldest first, bounded.

        The durable source of due work is PostgreSQL, not an in-memory queue: a restart loses nothing and a
        turn that cannot plan every row leaves the rest for the next one. Acquisition covers every live
        grounded Event, not only the delivered ones — a Reaction the reader never received is exactly what
        the potential-miss review needs (#88 §6).
        """

        stamp = int(now_ms)
        rows = self.conn.execute(
            DUE_REACTIONS_SQL,
            (REACTION_METRIC_VERSION, stamp - 3_600_000, stamp - 14_400_000, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def oldest_due_age_ms(self, *, now_ms: int, history_max_age_ms: int) -> int:
        """Backlog SLO input: how late the latest-running due Event-asset is against *its own* horizon.

        Lateness is measured per row against the horizon that row is waiting for — anchor+1H for one that has
        no price points yet, anchor+4H for a partial one. Measuring everything against the 1H horizon made a
        perfectly healthy system report ~180 min the moment any row became 4H-due, which would have pinned
        the ≤5 min / 15 min SLO permanently at "warning" and taught the operator to ignore it.
        """

        stamp = int(now_ms)
        row = self.conn.execute(
            """
            SELECT max(
                     CASE WHEN r.state = 'partial' THEN %s - (a.opened_at_ms + 14400000)
                          ELSE %s - (a.opened_at_ms + 3600000) END
                   ) AS lateness
              FROM news_event_assets a
              JOIN news_events e ON e.event_id = a.event_id AND e.ingest_mode = 'live'
              LEFT JOIN news_event_reactions r
                ON r.event_id = a.event_id AND r.symbol = a.symbol AND r.metric_version = %s
             WHERE a.opened_at_ms <= %s
               AND a.opened_at_ms >= %s
               AND (r.state IS NULL OR r.state IN ('pending', 'partial'))
               AND (
                 r.state IS DISTINCT FROM 'partial'
                 OR (a.opened_at_ms <= %s AND r.unavailable_reason IS NULL)
               )
            """,
            (
                stamp,
                stamp,
                REACTION_METRIC_VERSION,
                stamp - 3_600_000,
                stamp - int(history_max_age_ms),
                stamp - 14_400_000,
            ),
        ).fetchone()
        lateness = (row or {}).get("lateness")
        return 0 if lateness is None else max(0, int(lateness))

    def upsert_reaction(self, row: Mapping[str, Any], *, now_ms: int) -> None:
        """Idempotent by `(event_id, symbol, metric_version)`; a replay writes the same row again."""

        self.conn.execute(
            """
            INSERT INTO news_event_reactions
              (event_id, symbol, metric_version, venue, venue_symbol, instrument_class, anchor_at_ms,
               p0, p0_at_ms, p1, p1_at_ms, p4, p4_at_ms, return_1h_bps, return_4h_bps,
               is_primary, state, unavailable_reason, created_at_ms, updated_at_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id, symbol, metric_version) DO UPDATE SET
              venue = EXCLUDED.venue,
              venue_symbol = EXCLUDED.venue_symbol,
              instrument_class = EXCLUDED.instrument_class,
              p0 = COALESCE(news_event_reactions.p0, EXCLUDED.p0),
              p0_at_ms = COALESCE(news_event_reactions.p0_at_ms, EXCLUDED.p0_at_ms),
              p1 = COALESCE(news_event_reactions.p1, EXCLUDED.p1),
              p1_at_ms = COALESCE(news_event_reactions.p1_at_ms, EXCLUDED.p1_at_ms),
              p4 = COALESCE(news_event_reactions.p4, EXCLUDED.p4),
              p4_at_ms = COALESCE(news_event_reactions.p4_at_ms, EXCLUDED.p4_at_ms),
              return_1h_bps = COALESCE(news_event_reactions.return_1h_bps, EXCLUDED.return_1h_bps),
              return_4h_bps = COALESCE(news_event_reactions.return_4h_bps, EXCLUDED.return_4h_bps),
              is_primary = EXCLUDED.is_primary,
              state = EXCLUDED.state,
              unavailable_reason = EXCLUDED.unavailable_reason,
              updated_at_ms = EXCLUDED.updated_at_ms
            """,
            (
                str(row["event_id"]),
                str(row["symbol"]),
                REACTION_METRIC_VERSION,
                str(row.get("venue") or ""),
                str(row.get("venue_symbol") or ""),
                str(row.get("instrument_class") or "unknown"),
                int(row["anchor_at_ms"]),
                row.get("p0"),
                optional_int(row.get("p0_at_ms")),
                row.get("p1"),
                optional_int(row.get("p1_at_ms")),
                row.get("p4"),
                optional_int(row.get("p4_at_ms")),
                optional_int(row.get("return_1h_bps")),
                optional_int(row.get("return_4h_bps")),
                bool(row.get("is_primary")),
                str(row["state"]),
                row.get("unavailable_reason"),
                int(now_ms),
                int(now_ms),
            ),
        )

    def event_reactions(self, event_id: str) -> list[dict[str, Any]]:
        """Every per-asset Reaction for one Event, with the raw closes the returns were computed from."""

        rows = self.conn.execute(
            """
            SELECT symbol, metric_version, venue, venue_symbol, instrument_class, anchor_at_ms,
                   p0, p0_at_ms, p1, p1_at_ms, p4, p4_at_ms, return_1h_bps, return_4h_bps,
                   is_primary, state, unavailable_reason, updated_at_ms
              FROM news_event_reactions
             WHERE event_id = %s
             ORDER BY symbol, metric_version
            """,
            (str(event_id),),
        ).fetchall()
        return [_reaction_public(dict(row)) for row in rows]

    def event_reaction_aggregates(self, event_ids: Sequence[str], *, now_ms: int) -> dict[str, dict[str, Any]]:
        """The compact event-level 1H/4H aggregate for a bounded batch of Events, for the feed.

        One Event contributes one sample however many assets it mentions: the aggregate is the median signed
        return of the Triage *primaries* that resolve to a contract. An Event whose primaries price nothing
        has no aggregate — the per-asset rows stay inspectable on the detail page either way (#88 §6).
        """

        wanted = [str(event_id) for event_id in event_ids if str(event_id).strip()]
        if not wanted:
            return {}
        rows = self.conn.execute(
            """
            WITH wanted AS (SELECT unnest(%s::text[]) AS event_id),
            prim AS (
              -- Upper-case *then* strip, exactly like the Deduper writes `news_event_assets.symbol`
              -- (`repository.insert_event`). Doing it the other way leaves a model-authored `xyz-btc` as
              -- `XYZ-BTC` here and `BTC` there, and the join silently finds nothing.
              SELECT w.event_id, replace(upper(x ->> 'symbol'), 'XYZ-', '') AS symbol
                FROM wanted w
                JOIN LATERAL (
                  SELECT v.verdict FROM news_verdicts v
                   WHERE v.event_id = w.event_id AND v.stage = 'triage'
                     AND v.judgment_contract_version = 'news_judgment_v2'
                   ORDER BY v.created_at_ms DESC LIMIT 1
                ) t ON true,
                LATERAL jsonb_array_elements(COALESCE(t.verdict -> 'assets', '[]'::jsonb)) x
               WHERE x ->> 'role' = 'primary'
            )
            SELECT p.event_id,
                   min(e.opened_at_ms) AS anchor_at_ms,
                   count(*) AS primary_n,
                   count(r.event_id) AS row_n,
                   count(*) FILTER (WHERE r.state = 'unavailable') AS unavailable_n,
                   count(*) FILTER (WHERE r.return_1h_bps IS NOT NULL) AS priced_1h,
                   count(*) FILTER (WHERE r.return_4h_bps IS NOT NULL) AS priced_4h,
                   min(r.unavailable_reason) AS unavailable_reason,
                   array_remove(array_agg(r.p0), NULL) AS p0s,
                   array_remove(array_agg(r.return_1h_bps), NULL) AS bps_1h,
                   array_remove(array_agg(r.return_4h_bps), NULL) AS bps_4h
              FROM prim p
              JOIN news_events e ON e.event_id = p.event_id
              LEFT JOIN news_event_reactions r
                ON r.event_id = p.event_id AND r.symbol = p.symbol AND r.metric_version = %s
             GROUP BY p.event_id
            """,
            (wanted, REACTION_METRIC_VERSION),
        ).fetchall()
        return {str(row["event_id"]): _aggregate_public(dict(row), now_ms=int(now_ms)) for row in rows}


_JSON_SEPARATORS = (",", ":")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=_JSON_SEPARATORS, sort_keys=True)
