"""Durable state transitions for Telegram onchain route analysis."""

from __future__ import annotations

from typing import Any

from ..onchain import (
    OnchainAnalysisSession,
    OnchainAnalysisState,
    OnchainAssetCandidate,
    OnchainNewsSource,
    OnchainRouteAnalysis,
    OnchainTelegramEditEffect,
    OnchainTelegramEditPayload,
)
from ..onchain_execution import (
    OnchainExecutionIntent,
    OnchainExecutionPlan,
    OnchainExecutionState,
    OnchainSignedTransaction,
    onchain_wallet_fingerprint,
)
from .sql_values import _dumps

_ONCHAIN_EXECUTOR_LOCK = 0x5452464F  # TRFO

_SESSION_COLUMNS = """
session_id::text AS session_id, sources, actor_user_id, chat_id, source_message_id,
interaction_message_id, interaction_reply_attempted_at_ms, interaction_reply_state,
interaction_reply_error_code, state, selected_ticker,
candidates, selected_candidate, analysis, provider_errors, created_at_ms, updated_at_ms
"""

_EXECUTION_COLUMNS = """
intent.execution_id::text AS execution_id, intent.session_id::text AS session_id,
intent.actor_user_id, intent.chat_id, intent.interaction_message_id, intent.provider,
intent.wallet_address, intent.wallet_fingerprint, intent.request, intent.quote,
intent.state, intent.confirmation_update_id, intent.plan, intent.error_code,
intent.created_at_ms, intent.confirmed_at_ms, intent.updated_at_ms,
approval.signed_transaction AS approval_transaction,
approval.state AS approval_transaction_state,
swap.signed_transaction AS swap_transaction,
swap.state AS swap_transaction_state
"""

_EXECUTION_STATUS_COLUMNS = """
intent.execution_id::text AS execution_id, intent.session_id::text AS session_id,
intent.actor_user_id, intent.chat_id, intent.interaction_message_id, intent.provider,
intent.wallet_address, intent.wallet_fingerprint, intent.request, intent.quote,
intent.state, intent.confirmation_update_id, intent.plan, intent.error_code,
intent.created_at_ms, intent.confirmed_at_ms, intent.updated_at_ms,
CASE WHEN approval.execution_id IS NULL THEN NULL ELSE jsonb_build_object(
  'provider', intent.provider, 'leg', 'approval',
  'chain_id', (intent.request ->> 'chain_id')::bigint,
  'wallet_address', intent.wallet_address,
  'transaction_hash', approval.transaction_hash
) END AS approval_transaction,
approval.state AS approval_transaction_state,
CASE WHEN swap.execution_id IS NULL THEN NULL ELSE jsonb_build_object(
  'provider', intent.provider, 'leg', 'swap',
  'chain_id', (intent.request ->> 'chain_id')::bigint,
  'wallet_address', intent.wallet_address,
  'transaction_hash', swap.transaction_hash
) END AS swap_transaction,
swap.state AS swap_transaction_state
"""


