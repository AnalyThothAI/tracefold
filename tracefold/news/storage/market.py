"""The market read model: Items and their typed facts, read directly.

Nothing here goes through a verdict, a reader-history snapshot, an Event leader or a model. A market
observation exists because the provider reported it and this process stored it, and that is the whole
question this module answers (#553).

The list collapses *consecutive observations of the same group*, and the group is per kind: an OI
group is one provider, venue, native instrument and measurement definition; a liquidation group adds
the liquidated side instead of the definition; a smart-money group is one account acting one way on
one instrument. A uniform "latest row per symbol" would let one account's Close bury another
account's Open, and let a Binance liquidation bury an OKX one. An observation with no trustworthy
group fields is its own group -- unknown does not merge with unknown.

`notification_status` is reported beside `parse_status` and never folded into it. A raw card that was
sent and a parsed card that was not are both ordinary states, and one combined "outcome" column would
have to lie about one of them.

"Consecutive" is consecutive *in what the reader asked for*. Narrowing to one kind merges a run that
an observation of another kind sat between, which is the honest answer to the narrowed question: the
row that would have broken the run is not on the page, so reporting two groups there would show a
break the reader cannot see. The unfiltered list keeps every break.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, TypedDict

from ..market_contracts import MARKET_TIMELINE_MAX, MARKET_WINDOW_ROW_CAP
from ..market_notifications import MARKET_TRACK_FIELDS, notification_status
from ..oi_contracts import OI_METRIC_VERSION
from ..source_contracts import MARKET_KINDS
from .sql_values import _dumps


class MarketObservationRow(TypedDict):
    """One provider record, its typed fact if it has one, and the group it belongs to."""

    item_id: str
    market_kind: str
    source_strategy_id: str | None
    parse_status: str
    parse_error: str | None
    ingest_mode: str
    historical: bool
    group_key: str
    title: str
    event_at_ms: int
    received_at_ms: int
    available_at_ms: int | None
    provider: str | None
    source_venue: str | None
    raw_instrument: str | None
    symbol: str | None
    measurement_definition: str | None
    direction: str | None
    oi_change_bps: int | None
    oi_value_usd: int | None
    whale_long_profit_bps: int | None
    whale_oi_ratio_bps: int | None
    liquidated_position_side: str | None
    forced_order_side: str | None
    notional_usd: str | None
    price: str | None
    trader_label: str | None
    account_address: str | None
    action: str | None
    position_side: str | None
    pnl_usd: str | None
    # Reported beside `parse_status`, never folded into it (#553 §6). An observation with no attempt
    # says which rule is holding it; one with an attempt says what the send did.
    notification_status: str
    notification_reason: str
    notify_group_key: str | None
    delivery_key: str | None


class MarketGroupRow(TypedDict):
    """One run of consecutive observations of the same group, newest first."""

    group_key: str
    market_kind: str
    observation_count: int
    first_event_at_ms: int
    last_event_at_ms: int
    latest: MarketObservationRow
    # The run's *oldest* member, which is where the next page must start. Paging from the newest
    # member would re-scan the rest of this run and emit the same group a second time.
    oldest_received_at_ms: int
    oldest_item_id: str
    notification_status: str
    notification_reason: str


class MarketSourceSummaryRow(TypedDict):
    """What one market kind did in the requested window: what arrived, and what was told.

    Fact and receipt counts side by side, which is the whole of the status block #553 §6 asks for.
    `merged` is the honest name for observations a card spoke for without being the record that
    triggered it -- the noise reduction, stated as a number rather than as a claim.
    """

    market_kind: str
    received: int
    parsed: int
    raw: int
    groups: int
    last_received_at_ms: int | None
    merged: int
    sent: int
    failed: int
    unknown: int
    last_sent_at_ms: int | None
    last_failed_at_ms: int | None
    last_unknown_at_ms: int | None


# One observation, whatever kind it is. The three fact tables stay separate tables -- a shared
# supertable would need a column for every kind's semantics and a NULL for every other kind's -- and
# they are unioned into one shape only here, at the point a reader actually needs one list.
# The OI ledger's key is `(source_item_id, metric_version)`, so the join needs both halves. A second
# metric version is a re-parse of the same provider record under a new parser generation, not a second
# observation of the market -- joining on the Item alone would duplicate every OI row and double the
# `observation_count` a reader is shown the day one lands.
_OBSERVATIONS_SQL = f"""
    SELECT i.item_id,
           i.market_kind,
           i.market_source_strategy_id AS source_strategy_id,
           i.market_parse_status AS parse_status,
           i.market_parse_error AS parse_error,
           i.first_ingest_mode AS ingest_mode,
           COALESCE(o.historical, false) AS historical,
           i.title,
           i.published_at_ms AS event_at_ms,
           i.observed_at_ms AS received_at_ms,
           COALESCE(o.available_at_ms, l.available_at_ms, w.available_at_ms) AS available_at_ms,
           COALESCE(o.provider, l.provider, w.provider) AS provider,
           COALESCE(o.source_venue, l.source_venue, w.source_venue) AS source_venue,
           COALESCE(o.raw_instrument, l.raw_instrument, w.raw_instrument) AS raw_instrument,
           COALESCE(o.symbol, l.symbol, w.symbol) AS symbol,
           o.measurement_definition,
           o.direction,
           o.oi_change_bps,
           o.oi_value_usd,
           o.whale_long_profit_bps,
           o.whale_oi_ratio_bps,
           l.liquidated_position_side,
           l.forced_order_side,
           COALESCE(l.notional_usd, w.reported_notional_usd)::text AS notional_usd,
           COALESCE(l.price, w.price)::text AS price,
           w.trader_label,
           w.account_address,
           w.action,
           w.position_side,
           w.pnl_usd::text AS pnl_usd,
           i.market_notify_state AS notify_state,
           i.market_notify_group_key AS notify_group_key,
           i.market_notify_delivery_key AS delivery_key,
           d.state AS delivery_state,
           d.error AS delivery_error,
           d.trigger_item_id AS delivery_trigger_item_id,
           t.pending_reason AS track_reason,
           -- No card claimed this observation and its group has moved on to a later round: nothing
           -- is holding it and nothing will cover it. The comparison lives here because the round
           -- start is the track's, and the track is already joined (#562 PR-F).
           COALESCE(i.market_notify_delivery_key IS NULL AND i.observed_at_ms < t.round_started_at_ms, false)
             AS round_closed,
           CASE
             WHEN o.source_item_id IS NOT NULL THEN
               'oi|' || o.provider || '|' || COALESCE(o.source_venue, '') || '|'
                     || o.raw_instrument || '|' || o.measurement_definition
             WHEN l.item_id IS NOT NULL THEN
               'liquidation|' || l.provider || '|' || COALESCE(l.source_venue, '') || '|'
                     || l.raw_instrument || '|' || l.liquidated_position_side
             WHEN w.item_id IS NOT NULL THEN
               'smart_money|' || w.provider || '|' || w.source_strategy_id || '|' || w.trader_label
                     || '|' || COALESCE(w.account_address, '') || '|' || COALESCE(w.source_venue, '')
                     || '|' || w.raw_instrument || '|' || w.action || '|' || w.position_side
             ELSE 'raw|' || i.market_kind || '|' || i.item_id
           END AS group_key
      FROM news_items i
      LEFT JOIN news_oi_signals o
             ON o.source_item_id = i.item_id
            AND o.metric_version = '{OI_METRIC_VERSION}'
      LEFT JOIN news_market_liquidations l ON l.item_id = i.item_id
      LEFT JOIN news_market_smart_money w ON w.item_id = i.item_id
      LEFT JOIN news_market_deliveries d ON d.delivery_key = i.market_notify_delivery_key
      LEFT JOIN news_market_tracks t ON t.group_key = i.market_notify_group_key
