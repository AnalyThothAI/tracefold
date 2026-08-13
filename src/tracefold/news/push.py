from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from tracefold.platform.resource import (
    ResourceAdmissionTimeout,
    ResourceCapability,
    ResourceOperationOverrun,
)

from .notification import (
    PUSH_PROVIDER_SCORE_THRESHOLD,
    PUSH_SOURCE_FRESHNESS_MS,
    NewsPushEligibility,
    evaluate_news_push_eligibility,
)

PUSH_PAYLOAD_SCHEMA_VERSION = "news_story_push_v1"

_DELIVERY_MAX_ATTEMPTS = 6
_DELIVERY_RETRY_DELAYS_MS = (5_000, 30_000, 120_000, 600_000, 1_800_000)
_LEASE_MS = 60_000
_DELIVERY_TIMEOUT_SECONDS = 12.0
_RECONCILE_MINIMUM_CYCLE_MS = 25_000


class _NewsPushDeliverySuppressed(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NewsPushReceipt:
    provider: str
    receipt_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PreparedNewsPush:
    """One complete delivery envelope and its durable localization outcome."""

    payload: Mapping[str, Any]
    translation_status: str


class NewsPushDelivery(Protocol):
    """News-side bridge run only through FiniteOperations.

    ``prepare`` owns optional presentation-only external work and returns a
    complete envelope exactly once. The caller hashes and persists that opaque
    result. Its pre-submit callback is the durable at-most-once translation
    fence; an interrupted fenced preparation must render the original without
    resubmission. ``deliver`` receives only the frozen envelope on every retry.
    """

    async def prepare(
        self,
        source_payload: Mapping[str, Any],
        *,
        deadline_ms: int,
        before_translation_submit: Callable[[], Awaitable[None]] | None = None,
        interrupted_translation_attempted_at_ms: int | None = None,
    ) -> PreparedNewsPush: ...

    def deliver(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> NewsPushReceipt: ...

    def close(self) -> None: ...


class NewsPushDeliveryError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = str(code)[:500]
        self.retryable = bool(retryable)
        super().__init__(self.code)


class NewsStoryPush:
    """Durable Story-scoped push state machine.

    PostgreSQL calls are short phases around payload freezing and delivery.
    External I/O never runs inside a transaction or on the event loop: delivery
    uses FiniteOperations, and the pure one-time card renderer runs inline
    before its output is frozen.
    """

    def __init__(
        self,
        *,
        db: Any,
        finite_operations: Any,
        delivery: NewsPushDelivery,
        runtime_id: str,
        reconcile_cycle_ms: int = _RECONCILE_MINIMUM_CYCLE_MS,
    ) -> None:
        if reconcile_cycle_ms < 0:
            raise ValueError("news_push_reconcile_cycle_ms_invalid")
        self.db = db
        self.finite_operations = finite_operations
        self.delivery = delivery
        self.lease_owner = f"news_story_push:{runtime_id}"
        self.reconcile_cycle_ms = int(reconcile_cycle_ms)

    async def reconcile(self, *, now_ms: int) -> dict[str, int]:
        return cast(
            dict[str, int],
            await self.db.run_business(
                "news_story_push_reconcile",
                self._reconcile_sync,
                int(now_ms),
                operation_timeout_seconds=3.0,
            ),
        )

    async def close(self) -> None:
        """Close the synchronous delivery adapter through its owning capability."""

        await self.finite_operations.run(
            "news_story_push_delivery_close",
            self.delivery.close,
            timeout_seconds=5.0,
            allow_shutdown=True,
        )

    async def turn(self) -> bool | None:
        """Deliver one due Story without occupying the serial model arbiter."""

        try:
            now_ms = _now_ms()
            row = await self.db.run_business(
                "news_story_push_peek",
                self._peek_sync,
                now_ms,
                operation_timeout_seconds=3.0,
            )
            if row is None:
                return False
            return await self._execute_story(str(row["story_id"]), now_ms=now_ms)
        except ResourceAdmissionTimeout:
            return None

    async def _execute_story(self, story_id: str, *, now_ms: int) -> bool:
        lease_token = secrets.token_hex(16)
        claim = await self.db.run_business(
            "news_story_push_claim",
            self._claim_sync,
            story_id,
            lease_token,
            now_ms,
            operation_timeout_seconds=0.5,
        )
        if claim is None:
            return False

        source_payload = dict(claim["source_payload"])
        provider_evidence = dict(source_payload["provider_evidence"])
        published_at_ms = int(provider_evidence["published_at_ms"])
        translation_attempted_at_ms = (
            int(claim["updated_at_ms"]) if claim.get("translation_status") == "attempted" else None
        )
        push_baseline_at_ms = (
            int(claim["push_baseline_at_ms"]) if claim.get("push_baseline_at_ms") is not None else None
        )
        eligibility = await self._current_eligibility(
            selected_item_id=str(claim["selected_item_id"]),
            baseline_at_ms=push_baseline_at_ms,
            now_ms=now_ms,
        )
        if not eligibility.eligible and (
            claim.get("delivery_payload") is not None
            or translation_attempted_at_ms is None
            or eligibility.ineligible_reason != "stale"
        ):
            if claim.get("delivery_payload") is None:
                suppressed = await self.db.run_business(
                    "news_story_push_suppress_filtered",
                    self._suppress_claimed_sync,
                    story_id,
                    lease_token,
                    now_ms,
                    operation_timeout_seconds=1.0,
                )
            else:
                attempt = await self.db.run_business(
                    "news_story_push_start_filtered_suppression",
                    self._start_delivery_sync,
                    story_id,
                    lease_token,
                    now_ms,
                    operation_timeout_seconds=0.5,
                )
                if attempt is None:
                    return False
                suppressed = await self.db.run_business(
                    "news_story_push_suppress_filtered",
                    self._suppress_unsubmitted_sync,
                    story_id,
                    lease_token,
                    now_ms,
                    operation_timeout_seconds=0.5,
                )
            return bool(suppressed)
        if claim.get("delivery_payload") is None:
            deadline_ms = published_at_ms + PUSH_SOURCE_FRESHNESS_MS

            async def fence_translation_dispatch() -> None:
                nonlocal translation_attempted_at_ms
                attempted_at_ms = _now_ms()
                try:
                    fenced = await self.db.run_business(
                        "news_story_push_fence_translation",
                        self._fence_translation_sync,
                        story_id,
                        lease_token,
                        attempted_at_ms,
                        operation_timeout_seconds=0.5,
                    )
                except ResourceAdmissionTimeout:
                    raise
                except ResourceOperationOverrun:
                    raise
                except Exception as exc:
                    raise RuntimeError("news_story_push_translation_fence_failed") from exc
                if not fenced:
                    raise RuntimeError("news_story_push_translation_fence_lost")
                translation_attempted_at_ms = attempted_at_ms

            try:
                prepared = await self.delivery.prepare(
                    source_payload,
                    deadline_ms=deadline_ms,
                    before_translation_submit=(
                        fence_translation_dispatch if translation_attempted_at_ms is None else None
                    ),
                    interrupted_translation_attempted_at_ms=(translation_attempted_at_ms),
                )
                if (
                    not isinstance(prepared, PreparedNewsPush)
                    or prepared.translation_status not in {"translated", "not_needed", "unavailable"}
                    or not isinstance(prepared.payload, Mapping)
                    or not prepared.payload
                ):
                    raise NewsPushDeliveryError(
                        "news_story_push_prepare_result_invalid",
                        retryable=False,
                    )
                delivery_payload = dict(prepared.payload)
                try:
                    payload_fingerprint = _payload_fingerprint(delivery_payload)
                except (TypeError, ValueError):
                    raise NewsPushDeliveryError(
                        "news_story_push_prepare_result_invalid",
                        retryable=False,
                    ) from None
            except asyncio.CancelledError:
                await asyncio.shield(
                    self.db.run_business(
                        "news_story_push_release_preparation",
                        self._release_preparation_sync,
                        story_id,
                        lease_token,
                        _now_ms(),
                        operation_timeout_seconds=0.5,
                    )
                )
                raise
            except ResourceAdmissionTimeout:
                released = await self.db.run_business(
                    "news_story_push_release_preparation",
                    self._release_preparation_sync,
                    story_id,
                    lease_token,
                    _now_ms(),
                    operation_timeout_seconds=0.5,
                )
                return bool(released)
            except NewsPushDeliveryError as error:
                terminal_payload = {
                    "schema_version": PUSH_PAYLOAD_SCHEMA_VERSION,
                    "story_id": story_id,
                    "terminal_error": error.code,
                }
                terminalized = await self.db.run_business(
                    "news_story_push_render_failure",
                    self._render_failure_sync,
                    story_id,
                    lease_token,
                    "not_needed",
                    terminal_payload,
                    _payload_fingerprint(terminal_payload),
                    error.code,
                    _now_ms(),
                    operation_timeout_seconds=1.0,
                )
                return bool(terminalized)
            prepared_at_ms = _now_ms()
            prepared_eligibility = await self._current_eligibility(
                selected_item_id=str(claim["selected_item_id"]),
                baseline_at_ms=push_baseline_at_ms,
                now_ms=prepared_at_ms,
            )
            if not prepared_eligibility.eligible:
                if translation_attempted_at_ms is not None:
                    suppressed = await self.db.run_business(
                        "news_story_push_suppress_prepared_stale",
                        self._suppress_prepared_sync,
                        story_id,
                        lease_token,
                        prepared.translation_status,
                        delivery_payload,
                        payload_fingerprint,
                        prepared_at_ms,
                        operation_timeout_seconds=1.0,
                    )
                else:
                    suppressed = await self.db.run_business(
                        "news_story_push_suppress_stale",
                        self._suppress_claimed_sync,
                        story_id,
                        lease_token,
                        prepared_at_ms,
                        operation_timeout_seconds=1.0,
                    )
                return bool(suppressed)
            claim = await self.db.run_business(
                "news_story_push_freeze_payload",
                self._freeze_payload_sync,
                story_id,
                lease_token,
                prepared.translation_status,
                delivery_payload,
                payload_fingerprint,
                prepared_at_ms,
                operation_timeout_seconds=1.0,
            )
            if claim is None:
                return False

        delivery_payload = dict(claim.get("delivery_payload") or {})
        payload_fingerprint = str(claim.get("payload_fingerprint") or "")
        attempt = await self.db.run_business(
            "news_story_push_start_delivery",
            self._start_delivery_sync,
            story_id,
            lease_token,
            _now_ms(),
            operation_timeout_seconds=0.5,
        )
        if attempt is None:
            return False
        attempt_count = int(attempt["delivery_attempts"])

        if not delivery_payload or _payload_fingerprint(delivery_payload) != payload_fingerprint:
            await self._record_delivery_failure(
                story_id=story_id,
                lease_token=lease_token,
                attempt_count=attempt_count,
                error=NewsPushDeliveryError(
                    "news_story_push_payload_fingerprint_mismatch",
                    retryable=False,
                ),
            )
            return True

        submitted = False

        def mark_submitted() -> None:
            nonlocal submitted
            submitted = True

        async def suppress_if_stale_before_submit() -> None:
            submitted_at_ms = _now_ms()
            submission_eligibility = await self._current_eligibility(
                selected_item_id=str(claim["selected_item_id"]),
                baseline_at_ms=push_baseline_at_ms,
                now_ms=submitted_at_ms,
            )
            if submission_eligibility.eligible:
                return
            suppressed = await self.db.run_business(
                "news_story_push_suppress_unsubmitted_stale",
                self._suppress_unsubmitted_sync,
                story_id,
                lease_token,
                submitted_at_ms,
                operation_timeout_seconds=0.5,
            )
            if not suppressed:
                raise RuntimeError("news_story_push_stale_suppression_lost")
            raise _NewsPushDeliverySuppressed

        try:
            receipt = await self.finite_operations.run(
                "news_story_push_delivery",
                self.delivery.deliver,
                delivery_payload,
                idempotency_key=story_id,
                timeout_seconds=_DELIVERY_TIMEOUT_SECONDS,
                before_submit=suppress_if_stale_before_submit,
                on_submitted=mark_submitted,
            )
            if not isinstance(receipt, NewsPushReceipt) or not receipt.provider.strip():
                raise NewsPushDeliveryError(
                    "news_story_push_receipt_invalid",
                    retryable=False,
                )
        except _NewsPushDeliverySuppressed:
            return True
        except asyncio.CancelledError:
            if not submitted:
                await asyncio.shield(
                    self.db.run_business(
                        "news_story_push_release_delivery",
                        self._release_delivery_sync,
                        story_id,
                        lease_token,
                        _now_ms(),
                        operation_timeout_seconds=0.5,
                    )
                )
            raise
        except ResourceAdmissionTimeout:
            if submitted:
                raise RuntimeError("news_story_push_delivery_admission_after_submit") from None
            released = await self.db.run_business(
                "news_story_push_release_delivery",
                self._release_delivery_sync,
                story_id,
                lease_token,
                _now_ms(),
                operation_timeout_seconds=0.5,
            )
            return bool(released)
        except ResourceOperationOverrun as exc:
            if exc.capability is not ResourceCapability.FINITE_OPERATION:
                raise
            await self._record_delivery_failure(
                story_id=story_id,
                lease_token=lease_token,
                attempt_count=attempt_count,
                error=NewsPushDeliveryError(
                    "news_story_push_delivery_timeout",
                    retryable=True,
                ),
            )
            return True
        except NewsPushDeliveryError as error:
            await self._record_delivery_failure(
                story_id=story_id,
                lease_token=lease_token,
                attempt_count=attempt_count,
                error=error,
            )
            return True

        completed = await self.db.run_business(
            "news_story_push_complete",
            self._complete_sync,
            story_id,
            lease_token,
            _receipt_payload(receipt),
            _now_ms(),
            operation_timeout_seconds=1.0,
        )
        return bool(completed)

    async def _current_eligibility(
        self,
        *,
        selected_item_id: str,
        baseline_at_ms: int | None,
        now_ms: int,
    ) -> NewsPushEligibility:
        evidence = await self.db.run_business(
            "news_story_push_current_eligibility",
            self._current_eligibility_sync,
            selected_item_id,
            operation_timeout_seconds=0.5,
        )
        return evaluate_news_push_eligibility(
            evidence if isinstance(evidence, Mapping) else None,
            enabled=True,
            baseline_at_ms=baseline_at_ms,
            now_ms=now_ms,
        )

    async def _record_delivery_failure(
        self,
        *,
        story_id: str,
        lease_token: str,
        attempt_count: int,
        error: NewsPushDeliveryError,
    ) -> str | None:
        now_ms = _now_ms()
        return cast(
            str | None,
            await self.db.run_business(
                "news_story_push_fail",
                self._fail_sync,
                story_id,
                lease_token,
                error.code,
                error.retryable,
                now_ms + _retry_delay_ms(attempt_count),
                now_ms,
                operation_timeout_seconds=1.0,
            ),
        )

    def _reconcile_sync(self, now_ms: int) -> dict[str, int]:
        with self.db.worker_session("news_story_push_reconcile", 3.0) as repos, repos.transaction():
            repos.news.release_interrupted_push_translation_claims(
                active_lease_owner=self.lease_owner,
                now_ms=now_ms,
            )
            baseline_at_ms, _initialized = repos.news.initialize_push_baseline(now_ms=now_ms)
            inserted = 0
            suppressed = 0
            page_due, cycle_started_at_ms = repos.news.push_reconcile_page_schedule(
                now_ms=now_ms,
                minimum_cycle_ms=self.reconcile_cycle_ms,
            )
            if page_due:
                candidates, next_cursor = repos.news.story_push_reconcile_page()
                for candidate in candidates.values():
                    # The evidence query resolves the same Story/Item identity fences
                    # enforced by insert_push_candidate. Avoid issuing a guaranteed
                    # no-op INSERT for ledgered Stories on every reconcile turn.
                    if candidate.get("push_delivery_status") is not None:
                        continue
                    raw_evidence = candidate.get("provider_evidence")
                    if not isinstance(raw_evidence, Mapping):
                        continue
                    evidence = dict(raw_evidence)
                    eligibility = evaluate_news_push_eligibility(
                        evidence,
                        enabled=True,
                        baseline_at_ms=baseline_at_ms,
                        now_ms=now_ms,
                    )
                    if eligibility.ineligible_reason in {
                        "score_threshold",
                        "no_asset",
                        "cl_family_only",
                    }:
                        continue
                    score = float(evidence["provider_score"])
                    # Push is a live alert, not a recovery backfill. Suppress both
                    # the enablement snapshot and any provider score that arrives
                    # later for an old Item (for example through REST recovery).
                    should_suppress = not eligibility.eligible
                    created = repos.news.insert_push_candidate(
                        story_id=str(candidate["story_id"]),
                        selected_item_id=str(evidence["item_id"]),
                        provider_score=score,
                        threshold_observed_at_ms=int(evidence["threshold_observed_at_ms"]),
                        source_payload=_source_payload(candidate),
                        suppressed=should_suppress,
                        now_ms=now_ms,
                    )
                    inserted += int(created)
                    suppressed += int(created and should_suppress)
                repos.news.advance_push_reconcile_cursor(
                    story_id=next_cursor,
                    cycle_started_at_ms=cycle_started_at_ms,
                    now_ms=now_ms,
                )
            terminalized = repos.news.terminalize_exhausted_push_deliveries(
                now_ms=now_ms,
                max_attempts=_DELIVERY_MAX_ATTEMPTS,
            )
        return {
            "inserted": inserted,
            "suppressed": suppressed,
            "terminalized": terminalized,
        }

    def _current_eligibility_sync(self, selected_item_id: str) -> dict[str, Any] | None:
        with self.db.worker_session("news_story_push_current_eligibility", 0.5) as repos:
            return cast(
                dict[str, Any] | None,
                repos.news.current_push_eligibility_evidence(
                    selected_item_id=selected_item_id,
                ),
            )

    def _peek_sync(self, now_ms: int) -> dict[str, Any] | None:
        with self.db.worker_session("news_story_push_peek", 0.5) as repos:
            return cast(
                dict[str, Any] | None,
                repos.news.peek_push_delivery(
                    now_ms=now_ms,
                    max_attempts=_DELIVERY_MAX_ATTEMPTS,
                ),
            )

    def _claim_sync(
        self,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        with self.db.worker_session("news_story_push_claim", 0.5) as repos, repos.transaction():
            return cast(
                dict[str, Any] | None,
                repos.news.claim_push_delivery(
                    story_id=story_id,
                    now_ms=now_ms,
                    max_attempts=_DELIVERY_MAX_ATTEMPTS,
                    lease_owner=self.lease_owner,
                    lease_token=lease_token,
                    lease_expires_at_ms=now_ms + _LEASE_MS,
                ),
            )

    def _freeze_payload_sync(
        self,
        story_id: str,
        lease_token: str,
        translation_status: str,
        delivery_payload: Mapping[str, Any],
        payload_fingerprint: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        with self.db.worker_session("news_story_push_freeze_payload", 1.0) as repos, repos.transaction():
            return cast(
                dict[str, Any] | None,
                repos.news.freeze_push_delivery_payload(
                    story_id=story_id,
                    lease_token=lease_token,
                    translation_status=translation_status,
                    delivery_payload=delivery_payload,
                    payload_fingerprint=payload_fingerprint,
                    now_ms=now_ms,
                ),
            )

    def _fence_translation_sync(
        self,
        story_id: str,
        lease_token: str,
        attempted_at_ms: int,
    ) -> bool:
        with self.db.worker_session("news_story_push_fence_translation", 0.5) as repos, repos.transaction():
            return bool(
                repos.news.mark_push_translation_attempted(
                    story_id=story_id,
                    lease_token=lease_token,
                    attempted_at_ms=attempted_at_ms,
                )
            )

    def _suppress_claimed_sync(
        self,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        with self.db.worker_session("news_story_push_suppress_stale", 1.0) as repos, repos.transaction():
            return bool(
                repos.news.suppress_claimed_push_delivery(
                    story_id=story_id,
                    lease_token=lease_token,
                    now_ms=now_ms,
                )
            )

    def _suppress_prepared_sync(
        self,
        story_id: str,
        lease_token: str,
        translation_status: str,
        delivery_payload: Mapping[str, Any],
        payload_fingerprint: str,
        now_ms: int,
    ) -> bool:
        with self.db.worker_session("news_story_push_suppress_prepared_stale", 1.0) as repos, repos.transaction():
            return bool(
                repos.news.suppress_prepared_push_delivery(
                    story_id=story_id,
                    lease_token=lease_token,
                    translation_status=translation_status,
                    delivery_payload=delivery_payload,
                    payload_fingerprint=payload_fingerprint,
                    now_ms=now_ms,
                )
            )

    def _suppress_unsubmitted_sync(
        self,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        with self.db.worker_session("news_story_push_suppress_unsubmitted_stale", 0.5) as repos, repos.transaction():
            return bool(
                repos.news.suppress_unsubmitted_push_delivery(
                    story_id=story_id,
                    lease_token=lease_token,
                    now_ms=now_ms,
                )
            )

    def _render_failure_sync(
        self,
        story_id: str,
        lease_token: str,
        translation_status: str,
        delivery_payload: Mapping[str, Any],
        payload_fingerprint: str,
        error_code: str,
        now_ms: int,
    ) -> bool:
        with self.db.worker_session("news_story_push_render_failure", 1.0) as repos, repos.transaction():
            return bool(
                repos.news.record_push_render_failure(
                    story_id=story_id,
                    lease_token=lease_token,
                    translation_status=translation_status,
                    delivery_payload=delivery_payload,
                    payload_fingerprint=payload_fingerprint,
                    error_code=error_code,
                    now_ms=now_ms,
                )
            )

    def _release_preparation_sync(
        self,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        with self.db.worker_session("news_story_push_release_preparation", 0.5) as repos, repos.transaction():
            return bool(
                repos.news.release_push_preparation_claim(
                    story_id=story_id,
                    lease_token=lease_token,
                    now_ms=now_ms,
                )
            )

    def _start_delivery_sync(
        self,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        with self.db.worker_session("news_story_push_start_delivery", 0.5) as repos, repos.transaction():
            return cast(
                dict[str, Any] | None,
                repos.news.start_push_delivery_attempt(
                    story_id=story_id,
                    lease_token=lease_token,
                    now_ms=now_ms,
                ),
            )

    def _complete_sync(
        self,
        story_id: str,
        lease_token: str,
        receipt: Mapping[str, Any],
        now_ms: int,
    ) -> bool:
        with self.db.worker_session("news_story_push_complete", 1.0) as repos, repos.transaction():
            return bool(
                repos.news.complete_push_delivery(
                    story_id=story_id,
                    lease_token=lease_token,
                    receipt=receipt,
                    now_ms=now_ms,
                )
            )

    def _release_delivery_sync(
        self,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        with self.db.worker_session("news_story_push_release_delivery", 0.5) as repos, repos.transaction():
            return bool(
                repos.news.release_push_delivery_claim(
                    story_id=story_id,
                    lease_token=lease_token,
                    now_ms=now_ms,
                )
            )

    def _fail_sync(
        self,
        story_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        next_attempt_at_ms: int,
        now_ms: int,
    ) -> str | None:
        with self.db.worker_session("news_story_push_fail", 1.0) as repos, repos.transaction():
            return cast(
                str | None,
                repos.news.fail_push_delivery(
                    story_id=story_id,
                    lease_token=lease_token,
                    error_code=error_code,
                    retryable=retryable,
                    next_attempt_at_ms=next_attempt_at_ms,
                    max_attempts=_DELIVERY_MAX_ATTEMPTS,
                    now_ms=now_ms,
                ),
            )


def _source_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    evidence = dict(candidate["provider_evidence"])
    metadata = dict(evidence["provider_metadata"])
    return {
        "schema_version": PUSH_PAYLOAD_SCHEMA_VERSION,
        "story_id": str(candidate["story_id"]),
        "provider_evidence": {
            "item_id": str(evidence["item_id"]),
            "url": evidence["url"],
            "provider_metadata": metadata,
            "reporting_origin": str(evidence["reporting_origin"]),
            "title": str(evidence["title"]),
            "description": str(evidence["description"]),
            "lang": str(evidence["lang"]),
            "published_at_ms": int(evidence["published_at_ms"]),
            "provider_score": _json_number(float(evidence["provider_score"])),
        },
        "tracefold_story": {
            "importance_score": int(candidate["importance_score"]),
            "item_count": int(candidate["item_count"]),
            "source_count": int(candidate["source_count"]),
            "first_published_at_ms": int(candidate["first_published_at_ms"]),
            "last_published_at_ms": int(candidate["last_published_at_ms"]),
        },
    }


def _receipt_payload(receipt: NewsPushReceipt) -> dict[str, Any]:
    return {
        "provider": receipt.provider,
        "receipt_id": receipt.receipt_id,
        "details": dict(receipt.details),
    }


def _payload_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _retry_delay_ms(attempt_count: int) -> int:
    index = max(0, min(int(attempt_count) - 1, len(_DELIVERY_RETRY_DELAYS_MS) - 1))
    return _DELIVERY_RETRY_DELAYS_MS[index]


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "PUSH_PAYLOAD_SCHEMA_VERSION",
    "PUSH_PROVIDER_SCORE_THRESHOLD",
    "PUSH_SOURCE_FRESHNESS_MS",
    "NewsPushDelivery",
    "NewsPushDeliveryError",
    "NewsPushReceipt",
    "NewsStoryPush",
    "PreparedNewsPush",
]
