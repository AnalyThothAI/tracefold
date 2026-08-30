"""Durable Telegram update, manual session, event, and intent transitions."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from ..contracts import canonical_sha256
from ..manual import (
    ManualAccountSnapshot,
    ManualModificationGuard,
    ManualSessionState,
    ManualTargetPicker,
    ManualTargetPickerState,
    ManualTradeIntent,
    ManualTradeParameters,
    ManualTradePreview,
    ManualTradeSession,
    ManualTradeSource,
    ManualVenue,
    StrategyPreset,
    build_manual_trade_preview,
)
from ..manual_execution import ManualExecutionPlan
from ..manual_executor import (
    ManualExecutionRecord,
    ManualManagedPositionRecord,
    ManualProtectionExecutionRecord,
    ManualTradeOutcome,
    ManualTradeOutcomeState,
    ManualVenueOrderReceipt,
)
from ..manual_ledger import ManualTradeEvent, ManualTradeEventKind, create_manual_trade_event
from ..manual_portfolio import (
    ManualCloseRequest,
    ManualPositionState,
    ManualPositionView,
    ManualTradeHistoryEvent,
)
from .sql_values import _dumps

_SESSION_COLUMNS = """
session_id::text AS session_id, source_sha256, source, actor_user_id, chat_id,
source_message_id, interaction_message_id, state, preset, account_snapshot,
interaction_reply_attempted_at_ms, last_effect_update_id, last_effect_result_code,
recommended, selected, preview, guard, intent_id, version, created_at_ms, updated_at_ms
"""

_TARGET_PICKER_COLUMNS = """
picker_id::text AS picker_id, sources_sha256, sources, actor_user_id, chat_id,
source_message_id, interaction_message_id, reply_attempted_at_ms, state,
selected_symbol, consumed_session_id::text AS consumed_session_id, consumed_at_ms,
created_at_ms, updated_at_ms
"""


class ManualStorage:
    conn: Any

    def insert_telegram_development_test_news(
        self,
        *,
        source_id: str,
        delivery_message_id: int,
        delivery_target_sha256: str,
        test_kind: Literal["futures", "onchain"],
        headline_zh: str,
        direction: Literal["bullish", "bearish"],
        displayed_targets: tuple[str, ...],
        source_observed_at_ms: int,
        expires_at_ms: int,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO trading_telegram_development_test_news (
              source_id, delivery_message_id, delivery_target_sha256, test_kind,
              headline_zh, direction, displayed_targets, source_observed_at_ms,
              expires_at_ms, created_at_ms
            ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            """,
            (
                source_id,
                int(delivery_message_id),
                delivery_target_sha256,
                test_kind,
                headline_zh,
                direction,
                _dumps(displayed_targets),
                int(source_observed_at_ms),
                int(expires_at_ms),
                int(now_ms),
            ),
        )

    def telegram_development_test_news(
        self,
        *,
        message_id: int,
        target_sha256: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT source_id::text AS source_id, delivery_message_id,
                   delivery_target_sha256, test_kind, headline_zh, direction,
                   displayed_targets, source_observed_at_ms, expires_at_ms
              FROM trading_telegram_development_test_news
             WHERE delivery_message_id = %s
               AND delivery_target_sha256 = %s
               AND expires_at_ms > %s
            """,
            (int(message_id), target_sha256, int(now_ms)),
        ).fetchone()
        return None if row is None else dict(row)

    def register_trading_account_binding(
        self,
        *,
        account_ref: str,
        account_lane: Literal["manual", "auto"],
        venue: Literal["binance_usdm_demo", "binance_usdm_live"],
        credential_fingerprint: str,
        provider_account_fingerprint: str,
        now_ms: int,
    ) -> bool:
        if (account_lane == "auto" and venue != "binance_usdm_demo") or (
            account_lane == "manual" and venue != "binance_usdm_live"
        ):
            raise ValueError("trading_account_binding_lane_venue_invalid")
        row = self.conn.execute(
            """
            INSERT INTO trading_account_bindings (
              account_ref, account_lane, venue, credential_fingerprint,
              provider_account_fingerprint, created_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING account_ref
            """,
            (
                account_ref,
                account_lane,
                venue,
                credential_fingerprint,
                provider_account_fingerprint,
                int(now_ms),
            ),
        ).fetchone()
        if row is not None:
            return True
        existing = self.conn.execute(
            """
            SELECT account_lane, venue, credential_fingerprint, provider_account_fingerprint
              FROM trading_account_bindings
             WHERE account_ref = %s
            """,
            (account_ref,),
        ).fetchone()
        if existing is None:
            collision = self.conn.execute(
                """
                SELECT account_ref
                  FROM trading_account_bindings
                 WHERE credential_fingerprint = %s OR provider_account_fingerprint = %s
                """,
                (credential_fingerprint, provider_account_fingerprint),
            ).fetchone()
            if collision is not None:
                raise ValueError("trading_account_binding_isolation_conflict")
            raise ValueError("trading_account_binding_conflict")
        if (
            existing["account_lane"] != account_lane
            or existing["venue"] != venue
            or existing["credential_fingerprint"] != credential_fingerprint
            or existing["provider_account_fingerprint"] != provider_account_fingerprint
        ):
            raise ValueError("trading_account_binding_conflict")
        return False

    def manual_next_telegram_update_id(self) -> int:
        row = self.conn.execute("SELECT next_telegram_update_id FROM trading_manual_runtime WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("trading_manual_runtime_missing")
        return int(row["next_telegram_update_id"])

    def upsert_manual_account_snapshot(
        self,
        *,
        account_ref: str,
        venue: ManualVenue,
        equity_usd: Any,
        observed_at_ms: int,
        now_ms: int,
    ) -> bool:
        if venue != "binance_usdm_live":
            raise ValueError("manual_account_snapshot_live_venue_required")
        row = self.conn.execute(
            """
            INSERT INTO trading_manual_account_snapshots (
              account_ref, venue, equity_usd, observed_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (account_ref) DO UPDATE SET
              venue = EXCLUDED.venue,
              equity_usd = EXCLUDED.equity_usd,
              observed_at_ms = EXCLUDED.observed_at_ms,
              updated_at_ms = EXCLUDED.updated_at_ms
            WHERE trading_manual_account_snapshots.venue = EXCLUDED.venue
              AND trading_manual_account_snapshots.observed_at_ms <= EXCLUDED.observed_at_ms
            RETURNING account_ref
            """,
            (account_ref, venue, equity_usd, int(observed_at_ms), int(now_ms)),
        ).fetchone()
        return row is not None

    def manual_account_snapshot(self, account_ref: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT account_ref, venue, equity_usd, observed_at_ms, updated_at_ms
              FROM trading_manual_account_snapshots
             WHERE account_ref = %s
            """,
            (account_ref,),
        ).fetchone()
        return dict(row) if row is not None else None

    def claim_manual_telegram_update(self, update: Any, *, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            INSERT INTO trading_manual_telegram_updates (
              update_id, callback_query_id, actor_user_id, chat_id, message_id,
              callback_data, authorized, received_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING update_id
            """,
            (
                int(update.update_id),
                str(update.callback_query_id),
                int(update.actor_user_id),
                int(update.chat_id),
                int(update.message_id),
                str(update.data),
                bool(update.authorized),
                int(now_ms),
            ),
        ).fetchone()
        return row is not None

    def manual_telegram_update_state(self, update_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT state FROM trading_manual_telegram_updates WHERE update_id = %s",
            (int(update_id),),
        ).fetchone()
        return str(row["state"]) if row is not None else None

    def settle_manual_telegram_update(self, update_id: int, *, result_code: str, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_telegram_updates
               SET state = 'SETTLED', result_code = %s, settled_at_ms = %s
             WHERE update_id = %s AND state = 'RECEIVED'
            RETURNING update_id
            """,
            (str(result_code), int(now_ms), int(update_id)),
        ).fetchone()
        if row is not None:
            self.conn.execute(
                """
                UPDATE trading_manual_runtime
                   SET next_telegram_update_id = GREATEST(next_telegram_update_id, %s),
                       updated_at_ms = GREATEST(updated_at_ms, %s)
                 WHERE id = 1
                """,
                (int(update_id) + 1, int(now_ms)),
            )
        return row is not None

    def begin_manual_trade_session(
        self,
        *,
        session_id: str,
        source: ManualTradeSource,
        actor_user_id: int,
        chat_id: int,
        update_id: int,
        now_ms: int,
    ) -> tuple[ManualTradeSession, bool]:
        source_payload = source.model_dump(mode="json")
        source_sha256 = canonical_sha256(source_payload)
        row = self.conn.execute(
            f"""
            INSERT INTO trading_manual_sessions (
              session_id, source_sha256, source, actor_user_id, chat_id,
              source_message_id, state, last_effect_update_id, last_effect_result_code,
              created_at_ms, updated_at_ms
            ) VALUES (%s::uuid, %s, %s::jsonb, %s, %s, %s, 'AWAITING_STRATEGY', %s,
                      'session_created', %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING {_SESSION_COLUMNS}
            """,
            (
                session_id,
                source_sha256,
                _dumps(source_payload),
                int(actor_user_id),
                int(chat_id),
                source.delivery_message_id,
                int(update_id),
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        if row is not None:
            self._append_manual_event(
                session_id=session_id,
                event_kind="SESSION_CREATED",
                payload={"source": source_payload, "source_sha256": source_sha256},
                now_ms=now_ms,
            )
            return _manual_session(row), True
        existing = self.conn.execute(
            f"""
            SELECT {_SESSION_COLUMNS}
              FROM trading_manual_sessions
             WHERE chat_id = %s AND actor_user_id = %s AND source_message_id = %s
               AND state NOT IN ('REJECTED', 'CANCELLED', 'CLOSED')
            """,
            (int(chat_id), int(actor_user_id), source.delivery_message_id),
        ).fetchone()
        if existing is None:
            raise RuntimeError("manual_trade_session_conflict_unresolved")
        session = _manual_session(existing)
        if session.source_sha256 != source_sha256:
            raise ValueError("manual_trade_source_conflict")
        return session, False

    def begin_manual_target_picker(
        self,
        *,
        picker_id: str,
        sources: tuple[ManualTradeSource, ...],
        actor_user_id: int,
        chat_id: int,
        now_ms: int,
    ) -> tuple[ManualTargetPicker, bool]:
        if len(sources) < 2:
            raise ValueError("manual_target_picker_multiple_sources_required")
        source_message_ids = {source.delivery_message_id for source in sources}
        if len(source_message_ids) != 1:
            raise ValueError("manual_target_picker_source_message_mismatch")
        source_message_id = next(iter(source_message_ids))
        payload = [source.model_dump(mode="json") for source in sources]
        sources_sha256 = canonical_sha256(payload)
        self._lock_manual_target_picker_lane(
            chat_id=int(chat_id),
            actor_user_id=int(actor_user_id),
            source_message_id=int(source_message_id),
        )
        active_consumed = self.conn.execute(
            """
            SELECT picker.picker_id
              FROM trading_manual_target_pickers picker
              JOIN trading_manual_sessions session
                ON session.session_id = picker.consumed_session_id
             WHERE picker.chat_id = %s AND picker.actor_user_id = %s
               AND picker.source_message_id = %s AND picker.state = 'CONSUMED'
               AND session.state NOT IN ('REJECTED', 'CANCELLED', 'CLOSED')
             LIMIT 1
            """,
            (int(chat_id), int(actor_user_id), int(source_message_id)),
        ).fetchone()
        if active_consumed is not None:
            raise ValueError("manual_target_picker_session_active")
        row = self.conn.execute(
            f"""
            INSERT INTO trading_manual_target_pickers (
              picker_id, sources_sha256, sources, actor_user_id, chat_id,
              source_message_id, state, created_at_ms, updated_at_ms
            ) VALUES (%s::uuid, %s, %s::jsonb, %s, %s, %s, 'PENDING', %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING {_TARGET_PICKER_COLUMNS}
            """,
            (
                picker_id,
                sources_sha256,
                _dumps(payload),
                int(actor_user_id),
                int(chat_id),
                int(source_message_id),
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        if row is not None:
            return _manual_target_picker(row), True
        existing = self.conn.execute(
            f"""
            SELECT {_TARGET_PICKER_COLUMNS}
              FROM trading_manual_target_pickers
             WHERE chat_id = %s AND actor_user_id = %s AND source_message_id = %s
               AND state <> 'CONSUMED'
            """,
            (int(chat_id), int(actor_user_id), int(source_message_id)),
        ).fetchone()
        if existing is None:
            raise RuntimeError("manual_target_picker_conflict_unresolved")
        picker = _manual_target_picker(existing)
        if picker.sources_sha256 != sources_sha256:
            raise ValueError("manual_target_picker_sources_conflict")
        return picker, False

    def begin_manual_trade_session_from_picker(
        self,
        *,
        session_id: str,
        picker_id: str,
        source: ManualTradeSource,
        actor_user_id: int,
        chat_id: int,
        update_id: int,
        now_ms: int,
    ) -> tuple[ManualTradeSession, bool]:
        identity = self.conn.execute(
            """
            SELECT chat_id, actor_user_id, source_message_id
              FROM trading_manual_target_pickers
             WHERE picker_id = %s::uuid
            """,
            (picker_id,),
        ).fetchone()
        if identity is None:
            raise ValueError("manual_target_picker_missing")
        self._lock_manual_target_picker_lane(
            chat_id=int(identity["chat_id"]),
            actor_user_id=int(identity["actor_user_id"]),
            source_message_id=int(identity["source_message_id"]),
        )
        row = self.conn.execute(
            f"SELECT {_TARGET_PICKER_COLUMNS} FROM trading_manual_target_pickers WHERE picker_id = %s::uuid FOR UPDATE",
            (picker_id,),
        ).fetchone()
        if row is None:
            raise ValueError("manual_target_picker_missing")
        picker = _manual_target_picker(row)
        if picker.actor_user_id != int(actor_user_id) or picker.chat_id != int(chat_id):
            raise ValueError("manual_target_picker_binding_mismatch")
        candidate = next((item for item in picker.sources if item.base_symbol == source.base_symbol), None)
        if candidate != source:
            raise ValueError("manual_target_picker_source_conflict")
        if picker.state is ManualTargetPickerState.CONSUMED:
            if picker.selected_symbol != source.base_symbol or picker.consumed_session_id is None:
                raise ValueError("manual_target_picker_source_conflict")
            existing = self.manual_trade_session(picker.consumed_session_id)
            if existing is None or existing.source != source:
                raise RuntimeError("manual_target_picker_consumed_session_missing")
            return existing, False
        if picker.state is not ManualTargetPickerState.SENT:
            raise ValueError("manual_target_picker_not_ready")
        session, created = self.begin_manual_trade_session(
            session_id=session_id,
            source=source,
            actor_user_id=actor_user_id,
            chat_id=chat_id,
            update_id=update_id,
            now_ms=now_ms,
        )
        consumed = self.conn.execute(
            """
            UPDATE trading_manual_target_pickers
               SET state = 'CONSUMED', selected_symbol = %s,
                   consumed_session_id = %s::uuid, consumed_at_ms = %s, updated_at_ms = %s
             WHERE picker_id = %s::uuid AND state = 'SENT'
               AND selected_symbol IS NULL AND consumed_session_id IS NULL AND consumed_at_ms IS NULL
            RETURNING picker_id
            """,
            (source.base_symbol, session.session_id, int(now_ms), int(now_ms), picker_id),
        ).fetchone()
        if consumed is None:
            raise RuntimeError("manual_target_picker_consume_conflict")
        return session, created

    def _lock_manual_target_picker_lane(self, *, chat_id: int, actor_user_id: int, source_message_id: int) -> None:
        identity = f"tracefold:manual-target-picker:{chat_id}:{actor_user_id}:{source_message_id}"
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (identity,))

    def begin_manual_target_picker_reply(self, picker_id: str, *, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_target_pickers
               SET state = 'SENDING', reply_attempted_at_ms = %s, updated_at_ms = %s
             WHERE picker_id = %s::uuid AND state = 'PENDING'
               AND reply_attempted_at_ms IS NULL AND interaction_message_id IS NULL
            RETURNING picker_id
            """,
            (int(now_ms), int(now_ms), picker_id),
        ).fetchone()
        return row is not None

    def attach_manual_target_picker_message(self, picker_id: str, *, message_id: int, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_target_pickers
               SET state = 'SENT', interaction_message_id = %s, updated_at_ms = %s
             WHERE picker_id = %s::uuid AND state = 'SENDING'
               AND reply_attempted_at_ms IS NOT NULL AND interaction_message_id IS NULL
            RETURNING picker_id
            """,
            (int(message_id), int(now_ms), picker_id),
        ).fetchone()
        return row is not None

    def manual_target_picker(self, picker_id: str) -> ManualTargetPicker | None:
        row = self.conn.execute(
            f"SELECT {_TARGET_PICKER_COLUMNS} FROM trading_manual_target_pickers WHERE picker_id = %s::uuid",
            (picker_id,),
        ).fetchone()
        return None if row is None else _manual_target_picker(row)

    def begin_manual_interaction_reply(self, session_id: str, *, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_sessions
               SET interaction_reply_attempted_at_ms = %s, updated_at_ms = %s
             WHERE session_id = %s::uuid AND interaction_message_id IS NULL
               AND interaction_reply_attempted_at_ms IS NULL
            RETURNING session_id
            """,
            (int(now_ms), int(now_ms), session_id),
        ).fetchone()
        return row is not None

    def attach_manual_interaction_message(self, session_id: str, *, message_id: int, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_sessions
               SET interaction_message_id = %s, version = version + 1, updated_at_ms = %s
             WHERE session_id = %s::uuid
               AND state = 'AWAITING_STRATEGY'
               AND (interaction_message_id IS NULL OR interaction_message_id = %s)
            RETURNING session_id
            """,
            (int(message_id), int(now_ms), session_id, int(message_id)),
        ).fetchone()
        return row is not None

    def set_manual_trade_preview(
        self,
        *,
        session_id: str,
        preset: StrategyPreset,
        account_snapshot: ManualAccountSnapshot,
        recommended: ManualTradeParameters,
        selected: ManualTradeParameters,
        preview: ManualTradePreview,
        guard: ManualModificationGuard,
        update_id: int,
        result_code: str,
        now_ms: int,
    ) -> ManualTradeSession | None:
        if account_snapshot.venue != "binance_usdm_live" or preview.venue != "binance_usdm_live":
            raise ValueError("manual_trade_live_venue_required")
        current = self.conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM trading_manual_sessions WHERE session_id = %s::uuid FOR UPDATE",
            (session_id,),
        ).fetchone()
        if current is None:
            return None
        session = _manual_session(current)
        if session.last_effect_update_id == update_id:
            return session
        if session.last_effect_update_id is not None and session.last_effect_update_id > update_id:
            return None
        if session.state not in {
            ManualSessionState.AWAITING_STRATEGY,
            ManualSessionState.PREVIEW,
            ManualSessionState.MODIFYING,
            ManualSessionState.HIGH_RISK_CONFIRMATION,
        }:
            return None
        expected_preview = build_manual_trade_preview(
            side=session.source.side,
            venue=account_snapshot.venue,
            account_equity=account_snapshot.account_equity_usd,
            reference_entry=account_snapshot.reference_entry,
            parameters=selected,
            liquidation_distance_bps=account_snapshot.liquidation_distance_bps,
        )
        if preview != expected_preview:
            raise ValueError("manual_trade_preview_snapshot_mismatch")
        next_state = {
            "accepted": ManualSessionState.PREVIEW,
            "high_risk_confirmation": ManualSessionState.HIGH_RISK_CONFIRMATION,
            "rejected": ManualSessionState.MODIFYING,
        }[guard.state.value]
        row = self.conn.execute(
            f"""
            UPDATE trading_manual_sessions
               SET state = %s, preset = %s, account_snapshot = %s::jsonb,
                   recommended = %s::jsonb, selected = %s::jsonb, preview = %s::jsonb,
                   guard = %s::jsonb, version = version + 1, updated_at_ms = %s
                   , last_effect_update_id = %s, last_effect_result_code = %s
             WHERE session_id = %s::uuid
            RETURNING {_SESSION_COLUMNS}
            """,
            (
                next_state.value,
                preset.value,
                _dumps(account_snapshot.model_dump(mode="json")),
                _dumps(recommended.model_dump(mode="json")),
                _dumps(selected.model_dump(mode="json")),
                _dumps(preview.model_dump(mode="json")),
                _dumps(guard.model_dump(mode="json")),
                int(now_ms),
                int(update_id),
                str(result_code),
                session_id,
            ),
        ).fetchone()
        event_kind = "STRATEGY_SELECTED" if session.preset is None else "TRADE_MODIFIED"
        self._append_manual_event(
            session_id=session_id,
            event_kind=event_kind,
            payload={
                "preset": preset.value,
                "recommended": recommended.model_dump(mode="json"),
                "selected": selected.model_dump(mode="json"),
                "preview": preview.model_dump(mode="json"),
                "guard": guard.model_dump(mode="json"),
            },
            now_ms=now_ms,
        )
        return None if row is None else _manual_session(row)

    def confirm_manual_trade_intent(
        self,
        intent: ManualTradeIntent,
        *,
        update_id: int,
        result_code: str,
        now_ms: int,
    ) -> bool:
        if intent.venue != "binance_usdm_live":
            raise ValueError("manual_trade_live_venue_required")
        row = self.conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM trading_manual_sessions WHERE session_id = %s::uuid FOR UPDATE",
            (intent.session_id,),
        ).fetchone()
        if row is None:
            return False
        session = _manual_session(row)
        if session.last_effect_update_id == update_id:
            return session.intent_id == intent.intent_id
        if session.last_effect_update_id is not None and session.last_effect_update_id > update_id:
            return False
        if session.intent_id == intent.intent_id and session.state is ManualSessionState.CONFIRMED:
            return False
        if session.state not in {ManualSessionState.PREVIEW, ManualSessionState.HIGH_RISK_CONFIRMATION}:
            return False
        if (
            session.source != intent.source
            or session.actor_user_id != intent.actor_user_id
            or session.preset != intent.preset
            or session.recommended != intent.recommended
            or session.selected != intent.selected
            or session.guard != intent.guard
            or session.account_snapshot is None
            or session.account_snapshot.account_ref != intent.account_ref
            or session.account_snapshot.venue != intent.venue
            or session.account_snapshot.reference_entry != intent.reference_entry
            or session.account_snapshot.account_equity_usd != intent.account_equity_usd
        ):
            raise ValueError("manual_trade_intent_session_mismatch")
        inserted = self.conn.execute(
            """
            INSERT INTO trading_manual_intents (
              intent_id, session_id, account_ref, payload, updated_at_ms
            ) VALUES (%s, %s::uuid, %s, %s::jsonb, %s)
            ON CONFLICT DO NOTHING
            RETURNING intent_id
            """,
            (
                intent.intent_id,
                intent.session_id,
                intent.account_ref,
                _dumps(intent.model_dump(mode="json")),
                int(now_ms),
            ),
        ).fetchone()
        if inserted is None:
            return False
        updated = self.conn.execute(
            """
            UPDATE trading_manual_sessions
               SET state = 'CONFIRMED', intent_id = %s, version = version + 1, updated_at_ms = %s,
                   last_effect_update_id = %s, last_effect_result_code = %s
             WHERE session_id = %s::uuid AND intent_id IS NULL
            RETURNING session_id
            """,
            (intent.intent_id, int(now_ms), int(update_id), str(result_code), intent.session_id),
        ).fetchone()
        if updated is None:
            raise RuntimeError("manual_trade_intent_session_settle_failed")
        self._append_manual_event(
            session_id=intent.session_id,
            event_kind="TRADE_CONFIRMED",
            payload={"intent": intent.model_dump(mode="json")},
            now_ms=now_ms,
        )
        return True

    def cancel_manual_trade_session(
        self,
        session_id: str,
        *,
        update_id: int,
        result_code: str,
        now_ms: int,
    ) -> bool:
        current = self.manual_trade_session(session_id)
        if current is not None and current.last_effect_update_id == update_id:
            return current.state is ManualSessionState.CANCELLED
        row = self.conn.execute(
            """
            UPDATE trading_manual_sessions
               SET state = 'CANCELLED', version = version + 1, updated_at_ms = %s,
                   last_effect_update_id = %s, last_effect_result_code = %s
             WHERE session_id = %s::uuid
               AND state IN ('AWAITING_STRATEGY', 'PREVIEW', 'MODIFYING', 'HIGH_RISK_CONFIRMATION')
            RETURNING session_id
            """,
            (int(now_ms), int(update_id), str(result_code), session_id),
        ).fetchone()
        if row is None:
            return False
        self._append_manual_event(
            session_id=session_id,
            event_kind="TRADE_CANCELLED",
            payload={"reason": "operator_cancelled"},
            now_ms=now_ms,
        )
        return True

    def manual_trade_session(self, session_id: str) -> ManualTradeSession | None:
        row = self.conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM trading_manual_sessions WHERE session_id = %s::uuid",
            (session_id,),
        ).fetchone()
        return None if row is None else _manual_session(row)

    def manual_trade_sessions_for_actor(
        self,
        *,
        actor_user_id: int,
        chat_id: int,
        limit: int = 5,
    ) -> tuple[ManualTradeSession, ...]:
        bounded_limit = max(1, min(int(limit), 10))
        rows = self.conn.execute(
            f"""
            SELECT {_SESSION_COLUMNS}
              FROM trading_manual_sessions
             WHERE actor_user_id = %s AND chat_id = %s
             ORDER BY updated_at_ms DESC, session_id DESC
             LIMIT %s
            """,
            (int(actor_user_id), int(chat_id), bounded_limit),
        ).fetchall()
        return tuple(_manual_session(row) for row in rows)

    def manual_trade_events(self, session_id: str) -> list[ManualTradeEvent]:
        rows = self.conn.execute(
            """
            SELECT event_id, session_id::text AS session_id, event_index, event_kind,
                   payload, payload_sha256, created_at_ms
              FROM trading_manual_events
             WHERE session_id = %s::uuid
             ORDER BY event_index
            """,
            (session_id,),
        ).fetchall()
        return [ManualTradeEvent.model_validate(dict(row)) for row in rows]

    def manual_positions_for_actor(
        self,
        *,
        actor_user_id: int,
        chat_id: int,
        state: Literal["open", "closed", "all"] = "all",
        limit: int = 10,
    ) -> tuple[ManualPositionView, ...]:
        state_sql = {
            "open": "AND p.state IN ('OPEN', 'EXPOSED', 'CLOSING', 'MANUAL_REVIEW')",
            "closed": "AND p.state = 'CLOSED'",
            "all": "",
        }[state]
        bounded_limit = max(1, min(int(limit), 20))
        rows = self.conn.execute(
            f"""
            SELECT p.*, s.source, s.preset, s.recommended, s.selected,
                   (SELECT row_to_json(c) FROM (
                     SELECT close_id, intent_id, session_id::text AS session_id, requested_bps,
                            client_order_id, state, target_quantity, attempted_at_ms,
                            receipt, reconciled_at_ms, error_code, requested_at_ms, updated_at_ms
                       FROM trading_manual_close_orders c0
                      WHERE c0.intent_id = p.intent_id
                      ORDER BY c0.requested_at_ms DESC, c0.close_id DESC LIMIT 1
                   ) c) AS active_close
              FROM trading_manual_positions p
              JOIN trading_manual_sessions s ON s.session_id = p.session_id
             WHERE s.actor_user_id = %s AND s.chat_id = %s {state_sql}
             ORDER BY COALESCE(p.closed_at_ms, p.observed_at_ms) DESC, p.intent_id DESC
             LIMIT %s
            """,
            (int(actor_user_id), int(chat_id), bounded_limit),
        ).fetchall()
        return tuple(_manual_position_view(row) for row in rows)

    def manual_position_for_actor(
        self,
        *,
        session_id: str,
        actor_user_id: int,
        chat_id: int,
    ) -> ManualPositionView | None:
        rows = self.manual_positions_for_actor(
            actor_user_id=actor_user_id,
            chat_id=chat_id,
            state="all",
            limit=20,
        )
        return next((row for row in rows if row.session_id == session_id), None)

    def manual_trade_history_for_actor(
        self,
        *,
        actor_user_id: int,
        chat_id: int,
        limit: int = 20,
    ) -> tuple[ManualTradeHistoryEvent, ...]:
        bounded_limit = max(1, min(int(limit), 50))
        rows = self.conn.execute(
            """
            SELECT e.event_id, e.session_id::text AS session_id,
                   s.source ->> 'base_symbol' AS symbol,
                   e.event_kind, e.payload, e.created_at_ms
              FROM trading_manual_events e
              JOIN trading_manual_sessions s ON s.session_id = e.session_id
             WHERE s.actor_user_id = %s AND s.chat_id = %s
             ORDER BY e.created_at_ms DESC, e.event_id DESC
             LIMIT %s
            """,
            (int(actor_user_id), int(chat_id), bounded_limit),
        ).fetchall()
        return tuple(ManualTradeHistoryEvent.model_validate(dict(row)) for row in rows)

    def request_manual_position_close(
        self,
        *,
        session_id: str,
        actor_user_id: int,
        chat_id: int,
        requested_bps: Literal[3000, 5000, 10000],
        update_id: int,
        now_ms: int,
    ) -> ManualCloseRequest | None:
        position = self.conn.execute(
            """
            SELECT p.intent_id, p.session_id::text AS session_id
             FROM trading_manual_positions p
              JOIN trading_manual_sessions s ON s.session_id = p.session_id
             WHERE p.session_id = %s::uuid AND s.actor_user_id = %s AND s.chat_id = %s
               AND p.state IN ('OPEN', 'EXPOSED') AND p.quantity > 0
            """,
            (session_id, int(actor_user_id), int(chat_id)),
        ).fetchone()
        if position is None:
            return None
        identity = {
            "version": "manual_close_request_v1",
            "intent_id": str(position["intent_id"]),
            "actor_user_id": int(actor_user_id),
            "requested_bps": int(requested_bps),
            "telegram_update_id": int(update_id),
        }
        close_id = canonical_sha256(identity)
        client_order_id = f"tfm-c-{close_id[:24]}"
        inserted = self.conn.execute(
            """
            INSERT INTO trading_manual_close_orders (
              close_id, intent_id, session_id, requested_bps, client_order_id,
              requested_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s::uuid, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING close_id
            """,
            (
                close_id,
                position["intent_id"],
                position["session_id"],
                int(requested_bps),
                client_order_id,
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        row = self.conn.execute(
            """
            SELECT close_id, intent_id, session_id::text AS session_id, requested_bps,
                   client_order_id, state, target_quantity, attempted_at_ms,
                   receipt, reconciled_at_ms, error_code, requested_at_ms, updated_at_ms
              FROM trading_manual_close_orders WHERE close_id = %s
            """,
            (close_id,),
        ).fetchone()
        if row is None:
            return None
        if inserted is not None:
            self._append_manual_event(
                session_id=session_id,
                event_kind=ManualTradeEventKind.ORDER_FENCED,
                payload={
                    "leg": "manual_close",
                    "close_id": close_id,
                    "requested_bps": int(requested_bps),
                    "client_id": client_order_id,
                },
                now_ms=now_ms,
            )
        return _manual_close_request(row)

    def begin_manual_notification(self, *, now_ms: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            WITH candidate AS (
              SELECT n.notification_id
                FROM trading_manual_notifications n
                JOIN trading_manual_events e ON e.event_id = n.event_id
               WHERE n.state = 'PENDING'
               ORDER BY e.created_at_ms, e.session_id, e.event_index
               FOR UPDATE OF n SKIP LOCKED
               LIMIT 1
            )
            UPDATE trading_manual_notifications n
               SET state = 'SENDING', attempted_at_ms = %s
              FROM candidate c
             WHERE n.notification_id = c.notification_id
            RETURNING n.notification_id, n.event_id, n.session_id::text AS session_id,
                      n.source_message_id, n.notification_kind, n.payload, n.attempted_at_ms,
                      n.interaction_state, n.reply_state,
                      (SELECT s.chat_id FROM trading_manual_sessions s
                        WHERE s.session_id = n.session_id) AS chat_id,
                      (SELECT s.interaction_message_id FROM trading_manual_sessions s
                        WHERE s.session_id = n.session_id) AS interaction_message_id
            """,
            (int(now_ms),),
        ).fetchone()
        return None if row is None else dict(row)

    def terminalize_stale_manual_notifications(self, *, now_ms: int) -> int:
        interaction = self.conn.execute(
            """
            UPDATE trading_manual_notifications
               SET interaction_state = 'AMBIGUOUS',
                   interaction_error_code = 'interaction_ambiguous_after_crash',
                   interaction_settled_at_ms = %s
             WHERE interaction_state = 'SENDING' AND interaction_attempted_at_ms < %s
            """,
            (int(now_ms), int(now_ms) - 60_000),
        )
        reply = self.conn.execute(
            """
            UPDATE trading_manual_notifications
               SET state = 'AMBIGUOUS', error_code = 'reply_ambiguous_after_crash',
                   settled_at_ms = %s, reply_state = 'AMBIGUOUS',
                   reply_error_code = 'reply_ambiguous_after_crash', reply_settled_at_ms = %s
             WHERE reply_state = 'SENDING' AND reply_attempted_at_ms < %s
            """,
            (int(now_ms), int(now_ms), int(now_ms) - 60_000),
        )
        reset = self.conn.execute(
            """
            UPDATE trading_manual_notifications
               SET state = 'PENDING', attempted_at_ms = NULL
             WHERE state = 'SENDING' AND attempted_at_ms < %s AND reply_state = 'PENDING'
               AND interaction_state IN ('PENDING', 'SENT', 'AMBIGUOUS', 'SKIPPED')
            """,
            (int(now_ms) - 60_000,),
        )
        return int(interaction.rowcount or 0) + int(reply.rowcount or 0) + int(reset.rowcount or 0)

    def begin_manual_notification_effect(
        self,
        notification_id: str,
        *,
        effect: Literal["interaction", "reply"],
        now_ms: int,
    ) -> bool:
        prefix = _manual_notification_effect_prefix(effect)
        row = self.conn.execute(
            f"""
            UPDATE trading_manual_notifications
               SET {prefix}_state = 'SENDING', {prefix}_attempted_at_ms = %s
             WHERE notification_id = %s AND state = 'SENDING' AND {prefix}_state = 'PENDING'
            RETURNING notification_id
            """,
            (int(now_ms), notification_id),
        ).fetchone()
        return row is not None

    def skip_manual_notification_interaction(self, notification_id: str) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_notifications
               SET interaction_state = 'SKIPPED'
             WHERE notification_id = %s AND state = 'SENDING' AND interaction_state = 'PENDING'
            RETURNING notification_id
            """,
            (notification_id,),
        ).fetchone()
        return row is not None

    def settle_manual_notification_interaction(self, notification_id: str, *, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_notifications
               SET interaction_state = 'SENT', interaction_settled_at_ms = %s
             WHERE notification_id = %s AND state = 'SENDING' AND interaction_state = 'SENDING'
            RETURNING notification_id
            """,
            (int(now_ms), notification_id),
        ).fetchone()
        return row is not None

    def mark_manual_notification_interaction_ambiguous(
        self,
        notification_id: str,
        *,
        error_code: str,
        now_ms: int,
    ) -> bool:
        normalized_error = _manual_notification_error(error_code)
        row = self.conn.execute(
            """
            UPDATE trading_manual_notifications
               SET interaction_state = 'AMBIGUOUS', interaction_error_code = %s,
                   interaction_settled_at_ms = %s
             WHERE notification_id = %s AND state = 'SENDING' AND interaction_state = 'SENDING'
            RETURNING notification_id
            """,
            (normalized_error, int(now_ms), notification_id),
        ).fetchone()
        return row is not None

    def settle_manual_notification(
        self,
        notification_id: str,
        *,
        provider_message_id: int,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_notifications
               SET state = 'SENT', provider_message_id = %s, settled_at_ms = %s,
                   reply_state = 'SENT', reply_settled_at_ms = %s
             WHERE notification_id = %s AND state = 'SENDING' AND reply_state = 'SENDING'
            RETURNING notification_id
            """,
            (int(provider_message_id), int(now_ms), int(now_ms), notification_id),
        ).fetchone()
        return row is not None

    def mark_manual_notification_ambiguous(
        self,
        notification_id: str,
        *,
        error_code: str,
        now_ms: int,
    ) -> bool:
        try:
            normalized_error = _manual_notification_error(error_code)
        except ValueError:
            return False
        row = self.conn.execute(
            """
            UPDATE trading_manual_notifications
               SET state = 'AMBIGUOUS', error_code = %s, settled_at_ms = %s,
                   reply_state = 'AMBIGUOUS', reply_error_code = %s, reply_settled_at_ms = %s
             WHERE notification_id = %s AND state = 'SENDING' AND reply_state = 'SENDING'
            RETURNING notification_id
            """,
            (normalized_error, int(now_ms), normalized_error, int(now_ms), notification_id),
        ).fetchone()
        return row is not None

    def manual_next_execution_intent(self, *, account_ref: str) -> ManualExecutionRecord | None:
        row = self.conn.execute(
            """
            SELECT intent_id, session_id::text AS session_id, account_ref, payload, state,
                   execution_plan, execution_setting_attempted_at_ms, execution_setting_applied_at_ms,
                   entry_client_order_id, entry_fenced_at_ms,
                   entry_attempted_at_ms, entry_submitted_at_ms, entry_receipt,
                   take_profit_client_order_id, take_profit_fenced_at_ms,
                   take_profit_attempted_at_ms, take_profit_submitted_at_ms, take_profit_receipt,
                   stop_loss_client_order_id, stop_loss_fenced_at_ms,
                   stop_loss_attempted_at_ms, stop_loss_submitted_at_ms, stop_loss_receipt,
                   outcome, updated_at_ms
             FROM trading_manual_intents
             WHERE state IN ('PENDING', 'SUBMITTING', 'AMBIGUOUS')
               AND account_ref = %s
             ORDER BY CASE state WHEN 'AMBIGUOUS' THEN 0 WHEN 'SUBMITTING' THEN 1 ELSE 2 END,
                      updated_at_ms, intent_id
             LIMIT 1
            """,
            (account_ref,),
        ).fetchone()
        return None if row is None else _manual_execution_row(row)

    def manual_next_open_position(self, *, account_ref: str) -> ManualManagedPositionRecord | None:
        row = self.conn.execute(
            """
            SELECT i.payload, i.execution_plan,
                   COALESCE(p.opened_at_ms, i.entry_submitted_at_ms) AS opened_at_ms,
                   p.entry_price,
                   p.take_profit_cancel_attempted_at_ms, p.take_profit_cancelled_at_ms,
                   p.stop_loss_cancel_attempted_at_ms, p.stop_loss_cancelled_at_ms,
                   COALESCE((
                     SELECT jsonb_agg(c1.receipt ORDER BY c1.requested_at_ms, c1.close_id)
                       FROM trading_manual_close_orders c1
                      WHERE c1.intent_id = i.intent_id AND c1.state = 'FILLED'
                   ), '[]'::jsonb) AS close_receipts,
                   (SELECT row_to_json(c) FROM (
                     SELECT close_id, intent_id, session_id::text AS session_id, requested_bps,
                            client_order_id, state, target_quantity, attempted_at_ms,
                            receipt, reconciled_at_ms, error_code, requested_at_ms, updated_at_ms
                       FROM trading_manual_close_orders c0
                      WHERE c0.intent_id = i.intent_id
                        AND (c0.state IN ('PENDING', 'SUBMITTING', 'AMBIGUOUS')
                             OR (c0.state = 'FILLED' AND c0.reconciled_at_ms IS NULL))
                      ORDER BY c0.requested_at_ms DESC, c0.close_id DESC LIMIT 1
                   ) c) AS close_request
              FROM trading_manual_intents i
              JOIN trading_manual_sessions s ON s.session_id = i.session_id
              LEFT JOIN trading_manual_positions p ON p.intent_id = i.intent_id
             WHERE i.account_ref = %s AND i.state IN ('OPEN', 'EXPOSED')
               AND s.state IN ('OPEN', 'EXPOSED')
               AND (p.intent_id IS NULL OR p.state NOT IN ('CLOSED', 'MANUAL_REVIEW'))
             ORDER BY COALESCE(p.observed_at_ms, i.updated_at_ms), i.intent_id
             LIMIT 1
            """,
            (account_ref,),
        ).fetchone()
        if row is None:
            return None
        return ManualManagedPositionRecord(
            intent=ManualTradeIntent.model_validate(row["payload"]),
            plan=ManualExecutionPlan.model_validate(row["execution_plan"]),
            opened_at_ms=int(row["opened_at_ms"]),
            close_request=(
                None if row["close_request"] is None else ManualCloseRequest.model_validate(row["close_request"])
            ),
            take_profit_cancel_attempted=row["take_profit_cancel_attempted_at_ms"] is not None,
            take_profit_cancelled=row["take_profit_cancelled_at_ms"] is not None,
            stop_loss_cancel_attempted=row["stop_loss_cancel_attempted_at_ms"] is not None,
            stop_loss_cancelled=row["stop_loss_cancelled_at_ms"] is not None,
            entry_price=None if row["entry_price"] is None else Decimal(str(row["entry_price"])),
            close_receipts=tuple(_manual_order_receipt(value) for value in row["close_receipts"]),
        )

    def observe_manual_position(
        self,
        intent_id: str,
        *,
        position: Any,
        plan: ManualExecutionPlan,
        opened_at_ms: int,
        now_ms: int,
    ) -> bool:
        intent_row = self.conn.execute(
            """
            SELECT i.session_id::text AS session_id, i.account_ref, i.payload, i.state
              FROM trading_manual_intents i
             WHERE i.intent_id = %s AND i.state IN ('OPEN', 'EXPOSED')
            """,
            (intent_id,),
        ).fetchone()
        if intent_row is None:
            return False
        intent = ManualTradeIntent.model_validate(intent_row["payload"])
        quantity = abs(Decimal(str(position.quantity)))
        existing = self.conn.execute(
            "SELECT entry_price, mark_price, unrealized_pnl_usd, liquidation_price, state "
            "FROM trading_manual_positions WHERE intent_id = %s",
            (intent_id,),
        ).fetchone()
        if existing is None and quantity == 0:
            return False
        entry_price = (
            Decimal(str(position.entry_price)) if position.entry_price > 0 else Decimal(str(existing["entry_price"]))
        )
        mark_price = (
            Decimal(str(position.mark_price))
            if position.mark_price is not None and position.mark_price > 0
            else Decimal(str(existing["mark_price"]))
            if existing is not None
            else entry_price
        )
        unrealized = (
            Decimal(str(position.unrealized_pnl_usd))
            if position.unrealized_pnl_usd is not None
            else Decimal(str(existing["unrealized_pnl_usd"]))
            if existing is not None
            else Decimal("0")
        )
        liquidation = (
            Decimal(str(position.liquidation_price))
            if position.liquidation_price is not None
            else existing["liquidation_price"]
            if existing is not None
            else None
        )
        state = "CLOSING" if quantity == 0 else "EXPOSED" if intent_row["state"] == "EXPOSED" else "OPEN"
        self.conn.execute(
            """
            INSERT INTO trading_manual_positions (
              intent_id, session_id, account_ref, symbol, side, state, quantity,
              entry_price, mark_price, unrealized_pnl_usd, leverage, liquidation_price,
              take_profit_price, stop_loss_price, opened_at_ms, observed_at_ms
            ) VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (intent_id) DO UPDATE
              SET state = EXCLUDED.state, quantity = EXCLUDED.quantity,
                  entry_price = EXCLUDED.entry_price, mark_price = EXCLUDED.mark_price,
                  unrealized_pnl_usd = EXCLUDED.unrealized_pnl_usd,
                  leverage = EXCLUDED.leverage, liquidation_price = EXCLUDED.liquidation_price,
                  observed_at_ms = EXCLUDED.observed_at_ms, version = trading_manual_positions.version + 1
             WHERE trading_manual_positions.state IN ('OPEN', 'EXPOSED', 'CLOSING')
            """,
            (
                intent_id,
                intent_row["session_id"],
                intent_row["account_ref"],
                plan.symbol,
                intent.source.side.value,
                state,
                quantity,
                entry_price,
                mark_price,
                unrealized,
                int(position.leverage),
                liquidation,
                plan.take_profit_trigger,
                plan.stop_loss_trigger,
                int(opened_at_ms),
                int(now_ms),
            ),
        )
        return True

    def begin_manual_close_attempt(self, close_id: str, *, quantity: Decimal, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_close_orders
               SET state = 'SUBMITTING', target_quantity = %s,
                   attempted_at_ms = %s, updated_at_ms = %s
             WHERE close_id = %s AND state = 'PENDING'
            RETURNING intent_id
            """,
            (quantity, int(now_ms), int(now_ms), close_id),
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            "UPDATE trading_manual_positions SET state = 'CLOSING', version = version + 1 "
            "WHERE intent_id = %s AND state IN ('OPEN', 'EXPOSED')",
            (row["intent_id"],),
        )
        return True

    def record_manual_close_fill(self, close_id: str, *, receipt: dict[str, Any], now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_close_orders
               SET state = 'FILLED', receipt = %s::jsonb, updated_at_ms = %s
             WHERE close_id = %s AND state = 'SUBMITTING'
            RETURNING intent_id, session_id::text AS session_id, requested_bps, client_order_id
            """,
            (_dumps(receipt), int(now_ms), close_id),
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            "UPDATE trading_manual_positions p SET state = CASE "
            "WHEN i.state = 'EXPOSED' THEN 'EXPOSED' ELSE 'OPEN' END, version = p.version + 1 "
            "FROM trading_manual_intents i WHERE p.intent_id = %s AND p.state = 'CLOSING' "
            "AND i.intent_id = p.intent_id",
            (row["intent_id"],),
        )
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind=ManualTradeEventKind.ORDER_SUBMITTED,
            payload={
                "leg": "manual_close",
                "close_id": close_id,
                "requested_bps": int(row["requested_bps"]),
                "client_id": str(row["client_order_id"]),
                "receipt": receipt,
            },
            now_ms=now_ms,
        )
        return True

    def reconcile_manual_close_fill(self, close_id: str, *, receipt: dict[str, Any], now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_close_orders
               SET receipt = %s::jsonb, updated_at_ms = %s
             WHERE close_id = %s AND state = 'FILLED'
               AND (receipt ->> 'average_price' IS NULL
                    OR (receipt ->> 'average_price')::numeric <= 0)
            RETURNING session_id::text AS session_id, requested_bps, client_order_id
            """,
            (_dumps(receipt), int(now_ms), close_id),
        ).fetchone()
        if row is None:
            return False
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind=ManualTradeEventKind.ORDER_RECONCILED,
            payload={
                "leg": "manual_close",
                "close_id": close_id,
                "requested_bps": int(row["requested_bps"]),
                "client_id": str(row["client_order_id"]),
                "receipt": receipt,
                "reconciliation": "filled_price",
            },
            now_ms=now_ms,
        )
        return True

    def record_manual_partial_close_reconciled(
        self,
        close_id: str,
        *,
        remaining_quantity: Decimal,
        mark_price: Decimal,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_close_orders
               SET reconciled_at_ms = %s
             WHERE close_id = %s AND state = 'FILLED' AND reconciled_at_ms IS NULL
            RETURNING session_id::text AS session_id, requested_bps, receipt
            """,
            (int(now_ms), close_id),
        ).fetchone()
        if row is None:
            return False
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind=ManualTradeEventKind.ORDER_RECONCILED,
            payload={
                "leg": "manual_close",
                "close_id": close_id,
                "requested_bps": int(row["requested_bps"]),
                "remaining_quantity": str(remaining_quantity),
                "mark_price": str(mark_price),
                "protection_mode": "close_position",
                "take_profit_active": True,
                "stop_loss_active": True,
            },
            now_ms=now_ms,
        )
        return True

    def settle_manual_close_failure(
        self,
        close_id: str,
        *,
        state: Literal["AMBIGUOUS", "REJECTED"],
        error_code: str,
        now_ms: int,
    ) -> bool:
        normalized = str(error_code or "").strip()
        if not normalized or len(normalized) > 160:
            return False
        row = self.conn.execute(
            """
            UPDATE trading_manual_close_orders
               SET state = %s, error_code = %s, updated_at_ms = %s
             WHERE close_id = %s AND state IN ('PENDING', 'SUBMITTING')
            RETURNING intent_id, session_id::text AS session_id
            """,
            (state, normalized, int(now_ms), close_id),
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            "UPDATE trading_manual_positions p SET state = CASE "
            "WHEN %s = 'AMBIGUOUS' THEN 'MANUAL_REVIEW' "
            "WHEN i.state = 'EXPOSED' THEN 'EXPOSED' ELSE 'OPEN' END, "
            "last_error_code = %s, version = p.version + 1 "
            "FROM trading_manual_intents i WHERE p.intent_id = %s "
            "AND p.state IN ('OPEN', 'EXPOSED', 'CLOSING') AND i.intent_id = p.intent_id",
            (state, normalized, row["intent_id"]),
        )
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind=(
                ManualTradeEventKind.ORDER_AMBIGUOUS if state == "AMBIGUOUS" else ManualTradeEventKind.ORDER_REJECTED
            ),
            payload={"leg": "manual_close", "close_id": close_id, "error_code": normalized},
            now_ms=now_ms,
        )
        return True

    def begin_manual_protection_cancel(
        self,
        intent_id: str,
        *,
        leg: Literal["take_profit", "stop_loss"],
        now_ms: int,
    ) -> bool:
        prefix = _manual_protection_prefix(leg)
        row = self.conn.execute(
            f"""
            UPDATE trading_manual_positions
               SET {prefix}_cancel_attempted_at_ms = %s, version = version + 1
             WHERE intent_id = %s AND state = 'CLOSING'
               AND {prefix}_cancel_attempted_at_ms IS NULL
            RETURNING intent_id
            """,
            (int(now_ms), intent_id),
        ).fetchone()
        return row is not None

    def record_manual_protection_cancelled(
        self,
        intent_id: str,
        *,
        leg: Literal["take_profit", "stop_loss"],
        now_ms: int,
    ) -> bool:
        prefix = _manual_protection_prefix(leg)
        row = self.conn.execute(
            f"""
            UPDATE trading_manual_positions
               SET {prefix}_cancelled_at_ms = %s, version = version + 1
             WHERE intent_id = %s AND state = 'CLOSING'
               AND {prefix}_cancel_attempted_at_ms IS NOT NULL
               AND {prefix}_cancelled_at_ms IS NULL
            RETURNING intent_id
            """,
            (int(now_ms), intent_id),
        ).fetchone()
        return row is not None

    def close_manual_position(
        self,
        intent_id: str,
        *,
        exit_reason: str,
        exit_price: Decimal,
        realized_pnl_usd: Decimal,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_positions
               SET state = 'CLOSED', quantity = 0, mark_price = %s,
                   unrealized_pnl_usd = 0, closed_at_ms = %s, exit_reason = %s,
                   exit_price = %s, realized_pnl_usd = %s,
                   observed_at_ms = %s, version = version + 1
             WHERE intent_id = %s AND state = 'CLOSING'
               AND take_profit_cancelled_at_ms IS NOT NULL
               AND stop_loss_cancelled_at_ms IS NOT NULL
            RETURNING session_id::text AS session_id, symbol, side, entry_price, opened_at_ms
            """,
            (exit_price, int(now_ms), exit_reason, exit_price, realized_pnl_usd, int(now_ms), intent_id),
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            "UPDATE trading_manual_sessions SET state = 'CLOSED', version = version + 1, updated_at_ms = %s "
            "WHERE session_id = %s::uuid AND state IN ('OPEN', 'EXPOSED')",
            (int(now_ms), row["session_id"]),
        )
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind=ManualTradeEventKind.POSITION_CLOSED,
            payload={
                "symbol": str(row["symbol"]),
                "side": str(row["side"]),
                "entry_price": str(row["entry_price"]),
                "exit_reason": exit_reason,
                "exit_price": str(exit_price),
                "realized_pnl_usd": str(realized_pnl_usd),
                "holding_time_ms": int(now_ms) - int(row["opened_at_ms"]),
            },
            now_ms=now_ms,
        )
        return True

    def mark_manual_position_review(self, intent_id: str, *, error_code: str, now_ms: int) -> bool:
        normalized = str(error_code or "").strip()
        if not normalized or len(normalized) > 160:
            return False
        row = self.conn.execute(
            """
            UPDATE trading_manual_positions
               SET state = 'MANUAL_REVIEW', last_error_code = %s,
                   observed_at_ms = GREATEST(observed_at_ms, %s), version = version + 1
             WHERE intent_id = %s AND state IN ('OPEN', 'CLOSING')
            RETURNING session_id::text AS session_id
            """,
            (normalized, int(now_ms), intent_id),
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            "UPDATE trading_manual_sessions SET state = 'EXPOSED', version = version + 1, updated_at_ms = %s "
            "WHERE session_id = %s::uuid AND state = 'OPEN'",
            (int(now_ms), row["session_id"]),
        )
        return True

    def assert_manual_live_cutover_ready(self) -> None:
        row = self.conn.execute(
            """
            SELECT 1
              FROM trading_manual_intents
             WHERE state <> 'TERMINAL'
               AND payload ->> 'venue' IS DISTINCT FROM 'binance_usdm_live'
             LIMIT 1
            """
        ).fetchone()
        if row is not None:
            raise RuntimeError("manual_executor_live_cutover_blocked")

    def fence_manual_entry(self, intent_id: str, *, plan: ManualExecutionPlan, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_intents
               SET state = 'SUBMITTING', execution_plan = %s::jsonb,
                   entry_client_order_id = %s, entry_fenced_at_ms = %s, updated_at_ms = %s
             WHERE intent_id = %s AND state = 'PENDING'
            RETURNING session_id::text AS session_id
            """,
            (
                _dumps(plan.model_dump(mode="json")),
                plan.entry_client_order_id,
                int(now_ms),
                int(now_ms),
                intent_id,
            ),
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            """
            UPDATE trading_manual_sessions
               SET state = 'SUBMITTING', version = version + 1, updated_at_ms = %s
             WHERE session_id = %s::uuid AND state = 'CONFIRMED'
            """,
            (int(now_ms), row["session_id"]),
        )
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind="ORDER_FENCED",
            payload={"leg": "entry", "client_id": plan.entry_client_order_id, "plan": plan.model_dump(mode="json")},
            now_ms=now_ms,
        )
        return True

    def begin_manual_order_attempt(
        self,
        intent_id: str,
        *,
        leg: Literal["execution_setting", "entry", "take_profit", "stop_loss"],
        now_ms: int,
    ) -> bool:
        prefix = (
            "execution_setting"
            if leg == "execution_setting"
            else "entry"
            if leg == "entry"
            else _manual_protection_prefix(leg)
        )
        prerequisites = (
            "execution_plan IS NOT NULL"
            if leg == "execution_setting"
            else "execution_setting_applied_at_ms IS NOT NULL AND entry_client_order_id IS NOT NULL"
            if leg == "entry"
            else f"entry_receipt IS NOT NULL AND {prefix}_client_order_id IS NOT NULL"
        )
        row = self.conn.execute(
            f"""
            UPDATE trading_manual_intents
               SET {prefix}_attempted_at_ms = %s, updated_at_ms = %s
             WHERE intent_id = %s AND state IN ('SUBMITTING', 'AMBIGUOUS')
               AND {prerequisites} AND {prefix}_attempted_at_ms IS NULL
            RETURNING intent_id
            """,
            (int(now_ms), int(now_ms), intent_id),
        ).fetchone()
        return row is not None

    def record_manual_execution_setting(self, intent_id: str, *, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_intents
               SET state = 'SUBMITTING', execution_setting_applied_at_ms = %s,
                   outcome = NULL, updated_at_ms = %s
             WHERE intent_id = %s AND state IN ('SUBMITTING', 'AMBIGUOUS')
               AND execution_plan IS NOT NULL AND execution_setting_applied_at_ms IS NULL
            RETURNING session_id::text AS session_id
            """,
            (int(now_ms), int(now_ms), intent_id),
        ).fetchone()
        if row is None:
            return False
        self._restore_manual_session_submitting(str(row["session_id"]), now_ms=now_ms)
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind="ORDER_FENCED",
            payload={"leg": "execution_setting", "status": "applied"},
            now_ms=now_ms,
        )
        return True

    def record_manual_entry(self, intent_id: str, *, receipt: dict[str, Any], now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_manual_intents
               SET state = 'SUBMITTING', entry_submitted_at_ms = %s, entry_receipt = %s::jsonb,
                   outcome = NULL, updated_at_ms = %s
             WHERE intent_id = %s AND state IN ('SUBMITTING', 'AMBIGUOUS')
               AND entry_attempted_at_ms IS NOT NULL AND entry_receipt IS NULL
            RETURNING session_id::text AS session_id, entry_client_order_id, payload
            """,
            (int(now_ms), _dumps(receipt), int(now_ms), intent_id),
        ).fetchone()
        if row is None:
            return False
        self._restore_manual_session_submitting(str(row["session_id"]), now_ms=now_ms)
        intent = ManualTradeIntent.model_validate(row["payload"])
        payload = {
            "leg": "entry",
            "symbol": intent.source.base_symbol,
            "side": intent.source.side.value,
            "client_id": row["entry_client_order_id"],
            "receipt": receipt,
        }
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind="ORDER_SUBMITTED",
            payload=payload,
            now_ms=now_ms,
        )
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind="POSITION_OPENED",
            payload=payload,
            now_ms=now_ms,
        )
        return True

    def fence_manual_protection(
        self,
        intent_id: str,
        *,
        leg: Literal["take_profit", "stop_loss"],
        client_id: str,
        now_ms: int,
    ) -> bool:
        prefix = _manual_protection_prefix(leg)
        row = self.conn.execute(
            f"""
            UPDATE trading_manual_intents
               SET {prefix}_client_order_id = %s, {prefix}_fenced_at_ms = %s, updated_at_ms = %s
             WHERE intent_id = %s AND state IN ('SUBMITTING', 'AMBIGUOUS')
               AND entry_receipt IS NOT NULL AND {prefix}_client_order_id IS NULL
            RETURNING session_id::text AS session_id
            """,
            (client_id, int(now_ms), int(now_ms), intent_id),
        ).fetchone()
        if row is None:
            return False
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind="ORDER_FENCED",
            payload={"leg": leg, "client_id": client_id},
            now_ms=now_ms,
        )
        return True

    def record_manual_protection(
        self,
        intent_id: str,
        *,
        leg: Literal["take_profit", "stop_loss"],
        receipt: dict[str, Any],
        now_ms: int,
    ) -> bool:
        prefix = _manual_protection_prefix(leg)
        row = self.conn.execute(
            f"""
            UPDATE trading_manual_intents
               SET state = 'SUBMITTING', {prefix}_submitted_at_ms = %s,
                   {prefix}_receipt = %s::jsonb, outcome = NULL, updated_at_ms = %s
             WHERE intent_id = %s AND state IN ('SUBMITTING', 'AMBIGUOUS')
               AND {prefix}_client_order_id IS NOT NULL
               AND {prefix}_attempted_at_ms IS NOT NULL AND {prefix}_receipt IS NULL
            RETURNING session_id::text AS session_id, {prefix}_client_order_id AS client_id
            """,
            (int(now_ms), _dumps(receipt), int(now_ms), intent_id),
        ).fetchone()
        if row is None:
            return False
        self._restore_manual_session_submitting(str(row["session_id"]), now_ms=now_ms)
        event_kind = "TP_CREATED" if leg == "take_profit" else "SL_CREATED"
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind=event_kind,
            payload={"leg": leg, "client_id": row["client_id"], "receipt": receipt},
            now_ms=now_ms,
        )
        receipts = self.conn.execute(
            """
            SELECT entry_receipt, take_profit_receipt, stop_loss_receipt
              FROM trading_manual_intents
             WHERE intent_id = %s
            """,
            (intent_id,),
        ).fetchone()
        receipt_keys = ("entry_receipt", "take_profit_receipt", "stop_loss_receipt")
        if receipts is None or any(receipts[key] is None for key in receipt_keys):
            return True
        outcome = ManualTradeOutcome(
            state=ManualTradeOutcomeState.OPEN,
            entry=_manual_order_receipt(receipts["entry_receipt"]),
            take_profit=_manual_order_receipt(receipts["take_profit_receipt"]),
            stop_loss=_manual_order_receipt(receipts["stop_loss_receipt"]),
        )
        complete = self.conn.execute(
            """
            UPDATE trading_manual_intents
               SET state = 'OPEN', outcome = %s::jsonb, updated_at_ms = %s
             WHERE intent_id = %s AND state = 'SUBMITTING'
               AND entry_receipt IS NOT NULL
               AND take_profit_receipt IS NOT NULL AND stop_loss_receipt IS NOT NULL
            RETURNING session_id::text AS session_id
            """,
            (_dumps(outcome.model_dump(mode="json")), int(now_ms), intent_id),
        ).fetchone()
        if complete is not None:
            self.conn.execute(
                """
                UPDATE trading_manual_sessions
                   SET state = 'OPEN', version = version + 1, updated_at_ms = %s
                 WHERE session_id = %s::uuid AND state = 'SUBMITTING'
                """,
                (int(now_ms), complete["session_id"]),
            )
        return True

    def mark_manual_order_ambiguous(
        self,
        intent_id: str,
        *,
        leg: Literal["execution_setting", "entry", "take_profit", "stop_loss"],
        error_code: str,
        now_ms: int,
    ) -> bool:
        normalized_error = str(error_code or "").strip()
        if not normalized_error or len(normalized_error) > 160:
            return False
        row = self.conn.execute(
            """
            UPDATE trading_manual_intents
               SET state = 'AMBIGUOUS', outcome = %s::jsonb, updated_at_ms = %s
             WHERE intent_id = %s AND state = 'SUBMITTING'
            RETURNING session_id::text AS session_id
            """,
            (
                _dumps(
                    ManualTradeOutcome(
                        state=ManualTradeOutcomeState.AMBIGUOUS,
                        leg=leg,
                        error_code=normalized_error,
                    ).model_dump(mode="json")
                ),
                int(now_ms),
                intent_id,
            ),
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            """
            UPDATE trading_manual_sessions
               SET state = 'AMBIGUOUS', version = version + 1, updated_at_ms = %s
             WHERE session_id = %s::uuid AND state = 'SUBMITTING'
            """,
            (int(now_ms), row["session_id"]),
        )
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind="ORDER_AMBIGUOUS",
            payload={"leg": leg, "error_code": normalized_error},
            now_ms=now_ms,
        )
        return True

    def reject_manual_order(
        self,
        intent_id: str,
        *,
        leg: Literal["execution_setting", "entry", "take_profit", "stop_loss"],
        error_code: str,
        now_ms: int,
    ) -> bool:
        normalized_error = str(error_code or "").strip()
        if not normalized_error or len(normalized_error) > 160:
            return False
        outcome = ManualTradeOutcome(
            state=ManualTradeOutcomeState.REJECTED,
            leg=leg,
            error_code=normalized_error,
        )
        row = self.conn.execute(
            """
            UPDATE trading_manual_intents
               SET state = 'TERMINAL', outcome = %s::jsonb, updated_at_ms = %s
             WHERE intent_id = %s AND state IN ('PENDING', 'SUBMITTING', 'AMBIGUOUS')
            RETURNING session_id::text AS session_id
            """,
            (_dumps(outcome.model_dump(mode="json")), int(now_ms), intent_id),
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            """
            UPDATE trading_manual_sessions
               SET state = 'REJECTED', version = version + 1, updated_at_ms = %s
             WHERE session_id = %s::uuid AND state IN ('CONFIRMED', 'SUBMITTING', 'AMBIGUOUS')
            """,
            (int(now_ms), row["session_id"]),
        )
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind=ManualTradeEventKind.ORDER_REJECTED,
            payload={"leg": leg, "error_code": normalized_error},
            now_ms=now_ms,
        )
        return True

    def mark_manual_position_exposed(
        self,
        intent_id: str,
        *,
        leg: Literal["take_profit", "stop_loss"],
        error_code: str,
        now_ms: int,
    ) -> bool:
        normalized_error = str(error_code or "").strip()
        if not normalized_error or len(normalized_error) > 160:
            return False
        outcome = ManualTradeOutcome(
            state=ManualTradeOutcomeState.EXPOSED,
            leg=leg,
            error_code=normalized_error,
        )
        row = self.conn.execute(
            """
            UPDATE trading_manual_intents
               SET state = 'EXPOSED', outcome = %s::jsonb, updated_at_ms = %s
             WHERE intent_id = %s AND state IN ('SUBMITTING', 'AMBIGUOUS')
               AND entry_receipt IS NOT NULL
            RETURNING session_id::text AS session_id
            """,
            (_dumps(outcome.model_dump(mode="json")), int(now_ms), intent_id),
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            """
            UPDATE trading_manual_sessions
               SET state = 'EXPOSED', version = version + 1, updated_at_ms = %s
             WHERE session_id = %s::uuid AND state IN ('SUBMITTING', 'AMBIGUOUS')
            """,
            (int(now_ms), row["session_id"]),
        )
        self._append_manual_event(
            session_id=str(row["session_id"]),
            event_kind=ManualTradeEventKind.PROTECTION_REJECTED,
            payload={"leg": leg, "error_code": normalized_error, "exposure_requires_action": True},
            now_ms=now_ms,
        )
        return True

    def _append_manual_event(
        self,
        *,
        session_id: str,
        event_kind: ManualTradeEventKind | str,
        payload: dict[str, Any],
        now_ms: int,
    ) -> None:
        row = self.conn.execute(
            "SELECT COALESCE(max(event_index), 0) + 1 AS event_index "
            "FROM trading_manual_events WHERE session_id = %s::uuid",
            (session_id,),
        ).fetchone()
        event_index = int(row["event_index"])
        event = create_manual_trade_event(
            session_id=session_id,
            event_index=event_index,
            event_kind=ManualTradeEventKind(event_kind),
            payload=payload,
            created_at_ms=int(now_ms),
        )
        self.conn.execute(
            """
            INSERT INTO trading_manual_events (
              event_id, session_id, event_index, event_kind, payload, payload_sha256, created_at_ms
            ) VALUES (%s, %s::uuid, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                event.event_id,
                event.session_id,
                event.event_index,
                event.event_kind.value,
                _dumps(event.payload),
                event.payload_sha256,
                event.created_at_ms,
            ),
        )
        if event.event_kind in {
            ManualTradeEventKind.ORDER_REJECTED,
            ManualTradeEventKind.PROTECTION_REJECTED,
            ManualTradeEventKind.ORDER_AMBIGUOUS,
            ManualTradeEventKind.POSITION_OPENED,
            ManualTradeEventKind.TP_CREATED,
            ManualTradeEventKind.SL_CREATED,
            ManualTradeEventKind.POSITION_CLOSED,
        }:
            session = self.conn.execute(
                "SELECT source_message_id FROM trading_manual_sessions WHERE session_id = %s::uuid",
                (session_id,),
            ).fetchone()
            if session is None:
                raise RuntimeError("manual_notification_session_missing")
            notification_id = canonical_sha256(
                {
                    "version": "manual_trading_notification_v1",
                    "event_id": event.event_id,
                    "notification_kind": event.event_kind,
                }
            )
            self.conn.execute(
                """
                INSERT INTO trading_manual_notifications (
                  notification_id, event_id, session_id, source_message_id,
                  notification_kind, payload
                ) VALUES (%s, %s, %s::uuid, %s, %s, %s::jsonb)
                """,
                (
                    notification_id,
                    event.event_id,
                    session_id,
                    int(session["source_message_id"]),
                    event.event_kind.value,
                    _dumps(event.payload),
                ),
            )

    def _restore_manual_session_submitting(self, session_id: str, *, now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE trading_manual_sessions
               SET state = 'SUBMITTING', version = version + 1, updated_at_ms = %s
             WHERE session_id = %s::uuid AND state = 'AMBIGUOUS'
            """,
            (int(now_ms), session_id),
        )


