"""The Runtime's one outbox to PostgreSQL: execution observations and TradePlan transitions.

Nautilus callbacks run on the trading event loop and never touch PostgreSQL. They offer rows here; the
database bridge thread writes them one row per transaction (#680 RC6). A row the database refuses on
integrity grounds is dropped and logged, because no retry can change a verdict; a row that fails for
any other reason waits out a bounded backoff while every row behind it keeps flowing. Nothing here can
block a later write, and nothing here is fatal.

The one ordering rule is the entry handshake: a plan must be committed before its entry order exists,
so `prepare` hands the bridge exactly one pending insert and the Strategy submits only on a receipt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock

from tracefold.trading.execution_contracts import ExecutionObservationV1
from tracefold.trading.trade_plan import TradePlan

from .risk import DayStartBaseline

# Hours of fills and dispositions at this Runtime's volume. Past it the database has been unreachable
# for so long that the operator is already looking at a stale heartbeat; the venue keeps its own record.
_MAX_ROWS = 10_000
_MAX_BACKOFF_SECONDS = 30.0
_FIRST_BACKOFF_SECONDS = 0.5


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ObservationFactory:
    account_slot: str
    execution_strategy: str

    def create(
        self,
        *,
        normalized_kind: str,
        occurred_at_ns: int,
        observed_at_ns: int,
        signal_id: str | None = None,
        command_id: str | None = None,
        native_identity_references: Sequence[str] = (),
        summary: Mapping[str, str | int | bool] | None = None,
        event_identity: str | None = None,
        fixed_event_id: str | None = None,
    ) -> ExecutionObservationV1:
        # The summary and the normalized reference set fix the event id, so no two native events
        # collapse onto one and an identical re-offer is the same row.
        references = tuple(sorted(set(native_identity_references)))
        body = dict(summary or {})
        event_id = fixed_event_id or _sha256(
            {
                "account_slot": self.account_slot,
                "strategy": self.execution_strategy,
                "kind": normalized_kind,
                "signal": signal_id,
                "command": command_id,
                "native": references,
                "summary": _sha256(body),
                "identity": event_identity,
            }
        )
        return ExecutionObservationV1.model_validate(
            {
                "event_id": event_id,
                "account_slot": self.account_slot,
                "execution_strategy": self.execution_strategy,
                "signal_id": signal_id,
                "command_id": command_id,
                "normalized_kind": normalized_kind,
                "occurred_at_ns": occurred_at_ns,
                "observed_at_ns": max(observed_at_ns, occurred_at_ns),
                "native_identity_references": references,
                "summary": body,
            }
        )

    def day_start_baseline(
        self,
        *,
        utc_day: str,
        equity_usd: Decimal,
        recorded_at_ns: int,
    ) -> tuple[DayStartBaseline, ExecutionObservationV1]:
        if not equity_usd.is_finite() or equity_usd <= 0:
            raise ValueError("oi_runtime_day_start_equity_precision_invalid")
        summary: dict[str, str | int | bool] = {
            "risk_fact": "day_start_equity",
            "utc_day": utc_day,
            "equity_usd_decimal": format(equity_usd, "f"),
        }
        observation = self.create(
            normalized_kind="risk",
            occurred_at_ns=recorded_at_ns,
            observed_at_ns=recorded_at_ns,
            summary=summary,
            event_identity=f"day-start:{utc_day}",
            fixed_event_id=self.day_start_event_id(utc_day),
        )
        return (
            DayStartBaseline(
                utc_day=utc_day,
                equity_usd=equity_usd,
                recorded_at_ns=recorded_at_ns,
                event_id=observation.event_id,
            ),
            observation,
        )

    def day_start_event_id(self, utc_day: str) -> str:
        if len(utc_day) != 10:
            raise ValueError("oi_runtime_utc_day_invalid")
        return _sha256(
            {
                "account_slot": self.account_slot,
                "strategy": self.execution_strategy,
                "risk_fact": "day_start_equity",
                "utc_day": utc_day,
            }
        )


def day_start_baseline_from_observation(observation: ExecutionObservationV1) -> DayStartBaseline:
    summary = observation.summary
    if observation.normalized_kind != "risk" or summary.get("risk_fact") != "day_start_equity":
        raise ValueError("oi_runtime_day_start_observation_invalid")
    utc_day = summary.get("utc_day")
    decimal_value = summary.get("equity_usd_decimal")
    if not isinstance(utc_day, str) or not isinstance(decimal_value, str):
        raise ValueError("oi_runtime_day_start_observation_invalid")
    try:
        equity_usd = Decimal(decimal_value)
    except ArithmeticError as exc:
        raise ValueError("oi_runtime_day_start_observation_invalid") from exc
    if not equity_usd.is_finite() or equity_usd <= 0 or format(equity_usd, "f") != decimal_value:
        raise ValueError("oi_runtime_day_start_observation_invalid")
    return DayStartBaseline(
        utc_day=utc_day,
        equity_usd=equity_usd,
        recorded_at_ns=observation.occurred_at_ns,
        event_id=observation.event_id,
    )


@dataclass(frozen=True, slots=True)
class PlanReceipt:
    """The bridge's answer to one prepared plan.

    `committed` means the stored plan is exactly the one prepared, so its entry order may be sent.
    Anything else -- the database refused the insert, or a different plan already holds the identity
    -- carries the `reason` the Strategy disposes the input with, and no order is ever sent for it.
    """

    plan: TradePlan
    committed: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class EntryValidityReceipt:
    entry_id: str
    allowed: bool
    reason: str
    checked_at_ns: int
    version: str = "entry_validity_v1"


@dataclass(slots=True, eq=False)
class JournalRow:
    value: ExecutionObservationV1 | TradePlan
    attempts: int = 0
    not_before: float = 0.0

    @property
    def key(self) -> str:
        value = self.value
        return value.event_id if isinstance(value, ExecutionObservationV1) else f"plan:{value.entry_id}"


class ExecutionJournal:
    """Callback-safe FIFO of durable writes, drained one row per transaction by the bridge thread."""

    def __init__(self, *, factory: ObservationFactory, max_rows: int = _MAX_ROWS) -> None:
        if max_rows <= 0:
            raise ValueError("oi_runtime_journal_bounds_invalid")
        self.factory = factory
        self._max_rows = max_rows
        self._rows: list[JournalRow] = []
        self._index: dict[str, JournalRow] = {}
        self._prepare: TradePlan | None = None
        self._receipt: PlanReceipt | None = None
        self._validity_request: TradePlan | None = None
        self._validity_receipt: EntryValidityReceipt | None = None
        self._lock = Lock()

    # -- the Strategy's side ---------------------------------------------------------------------

    def offer(self, value: ExecutionObservationV1) -> bool:
        """Queue one observation. An identical re-offer is already on its way; a full journal refuses."""

        with self._lock:
            if value.event_id in self._index:
                return True
            if len(self._rows) >= self._max_rows:
                return False
            row = JournalRow(value)
            self._rows.append(row)
            self._index[row.key] = row
            return True

    def offer_plan(self, plan: TradePlan) -> None:
        """Queue one plan transition; a later transition of the same plan replaces a queued one.

        A plan transition is never refused: it is the durable intent a restart and a watchdog read,
        so a full journal grows by one row rather than losing it.
        """

        with self._lock:
            key = f"plan:{plan.entry_id}"
            queued = self._index.get(key)
            if queued is not None:
                current = queued.value
                if isinstance(current, TradePlan) and (
                    current.terminal_at_ns is not None or current.updated_at_ns > plan.updated_at_ns
                ):
                    return
                queued.value = plan
                return
            row = JournalRow(plan)
            self._rows.append(row)
            self._index[key] = row

    def prepare(self, plan: TradePlan) -> bool:
        if plan.status != "prepared":
            raise ValueError("trade_plan_prepare_status_invalid")
        with self._lock:
            if self._prepare is not None or self._receipt is not None:
                return False
            self._prepare = plan
            return True

    def take_receipt(self) -> PlanReceipt | None:
        with self._lock:
            receipt, self._receipt = self._receipt, None
            return receipt

    def request_entry_validity(self, plan: TradePlan) -> bool:
        with self._lock:
            if self._validity_request is not None or self._validity_receipt is not None:
                return False
            self._validity_request = plan
            return True

    def pending_entry_validity(self) -> TradePlan | None:
        with self._lock:
            return self._validity_request

    def settle_entry_validity(self, receipt: EntryValidityReceipt) -> None:
        with self._lock:
            if self._validity_request is None or self._validity_request.entry_id != receipt.entry_id:
                raise RuntimeError("entry_validity_identity_lost")
            self._validity_receipt = receipt
            self._validity_request = None

    def take_entry_validity(self) -> EntryValidityReceipt | None:
        with self._lock:
            receipt, self._validity_receipt = self._validity_receipt, None
            return receipt

    # -- the bridge's side -----------------------------------------------------------------------

    def pending_prepare(self) -> TradePlan | None:
        with self._lock:
            return self._prepare

    def settle_prepare(self, receipt: PlanReceipt) -> None:
        with self._lock:
            if self._prepare is None or self._prepare.entry_id != receipt.plan.entry_id:
                raise RuntimeError("trade_plan_prepare_identity_lost")
            self._receipt = receipt
            self._prepare = None

    def due(self, now_s: float) -> tuple[JournalRow, ...]:
        """Every queued row whose backoff has passed, oldest first."""

        with self._lock:
            return tuple(row for row in self._rows if row.not_before <= now_s)

    def written(self, row: JournalRow, value: ExecutionObservationV1 | TradePlan) -> None:
        """`value` is durable (or the database will never take it), so its row leaves the journal.

        A plan row whose value was replaced by a newer transition while the bridge was writing the
        older one stays queued: the newer transition still has to be written.
        """

        with self._lock:
            if row.value is not value:
                row.attempts = 0
                row.not_before = 0.0
                return
            if row in self._rows:
                self._rows.remove(row)
            if self._index.get(row.key) is row:
                del self._index[row.key]

    def retry_later(self, row: JournalRow, now_s: float) -> None:
        with self._lock:
            row.attempts += 1
            row.not_before = now_s + min(
                _MAX_BACKOFF_SECONDS, _FIRST_BACKOFF_SECONDS * (2 ** min(row.attempts - 1, 16))
            )

    def backlog(self) -> int:
        with self._lock:
            return len(self._rows)


__all__ = [
    "EntryValidityReceipt",
    "ExecutionJournal",
    "JournalRow",
    "ObservationFactory",
    "PlanReceipt",
    "day_start_baseline_from_observation",
]
