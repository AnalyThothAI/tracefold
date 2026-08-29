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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from ..admission import AdmissionRow
from ..blacklist import Blacklist
from ..catalog import VenueInstrumentCatalogSnapshotV1
from ..contracts import CURRENT_TERMINAL_STATES, CaseState, TradingCaseManifest
from ..intent import ACTIVE_INTENT_STATES
from .sql_values import _dumps


@dataclass(frozen=True, slots=True)
class CapitalAuthority:
    """Everything one turn plans against, read in a single bounded transaction.

    A missing `trading_runtime_state` row is not represented here at all: `capital_authority` returns
    `None`, and the lane halts without scanning, without a Case and without a provider call. The old
    reader defaulted the absent row to `{"control": "RUNNING"}`, which let a lane with no runtime
    authority create Cases and spend budget on the strength of a dictionary literal.
    """

    capital_control: str
    blacklist: Blacklist
    active_underlyings: frozenset[str]
    underlyings_in_flight: frozenset[str]
    cased_source_keys: frozenset[str]
    binding: BindingAuthority
    catalog: VenueInstrumentCatalogSnapshotV1 | None


@dataclass(frozen=True, slots=True)
class BindingAuthority:
    credential_state: str
    runtime_state: str
    account_state: str
    catalog_state: str
    catalog_snapshot_sha256: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class DecisionCommit:
    """The one terminal answer `commit_capital_disposition` reached, and what it wrote."""

    state: CaseState
    reason: str
    intent_id: str | None = None


class LaneStorage:
    conn: Any

    # ------------------------------------------------------------------ read
    def capital_authority(self, *, since_ms: int, now_ms: int) -> CapitalAuthority | None:
        """One snapshot of control, capacity, deny-list, in-flight work and the active universe.

        Returns `None` when the runtime authority row is absent. Every other failure raises: an
        unreadable deny-list is an infrastructure fault, and turning it into a "block everything"
        business snapshot filed one refusal per frame against a database problem.
        """

        runtime = self.conn.execute("SELECT control FROM trading_runtime_state WHERE id = 1").fetchone()
        if runtime is None:
            return None
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
        binding_row = cast(Any, self).binding_runtime(binding="BINANCE_USDM")
        if binding_row is None:
            raise RuntimeError("trading_binding_runtime_missing:BINANCE_USDM")
        catalog = cast(Any, self).active_venue_catalog(binding="BINANCE_USDM")
        return CapitalAuthority(
            capital_control=str(runtime["control"]),
            blacklist=Blacklist.from_rows([dict(row) for row in blacklist_rows]),
            active_underlyings=frozenset(str(row["underlying_key"]) for row in active),
            underlyings_in_flight=frozenset(str(row["underlying_key"]) for row in in_flight),
            cased_source_keys=frozenset(str(row["primary_source_key"]) for row in cased),
            binding=BindingAuthority(
                credential_state=str(binding_row["credential_state"]),
                runtime_state=str(binding_row["runtime_state"]),
                account_state=str(binding_row["account_state"]),
                catalog_state=str(binding_row["catalog_state"]),
                catalog_snapshot_sha256=binding_row["catalog_snapshot_sha256"],
                reason=binding_row["reason"],
            ),
            catalog=catalog,
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
        now_ms: int,
    ) -> DecisionCommit:
        """Re-prove authority and preserve a Policy LONG beside the exact capital refusal.

        #360 uniquely owns grant, arm, risk reservation and Intent creation. Until it lands there is
        no allowed branch here: control, a Key, a ready runtime, or all three can never emit capital.
        """
        runtime = self.conn.execute("SELECT control FROM trading_runtime_state WHERE id = 1 FOR UPDATE").fetchone()
        if runtime is None:
            raise RuntimeError("trading_runtime_state_missing")
        binding = cast(Any, self).binding_runtime(binding="BINANCE_USDM")
        if binding is None:
            raise RuntimeError("trading_binding_runtime_missing:BINANCE_USDM")
        if runtime["control"] == "PAUSED":
            reason = "capital_paused"
        elif runtime["control"] == "CLOSE_ONLY":
            reason = "capital_close_only"
        elif binding["credential_state"] == "unconfigured":
            reason = "credentials_unconfigured"
        elif binding["credential_state"] == "invalid":
            reason = "credentials_invalid"
        elif binding["catalog_snapshot_sha256"] != manifest.venue_catalog_snapshot_sha256:
            reason = "catalog_mismatch"
        elif binding["catalog_state"] != "ready":
            reason = "catalog_stale"
        elif binding["account_state"] == "exposure_present":
            reason = "unexpected_exposure"
        elif binding["runtime_state"] != "ready" or binding["account_state"] != "reconciled_flat":
            reason = "binding_unready"
        else:
            reason = "promotion_authority_unavailable"
        return self._block(case_id, run_id, policy_reason, reason, policy_checks, now_ms)

    def _block(
        self,
        case_id: str,
        run_id: str,
        policy_reason: str,
        capital_reason: str,
        policy_checks: Mapping[str, Any],
        now_ms: int,
    ) -> DecisionCommit:
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
        return DecisionCommit(state=CaseState.BLOCKED, reason=capital_reason)


__all__ = ["BindingAuthority", "CapitalAuthority", "DecisionCommit", "LaneStorage"]