"""  # noqa: S608 -- the only interpolation is the code-owned `OI_METRIC_VERSION` literal

_OBSERVATION_KEYS: Final[tuple[str, ...]] = (
    "item_id",
    "market_kind",
    "source_strategy_id",
    "parse_status",
    "parse_error",
    "ingest_mode",
    "historical",
    "group_key",
    "title",
    "event_at_ms",
    "received_at_ms",
    "available_at_ms",
    "provider",
    "source_venue",
    "raw_instrument",
    "symbol",
    "measurement_definition",
    "direction",
    "oi_change_bps",
    "oi_value_usd",
    "whale_long_profit_bps",
    "whale_oi_ratio_bps",
    "liquidated_position_side",
    "forced_order_side",
    "notional_usd",
    "price",
    "trader_label",
    "account_address",
    "action",
    "position_side",
    "pnl_usd",
    "notify_group_key",
    "delivery_key",
)

MARKET_GROUPS_SQL = f"""
    WITH observations AS MATERIALIZED (
      SELECT * FROM ({_OBSERVATIONS_SQL}
         WHERE i.market_kind IS NOT NULL
           AND i.market_kind = ANY(%s)
           AND i.observed_at_ms >= %s
           AND i.observed_at_ms < %s
           AND (i.observed_at_ms, i.item_id) < (%s, %s)
         ORDER BY i.observed_at_ms DESC, i.item_id DESC
         LIMIT %s) AS windowed
    ), islands AS (
      SELECT observations.*,
             row_number() OVER (ORDER BY received_at_ms DESC, item_id DESC)
               - row_number() OVER (PARTITION BY group_key ORDER BY received_at_ms DESC, item_id DESC) AS island
        FROM observations
    ), collapsed AS (
      SELECT group_key, island, count(*) AS observation_count,
             min(event_at_ms) AS first_event_at_ms,
             max(event_at_ms) AS last_event_at_ms,
             (array_agg(item_id ORDER BY received_at_ms DESC, item_id DESC))[1] AS latest_item_id,
             (array_agg(item_id ORDER BY received_at_ms ASC, item_id ASC))[1] AS oldest_item_id,
             max(received_at_ms) AS sort_received_at_ms,
             min(received_at_ms) AS oldest_received_at_ms,
             (SELECT count(*) FROM observations) AS scanned
        FROM islands
       GROUP BY group_key, island
    )
    SELECT c.observation_count, c.first_event_at_ms, c.last_event_at_ms,
           c.oldest_received_at_ms, c.oldest_item_id, c.scanned, i.*
      FROM collapsed c
      JOIN islands i ON i.item_id = c.latest_item_id
     ORDER BY c.sort_received_at_ms DESC, c.latest_item_id DESC
     LIMIT %s