class OnchainStorage:
    conn: Any

    def begin_onchain_analysis_session(
        self,
        *,
        session_id: str,
        sources: tuple[OnchainNewsSource, ...],
        selected_ticker: str | None,
        actor_user_id: int,
        chat_id: int,
        now_ms: int,
    ) -> tuple[OnchainAnalysisSession, bool]:
        if not sources or any(source.delivery_message_id != sources[0].delivery_message_id for source in sources):
            raise ValueError("onchain_session_sources_invalid")
        state = OnchainAnalysisState.RESOLVING if selected_ticker is not None else OnchainAnalysisState.AWAITING_TICKER
        row = self.conn.execute(
            f"""
            INSERT INTO trading_onchain_analysis_sessions (
              session_id, sources, actor_user_id, chat_id, source_message_id,
              state, selected_ticker, candidates, provider_errors, created_at_ms, updated_at_ms
            ) VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s, '[]'::jsonb, '[]'::jsonb, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING {_SESSION_COLUMNS}
            """,
            (
                session_id,
                _dumps([source.model_dump(mode="json") for source in sources]),
                int(actor_user_id),
                int(chat_id),
                sources[0].delivery_message_id,
                state,
                selected_ticker,
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        if row is not None:
            return _session(row), True
        existing = self.conn.execute(
            f"""
            SELECT {_SESSION_COLUMNS}
              FROM trading_onchain_analysis_sessions
             WHERE chat_id = %s AND actor_user_id = %s AND source_message_id = %s
               AND state <> 'CANCELLED'
            """,
            (int(chat_id), int(actor_user_id), sources[0].delivery_message_id),
        ).fetchone()
        if existing is None:
            raise ValueError("onchain_session_conflict")
        parsed = _session(existing)
        if parsed.sources != sources:
            raise ValueError("onchain_session_sources_conflict")
        return parsed, False

    def begin_onchain_interaction_reply(self, session_id: str, *, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_onchain_analysis_sessions
               SET interaction_reply_state = 'SENDING', interaction_reply_attempted_at_ms = %s,
                   updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE session_id = %s AND interaction_reply_state = 'PENDING'
               AND interaction_reply_attempted_at_ms IS NULL AND interaction_message_id IS NULL
            RETURNING session_id
            """,
            (int(now_ms), int(now_ms), session_id),
        ).fetchone()
        return row is not None

    def attach_onchain_interaction_message(self, session_id: str, *, message_id: int, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_onchain_analysis_sessions
               SET interaction_message_id = %s, interaction_reply_state = 'SENT',
                   updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE session_id = %s AND interaction_reply_state = 'SENDING'
               AND interaction_reply_attempted_at_ms IS NOT NULL
               AND interaction_message_id IS NULL
            RETURNING session_id
            """,
            (int(message_id), int(now_ms), session_id),
        ).fetchone()
        return row is not None

    def mark_onchain_interaction_reply_ambiguous(
        self,
        session_id: str,
        *,
        error_code: str,
        now_ms: int,
    ) -> bool:
        normalized = str(error_code).strip()
        if not normalized or len(normalized) > 100:
            raise ValueError("onchain_interaction_reply_error_invalid")
        row = self.conn.execute(
            """
            UPDATE trading_onchain_analysis_sessions
               SET interaction_reply_state = 'AMBIGUOUS', interaction_reply_error_code = %s,
                   updated_at_ms = GREATEST(updated_at_ms, %s)
             WHERE session_id = %s AND interaction_reply_state = 'SENDING'
               AND interaction_message_id IS NULL
            RETURNING session_id
            """,
            (normalized, int(now_ms), session_id),
        ).fetchone()
        return row is not None

    def onchain_analysis_session(self, session_id: str) -> OnchainAnalysisSession | None:
        row = self.conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM trading_onchain_analysis_sessions WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        return None if row is None else _session(row)

    def begin_onchain_execution(
        self,
        *,
        execution_id: str,
        session: OnchainAnalysisSession,
        provider: str,
        wallet_address: str,
        request: Any,
        quote: Any,
        now_ms: int,
    ) -> tuple[OnchainExecutionIntent, bool]:
        if session.interaction_message_id is None:
            raise ValueError("onchain_execution_interaction_message_missing")
        row = self.conn.execute(
            """
            INSERT INTO trading_onchain_execution_intents (
              execution_id, session_id, actor_user_id, chat_id, interaction_message_id,
              provider, wallet_address, wallet_fingerprint, request, quote, state,
              created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                      'AWAITING_CONFIRMATION', %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING execution_id::text AS execution_id
            """,
            (
                execution_id,
                session.session_id,
                session.actor_user_id,
                session.chat_id,
                session.interaction_message_id,
                provider,
                wallet_address,
                onchain_wallet_fingerprint(wallet_address),
                _dumps(request.model_dump(mode="json")),
                _dumps(quote.model_dump(mode="json")),
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        execution = self.onchain_execution_for_session(session.session_id)
        if execution is None:
            raise ValueError("onchain_execution_conflict")
        return execution, row is not None

    def onchain_execution_for_session(self, session_id: str) -> OnchainExecutionIntent | None:
        row = self.conn.execute(
            f"""
            SELECT {_EXECUTION_STATUS_COLUMNS}
              FROM trading_onchain_execution_intents AS intent
              LEFT JOIN trading_onchain_signed_transactions AS approval
                ON approval.execution_id = intent.execution_id AND approval.leg = 'approval'
              LEFT JOIN trading_onchain_signed_transactions AS swap
                ON swap.execution_id = intent.execution_id AND swap.leg = 'swap'
             WHERE intent.session_id = %s
            """,
            (session_id,),
        ).fetchone()
        return None if row is None else _execution(row)

    def onchain_execution_for_executor(self, session_id: str) -> OnchainExecutionIntent | None:
        row = self.conn.execute(
            f"""
            SELECT {_EXECUTION_COLUMNS}
              FROM trading_onchain_execution_intents AS intent
              LEFT JOIN trading_onchain_signed_transactions AS approval
                ON approval.execution_id = intent.execution_id AND approval.leg = 'approval'
              LEFT JOIN trading_onchain_signed_transactions AS swap
                ON swap.execution_id = intent.execution_id AND swap.leg = 'swap'
             WHERE intent.session_id = %s
            """,
            (session_id,),
        ).fetchone()
        return None if row is None else _execution(row)

    def confirm_onchain_execution(self, session_id: str, *, update_id: int, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_onchain_execution_intents
               SET state = 'PENDING', confirmation_update_id = %s,
                   confirmed_at_ms = %s, updated_at_ms = %s
             WHERE session_id = %s AND state = 'AWAITING_CONFIRMATION'
            RETURNING execution_id
            """,
            (int(update_id), int(now_ms), int(now_ms), session_id),
        ).fetchone()
        return row is not None

    def cancel_onchain_execution(self, session_id: str, *, update_id: int, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_onchain_execution_intents
               SET state = 'CANCELLED', confirmation_update_id = %s,
                   confirmed_at_ms = %s, updated_at_ms = %s
             WHERE session_id = %s AND state = 'AWAITING_CONFIRMATION'
            RETURNING execution_id
            """,
            (int(update_id), int(now_ms), int(now_ms), session_id),
        ).fetchone()
        return row is not None

    def claim_next_onchain_execution(
        self,
        *,
        wallet_fingerprint: str,
        now_ms: int,
    ) -> OnchainExecutionIntent | None:
        lock = self.conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS locked",
            (_ONCHAIN_EXECUTOR_LOCK,),
        ).fetchone()
        if lock is None or not bool(lock["locked"]):
            return None
        active = self.conn.execute(
            """
            SELECT session_id::text AS session_id
             FROM trading_onchain_execution_intents
             WHERE state IN ('CLAIMED', 'APPROVAL_SUBMITTED', 'SWAP_SUBMITTED')
               AND wallet_fingerprint = %s
             ORDER BY updated_at_ms, execution_id
             LIMIT 1
            """,
            (wallet_fingerprint,),
        ).fetchone()
        if active is not None:
            return self.onchain_execution_for_executor(str(active["session_id"]))
        row = self.conn.execute(
            """
            WITH candidate AS (
              SELECT execution_id
               FROM trading_onchain_execution_intents
               WHERE state = 'PENDING'
                 AND wallet_fingerprint = %s
               ORDER BY confirmed_at_ms, execution_id
               FOR UPDATE SKIP LOCKED
               LIMIT 1
            )
            UPDATE trading_onchain_execution_intents AS intent
               SET state = 'CLAIMED', updated_at_ms = %s
              FROM candidate
             WHERE intent.execution_id = candidate.execution_id
            RETURNING intent.session_id::text AS session_id
            """,
            (wallet_fingerprint, int(now_ms)),
        ).fetchone()
        return None if row is None else self.onchain_execution_for_executor(str(row["session_id"]))

    def record_onchain_executor_heartbeat(
        self,
        *,
        wallet_fingerprint: str,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            INSERT INTO trading_onchain_executor_runtime (
              wallet_fingerprint, started_at_ms, heartbeat_at_ms
            ) VALUES (%s, %s, %s)
            ON CONFLICT (wallet_fingerprint) DO UPDATE
               SET started_at_ms = trading_onchain_executor_runtime.started_at_ms,
                   heartbeat_at_ms = EXCLUDED.heartbeat_at_ms
             WHERE trading_onchain_executor_runtime.heartbeat_at_ms <= EXCLUDED.heartbeat_at_ms
            RETURNING wallet_fingerprint
            """,
            (wallet_fingerprint, int(now_ms), int(now_ms)),
        ).fetchone()
        return row is not None

    def onchain_executor_available(
        self,
        *,
        wallet_fingerprint: str,
        now_ms: int,
        stale_after_ms: int = 15_000,
    ) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 AS available
              FROM trading_onchain_executor_runtime
             WHERE wallet_fingerprint = %s
               AND heartbeat_at_ms BETWEEN %s AND %s
            """,
            (
                wallet_fingerprint,
                int(now_ms) - int(stale_after_ms),
                int(now_ms),
            ),
        ).fetchone()
        return row is not None

    def store_onchain_execution_plan(
        self,
        execution_id: str,
        *,
        plan: OnchainExecutionPlan,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_onchain_execution_intents
               SET plan = %s::jsonb, updated_at_ms = %s
             WHERE execution_id = %s AND state = 'CLAIMED' AND plan IS NULL
            RETURNING execution_id
            """,
            (_dumps(plan.model_dump(mode="json")), int(now_ms), execution_id),
        ).fetchone()
        return row is not None

    def append_onchain_signed_transaction(
        self,
        execution_id: str,
        *,
        signed: OnchainSignedTransaction,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            INSERT INTO trading_onchain_signed_transactions (
              execution_id, leg, signed_transaction, transaction_hash, state, signed_at_ms
            ) VALUES (%s, %s, %s::jsonb, %s, 'SIGNED', %s)
            ON CONFLICT DO NOTHING
            RETURNING execution_id
            """,
            (
                execution_id,
                signed.leg,
                _dumps(signed.model_dump(mode="json")),
                signed.transaction_hash,
                int(now_ms),
            ),
        ).fetchone()
        return row is not None

    def settle_onchain_signed_transaction(
        self,
        execution_id: str,
        *,
        leg: str,
        state: str,
        now_ms: int,
        receipt: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> bool:
        if state not in {"SUBMITTED", "CONFIRMED", "FAILED", "AMBIGUOUS"}:
            raise ValueError("onchain_signed_transaction_state_invalid")
        row = self.conn.execute(
            """
            UPDATE trading_onchain_signed_transactions
               SET state = %s,
                   receipt = %s::jsonb,
                   error_code = %s,
                   submitted_at_ms = CASE WHEN %s = 'SUBMITTED' THEN %s ELSE submitted_at_ms END,
                   settled_at_ms = CASE WHEN %s IN ('CONFIRMED', 'FAILED', 'AMBIGUOUS') THEN %s ELSE NULL END
             WHERE execution_id = %s AND leg = %s
               AND ((state = 'SIGNED' AND %s IN ('SUBMITTED', 'AMBIGUOUS'))
                 OR (state = 'SUBMITTED' AND %s IN ('CONFIRMED', 'FAILED', 'AMBIGUOUS')))
            RETURNING execution_id
            """,
            (
                state,
                None if receipt is None else _dumps(receipt),
                error_code,
                state,
                int(now_ms),
                state,
                int(now_ms),
                execution_id,
                leg,
                state,
                state,
            ),
        ).fetchone()
        return row is not None

    def advance_onchain_execution(
        self,
        execution_id: str,
        *,
        expected_state: OnchainExecutionState,
        state: OnchainExecutionState,
        now_ms: int,
        error_code: str | None = None,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_onchain_execution_intents
               SET state = %s, error_code = %s, updated_at_ms = %s
             WHERE execution_id = %s AND state = %s
            RETURNING execution_id
            """,
            (state, error_code, int(now_ms), execution_id, expected_state),
        ).fetchone()
        return row is not None

    def begin_onchain_resolution(
        self,
        session_id: str,
        *,
        ticker: str,
        now_ms: int,
    ) -> OnchainAnalysisSession | None:
        row = self.conn.execute(
            f"""
            UPDATE trading_onchain_analysis_sessions
               SET state = 'RESOLVING', selected_ticker = %s, candidates = '[]'::jsonb,
                   selected_candidate = NULL, analysis = NULL, provider_errors = '[]'::jsonb,
                   updated_at_ms = %s
             WHERE session_id = %s
               AND (state IN ('AWAITING_TICKER', 'UNAVAILABLE')
                 OR (state = 'RESOLVING' AND selected_ticker = %s))
               AND EXISTS (
                 SELECT 1 FROM jsonb_array_elements(sources) AS source
                  WHERE source ->> 'ticker' = %s
               )
            RETURNING {_SESSION_COLUMNS}
            """,
            (ticker, int(now_ms), session_id, ticker, ticker),
        ).fetchone()
        return None if row is None else _session(row)

    def set_onchain_candidates(
        self,
        session_id: str,
        *,
        candidates: tuple[OnchainAssetCandidate, ...],
        provider_errors: tuple[str, ...],
        now_ms: int,
    ) -> OnchainAnalysisSession | None:
        state = OnchainAnalysisState.AWAITING_CONTRACT if candidates else OnchainAnalysisState.UNAVAILABLE
        row = self.conn.execute(
            f"""
            UPDATE trading_onchain_analysis_sessions
               SET state = %s, candidates = %s::jsonb, provider_errors = %s::jsonb,
                   selected_candidate = NULL, analysis = NULL, updated_at_ms = %s
             WHERE session_id = %s AND state = 'RESOLVING'
            RETURNING {_SESSION_COLUMNS}
            """,
            (
                state,
                _dumps([candidate.model_dump(mode="json") for candidate in candidates]),
                _dumps(list(provider_errors)),
                int(now_ms),
                session_id,
            ),
        ).fetchone()
        return None if row is None else _session(row)

    def set_onchain_candidates_and_begin_edit(
        self,
        session_id: str,
        *,
        candidates: tuple[OnchainAssetCandidate, ...],
        provider_errors: tuple[str, ...],
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> tuple[OnchainAnalysisSession, OnchainTelegramEditEffect] | None:
        session = self.set_onchain_candidates(
            session_id,
            candidates=candidates,
            provider_errors=provider_errors,
            now_ms=now_ms,
        )
        if session is None:
            return None
        effect = self._begin_onchain_telegram_edit(
            session_id,
            update_id=update_id,
            payload=payload,
            result_code=result_code,
            now_ms=now_ms,
        )
        return session, effect

    def begin_onchain_quote(
        self,
        session_id: str,
        *,
        candidate_index: int | None,
        now_ms: int,
    ) -> OnchainAnalysisSession | None:
        if candidate_index is None:
            selected_sql = "selected_candidate"
            state_clause = "state IN ('ANALYZED', 'QUOTING') AND selected_candidate IS NOT NULL"
            params: tuple[object, ...] = (int(now_ms), session_id)
        else:
            if not 0 <= candidate_index <= 5:
                return None
            selected_sql = "candidates -> %s"
            state_clause = (
                "((state = 'AWAITING_CONTRACT' AND jsonb_array_length(candidates) > %s) "
                "OR (state = 'QUOTING' AND selected_candidate = candidates -> %s))"
            )
            params = (candidate_index, int(now_ms), session_id, candidate_index, candidate_index)
        row = self.conn.execute(
            f"""
            UPDATE trading_onchain_analysis_sessions
               SET state = 'QUOTING', selected_candidate = {selected_sql}, analysis = NULL, updated_at_ms = %s
             WHERE session_id = %s AND {state_clause}
            RETURNING {_SESSION_COLUMNS}
            """,
            params,
        ).fetchone()
        return None if row is None else _session(row)

    def set_onchain_analysis(
        self,
        session_id: str,
        *,
        analysis: OnchainRouteAnalysis,
        provider_errors: tuple[str, ...],
        now_ms: int,
    ) -> OnchainAnalysisSession | None:
        state = OnchainAnalysisState.ANALYZED if analysis.winner_provider else OnchainAnalysisState.UNAVAILABLE
        row = self.conn.execute(
            f"""
            UPDATE trading_onchain_analysis_sessions
               SET state = %s, analysis = %s::jsonb, provider_errors = %s::jsonb, updated_at_ms = %s
             WHERE session_id = %s AND state = 'QUOTING'
            RETURNING {_SESSION_COLUMNS}
            """,
            (
                state,
                _dumps(analysis.model_dump(mode="json")),
                _dumps(list(provider_errors)),
                int(now_ms),
                session_id,
            ),
        ).fetchone()
        return None if row is None else _session(row)

    def set_onchain_analysis_and_begin_edit(
        self,
        session_id: str,
        *,
        analysis: OnchainRouteAnalysis,
        provider_errors: tuple[str, ...],
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> tuple[OnchainAnalysisSession, OnchainTelegramEditEffect] | None:
        session = self.set_onchain_analysis(
            session_id,
            analysis=analysis,
            provider_errors=provider_errors,
            now_ms=now_ms,
        )
        if session is None:
            return None
        effect = self._begin_onchain_telegram_edit(
            session_id,
            update_id=update_id,
            payload=payload,
            result_code=result_code,
            now_ms=now_ms,
        )
        return session, effect

    def cancel_onchain_analysis(self, session_id: str, *, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_onchain_analysis_sessions
               SET state = 'CANCELLED', updated_at_ms = %s
             WHERE session_id = %s AND state <> 'CANCELLED'
            RETURNING session_id
            """,
            (int(now_ms), session_id),
        ).fetchone()
        return row is not None

    def cancel_onchain_analysis_and_begin_edit(
        self,
        session_id: str,
        *,
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> OnchainTelegramEditEffect | None:
        if not self.cancel_onchain_analysis(session_id, now_ms=now_ms):
            return None
        return self._begin_onchain_telegram_edit(
            session_id,
            update_id=update_id,
            payload=payload,
            result_code=result_code,
            now_ms=now_ms,
        )

    def onchain_telegram_edit_effect(
        self,
        session_id: str,
        *,
        update_id: int,
    ) -> OnchainTelegramEditEffect | None:
        row = self.conn.execute(
            """
            SELECT session_id::text AS session_id, update_id, payload, result_code,
                   state, error_code, attempted_at_ms, settled_at_ms
              FROM trading_onchain_telegram_edit_effects
             WHERE session_id = %s AND update_id = %s
            """,
            (session_id, int(update_id)),
        ).fetchone()
        return None if row is None else _edit_effect(row)

    def settle_onchain_telegram_edit_sent(
        self,
        session_id: str,
        *,
        update_id: int,
        now_ms: int,
    ) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_onchain_telegram_edit_effects
               SET state = 'SENT', settled_at_ms = %s
             WHERE session_id = %s AND update_id = %s AND state = 'SENDING'
            RETURNING session_id
            """,
            (int(now_ms), session_id, int(update_id)),
        ).fetchone()
        return row is not None

    def settle_onchain_telegram_edit_ambiguous(
        self,
        session_id: str,
        *,
        update_id: int,
        error_code: str,
        now_ms: int,
    ) -> bool:
        normalized = str(error_code).strip()
        if not normalized or len(normalized) > 100:
            raise ValueError("onchain_telegram_edit_error_invalid")
        row = self.conn.execute(
            """
            UPDATE trading_onchain_telegram_edit_effects
               SET state = 'AMBIGUOUS', error_code = %s, settled_at_ms = %s
             WHERE session_id = %s AND update_id = %s AND state = 'SENDING'
            RETURNING session_id
            """,
            (normalized, int(now_ms), session_id, int(update_id)),
        ).fetchone()
        return row is not None

    def begin_onchain_telegram_edit(
        self,
        session_id: str,
        *,
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> OnchainTelegramEditEffect:
        return self._begin_onchain_telegram_edit(
            session_id,
            update_id=update_id,
            payload=payload,
            result_code=result_code,
            now_ms=now_ms,
        )

    def _begin_onchain_telegram_edit(
        self,
        session_id: str,
        *,
        update_id: int,
        payload: OnchainTelegramEditPayload,
        result_code: str,
        now_ms: int,
    ) -> OnchainTelegramEditEffect:
        normalized_result = str(result_code).strip()
        if not normalized_result or len(normalized_result) > 100:
            raise ValueError("onchain_telegram_edit_result_invalid")
        row = self.conn.execute(
            """
            INSERT INTO trading_onchain_telegram_edit_effects (
              session_id, update_id, message_id, payload, result_code, state, attempted_at_ms
            ) VALUES (%s, %s, %s, %s::jsonb, %s, 'SENDING', %s)
            ON CONFLICT DO NOTHING
            RETURNING session_id::text AS session_id, update_id, payload, result_code,
                      state, error_code, attempted_at_ms, settled_at_ms
            """,
            (
                session_id,
                int(update_id),
                payload.message_id,
                _dumps(payload.model_dump(mode="json")),
                normalized_result,
                int(now_ms),
            ),
        ).fetchone()
        if row is None:
            raise ValueError("onchain_telegram_edit_effect_conflict")
        return _edit_effect(row)


def _session(row: Any) -> OnchainAnalysisSession:
    return OnchainAnalysisSession.model_validate(dict(row))


def _edit_effect(row: Any) -> OnchainTelegramEditEffect:
    return OnchainTelegramEditEffect.model_validate(dict(row))


def _execution(row: Any) -> OnchainExecutionIntent:
    return OnchainExecutionIntent.model_validate(dict(row))


__all__ = ["OnchainStorage"]