def _manual_session(row: Any) -> ManualTradeSession:
    values = dict(row)
    return ManualTradeSession.model_validate(values)


def _manual_target_picker(row: Any) -> ManualTargetPicker:
    values = dict(row)
    return ManualTargetPicker.model_validate(values)


def _manual_execution_row(row: Any) -> ManualExecutionRecord:
    values = dict(row)
    plan = None
    if values.get("execution_plan") is not None:
        plan = ManualExecutionPlan.model_validate(values["execution_plan"])
    return ManualExecutionRecord(
        intent=ManualTradeIntent.model_validate(values["payload"]),
        state=values["state"],
        plan=plan,
        execution_setting_attempted=values["execution_setting_attempted_at_ms"] is not None,
        execution_setting_applied=values["execution_setting_applied_at_ms"] is not None,
        entry_attempted=values["entry_attempted_at_ms"] is not None,
        entry_confirmed=values["entry_receipt"] is not None,
        take_profit=ManualProtectionExecutionRecord(
            client_order_id=values["take_profit_client_order_id"],
            attempted=values["take_profit_attempted_at_ms"] is not None,
            confirmed=values["take_profit_receipt"] is not None,
        ),
        stop_loss=ManualProtectionExecutionRecord(
            client_order_id=values["stop_loss_client_order_id"],
            attempted=values["stop_loss_attempted_at_ms"] is not None,
            confirmed=values["stop_loss_receipt"] is not None,
        ),
    )


