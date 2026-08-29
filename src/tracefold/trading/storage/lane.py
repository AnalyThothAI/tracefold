"""The capital lane's three concrete database operations, and their transaction boundaries.

Three named operations, not a repository abstraction (#331 §4): one bounded read of the whole capital
authority, one atomic Case creation, and one atomic decision commit. They live here because a
transaction boundary is a persistence fact, and the lane must not be able to open a session, hold a
connection across a provider call, or discover an invariant by catching an exception.

    capital_authority      one statement snapshot of everything the turn plans against
    create_case            Case row and its `CASE_CREATED` admission row, together or not at all
    commit_long_decision   final capability, blacklist, capacity and sizing recheck, then either the
                           Intent plus `INTENT_EMITTED` or a typed `BLOCKED`, in one transaction

`commit_long_decision` returns a closed typed disposition and never `None`/`False`. The old
`_emit_intent` caught every `Exception` and returned `False`, and the caller wrote
`BLOCKED / intent_admission_blocked` — so a PostgreSQL timeout, a serialization failure and a genuine
capability change all became the same business refusal, and the Source that caused it was consumed
forever. An unknown repository error now rolls back and propagates; the Case stays claimable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from ..admission import AdmissionRow
from ..blacklist import Blacklist, BlacklistSnapshotV1
from ..capabilities import ExecutionCapabilitySnapshotV1
from ..contracts import (
    CURRENT_TERMINAL_STATES,
    BlockedReason,
    CaseState,
    TradingCaseManifest,
)
from ..intent import ACTIVE_INTENT_STATES, TradeIntent, executable_instrument_id, is_executable_instrument
from .sql_values import _dumps


@dataclass(frozen=True, slots=True)
class CapitalAuthority:
    """Everything one turn plans against, read in a single bounded transaction.

    A missing `trading_runtime_state` row is not represented here at all: `capital_authority` returns
    `None`, and the lane halts without scanning, without a Case and without a provider call. The old
    reader defaulted the absent row to `{"control": "RUNNING"}`, which let a lane with no runtime
    authority create Cases and spend budget on the strength of a dictionary literal.
    """

    control: str
    entries_today: int
    blacklist: Blacklist
    active_underlyings: frozenset[str]
    underlyings_in_flight: frozenset[str]
    cased_source_keys: frozenset[str]
    capability: ExecutionCapabilitySnapshotV1 | None


@dataclass(frozen=True, slots=True)
class DecisionCommit:
    """The one terminal answer `commit_long_decision` reached, and what it wrote."""

    state: CaseState
    reason: str
    intent_id: str | None = None


class LaneStorage:
    conn: Any

    # ------------------------------------------------------------------ read
    def capital_authority(self, *, since_ms: int, day_start_ms: int, now_ms: int) -> CapitalAuthority | None:
        """One snapshot of control, capacity, deny-list, in-flight work and the active universe.

        Returns `None` when the runtime authority row is absent. Every other failure raises: an
        unreadable deny-list is an infrastructure fault, and turning it into a "block everything"
        business snapshot filed one refusal per frame against a database problem.
        """

        runtime = self.conn.execute(
            "SELECT control, active_capability_snapshot_sha256 FROM trading_runtime_state WHERE id = 1"
        ).fetchone()
        if runtime is None:
            return None
        entries = self.conn.execute(
            "SELECT count(*) AS n FROM trading_intents WHERE entry_fenced_at_ms >= %s AND entry_fenced_at_ms < %s",
            (int(day_start_ms), int(day_start_ms) + 86_400_000),
        ).fetchone()
        active = self.conn.execute(
            """
            SELECT DISTINCT COALESCE(i.underlying_key, c.underlying_key) AS underlying_key
              FROM trading_intents i JOIN trading_cases c ON c.case_id = i.case_id
             WHERE i.execution_state = ANY(%s)
            """,
            (list(ACTIVE_INTENT_STATES),),
        ).fetchall()
        in_flight = self.conn.execute(
            "SELECT DISTINCT underlying_key FROM trading_cases WHERE state IN ('PENDING', 'RUNNING')"
        ).fetchall()
        cased = self.conn.execute(
            "SELECT primary_source_key FROM trading_cases WHERE observed_at_ms >= %s",
            (int(since_ms),),
        ).fetchall()
        blacklist_rows = self.conn.execute(
            "SELECT base_symbol, reason, created_at_ms, expires_at_ms "
            "FROM trading_symbol_blacklist ORDER BY base_symbol"
        ).fetchall()
        digest = runtime["active_capability_snapshot_sha256"]
        capability: ExecutionCapabilitySnapshotV1 | None = None
        if digest is not None:
            payload = self.conn.execute(
                "SELECT payload FROM trading_execution_capability_snapshots WHERE snapshot_sha256 = %s",
                (str(digest),),
            ).fetchone()
            if payload is not None:
                capability = ExecutionCapabilitySnapshotV1.model_validate(payload["payload"])
        return CapitalAuthority(
            control=str(runtime["control"]),
            entries_today=0 if entries is None else int(entries["n"]),
            blacklist=Blacklist.from_rows([dict(row) for row in blacklist_rows]),
            active_underlyings=frozenset(str(row["underlying_key"]) for row in active),
            underlyings_in_flight=frozenset(str(row["underlying_key"]) for row in in_flight),
            cased_source_keys=frozenset(str(row["primary_source_key"]) for row in cased),
            capability=capability,
        )

    # ------------------------------------------------------------------ freeze
    def create_case(
        self,
        *,
        case_id: str,
        manifest: TradingCaseManifest,
        admission: AdmissionRow,
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
              manifest, manifest_sha256, state, observed_at_ms, source_observed_at_ms,
              trigger_persisted_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                      'PENDING', %s, %s, %s, %s, %s)
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
        cast(Any, self).record_gate_decision(now_ms=now_ms, **{**admission, "case_id": case_id})
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
                   decided_at_ms = %s,
                   updated_at_ms = %s
             WHERE case_id = %s AND run_id = %s AND state IN ('PENDING', 'RUNNING')
            """,
            (
                state.value,
                policy_decision,
                policy_reason,
                None if policy_checks is None else _dumps(dict(policy_checks)),
                int(now_ms),
                int(now_ms),
                case_id,
                run_id,
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def commit_long_decision(
        self,
        *,
        case_id: str,
        run_id: str,
        manifest: TradingCaseManifest,
        policy_reason: str,
        policy_checks: Mapping[str, Any],
        target_notional_usd: Decimal,
        now_ms: int,
    ) -> DecisionCommit:
        """Re-prove capital authority, then hand the Case and one Intent over atomically.

        Everything read here is read *inside* the commit: the active capability pointer, the deny-list
        and the two capacity fences can all move while a Case waits to be decided, and a decision that
        trusted the scan-time snapshot would emit an Intent against authority that no longer exists.

        Every refusal is a named `BlockedReason` written onto the Case in this same transaction, so a
        rolled-back attempt cannot leave a Case claimed-but-undecided. An unknown error is not one of
        them: it propagates, the transaction rolls back, and the Case stays claimable.
        """

        instrument_id = executable_instrument_id(manifest.instrument)
        snapshot = cast(Any, self).active_execution_capability_snapshot(for_update=True)
        if snapshot is None:
            return self._block(case_id, run_id, "capability_absent", policy_checks, now_ms)
        if snapshot.snapshot_sha256 != manifest.execution_capability_snapshot_sha256:
            # The pointer moved between the freeze and the decision. The Case's instrument was resolved
            # from a universe that is no longer authoritative, so it is not this Case's to trade.
            return self._block(case_id, run_id, "capability_mismatch", policy_checks, now_ms)
        if not instrument_id or not is_executable_instrument(manifest.instrument, snapshot):
            return self._block(case_id, run_id, "capability_mismatch", policy_checks, now_ms)
        blacklist: BlacklistSnapshotV1 = cast(Any, self).blacklist_snapshot(now_ms=now_ms, materialize_expiry=True)
        if any(row.underlying_key == manifest.underlying_key for row in blacklist.active_rows):
            return self._block(case_id, run_id, "blacklisted", policy_checks, now_ms)
        if not self._capacity_available(underlying_key=manifest.underlying_key):
            return self._block(case_id, run_id, "capacity_exhausted", policy_checks, now_ms)
        capability = snapshot.included[instrument_id]
        if not _quantity_is_executable(
            reference_price=manifest.mark_price,
            target_notional_usd=target_notional_usd,
            size_increment=capability.size_increment,
            min_quantity=capability.min_quantity,
        ):
            return self._block(case_id, run_id, "quantity_unexecutable", policy_checks, now_ms)

        intent = TradeIntent.create(
            case_id=case_id,
            case_manifest_sha256=manifest.digest(),
            execution_capability_snapshot_sha256=snapshot.snapshot_sha256,
            blacklist_snapshot=blacklist,
            instrument_id=instrument_id,
            underlying_key=manifest.underlying_key,
            created_at_ms=now_ms,
            reference_price=manifest.mark_price,
            target_notional_usd=target_notional_usd,
        )
        if not cast(Any, self).insert_intent(intent):
            # Content-addressed identity: the same Case and the same instant produce the same Intent.
            # A conflict therefore means another worker already handed this Case over, and the Case's
            # own terminal transition below is what settles which of the two owns the receipt.
            raise RuntimeError("trading_intent_insert_conflict")
        if not self.settle_case(
            case_id=case_id,
            run_id=run_id,
            state=CaseState.INTENT_EMITTED,
            policy_decision="long",
            policy_reason=policy_reason,
            policy_checks=policy_checks,
            now_ms=now_ms,
        ):
            # The caller owns the transaction; raising here rolls the Intent insert back too. A Case
            # that could not transition must never leave an Intent behind.
            raise RuntimeError("trading_intent_case_transition_failed")
        return DecisionCommit(state=CaseState.INTENT_EMITTED, reason=policy_reason, intent_id=intent.intent_id)

    def _capacity_available(self, *, underlying_key: str) -> bool:
        """One nonterminal Intent globally, one per underlying. Re-read inside the commit."""

        row = self.conn.execute(
            """
            SELECT EXISTS (
                     SELECT 1 FROM trading_intents WHERE execution_state = ANY(%(active)s)
                   ) AS any_active,
                   EXISTS (
                     SELECT 1 FROM trading_intents i
                       JOIN trading_cases c ON c.case_id = i.case_id
                      WHERE c.underlying_key = %(underlying)s AND i.execution_state = ANY(%(active)s)
                   ) AS underlying_active
            """,
            {
                "active": list(ACTIVE_INTENT_STATES),
                "underlying": underlying_key,
            },
        ).fetchone()
        if row is None:  # pragma: no cover - aggregate queries always return one row
            return False
        return not (row["any_active"] or row["underlying_active"])

    def _block(
        self,
        case_id: str,
        run_id: str,
        reason: BlockedReason,
        policy_checks: Mapping[str, Any],
        now_ms: int,
    ) -> DecisionCommit:
        if not self.settle_case(
            case_id=case_id,
            run_id=run_id,
            state=CaseState.BLOCKED,
            policy_decision="no_trade",
            policy_reason=reason,
            policy_checks=policy_checks,
            now_ms=now_ms,
        ):
            raise RuntimeError("trading_case_block_transition_failed")
        return DecisionCommit(state=CaseState.BLOCKED, reason=reason)


def _quantity_is_executable(
    *,
    reference_price: Decimal,
    target_notional_usd: Decimal,
    size_increment: str,
    min_quantity: str | None,
) -> bool:
    """Whether the venue's own lot size leaves anything to buy at this notional.

    `min_notional` is deliberately not re-checked here. The execution authority sizes from a *fresh*
    price bounded by `max_entry_drift_bps`, and that is the only price the venue will accept an order
    at; re-deciding a notional floor against the frozen mark would refuse Cases the venue would take.
    What this does prove is that flooring to the lot size leaves a positive, submittable quantity —
    a question the frozen capability answers on its own.
    """

    try:
        increment = Decimal(size_increment)
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not increment.is_finite() or increment <= 0 or reference_price <= 0:
        return False
    raw = target_notional_usd / reference_price
    quantity = (raw // increment) * increment
    if quantity <= 0:
        return False
    if min_quantity is not None:
        try:
            floor = Decimal(min_quantity)
        except (InvalidOperation, TypeError, ValueError):
            return False
        if quantity < floor:
            return False
    return True


__all__ = ["CapitalAuthority", "DecisionCommit", "LaneStorage"]
