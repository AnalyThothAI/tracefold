"""Bounded PostgreSQL snapshots for the credential-free Production V3 verifier."""

from __future__ import annotations

from typing import Any

from ..evidence_verification import FixedWindowAcceptanceV1, ProductionRollbackReceiptV2


class VerificationStorage:
    conn: Any

    def case_verification_snapshot(self, case_id: str) -> dict[str, Any] | None:
        case = self.conn.execute(
            """
            SELECT case_id, trigger_kind, state, policy_decision, policy_reason,
                   capital_disposition, capital_reason, created_at_ms, decided_at_ms
              FROM trading_cases
             WHERE case_id = %s
            """,
            (case_id,),
        ).fetchone()
        if case is None:
            return None
        gates = self.conn.execute(
            "SELECT count(*) AS n FROM trading_candidate_gate_decisions WHERE case_id = %s",
            (case_id,),
        ).fetchone()
        intents = self.conn.execute(
            """
            SELECT i.intent_id, i.intent_version, i.binding, i.execution_state, i.execution_phase,
                   i.terminal_outcome, i.entry_fenced_at_ms, i.entry_submitted_at_ms,
                   i.opened_at_ms, i.protected_at_ms, i.protection_order_id,
                   i.closed_at_ms, i.flat_verified_at_ms, i.realized_pnl_amount,
                   i.realized_pnl_currency, i.commissions_by_currency, i.funding_by_currency,
                   risk.status AS risk_status, risk.settlement_known
              FROM trading_intents i
              LEFT JOIN trading_capital_risk_reservation_state risk ON risk.intent_id = i.intent_id
             WHERE i.case_id = %s
             ORDER BY i.created_at_ms, i.intent_id
            """,
            (case_id,),
        ).fetchall()
        return {
            "case": dict(case),
            "gate_count": int(gates["n"] if gates is not None else 0),
            "intents": [dict(row) for row in intents],
        }

    def fixed_window_verification_snapshot(self, spec: FixedWindowAcceptanceV1) -> dict[str, Any]:
        row = self.conn.execute(
            """
            WITH gates AS (
              SELECT *
                FROM trading_candidate_gate_decisions
               WHERE trigger_kind = 'oi'
                 AND source_observed_at_ms >= %(start)s
                 AND source_observed_at_ms < %(end)s
            ), cases AS (
              SELECT c.*
                FROM trading_cases c
               WHERE c.trigger_kind = 'oi'
                 AND c.source_observed_at_ms >= %(start)s
                 AND c.source_observed_at_ms < %(end)s
            ), intents AS (
              SELECT i.*, c.capital_disposition,
                     risk.status AS risk_status, risk.settlement_known,
                     promotion.payload ->> 'approved_release' AS grant_release,
                     daily.approved_release AS risk_release
                FROM trading_intents i
                JOIN cases c ON c.case_id = i.case_id
                LEFT JOIN trading_capital_authorization_receipts auth
                  ON auth.authorization_receipt_sha256 = i.capital_authorization_receipt_sha256
                LEFT JOIN trading_capital_risk_reservations reservation
                  ON reservation.reservation_sha256 = auth.reservation_sha256
                LEFT JOIN trading_production_promotion_grants promotion
                  ON promotion.grant_sha256 = reservation.grant_sha256
                LEFT JOIN trading_daily_risk_policies daily
                  ON daily.risk_policy_sha256 = reservation.risk_policy_sha256
                LEFT JOIN trading_capital_risk_reservation_state risk ON risk.intent_id = i.intent_id
            ), per_case AS (
              SELECT c.case_id, c.capital_disposition, count(i.intent_id) AS intent_count
                FROM cases c
                LEFT JOIN intents i ON i.case_id = c.case_id
               GROUP BY c.case_id, c.capital_disposition
            )
            SELECT
              (SELECT count(*) FROM gates) AS gate_source_count,
              (SELECT array_agg(source_key ORDER BY source_key) FROM gates) AS gate_source_keys,
              (SELECT count(*) FROM gates
                WHERE gate_version <> %(gate_version)s
                   OR gate_config_digest <> %(gate_config)s) AS wrong_gate_contract_count,
              (SELECT count(*) FROM gates
                WHERE release_revision <> %(git_commit)s) AS wrong_release_source_count,
              (SELECT count(DISTINCT source_key) FROM gates) AS unique_source_count,
              (SELECT count(*) FROM gates WHERE status = 'CASE_CREATED') AS admitted_source_count,
              (SELECT count(*) FROM gates WHERE status <> 'CASE_CREATED') AS rejected_or_deferred_source_count,
              (SELECT count(*) FROM gates
                WHERE (status = 'CASE_CREATED') IS DISTINCT FROM (case_id IS NOT NULL)) AS invalid_gate_link_count,
              (SELECT count(*) FROM cases) AS case_count,
              (SELECT count(*) FROM cases c
                WHERE NOT EXISTS (SELECT 1 FROM gates g WHERE g.case_id = c.case_id)) AS case_without_gate_count,
              (SELECT count(*) FROM cases
                WHERE policy_decision IS NULL OR capital_disposition IS NULL) AS case_disposition_missing_count,
              (SELECT count(*) FROM per_case
                WHERE capital_disposition = 'allowed' AND intent_count <> 1) AS allowed_case_intent_mismatch_count,
              (SELECT count(*) FROM per_case
                WHERE capital_disposition <> 'allowed' AND intent_count <> 0) AS blocked_case_intent_mismatch_count,
              (SELECT count(*) FROM intents) AS intent_count,
              (SELECT count(*) FROM intents WHERE intent_version <> 'trade_intent_v3') AS non_v3_intent_count,
              (SELECT count(*) FROM intents
                WHERE grant_release IS DISTINCT FROM %(release_tag)s
                   OR risk_release IS DISTINCT FROM %(release_tag)s) AS intent_release_mismatch_count,
              (SELECT count(*) FROM intents WHERE execution_state <> 'TERMINAL') AS nonterminal_intent_count,
              (SELECT count(*) FROM intents
                WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW'))
                AS exposure_unknown_or_active_count,
              (SELECT count(*) FROM intents
                WHERE entry_submitted_at_ms IS NOT NULL
                  AND (entry_fenced_at_ms IS NULL OR entry_submitted_at_ms < entry_fenced_at_ms))
                AS provider_write_before_fence_count,
              (SELECT count(*) FROM intents
                WHERE opened_at_ms IS NOT NULL
                  AND (protected_at_ms IS NULL OR protection_order_id IS NULL)) AS unprotected_fill_count,
              (SELECT count(*) FROM intents WHERE terminal_outcome = 'CLOSED_FLAT') AS closed_flat_count,
              (SELECT count(*) FROM intents
                WHERE terminal_outcome = 'CLOSED_FLAT'
                  AND (closed_at_ms IS NULL OR flat_verified_at_ms IS NULL
                    OR risk_status <> 'SETTLED' OR settlement_known IS DISTINCT FROM TRUE))
                AS closed_flat_proof_missing_count,
              (SELECT count(*) FROM intents
                WHERE terminal_outcome = 'CLOSED_FLAT'
                  AND (realized_pnl_amount IS NULL OR realized_pnl_currency IS NULL
                    OR commissions_by_currency IS NULL OR funding_by_currency IS NULL))
                AS financial_accounting_missing_count
            """,
            {
                "gate_version": spec.gate_version,
                "gate_config": spec.gate_config_digest,
                "git_commit": spec.git_commit_sha,
                "release_tag": spec.release_tag,
                "start": spec.start_ms,
                "end": spec.end_ms,
            },
        ).fetchone()
        values = {} if row is None else dict(row)
        gate_source_keys = tuple(str(value) for value in (values.pop("gate_source_keys", None) or ()))
        counts = {name: int(value or 0) for name, value in values.items()}
        binding_rows = self.conn.execute(
            """
            WITH window_cases AS (
              SELECT c.*,
                     CASE lower(coalesce(c.manifest #>> '{primary_trigger,venue}', ''))
                       WHEN 'binance' THEN 'BINANCE_USDM'
                       WHEN 'binance.perp' THEN 'BINANCE_USDM'
                       WHEN 'binance.usdm' THEN 'BINANCE_USDM'
                       WHEN 'hyperliquid' THEN 'HYPERLIQUID_PERP'
                       WHEN 'hl.perp' THEN 'HYPERLIQUID_PERP'
                       WHEN 'hyperliquid.perp' THEN 'HYPERLIQUID_PERP'
                       ELSE 'UNBOUND'
                     END AS source_binding
                FROM trading_cases c
               WHERE c.trigger_kind = 'oi'
                 AND c.source_observed_at_ms >= %(start)s
                 AND c.source_observed_at_ms < %(end)s
            ), window_intents AS (
              SELECT i.*, c.source_binding,
                     risk.status AS risk_status,
                     risk.settlement_known AS settlement_known
                FROM trading_intents i
                JOIN window_cases c ON c.case_id = i.case_id
                LEFT JOIN trading_capital_risk_reservation_state risk ON risk.intent_id = i.intent_id
            ), bindings AS (
              SELECT source_binding AS binding FROM window_cases
              UNION SELECT binding FROM window_intents
            )
            SELECT b.binding,
                   (SELECT count(*) FROM window_cases c WHERE c.source_binding = b.binding) AS case_count,
                   (SELECT count(*) FROM window_intents i WHERE i.binding = b.binding) AS intent_count,
                   (SELECT count(*) FROM window_intents i
                     WHERE i.binding = b.binding AND i.terminal_outcome = 'CLOSED_FLAT') AS closed_flat_count,
                   (SELECT count(*) FROM window_intents i
                     WHERE i.binding = b.binding AND i.capital_authorization_receipt_sha256 IS NOT NULL)
                     AS reservation_count,
                   (SELECT count(*) FROM window_intents i
                     WHERE i.binding = b.binding AND i.entry_fenced_at_ms IS NOT NULL) AS fenced_count,
                   (SELECT count(*) FROM window_intents i
                     WHERE i.binding = b.binding AND i.entry_submitted_at_ms IS NOT NULL) AS submitted_count,
                   (SELECT count(*) FROM window_intents i
                     WHERE i.binding = b.binding AND i.opened_at_ms IS NOT NULL) AS opened_count,
                   (SELECT count(*) FROM window_intents i
                     WHERE i.binding = b.binding AND i.protected_at_ms IS NOT NULL) AS protected_count,
                   (SELECT count(*) FROM window_intents i
                     WHERE i.binding = b.binding AND i.closed_at_ms IS NOT NULL) AS closed_count,
                   (SELECT count(*) FROM window_intents i
                     WHERE i.binding = b.binding AND i.flat_verified_at_ms IS NOT NULL) AS flat_verified_count,
                   (SELECT count(*) FROM window_intents i WHERE i.binding = b.binding
                     AND (i.realized_pnl_amount IS NULL OR i.realized_pnl_currency IS NULL
                       OR i.commissions_by_currency IS NULL OR i.funding_by_currency IS NULL))
                     AS financial_missing_count,
                   (SELECT COALESCE(jsonb_object_agg(value, n ORDER BY value), '{}'::jsonb) FROM (
                     SELECT coalesce(c.policy_decision, 'missing') AS value, count(*) AS n
                       FROM window_cases c WHERE c.source_binding = b.binding GROUP BY value
                   ) x) AS policy_decisions,
                   (SELECT COALESCE(jsonb_object_agg(value, n ORDER BY value), '{}'::jsonb) FROM (
                     SELECT coalesce(c.policy_reason, 'missing') AS value, count(*) AS n
                       FROM window_cases c WHERE c.source_binding = b.binding GROUP BY value
                   ) x) AS policy_reasons,
                   (SELECT COALESCE(jsonb_object_agg(value, n ORDER BY value), '{}'::jsonb) FROM (
                     SELECT coalesce(c.capital_disposition, 'missing') AS value, count(*) AS n
                       FROM window_cases c WHERE c.source_binding = b.binding GROUP BY value
                   ) x) AS capital_dispositions,
                   (SELECT COALESCE(jsonb_object_agg(value, n ORDER BY value), '{}'::jsonb) FROM (
                     SELECT coalesce(c.capital_reason, 'missing') AS value, count(*) AS n
                       FROM window_cases c WHERE c.source_binding = b.binding GROUP BY value
                   ) x) AS capital_reasons,
                   (SELECT COALESCE(jsonb_object_agg(value, n ORDER BY value), '{}'::jsonb) FROM (
                     SELECT coalesce(i.entry_quote_q1 ->> 'reason', 'missing') AS value, count(*) AS n
                       FROM window_intents i WHERE i.binding = b.binding GROUP BY value
                   ) x) AS q1_reasons,
                   (SELECT COALESCE(jsonb_object_agg(value, n ORDER BY value), '{}'::jsonb) FROM (
                     SELECT coalesce(i.entry_quote_q2 ->> 'reason', 'missing') AS value, count(*) AS n
                       FROM window_intents i WHERE i.binding = b.binding GROUP BY value
                   ) x) AS q2_reasons,
                   (SELECT COALESCE(jsonb_object_agg(value, n ORDER BY value), '{}'::jsonb) FROM (
                     SELECT coalesce(i.risk_status, 'missing') AS value, count(*) AS n
                       FROM window_intents i WHERE i.binding = b.binding GROUP BY value
                   ) x) AS reservation_states,
                   (SELECT COALESCE(jsonb_object_agg(value, n ORDER BY value), '{}'::jsonb) FROM (
                     SELECT coalesce(i.execution_state, 'missing') AS value, count(*) AS n
                       FROM window_intents i WHERE i.binding = b.binding GROUP BY value
                   ) x) AS execution_states,
                   (SELECT COALESCE(jsonb_object_agg(value, n ORDER BY value), '{}'::jsonb) FROM (
                     SELECT coalesce(i.execution_phase, 'missing') AS value, count(*) AS n
                       FROM window_intents i WHERE i.binding = b.binding GROUP BY value
                   ) x) AS execution_phases,
                   (SELECT COALESCE(jsonb_object_agg(value, n ORDER BY value), '{}'::jsonb) FROM (
                     SELECT coalesce(i.terminal_outcome, 'missing') AS value, count(*) AS n
                       FROM window_intents i WHERE i.binding = b.binding GROUP BY value
                   ) x) AS terminal_outcomes,
                   (SELECT COALESCE(jsonb_object_agg(value, n ORDER BY value), '{}'::jsonb) FROM (
                     SELECT coalesce(i.reason_code, 'missing') AS value, count(*) AS n
                       FROM window_intents i WHERE i.binding = b.binding GROUP BY value
                   ) x) AS execution_reasons,
                   (SELECT COALESCE(jsonb_agg(jsonb_build_object(
                     'intent_id', i.intent_id,
                     'realized_pnl_amount', i.realized_pnl_amount,
                     'realized_pnl_currency', i.realized_pnl_currency,
                     'commissions_by_currency', i.commissions_by_currency,
                     'funding_by_currency', i.funding_by_currency
                   ) ORDER BY i.intent_id), '[]'::jsonb)
                      FROM window_intents i WHERE i.binding = b.binding) AS financials,
                   (SELECT jsonb_build_object(
                     'fence_avg_ms', avg(i.entry_fenced_at_ms - i.created_at_ms)::bigint,
                     'submit_avg_ms', avg(i.entry_submitted_at_ms - i.created_at_ms)::bigint,
                     'open_avg_ms', avg(i.opened_at_ms - i.created_at_ms)::bigint,
                     'protect_avg_ms', avg(i.protected_at_ms - i.created_at_ms)::bigint,
                     'close_avg_ms', avg(i.closed_at_ms - i.created_at_ms)::bigint,
                     'flat_avg_ms', avg(i.flat_verified_at_ms - i.created_at_ms)::bigint,
                     'flat_max_ms', max(i.flat_verified_at_ms - i.created_at_ms)
                   ) FROM window_intents i WHERE i.binding = b.binding) AS stage_latency
              FROM bindings b
             ORDER BY b.binding
            """,
            {"start": spec.start_ms, "end": spec.end_ms},
        ).fetchall()
        workers_runtime = self.conn.execute(
            "SELECT runtime_id::text AS runtime_id, runtime_revision, image_digest, "
            "lifecycle_state, started_at_ms, heartbeat_at_ms "
            "FROM workers_runtime WHERE singleton_key"
        ).fetchone()
        registration = self.conn.execute(
            "SELECT * FROM trading_production_release_registrations WHERE window_sha256 = %s",
            (spec.window_sha256,),
        ).fetchone()
        return {
            "counts": counts,
            "gate_source_keys": gate_source_keys,
            "by_binding": [dict(item) for item in binding_rows],
            "workers_runtime": None if workers_runtime is None else dict(workers_runtime),
            "release_registration": None if registration is None else dict(registration),
        }

    def release_verification_snapshot(
        self,
        *,
        evidence_receipts: tuple[str, ...],
        promotion_grants: tuple[str, ...],
        risk_policies: tuple[str, ...],
        canary_intents: tuple[str, ...],
        restart_runtime_ids: tuple[str, str],
    ) -> dict[str, Any]:
        migration = self.conn.execute("SELECT version_num FROM alembic_version").fetchone()
        runtime = self.conn.execute("SELECT control, arm_epoch FROM trading_runtime_state WHERE id = 1").fetchone()
        receipts = self.conn.execute(
            """
            WITH RECURSIVE evidence_chain AS (
              SELECT * FROM trading_evidence_clock_receipts WHERE receipt_sha256 = ANY(%s)
              UNION
              SELECT parent.*
                FROM trading_evidence_clock_receipts parent
                JOIN evidence_chain child ON child.parent_receipt_sha256 = parent.receipt_sha256
            )
            SELECT receipt_sha256, receipt_kind, terminal, binding, parent_receipt_sha256,
                   artifact_sha256, corpus_sha256, protocol_sha256,
                   created_at_ms, recorded_at_ms, payload
              FROM evidence_chain ORDER BY receipt_sha256
            """,
            (list(evidence_receipts),),
        ).fetchall()
        grants = self.conn.execute(
            "SELECT grant_sha256, binding, risk_policy_sha256, sealed_corpus_sha256, "
            "locked_future_report_sha256, payload FROM trading_production_promotion_grants "
            "WHERE grant_sha256 = ANY(%s) ORDER BY grant_sha256",
            (list(promotion_grants),),
        ).fetchall()
        policies = self.conn.execute(
            "SELECT risk_policy_sha256, approved_release FROM trading_daily_risk_policies "
            "WHERE risk_policy_sha256 = ANY(%s) ORDER BY risk_policy_sha256",
            (list(risk_policies),),
        ).fetchall()
        bindings = self.conn.execute(
            """
            SELECT runtime.binding, runtime.catalog_snapshot_sha256,
                   runtime.capability_snapshot_sha256, runtime.execution_binding_sha256,
                   runtime.account_generation, runtime.credential_fingerprint,
                   binding.payload AS execution_binding
              FROM trading_binding_runtime runtime
              LEFT JOIN trading_execution_bindings binding
                ON binding.binding_sha256 = runtime.execution_binding_sha256
             ORDER BY runtime.binding
            """
        ).fetchall()
        canaries = self.conn.execute(
            """
            SELECT intent.intent_id, intent.intent_version, intent.binding,
                   intent.execution_state, intent.terminal_outcome,
                   intent.entry_fenced_at_ms, intent.entry_submitted_at_ms,
                   intent.opened_at_ms, intent.protected_at_ms, intent.protection_order_id,
                   intent.closed_at_ms, intent.flat_verified_at_ms,
                   intent.realized_pnl_amount, intent.realized_pnl_currency,
                   intent.commissions_by_currency, intent.funding_by_currency,
                   reservation.grant_sha256, reservation.risk_policy_sha256,
                   promotion.sealed_corpus_sha256, promotion.locked_future_report_sha256,
                   risk.status AS risk_status, risk.settlement_known
              FROM trading_intents intent
              JOIN trading_capital_authorization_receipts auth
                ON auth.authorization_receipt_sha256 = intent.capital_authorization_receipt_sha256
              JOIN trading_capital_risk_reservations reservation
                ON reservation.reservation_sha256 = auth.reservation_sha256
              JOIN trading_production_promotion_grants promotion
                ON promotion.grant_sha256 = reservation.grant_sha256
              LEFT JOIN trading_capital_risk_reservation_state risk ON risk.intent_id = intent.intent_id
             WHERE intent.intent_id = ANY(%s)
             ORDER BY intent.intent_id
            """,
            (list(canary_intents),),
        ).fetchall()
        runtime_starts = self.conn.execute(
            """
            SELECT start_sha256, runtime_id::text AS runtime_id, runtime_revision,
                   image_digest, nautilus_version, nautilus_source_git_commit,
                   nautilus_wheel_identity, started_at_ms
              FROM trading_nautilus_runtime_starts
             WHERE runtime_id = ANY(%s)
             ORDER BY runtime_id
            """,
            (list(restart_runtime_ids),),
        ).fetchall()
        return {
            "migration_head": None if migration is None else str(migration["version_num"]),
            "runtime": None if runtime is None else dict(runtime),
            "receipts": [dict(item) for item in receipts],
            "grants": [dict(item) for item in grants],
            "risk_policies": [dict(item) for item in policies],
            "bindings": [dict(item) for item in bindings],
            "canaries": [dict(item) for item in canaries],
            "runtime_starts": [dict(item) for item in runtime_starts],
        }

    def rollback_verification_snapshot(self, receipt: ProductionRollbackReceiptV2) -> dict[str, Any]:
        runtime = self.conn.execute("SELECT control FROM trading_runtime_state WHERE id = 1").fetchone()
        decision = self.conn.execute(
            "SELECT state, heartbeat_at_ms, reason FROM trading_decision_runtime WHERE id = 1"
        ).fetchone()
        workers = self.conn.execute(
            "SELECT runtime_id::text AS runtime_id, lifecycle_state, started_at_ms, heartbeat_at_ms "
            "FROM workers_runtime WHERE singleton_key"
        ).fetchone()
        registration = self.conn.execute(
            "SELECT workers_runtime_id::text AS workers_runtime_id, "
            "serve_runtime_id::text AS serve_runtime_id "
            "FROM trading_production_release_registrations WHERE release_sha256 = %s",
            (receipt.release_candidate_sha256,),
        ).fetchone()
        active = self.conn.execute(
            "SELECT count(*) AS n FROM trading_intents "
            "WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')"
        ).fetchone()
        risk = self.conn.execute(
            "SELECT count(*) AS n FROM trading_capital_risk_reservation_state "
            "WHERE status IN ('RESERVED', 'FENCED', 'OPEN', 'MANUAL_REVIEW')"
        ).fetchone()
        bindings = self.conn.execute(
            "SELECT binding, runtime_state, account_state, catalog_state, capability_state, "
            "active_arm_receipt_sha256, heartbeat_at_ms, reason FROM trading_binding_runtime "
            "WHERE binding = ANY(%s) ORDER BY binding",
            (list(receipt.bindings),),
        ).fetchall()
        post_rollback = self.conn.execute(
            """
            SELECT count(*) FILTER (WHERE created_at_ms > %(rollback)s) AS intent_count,
                   count(*) FILTER (WHERE entry_submitted_at_ms > %(rollback)s) AS submission_count
              FROM trading_intents
            """,
            {"rollback": receipt.rolled_back_at_ms},
        ).fetchone()
        grants = self.conn.execute(
            """
            SELECT promotion.grant_sha256, promotion.expires_at_ms, revocation.revoked_at_ms
              FROM trading_production_promotion_grants promotion
              LEFT JOIN trading_promotion_grant_revocations revocation
                ON revocation.grant_sha256 = promotion.grant_sha256
             WHERE promotion.grant_sha256 = ANY(%s)
             ORDER BY promotion.grant_sha256
            """,
            (list(receipt.grant_sha256s),),
        ).fetchall()
        return {
            "control": None if runtime is None else str(runtime["control"]),
            "decision_runtime": None if decision is None else dict(decision),
            "workers_runtime": None if workers is None else dict(workers),
            "release_registration": None if registration is None else dict(registration),
            "active_intent_count": int(active["n"] if active is not None else 0),
            "active_risk_count": int(risk["n"] if risk is not None else 0),
            "post_rollback_intent_count": int(post_rollback["intent_count"] if post_rollback is not None else 0),
            "post_rollback_submission_count": int(
                post_rollback["submission_count"] if post_rollback is not None else 0
            ),
            "bindings": [dict(item) for item in bindings],
            "grants": [dict(item) for item in grants],
        }


__all__ = ["VerificationStorage"]
