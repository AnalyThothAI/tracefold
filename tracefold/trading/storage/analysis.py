"""Durable intake, fenced work and frozen decisions for the Analysis process."""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from tracefold.platform.postgres.client import require_transaction
from tracefold.trading.engine.policy import decision_identity
from tracefold.trading.engine.strategy import ENTRY_WINDOW_MS, MAX_HOLDING_SECONDS
from tracefold.trading.engine.target import TargetSelection
from tracefold.trading.execution_contracts import TradeSignalV2
from tracefold.trading.storage.execution_stream import ExecutionStreamStorage, PreparedTradeSignal


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    ).hexdigest()


_OUTCOME_HORIZONS = (900, 3_600, 14_400, 86_400)
_OUTCOME_VERSION = "price_path_v2"
TRADING_TRIGGER_BY_ID_SQL = (
    "SELECT trigger_id,kind,source_fact_key,source_revision,payload_sha256,payload,"
    "asset_id,target_selection,first_visible_at_ms,source_observed_at_ms,"
    "root_expires_at_ms,supersedes_ref,created_at_ms "
    "FROM trading_triggers WHERE trigger_id=%s"
)
TRADING_ANALYSIS_RUNTIME_SQL = (
    "SELECT heartbeat_at_ms,active_policy,model_name,model_configured,"
    "publish_signals,config_digest FROM trading_analysis_runtime WHERE runtime_id=%s"
)