def _manual_close_request(row: Any) -> ManualCloseRequest:
    return ManualCloseRequest.model_validate(dict(row))


def _manual_position_view(row: Any) -> ManualPositionView:
    values = dict(row)
    active_close = values.get("active_close")
    return ManualPositionView(
        intent_id=str(values["intent_id"]),
        session_id=str(values["session_id"]),
        source=ManualTradeSource.model_validate(values["source"]),
        account_ref=str(values["account_ref"]),
        symbol=str(values["symbol"]),
        side=str(values["side"]),
        preset=str(values["preset"]),
        recommended=ManualTradeParameters.model_validate(values["recommended"]),
        selected=ManualTradeParameters.model_validate(values["selected"]),
        state=ManualPositionState(str(values["state"])),
        quantity=values["quantity"],
        entry_price=values["entry_price"],
        mark_price=values["mark_price"],
        unrealized_pnl_usd=values["unrealized_pnl_usd"],
        leverage=int(values["leverage"]),
        liquidation_price=values.get("liquidation_price"),
        take_profit_price=values["take_profit_price"],
        stop_loss_price=values["stop_loss_price"],
        opened_at_ms=int(values["opened_at_ms"]),
        observed_at_ms=int(values["observed_at_ms"]),
        closed_at_ms=values.get("closed_at_ms"),
        exit_reason=values.get("exit_reason"),
        exit_price=values.get("exit_price"),
        realized_pnl_usd=values.get("realized_pnl_usd"),
        active_close=None if active_close is None else ManualCloseRequest.model_validate(active_close),
    )


def _manual_order_receipt(payload: Any) -> ManualVenueOrderReceipt:
    if not isinstance(payload, dict):
        raise ValueError("manual_order_receipt_invalid")
    return ManualVenueOrderReceipt(
        client_id=str(payload["client_id"]),
        provider_id=str(payload["provider_id"]),
        status=str(payload["status"]),
        executed_quantity=(
            None if payload.get("executed_quantity") is None else Decimal(str(payload["executed_quantity"]))
        ),
        average_price=None if payload.get("average_price") is None else Decimal(str(payload["average_price"])),
    )


def _manual_protection_prefix(leg: Literal["take_profit", "stop_loss"]) -> str:
    if leg not in {"take_profit", "stop_loss"}:
        raise ValueError("manual_protection_leg_invalid")
    return leg


def _manual_notification_effect_prefix(effect: Literal["interaction", "reply"]) -> str:
    if effect not in {"interaction", "reply"}:
        raise ValueError("manual_notification_effect_invalid")
    return effect


def _manual_notification_error(error_code: str) -> str:
    normalized_error = str(error_code or "").strip()
    if not normalized_error or len(normalized_error) > 160:
        raise ValueError("manual_notification_error_invalid")
    return normalized_error


__all__ = ["ManualStorage"]
