"""Durable intake, fenced work and frozen decisions for the Analysis process."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, cast

from tracefold.trading.engine.policy import decision_identity
from tracefold.trading.engine.target import TargetSelection
from tracefold.trading.execution_contracts import TradeSignalV2
from tracefold.trading.storage.execution_stream import ExecutionStreamStorage, PreparedTradeSignal


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    ).hexdigest()


_OUTCOME_HORIZONS = (900, 3_600, 14_400, 86_400)
_OUTCOME_VERSION = "price_path_v1"
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
            "source_observed_at_ms,first_visible_at_ms "
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
        return trigger_id, case_id, "accepted"

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
        max_watch_rechecks: int = 2,
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
                VALUES (%s,%s,'trade_assessment','v1',%s,%s,%s,%s::jsonb,
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
                decision.get("reason", "") if decision else analysis_status,
                analysis_status,
                evidence_ref,
                int(now_ms),
                int(now_ms),
                case_id,
            ),
        )
        if action == "WATCH" and decision is not None:
            self._schedule_watch_recheck(
                parent=row,
                decision=decision,
                now_ms=now_ms,
                max_rechecks=max_watch_rechecks,
            )
        return True

    def _schedule_watch_recheck(
        self,
        *,
        parent: dict[str, Any],
        decision: dict[str, Any],
        now_ms: int,
        max_rechecks: int,
    ) -> None:
        sequence = int(parent["recheck_seq"] or 0) + 1
        if sequence > max_rechecks:
            return
        watch = decision.get("watch_condition")
        if not isinstance(watch, dict):
            return
        delay = int(watch.get("due_after_seconds") or 0)
        if not 30 <= delay <= 300:
            return
        due_at = int(now_ms) + delay * 1_000
        root_expires = int(parent["root_expires_at_ms"])
        if due_at >= root_expires:
            return
        if parent["entry_scope_id"] is None:
            return
        used = self.conn.execute(
            "SELECT 1 FROM trading_trade_plans WHERE entry_scope_id=%s LIMIT 1",
            (parent["entry_scope_id"],),
        ).fetchone()
        if used is not None:
            return
        case_id = _sha((parent["trigger_id"], "recheck", sequence))
        manifest = dict(parent["manifest"])
        manifest.update(
            {
                "parent_case_id": parent["case_id"],
                "recheck_seq": sequence,
                "watch_condition": watch,
                "scheduled_at_ms": int(now_ms),
                "due_at_ms": due_at,
            }
        )
        self.conn.execute(
            """
            INSERT INTO trading_cases
              (case_id, underlying_key, trigger_kind, primary_source_key, manifest,
               manifest_sha256, state, policy_decision, policy_reason, observed_at_ms,
               source_observed_at_ms, trigger_persisted_at_ms, created_at_ms, updated_at_ms,
               trigger_id, run_kind, recheck_seq, target_asset_id, target_selection,
               entry_scope_id, mapping_semantics_digest, root_expires_at_ms,
               work_deadline_at_ms, next_attempt_at_ms, analysis_status)
            VALUES (%s,%s,%s,%s,%s::jsonb,%s,'PENDING','not_run','scheduled_watch',%s,
                    %s,%s,%s,%s,%s,'recheck',%s,%s,%s::jsonb,%s,%s,%s,%s,%s,'pending')
            ON CONFLICT DO NOTHING
            """,
            (
                case_id,
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
                min(due_at + 120_000, root_expires),
                due_at,
            ),
        )

    def due_analysis_outcomes(self, *, now_ms: int, limit: int = 16) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT o.case_id,o.axis,o.horizon_seconds,o.label_version,o.available_at_ms,
                   c.source_observed_at_ms,c.decided_at_ms,c.target_selection
              FROM trading_case_outcomes o JOIN trading_cases c USING (case_id)
             WHERE o.status='pending' AND o.available_at_ms<=%s
               AND o.next_attempt_at_ms<=%s
             ORDER BY o.available_at_ms,o.case_id,o.axis,o.horizon_seconds LIMIT %s
            """,
            (int(now_ms), int(now_ms), max(1, min(128, limit))),
        ).fetchall()
        return [dict(row) for row in rows]

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