class AnalysisStorage:
    conn: Any

    def analysis_runtime(self, runtime_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(TRADING_ANALYSIS_RUNTIME_SQL, (runtime_id,)).fetchone()
        return None if row is None else dict(row)

    def heartbeat_analysis_runtime(
        self,
        *,
        runtime_id: str,
        now_ms: int,
        active_policy: str,
        model_name: str | None,
        model_configured: bool,
        publish_signals: bool,
        config_digest: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO trading_analysis_runtime
              (runtime_id,heartbeat_at_ms,active_policy,model_name,model_configured,
               publish_signals,config_digest)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (runtime_id) DO UPDATE SET
              heartbeat_at_ms=EXCLUDED.heartbeat_at_ms,
              active_policy=EXCLUDED.active_policy,
              model_name=EXCLUDED.model_name,
              model_configured=EXCLUDED.model_configured,
              publish_signals=EXCLUDED.publish_signals,
              config_digest=EXCLUDED.config_digest
            """,
            (runtime_id, int(now_ms), active_policy, model_name, model_configured, publish_signals, config_digest),
        )

    def latest_entry_validity_check(self, entry_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT check_version,checked_at_ns,allowed,reason "
            "FROM trading_entry_validity_checks WHERE entry_id=%s "
            "ORDER BY checked_at_ns DESC,check_id DESC LIMIT 1",
            (entry_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def analysis_trigger(self, trigger_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(TRADING_TRIGGER_BY_ID_SQL, (trigger_id,)).fetchone()
        return dict(row) if row is not None else None

    def recent_asset_source_context(
        self,
        *,
        asset_id: str,
        known_at_ms: int,
        exclude_trigger_id: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Bounded same-asset source identities visible when this Case was created.

        Later decisions and outcome labels never enter the Agent's original input.
        """

        if not 1 <= limit <= 8:
            raise ValueError("analysis_source_context_limit_invalid")
        rows = self.conn.execute(
            "SELECT trigger_id,kind,source_fact_key,source_revision,payload_sha256,"
            "source_observed_at_ms,first_visible_at_ms,payload "
            "FROM trading_triggers WHERE asset_id=%s AND trigger_id<>%s "
            "AND first_visible_at_ms<=%s "
            "ORDER BY first_visible_at_ms DESC,trigger_id DESC LIMIT %s",
            (asset_id, exclude_trigger_id, int(known_at_ms), limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def accept_trigger(
        self,
        *,
        kind: str,
        source_fact_key: str,
        source_revision: str,
        payload_sha256: str,
        payload: dict[str, Any],
        selection: TargetSelection,
        now_ms: int,
        root_ttl_ms: int,
    ) -> tuple[str, str, str]:
        """Idempotently record one fact and initial Case in one short transaction.

        Returns (trigger_id, case_id, disposition). A conflicting duplicate
        retains the first fact and records the attempted digest separately.
        """

        if kind not in ("oi", "catalyst") or root_ttl_ms <= 0:
            raise ValueError("analysis_trigger_invalid")
        asset_key = selection.asset_id.key if selection.asset_id is not None else None
        supersedes_ref = payload.get("supersedes_or_revokes_ref")
        affected = {asset_key} if asset_key is not None else set()
        if isinstance(supersedes_ref, str) and supersedes_ref:
            affected.update(
                str(item["asset_id"])
                for item in self.conn.execute(
                    "SELECT asset_id FROM trading_triggers "
                    "WHERE (trigger_id=%s OR source_fact_key=%s) AND asset_id IS NOT NULL",
                    (supersedes_ref, supersedes_ref),
                ).fetchall()
            )
        affected.update(
            str(item["asset_id"])
            for item in self.conn.execute(
                "SELECT asset_id FROM trading_triggers WHERE kind=%s AND source_fact_key=%s AND asset_id IS NOT NULL",
                (kind, source_fact_key),
            ).fetchall()
        )
        for affected_asset in sorted(affected):
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 683))", (affected_asset,))
        source = self.conn.execute(
            """
            SELECT trigger_id, payload_sha256 FROM trading_triggers
             WHERE kind = %s AND source_fact_key = %s AND source_revision = %s
             FOR UPDATE
            """,
            (kind, source_fact_key, source_revision),
        ).fetchone()
        if source is not None:
            trigger_id = str(source["trigger_id"])
            if source["payload_sha256"] != payload_sha256:
                self.conn.execute(
                    """
                    INSERT INTO trading_trigger_conflicts
                      (kind, source_fact_key, source_revision, attempted_sha256,
                       original_sha256, observed_at_ms)
                    VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                    """,
                    (kind, source_fact_key, source_revision, payload_sha256, source["payload_sha256"], int(now_ms)),
                )
                return trigger_id, _sha((trigger_id, "initial", 0)), "source_conflict"
            return trigger_id, _sha((trigger_id, "initial", 0)), "duplicate"

        trigger_id = _sha((kind, source_fact_key, source_revision))
        case_id = _sha((trigger_id, "initial", 0))
        # The timestamp actually frozen by the producer bounds the age of a
        # delayed relay; retry cannot refresh it.
        source_recorded = int(payload.get("source_recorded_at_ms") or now_ms)
        source_observed = int(payload.get("provider_event_at_ms") or source_recorded)
        root_expires = source_recorded + root_ttl_ms
        scope = _sha((kind, source_fact_key, asset_key)) if asset_key else None
        selection_json = {
            "reason": selection.reason,
            "version": selection.version,
            "asset_id": asset_key,
            "candidates": selection.candidates,
            "registry_snapshot_ref": selection.registry_snapshot_ref,
            "instrument": None
            if selection.instrument is None
            else {
                "venue": selection.instrument.venue,
                "environment": selection.instrument.environment,
                "product": selection.instrument.product,
                "native_symbol": selection.instrument.native_symbol,
                "quote_asset": selection.instrument.quote_asset,
                "settlement_asset": selection.instrument.settlement_asset,
                "units_per_contract": str(selection.instrument.units_per_contract),
                "mapping_semantics_digest": selection.instrument.semantics_digest,
            },
        }
        manifest = {
            "manifest_version": "trade_analysis_v1",
            "trigger_id": trigger_id,
            "target_selection": selection_json,
            "source_payload_sha": payload_sha256,
        }
        state = "PENDING" if selection.reason == "selected" and now_ms < root_expires else "EXCLUDED"
        analysis_status = "pending" if state == "PENDING" else "expired" if now_ms >= root_expires else "excluded"
        reason = "source_expired" if now_ms >= root_expires else selection.reason
        inserted = self.conn.execute(
            """
            INSERT INTO trading_triggers
              (trigger_id, kind, source_fact_key, source_revision, payload_sha256, payload,
               asset_id, target_selection, first_visible_at_ms, source_observed_at_ms,
               root_expires_at_ms, supersedes_ref, created_at_ms)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s,%s)
            ON CONFLICT (kind, source_fact_key, source_revision) DO NOTHING
            RETURNING trigger_id
            """,
            (
                trigger_id,
                kind,
                source_fact_key,
                source_revision,
                payload_sha256,
                json.dumps(payload),
                asset_key,
                json.dumps(selection_json),
                int(now_ms),
                source_observed,
                root_expires,
                supersedes_ref,
                int(now_ms),
            ),
        ).fetchone()
        if inserted is None:
            # Another relay committed this fact while our initial SELECT was
            # waiting. Its Case committed in the same transaction.
            source = self.conn.execute(
                "SELECT trigger_id, payload_sha256 FROM trading_triggers "
                "WHERE kind=%s AND source_fact_key=%s AND source_revision=%s FOR UPDATE",
                (kind, source_fact_key, source_revision),
            ).fetchone()
            if source is None:
                raise RuntimeError("analysis_trigger_conflict_missing")
            if source["payload_sha256"] != payload_sha256:
                self.conn.execute(
                    "INSERT INTO trading_trigger_conflicts "
                    "(kind,source_fact_key,source_revision,attempted_sha256,original_sha256,observed_at_ms) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (kind, source_fact_key, source_revision, payload_sha256, source["payload_sha256"], int(now_ms)),
                )
                return str(source["trigger_id"]), case_id, "source_conflict"
            return str(source["trigger_id"]), case_id, "duplicate"
        self.conn.execute(
            """
            INSERT INTO trading_cases
              (case_id, underlying_key, trigger_kind, primary_source_key, manifest,
               manifest_sha256, state, policy_decision, policy_reason, observed_at_ms,
               source_observed_at_ms, trigger_persisted_at_ms, created_at_ms, updated_at_ms,
               trigger_id, run_kind, recheck_seq, target_asset_id, target_selection,
               entry_scope_id, mapping_semantics_digest, root_expires_at_ms,
               work_deadline_at_ms, next_attempt_at_ms, analysis_status)
            VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,'not_run',%s,%s,%s,%s,%s,%s,
                    %s,'initial',0,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
            """,
            (
                case_id,
                asset_key or f"unselected:{trigger_id}",
                kind,
                f"{kind}:{source_fact_key}:{source_revision}",
                json.dumps(manifest),
                _sha(manifest),
                state,
                reason,
                source_observed,
                source_observed,
                source_recorded,
                int(now_ms),
                int(now_ms),
                trigger_id,
                asset_key,
                json.dumps(selection_json),
                scope,
                selection.instrument.semantics_digest if selection.instrument else None,
                root_expires,
                root_expires,
                int(now_ms),
                analysis_status,
            ),
        )
        if asset_key is not None:
            for horizon in _OUTCOME_HORIZONS:
                self.conn.execute(
                    "INSERT INTO trading_case_outcomes "
                    "(case_id,axis,horizon_seconds,label_version,status,available_at_ms) "
                    "VALUES (%s,'source',%s,%s,'pending',%s)",
                    (case_id, horizon, _OUTCOME_VERSION, source_observed + horizon * 1_000),
                )
        if state == "PENDING":
            self.conn.execute(
                "INSERT INTO trading_root_market_tapes (case_id,next_sample_at_ms,expires_at_ms) VALUES (%s,%s,%s)",
                (case_id, int(now_ms), root_expires + MAX_HOLDING_SECONDS * 1_000 + ENTRY_WINDOW_MS + 300_000),
            )
        return trigger_id, case_id, "accepted"

    def due_root_research_tapes(self, *, now_ms: int, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT tape.case_id,tape.tape_ref,tape.next_sample_at_ms,tape.expires_at_ms,
                   c.target_selection,c.root_expires_at_ms,c.created_at_ms,t.first_visible_at_ms
              FROM trading_root_market_tapes tape
              JOIN trading_cases c USING (case_id)
              JOIN trading_triggers t USING (trigger_id)
             WHERE tape.next_sample_at_ms<=%s AND tape.expires_at_ms>=%s
             ORDER BY tape.next_sample_at_ms,tape.case_id LIMIT %s
            """,
            (int(now_ms), int(now_ms), max(1, min(64, limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_root_research_sample(
        self, *, case_id: str, prior_ref: str | None, tape_ref: str, sampled_at_ms: int
    ) -> bool:
        updated = self.conn.execute(
            """
            UPDATE trading_root_market_tapes SET tape_ref=%s,next_sample_at_ms=%s
             WHERE case_id=%s AND tape_ref IS NOT DISTINCT FROM %s
               AND next_sample_at_ms<=%s AND expires_at_ms>=%s
            """,
            (tape_ref, int(sampled_at_ms) + 60_000, case_id, prior_ref, int(sampled_at_ms), int(sampled_at_ms)),
        )
        return bool(updated.rowcount)

    def claim_analysis_case(self, *, now_ms: int, lease_ms: int) -> dict[str, Any] | None:
        if lease_ms <= 0:
            raise ValueError("analysis_lease_invalid")
        self.conn.execute(
            "UPDATE trading_cases SET state='EXCLUDED',analysis_status='expired',"
            "policy_reason='work_deadline_expired',updated_at_ms=%s "
            "WHERE case_id IN (SELECT case_id FROM trading_cases "
            "WHERE trigger_id IS NOT NULL AND state='PENDING' "
            "AND work_deadline_at_ms<=%s ORDER BY work_deadline_at_ms,case_id "
            "FOR UPDATE SKIP LOCKED LIMIT 128)",
            (int(now_ms), int(now_ms)),
        )
        # A former owner loses its fence before another Case for this asset can
        # become RUNNING. This bounded repair also works after process restart.
        self.conn.execute(
            """
            UPDATE trading_cases SET state='PENDING', claim_token=NULL,
                   lease_until_ms=NULL, updated_at_ms=%s
             WHERE case_id IN (
                 SELECT case_id FROM trading_cases
                  WHERE trigger_id IS NOT NULL AND state='RUNNING'
                    AND lease_until_ms<=%s
                  ORDER BY lease_until_ms, case_id
                  FOR UPDATE SKIP LOCKED LIMIT 128)
            """,
            (int(now_ms), int(now_ms)),
        )
        self.conn.execute(
            "UPDATE trading_case_attempts SET analysis_status='interrupted', "
            "provider_status=COALESCE(provider_status,'result_unknown'), "
            "error_code=COALESCE(error_code,'lease_expired'), ended_at_ms=%s "
            "WHERE analysis_status='running' AND case_id IN "
            "(SELECT case_id FROM trading_cases WHERE trigger_id IS NOT NULL "
            "AND state='PENDING' AND claim_attempt>0)",
            (int(now_ms),),
        )
        self.conn.execute(
            "UPDATE trading_model_calls call SET status='result_unknown' "
            "WHERE status='requested' AND EXISTS "
            "(SELECT 1 FROM trading_case_attempts attempt WHERE attempt.case_id=call.case_id "
            "AND attempt.claim_attempt=call.claim_attempt AND attempt.analysis_status='interrupted')"
        )
        row = self.conn.execute(
            """
            SELECT c.* FROM trading_cases c
             WHERE c.trigger_id IS NOT NULL AND c.target_asset_id IS NOT NULL
               AND c.state = 'PENDING' AND c.next_attempt_at_ms <= %s
               AND NOT EXISTS (
                 SELECT 1 FROM trading_cases running
                  WHERE running.target_asset_id = c.target_asset_id
                    AND running.case_id <> c.case_id AND running.state = 'RUNNING'
                    AND running.lease_until_ms > %s)
             ORDER BY c.created_at_ms, c.case_id
             FOR UPDATE OF c SKIP LOCKED LIMIT 1
            """,
            (int(now_ms), int(now_ms)),
        ).fetchone()
        if row is None:
            return None
        case_id = str(row["case_id"])
        asset_id = str(row["target_asset_id"])
        locked = self.conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 683)) AS locked",
            (asset_id,),
        ).fetchone()
        if not locked["locked"]:
            return None
        # The first query may have raced a different worker claiming another
        # Case for this asset. Recheck after acquiring its transaction lock.
        busy = self.conn.execute(
            "SELECT 1 FROM trading_cases WHERE target_asset_id=%s AND state='RUNNING' AND lease_until_ms>%s LIMIT 1",
            (asset_id, int(now_ms)),
        ).fetchone()
        if busy is not None:
            return None
        token = uuid.uuid4().hex
        updated = self.conn.execute(
            """
            UPDATE trading_cases SET state='RUNNING', claim_token=%s,
                   lease_until_ms=%s, claim_attempt=claim_attempt+1,
                   updated_at_ms=%s
             WHERE case_id=%s AND root_expires_at_ms>%s AND work_deadline_at_ms>%s
         RETURNING *
            """,
            (
                token,
                min(int(now_ms) + lease_ms, int(row["root_expires_at_ms"]), int(row["work_deadline_at_ms"])),
                int(now_ms),
                case_id,
                int(now_ms),
                int(now_ms),
            ),
        ).fetchone()
        if updated is None:
            self.conn.execute(
                """
                UPDATE trading_cases SET state='EXCLUDED', analysis_status='expired',
                       policy_reason='source_expired', updated_at_ms=%s
                 WHERE case_id=%s AND state IN ('PENDING','RUNNING')
                """,
                (int(now_ms), case_id),
            )
            return None
        self.conn.execute(
            """
            INSERT INTO trading_case_attempts
              (case_id,claim_attempt,claim_token,started_at_ms,analysis_status,cost_unknown_reason)
            VALUES (%s,%s,%s,%s,'running','not_called')
            """,
            (case_id, int(updated["claim_attempt"]), token, int(now_ms)),
        )
        return dict(updated)

    def finish_analysis_case(
        self,
        *,
        case_id: str,
        claim_token: str,
        now_ms: int,
        analysis_status: str,
        evidence_ref: str | None,
        decision: dict[str, Any] | None,
        assessment_ref: str | None = None,
        prepared_signal: PreparedTradeSignal | None = None,
        publish_block_reason: str | None = None,
    ) -> bool:
        """A late or replaced model answer has no authority to settle or publish."""

        asset = self.conn.execute(
            "SELECT target_asset_id FROM trading_cases WHERE case_id=%s",
            (case_id,),
        ).fetchone()
        if asset is not None and asset["target_asset_id"] is not None:
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 683))", (asset["target_asset_id"],))
        row = self.conn.execute(
            """
            SELECT * FROM trading_cases
             WHERE case_id=%s AND state='RUNNING' AND claim_token=%s
               AND lease_until_ms>%s AND root_expires_at_ms>%s
               AND work_deadline_at_ms>%s FOR UPDATE
            """,
            (case_id, claim_token, int(now_ms), int(now_ms), int(now_ms)),
        ).fetchone()
        if row is None:
            return False
        action = None if decision is None else str(decision["action"])
        published = False
        if decision is not None:
            decision_id = decision_identity(case_id, decision)
            superseded = (
                self.conn.execute(
                    """
                SELECT 1 FROM trading_triggers newer
                 JOIN trading_triggers original ON original.trigger_id=%s
                 WHERE (newer.supersedes_ref IN (original.trigger_id,original.source_fact_key)
                        OR (newer.kind=original.kind AND newer.source_fact_key=original.source_fact_key
                            AND newer.source_revision<>original.source_revision
                            AND COALESCE((newer.payload->>'source_recorded_at_ms')::bigint,
                                         newer.first_visible_at_ms)
                              > COALESCE((original.payload->>'source_recorded_at_ms')::bigint,
                                         original.first_visible_at_ms)))
                   AND newer.trigger_id<>original.trigger_id
                   AND newer.created_at_ms<=%s LIMIT 1
                """,
                    (row["trigger_id"], int(now_ms)),
                ).fetchone()
                is not None
            )
            publish_status = (
                "superseded"
                if superseded
                else ("blocked" if publish_block_reason else "shadow")
                if action == "TRADE" and prepared_signal is None
                else "not_applicable"
                if action != "TRADE"
                else "published"
            )
            if prepared_signal is not None and not superseded:
                signal = prepared_signal.value
                if (
                    not isinstance(signal, TradeSignalV2)
                    or signal.case_id != case_id
                    or signal.decision_id != decision_id
                    or signal.entry_scope_id != row["entry_scope_id"]
                    or signal.asset_id != row["target_asset_id"]
                    or signal.mapping_semantics_digest != row["mapping_semantics_digest"]
                    or signal.direction != decision.get("side")
                    or action != "TRADE"
                    or signal.expires_at_ns <= int(now_ms) * 1_000_000
                ):
                    raise ValueError("analysis_signal_identity_invalid")
                cast(ExecutionStreamStorage, self).append_trade_signal(prepared_signal)
                published = True
            self.conn.execute(
                """
                INSERT INTO trading_case_decisions
                  (case_id, decision_id, policy_id, policy_version, input_ref,
                   assessment_ref, action, decision, publish_status, publish_reason,
                   decided_at_ms, valid_until_ms)
                VALUES (%s,%s,'trade_assessment','v3',%s,%s,%s,%s::jsonb,
                        %s,%s,%s,%s)
                """,
                (
                    case_id,
                    decision_id,
                    evidence_ref,
                    assessment_ref,
                    action,
                    json.dumps(decision),
                    publish_status,
                    "source_superseded"
                    if superseded
                    else publish_block_reason
                    if publish_status == "blocked"
                    else "publish_disabled"
                    if publish_status == "shadow"
                    else None,
                    int(now_ms),
                    int(row["root_expires_at_ms"]),
                ),
            )
            for horizon in _OUTCOME_HORIZONS:
                self.conn.execute(
                    "INSERT INTO trading_case_outcomes "
                    "(case_id,axis,horizon_seconds,label_version,status,available_at_ms) "
                    "VALUES (%s,'decision',%s,%s,'pending',%s)",
                    (case_id, horizon, _OUTCOME_VERSION, int(now_ms) + 60_000 + horizon * 1_000),
                )
        state = "SIGNAL_EMITTED" if published else "DONE" if decision is not None else "FAILED"
        policy_decision = (
            "long"
            if action == "TRADE" and decision is not None and decision.get("side") == "long"
            else "short"
            if action == "TRADE"
            else "watch"
            if action == "WATCH"
            else "no_trade"
            if action == "NO_TRADE"
            else "not_run"
        )
        self.conn.execute(
            """
            UPDATE trading_cases SET state=%s, policy_decision=%s,
                   policy_reason=%s, analysis_status=%s, evidence_ref=%s,
                   decided_at_ms=%s, updated_at_ms=%s
             WHERE case_id=%s
            """,
            (
                state,
                policy_decision,
                decision.get("reason_code", "analysis_complete") if decision else analysis_status,
                analysis_status,
                evidence_ref,
                int(now_ms),
                int(now_ms),
                case_id,
            ),
        )
        self.conn.execute(
            "UPDATE trading_case_attempts SET settled=true WHERE case_id=%s AND claim_attempt=%s AND claim_token=%s",
            (case_id, int(row["claim_attempt"]), claim_token),
        )
        if action == "WATCH" and decision is not None and not superseded:
            self._create_watch_observation(
                parent=row,
                decision=decision,
                evidence_ref=evidence_ref,
                now_ms=now_ms,
            )
        return True

    def record_analysis_attempt(
        self,
        *,
        case_id: str,
        claim_attempt: int,
        claim_token: str,
        brief_ref: str | None,
        evidence_ref: str | None,
        assessment_ref: str | None,
        model_name: str | None,
        prompt_sha: str | None,
        started_at_ms: int | None,
        ended_at_ms: int,
        provider_status: str | None,
        analysis_status: str,
        error_code: str | None,
        validation_errors: tuple[dict[str, str], ...],
        input_tokens: int | None,
        output_tokens: int | None,
        cost_microusd: int | None,
        calls: tuple[dict[str, Any], ...],
        known_cost_microusd: int = 0,
        unknown_cost_calls: int = 0,
        cost_upper_estimate_microusd: int | None = None,
    ) -> None:
        """A late claim may leave diagnostics, but gains no settlement authority."""
        cost_unknown_reason = (
            ("not_called" if not calls else "one_or_more_physical_costs_unavailable") if cost_microusd is None else None
        )
        self.conn.execute(
            """
            UPDATE trading_case_attempts SET
              brief_ref=COALESCE(brief_ref,%s),evidence_ref=COALESCE(evidence_ref,%s),assessment_ref=%s,
              model_name=%s,prompt_sha=%s,ended_at_ms=%s,provider_status=%s,
              analysis_status=%s,error_code=%s,validation_errors=%s::jsonb,
              physical_call_count=%s,input_tokens=%s,output_tokens=%s,
              cost_microusd=%s,cost_unknown_reason=%s,
              known_cost_microusd=%s,unknown_cost_calls=%s,cost_upper_estimate_microusd=%s
            WHERE case_id=%s AND claim_attempt=%s AND claim_token=%s
            """,
            (
                brief_ref,
                evidence_ref,
                assessment_ref,
                model_name,
                prompt_sha,
                int(ended_at_ms),
                provider_status,
                analysis_status,
                error_code,
                json.dumps(validation_errors),
                len(calls),
                input_tokens,
                output_tokens,
                cost_microusd,
                cost_unknown_reason,
                known_cost_microusd,
                unknown_cost_calls,
                cost_upper_estimate_microusd,
                case_id,
                int(claim_attempt),
                claim_token,
            ),
        )
        for index, call in enumerate(calls):
            self.conn.execute(
                """
                INSERT INTO trading_model_calls
                  (case_id,claim_attempt,call_index,request_ref,response_ref,
                   input_tokens,output_tokens,cost_microusd,cost_unknown_reason,status,finished_at_ms)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (case_id,claim_attempt,call_index) DO UPDATE SET
                  response_ref=COALESCE(EXCLUDED.response_ref,trading_model_calls.response_ref),
                  input_tokens=EXCLUDED.input_tokens,
                  output_tokens=EXCLUDED.output_tokens,
                  cost_microusd=EXCLUDED.cost_microusd,
                  cost_unknown_reason=EXCLUDED.cost_unknown_reason,
                  status=EXCLUDED.status,
                  finished_at_ms=COALESCE(trading_model_calls.finished_at_ms,EXCLUDED.finished_at_ms)
                """,
                (
                    case_id,
                    int(claim_attempt),
                    index,
                    call.get("request_ref"),
                    call.get("response_ref"),
                    call.get("input_tokens"),
                    call.get("output_tokens"),
                    call.get("cost_microusd"),
                    call.get("cost_unknown_reason"),
                    call.get("status", "completed" if call.get("response_ref") else "result_unknown"),
                    call.get("finished_at_ms"),
                ),
            )

    def prior_analysis_snapshot(self, *, case_id: str, before_claim_attempt: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT evidence_ref,brief_ref FROM trading_case_attempts "
            "WHERE case_id=%s AND claim_attempt<%s "
            "AND evidence_ref IS NOT NULL AND brief_ref IS NOT NULL "
            "ORDER BY claim_attempt LIMIT 1",
            (case_id, int(before_claim_attempt)),
        ).fetchone()
        return None if row is None else dict(row)

    def record_analysis_snapshot(
        self,
        *,
        case_id: str,
        claim_attempt: int,
        claim_token: str,
        evidence_ref: str | None,
        brief_ref: str | None,
        now_ms: int,
    ) -> bool:
        updated = self.conn.execute(
            "UPDATE trading_case_attempts a SET evidence_ref=COALESCE(a.evidence_ref,%s),"
            "brief_ref=COALESCE(a.brief_ref,%s) "
            "FROM trading_cases c WHERE a.case_id=c.case_id AND a.case_id=%s "
            "AND a.claim_attempt=%s AND a.claim_token=%s "
            "AND c.state='RUNNING' AND c.claim_attempt=a.claim_attempt "
            "AND c.claim_token=a.claim_token AND c.lease_until_ms>%s "
            "AND c.work_deadline_at_ms>%s AND c.root_expires_at_ms>%s "
            "AND (a.evidence_ref IS NULL OR a.evidence_ref=%s) "
            "AND (a.brief_ref IS NULL OR a.brief_ref=%s)",
            (
                evidence_ref,
                brief_ref,
                case_id,
                claim_attempt,
                claim_token,
                int(now_ms),
                int(now_ms),
                int(now_ms),
                evidence_ref,
                brief_ref,
            ),
        )
        return bool(updated.rowcount)

    def record_model_call_start(
        self,
        *,
        case_id: str,
        claim_attempt: int,
        claim_token: str,
        call_index: int,
        request_ref: str,
        now_ms: int,
        timeout_ms: int,
        reserved_cost_microusd: int | None,
    ) -> bool:
        row = self.conn.execute(
            "SELECT LEAST(c.lease_until_ms,c.work_deadline_at_ms,c.root_expires_at_ms) AS deadline "
            "FROM trading_cases c JOIN trading_case_attempts a ON a.case_id=c.case_id "
            "AND a.claim_attempt=c.claim_attempt AND a.claim_token=c.claim_token "
            "WHERE c.case_id=%s AND c.claim_attempt=%s AND c.claim_token=%s "
            "AND c.state='RUNNING' AND c.lease_until_ms>%s AND c.work_deadline_at_ms>%s "
            "AND c.root_expires_at_ms>%s",
            (case_id, claim_attempt, claim_token, now_ms, now_ms, now_ms),
        ).fetchone()
        if row is None:
            return False
        remaining_ms = int(row["deadline"]) - now_ms
        actual_timeout_ms = min(timeout_ms, remaining_ms)
        self.conn.execute(
            "INSERT INTO trading_model_calls "
            "(case_id,claim_attempt,call_index,request_ref,started_at_ms,timeout_ms,remaining_deadline_ms,"
            "reserved_cost_microusd,cost_unknown_reason,status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'provider_cost_unavailable','requested')",
            (
                case_id,
                claim_attempt,
                call_index,
                request_ref,
                now_ms,
                actual_timeout_ms,
                remaining_ms,
                reserved_cost_microusd,
            ),
        )
        return True

    def record_model_call_finish(
        self,
        *,
        case_id: str,
        claim_attempt: int,
        claim_token: str,
        call_index: int,
        response_ref: str | None,
        finished_at_ms: int,
        status: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_microusd: int | None,
    ) -> None:
        if status not in ("completed", "result_unknown"):
            raise ValueError("model_call_status_invalid")
        self.conn.execute(
            "UPDATE trading_model_calls call SET status=%s,response_ref=%s,finished_at_ms=%s,"
            "input_tokens=%s,output_tokens=%s,cost_microusd=%s,cost_unknown_reason=%s "
            "WHERE case_id=%s AND claim_attempt=%s AND call_index=%s "
            "AND EXISTS (SELECT 1 FROM trading_case_attempts attempt "
            "WHERE attempt.case_id=call.case_id AND attempt.claim_attempt=call.claim_attempt "
            "AND attempt.claim_token=%s)",
            (
                status,
                response_ref,
                finished_at_ms,
                input_tokens,
                output_tokens,
                cost_microusd,
                "provider_cost_unavailable" if cost_microusd is None else None,
                case_id,
                claim_attempt,
                call_index,
                claim_token,
            ),
        )

    def mark_analysis_attempt_unsettled(
        self,
        *,
        case_id: str,
        claim_attempt: int,
        claim_token: str,
    ) -> None:
        self.conn.execute(
            "UPDATE trading_case_attempts SET settled=false,error_code='fenced_out' "
            "WHERE case_id=%s AND claim_attempt=%s AND claim_token=%s",
            (case_id, claim_attempt, claim_token),
        )

    def _create_watch_observation(
        self,
        *,
        parent: dict[str, Any],
        decision: dict[str, Any],
        evidence_ref: str | None,
        now_ms: int,
    ) -> None:
        if parent["run_kind"] != "initial":
            return
        watch = decision.get("watch_condition")
        if not isinstance(watch, dict):
            return
        watch = {**watch, "parent_evidence_ref": evidence_ref}
        if watch.get("kind") != "closed_1m_range_cross":
            return
        root_expires = int(parent["root_expires_at_ms"])
        if int(watch.get("expires_at_ms") or 0) != root_expires or now_ms >= root_expires:
            return
        if parent["entry_scope_id"] is None:
            return
        used = self.conn.execute(
            "SELECT 1 FROM trading_trade_plans WHERE entry_scope_id=%s LIMIT 1",
            (parent["entry_scope_id"],),
        ).fetchone()
        if used is not None:
            return
        self.conn.execute(
            """
            INSERT INTO trading_watch_observations
              (parent_case_id,trigger_id,condition,status,next_check_at_ms,expires_at_ms,created_at_ms,updated_at_ms)
            VALUES (%s,%s,%s::jsonb,'waiting',%s,%s,%s,%s)
            ON CONFLICT (trigger_id) DO NOTHING
            """,
            (
                parent["case_id"],
                parent["trigger_id"],
                json.dumps(watch),
                int(now_ms),
                root_expires,
                int(now_ms),
                int(now_ms),
            ),
        )

    def due_watch_observations(self, *, now_ms: int, limit: int = 16) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT w.*,c.trigger_id,c.recheck_seq,c.target_selection,c.target_asset_id,
                   c.entry_scope_id,c.root_expires_at_ms
              FROM trading_watch_observations w
              JOIN trading_cases c ON c.case_id=w.parent_case_id
             WHERE w.status='waiting' AND w.next_check_at_ms<=%s
             ORDER BY w.next_check_at_ms,w.parent_case_id LIMIT %s
            """,
            (int(now_ms), max(1, min(128, limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def advance_watch_observation(
        self,
        *,
        parent_case_id: str,
        now_ms: int,
        observation_status: str,
        observed_at_ms: int | None,
        observed_value: str | None,
        previous_close: str | None = None,
        trigger_side: str | None = None,
        observed_path: tuple[tuple[int, str], ...] = (),
        observation_ref: str | None = None,
    ) -> bool:
        if observation_status not in ("satisfied", "not_met", "data_missing", "expired", "missed"):
            raise ValueError("watch_observation_status_invalid")
        asset = self.conn.execute(
            "SELECT target_asset_id FROM trading_cases WHERE case_id=%s",
            (parent_case_id,),
        ).fetchone()
        if asset is not None and asset["target_asset_id"] is not None:
            self.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 683))",
                (asset["target_asset_id"],),
            )
        row = self.conn.execute(
            """
            SELECT w.*,c.* FROM trading_watch_observations w
            JOIN trading_cases c ON c.case_id=w.parent_case_id
            WHERE w.parent_case_id=%s AND w.status='waiting' FOR UPDATE OF w
            """,
            (parent_case_id,),
        ).fetchone()
        if row is None:
            return False
        parent = dict(row)
        condition = dict(parent["condition"])
        if observation_status in ("satisfied", "missed", "not_met", "data_missing") and observed_path:
            if not observation_ref:
                raise ValueError("watch_observation_evidence_missing")
            try:
                upper = Decimal(str(condition["upper_level"]))
                lower = Decimal(str(condition["lower_level"]))
                previous = Decimal(
                    str(
                        parent["last_observed_value"]
                        if parent["last_observed_value"] is not None
                        else condition["previous_close"]
                    )
                )
            except (InvalidOperation, KeyError, TypeError) as exc:
                raise ValueError("watch_observation_invalid") from exc
            expected_at = int(parent["last_observed_at_ms"] or condition["frozen_at_ms"]) + 60_000
            found_side = None
            for stamp, raw_close in observed_path:
                value = Decimal(str(raw_close))
                if (
                    stamp != expected_at
                    or stamp > now_ms
                    or stamp > int(condition["expires_at_ms"])
                    or not value.is_finite()
                    or value <= 0
                    or not previous.is_finite()
                    or not 0 < lower < upper
                ):
                    raise ValueError("watch_observation_path_invalid")
                side = (
                    "long"
                    if previous <= upper and value > upper
                    else "short"
                    if previous >= lower and value < lower
                    else None
                )
                if side is not None:
                    found_side = side
                    if stamp != observed_path[-1][0]:
                        raise ValueError("watch_observation_not_first_cross")
                previous, expected_at = value, stamp + 60_000
            if observed_at_ms != observed_path[-1][0] or observed_value != observed_path[-1][1]:
                raise ValueError("watch_observation_tail_mismatch")
            if (observation_status in ("satisfied", "missed")) != (
                found_side is not None
            ) or trigger_side != found_side:
                raise ValueError("watch_observation_condition_unmet")
        elif observation_status in ("satisfied", "missed"):
            raise ValueError("watch_observation_path_missing")
        root_expires = int(parent["root_expires_at_ms"])
        sequence = 1
        used = self.conn.execute(
            "SELECT 1 FROM trading_trade_plans WHERE entry_scope_id=%s LIMIT 1",
            (parent["entry_scope_id"],),
        ).fetchone()
        superseded = self.conn.execute(
            "SELECT 1 FROM trading_triggers newer JOIN trading_triggers original "
            "ON original.trigger_id=%s WHERE newer.trigger_id<>original.trigger_id "
            "AND (newer.supersedes_ref IN (original.trigger_id,original.source_fact_key) "
            "OR (newer.kind=original.kind AND newer.source_fact_key=original.source_fact_key "
            "AND newer.source_revision<>original.source_revision "
            "AND COALESCE((newer.payload->>'source_recorded_at_ms')::bigint,newer.first_visible_at_ms) "
            "> COALESCE((original.payload->>'source_recorded_at_ms')::bigint,original.first_visible_at_ms))) "
            "AND newer.created_at_ms<=%s LIMIT 1",
            (parent["trigger_id"], now_ms),
        ).fetchone()
        if used is not None or superseded is not None:
            final_status = "cancelled"
        elif now_ms >= root_expires or observation_status == "missed":
            final_status = "expired"
        elif observation_status == "satisfied":
            final_status = (
                "triggered"
                if observed_at_ms is not None and now_ms < min(root_expires, observed_at_ms + 120_000)
                else "expired"
            )
        else:
            final_status = "waiting"
        child_case_id = None
        if final_status == "triggered":
            if observed_at_ms is None:
                raise ValueError("watch_trigger_clock_missing")
            child_case_id = _sha((parent["trigger_id"], "conditional_cross", sequence))
            watch = dict(parent["condition"])
            parent_decision = self.conn.execute(
                "SELECT decision_id,decision FROM trading_case_decisions WHERE case_id=%s",
                (parent_case_id,),
            ).fetchone()
            manifest = dict(parent["manifest"])
            manifest.update(
                {
                    "parent_case_id": parent_case_id,
                    "parent_decision_id": None if parent_decision is None else parent_decision["decision_id"],
                    "parent_decision": None if parent_decision is None else parent_decision["decision"],
                    "recheck_seq": sequence,
                    "watch_condition": watch,
                    "parent_evidence_ref": watch.get("parent_evidence_ref"),
                    "watch_observed_at_ms": observed_at_ms,
                    "watch_observed_value": observed_value,
                    "watch_previous_close": previous_close,
                    "watch_trigger_side": trigger_side,
                    "watch_observation_ref": observation_ref,
                    "triggered_at_ms": int(now_ms),
                }
            )
            self.conn.execute(
                """
                INSERT INTO trading_cases
                  (case_id,underlying_key,trigger_kind,primary_source_key,manifest,
                   manifest_sha256,state,policy_decision,policy_reason,observed_at_ms,
                   source_observed_at_ms,trigger_persisted_at_ms,created_at_ms,updated_at_ms,
                   trigger_id,run_kind,recheck_seq,target_asset_id,target_selection,
                   entry_scope_id,mapping_semantics_digest,root_expires_at_ms,
                   work_deadline_at_ms,next_attempt_at_ms,analysis_status)
                VALUES (%s,%s,%s,%s,%s::jsonb,%s,'PENDING','not_run',
                        'watch_condition_satisfied',%s,%s,%s,%s,%s,%s,'conditional',%s,%s,
                        %s::jsonb,%s,%s,%s,%s,%s,'pending')
                ON CONFLICT DO NOTHING
                """,
                (
                    child_case_id,
                    parent["underlying_key"],
                    parent["trigger_kind"],
                    parent["primary_source_key"],
                    json.dumps(manifest),
                    _sha(manifest),
                    parent["observed_at_ms"],
                    parent["source_observed_at_ms"],
                    parent["trigger_persisted_at_ms"],
                    int(now_ms),
                    int(now_ms),
                    parent["trigger_id"],
                    sequence,
                    parent["target_asset_id"],
                    json.dumps(parent["target_selection"]),
                    parent["entry_scope_id"],
                    parent["mapping_semantics_digest"],
                    root_expires,
                    min(int(observed_at_ms) + 120_000, root_expires),
                    int(now_ms),
                ),
            )
            exists = self.conn.execute(
                "SELECT 1 FROM trading_cases WHERE case_id=%s",
                (child_case_id,),
            ).fetchone()
            if exists is None:
                final_status, child_case_id = "cancelled", None
        self.conn.execute(
            """
            UPDATE trading_watch_observations
               SET status=%s,last_observation_status=%s,last_observed_at_ms=%s,
                   last_observed_value=%s,last_observation_ref=%s,next_check_at_ms=%s,
                   child_case_id=%s,trigger_side=%s,updated_at_ms=%s
             WHERE parent_case_id=%s
            """,
            (
                final_status,
                observation_status if observation_status != "expired" else None,
                observed_at_ms,
                observed_value if observed_value is not None else parent["last_observed_value"],
                observation_ref or parent["last_observation_ref"],
                int(now_ms) + 30_000,
                child_case_id,
                trigger_side,
                int(now_ms),
                parent_case_id,
            ),
        )
        return True

    def due_analysis_outcomes(
        self, *, now_ms: int, limit: int = 16, label_version: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT o.case_id,o.axis,o.horizon_seconds,o.label_version,o.available_at_ms,
                   c.source_observed_at_ms,c.decided_at_ms,c.target_selection
              FROM trading_case_outcomes o JOIN trading_cases c USING (case_id)
             WHERE o.status='pending' AND o.available_at_ms<=%s
               AND o.next_attempt_at_ms<=%s
               AND o.label_version=COALESCE(%s::text,o.label_version)
             ORDER BY o.available_at_ms,o.case_id,o.axis,o.horizon_seconds LIMIT %s
            """,
            (int(now_ms), int(now_ms), label_version, max(1, min(128, limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def queue_price_path_v2_corrections(self, *, limit: int = 256) -> int:
        """Append v2 work beside settled v1 labels; never rewrite the old audit."""
        require_transaction(self.conn, operation="queue_price_path_v2_corrections")
        result = self.conn.execute(
            """
            INSERT INTO trading_case_outcomes
              (case_id,axis,horizon_seconds,label_version,status,available_at_ms,next_attempt_at_ms)
            SELECT old.case_id,old.axis,old.horizon_seconds,'price_path_v2','pending',
                   old.available_at_ms,0
              FROM trading_case_outcomes old
             WHERE old.label_version='price_path_v1' AND old.status IN ('ok','missing')
               AND NOT EXISTS (
                   SELECT 1 FROM trading_case_outcomes newer
                    WHERE newer.case_id=old.case_id AND newer.axis=old.axis
                      AND newer.horizon_seconds=old.horizon_seconds
                      AND newer.label_version='price_path_v2'
               )
             ORDER BY old.case_id,old.axis,old.horizon_seconds
             LIMIT %s
            ON CONFLICT DO NOTHING
            """,
            (max(1, min(10_000, limit)),),
        )
        return int(result.rowcount)

    def settle_analysis_outcome(
        self,
        *,
        case_id: str,
        axis: str,
        horizon_seconds: int,
        label_version: str,
        status: str,
        return_bps: str | None,
        path_ref: str | None,
        now_ms: int,
    ) -> bool:
        if status not in ("ok", "missing") or (status == "ok") != (return_bps is not None):
            raise ValueError("analysis_outcome_invalid")
        result = self.conn.execute(
            """
            UPDATE trading_case_outcomes SET status=%s,return_bps=%s,path_ref=%s,labeled_at_ms=%s
             WHERE case_id=%s AND axis=%s AND horizon_seconds=%s
               AND label_version=%s AND status='pending'
            """,
            (status, return_bps, path_ref, int(now_ms), case_id, axis, horizon_seconds, label_version),
        )
        return bool(result.rowcount)

    def retry_analysis_outcome(
        self,
        *,
        case_id: str,
        axis: str,
        horizon_seconds: int,
        label_version: str,
        next_attempt_at_ms: int,
    ) -> None:
        self.conn.execute(
            "UPDATE trading_case_outcomes SET next_attempt_at_ms=%s "
            "WHERE case_id=%s AND axis=%s AND horizon_seconds=%s "
            "AND label_version=%s AND status='pending'",
            (int(next_attempt_at_ms), case_id, axis, horizon_seconds, label_version),
        )

    def record_shadow_evaluation(
        self,
        *,
        case_id: str,
        decision_at_ms: int,
        scheduled_at_ms: int,
        due_at_ms: int,
        decision_quote_ref: str | None,
        planned_quote_ref: str | None,
        initial_result: dict[str, Any] | None,
    ) -> None:
        status = "pending" if initial_result is None else "unevaluable"
        self.conn.execute(
            """
            INSERT INTO trading_case_evaluations
              (case_id,source,evaluation_version,status,reason,decision_at_ms,
               scheduled_at_ms,due_at_ms,next_attempt_at_ms,decision_quote_ref,
               planned_quote_ref,result,evaluated_at_ms,next_quote_at_ms)
            VALUES (%s,'shadow_simulation','shadow_net_v1',%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (case_id,source,evaluation_version) DO NOTHING
            """,
            (
                case_id,
                status,
                None if initial_result is None else initial_result.get("reason"),
                int(decision_at_ms),
                int(scheduled_at_ms),
                int(due_at_ms),
                int(due_at_ms),
                decision_quote_ref,
                planned_quote_ref,
                None if initial_result is None else json.dumps(initial_result),
                None if initial_result is None else int(scheduled_at_ms),
                int(scheduled_at_ms) + 60_000 if initial_result is None else None,
            ),
        )

    def due_shadow_quote_samples(self, *, now_ms: int, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT evaluation.case_id,evaluation.quote_tape_ref,evaluation.next_quote_at_ms,
                   c.target_selection
              FROM trading_case_evaluations evaluation
              JOIN trading_cases c USING (case_id)
             WHERE evaluation.source='shadow_simulation' AND evaluation.status='pending'
               AND evaluation.next_quote_at_ms<=%s AND evaluation.due_at_ms>=%s
             ORDER BY evaluation.next_quote_at_ms,evaluation.case_id LIMIT %s
            """,
            (int(now_ms), int(now_ms), max(1, min(64, limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_shadow_quote_sample(
        self, *, case_id: str, prior_ref: str | None, tape_ref: str, sampled_at_ms: int
    ) -> bool:
        updated = self.conn.execute(
            """
            UPDATE trading_case_evaluations
               SET quote_tape_ref=%s,next_quote_at_ms=%s
             WHERE case_id=%s AND source='shadow_simulation' AND status='pending'
               AND quote_tape_ref IS NOT DISTINCT FROM %s
               AND next_quote_at_ms<=%s
            """,
            (tape_ref, int(sampled_at_ms) + 60_000, case_id, prior_ref, int(sampled_at_ms)),
        )
        return bool(updated.rowcount)

    def due_shadow_evaluations(self, *, now_ms: int, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT evaluation.*,c.target_selection,d.decision,frozen.evidence_ref,
                   tape.tape_ref AS root_market_tape_ref,
                   root_case.case_id AS root_case_id,
                   root_case.created_at_ms AS root_accepted_at_ms,
                   root_case.root_expires_at_ms AS root_expires_at_ms
              FROM trading_case_evaluations evaluation
              JOIN trading_cases c USING (case_id)
              JOIN trading_case_decisions d USING (case_id)
              LEFT JOIN trading_root_market_tapes tape
                ON tape.case_id=COALESCE(c.manifest->>'parent_case_id',c.case_id)
              LEFT JOIN trading_cases root_case ON root_case.case_id=tape.case_id
              LEFT JOIN LATERAL (
                SELECT a.evidence_ref FROM trading_case_attempts a
                 WHERE a.case_id=evaluation.case_id AND a.evidence_ref IS NOT NULL
                 ORDER BY a.claim_attempt LIMIT 1
              ) frozen ON true
             WHERE evaluation.source='shadow_simulation' AND evaluation.status='pending'
               AND evaluation.next_attempt_at_ms<=%s
             ORDER BY evaluation.next_attempt_at_ms,evaluation.case_id LIMIT %s
            """,
            (int(now_ms), max(1, min(64, limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def settle_shadow_evaluation(
        self,
        *,
        case_id: str,
        result: dict[str, Any],
        mark_path_ref: str | None,
        funding_ref: str | None,
        now_ms: int,
    ) -> bool:
        status = result.get("status")
        if status not in ("simulated", "unevaluable"):
            raise ValueError("shadow_evaluation_status_invalid")
        updated = self.conn.execute(
            """
            UPDATE trading_case_evaluations
               SET status=%s,reason=%s,mark_path_ref=%s,funding_ref=%s,
                   result=%s::jsonb,evaluated_at_ms=%s
             WHERE case_id=%s AND source='shadow_simulation'
               AND evaluation_version='shadow_net_v1' AND status='pending'
            """,
            (status, result.get("reason"), mark_path_ref, funding_ref, json.dumps(result), int(now_ms), case_id),
        )
        return bool(updated.rowcount)

    def retry_shadow_evaluation(self, *, case_id: str, next_attempt_at_ms: int) -> None:
        self.conn.execute(
            "UPDATE trading_case_evaluations SET next_attempt_at_ms=%s "
            "WHERE case_id=%s AND source='shadow_simulation' AND status='pending'",
            (int(next_attempt_at_ms), case_id),
        )

    def validate_signal_entry(self, *, entry_id: str, now_ns: int) -> tuple[bool, str]:
        """Persist the last Trading fact check before Nautilus submits a V2 entry."""
        asset = self.conn.execute(
            "SELECT c.target_asset_id FROM trading_trade_signals s "
            "JOIN trading_cases c ON c.case_id=s.case_id WHERE s.signal_id=%s",
            (entry_id,),
        ).fetchone()
        if asset is not None and asset["target_asset_id"] is not None:
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 683))", (asset["target_asset_id"],))
        row = self.conn.execute(
            """
            SELECT plan.entry_id,plan.entry_scope_id,plan.account_slot,
                   plan.runtime_mode_at_creation,plan.market_key,plan.direction,
                   plan.terminal_at_ns,signal.payload,signal.seq,
                   case_row.state,case_row.target_asset_id,
                   case_row.mapping_semantics_digest,case_row.root_expires_at_ms,
                   decision.publish_status,
                   EXISTS (
                     SELECT 1 FROM trading_triggers newer
                      JOIN trading_triggers original ON original.trigger_id=case_row.trigger_id
                     WHERE (newer.supersedes_ref IN (original.trigger_id,original.source_fact_key)
                            OR (newer.kind=original.kind AND newer.source_fact_key=original.source_fact_key
                                AND newer.source_revision<>original.source_revision
                                AND COALESCE((newer.payload->>'source_recorded_at_ms')::bigint,
                                             newer.first_visible_at_ms)
                                  > COALESCE((original.payload->>'source_recorded_at_ms')::bigint,
                                             original.first_visible_at_ms)))
                       AND newer.trigger_id<>original.trigger_id
                   ) AS superseded
              FROM trading_trade_plans plan
              JOIN trading_trade_signals signal ON signal.signal_id=plan.entry_id
              JOIN trading_cases case_row ON case_row.case_id=signal.case_id
              JOIN trading_case_decisions decision ON decision.case_id=case_row.case_id
             WHERE plan.entry_id=%s
            """,
            (entry_id,),
        ).fetchone()
        reason = "valid"
        if row is None:
            reason = "entry_fact_missing"
        else:
            signal = TradeSignalV2.model_validate_json(json.dumps(dict(row["payload"]) | {"seq": int(row["seq"])}))
            if row["terminal_at_ns"] is not None:
                reason = "plan_terminal"
            elif signal.expires_at_ns <= now_ns or signal.entry_envelope.root_expires_at_ns <= now_ns:
                reason = "expired"
            elif row["state"] != "SIGNAL_EMITTED" or row["publish_status"] != "published":
                reason = "decision_not_published"
            elif row["superseded"]:
                reason = "source_superseded"
            elif (
                row["entry_scope_id"] != signal.entry_scope_id
                or row["account_slot"] != signal.account_slot
                or row["runtime_mode_at_creation"] != signal.runtime_mode
                or row["market_key"] != signal.market_key
                or row["direction"] != signal.direction
                or row["target_asset_id"] != signal.asset_id
                or row["mapping_semantics_digest"] != signal.mapping_semantics_digest
            ):
                reason = "entry_identity_changed"
        self.conn.execute(
            "INSERT INTO trading_entry_validity_checks "
            "(entry_id,check_version,checked_at_ns,allowed,reason) "
            "VALUES (%s,'entry_validity_v1',%s,%s,%s)",
            (entry_id, int(now_ns), reason == "valid", reason),
        )
        return reason == "valid", reason
