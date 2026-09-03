"""PostgreSQL transport for engine-neutral execution facts."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final, Literal
from uuid import UUID

from tracefold.platform.postgres.audit import ReadQuerySpec
from tracefold.platform.postgres.client import require_transaction

from ..contracts import EXECUTION_STRATEGY_ID
from ..execution_contracts import (
    MARKET_KEY_PATTERN,
    ExecutionObservationV1,
    OperatorIntentV1,
    TradeSignalV1,
    postgres_text_valid,
)

EXECUTION_STREAM_NOTIFY_CHANNEL = "tracefold_trading_execution_stream"
MAX_EXECUTION_READ_BATCH = 1_000
MAX_OBSERVATION_APPEND_BATCH = 128
MAX_OBSERVATION_APPEND_BYTES = 1_048_576
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$")
_MARKET_KEY = re.compile(MARKET_KEY_PATTERN)
# How many `market_key`s one Runtime generation may publish.
MAX_EXECUTION_ROUTES = 1_024
_OBSERVATION_BATCH_SAVEPOINT = "tracefold_execution_observation_batch"

type StoredExecutionPayload = tuple[int, dict[str, Any]]


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
     WHERE signal.expires_at_ns > %s
       AND disposition.event_id IS NULL
     ORDER BY signal.seq
     LIMIT %s
"""

