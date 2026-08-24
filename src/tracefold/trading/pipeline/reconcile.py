"""Read reconciliation and deterministic exit ownership for Trading orders."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any, ClassVar, cast

from ..contracts import (
    TRADING_LIVE_APPROVAL_MARKER,
    TRADING_LIVE_APPROVAL_TTL_MS,
    TRADING_LIVE_MAX_ENTRY_DRIFT_BPS,
    TRADING_LIVE_PREFLIGHT_MAX_AGE_MS,
    Bar,
    ExchangeId,
    ExecutionAdapter,
    ExecutionObservation,
    InstrumentRef,
    LiveExecutionAdapter,
    LivePreflight,
    OrderSide,
    OrderState,
    PreparedOrder,
    TradingMode,
    canonical_sha256,
)
from ..contracts import (
    utc_day_key as _day_key,
)
from ..execution.order import must_close_at, protection_matches, realized_bps
from ..execution.paper import evaluate_paper_exit
from ..execution.submission import attempt_once, commit_order
from ..telemetry import (
    TradingExternalDataTelemetryPort,
    TradingWorkSemantics,
    external_data_source,
    observe_provider_call,
)
from .runtime import (
    BAR_INTERVAL_MS as _BAR_INTERVAL_MS,
)
from .runtime import (
    COLD_READ_TIMEOUT_SECONDS as _COLD_READ_TIMEOUT_SECONDS,
)
from .runtime import (
    COLD_WRITE_TIMEOUT_SECONDS as _COLD_WRITE_TIMEOUT_SECONDS,
)
from .runtime import (
    RECONCILE_BACKOFF_MS as _RECONCILE_BACKOFF_MS,
)
from .runtime import (
    BarFetcherFactory,
    TradingConfig,
    TradingDatabasePort,
)
from .runtime import (
    now_ms as _now_ms,
)
from .runtime import (
    sleep_or_stop as _sleep_or_stop,
)

log = logging.getLogger("tracefold.trading")

_RECONCILE_PERIOD_SECONDS = 30.0
_RECONCILE_BATCH = 32
# A prepared intent that never reached the network is provably harmless after this, and terminalising
# it is the only way a crash between the insert and the attempt claim gives back its underlying slot.
_PREPARED_TTL_MS = 900_000


class ReconcileRunner:
    """Read-only truth-seeking plus the two deterministic exits: the protective stop and the clock."""

    work_semantics: ClassVar[tuple[TradingWorkSemantics, ...]] = ("capital_truth",)

    def __init__(
        self,
        *,
        db: TradingDatabasePort,
        config: TradingConfig,
        bars: BarFetcherFactory,
        adapter: ExecutionAdapter,
        clock: Callable[[], int] = _now_ms,
        telemetry: TradingExternalDataTelemetryPort | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._bars = bars
        self._adapter = adapter
        self._clock = clock
        self._telemetry = telemetry
        self._last_failed = 0

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            started = time.perf_counter()
            try:
                result = await self.turn()
            except Exception:
                if self._telemetry is not None:
                    self._telemetry.record_external_data_turn(
                        "trading_reconcile",
                        "error",
                        time.perf_counter() - started,
                    )
                log.exception("trading reconcile turn failed")
            else:
                if self._telemetry is not None:
                    resolved = int(result.get("resolved") or 0)
                    self._telemetry.record_external_data_turn(
                        "trading_reconcile",
                        "partial" if self._last_failed and resolved else ("error" if self._last_failed else "success"),
                        time.perf_counter() - started,
                        target_count=int(result.get("due") or 0),
                    )
            await _sleep_or_stop(stop_event, _RECONCILE_PERIOD_SECONDS)

    async def turn(self) -> dict[str, Any]:
        now = self._clock()
        scan = await self._db.read(
            "trading_reconcile_scan",
            lambda repos: (
                repos.trading.due_orders(now_ms=now, limit=_RECONCILE_BATCH),
                (repos.trading.runtime_state() or {}).get("control", "RUNNING"),
            ),
            timeout_seconds=_COLD_READ_TIMEOUT_SECONDS,
        )
        due, control = scan
        resolved = 0
        self._last_failed = 0
        for row in due:
            try:
                if await self._reconcile(row, control=str(control), now=now):
                    resolved += 1
            except Exception:
                self._last_failed += 1
                log.exception("trading reconcile failed order=%s", row.get("order_id"))
        return {"due": len(due), "control": control, "resolved": resolved}

    async def _reconcile(self, row: dict[str, Any], *, control: str, now: int) -> bool:
        order_id = str(row["order_id"])
        state = str(row["state"])
        order = _order_from_row(row, max_holding_ms=self._config.order.max_holding_ms)

        if state in (OrderState.AMBIGUOUS.value, OrderState.RECONCILING.value):
            return await self._resolve_ambiguity(order, row, now=now)

        approval_deadline_ms: int | None = None
        if order.mode != "paper" and state == OrderState.AWAITING_APPROVAL.value:
            approval_age = now - int(row["created_at_ms"])
            if approval_age < 0 or approval_age > TRADING_LIVE_APPROVAL_TTL_MS:
                return await self._reject_unsubmitted(
                    order_id,
                    expected_state=OrderState.AWAITING_APPROVAL.value,
                    reason="approval_expired",
                    now=now,
                )
        elif order.mode != "paper" and state == OrderState.APPROVED.value:
            approval_deadline_ms = _approved_submission_deadline(
                created_at_ms=int(row["created_at_ms"]),
                approved_at_ms=int(row["updated_at_ms"]),
                approval_marker=str(row.get("state_reason") or ""),
            )
            if approval_deadline_ms is None or now > approval_deadline_ms:
                return await self._reject_unsubmitted(
                    order_id,
                    expected_state=OrderState.APPROVED.value,
                    reason="approval_expired",
                    now=now,
                )

        if state == OrderState.AWAITING_APPROVAL.value:
            await self._defer(order_id, state, now)
            return False

        if state == OrderState.APPROVED.value:
            if control in ("PAUSED", "CLOSE_ONLY"):
                # Both block *new* exposure and an approved-but-unsubmitted order is new exposure.
                # The reconciler reading the control state at all is the fix: before this, no control
                # setting reached it, so `close-only` and `paused` were indistinguishable and neither
                # could stop an approved entry from being submitted.
                await self._defer(order_id, state, now)
                return False
            if not self._adapter.writes_enabled:
                await self._defer(order_id, state, now)
                return False
            # The one place an approved order becomes a provider write. The caps are re-checked here
            # because `22eabf66` moved them into `_place._insert` — "count it where it is spent" — and
            # an AWAITING_APPROVAL order is inserted long before it is spent. Without this, ten orders
            # approved over several days all submit in one turn regardless of `max_orders_per_day`.
            over_cap = await self._db.read(
                "trading_approved_caps",
                lambda repos: (
                    repos.trading.orders_today(day_key=_day_key(now)) >= self._config.order.max_orders_per_day,
                    len(repos.trading.active_underlyings()) > self._config.order.max_open_underlyings,
                ),
                timeout_seconds=_COLD_READ_TIMEOUT_SECONDS,
            )
            if any(over_cap):
                await self._defer(order_id, state, now)
                return False
            return await self._submit_approved(
                order,
                created_at_ms=int(row["created_at_ms"]),
                approval_deadline_ms=cast(int, approval_deadline_ms),
                now=now,
            )

        if state == OrderState.SAFETY_CLOSING.value:
            # The exit's analogue of the `SUBMITTING` orphan. Without it a crash between the attempt
            # claim and the receipt write left a row the catch-all re-deferred forever, holding the
            # slot with an exit that could never be re-claimed.
            await self._db.tx(
                "trading_reconcile_exit_orphan",
                lambda repos: repos.trading.update_order(
                    order_id=order_id,
                    state=OrderState.AMBIGUOUS.value,
                    state_reason="exit_safety_closing_after_restart",
                    next_reconcile_at_ms=now,
                    now_ms=now,
                ),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
            return True

        if state == OrderState.SUBMITTING.value:
            # A `SUBMITTING` row that survived a restart is not a pending call — it is an attempt
            # whose answer was lost. Never resent, and never rerouted to the other venue.
            await self._db.tx(
                "trading_reconcile_orphan",
                lambda repos: repos.trading.update_order(
                    order_id=order_id,
                    state=OrderState.AMBIGUOUS.value,
                    state_reason="entry_submitting_after_restart",
                    next_reconcile_at_ms=now,
                    now_ms=now,
                ),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
            return True

        if state == OrderState.PREPARED.value:
            # `provider_attempt_count = 0` proves no network call ever left, so an expired prepared
            # intent is provably harmless to terminalise. Without this a crash between the insert and
            # the attempt claim held one of `max_open_underlyings` forever with no CLI able to clear it.
            if int(row.get("provider_attempt_count") or 0) == 0 and now - int(row["created_at_ms"]) > _PREPARED_TTL_MS:
                await self._db.tx(
                    "trading_reconcile_expire_prepared",
                    lambda repos: repos.trading.update_order(
                        order_id=order_id,
                        state=OrderState.REJECTED.value,
                        state_reason="prepared_expired_never_submitted",
                        closed_at_ms=now,
                        next_reconcile_at_ms=None,
                        now_ms=now,
                    ),
                    timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
                )
                return True
            await self._defer(order_id, state, now)
            return False

        if state in (
            OrderState.ACKNOWLEDGED.value,
            OrderState.OPEN.value,
            OrderState.PARTIAL.value,
            OrderState.UNPROTECTED.value,
        ):
            return await self._manage_open(order, row, now=now)

        await self._defer(order_id, state, now)
        return False

    async def _commit(self, order: PreparedOrder, now: int) -> bool:
        return await commit_order(
            db=self._db,
            adapter=self._adapter,
            order=order,
            now=now,
            observe_call=lambda call: observe_provider_call(
                self._telemetry,
                name="trading_reconcile",
                source=external_data_source(order.instrument.exchange_id),
                call=call,
            ),
        )

    async def _submit_approved(
        self,
        order: PreparedOrder,
        *,
        created_at_ms: int,
        approval_deadline_ms: int,
        now: int,
    ) -> bool:
        if order.mode == "paper":
            return await self._commit(order, now)
        if not isinstance(self._adapter, LiveExecutionAdapter):
            return await self._reject_unsubmitted(
                order.order_id,
                expected_state=OrderState.APPROVED.value,
                reason="live_repreflight_unavailable",
                now=now,
            )
        try:
            preflight = await observe_provider_call(
                self._telemetry,
                name="trading_reconcile",
                source=external_data_source(order.instrument.exchange_id),
                call=self._adapter.preflight(instrument=order.instrument, account_ref=order.account_ref),
            )
        except Exception:
            return await self._reject_unsubmitted(
                order.order_id,
                expected_state=OrderState.APPROVED.value,
                reason="live_repreflight_failed",
                now=self._clock(),
            )
        submit_now = self._clock()
        approval_expired = submit_now < created_at_ms or submit_now > approval_deadline_ms
        rejection = (
            "approval_expired"
            if approval_expired
            else _live_repreflight_rejection(order, preflight, config=self._config, now=submit_now)
        )
        audit = preflight.audit_payload()

        def _record(repos: Any) -> bool:
            repos.trading.record_observation(
                order_id=order.order_id,
                observation_kind="live_repreflight",
                content_sha256=canonical_sha256(audit),
                content=audit,
                now_ms=submit_now,
            )
            if rejection is not None:
                return bool(
                    repos.trading.reject_unsubmitted(
                        order_id=order.order_id,
                        expected_state=OrderState.APPROVED.value,
                        reason=(
                            "approval_expired" if rejection == "approval_expired" else f"live_repreflight_{rejection}"
                        ),
                        now_ms=submit_now,
                    )
                )
            return False

        rejected = await self._db.tx("trading_live_repreflight", _record, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        if rejection is not None:
            return bool(rejected)

        claim_now = self._clock()
        if claim_now < created_at_ms or claim_now > approval_deadline_ms:
            return await self._reject_unsubmitted(
                order.order_id,
                expected_state=OrderState.APPROVED.value,
                reason="approval_expired",
                now=claim_now,
            )
        late_rejection = _live_repreflight_rejection(order, preflight, config=self._config, now=claim_now)
        if late_rejection is not None:
            return await self._reject_unsubmitted(
                order.order_id,
                expected_state=OrderState.APPROVED.value,
                reason=f"live_repreflight_{late_rejection}",
                now=claim_now,
            )

        await self._commit(order, claim_now)
        current = await self._db.read(
            "trading_order_after_submit",
            lambda repos: repos.trading.order(order_id=order.order_id),
            timeout_seconds=_COLD_READ_TIMEOUT_SECONDS,
        )
        if current is None:
            return True
        if str(current["state"]) in {OrderState.ACKNOWLEDGED.value, OrderState.AMBIGUOUS.value}:
            return await self._reconcile(current, control="RUNNING", now=claim_now)
        return True

    async def _reject_unsubmitted(self, order_id: str, *, expected_state: str, reason: str, now: int) -> bool:
        return bool(
            await self._db.tx(
                "trading_reject_unsubmitted",
                lambda repos: repos.trading.reject_unsubmitted(
                    order_id=order_id,
                    expected_state=expected_state,
                    reason=reason,
                    now_ms=now,
                ),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
        )

    async def _defer(self, order_id: str, state: str, now: int) -> None:
        await self._db.tx(
            "trading_reconcile_defer",
            lambda repos: repos.trading.reschedule_order(
                order_id=order_id,
                expected_state=state,
                next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                now_ms=now,
            ),
            timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
        )

    async def _resolve_ambiguity(self, order: PreparedOrder, row: dict[str, Any], *, now: int) -> bool:
        """Read, never resend. A read that cannot prove either answer escalates to a human."""

        reason = str(row.get("state_reason") or "")
        exiting = reason.startswith("exit_") or reason == "close_ambiguous"

        try:
            observation = await observe_provider_call(
                self._telemetry,
                name="trading_reconcile",
                source=external_data_source(order.instrument.exchange_id),
                call=self._adapter.observe(order),
            )
        except Exception:
            # The row is still AMBIGUOUS; `reschedule_order` matches on the state it actually has, so
            # passing an aspirational one silently updated nothing and defeated the backoff.
            await self._defer(order.order_id, str(row["state"]), now)
            return False

        observed_now = self._clock()
        if order.mode != "paper":
            return await self._apply_live_observation(order, row, observation, now=observed_now, exiting=exiting)

        observed_state = str(observation.state)
        if observed_state == "ABSENT_CONFIRMED":
            if exiting:
                await self._escalate(order.order_id, "exit_ambiguous_position_absent", now)
                return True

            def _absent(repos: Any) -> None:
                repos.trading.record_observation(
                    order_id=order.order_id,
                    observation_kind="reconcile",
                    content_sha256=observation.snapshot_sha256,
                    content=observation.model_dump(mode="json"),
                    now_ms=now,
                )
                repos.trading.update_order(
                    order_id=order.order_id,
                    state=OrderState.REJECTED.value,
                    state_reason="proven_absent",
                    closed_at_ms=now,
                    next_reconcile_at_ms=None,
                    now_ms=now,
                )

            await self._db.tx("trading_reconcile_absent", _absent, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
            return True
        if exiting and observed_state == "UNKNOWN":
            await self._escalate(order.order_id, "exit_ambiguous_position_unknown", now)
            return True
        if observed_state == "WORKING":
            await self._db.tx(
                "trading_reconcile_working",
                lambda repos: repos.trading.update_order(
                    order_id=order.order_id,
                    state=OrderState.ACKNOWLEDGED.value,
                    state_reason="provider_working",
                    remote_order_id=observation.remote_order_id,
                    next_reconcile_at_ms=now,
                    now_ms=now,
                ),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
            return True
        if observed_state not in {"PARTIAL", "OPEN_PROTECTED", "OPEN_UNPROTECTED"}:
            await self._escalate(order.order_id, f"observed_{observed_state.lower()}", now)
            return True

        # A precise provider fill time wins. Otherwise submission/creation is the conservative lower
        # bound; reconciliation time must never extend the holding period.
        opened = int(observation.first_fill_at_ms or row.get("created_at_ms") or now)
        next_state = {
            "PARTIAL": OrderState.PARTIAL,
            "OPEN_PROTECTED": OrderState.OPEN,
            "OPEN_UNPROTECTED": OrderState.UNPROTECTED,
        }[observed_state]

        def _adopt(repos: Any) -> None:
            if exiting:
                # The close did not take effect — the venue still shows the position — so re-issuing it
                # cannot double-close. This release is the only thing that makes an ambiguous exit
                # recoverable; without it the position was unclosable and the row hot-looped forever.
                repos.trading.release_exit_attempt(order_id=order.order_id, now_ms=now)
            repos.trading.record_observation(
                order_id=order.order_id,
                observation_kind="reconcile",
                content_sha256=observation.snapshot_sha256,
                content=observation.model_dump(mode="json"),
                now_ms=now,
            )
            repos.trading.update_order(
                order_id=order.order_id,
                state=next_state.value,
                state_reason=f"resolved_by_read:{observed_state.lower()}",
                remote_order_id=observation.remote_order_id,
                filled_quantity=None if observation.filled_quantity is None else str(observation.filled_quantity),
                average_price=None if observation.average_price is None else str(observation.average_price),
                position_opened_at_ms=opened,
                must_close_at_ms=must_close_at(opened_at_ms=opened, policy=self._config.order),
                next_reconcile_at_ms=now,
                now_ms=now,
            )

        await self._db.tx("trading_reconcile_adopt", _adopt, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        return True

    async def _manage_live(self, order: PreparedOrder, row: dict[str, Any], *, now: int) -> bool:
        try:
            observation = await observe_provider_call(
                self._telemetry,
                name="trading_reconcile",
                source=external_data_source(order.instrument.exchange_id),
                call=self._adapter.observe(order),
            )
        except Exception:
            await self._defer(order.order_id, str(row["state"]), self._clock())
            return False
        return await self._apply_live_observation(order, row, observation, now=self._clock(), exiting=False)

    async def _apply_live_observation(
        self,
        order: PreparedOrder,
        row: dict[str, Any],
        observation: ExecutionObservation,
        *,
        now: int,
        exiting: bool,
    ) -> bool:
        observed_state = str(observation.state)
        prior_state = str(row["state"])
        had_position = prior_state in {
            OrderState.PARTIAL.value,
            OrderState.OPEN.value,
            OrderState.UNPROTECTED.value,
            OrderState.SAFETY_CLOSING.value,
            OrderState.RECONCILING.value,
        }
        if _observation_time_invalid(observation):
            await self._record_and_escalate(order.order_id, observation, "live_observation_time_invalid", now)
            return True
        if (
            order.remote_order_id is not None
            and observation.remote_order_id is not None
            and observation.remote_order_id != order.remote_order_id
        ):
            await self._record_and_escalate(order.order_id, observation, "live_remote_order_mismatch", now)
            return True
        if observed_state in {"UNKNOWN", "ABSENT_CONFIRMED", "WORKING", "REJECTED"} and (exiting or had_position):
            await self._record_and_escalate(
                order.order_id,
                observation,
                f"exit_position_{observed_state.lower()}",
                now,
            )
            return True
        if observed_state == "ABSENT_CONFIRMED" and prior_state == OrderState.ACKNOWLEDGED.value:
            if str(row.get("state_reason") or "") == "provider_visibility_pending":
                await self._record_and_escalate(
                    order.order_id,
                    observation,
                    "live_ack_visibility_unresolved",
                    now,
                )
            else:
                await self._record_live_state(
                    order,
                    observation,
                    state=OrderState.ACKNOWLEDGED,
                    reason="provider_visibility_pending",
                    now=now,
                )
            return True
        if observed_state in {"ABSENT_CONFIRMED", "REJECTED"}:
            content = observation.model_dump(mode="json")

            def _reject(repos: Any) -> None:
                repos.trading.record_observation(
                    order_id=order.order_id,
                    observation_kind="reconcile",
                    content_sha256=observation.snapshot_sha256,
                    content=content,
                    now_ms=now,
                )
                repos.trading.update_order(
                    order_id=order.order_id,
                    state=OrderState.REJECTED.value,
                    state_reason="proven_absent" if observed_state == "ABSENT_CONFIRMED" else "provider_rejected",
                    closed_at_ms=now,
                    next_reconcile_at_ms=None,
                    now_ms=now,
                )

            await self._db.tx("trading_live_rejected", _reject, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
            return True
        if observed_state == "CLOSED":
            if (
                observation.closed_at_ms is None
                or observation.average_price is None
                or observation.average_price <= 0
                or observation.exit_price is None
                or observation.exit_price <= 0
            ):
                await self._record_and_escalate(order.order_id, observation, "live_closed_evidence_incomplete", now)
                return True
            closed_at = int(observation.closed_at_ms)
            opened_at = int(
                observation.first_fill_at_ms or row.get("position_opened_at_ms") or row.get("created_at_ms") or now
            )
            if opened_at > closed_at:
                await self._record_and_escalate(order.order_id, observation, "live_closed_time_invalid", now)
                return True
            bps = realized_bps(
                side=order.side,
                entry=observation.average_price,
                exit_price=observation.exit_price,
                fee_bps=self._config.order.taker_fee_bps,
            )
            content = observation.model_dump(mode="json")

            def _closed(repos: Any) -> None:
                repos.trading.record_observation(
                    order_id=order.order_id,
                    observation_kind="reconcile",
                    content_sha256=observation.snapshot_sha256,
                    content=content,
                    now_ms=now,
                )
                repos.trading.update_order(
                    order_id=order.order_id,
                    state=OrderState.CLOSED.value,
                    state_reason="provider_closed",
                    average_price=None if observation.average_price is None else str(observation.average_price),
                    exit_price=None if observation.exit_price is None else str(observation.exit_price),
                    exit_reason="provider_closed",
                    realized_bps=bps,
                    position_opened_at_ms=opened_at,
                    position_closed_at_ms=closed_at,
                    closed_at_ms=closed_at,
                    next_reconcile_at_ms=None,
                    now_ms=now,
                )

            await self._db.tx("trading_live_closed", _closed, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
            return True
        if observed_state == "WORKING":
            if exiting:
                await self._record_and_escalate(order.order_id, observation, "exit_position_working", now)
                return True
            await self._record_live_state(
                order,
                observation,
                state=OrderState.ACKNOWLEDGED,
                reason="provider_working",
                now=now,
            )
            return True
        if observed_state not in {"PARTIAL", "OPEN_PROTECTED", "OPEN_UNPROTECTED"}:
            await self._record_and_escalate(
                order.order_id,
                observation,
                f"live_observed_{observed_state.lower()}",
                now,
            )
            return True
        if (
            observation.actual_position_quantity is None
            or observation.actual_position_quantity <= 0
            or observation.actual_position_quantity > order.quantity
            or observation.filled_quantity is None
            or observation.filled_quantity < observation.actual_position_quantity
            or observation.filled_quantity > order.quantity
            or observation.filled_quantity not in {observation.actual_position_quantity, order.quantity}
            or observation.average_price is None
        ):
            await self._record_and_escalate(order.order_id, observation, "live_position_evidence_incomplete", now)
            return True
        observed_order = (
            order
            if order.remote_order_id is not None or observation.remote_order_id is None
            else order.model_copy(update={"remote_order_id": observation.remote_order_id})
        )
        price_tick = _payload_decimal(order.payload, "_tracefoldPriceTick")
        protected = protection_matches(order=observed_order, observation=observation, price_tick=price_tick)
        partial = observation.filled_quantity < order.quantity
        next_state = (
            OrderState.PARTIAL if partial and protected else (OrderState.OPEN if protected else OrderState.UNPROTECTED)
        )
        opened = int(
            observation.first_fill_at_ms or row.get("position_opened_at_ms") or row.get("created_at_ms") or now
        )
        deadline = must_close_at(opened_at_ms=opened, policy=self._config.order)
        content = observation.model_dump(mode="json")

        def _position(repos: Any) -> None:
            if exiting:
                repos.trading.release_exit_attempt(order_id=order.order_id, now_ms=now)
            repos.trading.record_observation(
                order_id=order.order_id,
                observation_kind="reconcile",
                content_sha256=observation.snapshot_sha256,
                content=content,
                now_ms=now,
            )
            repos.trading.update_order(
                order_id=order.order_id,
                state=next_state.value,
                state_reason=(
                    "provider_partial_protected"
                    if next_state is OrderState.PARTIAL
                    else (
                        "provider_open_protected" if next_state is OrderState.OPEN else "native_protection_unverified"
                    )
                ),
                remote_order_id=observation.remote_order_id,
                filled_quantity=str(observation.filled_quantity),
                average_price=str(observation.average_price),
                position_opened_at_ms=opened,
                must_close_at_ms=deadline,
                next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                now_ms=now,
            )

        await self._db.tx("trading_live_position", _position, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        if not protected:
            return await self._attempt_live_close(
                observed_order,
                quantity=observation.actual_position_quantity,
                reason="unprotected",
                now=now,
            )
        if now >= deadline:
            return await self._attempt_live_close(
                observed_order,
                quantity=observation.actual_position_quantity,
                reason="max_holding",
                now=now,
            )
        return True

    async def _record_live_state(
        self,
        order: PreparedOrder,
        observation: ExecutionObservation,
        *,
        state: OrderState,
        reason: str,
        now: int,
    ) -> None:
        content = observation.model_dump(mode="json")

        def _write(repos: Any) -> None:
            repos.trading.record_observation(
                order_id=order.order_id,
                observation_kind="reconcile",
                content_sha256=observation.snapshot_sha256,
                content=content,
                now_ms=now,
            )
            repos.trading.update_order(
                order_id=order.order_id,
                state=state.value,
                state_reason=reason,
                remote_order_id=observation.remote_order_id,
                next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                now_ms=now,
            )

        await self._db.tx("trading_live_observation", _write, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)

    async def _attempt_live_close(
        self,
        order: PreparedOrder,
        *,
        quantity: Decimal,
        reason: str,
        now: int,
    ) -> bool:
        receipt, claim = await attempt_once(
            db=self._db,
            order=order,
            kind="exit",
            call=lambda: self._adapter.close(order, quantity=quantity),
            now=now,
            observe_call=lambda call: observe_provider_call(
                self._telemetry,
                name="trading_reconcile",
                source=external_data_source(order.instrument.exchange_id),
                call=call,
            ),
        )
        if receipt is None:
            if claim == "exhausted":
                await self._escalate(order.order_id, "exit_attempts_exhausted", now)
            return True
        content = receipt.model_dump(mode="json")

        def _write(repos: Any) -> None:
            repos.trading.record_observation(
                order_id=order.order_id,
                observation_kind="close",
                content_sha256=canonical_sha256(content),
                content=content,
                now_ms=now,
            )
            if receipt.state == "REJECTED":
                repos.trading.update_order(
                    order_id=order.order_id,
                    state=OrderState.MANUAL_REVIEW_REQUIRED.value,
                    state_reason=f"exit_rejected:{receipt.reason}",
                    next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                    now_ms=now,
                )
            else:
                repos.trading.update_order(
                    order_id=order.order_id,
                    state=(OrderState.AMBIGUOUS if receipt.state == "AMBIGUOUS" else OrderState.RECONCILING).value,
                    state_reason=(
                        "exit_close_ambiguous" if receipt.state == "AMBIGUOUS" else f"exit_{reason}_acknowledged"
                    ),
                    next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                    now_ms=now,
                )

        await self._db.tx("trading_live_close_receipt", _write, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        return True

    async def _record_and_escalate(
        self,
        order_id: str,
        observation: ExecutionObservation,
        reason: str,
        now: int,
    ) -> None:
        def _write(repos: Any) -> None:
            repos.trading.record_observation(
                order_id=order_id,
                observation_kind="reconcile",
                content_sha256=observation.snapshot_sha256,
                content=observation.model_dump(mode="json"),
                now_ms=now,
            )
            repos.trading.update_order(
                order_id=order_id,
                state=OrderState.MANUAL_REVIEW_REQUIRED.value,
                state_reason=reason,
                next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                now_ms=now,
            )

        await self._db.tx("trading_reconcile_live_observation", _write, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)

    async def _escalate(self, order_id: str, reason: str, now: int) -> None:
        await self._db.tx(
            "trading_reconcile_escalate",
            lambda repos: repos.trading.update_order(
                order_id=order_id,
                state=OrderState.MANUAL_REVIEW_REQUIRED.value,
                state_reason=reason,
                next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                now_ms=now,
            ),
            timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
        )

    async def _manage_open(self, order: PreparedOrder, row: dict[str, Any], *, now: int) -> bool:
        order_id = str(row["order_id"])
        current_state = str(row["state"])
        if order.mode != "paper":
            return await self._manage_live(order, row, now=now)
        if current_state == OrderState.ACKNOWLEDGED.value:
            try:
                observation = await observe_provider_call(
                    self._telemetry,
                    name="trading_reconcile",
                    source=external_data_source(order.instrument.exchange_id),
                    call=self._adapter.observe(order),
                )
            except Exception:
                await self._defer(order_id, current_state, now)
                return False
            if (
                str(observation.state) != "OPEN_PROTECTED"
                or observation.filled_quantity is None
                or observation.average_price is None
            ):
                await self._escalate(order_id, f"paper_observed_{str(observation.state).lower()}", now)
                return True
            opened = int(observation.first_fill_at_ms or row.get("created_at_ms") or now)
            deadline = must_close_at(opened_at_ms=opened, policy=self._config.order)

            def _promote(repos: Any) -> None:
                repos.trading.record_observation(
                    order_id=order_id,
                    observation_kind="reconcile",
                    content_sha256=observation.snapshot_sha256,
                    content=observation.model_dump(mode="json"),
                    now_ms=now,
                )
                repos.trading.promote_acknowledged(
                    order_id=order_id,
                    remote_order_id=observation.remote_order_id,
                    filled_quantity=str(observation.filled_quantity),
                    average_price=str(observation.average_price),
                    position_opened_at_ms=opened,
                    must_close_at_ms=deadline,
                    now_ms=now,
                )

            await self._db.tx(
                "trading_reconcile_open",
                _promote,
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
            current_state = OrderState.OPEN.value
        else:
            opened = int(row.get("position_opened_at_ms") or row.get("created_at_ms") or now)
            deadline = int(row.get("must_close_at_ms") or must_close_at(opened_at_ms=opened, policy=self._config.order))

        fetcher = self._bars(str(row["exchange_id"]))
        bars: Sequence[Bar] = ()
        if fetcher is not None:
            try:
                bars = await observe_provider_call(
                    self._telemetry,
                    name="trading_reconcile",
                    source=external_data_source(str(row["exchange_id"])),
                    call=fetcher(str(row["provider_symbol"]), opened, now + _BAR_INTERVAL_MS),
                )
            except Exception:
                log.warning("trading reconcile bar fetch failed order=%s", order_id)
                bars = ()

        exit_at = evaluate_paper_exit(
            side=cast(OrderSide, row["side"]),
            entry=Decimal(str(row["entry_reference"])),
            stop_price=Decimal(str(row["stop_price"])),
            take_profit_price=None if row.get("take_profit_price") is None else Decimal(str(row["take_profit_price"])),
            opened_at_ms=opened,
            must_close_at_ms=deadline,
            bars=bars,
            now_ms=now,
        )
        if exit_at is None:
            if now >= deadline:
                # The clock says close and the feed cannot price it. Deferring forever would hold the
                # active-underlying slot with no alarm, so the deadline escalates instead of waiting.
                await self._escalate(order_id, "max_holding_without_price", now)
                return True
            await self._defer(order_id, current_state, now)
            return False

        receipt, claim = await attempt_once(
            db=self._db,
            order=order,
            kind="exit",
            call=lambda: self._adapter.close(order, quantity=order.quantity),
            now=now,
            observe_call=lambda call: observe_provider_call(
                self._telemetry,
                name="trading_reconcile",
                source=external_data_source(order.instrument.exchange_id),
                call=call,
            ),
        )
        if receipt is None:
            if claim == "exhausted":
                # The bounded retry is spent. Escalate rather than returning silently: an unwritten
                # row stays due and re-enters this path every turn. The reason comes from the claim
                # itself, not from the batch snapshot — that copy of `exit_attempt_total` predates the
                # increment the claim just tested, so it was always one behind the value it guarded.
                await self._escalate(order_id, "exit_attempts_exhausted", now)
            elif claim in ("already_spent", "wrong_state"):
                await self._defer(order_id, current_state, now)
            return True
        if receipt.state == "AMBIGUOUS":
            await self._db.tx(
                "trading_close_ambiguous",
                lambda repos: repos.trading.update_order(
                    order_id=order_id,
                    state=OrderState.AMBIGUOUS.value,
                    state_reason="exit_close_ambiguous",
                    next_reconcile_at_ms=now + _RECONCILE_BACKOFF_MS,
                    now_ms=now,
                ),
                timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS,
            )
            return True
        if receipt.state == "REJECTED":
            # The venue refused the close, so the position is still open. Writing CLOSED here booked a
            # fabricated PnL and freed the underlying for a second order on top of a live position.
            await self._escalate(order_id, f"exit_rejected:{receipt.reason}", now)
            return True

        bps = realized_bps(
            side=cast(OrderSide, row["side"]),
            entry=Decimal(str(row["entry_reference"])),
            exit_price=exit_at.exit_price,
            fee_bps=self._config.order.taker_fee_bps,
        )

        def _close(repos: Any) -> None:
            repos.trading.record_observation(
                order_id=order_id,
                observation_kind="close",
                content_sha256=canonical_sha256(receipt.model_dump(mode="json")),
                content=receipt.model_dump(mode="json"),
                now_ms=now,
            )
            repos.trading.update_order(
                order_id=order_id,
                state=OrderState.CLOSED.value,
                state_reason=exit_at.reason,
                exit_price=str(exit_at.exit_price),
                exit_reason=exit_at.reason,
                realized_bps=bps,
                # Two timestamps on purpose: `position_closed_at_ms` is a real exit and is what the
                # cooldown and the realised-PnL denominator read; `closed_at_ms` only says the row is
                # terminal, and four different paths write it.
                position_closed_at_ms=exit_at.exit_at_ms,
                closed_at_ms=exit_at.exit_at_ms,
                next_reconcile_at_ms=None,
                now_ms=now,
            )

        await self._db.tx("trading_close", _close, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
        return True


def _approved_submission_deadline(*, created_at_ms: int, approved_at_ms: int, approval_marker: str) -> int | None:
    if approval_marker != TRADING_LIVE_APPROVAL_MARKER:
        return None
    approval_age = approved_at_ms - created_at_ms
    if approval_age < 0 or approval_age > TRADING_LIVE_APPROVAL_TTL_MS:
        return None
    return max(
        created_at_ms + TRADING_LIVE_APPROVAL_TTL_MS,
        approved_at_ms + _RECONCILE_BACKOFF_MS,
    )


def _live_repreflight_rejection(
    order: PreparedOrder,
    preflight: LivePreflight,
    *,
    config: TradingConfig,
    now: int,
) -> str | None:
    if not preflight.venue_healthy:
        return "venue_unhealthy"
    age = now - preflight.observed_at_ms
    if age < -TRADING_LIVE_PREFLIGHT_MAX_AGE_MS or age > TRADING_LIVE_PREFLIGHT_MAX_AGE_MS:
        return "stale"
    if preflight.requested_account_ref != order.account_ref or preflight.observed_account_ref != order.account_ref:
        return "account_mismatch"
    if preflight.positions or preflight.open_orders:
        return "remote_exposure"
    expected_contract = order.payload.get("_tracefoldExecutionContractSha256")
    if not isinstance(expected_contract, str) or expected_contract != preflight.execution_contract_sha256:
        return "execution_contract_drift"
    if order.payload.get("hedged") is not preflight.hedged:
        return "position_mode_drift"
    if preflight.leverage != 1:
        return "leverage_drift"
    if not preflight.margin_mode:
        return "margin_mode_unknown"
    if preflight.spread_bps < 0 or preflight.spread_bps > config.order.max_spread_bps:
        return "spread"
    if preflight.available_balance is None or preflight.available_balance < order.notional_usd:
        return "balance"
    if order.entry_reference <= 0 or preflight.mark_price <= 0:
        return "mark_invalid"
    drift_bps = abs(preflight.mark_price - order.entry_reference) / order.entry_reference * Decimal(10_000)
    if drift_bps > TRADING_LIVE_MAX_ENTRY_DRIFT_BPS:
        return "entry_drift"
    return None


def _payload_decimal(payload: dict[str, Any], key: str) -> Decimal | None:
    value = payload.get(key)
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _observation_time_invalid(observation: ExecutionObservation) -> bool:
    first_fill = observation.first_fill_at_ms
    closed = observation.closed_at_ms
    if first_fill is not None and (first_fill <= 0 or first_fill > observation.observed_at_ms):
        return True
    if closed is not None and (closed <= 0 or closed > observation.observed_at_ms):
        return True
    return first_fill is not None and closed is not None and first_fill > closed


def _order_from_row(row: dict[str, Any], *, max_holding_ms: int) -> PreparedOrder:
    return PreparedOrder(
        order_id=str(row["order_id"]),
        case_id=str(row["case_id"]),
        underlying_key=str(row["underlying_key"]),
        account_ref=str(row["account_ref"]),
        remote_order_id=None if row.get("remote_order_id") is None else str(row["remote_order_id"]),
        instrument=InstrumentRef(
            exchange_id=cast(ExchangeId, row["exchange_id"]),
            venue=str(row["exchange_id"]),
            provider_symbol=str(row["provider_symbol"]),
            base_symbol=str(row["underlying_key"]).removeprefix("crypto:"),
            instrument_class="crypto",
            observed_at_ms=int(row["created_at_ms"]),
        ),
        mode=cast(TradingMode, row["mode"]),
        side=cast(OrderSide, row["side"]),
        notional_usd=Decimal(str(row["notional_usd"])),
        quantity=Decimal(str(row["quantity"])),
        entry_reference=Decimal(str(row["entry_reference"])),
        stop_price=Decimal(str(row["stop_price"])),
        take_profit_price=None if row.get("take_profit_price") is None else Decimal(str(row["take_profit_price"])),
        must_close_after_ms=max_holding_ms,
        payload=dict(row.get("payload") or {}),
    )


__all__ = ["ReconcileRunner"]
