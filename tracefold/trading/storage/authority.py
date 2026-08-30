"""Persistence for immutable capital authority facts and their active arm pointers."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, cast

from psycopg import sql

from ..capital_authority import (
    CapitalAuthorizationReceiptV1,
    CapitalRiskReservationV1,
    DailyRiskPolicyV1,
    OperatorArmReceiptV1,
    ProductionPromotionGrantRevocationV1,
    ProductionPromotionGrantV1,
)
from ..contracts import VenueBinding
from ..evidence_clock import CandidateLockedV1
from ..intent import TradeIntent
from .sql_values import _dumps


class AuthorityStorage:
    conn: Any

    # ---------------------------------------------------------------- immutable approvals
    def append_daily_risk_policy(self, value: DailyRiskPolicyV1, *, created_at_ms: int) -> bool:
        self._lock_capital_runtime()
        if value.issued_at_ms > created_at_ms:
            raise ValueError("daily_risk_policy_issued_in_future")
        digest = value.risk_policy_sha256
        inserted = self.conn.execute(
            """
            INSERT INTO trading_daily_risk_policies (
              risk_policy_sha256, approved_release, effective_from_ms, expires_at_ms,
              created_at_ms, payload
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (risk_policy_sha256) DO NOTHING
            RETURNING risk_policy_sha256
            """,
            (
                digest,
                value.approved_release,
                value.effective_from_ms,
                value.expires_at_ms,
                int(created_at_ms),
                _dumps(value.model_dump(mode="json")),
            ),
        ).fetchone()
        self._require_payload_identity(
            table="trading_daily_risk_policies",
            key_name="risk_policy_sha256",
            key=digest,
            payload=value.model_dump(mode="json"),
        )
        return inserted is not None

    def daily_risk_policy(self, risk_policy_sha256: str) -> DailyRiskPolicyV1 | None:
        row = self.conn.execute(
            "SELECT payload FROM trading_daily_risk_policies WHERE risk_policy_sha256 = %s",
            (risk_policy_sha256,),
        ).fetchone()
        return None if row is None else DailyRiskPolicyV1.model_validate(row["payload"])

    def append_production_promotion_grant(
        self,
        value: ProductionPromotionGrantV1,
        *,
        created_at_ms: int,
    ) -> bool:
        self._lock_capital_runtime()
        if value.issued_at_ms > created_at_ms:
            raise ValueError("production_grant_issued_in_future")
        policy = self.daily_risk_policy(value.risk_policy_sha256)
        if policy is None or policy.cost_model_sha256 != value.cost_model_sha256:
            raise ValueError("production_grant_risk_policy_mismatch")
        future_result = cast(Any, self).future_holdout_result_for_artifact(value.locked_future_report_sha256)
        if (
            future_result is None
            or future_result.terminal != "PROMOTE"
            or future_result.binding != value.binding
            or future_result.sealed_corpus_sha256 != value.sealed_corpus_sha256
        ):
            raise ValueError("production_grant_future_evidence_mismatch")
        candidate = cast(Any, self).locked_candidate_for_receipt(future_result.candidate_receipt_sha256)
        if not isinstance(candidate, CandidateLockedV1) or (
            candidate.binding != value.binding
            or candidate.sealed_corpus_sha256 != value.sealed_corpus_sha256
            or candidate.protocol_sha256 != future_result.protocol_sha256
            or candidate.source_contract_sha256 != value.source_contract_sha256
            or candidate.feature_contract_sha256 != value.feature_contract_sha256
            or candidate.policy_id != value.policy_id
            or candidate.policy_config_sha256 != value.policy_config_sha256
            or candidate.execution.cost_model_sha256 != value.cost_model_sha256
            or candidate.execution.adapter_contract_sha256 != value.adapter_contract_sha256
            or candidate.execution.execution_policy_sha256 != value.execution_policy_sha256
            or candidate.execution.quote_contract_sha256 != value.quote_contract_sha256
            or candidate.execution.protection_contract_sha256 != value.protection_contract_sha256
        ):
            raise ValueError("production_grant_candidate_evidence_mismatch")
        digest = value.grant_sha256
        inserted = self.conn.execute(
            """
            INSERT INTO trading_production_promotion_grants (
              grant_sha256, binding, risk_policy_sha256, issued_at_ms, review_at_ms,
              expires_at_ms, created_at_ms, sealed_corpus_sha256,
              locked_future_report_sha256, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (grant_sha256) DO NOTHING
            RETURNING grant_sha256
            """,
            (
                digest,
                value.binding,
                value.risk_policy_sha256,
                value.issued_at_ms,
                value.review_at_ms,
                value.expires_at_ms,
                int(created_at_ms),
                value.sealed_corpus_sha256,
                value.locked_future_report_sha256,
                _dumps(value.model_dump(mode="json")),
            ),
        ).fetchone()
        self._require_payload_identity(
            table="trading_production_promotion_grants",
            key_name="grant_sha256",
            key=digest,
            payload=value.model_dump(mode="json"),
        )
        return inserted is not None

    def production_promotion_grant(self, grant_sha256: str) -> ProductionPromotionGrantV1 | None:
        row = self.conn.execute(
            "SELECT payload FROM trading_production_promotion_grants WHERE grant_sha256 = %s",
            (grant_sha256,),
        ).fetchone()
        return None if row is None else ProductionPromotionGrantV1.model_validate(row["payload"])

    def active_promotion_grants(
        self,
        *,
        binding: VenueBinding,
        now_ms: int,
    ) -> tuple[ProductionPromotionGrantV1, ...]:
        rows = self.conn.execute(
            """
            SELECT promotion.payload
              FROM trading_production_promotion_grants promotion
              LEFT JOIN trading_promotion_grant_revocations revoked
                ON revoked.grant_sha256 = promotion.grant_sha256
             WHERE promotion.binding = %s
               AND promotion.issued_at_ms <= %s
               AND promotion.review_at_ms > %s
               AND promotion.expires_at_ms > %s
               AND (revoked.grant_sha256 IS NULL OR revoked.revoked_at_ms > %s)
             ORDER BY promotion.grant_sha256
            """,
            (binding, int(now_ms), int(now_ms), int(now_ms), int(now_ms)),
        ).fetchall()
        return tuple(ProductionPromotionGrantV1.model_validate(row["payload"]) for row in rows)

    def promotion_grants(self, *, binding: VenueBinding) -> tuple[ProductionPromotionGrantV1, ...]:
        rows = self.conn.execute(
            "SELECT payload FROM trading_production_promotion_grants WHERE binding = %s ORDER BY grant_sha256",
            (binding,),
        ).fetchall()
        return tuple(ProductionPromotionGrantV1.model_validate(row["payload"]) for row in rows)

    def revoke_production_promotion_grant(
        self,
        value: ProductionPromotionGrantRevocationV1,
        *,
        created_at_ms: int,
    ) -> bool:
        self._lock_capital_runtime()
        grant = self.production_promotion_grant(value.grant_sha256)
        if grant is None:
            raise ValueError("production_grant_missing")
        if value.revoked_at_ms < grant.issued_at_ms or value.revoked_at_ms > created_at_ms:
            raise ValueError("production_grant_revocation_clock_invalid")
        inserted = self.conn.execute(
            """
            INSERT INTO trading_promotion_grant_revocations (
              revocation_sha256, grant_sha256, revoked_at_ms, created_at_ms, payload
            ) VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (revocation_sha256) DO NOTHING
            RETURNING revocation_sha256
            """,
            (
                value.revocation_sha256,
                value.grant_sha256,
                value.revoked_at_ms,
                int(created_at_ms),
                _dumps(value.model_dump(mode="json")),
            ),
        ).fetchone()
        return inserted is not None

    def append_operator_arm_receipt(self, value: OperatorArmReceiptV1, *, created_at_ms: int) -> bool:
        runtime = self.conn.execute(
            "SELECT control, arm_epoch FROM trading_runtime_state WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if runtime is None:
            raise RuntimeError("trading_runtime_state_missing")
        if runtime["control"] != "PAUSED":
            raise ValueError("operator_arm_requires_paused")
        if value.armed_at_ms > created_at_ms or value.reconciled_at_ms > created_at_ms:
            raise ValueError("operator_arm_issued_in_future")
        if int(runtime["arm_epoch"]) != value.arm_epoch:
            raise ValueError("operator_arm_epoch_mismatch")
        grant = self.production_promotion_grant(value.grant_sha256)
        policy = self.daily_risk_policy(value.risk_policy_sha256)
        if grant is None or policy is None or grant.risk_policy_sha256 != policy.risk_policy_sha256:
            raise ValueError("operator_arm_authority_missing")
        digest = value.arm_receipt_sha256
        inserted = self.conn.execute(
            """
            INSERT INTO trading_operator_arm_receipts (
              arm_receipt_sha256, arm_epoch, binding, grant_sha256, risk_policy_sha256,
              armed_at_ms, expires_at_ms, created_at_ms, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (arm_receipt_sha256) DO NOTHING
            RETURNING arm_receipt_sha256
            """,
            (
                digest,
                value.arm_epoch,
                value.binding,
                value.grant_sha256,
                value.risk_policy_sha256,
                value.armed_at_ms,
                value.expires_at_ms,
                int(created_at_ms),
                _dumps(value.model_dump(mode="json")),
            ),
        ).fetchone()
        self._require_payload_identity(
            table="trading_operator_arm_receipts",
            key_name="arm_receipt_sha256",
            key=digest,
            payload=value.model_dump(mode="json"),
        )
        return inserted is not None

    def operator_arm_receipt(self, arm_receipt_sha256: str) -> OperatorArmReceiptV1 | None:
        row = self.conn.execute(
            "SELECT payload FROM trading_operator_arm_receipts WHERE arm_receipt_sha256 = %s",
            (arm_receipt_sha256,),
        ).fetchone()
        return None if row is None else OperatorArmReceiptV1.model_validate(row["payload"])

    def activate_operator_arms(self, arm_receipt_sha256s: Sequence[str], *, now_ms: int) -> bool:
        """Move PAUSED to RUNNING only when every configured binding has one current exact arm."""

        requested = tuple(sorted(set(arm_receipt_sha256s)))
        if not requested or len(requested) != len(arm_receipt_sha256s):
            raise ValueError("operator_arm_activation_set_invalid")
        runtime = self.conn.execute(
            "SELECT control, arm_epoch FROM trading_runtime_state WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if runtime is None:
            raise RuntimeError("trading_runtime_state_missing")
        if runtime["control"] != "PAUSED":
            raise ValueError("operator_arm_activation_requires_paused")
        epoch = int(runtime["arm_epoch"])
        rows = self.conn.execute(
            """
            SELECT binding, credential_state, credential_fingerprint, runtime_state, account_state,
                   account_generation, catalog_state, catalog_snapshot_sha256, capability_state,
                   capability_snapshot_sha256, execution_binding_sha256
              FROM trading_binding_runtime
             ORDER BY binding
               FOR UPDATE
            """
        ).fetchall()
        configured = {str(row["binding"]) for row in rows if row["credential_state"] == "configured"}
        if any(row["credential_state"] == "invalid" or row["account_state"] == "exposure_present" for row in rows):
            raise ValueError("operator_arm_global_account_unproven")
        arms = tuple(self.operator_arm_receipt(digest) for digest in requested)
        if any(arm is None for arm in arms):
            raise ValueError("operator_arm_receipt_missing")
        typed_arms = cast(tuple[OperatorArmReceiptV1, ...], arms)
        if {arm.binding for arm in typed_arms} != configured:
            raise ValueError("operator_arm_configured_binding_set_mismatch")
        by_binding = {str(row["binding"]): row for row in rows}
        for arm in typed_arms:
            row = by_binding[arm.binding]
            grant = self.production_promotion_grant(arm.grant_sha256)
            policy = self.daily_risk_policy(arm.risk_policy_sha256)
            revoked = self.conn.execute(
                "SELECT 1 FROM trading_promotion_grant_revocations WHERE grant_sha256 = %s AND revoked_at_ms <= %s",
                (arm.grant_sha256, int(now_ms)),
            ).fetchone()
            if (
                arm.arm_epoch != epoch
                or arm.armed_at_ms > now_ms
                or arm.expires_at_ms <= now_ms
                or grant is None
                or policy is None
                or revoked is not None
                or grant.review_at_ms <= now_ms
                or grant.expires_at_ms <= now_ms
                or policy.effective_from_ms > now_ms
                or policy.expires_at_ms <= now_ms
                or arm.approved_release != grant.approved_release
                or policy.approved_release != grant.approved_release
                or arm.binding != grant.binding
                or arm.account_generation != int(row["account_generation"])
                or arm.credential_fingerprint != row["credential_fingerprint"]
                or arm.catalog_snapshot_sha256 != row["catalog_snapshot_sha256"]
                or arm.capability_snapshot_sha256 != row["capability_snapshot_sha256"]
                or arm.execution_binding_sha256 != row["execution_binding_sha256"]
                or row["runtime_state"] != "ready"
                or row["account_state"] != "reconciled_flat"
                or row["catalog_state"] != "ready"
                or row["capability_state"] != "ready"
            ):
                raise ValueError(f"operator_arm_identity_invalid:{arm.binding}")
        self.conn.execute("UPDATE trading_binding_runtime SET active_arm_receipt_sha256 = NULL")
        for arm in typed_arms:
            self.conn.execute(
                "UPDATE trading_binding_runtime SET active_arm_receipt_sha256 = %s, updated_at_ms = %s "
                "WHERE binding = %s",
                (arm.arm_receipt_sha256, int(now_ms), arm.binding),
            )
        updated = self.conn.execute(
            "UPDATE trading_runtime_state SET control = 'RUNNING', updated_at_ms = %s "
            "WHERE id = 1 AND control = 'PAUSED' AND arm_epoch = %s RETURNING id",
            (int(now_ms), epoch),
        ).fetchone()
        return updated is not None

    # ---------------------------------------------------------------- atomic reservation bundle
    def insert_authorized_intent_bundle(
        self,
        *,
        reservation: CapitalRiskReservationV1,
        receipt: CapitalAuthorizationReceiptV1,
        intent: TradeIntent,
        now_ms: int,
    ) -> bool:
        """Insert reservation, receipt, Intent, initial state, and event in the caller's transaction."""

        if (
            receipt.reservation_sha256 != reservation.reservation_sha256
            or receipt.case_id != reservation.case_id
            or intent.case_id != reservation.case_id
            or intent.capital_authorization_receipt_sha256 != receipt.authorization_receipt_sha256
            or intent.economic_lifecycle_id != reservation.economic_lifecycle_id
        ):
            raise ValueError("capital_authorized_bundle_identity_mismatch")
        self.conn.execute(
            """
            INSERT INTO trading_capital_risk_reservations (
              reservation_sha256, case_id, economic_lifecycle_id, binding, settlement_asset,
              risk_policy_sha256, grant_sha256, arm_receipt_sha256,
              risk_day_start_ms, risk_day_end_ms, target_notional, planned_risk_amount,
              created_at_ms, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                reservation.reservation_sha256,
                reservation.case_id,
                reservation.economic_lifecycle_id,
                reservation.binding,
                reservation.settlement_asset,
                reservation.risk_policy_sha256,
                reservation.grant_sha256,
                reservation.arm_receipt_sha256,
                reservation.risk_day_start_ms,
                reservation.risk_day_end_ms,
                reservation.target_notional,
                reservation.planned_risk_amount,
                reservation.created_at_ms,
                _dumps(reservation.model_dump(mode="json")),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO trading_capital_authorization_receipts (
              authorization_receipt_sha256, reservation_sha256, case_id, binding,
              created_at_ms, payload
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                receipt.authorization_receipt_sha256,
                receipt.reservation_sha256,
                receipt.case_id,
                receipt.binding,
                receipt.evaluated_at_ms,
                _dumps(receipt.model_dump(mode="json")),
            ),
        )
        if not cast(Any, self).insert_intent(intent):
            raise RuntimeError("capital_authorized_intent_insert_failed")
        self.conn.execute(
            """
            INSERT INTO trading_capital_risk_reservation_state (
              reservation_sha256, intent_id, status, current_planned_risk_amount,
              attempt_consumed, attempt_day_start_ms, attempt_day_end_ms,
              settlement_known, updated_at_ms
            ) VALUES (%s, %s, 'RESERVED', %s, false, NULL, NULL, false, %s)
            """,
            (reservation.reservation_sha256, intent.intent_id, reservation.planned_risk_amount, int(now_ms)),
        )
        self._append_risk_event(
            reservation_sha256=reservation.reservation_sha256,
            intent_id=intent.intent_id,
            event_kind="RESERVED",
            current_planned_risk_amount=reservation.planned_risk_amount,
            attempt_consumed=False,
            occurred_at_ms=now_ms,
        )
        return True

    def commit_capital_risk_fence(
        self,
        *,
        intent_id: str,
        reservation_sha256: str,
        planned_risk_amount: Decimal,
        attempt_day_start_ms: int,
        attempt_day_end_ms: int,
        now_ms: int,
    ) -> None:
        row = self.conn.execute(
            """
            UPDATE trading_capital_risk_reservation_state
               SET status = 'FENCED', current_planned_risk_amount = %s,
                   attempt_consumed = true, attempt_day_start_ms = %s, attempt_day_end_ms = %s,
                   updated_at_ms = %s
             WHERE reservation_sha256 = %s AND intent_id = %s
               AND status = 'RESERVED' AND NOT attempt_consumed
               AND current_planned_risk_amount >= %s
         RETURNING reservation_sha256
            """,
            (
                planned_risk_amount,
                int(attempt_day_start_ms),
                int(attempt_day_end_ms),
                int(now_ms),
                reservation_sha256,
                intent_id,
                planned_risk_amount,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("capital_risk_fence_state_invalid")
        self._append_risk_event(
            reservation_sha256=reservation_sha256,
            intent_id=intent_id,
            event_kind="FENCE_COMMITTED",
            current_planned_risk_amount=planned_risk_amount,
            attempt_consumed=True,
            occurred_at_ms=now_ms,
        )

    def release_capital_risk_zero_submit(self, *, intent_id: str, now_ms: int) -> bool:
        """Release planned risk at authoritative zero-submit; never refund a committed attempt."""

        current = self.conn.execute(
            """
            SELECT reservation_sha256, status, attempt_consumed
              FROM trading_capital_risk_reservation_state
             WHERE intent_id = %s
               FOR UPDATE
            """,
            (intent_id,),
        ).fetchone()
        if current is None:
            raise RuntimeError("capital_risk_state_missing")
        if current["status"] in {"RELEASED", "SETTLED"}:
            return False
        if current["status"] not in {"RESERVED", "FENCED"}:
            raise RuntimeError("capital_risk_zero_submit_state_invalid")
        updated = self.conn.execute(
            """
            UPDATE trading_capital_risk_reservation_state
               SET status = 'RELEASED', current_planned_risk_amount = 0,
                   settlement_known = false, updated_at_ms = %s
             WHERE intent_id = %s AND status = %s
         RETURNING reservation_sha256
            """,
            (int(now_ms), intent_id, current["status"]),
        ).fetchone()
        if updated is None:
            raise RuntimeError("capital_risk_zero_submit_transition_failed")
        self._append_risk_event(
            reservation_sha256=str(current["reservation_sha256"]),
            intent_id=intent_id,
            event_kind="PLANNED_RISK_RELEASED",
            current_planned_risk_amount=Decimal(0),
            attempt_consumed=bool(current["attempt_consumed"]),
            occurred_at_ms=now_ms,
        )
        return True

    def mark_capital_risk_open(self, *, intent_id: str, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_capital_risk_reservation_state
               SET status = 'OPEN', updated_at_ms = %s
             WHERE intent_id = %s AND status IN ('FENCED', 'MANUAL_REVIEW') AND attempt_consumed
         RETURNING reservation_sha256, current_planned_risk_amount
            """,
            (int(now_ms), intent_id),
        ).fetchone()
        if row is None:
            return False
        self._append_risk_event(
            reservation_sha256=str(row["reservation_sha256"]),
            intent_id=intent_id,
            event_kind="EXPOSURE_OPENED",
            current_planned_risk_amount=row["current_planned_risk_amount"],
            attempt_consumed=True,
            occurred_at_ms=now_ms,
        )
        return True

    def mark_capital_risk_manual_review(self, *, intent_id: str, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE trading_capital_risk_reservation_state
               SET status = 'MANUAL_REVIEW', updated_at_ms = %s
             WHERE intent_id = %s AND status IN ('FENCED', 'OPEN')
         RETURNING reservation_sha256, current_planned_risk_amount, attempt_consumed
            """,
            (int(now_ms), intent_id),
        ).fetchone()
        if row is None:
            return False
        self._append_risk_event(
            reservation_sha256=str(row["reservation_sha256"]),
            intent_id=intent_id,
            event_kind="MANUAL_REVIEW",
            current_planned_risk_amount=row["current_planned_risk_amount"],
            attempt_consumed=bool(row["attempt_consumed"]),
            occurred_at_ms=now_ms,
        )
        return True

    def settle_capital_risk_closed_flat(
        self,
        *,
        intent_id: str,
        realized_pnl_amount: Decimal | None,
        realized_pnl_currency: str | None,
        commissions_by_currency: dict[str, str] | None,
        funding_by_currency: dict[str, str] | None,
        now_ms: int,
    ) -> bool:
        """Settle once from authoritative same-asset PnL, fees, and signed funding cash flows."""

        row = self.conn.execute(
            """
            SELECT state.reservation_sha256, state.status, state.attempt_consumed,
                   reservation.settlement_asset
              FROM trading_capital_risk_reservation_state state
              JOIN trading_capital_risk_reservations reservation
                ON reservation.reservation_sha256 = state.reservation_sha256
             WHERE state.intent_id = %s
               FOR UPDATE OF state
            """,
            (intent_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("capital_risk_state_missing")
        if row["status"] == "SETTLED":
            return True
        settlement_asset = str(row["settlement_asset"])
        commission_amounts: list[Decimal] = []
        funding_amounts: list[Decimal] = []
        known = (
            row["status"] in {"OPEN", "MANUAL_REVIEW"}
            and bool(row["attempt_consumed"])
            and realized_pnl_amount is not None
            and realized_pnl_currency == settlement_asset
            and commissions_by_currency is not None
            and set(commissions_by_currency).issubset({settlement_asset})
            and funding_by_currency is not None
            and set(funding_by_currency).issubset({settlement_asset})
        )
        if known:
            try:
                commission_amounts = [Decimal(value) for value in (commissions_by_currency or {}).values()]
                funding_amounts = [Decimal(value) for value in (funding_by_currency or {}).values()]
            except (ArithmeticError, ValueError):
                known = False
            else:
                known = all(value.is_finite() and value >= 0 for value in commission_amounts) and all(
                    value.is_finite() for value in funding_amounts
                )
        if not known:
            self.mark_capital_risk_manual_review(intent_id=intent_id, now_ms=now_ms)
            return False
        if realized_pnl_amount is None:  # pragma: no cover - narrowed by the `known` guard above
            raise RuntimeError("capital_risk_settlement_pnl_missing")
        # Funding values are signed provider cash flows: a negative value was paid.  Keep the first
        # policy conservative by not using positive funding or price PnL to offset another loss leg.
        realized_loss = (
            max(Decimal(0), -realized_pnl_amount)
            + sum(commission_amounts, Decimal(0))
            + sum((max(Decimal(0), -value) for value in funding_amounts), Decimal(0))
        )
        updated = self.conn.execute(
            """
            UPDATE trading_capital_risk_reservation_state
               SET status = 'SETTLED', current_planned_risk_amount = 0,
                   settlement_known = true, updated_at_ms = %s
             WHERE intent_id = %s AND status IN ('OPEN', 'MANUAL_REVIEW')
         RETURNING reservation_sha256
            """,
            (int(now_ms), intent_id),
        ).fetchone()
        if updated is None:
            raise RuntimeError("capital_risk_settlement_transition_failed")
        self._append_risk_event(
            reservation_sha256=str(row["reservation_sha256"]),
            intent_id=intent_id,
            event_kind="SETTLED",
            current_planned_risk_amount=Decimal(0),
            attempt_consumed=True,
            settlement_asset=settlement_asset,
            realized_loss_amount=realized_loss,
            occurred_at_ms=now_ms,
        )
        return True

    def _append_risk_event(
        self,
        *,
        reservation_sha256: str,
        intent_id: str,
        event_kind: str,
        current_planned_risk_amount: Any,
        attempt_consumed: bool,
        occurred_at_ms: int,
        settlement_asset: str | None = None,
        realized_loss_amount: Any | None = None,
        event_identity: str | None = None,
    ) -> bool:
        inserted = self.conn.execute(
            """
            WITH event AS (
              SELECT jsonb_build_object(
                       'event_version', 'capital_risk_event_v1',
                       'reservation_sha256', %(reservation)s::text,
                       'intent_id', %(intent)s::text,
                       'event_kind', %(kind)s::text,
                       'current_planned_risk_amount', %(amount)s::text,
                       'attempt_consumed', %(consumed)s::boolean,
                       'settlement_asset', %(asset)s::text,
                       'realized_loss_amount', %(loss)s::text,
                       'occurred_at_ms', %(occurred)s::bigint,
                       'event_identity', %(identity)s::text
                     ) AS payload
            )
            INSERT INTO trading_capital_risk_events (
              event_sha256, reservation_sha256, intent_id, event_kind,
              current_planned_risk_amount, attempt_consumed, settlement_asset,
              realized_loss_amount, occurred_at_ms, payload
            )
            SELECT encode(sha256(convert_to(trading_canonical_jsonb(payload), 'UTF8')), 'hex'),
                   %(reservation)s::text, %(intent)s::text, %(kind)s::text, %(amount)s::numeric,
                   %(consumed)s::boolean, %(asset)s::text, %(loss)s::numeric, %(occurred)s::bigint, payload
              FROM event
            ON CONFLICT (event_sha256) DO NOTHING
            RETURNING event_sha256
            """,
            {
                "reservation": reservation_sha256,
                "intent": intent_id,
                "kind": event_kind,
                "amount": str(current_planned_risk_amount),
                "consumed": bool(attempt_consumed),
                "asset": settlement_asset,
                "loss": None if realized_loss_amount is None else str(realized_loss_amount),
                "occurred": int(occurred_at_ms),
                "identity": event_identity,
            },
        ).fetchone()
        return inserted is not None

    def _require_payload_identity(
        self,
        *,
        table: str,
        key_name: str,
        key: str,
        payload: dict[str, Any],
    ) -> None:
        if table not in {
            "trading_daily_risk_policies",
            "trading_production_promotion_grants",
            "trading_operator_arm_receipts",
        } or key_name not in {"risk_policy_sha256", "grant_sha256", "arm_receipt_sha256"}:
            raise RuntimeError("capital_authority_identity_query_invalid")
        row = self.conn.execute(
            sql.SQL("SELECT payload FROM {} WHERE {} = %s").format(
                sql.Identifier(table),
                sql.Identifier(key_name),
            ),
            (key,),
        ).fetchone()
        if row is None or row["payload"] != payload:
            raise RuntimeError("capital_authority_identity_conflict")

    def _lock_capital_runtime(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM trading_runtime_state WHERE id = 1 FOR UPDATE").fetchone()
        if row is None:
            raise RuntimeError("trading_runtime_state_missing")
        return dict(row)


__all__ = ["AuthorityStorage"]