"""  # noqa: S608 -- the only interpolation is this module's own observation projection

# The second `news_items` reference is a primary-key lookup for the three columns only the detail page
# needs. Widening the shared observation projection with them would put a provider payload into every
# row of every list page to serve one row of one detail page.
MARKET_ITEM_SQL = f"""
    SELECT windowed.*, i2.provider_params, i2.description, i2.raw_first_line
      FROM ({_OBSERVATIONS_SQL} WHERE i.item_id = %s AND i.market_kind IS NOT NULL) AS windowed
      JOIN news_items i2 ON i2.item_id = windowed.item_id
"""  # noqa: S608 -- interpolates only this module's own observation projection

MARKET_TIMELINE_SQL = f"""
    SELECT * FROM ({_OBSERVATIONS_SQL} WHERE i.market_kind IS NOT NULL) AS windowed
     WHERE windowed.group_key = %s
     ORDER BY windowed.received_at_ms DESC, windowed.item_id DESC
     LIMIT {MARKET_TIMELINE_MAX}
"""  # noqa: S608 -- interpolates only this module's own timeline cap

# The receipt side of the status block, read from the cards themselves rather than from the Items:
# one card covers many observations, so "how many observations were told about" and "how many cards
# were sent" are two different questions, and this one asks the second.
MARKET_DELIVERY_SUMMARY_SQL = """
    SELECT market_kind,
           count(*) FILTER (WHERE state = 'sent') AS sent,
           count(*) FILTER (WHERE state = 'failed') AS failed,
           count(*) FILTER (WHERE state = 'unknown') AS unknown,
           max(settled_at_ms) FILTER (WHERE state = 'sent') AS last_sent_at_ms,
           max(settled_at_ms) FILTER (WHERE state = 'failed') AS last_failed_at_ms,
           max(settled_at_ms) FILTER (WHERE state = 'unknown') AS last_unknown_at_ms
      FROM news_market_deliveries
     WHERE created_at_ms >= %s
       AND created_at_ms < %s
     GROUP BY market_kind
"""

# Deliberately uncapped. This is the answer to "what arrived", and a capped count would report a
# ceiling as a fact -- `received = 5000` on a busy window would read as the provider's number. The
# window is already bounded by the caller to at most `MARKET_WINDOW_MAX_MS`, and the aggregate is one
# indexed pass over it. `groups` counts distinct group keys rather than collapsed runs, which is the
# per-kind subject count a reader wants beside the intake.
MARKET_SOURCES_SQL = f"""
    WITH observations AS MATERIALIZED (
      SELECT * FROM ({_OBSERVATIONS_SQL}
         WHERE i.market_kind IS NOT NULL
           AND i.observed_at_ms >= %s
           AND i.observed_at_ms < %s) AS windowed
    )
    SELECT market_kind,
           count(*) AS received,
           count(*) FILTER (WHERE parse_status = 'parsed') AS parsed,
           count(*) FILTER (WHERE parse_status = 'raw') AS raw,
           count(DISTINCT group_key) AS groups,
           max(received_at_ms) AS last_received_at_ms,
           count(*) FILTER (
             WHERE delivery_key IS NOT NULL AND item_id IS DISTINCT FROM delivery_trigger_item_id
           ) AS merged
      FROM observations
     GROUP BY market_kind
"""  # noqa: S608 -- interpolates only this module's own observation projection


# `MarketTrack`'s own columns, in the module that defines them. Building the statement from the tuple
# rather than restating it is what makes a new column impossible to add to the dataclass and forget
# in the INSERT, the VALUES and the conflict update at once.
_TRACK_COLUMNS: Final[tuple[str, ...]] = MARKET_TRACK_FIELDS
_TRACK_INSERT = ", ".join(_TRACK_COLUMNS)
_TRACK_VALUES = ", ".join(f"%({column})s" for column in _TRACK_COLUMNS)
_TRACK_UPDATE = ",\n      ".join(f"{column} = EXCLUDED.{column}" for column in _TRACK_COLUMNS[1:])

# The loop's take query. `market_notify_state = 'pending'` is a marker, not a cursor: an Item stays in
# this answer until the loop has grouped it, whatever order its transaction became visible in.
MARKET_NOTIFY_BACKLOG_SQL = f"""
    SELECT * FROM ({_OBSERVATIONS_SQL} WHERE i.market_notify_state = 'pending') AS backlog
     ORDER BY backlog.received_at_ms ASC, backlog.item_id ASC
     LIMIT %s
