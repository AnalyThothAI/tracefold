"""The Decision lane's concrete database operations and transaction boundaries (#350).

Three named operations, not a repository abstraction (#331 §4): one bounded read of the whole capital
authority, one atomic Case creation, and one atomic decision commit. They live here because a
transaction boundary is a persistence fact, and the lane must not be able to open a session, hold a
connection across a provider call, or discover an invariant by catching an exception.

    capital_authority      one statement snapshot of everything the turn plans against
    create_case            Case row and its `CASE_CREATED` admission row, together or not at all
    commit_capital_disposition  preserve Policy LONG and write the exact independent capital block

`commit_capital_disposition` returns a closed typed disposition and never `None`/`False`. The old
`_emit_intent` caught every `Exception` and returned `False`, and the caller wrote
`BLOCKED / intent_admission_blocked` — so a PostgreSQL timeout, a serialization failure and a genuine
capability change all became the same business refusal, and the Source that caused it was consumed
forever. An unknown repository error now rolls back and propagates; the Case stays claimable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypedDict, cast

from ..admission import AdmissionRow
from ..blacklist import Blacklist
from ..capital_authority import (
    CapitalAuthorizationReceiptV1,
    CapitalRiskReservationV1,
    planned_risk_components,
    risk_day_bounds,
)
from ..catalog import VenueInstrumentCatalogSnapshotV1
from ..contracts import CURRENT_TERMINAL_STATES, CaseState, TradingCaseManifest, VenueBinding
from ..execution_policy import EXECUTION_POLICY_SHA256, PROTECTION_CONTRACT_SHA256, STOP_LOSS_BPS
from ..intent import ACTIVE_INTENT_STATES, TradeIntent, economic_lifecycle_id
from ..quote_authority import QUOTE_CONTRACT_SHA256
from .sql_values import _dumps


@dataclass(frozen=True, slots=True)
class CapitalAuthority:
    """Everything one turn plans against, read in a single bounded transaction.

    A missing `trading_runtime_state` row is not represented here at all: `capital_authority` returns
    `None`, and the lane faults without scanning, without a Case and without a provider call. The old
    reader defaulted the absent row to `{"control": "RUNNING"}`, which let a lane with no runtime
    authority create Cases and spend budget on the strength of a dictionary literal.
    """

    capital_control: str
    blacklist: Blacklist
    active_underlyings: frozenset[str]
    underlyings_in_flight: frozenset[str]
    cased_source_keys: frozenset[str]
    bindings: Mapping[VenueBinding, BindingAuthority]
    catalogs: Mapping[VenueBinding, VenueInstrumentCatalogSnapshotV1 | None]


@dataclass(frozen=True, slots=True)
class BindingAuthority:
    credential_state: str
    runtime_state: str
    account_state: str
    catalog_state: str
    catalog_snapshot_sha256: str | None
    reason: str | None


class CapitalAuthoritySnapshotRow(TypedDict):
    """Raw result of the authority's one PostgreSQL statement.

    JSON domain materialization is deliberately absent: the worker closes the read transaction before
    `materialize_capital_authority` validates either venue catalog and recomputes its identity.
    """

    capital_control: str
    active_underlyings: list[str]
    underlyings_in_flight: list[str]
    cased_source_keys: list[str]
    blacklist_rows_json: str
    binding_rows_json: str


_CLOSED_BINDINGS: tuple[VenueBinding, ...] = ("BINANCE_USDM", "HYPERLIQUID_PERP")


def materialize_capital_authority(snapshot: CapitalAuthoritySnapshotRow | None) -> CapitalAuthority | None:
    """Validate one raw snapshot after its database transaction has closed."""

    if snapshot is None:
        return None
    binding_rows = json.loads(snapshot["binding_rows_json"])
    if set(binding_rows) != set(_CLOSED_BINDINGS):
        raise RuntimeError("trading_binding_runtime_missing")
    catalogs: dict[VenueBinding, VenueInstrumentCatalogSnapshotV1 | None] = {}
    for binding in _CLOSED_BINDINGS:
        row = binding_rows[binding]
        payload = row["catalog_payload"]
        if payload is None:
            catalogs[binding] = None
            continue
        catalog = VenueInstrumentCatalogSnapshotV1.model_validate(payload)
        if catalog.snapshot_sha256 != row["catalog_snapshot_sha256"]:
            raise RuntimeError("venue_catalog_snapshot_digest_mismatch")
        catalogs[binding] = catalog
    return CapitalAuthority(
        capital_control=snapshot["capital_control"],
        blacklist=Blacklist.from_rows(json.loads(snapshot["blacklist_rows_json"])),
        active_underlyings=frozenset(snapshot["active_underlyings"]),
        underlyings_in_flight=frozenset(snapshot["underlyings_in_flight"]),
        cased_source_keys=frozenset(snapshot["cased_source_keys"]),
        bindings={
            binding: BindingAuthority(
                credential_state=str(binding_rows[binding]["credential_state"]),
                runtime_state=str(binding_rows[binding]["runtime_state"]),
                account_state=str(binding_rows[binding]["account_state"]),
                catalog_state=str(binding_rows[binding]["catalog_state"]),
                catalog_snapshot_sha256=binding_rows[binding]["catalog_snapshot_sha256"],
                reason=binding_rows[binding]["reason"],
            )
            for binding in _CLOSED_BINDINGS
        },
        catalogs=catalogs,
    )


@dataclass(frozen=True, slots=True)
class CapitalDispositionCommit:
    """The one terminal answer `commit_capital_disposition` reached, and what it wrote."""

    state: CaseState
    reason: str
    grant_sha256: str | None = None
    arm_receipt_sha256: str | None = None
    risk_policy_sha256: str | None = None
    reservation_sha256: str | None = None
    authorization_receipt_sha256: str | None = None
    intent_id: str | None = None


class LaneStorage:
    conn: Any

    # ------------------------------------------------------------------ read
    def capital_authority_snapshot(self, *, since_ms: int, now_ms: int) -> CapitalAuthoritySnapshotRow | None:
        """One SQL snapshot of control, capacity, deny-list, in-flight work and the active universe.

        Returns `None` when the runtime authority row is absent. Every other failure raises: an
        unreadable deny-list is an infrastructure fault, and turning it into a "block everything"
        business snapshot filed one refusal per frame against a database problem.
        """

        row = self.conn.execute(
            """
            WITH capital_runtime AS (
                SELECT control FROM trading_runtime_state WHERE id = 1
            )
            SELECT capital_runtime.control AS capital_control,
                   ARRAY(
                       SELECT DISTINCT COALESCE(intent.underlying_key, trading_case.underlying_key)
                         FROM trading_intents intent
                         JOIN trading_cases trading_case ON trading_case.case_id = intent.case_id
                        WHERE intent.execution_state = ANY(%(active_states)s)
                        ORDER BY 1
                   ) AS active_underlyings,
                   ARRAY(
                       SELECT DISTINCT underlying_key
                         FROM trading_cases
                        WHERE state IN ('PENDING', 'RUNNING')
                        ORDER BY 1
                   ) AS underlyings_in_flight,
                   ARRAY(
                       SELECT DISTINCT primary_source_key
                         FROM trading_cases
                        WHERE observed_at_ms >= %(since_ms)s
                        ORDER BY 1
                   ) AS cased_source_keys,
                   COALESCE((
                       SELECT jsonb_agg(
                           jsonb_build_object(
                               'base_symbol', base_symbol,
                               'reason', reason,
                               'created_at_ms', created_at_ms,
                               'expires_at_ms', expires_at_ms
                           ) ORDER BY base_symbol
                       )
                         FROM trading_symbol_blacklist
                   ), '[]'::jsonb)::text AS blacklist_rows_json,
                   COALESCE((
                       SELECT jsonb_object_agg(
                           binding_runtime.binding,
                           jsonb_build_object(
                               'credential_state', binding_runtime.credential_state,
                               'runtime_state', binding_runtime.runtime_state,
                               'account_state', binding_runtime.account_state,
                               'catalog_state', CASE
                                   WHEN binding_runtime.catalog_state = 'ready'
                                    AND catalog.stale_after_ms IS NOT NULL
                                    AND binding_runtime.catalog_captured_at_ms + catalog.stale_after_ms <= %(now_ms)s
                                   THEN 'stale'
                                   ELSE binding_runtime.catalog_state
                               END,
                               'catalog_snapshot_sha256', binding_runtime.catalog_snapshot_sha256,
                               'catalog_payload', catalog.payload,
                               'reason', binding_runtime.reason
                           )
                       )
                         FROM trading_binding_runtime binding_runtime
                         LEFT JOIN trading_venue_catalog_snapshots catalog
                           ON catalog.snapshot_sha256 = binding_runtime.catalog_snapshot_sha256
                        WHERE binding_runtime.binding = ANY(%(bindings)s)
                   ), '{}'::jsonb)::text AS binding_rows_json
              FROM capital_runtime
            """,
            {
                "active_states": list(ACTIVE_INTENT_STATES),
                "bindings": list(_CLOSED_BINDINGS),
                "since_ms": int(since_ms),
                "now_ms": int(now_ms),
            },
        ).fetchone()
        return None if row is None else cast(CapitalAuthoritySnapshotRow, dict(row))

    # ------------------------------------------------------------------ freeze
    def create_case(
        self,
        *,
        case_id: str,
        manifest: TradingCaseManifest,
        admission: AdmissionRow,
        release_revision: str,
        now_ms: int,
    ) -> bool:
        """Insert one immutable Case and its `CASE_CREATED` admission row, together or not at all.

        Same transaction, deliberately. A Case with no admission row — or an admission row naming a
        Case that failed to insert — is precisely the ambiguity the ledger exists to remove.

        The `ON CONFLICT` deliberately names no target: `primary_source_key` makes a re-scanned window
        a no-op, and the partial unique index on an in-flight `underlying_key` stops a second
        concurrent thesis for the same issuer. Naming one target would turn the other into an
        exception in a transaction that has already done work.
        """

        trigger = manifest.primary_trigger
        cursor = self.conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
              strategy_config_digest, primary_source_key, supplemental_source_keys,
              manifest, manifest_sha256, state, policy_decision, policy_reason,
              capital_disposition, capital_reason, observed_at_ms, source_observed_at_ms,
              trigger_persisted_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                      'PENDING', 'not_run', 'not_run', 'not_applicable', NULL, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                case_id,
                manifest.underlying_key,
                manifest.trigger_kind,
                manifest.policy_id,
                manifest.policy_version,
                manifest.policy_config_digest,
                trigger.source_key,
                _dumps([]),
                _dumps(manifest.model_dump(mode="json")),
                manifest.digest(),
                int(manifest.cutoff_ms),
                int(trigger.observed_at_ms),
                int(trigger.persisted_at_ms),
                int(now_ms),
                int(now_ms),
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) <= 0:
            return False
        cast(Any, self).record_gate_decision(
            now_ms=now_ms,
            release_revision=release_revision,
            **{**admission, "case_id": case_id},
        )
        return True

    # ------------------------------------------------------------------ decide
    def settle_case(
        self,
        *,
        case_id: str,
        run_id: str,
        state: CaseState,
        policy_decision: str | None,
        policy_reason: str,
        capital_disposition: str,
        capital_reason: str | None,
        policy_checks: Mapping[str, Any] | None = None,
        now_ms: int,
    ) -> bool:
        """Terminalise a claimed Case, and only one that is still undecided.

        `state` is restricted to `CURRENT_TERMINAL_STATES` here rather than by a CHECK constraint,
        because production holds 225 `POLICY_REJECTED` and 2 `ORDER_PREPARED` rows that must stay
        readable. This is the writer-side half of that: the two historical states have no path back
        into the table.

        `run_id` alone is not enough. A worker holding a valid lease can be inside a commit while
        something else terminalises the Case, and without the state predicate the returning worker
        would write its own answer straight over the top.
        """

        if state not in CURRENT_TERMINAL_STATES:
            raise ValueError(f"trading_case_terminal_state_retired:{state}")
        cursor = self.conn.execute(
            """
            UPDATE trading_cases
               SET state = %s,
                   policy_decision = %s,
                   policy_reason = %s,
                   policy_checks = coalesce(%s::jsonb, policy_checks),
                   capital_disposition = %s,
                   capital_reason = %s,
                   decided_at_ms = %s,
                   updated_at_ms = %s
             WHERE case_id = %s AND run_id = %s AND state IN ('PENDING', 'RUNNING')
            """,
            (
                state.value,
                policy_decision,
                policy_reason,
                None if policy_checks is None else _dumps(dict(policy_checks)),
                capital_disposition,
                capital_reason,
                int(now_ms),
                int(now_ms),
                case_id,
                run_id,
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def commit_capital_disposition(
        self,
        *,
        case_id: str,
        run_id: str,
        manifest: TradingCaseManifest,
        policy_reason: str,
        policy_checks: Mapping[str, Any],
        release_revision: str,
        source_contract_sha256: str,
        feature_contract_sha256: str,
        target_notional: Decimal,
        now_ms: int,
    ) -> CapitalDispositionCommit:
        """Authorize and emit one Intent, or preserve LONG beside one exact capital refusal.

        This method is the sole transaction owner for the global authority snapshot.  The runtime
        row serializes authorization with grant/arm/revocation changes; every binding row is locked so
        a reconciliation or identity generation cannot move while the receipt is being frozen.
        """
        case = self.conn.execute(
            "SELECT manifest_sha256 FROM trading_cases "
            "WHERE case_id = %s AND run_id = %s AND state IN ('PENDING', 'RUNNING') FOR UPDATE",
            (case_id, run_id),
        ).fetchone()
        if case is None or str(case["manifest_sha256"]) != manifest.digest():
            raise RuntimeError("trading_case_capital_claim_invalid")

        runtime = self.conn.execute("SELECT * FROM trading_runtime_state WHERE id = 1 FOR UPDATE").fetchone()
        if runtime is None:
            raise RuntimeError("trading_runtime_state_missing")
        binding_rows = self.conn.execute("SELECT * FROM trading_binding_runtime ORDER BY binding FOR UPDATE").fetchall()
        raw_by_binding = {str(row["binding"]): row for row in binding_rows}
        raw_binding = raw_by_binding.get(manifest.instrument.binding)
        if raw_binding is None:
            raise RuntimeError(f"trading_binding_runtime_missing:{manifest.instrument.binding}")
        binding = cast(Any, self).binding_runtime(binding=manifest.instrument.binding, now_ms=now_ms)
        if binding is None:
            raise RuntimeError(f"trading_binding_runtime_missing:{manifest.instrument.binding}")
        if runtime["control"] == "PAUSED":
            reason = "capital_paused"
        elif runtime["control"] == "CLOSE_ONLY":
            reason = "capital_close_only"
        elif any(row["account_state"] == "exposure_present" for row in binding_rows):
            reason = "unexpected_exposure"
        elif binding.credential_state == "unconfigured":
            reason = "credentials_unconfigured"
        elif binding.credential_state == "invalid":
            reason = "credentials_invalid"
        elif binding.catalog_snapshot_sha256 != manifest.venue_catalog_snapshot_sha256:
            reason = "catalog_mismatch"
        elif binding.catalog_state != "ready":
            reason = "catalog_stale"
        elif (
            binding.runtime_state != "ready"
            or binding.account_state != "reconciled_flat"
            or binding.capability_state != "ready"
            or binding.execution_binding_sha256 is None
        ):
            reason = "binding_unready"
        else:
            reason = None
        if reason is not None:
            return self._block(case_id, run_id, policy_reason, reason, policy_checks, now_ms)

        active = self.conn.execute(
            "SELECT 1 FROM trading_intents WHERE execution_state = ANY(%s) LIMIT 1",
            (list(ACTIVE_INTENT_STATES),),
        ).fetchone()
        if active is not None:
            return self._block(case_id, run_id, policy_reason, "active_lifecycle_present", policy_checks, now_ms)

        blacklist = cast(Any, self).blacklist_snapshot(now_ms=now_ms, materialize_expiry=True)
        if any(row.underlying_key == manifest.underlying_key for row in blacklist.active_rows):
            return self._block(case_id, run_id, policy_reason, "capital_blacklisted", policy_checks, now_ms)

        capability = cast(Any, self).active_execution_capability_snapshot(binding=manifest.instrument.binding)
        execution_binding = cast(Any, self).active_execution_binding(binding=manifest.instrument.binding)
        capability_entry = None if capability is None else capability.resolve(manifest.base_symbol)
        if (
            capability is None
            or execution_binding is None
            or capability_entry is None
            or capability.snapshot_sha256 != binding.capability_snapshot_sha256
            or capability.catalog_snapshot_sha256 != manifest.venue_catalog_snapshot_sha256
            or capability_entry.native_symbol != manifest.instrument.provider_symbol
            or capability_entry.binding != manifest.instrument.binding
            or capability_entry.venue != manifest.instrument.venue
            or execution_binding.binding_sha256 != binding.execution_binding_sha256
            or execution_binding.catalog_snapshot_sha256 != manifest.venue_catalog_snapshot_sha256
            or execution_binding.capability_snapshot_sha256 != capability.snapshot_sha256
            or execution_binding.account_generation != binding.account_generation
            or execution_binding.credential_fingerprint != binding.credential_fingerprint
            or execution_binding.adapter_contract_sha256 != capability.adapter_contract_sha256
            or execution_binding.quote_contract_sha256 != capability.quote_contract_sha256
            or execution_binding.protection_contract_sha256 != capability.protection_contract_sha256
            or capability.quote_contract_sha256 != QUOTE_CONTRACT_SHA256
            or capability.protection_contract_sha256 != PROTECTION_CONTRACT_SHA256
        ):
            return self._block(case_id, run_id, policy_reason, "capability_mismatch", policy_checks, now_ms)

        all_grants = cast(Any, self).promotion_grants(binding=manifest.instrument.binding)
        active_grants = cast(Any, self).active_promotion_grants(binding=manifest.instrument.binding, now_ms=now_ms)
        if not active_grants:
            grant_reason = "promotion_grant_absent" if not all_grants else "promotion_grant_expired"
            return self._block(case_id, run_id, policy_reason, grant_reason, policy_checks, now_ms)
        matching_grants = tuple(
            grant
            for grant in active_grants
            if grant.venue == manifest.instrument.venue
            and grant.source_contract_sha256 == source_contract_sha256
            and grant.feature_contract_sha256 == feature_contract_sha256
            and grant.policy_id == manifest.policy_id
            and grant.policy_config_sha256 == manifest.policy_config_digest
            and grant.catalog_snapshot_sha256 == manifest.venue_catalog_snapshot_sha256
            and grant.capability_snapshot_sha256 == capability.snapshot_sha256
            and grant.execution_binding_sha256 == execution_binding.binding_sha256
            and grant.adapter_contract_sha256 == execution_binding.adapter_contract_sha256
            and grant.execution_policy_sha256 == EXECUTION_POLICY_SHA256
            and grant.quote_contract_sha256 == execution_binding.quote_contract_sha256
            and grant.protection_contract_sha256 == execution_binding.protection_contract_sha256
            and grant.approved_release == release_revision
            and capability_entry.catalog_entry_id in grant.allowed_capability_entry_ids
        )
        if len(matching_grants) != 1:
            return self._block(case_id, run_id, policy_reason, "promotion_grant_mismatch", policy_checks, now_ms)
        grant = matching_grants[0]
        if target_notional > grant.max_target_notional:
            return self._block(
                case_id,
                run_id,
                policy_reason,
                "target_notional_exceeds_authority",
                policy_checks,
                now_ms,
            )

        risk_policy = cast(Any, self).daily_risk_policy(grant.risk_policy_sha256)
        if risk_policy is None:
            return self._block(case_id, run_id, policy_reason, "risk_policy_unavailable", policy_checks, now_ms)
        if risk_policy.effective_from_ms > now_ms or risk_policy.expires_at_ms <= now_ms:
            return self._block(case_id, run_id, policy_reason, "risk_policy_expired", policy_checks, now_ms)
        if (
            risk_policy.approved_release != release_revision
            or risk_policy.cost_model_sha256 != grant.cost_model_sha256
            or risk_policy.risk_policy_sha256 != grant.risk_policy_sha256
        ):
            return self._block(case_id, run_id, policy_reason, "risk_policy_mismatch", policy_checks, now_ms)
        if target_notional > risk_policy.max_target_notional:
            return self._block(
                case_id,
                run_id,
                policy_reason,
                "target_notional_exceeds_authority",
                policy_checks,
                now_ms,
            )
        limit = risk_policy.limit_for(capability_entry.settlement_asset)
        if limit is None:
            return self._block(case_id, run_id, policy_reason, "risk_policy_unavailable", policy_checks, now_ms)

        arm_digest = binding.active_arm_receipt_sha256
        arm = None if arm_digest is None else cast(Any, self).operator_arm_receipt(arm_digest)
        if (
            arm is None
            or arm.arm_epoch != int(runtime["arm_epoch"])
            or arm.binding != manifest.instrument.binding
            or arm.approved_release != release_revision
            or arm.account_generation != binding.account_generation
            or arm.credential_fingerprint != binding.credential_fingerprint
            or arm.catalog_snapshot_sha256 != manifest.venue_catalog_snapshot_sha256
            or arm.capability_snapshot_sha256 != capability.snapshot_sha256
            or arm.execution_binding_sha256 != execution_binding.binding_sha256
            or arm.grant_sha256 != grant.grant_sha256
            or arm.risk_policy_sha256 != risk_policy.risk_policy_sha256
            or arm.reconciliation_state != "reconciled_flat"
            or arm.armed_at_ms > now_ms
            or arm.expires_at_ms <= now_ms
        ):
            return self._block(case_id, run_id, policy_reason, "operator_arm_invalid", policy_checks, now_ms)

        manual_review = self.conn.execute(
            "SELECT 1 FROM trading_capital_risk_reservation_state WHERE status = 'MANUAL_REVIEW' LIMIT 1"
        ).fetchone()
        if manual_review is not None:
            return self._block(case_id, run_id, policy_reason, "risk_manual_review", policy_checks, now_ms)
        unknown = self.conn.execute(
            """
            SELECT 1
              FROM trading_capital_risk_reservation_state state
              JOIN trading_intents intent ON intent.intent_id = state.intent_id
             WHERE (intent.execution_state = 'TERMINAL' AND state.status NOT IN ('RELEASED', 'SETTLED'))
                OR (state.status = 'SETTLED' AND NOT EXISTS (
                     SELECT 1 FROM trading_capital_risk_events event
                      WHERE event.reservation_sha256 = state.reservation_sha256
                        AND event.event_kind = 'SETTLED'
                   ))
             LIMIT 1
            """
        ).fetchone()
        if unknown is not None:
            return self._block(case_id, run_id, policy_reason, "risk_settlement_unknown", policy_checks, now_ms)

        day_start, day_end = risk_day_bounds(now_ms)
        attempts_row = self.conn.execute(
            "SELECT count(*) AS n FROM trading_capital_risk_reservation_state "
            "WHERE attempt_consumed AND attempt_day_start_ms = %s AND attempt_day_end_ms = %s",
            (day_start, day_end),
        ).fetchone()
        committed_attempts = 0 if attempts_row is None else int(attempts_row["n"])
        if committed_attempts >= risk_policy.max_committed_entry_attempts:
            return self._block(case_id, run_id, policy_reason, "risk_attempts_exhausted", policy_checks, now_ms)
        planned_row = self.conn.execute(
            """
            SELECT COALESCE(sum(state.current_planned_risk_amount), 0) AS amount
              FROM trading_capital_risk_reservation_state state
              JOIN trading_capital_risk_reservations reservation
                ON reservation.reservation_sha256 = state.reservation_sha256
             WHERE reservation.settlement_asset = %s
               AND state.status NOT IN ('RELEASED', 'SETTLED')
            """,
            (capability_entry.settlement_asset,),
        ).fetchone()
        open_planned_risk = Decimal(0) if planned_row is None else Decimal(planned_row["amount"])
        settled_row = self.conn.execute(
            """
            SELECT COALESCE(sum(realized_loss_amount), 0) AS amount
              FROM trading_capital_risk_events
             WHERE event_kind = 'SETTLED' AND settlement_asset = %s
               AND occurred_at_ms >= %s AND occurred_at_ms < %s
            """,
            (capability_entry.settlement_asset, day_start, day_end),
        ).fetchone()
        realized_loss = Decimal(0) if settled_row is None else Decimal(settled_row["amount"])
        if realized_loss >= limit.max_realized_loss_amount:
            return self._block(
                case_id,
                run_id,
                policy_reason,
                "risk_realized_loss_exhausted",
                policy_checks,
                now_ms,
            )
        stop_risk, fee_reserve, planned_risk = planned_risk_components(
            target_notional=target_notional,
            stop_loss_bps=STOP_LOSS_BPS,
            fee_slippage_reserve_bps=limit.fee_slippage_reserve_bps,
        )
        if open_planned_risk + planned_risk > limit.max_planned_risk_amount:
            return self._block(case_id, run_id, policy_reason, "risk_planned_exhausted", policy_checks, now_ms)

        source_identity = manifest.primary_trigger.source_key
        lifecycle_id = economic_lifecycle_id(
            case_id=case_id,
            source_identity=source_identity,
            binding=manifest.instrument.binding,
            provider_instrument_id=capability_entry.provider_instrument_id,
        )
        reservation = CapitalRiskReservationV1(
            case_id=case_id,
            source_identity=source_identity,
            economic_lifecycle_id=lifecycle_id,
            binding=manifest.instrument.binding,
            settlement_asset=capability_entry.settlement_asset,
            risk_policy_sha256=risk_policy.risk_policy_sha256,
            grant_sha256=grant.grant_sha256,
            arm_receipt_sha256=arm.arm_receipt_sha256,
            risk_day_start_ms=day_start,
            risk_day_end_ms=day_end,
            target_notional=target_notional,
            planned_stop_risk_amount=stop_risk,
            fee_slippage_reserve_amount=fee_reserve,
            planned_risk_amount=planned_risk,
            created_at_ms=now_ms,
        )
        receipt = CapitalAuthorizationReceiptV1(
            case_id=case_id,
            reservation_sha256=reservation.reservation_sha256,
            binding=manifest.instrument.binding,
            account_generation=binding.account_generation,
            execution_binding_sha256=execution_binding.binding_sha256,
            grant_sha256=grant.grant_sha256,
            arm_receipt_sha256=arm.arm_receipt_sha256,
            risk_policy_sha256=risk_policy.risk_policy_sha256,
            risk_day_start_ms=day_start,
            risk_day_end_ms=day_end,
            settlement_asset=capability_entry.settlement_asset,
            committed_attempts_before=committed_attempts,
            committed_attempts_limit=risk_policy.max_committed_entry_attempts,
            open_planned_risk_before=open_planned_risk,
            open_planned_risk_after=open_planned_risk + planned_risk,
            planned_risk_limit=limit.max_planned_risk_amount,
            realized_loss_to_date=realized_loss,
            realized_loss_limit=limit.max_realized_loss_amount,
            approved_release=release_revision,
            evaluated_at_ms=now_ms,
        )
        intent = TradeIntent.create(
            case_id=case_id,
            case_manifest_sha256=manifest.digest(),
            source_venue=capability_entry.venue,
            source_identity=source_identity,
            canonical_asset=capability_entry.canonical_asset,
            binding=manifest.instrument.binding,
            account_generation=binding.account_generation,
            execution_binding_sha256=execution_binding.binding_sha256,
            venue_catalog_snapshot_sha256=manifest.venue_catalog_snapshot_sha256,
            execution_capability_snapshot_sha256=capability.snapshot_sha256,
            capability_entry_id=capability_entry.catalog_entry_id,
            provider_instrument_id=capability_entry.provider_instrument_id,
            instrument_id=capability_entry.instrument_id,
            settlement_asset=capability_entry.settlement_asset,
            capital_authorization_receipt_sha256=receipt.authorization_receipt_sha256,
            blacklist_snapshot=blacklist,
            created_at_ms=now_ms,
            reference_price=manifest.contexts.market.mark_price,
            target_notional=target_notional,
            max_risk_amount=planned_risk,
            risk_currency=capability_entry.settlement_asset,
        )
        cast(Any, self).insert_authorized_intent_bundle(
            reservation=reservation,
            receipt=receipt,
            intent=intent,
            now_ms=now_ms,
        )
        if not self.settle_case(
            case_id=case_id,
            run_id=run_id,
            state=CaseState.INTENT_EMITTED,
            policy_decision="long",
            policy_reason=policy_reason,
            policy_checks=policy_checks,
            capital_disposition="allowed",
            capital_reason="capital_authorized",
            now_ms=now_ms,
        ):
            raise RuntimeError("trading_case_intent_transition_failed")
        return CapitalDispositionCommit(
            state=CaseState.INTENT_EMITTED,
            reason="capital_authorized",
            grant_sha256=grant.grant_sha256,
            arm_receipt_sha256=arm.arm_receipt_sha256,
            risk_policy_sha256=risk_policy.risk_policy_sha256,
            reservation_sha256=reservation.reservation_sha256,
            authorization_receipt_sha256=receipt.authorization_receipt_sha256,
            intent_id=intent.intent_id,
        )

    def _block(
        self,
        case_id: str,
        run_id: str,
        policy_reason: str,
        capital_reason: str,
        policy_checks: Mapping[str, Any],
        now_ms: int,
    ) -> CapitalDispositionCommit:
        if not self.settle_case(
            case_id=case_id,
            run_id=run_id,
            state=CaseState.BLOCKED,
            policy_decision="long",
            policy_reason=policy_reason,
            policy_checks=policy_checks,
            capital_disposition="blocked",
            capital_reason=capital_reason,
            now_ms=now_ms,
        ):
            raise RuntimeError("trading_case_block_transition_failed")
        return CapitalDispositionCommit(state=CaseState.BLOCKED, reason=capital_reason)


__all__ = ["BindingAuthority", "CapitalAuthority", "CapitalDispositionCommit", "LaneStorage"]