UNRESOLVED_OPERATOR_INTENTS_SQL: Final = """
    SELECT command.seq, command.payload
      FROM trading_operator_intents command
      LEFT JOIN trading_execution_observations disposition
        ON disposition.execution_strategy = %s
       AND disposition.account_slot = command.account_slot
       AND disposition.command_id = command.command_id
       AND disposition.normalized_kind = 'control_disposition'
     WHERE command.account_slot = %s
       AND command.expires_at_ns > %s
       AND disposition.event_id IS NULL
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
        ),
        ReadQuerySpec(
            name="trading_unresolved_operator_intents",
            sql=UNRESOLVED_OPERATOR_INTENTS_SQL,
            params=params,
            max_read_return_amplification=20.0,
        ),
    )


@dataclass(frozen=True, slots=True)
class PreparedTradeSignal:
    """Validated Signal input and canonical JSON prepared before the DB callback."""

    value: TradeSignalV1
    metadata_json: str
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
    """One bounded position row in the current Runtime read projection."""

    position_id: str
    instrument_id: str
    side: Literal["long", "short"]
    quantity: str
    entry_price: str
    mark_price: str | None
    unrealized_pnl_usd: str | None
    owned: bool
    protection_status: Literal["protected", "pending", "unprotected", "unknown"]
    protection_quantity: str | None
    protection_trigger_price: str | None
    protection_full_coverage: bool

    def __post_init__(self) -> None:
        if not self.position_id or len(self.position_id) > 256 or not postgres_text_valid(self.position_id):
            raise ValueError("execution_account_position_identity_invalid")
        if _IDENTITY.fullmatch(self.instrument_id) is None:
            raise ValueError("execution_account_position_instrument_invalid")
        if not self.quantity or not self.entry_price:
            raise ValueError("execution_account_position_value_invalid")


@dataclass(frozen=True, slots=True)
class ExecutionAccountOrder:
    """One open or in-flight order in the current Runtime read projection."""

    client_order_id: str
    instrument_id: str
    state: Literal["open", "inflight"]
    leg: Literal["entry", "exit", "protection", "unknown"]
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
    """Current account read model derived by the sole Nautilus Runtime owner."""

    observed_at_ns: int
    market_observed_at_ns: int | None
    equity_usd: str | None
    day_start_equity_usd: str | None
    daily_drawdown_usd: str | None
    daily_drawdown_bps: int | None
    aggregate_risk_usd: str | None
    positions: tuple[ExecutionAccountPosition, ...]
    orders: tuple[ExecutionAccountOrder, ...]
    open_orders_count: int
    inflight_orders_count: int
    unknown_orders_count: int
    complete: bool
    truncated: bool = False
    # Whether the Runtime's durable audit copy is keeping up. It is reported, never enforced: Binance
    # holds the account's own order and fill history, so an unwritable local copy is a thing to show
    # an operator, not a reason to refuse exposure (#520 PR-B).
    audit_healthy: bool = True
    audit_failure_reason: str | None = None

    def __post_init__(self) -> None:
        if self.observed_at_ns <= 0 or (self.market_observed_at_ns is not None and self.market_observed_at_ns <= 0):
            raise ValueError("execution_account_snapshot_clock_invalid")
        if min(self.open_orders_count, self.inflight_orders_count, self.unknown_orders_count) < 0:
            raise ValueError("execution_account_snapshot_count_invalid")
        if len(self.positions) > 100 or len(self.orders) > 200:
            raise ValueError("execution_account_snapshot_bounds_invalid")
        if self.audit_failure_reason is not None and len(self.audit_failure_reason) > 128:
            raise ValueError("execution_account_snapshot_bounds_invalid")

    def payload(self) -> dict[str, Any]:
        return {
            "version": "execution_account_snapshot_v1",
            **asdict(self),
        }

    @classmethod
    def from_payload(cls, value: object) -> ExecutionAccountSnapshot:
        if not isinstance(value, dict) or value.get("version") != "execution_account_snapshot_v1":
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
    """The sole durable current projection for one execution account slot."""

    account_slot: str
    mode: Literal["paper", "live"]
    runtime_release: str
    config_sha256: str
    runtime_id: UUID
    runtime_revision: str
    image_digest: str
    credential_fingerprint: str
    lifecycle_state: Literal["starting", "running", "stopping", "stopped", "failed"]
    alive: bool
    execution_safe: bool
    entries_armed: bool
    startup_reconciled: bool
    unexpected_exposure: bool
    account_flat: bool
    positions_count: int
    open_orders_count: int
    protection_status: Literal["not_applicable", "protected", "pending", "unprotected", "unknown"]
    reconciliation_observed_at_ns: int
    heartbeat_at_ns: int
    entry_block_reason: str | None
    started_at_ns: int
    updated_at_ns: int
    account_snapshot: ExecutionAccountSnapshot | None = None
    # Every `market_key` this Runtime generation can actually reach, sorted by code point and unique,
    # as the Runtime discovered it at start. Fixed for the life of one `runtime_id`, like the release
    # and the config digest beside it, so only the insert writes it. Empty means no catalogue is
    # published and no catalogue is published and the Signal lane applies no routability rule.
    routes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _IDENTITY.fullmatch(self.account_slot) is None:
            raise ValueError("execution_runtime_identity_invalid")
        if self.mode not in {"paper", "live"}:
            raise ValueError("execution_runtime_mode_invalid")
        if _SHA256.fullmatch(self.config_sha256) is None:
            raise ValueError("execution_runtime_config_invalid")
        if self.image_digest != "unversioned" and re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_digest) is None:
            raise ValueError("execution_runtime_image_invalid")
        if _SHA256.fullmatch(self.credential_fingerprint) is None:
            raise ValueError("execution_runtime_credential_invalid")
        if self.reconciliation_observed_at_ns < 0 or min(self.heartbeat_at_ns, self.started_at_ns) <= 0:
            raise ValueError("execution_runtime_clock_invalid")
        if self.updated_at_ns < max(self.heartbeat_at_ns, self.started_at_ns):
            raise ValueError("execution_runtime_clock_invalid")
        if min(self.positions_count, self.open_orders_count) < 0:
            raise ValueError("execution_runtime_counts_invalid")
        if self.alive and self.lifecycle_state not in {"starting", "running", "stopping"}:
            raise ValueError("execution_runtime_alive_invalid")
        if self.execution_safe and not (self.alive and self.startup_reconciled and not self.unexpected_exposure):
            raise ValueError("execution_runtime_safe_invalid")
        if self.entries_armed and not self.execution_safe:
            raise ValueError("execution_runtime_armed_invalid")
        if self.entries_armed != (self.entry_block_reason is None):
            raise ValueError("execution_runtime_entry_reason_invalid")
        if self.entry_block_reason is not None and _IDENTITY.fullmatch(self.entry_block_reason) is None:
            raise ValueError("execution_runtime_entry_reason_invalid")
        if (
            len(self.routes) > MAX_EXECUTION_ROUTES
            or len(set(self.routes)) != len(self.routes)
            or list(self.routes) != sorted(self.routes)
            or any(_MARKET_KEY.fullmatch(value) is None for value in self.routes)
        ):
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
    alpha_metadata: dict[str, str | int | bool] | None = None,
) -> PreparedTradeSignal:
    value = TradeSignalV1(
        seq=1,
        signal_id=signal_id,
        case_id=case_id,
        market_key=market_key,
        direction=direction,
        observed_at_ns=observed_at_ns,
        expires_at_ns=expires_at_ns,
        alpha_metadata=alpha_metadata or {},
    )
    return PreparedTradeSignal(
        value=value,
        metadata_json=_dumps(value.alpha_metadata),
        payload_json=_dumps(value.model_dump(mode="json", exclude={"seq"})),
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


def materialize_trade_signal(row: StoredExecutionPayload) -> TradeSignalV1:
    seq, payload = row
    return TradeSignalV1.model_validate(payload | {"seq": seq})


def materialize_trade_signals(rows: Sequence[StoredExecutionPayload]) -> tuple[TradeSignalV1, ...]:
    return tuple(materialize_trade_signal(row) for row in rows)


def materialize_operator_intent(row: StoredExecutionPayload) -> OperatorIntentV1:
    seq, payload = row
    return OperatorIntentV1.model_validate(payload | {"seq": seq})


def materialize_operator_intents(rows: Sequence[StoredExecutionPayload]) -> tuple[OperatorIntentV1, ...]:
    return tuple(materialize_operator_intent(row) for row in rows)


def materialize_execution_observation(row: StoredExecutionPayload) -> ExecutionObservationV1:
    seq, payload = row
    del seq
    return ExecutionObservationV1.model_validate(payload)


class ExecutionStreamStorage:
    conn: Any

    def append_trade_signal(self, prepared: PreparedTradeSignal) -> StoredExecutionPayload:
        require_transaction(self.conn, operation="append_trade_signal")
        candidate = prepared.value
        inserted = self.conn.execute(
            """
            INSERT INTO trading_trade_signals (
              signal_id, case_id, market_key, direction,
              observed_at_ns, expires_at_ns, alpha_metadata, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
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
                prepared.metadata_json,
                prepared.payload_json,
            ),
        ).fetchone()
        if inserted is not None:
            self._notify("signal")
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
            self._notify("command")
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
            inserted = self.conn.execute(
                """
                INSERT INTO trading_execution_observations (
                  event_id, account_slot, runtime_release, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload
                )
                SELECT payload ->> 'event_id', payload ->> 'account_slot',
                       payload ->> 'runtime_release', payload ->> 'execution_strategy',
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
            if inserted.rowcount > 0:
                self._notify("observation")
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
                 AND (
                   (
                     offered.payload ->> 'normalized_kind' = 'control_disposition'
                     AND offered.payload -> 'summary' ->> 'disposition' IN ('accepted', 'completed')
                   ) OR (
                     command.action = 'flatten'
                     AND offered.payload ->> 'normalized_kind' = 'readiness'
                     AND offered.payload -> 'summary' ->> 'control_stage' = 'runtime_accepted'
                   )
                 )
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
    ) -> tuple[StoredExecutionPayload, ...]:
        self._validate_read_limit(limit)
        self._validate_slot_clock(account_slot, now_ns)
        rows = self.conn.execute(
            UNRESOLVED_TRADE_SIGNALS_SQL,
            (execution_strategy, account_slot, now_ns, limit),
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

    def execution_recovery_signals(
        self,
        *,
        account_slot: str,
        since_ns: int,
        limit: int,
    ) -> tuple[StoredExecutionPayload, ...]:
        """Read the Signals whose durable entry order can still hold Binance exposure.

        An identity is a recovery candidate only while its own facts leave that possible: it
        submitted an entry order inside the window, its latest position fact is not `closed`, and
        its latest entry-order fact is not terminal. A stopped-out identity is excluded here
        because `_matched_position` claims by instrument and direction alone, and a retired
        identity would otherwise adopt an unrelated position on the same route.
        """

        self._validate_read_limit(limit)
        self._validate_recovery_window(account_slot, since_ns)
        rows = self.conn.execute(
            """
            SELECT signal.seq, signal.payload
              FROM trading_trade_signals signal
             WHERE EXISTS (
                 SELECT 1
                   FROM trading_execution_observations entry_fact
                  WHERE entry_fact.account_slot = %s
                    AND entry_fact.signal_id = signal.signal_id
                    AND entry_fact.normalized_kind = 'order'
                    AND entry_fact.summary ->> 'leg' = 'entry'
                    AND entry_fact.observed_at_ns >= %s
               )
               AND NOT EXISTS (
                 SELECT 1
                   FROM trading_execution_observations closed_position
                  WHERE closed_position.account_slot = %s
                    AND closed_position.signal_id = signal.signal_id
                    AND closed_position.normalized_kind = 'position'
                    AND closed_position.summary ->> 'status' = 'closed'
                    AND closed_position.seq = (
                      SELECT max(latest.seq)
                        FROM trading_execution_observations latest
                       WHERE latest.account_slot = closed_position.account_slot
                         AND latest.signal_id = signal.signal_id
                         AND latest.normalized_kind = 'position'
                    )
               )
               AND NOT EXISTS (
                 SELECT 1
                   FROM trading_execution_observations retired_entry
                  WHERE retired_entry.account_slot = %s
                    AND retired_entry.signal_id = signal.signal_id
                    AND retired_entry.normalized_kind = 'order'
                    AND retired_entry.summary ->> 'leg' = 'entry'
                    AND retired_entry.summary ->> 'status' IN ('canceled', 'rejected', 'denied', 'expired')
                    AND retired_entry.seq = (
                      SELECT max(latest.seq)
                        FROM trading_execution_observations latest
                       WHERE latest.account_slot = retired_entry.account_slot
                         AND latest.signal_id = signal.signal_id
                         AND latest.normalized_kind = 'order'
                         AND latest.summary ->> 'leg' = 'entry'
                    )
               )
             ORDER BY signal.seq DESC
             LIMIT %s
            """,
            (account_slot, since_ns, account_slot, account_slot, limit),
        ).fetchall()
        return tuple((int(row["seq"]), dict(row["payload"])) for row in reversed(rows))

    def execution_recovery_manual_entries(
        self,
        *,
        account_slot: str,
        since_ns: int,
        limit: int,
    ) -> tuple[StoredExecutionPayload, ...]:
        """Read the manual entries whose durable entry order can still hold Binance exposure."""

        self._validate_read_limit(limit)
        self._validate_recovery_window(account_slot, since_ns)
        rows = self.conn.execute(
            """
            SELECT command.seq, command.payload
              FROM trading_operator_intents command
             WHERE command.account_slot = %s
               AND command.action = 'manual_entry'
               AND EXISTS (
                 SELECT 1
                   FROM trading_execution_observations entry_fact
                  WHERE entry_fact.account_slot = command.account_slot
                    AND entry_fact.command_id = command.command_id
                    AND entry_fact.normalized_kind = 'order'
                    AND entry_fact.summary ->> 'leg' = 'entry'
                    AND entry_fact.observed_at_ns >= %s
               )
               AND NOT EXISTS (
                 SELECT 1
                   FROM trading_execution_observations closed_position
                  WHERE closed_position.account_slot = command.account_slot
                    AND closed_position.command_id = command.command_id
                    AND closed_position.normalized_kind = 'position'
                    AND closed_position.summary ->> 'status' = 'closed'
                    AND closed_position.seq = (
                      SELECT max(latest.seq)
                        FROM trading_execution_observations latest
                       WHERE latest.account_slot = command.account_slot
                         AND latest.command_id = command.command_id
                         AND latest.normalized_kind = 'position'
                    )
               )
               AND NOT EXISTS (
                 SELECT 1
                   FROM trading_execution_observations retired_entry
                  WHERE retired_entry.account_slot = command.account_slot
                    AND retired_entry.command_id = command.command_id
                    AND retired_entry.normalized_kind = 'order'
                    AND retired_entry.summary ->> 'leg' = 'entry'
                    AND retired_entry.summary ->> 'status' IN ('canceled', 'rejected', 'denied', 'expired')
                    AND retired_entry.seq = (
                      SELECT max(latest.seq)
                        FROM trading_execution_observations latest
                       WHERE latest.account_slot = command.account_slot
                         AND latest.command_id = command.command_id
                         AND latest.normalized_kind = 'order'
                         AND latest.summary ->> 'leg' = 'entry'
                    )
               )
             ORDER BY command.seq DESC
             LIMIT %s
            """,
            (account_slot, since_ns, limit),
        ).fetchall()
        return tuple((int(row["seq"]), dict(row["payload"])) for row in reversed(rows))

    @staticmethod
    def _validate_recovery_window(account_slot: str, since_ns: int) -> None:
        if _IDENTITY.fullmatch(account_slot) is None:
            raise ValueError("execution_account_slot_invalid")
        if since_ns < 0:
            raise ValueError("execution_recovery_window_invalid")

    def execution_runtime_state(self, account_slot: str, *, for_update: bool = False) -> ExecutionRuntimeState | None:
        if _IDENTITY.fullmatch(account_slot) is None:
            raise ValueError("execution_account_slot_invalid")
        query = (
            """
            SELECT account_slot, mode, runtime_release, config_sha256,
                   runtime_id, runtime_revision, image_digest, credential_fingerprint,
                   lifecycle_state, alive, execution_safe, entries_armed,
                   startup_reconciled, unexpected_exposure, account_flat,
                   positions_count, open_orders_count, protection_status,
                   reconciliation_observed_at_ns, heartbeat_at_ns, entry_block_reason,
                   started_at_ns, updated_at_ns, account_snapshot, routes
              FROM trading_execution_runtime_state
             WHERE account_slot = %s
             FOR UPDATE
            """
            if for_update
            else """
            SELECT account_slot, mode, runtime_release, config_sha256,
                   runtime_id, runtime_revision, image_digest, credential_fingerprint,
                   lifecycle_state, alive, execution_safe, entries_armed,
                   startup_reconciled, unexpected_exposure, account_flat,
                   positions_count, open_orders_count, protection_status,
                   reconciliation_observed_at_ns, heartbeat_at_ns, entry_block_reason,
                   started_at_ns, updated_at_ns, account_snapshot, routes
              FROM trading_execution_runtime_state
             WHERE account_slot = %s
            """
        )
        row = self.conn.execute(query, (account_slot,)).fetchone()
        return None if row is None else self._materialize_runtime_state(row)

    def put_execution_runtime_state(self, value: ExecutionRuntimeState) -> ExecutionRuntimeState:
        require_transaction(self.conn, operation="put_execution_runtime_state")
        self.conn.execute(
            """
            INSERT INTO trading_execution_runtime_state (
              account_slot, mode, runtime_release, config_sha256,
              runtime_id, runtime_revision, image_digest, credential_fingerprint,
              lifecycle_state, alive, execution_safe, entries_armed,
              startup_reconciled, unexpected_exposure, account_flat,
              positions_count, open_orders_count, protection_status,
              reconciliation_observed_at_ns, heartbeat_at_ns, entry_block_reason,
              started_at_ns, updated_at_ns, account_snapshot, routes
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s::jsonb, %s::jsonb
            )
            ON CONFLICT (account_slot) DO UPDATE SET
              mode = EXCLUDED.mode,
              runtime_release = EXCLUDED.runtime_release,
              config_sha256 = EXCLUDED.config_sha256,
              runtime_id = EXCLUDED.runtime_id,
              runtime_revision = EXCLUDED.runtime_revision,
              image_digest = EXCLUDED.image_digest,
              credential_fingerprint = EXCLUDED.credential_fingerprint,
              lifecycle_state = EXCLUDED.lifecycle_state,
              alive = EXCLUDED.alive,
              execution_safe = EXCLUDED.execution_safe,
              entries_armed = EXCLUDED.entries_armed,
              startup_reconciled = EXCLUDED.startup_reconciled,
              unexpected_exposure = EXCLUDED.unexpected_exposure,
              account_flat = EXCLUDED.account_flat,
              positions_count = EXCLUDED.positions_count,
              open_orders_count = EXCLUDED.open_orders_count,
              protection_status = EXCLUDED.protection_status,
              reconciliation_observed_at_ns = EXCLUDED.reconciliation_observed_at_ns,
              heartbeat_at_ns = EXCLUDED.heartbeat_at_ns,
              entry_block_reason = EXCLUDED.entry_block_reason,
              started_at_ns = EXCLUDED.started_at_ns,
              updated_at_ns = EXCLUDED.updated_at_ns,
              account_snapshot = EXCLUDED.account_snapshot,
              routes = EXCLUDED.routes
            """,
            self._runtime_state_values(value),
        )
        return value

    def update_execution_runtime_state(self, value: ExecutionRuntimeState) -> bool:
        """Heartbeat only the generation that still owns the account-slot row."""

        require_transaction(self.conn, operation="update_execution_runtime_state")
        updated = self.conn.execute(
            """
            UPDATE trading_execution_runtime_state
               SET lifecycle_state = %s, alive = %s, execution_safe = %s,
                   entries_armed = %s,
                   startup_reconciled = %s, unexpected_exposure = %s, account_flat = %s,
                   positions_count = %s, open_orders_count = %s, protection_status = %s,
                   reconciliation_observed_at_ns = %s, heartbeat_at_ns = %s,
                   entry_block_reason = %s, updated_at_ns = %s,
                   account_snapshot = %s::jsonb
             WHERE account_slot = %s AND runtime_id = %s
            """,
            (
                value.lifecycle_state,
                value.alive,
                value.execution_safe,
                value.entries_armed,
                value.startup_reconciled,
                value.unexpected_exposure,
                value.account_flat,
                value.positions_count,
                value.open_orders_count,
                value.protection_status,
                value.reconciliation_observed_at_ns,
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
            runtime_release=str(row["runtime_release"]),
            config_sha256=str(row["config_sha256"]),
            runtime_id=UUID(str(row["runtime_id"])),
            runtime_revision=str(row["runtime_revision"]),
            image_digest=str(row["image_digest"]),
            credential_fingerprint=str(row["credential_fingerprint"]),
            lifecycle_state=row["lifecycle_state"],
            alive=bool(row["alive"]),
            execution_safe=bool(row["execution_safe"]),
            entries_armed=bool(row["entries_armed"]),
            startup_reconciled=bool(row["startup_reconciled"]),
            unexpected_exposure=bool(row["unexpected_exposure"]),
            account_flat=bool(row["account_flat"]),
            positions_count=int(row["positions_count"]),
            open_orders_count=int(row["open_orders_count"]),
            protection_status=row["protection_status"],
            reconciliation_observed_at_ns=int(row["reconciliation_observed_at_ns"]),
            heartbeat_at_ns=int(row["heartbeat_at_ns"]),
            entry_block_reason=(None if row["entry_block_reason"] is None else str(row["entry_block_reason"])),
            started_at_ns=int(row["started_at_ns"]),
            updated_at_ns=int(row["updated_at_ns"]),
            account_snapshot=(
                None
                if row["account_snapshot"] is None
                else ExecutionAccountSnapshot.from_payload(dict(row["account_snapshot"]))
            ),
            routes=tuple(str(value) for value in row["routes"]),
        )

    @staticmethod
    def _account_snapshot_json(value: ExecutionAccountSnapshot | None) -> str | None:
        return None if value is None else _dumps(value.payload())

    @classmethod
    def _runtime_state_values(cls, value: ExecutionRuntimeState) -> tuple[Any, ...]:
        return (
            value.account_slot,
            value.mode,
            value.runtime_release,
            value.config_sha256,
            value.runtime_id,
            value.runtime_revision,
            value.image_digest,
            value.credential_fingerprint,
            value.lifecycle_state,
            value.alive,
            value.execution_safe,
            value.entries_armed,
            value.startup_reconciled,
            value.unexpected_exposure,
            value.account_flat,
            value.positions_count,
            value.open_orders_count,
            value.protection_status,
            value.reconciliation_observed_at_ns,
            value.heartbeat_at_ns,
            value.entry_block_reason,
            value.started_at_ns,
            value.updated_at_ns,
            cls._account_snapshot_json(value.account_snapshot),
            _dumps(list(value.routes)),
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

    def _notify(self, kind: str) -> None:
        self.conn.execute("SELECT pg_notify(%s, %s)", (EXECUTION_STREAM_NOTIFY_CHANNEL, kind))


__all__ = [
    "EXECUTION_STREAM_NOTIFY_CHANNEL",
    "MAX_EXECUTION_READ_BATCH",
    "MAX_OBSERVATION_APPEND_BATCH",
    "MAX_OBSERVATION_APPEND_BYTES",
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
    "materialize_operator_intent",
    "materialize_operator_intents",
    "materialize_trade_signal",
    "materialize_trade_signals",
    "prepare_execution_observations",
    "prepare_operator_intent",
    "prepare_trade_signal",
]