"""  # noqa: S608 -- interpolates only this module's own observation projection

MARKET_TRACK_SQL = "SELECT * FROM news_market_tracks WHERE group_key = %s"

MARKET_TRACK_UPSERT_SQL = f"""
    INSERT INTO news_market_tracks ({_TRACK_INSERT}, created_at_ms, updated_at_ms)
    VALUES ({_TRACK_VALUES}, %(now_ms)s, %(now_ms)s)
    ON CONFLICT (group_key) DO UPDATE SET
      {_TRACK_UPDATE},
      updated_at_ms = EXCLUDED.updated_at_ms
"""  # noqa: S608 -- interpolates only this module's own column identifiers

MARKET_MARK_PROCESSED_SQL = """
    UPDATE news_items
       SET market_notify_state = 'processed', market_notify_group_key = %s
     WHERE item_id = ANY(%s)
       AND market_notify_state = 'pending'
"""

# `ON CONFLICT DO NOTHING` answers both keys at once: the primary key, which is why a restart never
# executes a card twice, and the partial unique index, which is why a group never has two un-started
# cards. Neither needs a read-then-write, and a read-then-write is exactly what two processes would
# interleave.
MARKET_OPEN_DELIVERY_SQL = """
    INSERT INTO news_market_deliveries (
      delivery_key, group_key, market_kind, trigger_reason, trigger_item_id, state,
      next_attempt_at_ms, created_at_ms, updated_at_ms
    ) VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s)
    ON CONFLICT DO NOTHING
    RETURNING delivery_key
"""

# The lower bound is the group's current alert round, and it is what stops a card from speaking for a
# round that ended before it. Without it, an observation a rule held hours ago -- an OI change under
# the follow-up threshold, then four quiet hours -- is swept into whatever card comes next, which is
# how the first production MARSCOIN card came to cover `01:20-07:34` (#562 PR-F). The partial index
# `ix_news_items_market_notify_unclaimed (market_notify_group_key, observed_at_ms)` serves exactly
# this predicate.
MARKET_ADOPT_UNCLAIMED_SQL = """
    UPDATE news_items
       SET market_notify_delivery_key = %s
     WHERE market_notify_group_key = %s
       AND market_notify_delivery_key IS NULL
       AND market_notify_state = 'processed'
       AND observed_at_ms >= %s
"""

# Named rather than starred: this one is on a public route, and a star over a base relation is how a
# column added later silently reaches the wire.
_DELIVERY_COLUMNS = """
           delivery_key, group_key, market_kind, trigger_reason, trigger_item_id, state, attempts,
           covered_count, covered_from_ms, covered_to_ms, card, receipt, error, next_attempt_at_ms,
           first_attempt_at_ms, last_attempt_at_ms, settled_at_ms, created_at_ms, updated_at_ms
"""

MARKET_DUE_DELIVERY_SQL = f"""
    SELECT {_DELIVERY_COLUMNS} FROM news_market_deliveries
     WHERE state = ANY (ARRAY['pending', 'unavailable'])
       AND next_attempt_at_ms <= %s
     ORDER BY next_attempt_at_ms, created_at_ms, delivery_key
     LIMIT 1
     FOR UPDATE SKIP LOCKED
"""  # noqa: S608 -- interpolates only this module's own column list

MARKET_DELIVERY_SQL = f"""
    SELECT {_DELIVERY_COLUMNS} FROM news_market_deliveries WHERE delivery_key = %s
"""  # noqa: S608 -- interpolates only this module's own column list

# The one un-started card of a group, read from the unique partial index that enforces there is at
# most one. Asking the index rather than the track's copy of the key is what keeps two processes
# from each believing they opened the first card.
MARKET_GROUP_OPEN_DELIVERY_SQL = """
    SELECT delivery_key FROM news_market_deliveries
     WHERE group_key = %s
       AND state = ANY (ARRAY['pending', 'unavailable'])
       AND attempts = 0
"""

MARKET_DISCARD_DELIVERY_SQL = """
    DELETE FROM news_market_deliveries
     WHERE delivery_key = %s
       AND attempts = 0
"""

MARKET_DELIVERY_ITEM_IDS_SQL = f"""
    SELECT item_id FROM news_items
     WHERE market_notify_delivery_key = %s
     ORDER BY observed_at_ms, item_id
     LIMIT {MARKET_TIMELINE_MAX}
"""  # noqa: S608 -- interpolates only this module's own timeline cap

# Deliberately uncapped, unlike the detail page's timeline. This is the set the card *is*: its report
# count, its span and the anchor the next alert is measured against all come from these rows, and a
# `LIMIT 200` here would have reported the 200th oldest observation as the newest one the card
# covered. The set is bounded by the alert round it belongs to, and the card's own line cap bounds
# what is rendered from it (#562 PR-F).
MARKET_DELIVERY_OBSERVATIONS_SQL = f"""
    SELECT * FROM ({_OBSERVATIONS_SQL} WHERE i.market_notify_delivery_key = %s) AS covered
     ORDER BY covered.received_at_ms ASC, covered.item_id ASC
