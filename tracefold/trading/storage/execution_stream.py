"""PostgreSQL transport for engine-neutral execution facts."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final, Literal
from uuid import UUID

from tracefold.platform.postgres.audit import BOUNDED_WINDOW_SCAN_BUDGET, ReadQuerySpec
from tracefold.platform.postgres.client import require_transaction

from ..execution_contracts import (
    EXECUTION_STRATEGY_ID,
    IDENTITY_PATTERN,
    MAX_OBSERVATION_APPEND_BATCH,
    MAX_OBSERVATION_APPEND_BYTES,
    SHA256_PATTERN,
    ExecutionObservationV1,
    OperatorIntentV1,
    TradeSignalV1,
    TradeSignalV2,
    postgres_text_valid,
)

MAX_EXECUTION_READ_BATCH = 1_000
# The append bounds and the two identity shapes come from the contract module that states them; this
# adapter used to re-declare all four at the same values, under a docstring over there claiming they
# had been unified (#604 T2).
_SHA256 = re.compile(SHA256_PATTERN)
_IDENTITY = re.compile(IDENTITY_PATTERN)
_OBSERVATION_BATCH_SAVEPOINT = "tracefold_execution_observation_batch"

type StoredExecutionPayload = tuple[int, dict[str, Any]]
# The current-projection columns, in the order `_runtime_state_values` binds them.
_RUNTIME_STATE_FIELDS: Final = (
    "account_slot",
    "mode",
    "runtime_id",
    "alive",
    "entries_armed",
    "unexpected_exposure",
    "positions_count",
    "open_orders_count",
    "protection_status",
    "heartbeat_at_ns",
    "entry_block_reason",
    "started_at_ns",
    "updated_at_ns",
    "account_snapshot",
    "routes_count",
)
_RUNTIME_STATE_COLUMNS: Final = ", ".join(_RUNTIME_STATE_FIELDS)


# The one canonical jsonb encoder for this package: sorted keys and no whitespace drift, so a payload
# and its digest cannot disagree. `lane` and `gate` import it from here.
def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


# The two reads the Runtime bridge runs every cycle: every Signal and Command for this account slot
# that is still inside its own TTL and has no disposition observation yet. Anti-joined rather than
# flagged, because a mutable "consumed" column on an append-only ledger would be a second truth about
# the same fact. Expiry is the bound: until #520 these were fenced by an activation waterline instead,
# which meant a Runtime could only be told about facts newer than the row that named it, and an
# expired intent stayed pending until someone wrote a disposition for it.
UNRESOLVED_TRADE_SIGNALS_SQL: Final = """
    SELECT signal.seq, signal.payload
      FROM trading_trade_signals signal
      LEFT JOIN trading_execution_observations disposition
        ON disposition.execution_strategy = %s
       AND disposition.account_slot = %s
       AND disposition.signal_id = signal.signal_id
       AND disposition.normalized_kind = 'signal_disposition'
      LEFT JOIN trading_trade_plans plan ON plan.entry_id = signal.signal_id
     WHERE signal.expires_at_ns > %s
       AND disposition.event_id IS NULL AND plan.entry_id IS NULL
     ORDER BY signal.seq
     LIMIT %s
"""

UNRESOLVED_TRADE_SIGNALS_V2_SQL: Final = """
    SELECT signal.seq, signal.payload
      FROM trading_trade_signals signal
      LEFT JOIN trading_execution_observations disposition
        ON disposition.execution_strategy = %s
       AND disposition.account_slot = %s
       AND disposition.signal_id = signal.signal_id
       AND disposition.normalized_kind = 'signal_disposition'
      LEFT JOIN trading_trade_plans plan ON plan.entry_id = signal.signal_id
     WHERE signal.account_slot = %s AND signal.runtime_mode = %s
       AND signal.payload ->> 'signal_version' = 'trade_signal_v2'
       AND signal.expires_at_ns > %s
       AND disposition.event_id IS NULL AND plan.entry_id IS NULL
     ORDER BY signal.seq LIMIT %s
"""

UNRESOLVED_OPERATOR_INTENTS_SQL: Final = """
    SELECT command.seq, command.payload
      FROM trading_operator_intents command
      LEFT JOIN trading_execution_observations disposition
        ON disposition.execution_strategy = %s
       AND disposition.account_slot = command.account_slot
       AND disposition.command_id = command.command_id
       AND disposition.normalized_kind = 'control_disposition'
      LEFT JOIN trading_trade_plans plan ON plan.entry_id = command.command_id
     WHERE command.account_slot = %s
       AND command.expires_at_ns > %s
       AND disposition.event_id IS NULL AND plan.entry_id IS NULL
     ORDER BY command.seq
     LIMIT %s
