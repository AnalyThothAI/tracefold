"""Dormant PostgreSQL transport for engine-neutral execution facts."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from tracefold.platform.postgres.client import require_transaction

from ..execution_contracts import ExecutionObservationV1, OperatorIntentV1, TradeSignalV1
from .execution_stream_sql import UNRESOLVED_OPERATOR_INTENTS_SQL, UNRESOLVED_TRADE_SIGNALS_SQL
from .sql_values import _dumps

EXECUTION_STREAM_NOTIFY_CHANNEL = "tracefold_trading_execution_stream"
MAX_EXECUTION_READ_BATCH = 1_000
MAX_OBSERVATION_APPEND_BATCH = 128
MAX_OBSERVATION_APPEND_BYTES = 1_048_576
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$")
_OBSERVATION_BATCH_SAVEPOINT = "tracefold_execution_observation_batch"

type StoredExecutionPayload = tuple[int, dict[str, Any]]


def _postgres_text_valid(value: str) -> bool:
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


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
class ExecutionProfileActivation:
    """One immutable Runtime activation waterline; not a fourth process contract."""

    runtime_profile_id: str
    account_slot: str
    activated_after_signal_seq: int
    activated_after_command_seq: int
    mode: Literal["disabled", "paper", "live"]
    runtime_release: str
    config_sha256: str
    created_at_ns: int

    def __post_init__(self) -> None:
        if _IDENTITY.fullmatch(self.runtime_profile_id) is None:
            raise ValueError("execution_profile_identity_invalid")
        if _IDENTITY.fullmatch(self.account_slot) is None:
            raise ValueError("execution_account_slot_invalid")
        if self.activated_after_signal_seq < 0 or self.activated_after_command_seq < 0:
            raise ValueError("execution_profile_fence_invalid")
        if (
            not self.runtime_release
            or len(self.runtime_release) > 128
            or not _postgres_text_valid(self.runtime_release)
        ):
            raise ValueError("execution_profile_release_invalid")
        if _SHA256.fullmatch(self.config_sha256) is None:
            raise ValueError("execution_profile_config_invalid")
        if self.created_at_ns <= 0:
            raise ValueError("execution_profile_clock_invalid")

    def as_kwargs(self) -> dict[str, Any]:
        return asdict(self)


def prepare_trade_signal(
    *,
    signal_id: str,
    case_id: str,
    alpha_contract_sha256: str,
    market_key: str,
    direction: Literal["long", "short"],
    observed_at_ns: int,
    expires_at_ns: int,
    evidence_sha256: str,
    alpha_metadata: dict[str, str | int | bool] | None = None,
) -> PreparedTradeSignal:
    value = TradeSignalV1(
        seq=1,
        signal_id=signal_id,
        case_id=case_id,
        alpha_contract_sha256=alpha_contract_sha256,
        market_key=market_key,
        direction=direction,
        observed_at_ns=observed_at_ns,
        expires_at_ns=expires_at_ns,
        evidence_sha256=evidence_sha256,
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
    target_profile_id: str,
    action: str,
    scope: str,
    reason: str,
    operator_identity: str,
    authentication_identity: str,
    requested_at_ns: int,
    expires_at_ns: int,
    confirmation_identity: str | None,
    market_key: str | None,
    direction: str | None,
) -> PreparedOperatorIntent:
    value = OperatorIntentV1.model_validate(
        {
            "seq": 1,
            "command_id": command_id,
            "target_profile_id": target_profile_id,
            "action": action,
            "scope": scope,
            "reason": reason,
            "operator_identity": operator_identity,
            "authentication_identity": authentication_identity,
            "requested_at_ns": requested_at_ns,
            "expires_at_ns": expires_at_ns,
            "confirmation_identity": confirmation_identity,
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


class ExecutionStreamStorage:
    conn: Any

    def append_trade_signal(self, prepared: PreparedTradeSignal) -> StoredExecutionPayload:
        require_transaction(self.conn, operation="append_trade_signal")
        candidate = prepared.value
        inserted = self.conn.execute(
            """
            INSERT INTO trading_trade_signals (
              signal_id, case_id, alpha_contract_sha256, market_key, direction,
              observed_at_ns, expires_at_ns, evidence_sha256, alpha_metadata, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT DO NOTHING
            RETURNING seq, payload
            """,
            (
                candidate.signal_id,
                candidate.case_id,
                candidate.alpha_contract_sha256,
                candidate.market_key,
                candidate.direction,
                candidate.observed_at_ns,
                candidate.expires_at_ns,
                candidate.evidence_sha256,
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
              command_id, target_profile_id, action, scope, reason, operator_identity,
              authentication_identity, requested_at_ns, expires_at_ns,
              confirmation_identity, market_key, direction, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (command_id) DO NOTHING
            RETURNING seq, payload
            """,
            (
                candidate.command_id,
                candidate.target_profile_id,
                candidate.action,
                candidate.scope,
                candidate.reason,
                candidate.operator_identity,
                candidate.authentication_identity,
                candidate.requested_at_ns,
                candidate.expires_at_ns,
                candidate.confirmation_identity,
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
            inserted = self.conn.execute(
                """
                WITH batch AS (
                  SELECT payloads,
                         CASE WHEN jsonb_typeof(payloads) = 'array' THEN
                           jsonb_array_length(payloads) = %s
                           AND jsonb_array_length(payloads) <= 128
                           AND payload_bytes <= 1048576
                         ELSE FALSE END AS bounded
                    FROM (
                      SELECT %s::jsonb AS payloads, octet_length(%s::text) AS payload_bytes
                    ) input
                ), offered AS (
                  SELECT value AS payload, ordinality::integer AS ordinal
                    FROM batch
                    CROSS JOIN LATERAL jsonb_array_elements(
                      CASE WHEN batch.bounded THEN batch.payloads ELSE '[]'::jsonb END
                    ) WITH ORDINALITY
                ), identity_guard AS (
                  SELECT count(*) = count(DISTINCT payload ->> 'event_id') AS unique_event_ids
                    FROM offered
                ), inserted AS (
                  INSERT INTO trading_execution_observations (
                    event_id, runtime_profile_id, runtime_release, execution_strategy,
                    signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                    native_identity_references, summary, payload_digest, payload
                  )
                  SELECT payload ->> 'event_id', payload ->> 'runtime_profile_id',
                         payload ->> 'runtime_release', payload ->> 'execution_strategy',
                         payload ->> 'signal_id', payload ->> 'command_id',
                         payload ->> 'normalized_kind', (payload ->> 'occurred_at_ns')::bigint,
                         (payload ->> 'observed_at_ns')::bigint,
                         payload -> 'native_identity_references', payload -> 'summary',
                         payload ->> 'payload_digest', payload
                    FROM offered
                   WHERE (SELECT unique_event_ids FROM identity_guard)
                   ORDER BY ordinal
                  ON CONFLICT (event_id) DO NOTHING
                  RETURNING seq
                )
                SELECT batch.bounded, identity_guard.unique_event_ids,
                       (SELECT count(*) FROM inserted) AS inserted_count
                  FROM batch CROSS JOIN identity_guard
                """,
                (prepared.count, prepared.payload_json, prepared.payload_json),
            ).fetchone()
            if inserted is None or not inserted["bounded"]:
                raise ValueError("execution_observation_batch_bounds_invalid")
            if not inserted["unique_event_ids"]:
                raise ValueError("execution_observation_batch_identity_duplicate")
            resolved = self.conn.execute(
                """
                WITH offered AS (
                  SELECT value AS payload, ordinality::integer AS ordinal
                    FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY
                )
                SELECT count(*) = %s AS resolved_all,
                       COALESCE(bool_and(existing.payload = offered.payload), FALSE) AS all_exact,
                       array_agg(existing.seq ORDER BY offered.ordinal) AS sequences
                  FROM offered
                  JOIN trading_execution_observations existing
                    ON existing.event_id = offered.payload ->> 'event_id'
                """,
                (prepared.payload_json, prepared.count),
            ).fetchone()
            if resolved is None or not resolved["resolved_all"] or not resolved["all_exact"]:
                raise RuntimeError("execution_stream_identity_conflict")
            if int(inserted["inserted_count"]) > 0:
                self._notify("observation")
            sequences = tuple(int(seq) for seq in resolved["sequences"])
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {_OBSERVATION_BATCH_SAVEPOINT}")
            self.conn.execute(f"RELEASE SAVEPOINT {_OBSERVATION_BATCH_SAVEPOINT}")
            raise
        self.conn.execute(f"RELEASE SAVEPOINT {_OBSERVATION_BATCH_SAVEPOINT}")
        return sequences

    def append_execution_profile_activation(
        self,
        activation: ExecutionProfileActivation,
    ) -> ExecutionProfileActivation:
        require_transaction(self.conn, operation="append_execution_profile_activation")
        inserted = self.conn.execute(
            """
            INSERT INTO trading_execution_profile_activations (
              runtime_profile_id, account_slot, activated_after_signal_seq,
              activated_after_command_seq, mode, runtime_release, config_sha256, created_at_ns
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (runtime_profile_id) DO NOTHING
            RETURNING runtime_profile_id
            """,
            (
                activation.runtime_profile_id,
                activation.account_slot,
                activation.activated_after_signal_seq,
                activation.activated_after_command_seq,
                activation.mode,
                activation.runtime_release,
                activation.config_sha256,
                activation.created_at_ns,
            ),
        ).fetchone()
        if inserted is not None:
            self._notify("activation")
            return activation
        row = self.conn.execute(
            """
            SELECT account_slot = %s
                   AND activated_after_signal_seq = %s
                   AND activated_after_command_seq = %s
                   AND mode = %s
                   AND runtime_release = %s
                   AND config_sha256 = %s
                   AND created_at_ns = %s AS exact
              FROM trading_execution_profile_activations
             WHERE runtime_profile_id = %s
            """,
            (
                activation.account_slot,
                activation.activated_after_signal_seq,
                activation.activated_after_command_seq,
                activation.mode,
                activation.runtime_release,
                activation.config_sha256,
                activation.created_at_ns,
                activation.runtime_profile_id,
            ),
        ).fetchone()
        if row is None or not row["exact"]:
            raise RuntimeError("execution_stream_identity_conflict")
        return activation

    def unresolved_trade_signals(
        self,
        *,
        runtime_profile_id: str,
        execution_strategy: str,
        limit: int,
    ) -> tuple[StoredExecutionPayload, ...]:
        self._validate_read_limit(limit)
        rows = self.conn.execute(
            UNRESOLVED_TRADE_SIGNALS_SQL,
            (execution_strategy, runtime_profile_id, limit),
        ).fetchall()
        self._require_activation(runtime_profile_id)
        return tuple((int(row["seq"]), dict(row["payload"])) for row in rows)

    def unresolved_operator_intents(
        self,
        *,
        runtime_profile_id: str,
        execution_strategy: str,
        limit: int,
    ) -> tuple[StoredExecutionPayload, ...]:
        self._validate_read_limit(limit)
        rows = self.conn.execute(
            UNRESOLVED_OPERATOR_INTENTS_SQL,
            (execution_strategy, runtime_profile_id, limit),
        ).fetchall()
        self._require_activation(runtime_profile_id)
        return tuple((int(row["seq"]), dict(row["payload"])) for row in rows)

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

    def _require_activation(self, runtime_profile_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM trading_execution_profile_activations WHERE runtime_profile_id = %s",
            (runtime_profile_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("execution_profile_activation_missing")

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
    "ExecutionProfileActivation",
    "ExecutionStreamStorage",
    "PreparedExecutionObservationBatch",
    "PreparedOperatorIntent",
    "PreparedTradeSignal",
    "StoredExecutionPayload",
    "materialize_operator_intent",
    "materialize_operator_intents",
    "materialize_trade_signal",
    "materialize_trade_signals",
    "prepare_execution_observations",
    "prepare_operator_intent",
    "prepare_trade_signal",
]