"""  # noqa: S608 -- interpolates only this module's own projection

# The snapshot is frozen on the first attempt only. A retry is the same card being sent again after a
# failure that provably delivered nothing, and re-rendering it would change what the reader is told
# between two attempts of one intent.
#
# This is also the claim, and the whole of it: the row was read in an earlier transaction (#562 PR-B
# reads the quote between the two, holding no lock across it), so `FOR UPDATE SKIP LOCKED` no longer
# spans the read and the write. The predicate carries the two values the reader saw -- the attempt
# count and the due time -- so a card another process has since claimed, settled and re-queued fails
# this update instead of spending its second attempt early against a snapshot from the first.
MARKET_BEGIN_SEND_SQL = """
    UPDATE news_market_deliveries
       SET state = 'sending',
           attempts = attempts + 1,
           card = CASE WHEN attempts = 0 THEN %s::jsonb ELSE card END,
           covered_count = CASE WHEN attempts = 0 THEN %s ELSE covered_count END,
           covered_from_ms = CASE WHEN attempts = 0 THEN %s ELSE covered_from_ms END,
           covered_to_ms = CASE WHEN attempts = 0 THEN %s ELSE covered_to_ms END,
           first_attempt_at_ms = COALESCE(first_attempt_at_ms, %s),
           last_attempt_at_ms = %s,
           updated_at_ms = %s
     WHERE delivery_key = %s
       AND state = ANY (ARRAY['pending', 'unavailable'])
       AND attempts = %s
       AND next_attempt_at_ms <= %s
"""

MARKET_SETTLE_DELIVERY_SQL = """
    UPDATE news_market_deliveries
       SET state = %s,
           receipt = %s::jsonb,
           error = %s,
           next_attempt_at_ms = COALESCE(%s, next_attempt_at_ms),
           settled_at_ms = CASE
             WHEN %s = ANY (ARRAY['sent', 'failed', 'unknown']) THEN %s ELSE NULL END,
           updated_at_ms = %s
     WHERE delivery_key = %s
       AND state = 'sending'
"""

MARKET_TRACK_ATTEMPT_SQL = """
    UPDATE news_market_tracks
       SET anchor_attempt_at_ms = %s,
           open_delivery_key = CASE WHEN open_delivery_key = %s THEN NULL ELSE open_delivery_key END,
           next_due_at_ms = NULL,
           updated_at_ms = %s
     WHERE group_key = %s
"""

# What the delivered card ended on, and nothing about the newest observation: by the time a card
# settles the loop may already have recorded a later one, and a column that mixed the two would move
# the group's idea of what a reader has been told (#582 §3.1).
MARKET_TRACK_ANCHOR_SQL = """
    UPDATE news_market_tracks
       SET anchor_state = %s,
           anchor_delivery_key = %s,
           anchor_oi_change_bps = COALESCE(%s, anchor_oi_change_bps),
           anchor_direction = COALESCE(%s, anchor_direction),
           anchor_action = COALESCE(%s, anchor_action),
           anchor_position_side = COALESCE(%s, anchor_position_side),
           pending_reason = %s,
           updated_at_ms = %s
     WHERE group_key = %s
"""

MARKET_HOLD_UNAVAILABLE_SQL = """
    UPDATE news_market_deliveries
       SET state = 'unavailable', error = %s, updated_at_ms = %s
     WHERE state = 'pending'
       AND next_attempt_at_ms <= %s
"""

MARKET_HELD_EXISTS_SQL = """
    SELECT 1 FROM news_market_deliveries WHERE state = 'unavailable' LIMIT 1
"""

MARKET_RELEASE_UNAVAILABLE_SQL = """
    UPDATE news_market_deliveries
       SET state = 'pending', error = NULL, updated_at_ms = %s
     WHERE state = 'unavailable'
"""

MARKET_SWEEP_INTERRUPTED_SQL = """
    UPDATE news_market_deliveries
       SET state = 'unknown', error = %s, settled_at_ms = %s, updated_at_ms = %s
     WHERE state = 'sending'
    RETURNING delivery_key, group_key, market_kind
"""

MARKET_PRUNE_TRACKS_SQL = """
    DELETE FROM news_market_tracks
     WHERE group_key IN (
       SELECT t.group_key FROM news_market_tracks t
        WHERE t.last_observed_at_ms < %s
          AND NOT EXISTS (
            SELECT 1 FROM news_items i WHERE i.market_notify_group_key = t.group_key)
        ORDER BY t.last_observed_at_ms
        LIMIT %s)