"""


def execution_stream_query_specs(
    *,
    account_slot: str = "query-audit-disabled",
    execution_strategy: str = EXECUTION_STRATEGY_ID,
    now_ns: int = 1,
) -> tuple[ReadQuerySpec, ...]:
    """The two bridge reads, bound, for the query-plan audit."""

    params = (execution_strategy, account_slot, now_ns, 100)
    return (
        ReadQuerySpec(
            name="trading_unresolved_trade_signals",
            sql=UNRESOLVED_TRADE_SIGNALS_SQL,
            params=params,
            max_read_return_amplification=20.0,
            max_scanned_rows=BOUNDED_WINDOW_SCAN_BUDGET,
        ),
        ReadQuerySpec(
            name="trading_unresolved_operator_intents",
            sql=UNRESOLVED_OPERATOR_INTENTS_SQL,
            params=params,
            max_read_return_amplification=20.0,
            max_scanned_rows=BOUNDED_WINDOW_SCAN_BUDGET,
        ),
    )


@dataclass(frozen=True, slots=True)
class PreparedTradeSignal:
    """Validated Signal input and canonical JSON prepared before the DB callback."""

    value: TradeSignalV1 | TradeSignalV2
    payload_json: str


@dataclass(frozen=True, slots=True)
class PreparedOperatorIntent:
    """Validated Command input and canonical JSON prepared before the DB callback."""

    value: OperatorIntentV1
    payload_json: str


@dataclass(frozen=True, slots=True)
class PreparedExecutionObservationBatch:
    """Validated, bounded Observation JSON prepared before the DB callback."""

    payload_json: str
    count: int


@dataclass(frozen=True, slots=True)
class ExecutionAccountPosition:
    """One position the Nautilus Cache holds, with the protective orders resting against it."""

    position_id: str
    instrument_id: str
    side: Literal["long", "short"]
    quantity: str
    entry_price: str
    mark_price: str | None
    unrealized_pnl_usd: str | None
    # Whether a non-terminal plan claims this instrument. Exposure no plan claims blocks new entries.
    owned: bool
    stop_trigger_price: str | None
    take_profit_trigger_price: str | None

    def __post_init__(self) -> None:
        if not self.position_id or len(self.position_id) > 256 or not postgres_text_valid(self.position_id):
            raise ValueError("execution_account_position_identity_invalid")
        if _IDENTITY.fullmatch(self.instrument_id) is None:
            raise ValueError("execution_account_position_instrument_invalid")
        if not self.quantity or not self.entry_price:
            raise ValueError("execution_account_position_value_invalid")

    @property
    def protected(self) -> bool:
        return self.stop_trigger_price is not None and self.take_profit_trigger_price is not None


@dataclass(frozen=True, slots=True)
class ExecutionAccountOrder:
    """One open or in-flight order in the Nautilus Cache."""

    client_order_id: str
    instrument_id: str
    state: Literal["open", "inflight"]
    leg: Literal["entry", "stop", "take_profit", "exit", "unknown"]
    quantity: str
    reduce_only: bool
    trigger_price: str | None
    owned: bool

    def __post_init__(self) -> None:
        if not self.client_order_id or len(self.client_order_id) > 256 or not postgres_text_valid(self.client_order_id):
            raise ValueError("execution_account_order_identity_invalid")
        if _IDENTITY.fullmatch(self.instrument_id) is None or not self.quantity:
            raise ValueError("execution_account_order_value_invalid")


@dataclass(frozen=True, slots=True)
class ExecutionAccountSnapshot:
    """What the account holds, read from the Nautilus Cache the Runtime executes against (#680).

    Nautilus reconciles the Cache with the venue at start and every five seconds after, so this is the
    Runtime's own picture, not a second proof beside it. `complete` says every position could be
    marked and the account balance was known, so `equity_usd` and the drawdown are whole numbers.
    """

    observed_at_ns: int
    equity_usd: str | None
    daily_drawdown_usd: str | None
    daily_drawdown_bps: int | None
    positions: tuple[ExecutionAccountPosition, ...]
    orders: tuple[ExecutionAccountOrder, ...]
    open_orders_count: int
    inflight_orders_count: int
    complete: bool

    def __post_init__(self) -> None:
        if self.observed_at_ns <= 0:
            raise ValueError("execution_account_snapshot_clock_invalid")
        if min(self.open_orders_count, self.inflight_orders_count) < 0:
            raise ValueError("execution_account_snapshot_count_invalid")
        if len(self.positions) > 100 or len(self.orders) > 200:
            raise ValueError("execution_account_snapshot_bounds_invalid")

    def payload(self) -> dict[str, Any]:
        return {
            "version": "execution_account_snapshot_v2",
            **asdict(self),
        }

    @classmethod
    def from_payload(cls, value: object) -> ExecutionAccountSnapshot:
        if not isinstance(value, dict) or value.get("version") != "execution_account_snapshot_v2":
            raise ValueError("execution_account_snapshot_invalid")
        try:
            payload = {key: item for key, item in value.items() if key != "version"}
            payload["positions"] = tuple(ExecutionAccountPosition(**item) for item in payload["positions"])
            payload["orders"] = tuple(ExecutionAccountOrder(**item) for item in payload["orders"])
            return cls(**payload)
        except (KeyError, TypeError, ValueError):
            raise ValueError("execution_account_snapshot_invalid") from None


@dataclass(frozen=True, slots=True)
class ExecutionRuntimeState:
    """The sole durable current projection for one execution account slot.

    `runtime_id` is the generation fence: `update_execution_runtime_state` only writes the row the
    running generation inserted, so a departing Runtime cannot overwrite its successor. The private
    account-proof facts that stood here -- `execution_safe`, `startup_reconciled`, `account_flat`,
    `reconciliation_observed_at_ns` and `facts_expire_at_ns` -- went with the proof (#680): Nautilus
    reconciles before the Strategy starts, so a running Runtime is a reconciled one.
    """

    account_slot: str
    mode: Literal["paper", "live"]
    runtime_id: UUID
    alive: bool
    entries_armed: bool
    unexpected_exposure: bool
    positions_count: int
    open_orders_count: int
    protection_status: Literal["not_applicable", "protected", "unprotected"]
    heartbeat_at_ns: int
    entry_block_reason: str | None
    started_at_ns: int
    updated_at_ns: int
    account_snapshot: ExecutionAccountSnapshot | None = None
    # How many `market_key`s this Runtime generation routes. Fixed for the life of one `runtime_id`,
    # so only the insert writes it.
    routes_count: int = 0

    def __post_init__(self) -> None:
        if _IDENTITY.fullmatch(self.account_slot) is None:
            raise ValueError("execution_runtime_identity_invalid")
        if self.mode not in {"paper", "live"}:
            raise ValueError("execution_runtime_mode_invalid")
        if min(self.heartbeat_at_ns, self.started_at_ns) <= 0:
            raise ValueError("execution_runtime_clock_invalid")
        if self.updated_at_ns < max(self.heartbeat_at_ns, self.started_at_ns):
            raise ValueError("execution_runtime_clock_invalid")
        if min(self.positions_count, self.open_orders_count) < 0:
            raise ValueError("execution_runtime_counts_invalid")
        if self.entries_armed and not (self.alive and not self.unexpected_exposure):
            raise ValueError("execution_runtime_armed_invalid")
        if self.entries_armed != (self.entry_block_reason is None):
            raise ValueError("execution_runtime_entry_reason_invalid")
        if self.entry_block_reason is not None and _IDENTITY.fullmatch(self.entry_block_reason) is None:
            raise ValueError("execution_runtime_entry_reason_invalid")
        if self.routes_count < 0:
            raise ValueError("execution_runtime_routes_invalid")


@dataclass(frozen=True, slots=True)
class ExecutionRuntimeControlState:
    """One slot-keyed current control projection; history stays append-only."""

    account_slot: str
    entries_paused: bool
    emergency_halted: bool
    last_command_seq: int
    last_command_id: str | None
    updated_at_ns: int

    def __post_init__(self) -> None:
        if _IDENTITY.fullmatch(self.account_slot) is None:
            raise ValueError("execution_account_slot_invalid")
        if self.last_command_seq < 0 or self.updated_at_ns <= 0:
            raise ValueError("execution_runtime_control_state_invalid")
        if self.last_command_id is not None and _SHA256.fullmatch(self.last_command_id) is None:
            raise ValueError("execution_runtime_control_state_invalid")
        if self.emergency_halted and not self.entries_paused:
            raise ValueError("execution_runtime_control_state_invalid")


def prepare_trade_signal(
    *,
    signal_id: str,
    case_id: str,
    market_key: str,
    direction: Literal["long", "short"],
    observed_at_ns: int,
    expires_at_ns: int,
) -> PreparedTradeSignal:
    value = TradeSignalV1(
        seq=1,
        signal_id=signal_id,
        case_id=case_id,
        market_key=market_key,
        direction=direction,
        observed_at_ns=observed_at_ns,
        expires_at_ns=expires_at_ns,
    )
    return PreparedTradeSignal(
        value=value,
        payload_json=_dumps(value.model_dump(mode="json", exclude={"seq"})),
    )


def prepare_trade_signal_v2(value: TradeSignalV2) -> PreparedTradeSignal:
    validated = TradeSignalV2.model_validate(value.model_dump())
    return PreparedTradeSignal(
        value=validated,
        payload_json=_dumps(validated.model_dump(mode="json", exclude={"seq"})),
    )


def prepare_operator_intent(
    *,
    command_id: str,
    account_slot: str,
    action: str,
    scope: str,
    reason: str,
    operator_identity: str,
    authentication_identity: str,
    requested_at_ns: int,
    expires_at_ns: int,
    market_key: str | None,
    direction: str | None,
) -> PreparedOperatorIntent:
    value = OperatorIntentV1.model_validate(
        {
            "seq": 1,
            "command_id": command_id,
            "account_slot": account_slot,
            "action": action,
            "scope": scope,
            "reason": reason,
            "operator_identity": operator_identity,
            "authentication_identity": authentication_identity,
            "requested_at_ns": requested_at_ns,
            "expires_at_ns": expires_at_ns,
            "market_key": market_key,
            "direction": direction,
        }
    )
    return PreparedOperatorIntent(
        value=value,
        payload_json=_dumps(value.model_dump(mode="json", exclude={"seq"})),
    )


def prepare_execution_observations(
    values: Sequence[ExecutionObservationV1],
) -> PreparedExecutionObservationBatch:
    if len(values) > MAX_OBSERVATION_APPEND_BATCH:
        raise ValueError("execution_observation_batch_count_exceeded")
    event_ids = tuple(value.event_id for value in values)
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("execution_observation_batch_identity_duplicate")
    payload_json = _dumps([value.model_dump(mode="json") for value in values])
    if len(payload_json.encode()) > MAX_OBSERVATION_APPEND_BYTES:
        raise ValueError("execution_observation_batch_bytes_exceeded")
    return PreparedExecutionObservationBatch(payload_json=payload_json, count=len(values))


def materialize_trade_signals(
    rows: Sequence[StoredExecutionPayload],
) -> tuple[TradeSignalV1 | TradeSignalV2, ...]:
    return tuple(
        TradeSignalV2.model_validate_json(_dumps(payload | {"seq": seq}))
        if payload.get("signal_version") == "trade_signal_v2"
        else TradeSignalV1.model_validate(payload | {"seq": seq})
        for seq, payload in rows
    )


def materialize_operator_intents(rows: Sequence[StoredExecutionPayload]) -> tuple[OperatorIntentV1, ...]:
    return tuple(OperatorIntentV1.model_validate(payload | {"seq": seq}) for seq, payload in rows)


def materialize_execution_observation(row: StoredExecutionPayload) -> ExecutionObservationV1:
    seq, payload = row
    del seq
    return ExecutionObservationV1.model_validate(payload)


class ExecutionStreamStorage:
    conn: Any

    def trade_signal(self, signal_id: str) -> StoredExecutionPayload | None:
        row = self.conn.execute(
            "SELECT seq,payload FROM trading_trade_signals WHERE signal_id=%s",
            (signal_id,),
        ).fetchone()
        return None if row is None else (int(row["seq"]), dict(row["payload"]))

    def append_trade_signal(self, prepared: PreparedTradeSignal) -> StoredExecutionPayload:
        require_transaction(self.conn, operation="append_trade_signal")
        candidate = prepared.value
        inserted = self.conn.execute(
            """
            INSERT INTO trading_trade_signals (
              signal_id, case_id, market_key, direction,
              observed_at_ns, expires_at_ns, payload,
              account_slot, runtime_mode, entry_scope_id, asset_id, mapping_semantics_digest
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING seq, payload
            """,
            (
                candidate.signal_id,
                candidate.case_id,
                candidate.market_key,
                candidate.direction,
                candidate.observed_at_ns,
                candidate.expires_at_ns,
                prepared.payload_json,
                candidate.account_slot if isinstance(candidate, TradeSignalV2) else None,
                candidate.runtime_mode if isinstance(candidate, TradeSignalV2) else None,
                candidate.entry_scope_id if isinstance(candidate, TradeSignalV2) else None,
                candidate.asset_id if isinstance(candidate, TradeSignalV2) else None,
                candidate.mapping_semantics_digest if isinstance(candidate, TradeSignalV2) else None,
            ),
        ).fetchone()
        if inserted is not None:
            return int(inserted["seq"]), dict(inserted["payload"])
        rows = self.conn.execute(
            """
            SELECT seq, payload, payload = %s::jsonb AS exact
              FROM trading_trade_signals
             WHERE signal_id = %s OR case_id = %s
             ORDER BY seq
            """,
            (prepared.payload_json, candidate.signal_id, candidate.case_id),
        ).fetchall()
        return self._require_single_exact_payload(rows)

    def append_operator_intent(self, prepared: PreparedOperatorIntent) -> StoredExecutionPayload:
        require_transaction(self.conn, operation="append_operator_intent")
        candidate = prepared.value
        inserted = self.conn.execute(
            """
            INSERT INTO trading_operator_intents (
              command_id, account_slot, action, scope, reason, operator_identity,
              authentication_identity, requested_at_ns, expires_at_ns,
              market_key, direction, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (command_id) DO NOTHING
            RETURNING seq, payload
            """,
            (
                candidate.command_id,
                candidate.account_slot,
                candidate.action,
                candidate.scope,
                candidate.reason,
                candidate.operator_identity,
                candidate.authentication_identity,
                candidate.requested_at_ns,
                candidate.expires_at_ns,
                candidate.market_key,
                candidate.direction,
                prepared.payload_json,
            ),
        ).fetchone()
        if inserted is not None:
            return int(inserted["seq"]), dict(inserted["payload"])
        rows = self.conn.execute(
            "SELECT seq, payload, payload = %s::jsonb AS exact FROM trading_operator_intents WHERE command_id = %s",
            (prepared.payload_json, candidate.command_id),
        ).fetchall()
        return self._require_single_exact_payload(rows)

    def append_execution_observations(self, prepared: PreparedExecutionObservationBatch) -> tuple[int, ...]:
        require_transaction(self.conn, operation="append_execution_observations")
        if prepared.count == 0:
            if prepared.payload_json != "[]":
                raise ValueError("execution_observation_batch_bounds_invalid")
            return ()
        self.conn.execute(f"SAVEPOINT {_OBSERVATION_BATCH_SAVEPOINT}")
        try:
            # `prepare_execution_observations` already bounded the batch, refused duplicate event ids
            # and validated every row, so the append is one ordinary INSERT. Until #520 PR-C this
            # statement re-derived those same bounds in SQL to feed the per-key `payload` CHECK.
            self.conn.execute(
                """
                INSERT INTO trading_execution_observations (
                  event_id, account_slot, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload
                )
                SELECT payload ->> 'event_id', payload ->> 'account_slot',
                       payload ->> 'execution_strategy',
                       payload ->> 'signal_id', payload ->> 'command_id',
                       payload ->> 'normalized_kind', (payload ->> 'occurred_at_ns')::bigint,
                       (payload ->> 'observed_at_ns')::bigint,
                       payload -> 'native_identity_references', payload -> 'summary', payload
                  FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS offered(payload, ordinal)
                 ORDER BY offered.ordinal
                ON CONFLICT (event_id) DO NOTHING
                """,
                (prepared.payload_json,),
            )
            resolved = self.conn.execute(
                """
                SELECT array_agg(existing.seq ORDER BY offered.ordinal) AS sequences
                  FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS offered(payload, ordinal)
                  JOIN trading_execution_observations existing
                    ON existing.event_id = offered.payload ->> 'event_id'
                """,
                (prepared.payload_json,),
            ).fetchone()
            stored = () if resolved is None else (resolved["sequences"] or ())
            if len(stored) != prepared.count:
                raise RuntimeError("execution_stream_identity_conflict")
            sequences = tuple(int(seq) for seq in stored)
            self._project_runtime_control_state(prepared.payload_json)
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {_OBSERVATION_BATCH_SAVEPOINT}")
            self.conn.execute(f"RELEASE SAVEPOINT {_OBSERVATION_BATCH_SAVEPOINT}")
            raise
        self.conn.execute(f"RELEASE SAVEPOINT {_OBSERVATION_BATCH_SAVEPOINT}")
        return sequences

    def _project_runtime_control_state(self, payload_json: str) -> None:
        """Advance current control only from durable Runtime-owned observations."""

        controls = self.conn.execute(
            """
            WITH offered AS (
              SELECT value AS payload
                FROM jsonb_array_elements(%s::jsonb)
            ), trading_eligible AS (
              SELECT DISTINCT ON (command.account_slot, command.seq)
                     command.account_slot,
                     command.seq AS command_seq,
                     command.command_id,
                     command.action,
                     (offered.payload ->> 'observed_at_ns')::bigint AS observed_at_ns
                FROM offered
                JOIN trading_operator_intents command
                  ON command.command_id = offered.payload ->> 'command_id'
                 AND command.account_slot = offered.payload ->> 'account_slot'
               WHERE command.action IN ('pause_entries', 'resume_entries', 'emergency_halt', 'flatten')
                 AND offered.payload ->> 'normalized_kind' = 'control_disposition'
                 AND offered.payload -> 'summary' ->> 'disposition' = 'accepted'
               ORDER BY command.account_slot, command.seq,
                        (offered.payload ->> 'observed_at_ns')::bigint DESC
            )
            SELECT account_slot, command_seq, command_id, action, observed_at_ns
              FROM trading_eligible
             ORDER BY command_seq, command_id
            """,
            (payload_json,),
        ).fetchall()
        for control in controls:
            self.conn.execute(
                """
                UPDATE trading_execution_runtime_control_state
                   SET entries_paused = CASE
                         WHEN emergency_halted THEN TRUE
                         WHEN %s = 'resume_entries' THEN FALSE
                         ELSE TRUE
                       END,
                       emergency_halted = emergency_halted OR %s = 'emergency_halt',
                       last_command_seq = %s,
                       last_command_id = %s,
                       updated_at_ns = GREATEST(updated_at_ns, %s)
                 WHERE account_slot = %s
                   AND last_command_seq < %s
                """,
                (
                    control["action"],
                    control["action"],
                    control["command_seq"],
                    control["command_id"],
                    control["observed_at_ns"],
                    control["account_slot"],
                    control["command_seq"],
                ),
            )

    def unresolved_trade_signals(
        self,
        *,
        account_slot: str,
        execution_strategy: str,
        now_ns: int,
        limit: int,
        runtime_mode: str | None = None,
    ) -> tuple[StoredExecutionPayload, ...]:
        self._validate_read_limit(limit)
        self._validate_slot_clock(account_slot, now_ns)
        if runtime_mode is None:
            rows = self.conn.execute(
                UNRESOLVED_TRADE_SIGNALS_SQL,
                (execution_strategy, account_slot, now_ns, limit),
            ).fetchall()
        else:
            if runtime_mode not in ("paper", "live"):
                raise ValueError("execution_runtime_mode_invalid")
            rows = self.conn.execute(
                UNRESOLVED_TRADE_SIGNALS_V2_SQL,
                (execution_strategy, account_slot, account_slot, runtime_mode, now_ns, limit),
            ).fetchall()
        return tuple((int(row["seq"]), dict(row["payload"])) for row in rows)

    def unresolved_operator_intents(
        self,
        *,
        account_slot: str,
        execution_strategy: str,
        now_ns: int,
        limit: int,
    ) -> tuple[StoredExecutionPayload, ...]:
        self._validate_read_limit(limit)
        self._validate_slot_clock(account_slot, now_ns)
        rows = self.conn.execute(
            UNRESOLVED_OPERATOR_INTENTS_SQL,
            (execution_strategy, account_slot, now_ns, limit),
        ).fetchall()
        return tuple((int(row["seq"]), dict(row["payload"])) for row in rows)

    def execution_runtime_state(self, account_slot: str) -> ExecutionRuntimeState | None:
        if _IDENTITY.fullmatch(account_slot) is None:
            raise ValueError("execution_account_slot_invalid")
        row = self.conn.execute(
            f"SELECT {_RUNTIME_STATE_COLUMNS} FROM trading_execution_runtime_state WHERE account_slot = %s",  # noqa: S608
            (account_slot,),
        ).fetchone()
        return None if row is None else self._materialize_runtime_state(row)

    def put_execution_runtime_state(self, value: ExecutionRuntimeState) -> ExecutionRuntimeState:
        require_transaction(self.conn, operation="put_execution_runtime_state")
        self.conn.execute(
            f"""
            INSERT INTO trading_execution_runtime_state ({_RUNTIME_STATE_COLUMNS})
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (account_slot) DO UPDATE SET
              {", ".join(f"{column} = EXCLUDED.{column}" for column in _RUNTIME_STATE_FIELDS[1:])}
            """,  # noqa: S608 -- module-owned column names; every value stays bound
            self._runtime_state_values(value),
        )
        return value

    def update_execution_runtime_state(self, value: ExecutionRuntimeState) -> bool:
        """Heartbeat only the generation that still owns the account-slot row."""

        require_transaction(self.conn, operation="update_execution_runtime_state")
        updated = self.conn.execute(
            """
            UPDATE trading_execution_runtime_state
               SET alive = %s, entries_armed = %s, unexpected_exposure = %s,
                   positions_count = %s, open_orders_count = %s, protection_status = %s,
                   heartbeat_at_ns = %s, entry_block_reason = %s, updated_at_ns = %s,
                   account_snapshot = %s::jsonb
             WHERE account_slot = %s AND runtime_id = %s
            """,
            (
                value.alive,
                value.entries_armed,
                value.unexpected_exposure,
                value.positions_count,
                value.open_orders_count,
                value.protection_status,
                value.heartbeat_at_ns,
                value.entry_block_reason,
                value.updated_at_ns,
                self._account_snapshot_json(value.account_snapshot),
                value.account_slot,
                value.runtime_id,
            ),
        )
        return bool(updated.rowcount == 1)

    @staticmethod
    def _materialize_runtime_state(row: Any) -> ExecutionRuntimeState:
        return ExecutionRuntimeState(
            account_slot=str(row["account_slot"]),
            mode=row["mode"],
            runtime_id=UUID(str(row["runtime_id"])),
            alive=bool(row["alive"]),
            entries_armed=bool(row["entries_armed"]),
            unexpected_exposure=bool(row["unexpected_exposure"]),
            positions_count=int(row["positions_count"]),
            open_orders_count=int(row["open_orders_count"]),
            protection_status=row["protection_status"],
            heartbeat_at_ns=int(row["heartbeat_at_ns"]),
            entry_block_reason=(None if row["entry_block_reason"] is None else str(row["entry_block_reason"])),
            started_at_ns=int(row["started_at_ns"]),
            updated_at_ns=int(row["updated_at_ns"]),
            account_snapshot=(
                None
                if row["account_snapshot"] is None
                else ExecutionAccountSnapshot.from_payload(dict(row["account_snapshot"]))
            ),
            routes_count=int(row["routes_count"]),
        )

    @staticmethod
    def _account_snapshot_json(value: ExecutionAccountSnapshot | None) -> str | None:
        return None if value is None else _dumps(value.payload())

    @classmethod
    def _runtime_state_values(cls, value: ExecutionRuntimeState) -> tuple[Any, ...]:
        return (
            value.account_slot,
            value.mode,
            value.runtime_id,
            value.alive,
            value.entries_armed,
            value.unexpected_exposure,
            value.positions_count,
            value.open_orders_count,
            value.protection_status,
            value.heartbeat_at_ns,
            value.entry_block_reason,
            value.started_at_ns,
            value.updated_at_ns,
            cls._account_snapshot_json(value.account_snapshot),
            value.routes_count,
        )

    def ensure_execution_runtime_control_state(
        self,
        account_slot: str,
        *,
        now_ns: int,
    ) -> ExecutionRuntimeControlState:
        """Return this slot's current control row, creating an unpaused one the first time.

        Control is a property of the account slot, not of a deployment: a slot that was resumed stays
        resumed across a restart, an image change or a risk-config change, and only a Command moves
        it. Before #520 every new `profile_id` inserted a fresh `entries_paused = TRUE` row, so each
        deploy silently disarmed entries and needed another authenticated `/resume`.
        """

        require_transaction(self.conn, operation="ensure_execution_runtime_control_state")
        if _IDENTITY.fullmatch(account_slot) is None:
            raise ValueError("execution_account_slot_invalid")
        if now_ns <= 0:
            raise ValueError("execution_runtime_control_state_invalid")
        self.conn.execute(
            """
            INSERT INTO trading_execution_runtime_control_state (
              account_slot, entries_paused, emergency_halted,
              last_command_seq, last_command_id, updated_at_ns
            ) VALUES (%s, FALSE, FALSE, 0, NULL, %s)
            ON CONFLICT (account_slot) DO NOTHING
            """,
            (account_slot, now_ns),
        )
        state = self.execution_runtime_control_state(account_slot)
        if state is None:
            raise RuntimeError("execution_runtime_control_state_unavailable")
        return state

    def execution_runtime_control_state(
        self,
        account_slot: str,
    ) -> ExecutionRuntimeControlState | None:
        """Read the one current control row; startup never folds command history."""

        if _IDENTITY.fullmatch(account_slot) is None:
            raise ValueError("execution_account_slot_invalid")
        row = self.conn.execute(
            """
            SELECT account_slot, entries_paused, emergency_halted,
                   last_command_seq, last_command_id, updated_at_ns
              FROM trading_execution_runtime_control_state
             WHERE account_slot = %s
            """,
            (account_slot,),
        ).fetchone()
        if row is None:
            return None
        return ExecutionRuntimeControlState(
            account_slot=str(row["account_slot"]),
            entries_paused=bool(row["entries_paused"]),
            emergency_halted=bool(row["emergency_halted"]),
            last_command_seq=int(row["last_command_seq"]),
            last_command_id=None if row["last_command_id"] is None else str(row["last_command_id"]),
            updated_at_ns=int(row["updated_at_ns"]),
        )

    def execution_observation(self, event_id: str) -> StoredExecutionPayload | None:
        if _SHA256.fullmatch(event_id) is None:
            raise ValueError("execution_observation_identity_invalid")
        row = self.conn.execute(
            "SELECT seq, payload FROM trading_execution_observations WHERE event_id = %s",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return int(row["seq"]), dict(row["payload"])

    def try_acquire_execution_account_slot(self, account_slot: str) -> bool:
        if _IDENTITY.fullmatch(account_slot) is None:
            raise ValueError("execution_account_slot_invalid")
        row = self.conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
            (f"tracefold:trading:execution-account-slot:{account_slot}",),
        ).fetchone()
        return bool(row and row["acquired"])

    def release_execution_account_slot(self, account_slot: str) -> bool:
        if _IDENTITY.fullmatch(account_slot) is None:
            raise ValueError("execution_account_slot_invalid")
        row = self.conn.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0)) AS released",
            (f"tracefold:trading:execution-account-slot:{account_slot}",),
        ).fetchone()
        return bool(row and row["released"])

    @staticmethod
    def _validate_slot_clock(account_slot: str, now_ns: int) -> None:
        if _IDENTITY.fullmatch(account_slot) is None:
            raise ValueError("execution_account_slot_invalid")
        if now_ns <= 0:
            raise ValueError("execution_stream_read_clock_invalid")

    @staticmethod
    def _validate_read_limit(limit: int) -> None:
        if not 1 <= limit <= MAX_EXECUTION_READ_BATCH:
            raise ValueError("execution_stream_read_limit_invalid")

    @staticmethod
    def _require_single_exact_payload(rows: Sequence[Any]) -> StoredExecutionPayload:
        if len(rows) != 1 or not rows[0]["exact"]:
            raise RuntimeError("execution_stream_identity_conflict")
        return int(rows[0]["seq"]), dict(rows[0]["payload"])


__all__ = [
    "MAX_EXECUTION_READ_BATCH",
    "UNRESOLVED_OPERATOR_INTENTS_SQL",
    "UNRESOLVED_TRADE_SIGNALS_SQL",
    "ExecutionAccountOrder",
    "ExecutionAccountPosition",
    "ExecutionAccountSnapshot",
    "ExecutionRuntimeState",
    "ExecutionStreamStorage",
    "PreparedExecutionObservationBatch",
    "PreparedOperatorIntent",
    "PreparedTradeSignal",
    "StoredExecutionPayload",
    "execution_stream_query_specs",
    "materialize_execution_observation",
    "materialize_operator_intents",
    "materialize_trade_signals",
    "prepare_execution_observations",
    "prepare_operator_intent",
    "prepare_trade_signal",
]
