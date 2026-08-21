"""Price Review repository (#88): Quote Snapshots, Event Reactions, and the bounded review aggregates.

Every read is bounded by an explicit window or an explicit id list. Every write is idempotent by key:
`news_quote_snapshots(source_key)` is last-value-wins, `news_event_reactions(event_id, symbol,
metric_version)` is a versioned deterministic row that a replay rewrites identically.

Callers own the transaction (`worker_session` / `api_session`), exactly like `NewsRepository`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from .instruments import REFERENCE_VENUES, normalize_symbol
from .outcome import decision_zh, direction_zh, event_type_zh, magnitude_zh, override_rule_zh, throttled_by_zh
from .pricing import (
    QUOTE_SOURCE_GROUP_MAX,
    QUOTE_TARGET_MAX,
    REACTION_HISTORY_MAX_AGE_MS,
    REACTION_METRIC_VERSION,
    REVIEW_POTENTIAL_MISS_LIMIT,
    PriceInstrument,
    Quote,
    change_basis_zh,
    coverage_pct,
    horizon_zh,
    median_bps,
    price_kind_for,
    price_kind_zh,
    quote_asset_rank_sql,
    quote_state,
    quote_state_zh,
    reaction_reason_zh,
    reaction_state_zh,
    source_rank_sql,
)
from .similarity import similarity

_JSON_SEPARATORS = (",", ":")
_REVIEW_DISCOVERY_MAX_HOURS: Final = 168

# The latest Triage verdict per Event, and the Events a review window covers. Both sections below start here
# so "which Event counts" is written once.
_REVIEW_FACTS_CTE: Final = """
    -- One flat pass, deliberately. Two things make this query fast enough to live under Serve's one-second
    -- statement timeout at the 720 h bound (#88 §14): the latest verdict per Event is picked in the same scan
    -- that filters the window, so the planner estimates from table statistics instead of guessing 250 rows
    -- for a CTE chain and choosing nested loops; and the sort carries only what the aggregates read — the
    -- long text an Event needs on screen is fetched afterwards for the fifty rows that reach the page.
    ev AS (
      SELECT DISTINCT ON (v.event_id)
             v.event_id, e.opened_at_ms,
             e.opened_at_ms <= %s - 3600000 AS mature_1h,
             e.opened_at_ms <= %s - 14400000 AS mature_4h,
             v.final_decision, v.degraded, v.override_rule, v.throttled_by,
             v.verdict ->> 'direction' AS direction,
             COALESCE((v.verdict ->> 'magnitude')::int, 0) AS magnitude,
             COALESCE(v.verdict ->> 'event_type', 'other') AS event_type,
             (d.state = 'sent') AS delivered
        FROM news_verdicts v
        JOIN news_events e ON e.event_id = v.event_id
        LEFT JOIN news_deliveries d ON d.event_id = v.event_id AND d.kind = 'first'
       WHERE v.stage = 'triage' AND e.ingest_mode = 'live'
         AND e.opened_at_ms >= %s AND e.opened_at_ms < %s
         {cohort_filter}
       ORDER BY v.event_id, v.created_at_ms DESC
    ),
    agg AS (
      SELECT r.event_id,
             count(*) AS asset_n,
             count(r.return_1h_bps) AS priced_1h,
             count(r.return_4h_bps) AS priced_4h,
             count(*) FILTER (WHERE r.state = 'unavailable') AS unavailable_n,
             min(r.unavailable_reason) AS unavailable_reason,
             (array_agg(r.return_1h_bps ORDER BY r.return_1h_bps)
                FILTER (WHERE r.return_1h_bps IS NOT NULL AND r.anchor_at_ms >= %s))[
                  (count(r.return_1h_bps) FILTER (WHERE r.anchor_at_ms >= %s) + 1) / 2
                ] AS bps_1h,
             NULL::integer AS bps_4h
        FROM news_event_reactions r
       WHERE r.metric_version = %s AND r.is_primary
         AND r.anchor_at_ms >= %s AND r.anchor_at_ms < %s
       GROUP BY r.event_id
    ),
    fact AS (
      SELECT ev.event_id, ev.opened_at_ms, ev.mature_1h, ev.mature_4h,
             ev.final_decision, ev.degraded, ev.override_rule, ev.throttled_by,
             ev.direction, ev.magnitude, ev.event_type, ev.delivered,
             a.event_id IS NOT NULL AS has_primary,
             COALESCE(a.asset_n, 0) AS asset_n,
             COALESCE(a.priced_1h, 0) AS priced_1h,
             COALESCE(a.priced_4h, 0) AS priced_4h,
             a.unavailable_reason, a.bps_1h, a.bps_4h
        FROM ev LEFT JOIN agg a ON a.event_id = ev.event_id
    )
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=_JSON_SEPARATORS, sort_keys=True)


class PriceRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ instrument resolution
    def resolve_instruments(self, symbols: Iterable[str]) -> dict[str, PriceInstrument]:
        """Raw provider tag -> the one contract its price comes from, for a bounded batch.

        Exact-symbol-first: a symbol that is itself tradeable is never resolved through an issuer alias, so
        `SKHX` prices SKHX even though storyline identity normalizes it to `SKHY` (#88 §3). The alias is
        the fallback for tags that name nothing on their own. Reference-only tiers (`us.listed`) are excluded
        here rather than filtered later — they answer "does this ticker exist", not "what does it cost".

        Keyed by the caller's raw symbol so no caller needs normalization knowledge of its own.
        """

        normalized = {str(symbol): normalize_symbol(symbol) for symbol in symbols if str(symbol).strip()}
        wanted = sorted({value for value in normalized.values() if value})
        if not wanted:
            return {}
        rows = self.conn.execute(
            f"""
            SELECT s.symbol AS requested, m.venue, m.venue_symbol, m.base_symbol, m.instrument_class,
                   m.quote_asset
              FROM unnest(%s::text[]) AS s(symbol)
              LEFT JOIN news_symbol_aliases a ON a.alias = s.symbol
              LEFT JOIN LATERAL (
                SELECT i.venue, i.venue_symbol, i.base_symbol, i.instrument_class, i.quote_asset
                  FROM news_market_instruments i
                 WHERE i.status = 'trading'
                   AND NOT (i.venue = ANY(%s))
                   AND i.base_symbol IN (s.symbol, COALESCE(a.base_symbol, s.symbol))
                 ORDER BY (i.base_symbol = s.symbol) DESC,
                          {source_rank_sql()}, {quote_asset_rank_sql()}, i.venue, i.venue_symbol
                 LIMIT 1
              ) m ON true
             WHERE m.venue IS NOT NULL
            """,
            (wanted, sorted(REFERENCE_VENUES)),
        ).fetchall()
        resolved = {
            str(row["requested"]): PriceInstrument(
                venue=str(row["venue"]),
                venue_symbol=str(row["venue_symbol"]),
                base_symbol=str(row["base_symbol"]),
                instrument_class=str(row["instrument_class"]),
                quote_asset=str(row["quote_asset"]) if row["quote_asset"] else None,
            )
            for row in rows
        }
        return {raw: resolved[norm] for raw, norm in normalized.items() if norm in resolved}

    # ------------------------------------------------------------------ quote targets
    def quote_target_symbols(self, *, since_ms: int, limit: int = 1000) -> list[str]:
        """Exact grounded assets on recent live Events, most recently opened first.

        One symbol per row however many Events carried it: a hundred BTC Events are one Quote target, and the
        provider work that follows is `O(source groups)`, never `O(Events x assets)` (#88 §13).
        """

        rows = self.conn.execute(
            """
            SELECT a.symbol, max(a.opened_at_ms) AS last_ms
              FROM news_event_assets a
              JOIN news_events e ON e.event_id = a.event_id AND e.ingest_mode = 'live'
             WHERE a.opened_at_ms >= %s
             GROUP BY a.symbol
             ORDER BY last_ms DESC
             LIMIT %s
            """,
            (int(since_ms), int(limit)),
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
        resolved = self.resolve_instruments(ordered)
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

    # ------------------------------------------------------------------ quote snapshots
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

    def quotes_for_symbols(self, symbols: Sequence[str], *, now_ms: int) -> list[dict[str, Any]]:
        """One result per requested symbol, in request order, each naming its own state.

        `unlisted` and `unavailable` are different answers: the first says no venue we poll lists this tag,
        the second says we have not managed to quote it yet. Neither is ever rendered as a price of zero.
        """

        requested: list[str] = []
        for symbol in symbols:
            text = str(symbol).strip()
            if text and text not in requested:
                requested.append(text)
        if not requested:
            return []
        instruments = self.resolve_instruments(requested)
        snapshots = self.quote_snapshots() if instruments else {}
        out: list[dict[str, Any]] = []
        for symbol in requested:
            instrument = instruments.get(symbol)
            if instrument is None:
                out.append(_unlisted_quote(symbol))
                continue
            snapshot = snapshots.get(instrument.source_key)
            entry = (snapshot or {}).get("quotes", {}).get(f"{instrument.venue_symbol}|{instrument.price_kind}")
            if not snapshot or not isinstance(entry, Mapping):
                out.append(_unavailable_quote(symbol, instrument))
                continue
            received_at_ms = int(snapshot["received_at_ms"])
            age_ms = max(0, int(now_ms) - received_at_ms)
            state = quote_state(age_ms)
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
                    "change_pct": _optional_float(entry.get("change_pct")),
                    "change_basis": entry.get("change_basis"),
                    "change_basis_zh": change_basis_zh(entry.get("change_basis")),
                    "source_at_ms": _optional_int(entry.get("source_at_ms")),
                    "received_at_ms": received_at_ms,
                    "age_ms": age_ms,
                    "state": state,
                    "state_zh": quote_state_zh(state),
                }
            )
        return out

    # ------------------------------------------------------------------ event reactions
    def due_reactions(self, *, now_ms: int, limit: int) -> list[dict[str, Any]]:
        """Event-assets whose next horizon is due, oldest first, bounded.

        The durable source of due work is PostgreSQL, not an in-memory queue: a restart loses nothing and a
        turn that cannot plan every row leaves the rest for the next one. Acquisition covers every live
        grounded Event, not only the delivered ones — a Reaction the reader never received is exactly what
        the potential-miss review needs (#88 §6).
        """

        stamp = int(now_ms)
        rows = self.conn.execute(
            """
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
                          ORDER BY v.created_at_ms DESC LIMIT 1
                       ) t, LATERAL jsonb_array_elements(COALESCE(t.verdict -> 'assets', '[]'::jsonb)) x
                      WHERE x ->> 'role' = 'primary'
                   ), false) AS is_primary
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
            """,
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
                _optional_int(row.get("p0_at_ms")),
                row.get("p1"),
                _optional_int(row.get("p1_at_ms")),
                row.get("p4"),
                _optional_int(row.get("p4_at_ms")),
                _optional_int(row.get("return_1h_bps")),
                _optional_int(row.get("return_4h_bps")),
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
             ORDER BY symbol
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

    # ------------------------------------------------------------------ review
    def review(
        self,
        *,
        hours: int,
        now_ms: int,
        cohort: tuple[str, str, str] | None = None,
    ) -> dict[str, Any]:
        """The whole 命中复盘 payload for one bounded window. Coverage first, then accuracy.

        One pass, not five. The shared fact set — every live Event in the window with its latest Triage
        verdict and its event-level aggregate — is expensive enough at the 720 h bound that re-deriving it per
        section cost 3.7 s against a 250 ms budget (#88 §14). PostgreSQL materializes a CTE referenced more
        than once, so the sections below read it instead of rebuilding it, and the whole page is one round
        trip that returns tens of rows rather than the window.
        """

        window_ms = int(hours) * 3_600_000
        start_ms, end_ms = int(now_ms) - window_ms, int(now_ms)
        discovery_start_ms = max(start_ms, end_ms - _REVIEW_DISCOVERY_MAX_HOURS * 3_600_000)
        # `ev` bounds the window, `agg` bounds the same window on the Reaction index.
        params: list[Any] = [end_ms, end_ms, start_ms, end_ms]
        cohort_filter = ""
        if cohort is not None:
            cohort_filter = (
                "AND COALESCE(v.prompt_version, '') = %s "
                "AND COALESCE(v.policy_version, '') = %s "
                "AND COALESCE(v.model, '') = %s"
            )
            params.extend(cohort)
        params.extend(
            (
                discovery_start_ms,
                discovery_start_ms,
                REACTION_METRIC_VERSION,
                start_ms,
                end_ms,
            )
        )
        sections = self._review_sections(tuple(params), cohort_filter=cohort_filter)
        coverage = _coverage_rows(sections["coverage"][0] if sections["coverage"] else {})
        return {
            "meta": {
                "hours": int(hours),
                "window_start_ms": start_ms,
                "window_end_ms": end_ms,
                "discovery_window_start_ms": discovery_start_ms,
                "metric_version": REACTION_METRIC_VERSION,
                "measured_at_ms": int(now_ms),
                "cohort": "/".join(cohort) if cohort else None,
            },
            "coverage": coverage,
            "directions": [],
            # Retired from the product surface in #112: these post-event price rankings were not causal
            # quality evidence, and their ordered aggregates dominated the 30-day query budget.
            "magnitudes": [],
            "event_types": [],
            "potential_misses": self._miss_rows(sections["miss"]),
            "summary": {
                "hit_1h_pct": None,
                "hit_1h_n": 0,
                "coverage_1h_pct": (coverage[0] if coverage else {}).get("coverage_pct"),
            },
        }

    def _review_sections(self, params: tuple[Any, ...], *, cohort_filter: str = "") -> dict[str, list[dict[str, Any]]]:
        """Every section of the page in one statement, tagged by section and carried as JSON rows."""

        facts_cte = _REVIEW_FACTS_CTE.format(cohort_filter=cohort_filter)
        rows = self.conn.execute(
            f"""
            WITH {facts_cte},
            -- One coverage aggregate and one bounded discovery queue over one materialized fact set.  #112
            -- retired the direction/magnitude/event-type price rankings: they were neither causal quality
            -- evidence nor rendered by ReviewDesk, and they consumed the 30-day query budget.
            coverage AS (
              SELECT count(*) FILTER (WHERE mature_1h) AS eligible_1h,
                     count(*) FILTER (WHERE mature_4h) AS eligible_4h,
                     count(*) FILTER (WHERE mature_1h AND priced_1h > 0) AS priced_1h,
                     count(*) FILTER (WHERE mature_4h AND priced_4h > 0) AS priced_4h,
                     count(*) FILTER (WHERE mature_1h AND NOT has_primary) AS no_primary_1h,
                     count(*) FILTER (WHERE mature_4h AND NOT has_primary) AS no_primary_4h,
                     count(*) FILTER (WHERE mature_1h AND degraded) AS degraded_1h,
                     count(*) FILTER (WHERE mature_4h AND degraded) AS degraded_4h,
                     count(*) FILTER (
                       WHERE mature_1h AND unavailable_reason = 'instrument_unresolved'
                     ) AS unresolved_1h,
                     count(*) FILTER (
                       WHERE mature_4h AND unavailable_reason = 'instrument_unresolved'
                     ) AS unresolved_4h,
                     count(*) FILTER (WHERE mature_1h AND unavailable_reason = 'no_candle_within_gap') AS gap_1h,
                     count(*) FILTER (WHERE mature_4h AND unavailable_reason = 'no_candle_within_gap') AS gap_4h,
                     count(*) FILTER (WHERE mature_1h AND unavailable_reason = 'history_expired') AS expired_1h,
                     count(*) FILTER (WHERE mature_4h AND unavailable_reason = 'history_expired') AS expired_4h,
                     count(*) FILTER (WHERE mature_1h AND unavailable_reason = 'reference_only') AS reference_1h,
                     count(*) FILTER (WHERE mature_4h AND unavailable_reason = 'reference_only') AS reference_4h
                FROM fact
            ),
            miss AS (
              SELECT event_id, opened_at_ms, final_decision, override_rule, throttled_by, direction,
                     magnitude, event_type, bps_1h, bps_4h, asset_n
                FROM fact
               WHERE COALESCE(delivered, false) IS NOT TRUE AND NOT degraded AND bps_1h IS NOT NULL
               ORDER BY abs(bps_1h) DESC, opened_at_ms DESC
               LIMIT {int(REVIEW_POTENTIAL_MISS_LIMIT)}
            )
            -- Each CTE is aliased before `to_jsonb`: a bare CTE name that also names one of its columns
            -- resolves to the column, and `to_jsonb(direction)` silently returned the string 'bullish'.
            SELECT 'coverage' AS section, to_jsonb(c) AS payload FROM coverage c
            UNION ALL SELECT 'miss', to_jsonb(x) FROM miss x
            """,
            params,
        ).fetchall()
        out: dict[str, list[dict[str, Any]]] = {
            "coverage": [],
            "miss": [],
        }
        for row in rows:
            payload = row["payload"]
            # psycopg hands jsonb back as a mapping when a loader is registered and as text otherwise; the
            # section rows are small, so accept either rather than depending on connection setup.
            out[str(row["section"])].append(dict(json.loads(payload) if isinstance(payload, str) else payload))
        return out

    def _miss_rows(self, misses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Withheld Events ranked by how much the market moved afterwards — a queue, never a verdict.

        Price movement does not prove the Event caused the move or that it should have been pushed, so every
        row carries the decision and the named rule that produced it and nothing here writes a label.
        """

        event_ids = [str(row["event_id"]) for row in misses]
        assets = self._reaction_assets_for(event_ids)
        text = self._miss_text(event_ids)
        rows: list[dict[str, Any]] = [
            {
                "event_id": str(data["event_id"]),
                "opened_at_ms": int(data["opened_at_ms"]),
                "headline_zh": (
                    text.get(str(data["event_id"]), {}).get("headline_zh")
                    or text.get(str(data["event_id"]), {}).get("leader_title")
                ),
                "leader_title": str(text.get(str(data["event_id"]), {}).get("leader_title") or ""),
                "storyline_key": str(text.get(str(data["event_id"]), {}).get("storyline_key") or ""),
                "final_decision": str(data.get("final_decision") or ""),
                "decision_zh": decision_zh(data.get("final_decision")),
                "override_rule": data.get("override_rule"),
                "override_rule_zh": override_rule_zh(data.get("override_rule")),
                "throttled_by": data.get("throttled_by"),
                "throttled_by_zh": throttled_by_zh(data.get("throttled_by")),
                "direction": data.get("direction"),
                "direction_zh": direction_zh(data.get("direction")),
                "magnitude": _optional_int(data.get("magnitude")),
                "magnitude_zh": magnitude_zh(_optional_int(data.get("magnitude"))),
                "event_type": data.get("event_type"),
                "event_type_zh": event_type_zh(data.get("event_type")),
                "return_1h_bps": _optional_int(data.get("bps_1h")),
                "return_4h_bps": median_bps(
                    [
                        int(asset["return_4h_bps"])
                        for asset in assets.get(str(data["event_id"]), [])
                        if asset.get("is_primary") and asset.get("return_4h_bps") is not None
                    ]
                ),
                "asset_n": int(data.get("asset_n") or 0),
                "assets": assets.get(str(data["event_id"]), []),
            }
            for data in misses
        ]
        # The market queue is about distinct facts, not how many provider Items happened to become Events.
        # N is capped at 50, so an explicit pairwise connected-component fold is simpler and more auditable
        # than adding another persistence model.  Similar wording within four hours is one discovery case;
        # the highest-move row remains the representative because `miss` already arrives in that order.
        parents = list(range(len(rows)))

        def root(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            a, b = root(left), root(right)
            if a != b:
                parents[b] = a

        for left, first in enumerate(rows):
            first_text = str(first.get("headline_zh") or first.get("leader_title") or "")
            for right in range(left + 1, len(rows)):
                second = rows[right]
                if abs(int(first["opened_at_ms"]) - int(second["opened_at_ms"])) > 4 * 3_600_000:
                    continue
                second_text = str(second.get("headline_zh") or second.get("leader_title") or "")
                if similarity(first_text, second_text) >= 0.55:
                    union(left, right)

        groups: dict[int, list[int]] = {}
        for index in range(len(rows)):
            groups.setdefault(root(index), []).append(index)
        clustered: list[dict[str, Any]] = []
        for members in sorted(groups.values(), key=min):
            representative = dict(rows[min(members)])
            event_ids = sorted(str(rows[index]["event_id"]) for index in members)
            representative["fact_cluster_key"] = hashlib.sha256("|".join(event_ids).encode()).hexdigest()
            representative["fact_cluster_n"] = len(event_ids)
            representative["related_event_ids"] = event_ids
            clustered.append(representative)
        return clustered

    def _miss_text(self, event_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Reader text for the rows that actually reach the page — never carried through the window's sort."""

        if not event_ids:
            return {}
        rows = self.conn.execute(
            """
            SELECT e.event_id, e.leader_title, e.storyline_key,
                   (SELECT v.verdict ->> 'headline_zh' FROM news_verdicts v
                     WHERE v.event_id = e.event_id AND v.stage = 'triage'
                     ORDER BY v.created_at_ms DESC LIMIT 1) AS headline_zh
              FROM news_events e
             WHERE e.event_id = ANY(%s)
            """,
            (list(event_ids),),
        ).fetchall()
        return {str(row["event_id"]): dict(row) for row in rows}

    def _reaction_assets_for(self, event_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        if not event_ids:
            return {}
        rows = self.conn.execute(
            """
            SELECT event_id, symbol, metric_version, venue, venue_symbol, instrument_class, anchor_at_ms,
                   p0, p0_at_ms, p1, p1_at_ms, p4, p4_at_ms, return_1h_bps, return_4h_bps,
                   is_primary, state, unavailable_reason, updated_at_ms
              FROM news_event_reactions
             WHERE event_id = ANY(%s) AND metric_version = %s
             ORDER BY event_id, symbol
            """,
            (list(event_ids), REACTION_METRIC_VERSION),
        ).fetchall()
        out: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            data = dict(row)
            out.setdefault(str(data["event_id"]), []).append(_reaction_public(data))
        return out

    # ------------------------------------------------------------------ telemetry
    def price_status(self, *, now_ms: int) -> dict[str, Any]:
        """What an operator needs before the UI shows it: source freshness and Reaction backlog."""

        snapshots = self.quote_snapshots()
        sources = [
            {
                "source_key": key,
                "target_count": int(row.get("target_count") or 0),
                "quote_count": len(row.get("quotes") or {}),
                "age_ms": max(0, int(now_ms) - int(row["received_at_ms"])),
                "state": quote_state(max(0, int(now_ms) - int(row["received_at_ms"]))),
                "source_at_ms": _optional_int(row.get("source_at_ms")),
                "received_at_ms": int(row["received_at_ms"]),
            }
            for key, row in sorted(snapshots.items())
        ]
        row = self.conn.execute(
            """
            SELECT count(*) FILTER (WHERE state = 'partial') AS partial_n,
                   count(*) FILTER (WHERE state = 'complete') AS complete_n,
                   count(*) FILTER (WHERE state = 'unavailable') AS unavailable_n
              FROM news_event_reactions
             WHERE metric_version = %s AND anchor_at_ms >= %s
            """,
            (REACTION_METRIC_VERSION, int(now_ms) - 7 * 24 * 3_600_000),
        ).fetchone()
        counts = dict(row or {})
        return {
            "metric_version": REACTION_METRIC_VERSION,
            # The backlog SLO (#88 §14) is oldest-due age, not loop frequency: a turn can run on time and
            # still fall behind. Reporting it is what makes "healthy under 5 minutes" observable at all.
            "oldest_due_age_ms": self.oldest_due_age_ms(now_ms=now_ms, history_max_age_ms=REACTION_HISTORY_MAX_AGE_MS),
            "sources": sources,
            "fresh_sources": sum(1 for source in sources if source["state"] == "fresh"),
            "quotes": sum(source["quote_count"] for source in sources),
            "reaction_partial_7d": int(counts.get("partial_n") or 0),
            "reaction_complete_7d": int(counts.get("complete_n") or 0),
            "reaction_unavailable_7d": int(counts.get("unavailable_n") or 0),
        }


def _coverage_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Coverage before accuracy: the denominators and the named reasons a horizon could not be priced."""

    rows: list[dict[str, Any]] = []
    for horizon, key in (("1h", "priced_1h"), ("4h", "priced_4h")):
        eligible = int(data.get(f"eligible_{horizon}") or 0)
        unavailable = [
            {"reason": reason, "reason_zh": reaction_reason_zh(reason), "n": int(data.get(reason_key) or 0)}
            for reason, reason_key in (
                ("instrument_unresolved", f"unresolved_{horizon}"),
                ("no_candle_within_gap", f"gap_{horizon}"),
                ("history_expired", f"expired_{horizon}"),
                ("reference_only", f"reference_{horizon}"),
            )
            if int(data.get(reason_key) or 0) > 0
        ]
        rows.append(
            {
                "horizon": horizon,
                "horizon_zh": horizon_zh(horizon),
                "eligible_n": eligible,
                "priced_n": int(data.get(key) or 0),
                "coverage_pct": coverage_pct(int(data.get(key) or 0), eligible),
                "no_primary_n": int(data.get(f"no_primary_{horizon}") or 0),
                "degraded_n": int(data.get(f"degraded_{horizon}") or 0),
                "unavailable": unavailable,
            }
        )
    return rows


def _unlisted_quote(symbol: str) -> dict[str, Any]:
    return {
        "requested_symbol": symbol,
        "symbol": normalize_symbol(symbol),
        "base_symbol": normalize_symbol(symbol),
        "venue": None,
        "venue_symbol": None,
        "instrument_class": None,
        "quote_asset": None,
        "price": None,
        "price_kind": None,
        "price_kind_zh": "",
        "change_pct": None,
        "change_basis": None,
        "change_basis_zh": "",
        "source_at_ms": None,
        "received_at_ms": None,
        "age_ms": None,
        "state": "unlisted",
        "state_zh": quote_state_zh("unlisted"),
    }


def _unavailable_quote(symbol: str, instrument: PriceInstrument) -> dict[str, Any]:
    return {
        "requested_symbol": symbol,
        "symbol": instrument.base_symbol,
        "base_symbol": instrument.base_symbol,
        "venue": instrument.venue,
        "venue_symbol": instrument.venue_symbol,
        "instrument_class": instrument.instrument_class,
        "quote_asset": instrument.quote_asset,
        "price": None,
        "price_kind": price_kind_for(instrument.venue),
        "price_kind_zh": price_kind_zh(price_kind_for(instrument.venue)),
        "change_pct": None,
        "change_basis": None,
        "change_basis_zh": "",
        "source_at_ms": None,
        "received_at_ms": None,
        "age_ms": None,
        "state": "unavailable",
        "state_zh": quote_state_zh("unavailable"),
    }


def _reaction_public(row: Mapping[str, Any]) -> dict[str, Any]:
    state = str(row.get("state") or "pending")
    reason = row.get("unavailable_reason")
    return {
        "symbol": str(row["symbol"]),
        "metric_version": str(row.get("metric_version") or ""),
        "venue": str(row.get("venue") or "") or None,
        "venue_symbol": str(row.get("venue_symbol") or "") or None,
        "instrument_class": str(row.get("instrument_class") or "unknown"),
        "anchor_at_ms": int(row["anchor_at_ms"]),
        "p0": None if row.get("p0") is None else str(row["p0"]),
        "p0_at_ms": _optional_int(row.get("p0_at_ms")),
        "p1": None if row.get("p1") is None else str(row["p1"]),
        "p1_at_ms": _optional_int(row.get("p1_at_ms")),
        "p4": None if row.get("p4") is None else str(row["p4"]),
        "p4_at_ms": _optional_int(row.get("p4_at_ms")),
        "return_1h_bps": _optional_int(row.get("return_1h_bps")),
        "return_4h_bps": _optional_int(row.get("return_4h_bps")),
        "is_primary": bool(row.get("is_primary")),
        "state": state,
        "state_zh": reaction_state_zh(state),
        "unavailable_reason": reason,
        "unavailable_reason_zh": reaction_reason_zh(reason),
        "updated_at_ms": _optional_int(row.get("updated_at_ms")),
    }


def _aggregate_public(row: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Event-level state: pending until a horizon matures, unavailable when nothing about it can be priced."""

    anchor = int(row["anchor_at_ms"])
    primary_n = int(row.get("primary_n") or 0)
    row_n = int(row.get("row_n") or 0)
    unavailable_n = int(row.get("unavailable_n") or 0)
    matured = int(now_ms) >= anchor + 3_600_000
    bps_1h = [int(value) for value in (row.get("bps_1h") or [])]
    bps_4h = [int(value) for value in (row.get("bps_4h") or [])]
    reason = row.get("unavailable_reason")
    if bps_1h and bps_4h and len(bps_4h) >= len(bps_1h):
        state = "complete"
    elif bps_1h:
        state = "partial"
    elif primary_n > 0 and row_n > 0 and unavailable_n >= row_n:
        state = "unavailable"
    elif matured and row_n == 0:
        # The model named primaries the Gate never grounded, so nothing was ever measured for them. That is
        # a permanent answer once the horizon has passed, and calling it "pending" would leave the card
        # saying 未到期 for the rest of retention.
        state, reason = "unavailable", reason or "instrument_unresolved"
    else:
        # Either the horizon has not matured or the cold loop has not reached it yet; both are "not yet",
        # and neither may render as a zero return.
        state = "pending"
    if state != "unavailable":
        reason = None
    return {
        "state": state,
        "state_zh": reaction_state_zh(state),
        "return_1h_bps": median_bps(bps_1h),
        "return_4h_bps": median_bps(bps_4h),
        "asset_n": primary_n,
        "priced_n": len(bps_1h),
        "unavailable_reason": reason,
        "unavailable_reason_zh": reaction_reason_zh(reason),
        "metric_version": REACTION_METRIC_VERSION,
    }


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["PriceRepository"]