"""


class MarketStorage:
    conn: Any

    def market_groups(
        self,
        *,
        kinds: tuple[str, ...],
        from_ms: int,
        to_ms: int,
        cursor_received_at_ms: int,
        cursor_item_id: str,
        limit: int,
    ) -> tuple[list[MarketGroupRow], bool]:
        """One page of collapsed groups, newest observation first, and whether the scan hit its cap.

        The page is scanned from the cursor downward rather than from the top of the window, so the
        row cap bounds one page instead of the whole window: it can no longer end pagination early by
        running out of scan before it runs out of groups. What the cap can still do is split a single
        run longer than `MARKET_WINDOW_ROW_CAP`, which is why the caller is told when it was reached
        instead of being handed a count that quietly stopped counting.
        """

        # The cap is bound, not baked: a statement that carried it as a literal could not be narrowed
        # by a test, so a test that thought it was proving the bound would be scanning the whole
        # window and passing against the very shape this replaced.
        rows = self.conn.execute(
            MARKET_GROUPS_SQL,
            (
                list(kinds or MARKET_KINDS),
                int(from_ms),
                int(to_ms),
                int(cursor_received_at_ms),
                cursor_item_id,
                MARKET_WINDOW_ROW_CAP,
                int(limit),
            ),
        ).fetchall()
        groups = [
            MarketGroupRow(
                group_key=str(row["group_key"]),
                market_kind=str(row["market_kind"]),
                observation_count=int(row["observation_count"]),
                first_event_at_ms=int(row["first_event_at_ms"]),
                last_event_at_ms=int(row["last_event_at_ms"]),
                latest=_observation(row),
                oldest_received_at_ms=int(row["oldest_received_at_ms"]),
                oldest_item_id=str(row["oldest_item_id"]),
                # The run's newest member answers for the run: it is the observation whose card, or
                # whose reason for not having one, is the current one. An older member's settled
                # outcome is still on the expanded timeline, where it belongs.
                notification_status=_observation(row)["notification_status"],
                notification_reason=_observation(row)["notification_reason"],
            )
            for row in rows
        ]
        scanned = int(rows[0]["scanned"]) if rows else 0
        return groups, scanned >= MARKET_WINDOW_ROW_CAP

    def market_item(self, *, item_id: str) -> dict[str, Any] | None:
        """One market Item with its stored provider payload. Not bound by the list's window."""

        row = self.conn.execute(MARKET_ITEM_SQL, (item_id,)).fetchone()
        if row is None:
            return None
        detail: dict[str, Any] = dict(_observation(row))
        detail["provider_params"] = dict(row["provider_params"] or {})
        detail["description"] = str(row["description"] or "")
        detail["raw_first_line"] = str(row["raw_first_line"] or "")
        delivery_key = detail.get("delivery_key")
        detail["notification_delivery"] = (
            None if not delivery_key else self.market_delivery(delivery_key=str(delivery_key))
        )
        detail["notification_covered_item_ids"] = (
            [] if not delivery_key else self.market_delivery_item_ids(delivery_key=str(delivery_key))
        )
        return detail

    def market_group_timeline(self, *, group_key: str) -> list[MarketObservationRow]:
        """Every retained observation of one group, newest first."""

        rows = self.conn.execute(MARKET_TIMELINE_SQL, (group_key,)).fetchall()
        return [_observation(row) for row in rows]

    def market_sources(self, *, from_ms: int, to_ms: int) -> list[MarketSourceSummaryRow]:
        """Per-kind intake for the window: what arrived, what parsed, how much it collapses to."""

        rows = self.conn.execute(MARKET_SOURCES_SQL, (int(from_ms), int(to_ms))).fetchall()
        by_kind = {str(row["market_kind"]): row for row in rows}
        sent_rows = self.conn.execute(MARKET_DELIVERY_SUMMARY_SQL, (int(from_ms), int(to_ms))).fetchall()
        by_delivery = {str(row["market_kind"]): row for row in sent_rows}

        def _count(source: dict[str, Any], kind: str, column: str) -> int:
            row = source.get(kind)
            return 0 if row is None or row[column] is None else int(row[column])

        def _stamp(source: dict[str, Any], kind: str, column: str) -> int | None:
            row = source.get(kind)
            return None if row is None or row[column] is None else int(row[column])

        return [
            MarketSourceSummaryRow(
                market_kind=kind,
                received=_count(by_kind, kind, "received"),
                parsed=_count(by_kind, kind, "parsed"),
                raw=_count(by_kind, kind, "raw"),
                groups=_count(by_kind, kind, "groups"),
                last_received_at_ms=_stamp(by_kind, kind, "last_received_at_ms"),
                # Covered by a card that some other record triggered: the observations the merging
                # rules folded in rather than interrupting a reader a second time for.
                merged=_count(by_kind, kind, "merged"),
                sent=_count(by_delivery, kind, "sent"),
                failed=_count(by_delivery, kind, "failed"),
                unknown=_count(by_delivery, kind, "unknown"),
                last_sent_at_ms=_stamp(by_delivery, kind, "last_sent_at_ms"),
                last_failed_at_ms=_stamp(by_delivery, kind, "last_failed_at_ms"),
                last_unknown_at_ms=_stamp(by_delivery, kind, "last_unknown_at_ms"),
            )
            for kind in MARKET_KINDS
        ]

    # --- the notification loop's own two states (#553 PR-2 §5.1) ---

    def market_notification_backlog(self, *, limit: int) -> list[MarketObservationRow]:
        """The oldest un-notified market observations, by marker rather than by a stamp cursor.

        A high-water mark over `created_at_ms` or an autoincrement would skip a transaction that
        commits late with an earlier stamp -- permanently, because the cursor has already passed it.
        The marker cannot: a row is in this answer until the loop has grouped it and said so.
        """

        rows = self.conn.execute(MARKET_NOTIFY_BACKLOG_SQL, (int(limit),)).fetchall()
        return [_observation(row) for row in rows]

    def market_track(self, *, group_key: str, for_update: bool = False) -> dict[str, Any] | None:
        """One group's alerting state. `for_update` is what serialises two processes on one group."""

        statement = MARKET_TRACK_SQL + (" FOR UPDATE" if for_update else "")
        row = self.conn.execute(statement, (group_key,)).fetchone()
        return None if row is None else dict(row)

    def market_save_track(self, *, track: Mapping[str, Any], now_ms: int) -> None:
        """Write one group's alerting state. Never a copy of an observation, only what the rules read."""

        params = {key: track.get(key) for key in _TRACK_COLUMNS}
        params["now_ms"] = int(now_ms)
        self.conn.execute(MARKET_TRACK_UPSERT_SQL, params)

    def market_mark_processed(self, *, item_ids: Sequence[str], group_key: str) -> int:
        """Record which notification group the loop put these observations in, and that it did."""

        cursor = self.conn.execute(MARKET_MARK_PROCESSED_SQL, (group_key, list(item_ids)))
        return int(cursor.rowcount or 0)

    def market_open_delivery(
        self,
        *,
        delivery_key: str,
        group_key: str,
        market_kind: str,
        trigger_reason: str,
        trigger_item_id: str,
        due_at_ms: int,
        now_ms: int,
    ) -> bool:
        """Create one un-started card, or leave the one that already exists alone.

        `ON CONFLICT DO NOTHING` on a key derived from the group, the trigger and the reason is the
        whole of "a confirmed delivery is never executed twice": a restart recomputes the same key and
        finds the row it already wrote.
        """

        row = self.conn.execute(
            MARKET_OPEN_DELIVERY_SQL,
            (
                delivery_key,
                group_key,
                market_kind,
                trigger_reason,
                trigger_item_id,
                int(due_at_ms),
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        return row is not None

    def market_adopt_unclaimed(self, *, group_key: str, delivery_key: str, min_received_at_ms: int) -> int:
        """Hand this card every observation of the group's current round that none has spoken for.

        `min_received_at_ms` is where that round started. Observations below it were held by a rule
        in a round that has since ended: they were never told to anyone, they are shown on the page
        as exactly that, and they are not folded into a card about something else (#562 PR-F).
        """

        cursor = self.conn.execute(MARKET_ADOPT_UNCLAIMED_SQL, (delivery_key, group_key, int(min_received_at_ms)))
        return int(cursor.rowcount or 0)

    def market_due_delivery(self, *, now_ms: int) -> dict[str, Any] | None:
        """Lock one due card for this process. `SKIP LOCKED` so two processes never take the same one."""

        row = self.conn.execute(MARKET_DUE_DELIVERY_SQL, (int(now_ms),)).fetchone()
        return None if row is None else dict(row)

    def market_delivery(self, *, delivery_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(MARKET_DELIVERY_SQL, (delivery_key,)).fetchone()
        return None if row is None else dict(row)

    def market_group_open_delivery(self, *, group_key: str) -> str | None:
        """This group's un-started card, if it has one. New observations merge into that one."""

        row = self.conn.execute(MARKET_GROUP_OPEN_DELIVERY_SQL, (group_key,)).fetchone()
        return None if row is None else str(row["delivery_key"])

    def market_discard_delivery(self, *, delivery_key: str) -> bool:
        """Drop an intent nothing was left to put on. Never touches one that was already attempted."""

        cursor = self.conn.execute(MARKET_DISCARD_DELIVERY_SQL, (delivery_key,))
        return bool(cursor.rowcount)

    def market_delivery_item_ids(self, *, delivery_key: str) -> list[str]:
        rows = self.conn.execute(MARKET_DELIVERY_ITEM_IDS_SQL, (delivery_key,)).fetchall()
        return [str(row["item_id"]) for row in rows]

    def market_delivery_observations(self, *, delivery_key: str) -> list[MarketObservationRow]:
        """Every observation this card speaks for, oldest first: the content the card renders from."""

        rows = self.conn.execute(MARKET_DELIVERY_OBSERVATIONS_SQL, (delivery_key,)).fetchall()
        return [_observation(row) for row in rows]

    def market_begin_send(
        self,
        *,
        delivery_key: str,
        card: Mapping[str, Any],
        covered_count: int,
        covered_from_ms: int,
        covered_to_ms: int,
        attempts: int,
        due_at_ms: int,
        now_ms: int,
    ) -> bool:
        """Freeze this card and claim the attempt, or answer False. A retry keeps its own snapshot.

        `attempts` and `due_at_ms` are the values this card was *read* with, and they make this a
        compare-and-set rather than a blind update: only the reader that still describes the row may
        claim it. The snapshot is written on the first attempt only -- a second attempt is the same
        card being sent again after a failure that provably delivered nothing, and re-rendering it
        would silently change what the reader is told between two attempts of one intent.
        """

        cursor = self.conn.execute(
            MARKET_BEGIN_SEND_SQL,
            (
                _dumps(dict(card)),
                int(covered_count),
                int(covered_from_ms),
                int(covered_to_ms),
                int(now_ms),
                int(now_ms),
                int(now_ms),
                delivery_key,
                int(attempts),
                int(due_at_ms),
            ),
        )
        return bool(cursor.rowcount)

    def market_settle_delivery(
        self,
        *,
        delivery_key: str,
        state: str,
        receipt: Mapping[str, Any] | None,
        error: str | None,
        next_attempt_at_ms: int | None,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            MARKET_SETTLE_DELIVERY_SQL,
            (
                state,
                _dumps(dict(receipt)) if receipt is not None else None,
                error,
                int(next_attempt_at_ms) if next_attempt_at_ms is not None else None,
                state,
                int(now_ms),
                int(now_ms),
                delivery_key,
            ),
        )
        return bool(cursor.rowcount)

    def market_set_track_attempt(self, *, group_key: str, delivery_key: str, attempt_at_ms: int) -> bool:
        """A send attempt started: anchor the follow-up window and stop merging into that card.

        The window is anchored on the attempt rather than on the newest record, so a group reporting
        continuously cannot push its own follow-up further away for ever (§4.3).
        """

        cursor = self.conn.execute(
            MARKET_TRACK_ATTEMPT_SQL, (int(attempt_at_ms), delivery_key, int(attempt_at_ms), group_key)
        )
        return bool(cursor.rowcount)

    def market_set_track_anchor(
        self,
        *,
        group_key: str,
        anchor_state: str,
        anchor_delivery_key: str,
        anchor_oi_change_bps: int | None,
        anchor_direction: str | None,
        anchor_action: str | None,
        anchor_position_side: str | None,
        pending_reason: str,
        now_ms: int,
    ) -> bool:
        """The anchor the *next* alert is measured against: the observation this card covered.

        `sent` and `unknown` both set it -- an unknown attempt may well have reached the reader, so
        re-sending the same snapshot would double-notify -- but only `sent` claims a delivery.
        """

        cursor = self.conn.execute(
            MARKET_TRACK_ANCHOR_SQL,
            (
                anchor_state,
                anchor_delivery_key,
                anchor_oi_change_bps,
                anchor_direction,
                anchor_action,
                anchor_position_side,
                pending_reason,
                int(now_ms),
                group_key,
            ),
        )
        return bool(cursor.rowcount)

    def market_hold_unavailable(self, *, reason: str, now_ms: int) -> int:
        """No sender: due cards say so and consume no attempt, and observations keep merging."""

        cursor = self.conn.execute(MARKET_HOLD_UNAVAILABLE_SQL, (reason, int(now_ms), int(now_ms)))
        return int(cursor.rowcount or 0)

    def market_release_unavailable(self, *, now_ms: int) -> int:
        """A sender exists again: every held card becomes due, still as one card per group.

        Read first. This runs on every claim tick for the life of the process, and an unconditional
        `UPDATE` would be forty thousand empty write transactions a day for the one moment a sender
        actually comes back. The probe is an index-only lookup on the same partial index the write
        uses.
        """

        if self.conn.execute(MARKET_HELD_EXISTS_SQL).fetchone() is None:
            return 0
        cursor = self.conn.execute(MARKET_RELEASE_UNAVAILABLE_SQL, (int(now_ms),))
        return int(cursor.rowcount or 0)

    def market_sweep_interrupted_sends(self, *, reason: str, now_ms: int) -> list[dict[str, Any]]:
        """A previous process left a card in flight. It is `unknown`, and it is never re-sent.

        Run once, after this process has taken Workers ownership, which is the only moment `sending`
        can be read as "nobody is sending this" rather than "someone is".
        """

        rows = self.conn.execute(MARKET_SWEEP_INTERRUPTED_SQL, (reason, int(now_ms), int(now_ms))).fetchall()
        return [dict(row) for row in rows]

    def market_prune_tracks(self, *, cutoff_ms: int, limit: int) -> int:
        """Drop alerting state for groups whose every observation has left the retention window."""

        cursor = self.conn.execute(MARKET_PRUNE_TRACKS_SQL, (int(cutoff_ms), int(limit)))
        return int(cursor.rowcount or 0)


def _observation(row: Any) -> MarketObservationRow:
    """Name every projected column once. No coercion beyond the two integer identities."""

    values: dict[str, Any] = {key: row[key] for key in _OBSERVATION_KEYS}
    values["historical"] = bool(values["historical"])
    values["event_at_ms"] = int(values["event_at_ms"])
    values["received_at_ms"] = int(values["received_at_ms"])
    status, reason = notification_status(
        notify_state=str(row["notify_state"] or ""),
        delivery_state=None if row["delivery_state"] is None else str(row["delivery_state"]),
        delivery_error=None if row["delivery_error"] is None else str(row["delivery_error"]),
        track_reason=None if row["track_reason"] is None else str(row["track_reason"]),
        round_closed=bool(row["round_closed"]),
    )
    values["notification_status"] = status
    values["notification_reason"] = reason
    observation: MarketObservationRow = values  # type: ignore[assignment]
    return observation


__all__ = [
    "MARKET_GROUPS_SQL",
    "MARKET_ITEM_SQL",
    "MARKET_SOURCES_SQL",
    "MARKET_TIMELINE_SQL",
    "MarketGroupRow",
    "MarketObservationRow",
    "MarketSourceSummaryRow",
    "MarketStorage",
]
