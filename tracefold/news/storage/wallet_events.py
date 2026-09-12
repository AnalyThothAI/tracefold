"""Wallet episode persistence: complete transactions, two windows and one first intent."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Final, cast

from ..chain_tape.contracts import ClassifiedFill
from ..wallet_contracts import WALLET_OUTCOME_HORIZONS, WalletEvent, WalletOutcome
from .sql_values import _dumps

FILL_COLUMNS: Final = """chain_id, tx_hash, log_index, block_number, block_hash, wallet, token,
    token_symbol, token_decimals, kind, amount_raw, cash_token, cash_amount_raw, cash_decimals,
    usd, usd_source, event_at_ms, received_at_ms, classified_at_ms, roster_version, provider"""

NET_BUY_WINDOW_SQL: Final = f"""
    SELECT {FILL_COLUMNS} FROM news_market_wallet_fills
     WHERE chain_id = %(chain_id)s AND token = %(token)s
       AND event_at_ms > %(from_ms)s AND event_at_ms <= %(to_ms)s
       AND (block_number, log_index) <= (%(block)s, %(log)s)
     ORDER BY block_number, log_index, tx_hash
"""  # noqa: S608 -- interpolates only code-owned SQL identifiers.

EVENT_COLUMNS: Final = """e.item_id, e.chain_id, e.token, e.token_symbol, e.trigger_tx_hash,
    e.event_at_ms, e.received_at_ms, e.detected_at_ms, e.last_effective_buy_at_ms, e.ended_at_ms,
    e.initial_snapshot, e.latest_snapshot, e.latest_matched, e.change_reason, e.updated_at_ms,
    e.trigger_max_age_s, e.notification_eligible, e.notification_reason, e.send_snapshot,
    e.reference_price, e.reference_at_ms, e.reference_source"""


WALLET_PENDING_RECEIPTS_SQL: Final = f"""
            WITH seed AS (
                SELECT f.chain_id, f.tx_hash, f.block_number AS block, f.log_index AS log
                  FROM news_market_wallet_fills f
                  JOIN news_market_wallet_tape_state s ON s.state_id = 'chain_tape'
                 WHERE f.derived_at_ms IS NULL
                   AND (f.block_number, f.log_index) <= (s.scanned_block, s.scanned_log)
                 ORDER BY f.block_number, f.log_index, f.chain_id, f.tx_hash LIMIT %s
            ), pending AS (
                SELECT chain_id, tx_hash, min(block) AS block, min(log) AS log FROM seed
                 GROUP BY chain_id, tx_hash
            )
            SELECT {", ".join("f." + name.strip() for name in FILL_COLUMNS.split(","))}
              FROM news_market_wallet_fills f JOIN pending p USING (chain_id, tx_hash)
             ORDER BY p.block, p.log, f.chain_id, f.tx_hash, f.log_index
        """  # noqa: S608 -- code-owned SQL identifiers.

WALLET_EVENTS_SQL: Final = f"""
            SELECT {EVENT_COLUMNS}, d.state AS notification_state,
                   COALESCE(d.error, CASE WHEN t.pending_reason IN (
                       'invalidated_before_send','stale_before_send','wallet_notifications_disabled'
                   ) THEN t.pending_reason END) AS notification_error,
                   d.created_at_ms AS intent_at_ms, d.first_attempt_at_ms,
                   d.settled_at_ms, d.attempts
              FROM news_market_wallet_events e
              JOIN news_items i ON i.item_id = e.item_id
              LEFT JOIN news_market_deliveries d ON d.delivery_key = i.market_notify_delivery_key
              LEFT JOIN news_market_tracks t ON t.group_key = i.market_notify_group_key
             WHERE e.event_at_ms >= %s AND e.event_at_ms < %s
               AND (%s::bigint IS NULL OR (e.event_at_ms,e.item_id) < (%s,%s))
             ORDER BY e.event_at_ms DESC, e.item_id DESC LIMIT %s
        """  # noqa: S608 -- code-owned SQL identifiers.

WALLET_EVENT_TOTALS_SQL: Final = """
            SELECT count(*) AS total, count(*) FILTER (WHERE e.ended_at_ms IS NULL) AS active,
                   count(*) FILTER (WHERE d.state = 'sent') AS sent
              FROM news_market_wallet_events e JOIN news_items i USING (item_id)
              LEFT JOIN news_market_deliveries d ON d.delivery_key = i.market_notify_delivery_key
             WHERE e.event_at_ms >= %s AND e.event_at_ms < %s
        """

WALLET_EVENT_FILLS_SQL: Final = """
            SELECT chain_id, tx_hash, log_index, block_number, block_hash, wallet, token,
                   token_symbol, token_decimals, kind, amount_raw::text, usd::text, usd_source,
                   event_at_ms, received_at_ms, classified_at_ms, roster_version
              FROM news_market_wallet_fills
             WHERE chain_id = %s AND token = %s AND event_at_ms > %s AND event_at_ms <= %s
               AND (block_number,log_index) <= (%s,%s)
               AND (%s::bigint IS NULL OR (block_number,log_index) < (%s,%s))
             ORDER BY block_number DESC, log_index DESC LIMIT %s
        """

WALLET_DUE_OUTCOMES_SQL: Final = """
                SELECT e.item_id, e.chain_id, e.token, e.reference_price, e.reference_at_ms,
                       i.market_notify_delivery_key AS delivery_key, e.event_at_ms + %s AS target_at_ms
                  FROM news_market_wallet_events e JOIN news_items i USING (item_id)
                  LEFT JOIN news_market_wallet_outcomes o ON o.item_id = e.item_id AND o.horizon = %s
                 WHERE e.event_at_ms + %s <= %s AND o.item_id IS NULL
                 ORDER BY COALESCE(e.outcome_attempted_at_ms,0), e.event_at_ms, e.item_id LIMIT %s
            """

WALLET_OUTCOMES_SQL: Final = """
            SELECT horizon, target_at_ms, at_ms, price::text, source, reference_price::text,
                   reference_at_ms, status,
                   CASE WHEN status = 'comparable' THEN ((price / reference_price - 1) * 100)::text
                   END AS change_percent
              FROM news_market_wallet_outcomes WHERE item_id = %s ORDER BY target_at_ms
        """

WALLET_EVENT_SQL: Final = f"""
            SELECT {EVENT_COLUMNS}, d.state AS notification_state,
                   COALESCE(d.error, CASE WHEN t.pending_reason IN (
                       'invalidated_before_send','stale_before_send','wallet_notifications_disabled'
                   ) THEN t.pending_reason END) AS notification_error,
                   d.created_at_ms AS intent_at_ms, d.first_attempt_at_ms,
                   d.settled_at_ms, d.attempts, d.card AS frozen_card
              FROM news_market_wallet_events e
              JOIN news_items i ON i.item_id = e.item_id
              LEFT JOIN news_market_deliveries d ON d.delivery_key = i.market_notify_delivery_key
              LEFT JOIN news_market_tracks t ON t.group_key = i.market_notify_group_key
             WHERE e.item_id = %s
        """  # noqa: S608 -- code-owned SQL identifiers.


def _fill(row: dict[str, Any]) -> ClassifiedFill:
    return ClassifiedFill(**{name: row[name] for name in ClassifiedFill.__dataclass_fields__})


class WalletEventStorage:
    conn: Any

    @contextmanager
    def wallet_read_snapshot(self) -> Iterator[None]:
        with self.conn.transaction():
            self.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield

    def wallet_pending_receipts(self, *, limit: int = 20) -> list[list[ClassifiedFill]]:
        """LIMIT applies to transactions; every returned receipt includes all of its fills."""
        rows = self.conn.execute(
            WALLET_PENDING_RECEIPTS_SQL,
            (int(limit),),
        ).fetchall()
        grouped: dict[tuple[int, str], list[ClassifiedFill]] = {}
        for row in rows:
            grouped.setdefault((row["chain_id"], row["tx_hash"]), []).append(_fill(row))
        return list(grouped.values())

    def wallet_window_fills(
        self,
        *,
        chain_id: int,
        token: str,
        from_ms: int,
        to_ms: int,
        block: int,
        log: int,
    ) -> list[ClassifiedFill]:
        rows = self.conn.execute(
            NET_BUY_WINDOW_SQL,
            {
                "chain_id": chain_id,
                "token": token,
                "from_ms": from_ms,
                "to_ms": to_ms,
                "block": block,
                "log": log,
            },
        ).fetchall()
        return [_fill(row) for row in rows]

    def wallet_receipt_pending(self, *, chain_id: int, tx_hash: str) -> bool:
        return (
            self.conn.execute(
                """
            SELECT 1 FROM news_market_wallet_fills
             WHERE chain_id = %s AND tx_hash = %s AND derived_at_ms IS NULL
             LIMIT 1 FOR UPDATE
        """,
                (chain_id, tx_hash),
            ).fetchone()
            is not None
        )

    def wallet_has_pending_receipts(self) -> bool:
        return (
            self.conn.execute("""
            SELECT 1 FROM news_market_wallet_fills f
              JOIN news_market_wallet_tape_state s ON s.state_id = 'chain_tape'
             WHERE f.derived_at_ms IS NULL
               AND (f.block_number, f.log_index) <= (s.scanned_block, s.scanned_log) LIMIT 1
        """).fetchone()
            is not None
        )

    def wallet_mark_receipt_derived(self, *, chain_id: int, tx_hash: str, now_ms: int, reasons: dict[str, str]) -> None:
        for token, reason in reasons.items():
            self.conn.execute(
                """
                UPDATE news_market_wallet_fills SET derived_at_ms = %s, derived_reason = %s
                 WHERE chain_id = %s AND tx_hash = %s AND token = %s AND derived_at_ms IS NULL
            """,
                (now_ms, reason, chain_id, tx_hash, token),
            )

    def wallet_active_event(self, *, chain_id: int, token: str) -> dict[str, Any] | None:
        return cast(
            dict[str, Any] | None,
            self.conn.execute(
                f"""
            SELECT {EVENT_COLUMNS} FROM news_market_wallet_events e
             WHERE chain_id = %s AND token = %s AND ended_at_ms IS NULL
        """,  # noqa: S608 -- interpolates only code-owned SQL identifiers.
                (chain_id, token),
            ).fetchone(),
        )

    def wallet_active_events(self, *, after_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        return list(
            self.conn.execute(
                f"""
            SELECT {EVENT_COLUMNS} FROM news_market_wallet_events e
             WHERE ended_at_ms IS NULL AND item_id > %s ORDER BY item_id LIMIT %s
        """,  # noqa: S608 -- interpolates only code-owned SQL identifiers.
                (after_id, limit),
            ).fetchall()
        )

    def chain_tape_insert_wallet_event(self, event: WalletEvent, *, snapshot_json: str) -> bool:
        snapshot = snapshot_json
        return bool(
            self.conn.execute(
                """
            INSERT INTO news_market_wallet_events (
                item_id, chain_id, token, token_symbol, trigger_tx_hash, event_at_ms,
                received_at_ms, detected_at_ms, last_effective_buy_at_ms,
                initial_snapshot, latest_snapshot, latest_matched, change_reason, updated_at_ms,
                trigger_max_age_s, notification_eligible, notification_reason,
                reference_price, reference_at_ms, reference_source
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,'triggered',%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (item_id) DO NOTHING
        """,
                (
                    event.item_id,
                    event.chain_id,
                    event.token,
                    event.token_symbol,
                    event.trigger_tx_hash,
                    event.event_at_ms,
                    event.received_at_ms,
                    event.detected_at_ms,
                    event.event_at_ms,
                    snapshot,
                    snapshot,
                    event.initial_snapshot.matched,
                    event.detected_at_ms,
                    event.trigger_max_age_s,
                    event.notification_eligible,
                    event.notification_reason,
                    event.reference_price,
                    event.reference_at_ms,
                    event.reference_source,
                ),
            ).rowcount
        )

    def wallet_update_event(
        self,
        *,
        item_id: str,
        snapshot_json: str,
        matched: bool,
        reason: str,
        last_effective_buy_at_ms: int,
        ended_at_ms: int | None,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_market_wallet_events
               SET latest_snapshot = %s::jsonb, latest_matched = %s, change_reason = %s,
                   last_effective_buy_at_ms = %s, ended_at_ms = %s, updated_at_ms = %s
             WHERE item_id = %s AND ended_at_ms IS NULL
        """,
            (snapshot_json, matched, reason, last_effective_buy_at_ms, ended_at_ms, now_ms, item_id),
        )

    def wallet_event(self, episode_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        locking = " FOR UPDATE OF e" if for_update else ""
        return cast(
            dict[str, Any] | None,
            self.conn.execute(
                WALLET_EVENT_SQL + locking,
                (episode_id,),
            ).fetchone(),
        )

    def wallet_events(
        self,
        *,
        from_ms: int,
        to_ms: int,
        before_at_ms: int | None,
        before_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        return list(
            self.conn.execute(
                WALLET_EVENTS_SQL,
                (from_ms, to_ms, before_at_ms, before_at_ms, before_id, limit),
            ).fetchall()
        )

    def wallet_event_totals(self, *, from_ms: int, to_ms: int) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.conn.execute(
                WALLET_EVENT_TOTALS_SQL,
                (from_ms, to_ms),
            ).fetchone(),
        )

    def wallet_event_fills(
        self,
        *,
        chain_id: int,
        token: str,
        from_ms: int,
        to_ms: int,
        cutoff_block: int,
        cutoff_log: int,
        before_block: int | None,
        before_log: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        return list(
            self.conn.execute(
                WALLET_EVENT_FILLS_SQL,
                (
                    chain_id,
                    token,
                    from_ms,
                    to_ms,
                    cutoff_block,
                    cutoff_log,
                    before_block,
                    before_block,
                    before_log,
                    limit,
                ),
            ).fetchall()
        )

    def wallet_freeze_send_snapshot(self, *, item_id: str, snapshot: dict[str, Any]) -> None:
        self.conn.execute(
            """
            UPDATE news_market_wallet_events SET send_snapshot = %s::jsonb
             WHERE item_id = %s AND send_snapshot IS NULL
        """,
            (_dumps(snapshot), item_id),
        )

    def wallet_suppress_delivery(self, *, delivery_key: str, reason: str, now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_market_deliveries SET state = 'failed', error = %s,
                   settled_at_ms = %s, updated_at_ms = %s
             WHERE delivery_key = %s AND state IN ('pending','unavailable') AND attempts = 0
        """,
            (reason, now_ms, now_ms, delivery_key),
        )
        self.conn.execute(
            """
            UPDATE news_market_tracks SET open_delivery_key = NULL, next_due_at_ms = NULL,
                   pending_reason = %s, updated_at_ms = %s
             WHERE open_delivery_key = %s
        """,
            (reason, now_ms, delivery_key),
        )

    def wallet_unprocessed_token(self, *, chain_id: int, token: str) -> bool:
        return (
            self.conn.execute(
                """
            SELECT 1 FROM news_market_wallet_fills
             WHERE chain_id = %s AND token = %s AND derived_at_ms IS NULL LIMIT 1
        """,
                (chain_id, token),
            ).fetchone()
            is not None
        )

    def chain_tape_due_outcomes(self, *, now_ms: int, limit: int) -> list[dict[str, Any]]:
        due: list[dict[str, Any]] = []
        for horizon, horizon_ms in WALLET_OUTCOME_HORIZONS:
            rows = self.conn.execute(
                WALLET_DUE_OUTCOMES_SQL,
                (horizon_ms, horizon, horizon_ms, now_ms, max(1, limit // 3)),
            ).fetchall()
            due.extend({**row, "horizon": horizon} for row in rows)
        return due

    def chain_tape_record_outcome(self, outcome: WalletOutcome) -> bool:
        return bool(
            self.conn.execute(
                """
            INSERT INTO news_market_wallet_outcomes
              (item_id,horizon,delivery_key,target_at_ms,at_ms,price,source,
               reference_price,reference_at_ms,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """,
                (
                    outcome.item_id,
                    outcome.horizon,
                    outcome.delivery_key,
                    outcome.target_at_ms,
                    outcome.at_ms,
                    outcome.price,
                    outcome.source,
                    outcome.reference_price,
                    outcome.reference_at_ms,
                    outcome.status,
                ),
            ).rowcount
        )

    def chain_tape_mark_outcome_attempted(self, item_ids: Sequence[str], *, now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_market_wallet_events SET outcome_attempted_at_ms = %s WHERE item_id = ANY(%s)
        """,
            (now_ms, list(item_ids)),
        )

    def wallet_outcomes(self, item_id: str) -> list[dict[str, Any]]:
        return list(
            self.conn.execute(
                WALLET_OUTCOMES_SQL,
                (item_id,),
            ).fetchall()
        )
