from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from tracefold.platform.model_candidate import ModelCandidate
from tracefold.platform.resource import ResourceAdmissionTimeout

PUSH_PAYLOAD_SCHEMA_VERSION = "news_story_push_v1"
PUSH_PROVIDER_SCORE_THRESHOLD = 70.0

_DELIVERY_MAX_ATTEMPTS = 6
_DELIVERY_RETRY_DELAYS_MS = (5_000, 30_000, 120_000, 600_000, 1_800_000)
_LEASE_MS = 60_000
_TRANSLATION_TIMEOUT_SECONDS = 20.0
_DELIVERY_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True, slots=True)
class NewsPushTranslation:
    title_zh: str
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class NewsPushReceipt:
    provider: str
    receipt_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


class NewsPushTranslator(Protocol):
    """News-side bridge run only through the process ModelAdapter.

    The composition root adapts the raw DeepSeek client to this contract and
    maps its sanitized failures to ``NewsPushTranslationError``.
    """

    def translate_title(self, title: str) -> NewsPushTranslation: ...

    def close(self) -> None: ...


class NewsPushDelivery(Protocol):
    """News-side bridge run only through FiniteOperations.

    ``render`` is pure and is called exactly once before its opaque result is
    hashed and persisted. ``deliver`` receives that frozen result on every
    retry. The composition root owns Feishu card JSON and maps raw client
    failures to ``NewsPushDeliveryError``.
    """

    def render(
        self,
        source_payload: Mapping[str, Any],
        translation: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def deliver(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> NewsPushReceipt: ...

    def close(self) -> None: ...


class NewsPushTranslationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code)[:500]
        super().__init__(self.code)


class NewsPushDeliveryError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = str(code)[:500]
        self.retryable = bool(retryable)
        super().__init__(self.code)


class NewsStoryPush:
    """Durable Story-scoped push state machine.

    PostgreSQL calls are short phases around translation and delivery. External
    I/O never runs inside a transaction or on the event loop: translation uses
    the serial ModelAdapter, delivery uses FiniteOperations, and only the pure
    one-time card renderer runs inline before its output is frozen.
    """

    def __init__(
        self,
        *,
        db: Any,
        model_adapter: Any,
        finite_operations: Any,
        translator: NewsPushTranslator,
        delivery: NewsPushDelivery,
        runtime_id: str,
        stable_order: int = 10,
    ) -> None:
        self.db = db
        self.model_adapter = model_adapter
        self.finite_operations = finite_operations
        self.translator = translator
        self.delivery = delivery
        self.lease_owner = f"news_story_push:{runtime_id}"
        self.stable_order = int(stable_order)

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

    async def peek(self, *, now_ms: int) -> ModelCandidate | None:
        row = await self.db.run_business(
            "news_story_push_peek",
            self._peek_sync,
            int(now_ms),
            operation_timeout_seconds=3.0,
        )
        if row is None:
            return None
        return ModelCandidate(
            kind="news_story_push",
            target_key=str(row["story_id"]),
            due_at_ms=int(row["next_attempt_at_ms"]),
            stable_order=self.stable_order,
        )

    async def health_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.db.run_business(
                "news_story_push_health",
                self._health_sync,
                int(now_ms),
                operation_timeout_seconds=0.5,
            ),
        )

    async def close(self) -> None:
        """Close both synchronous adapters through their owning capabilities."""

        try:
            await self.model_adapter.run(
                "news_story_push_translator_close",
                self.translator.close,
                timeout_seconds=5.0,
                allow_shutdown=True,
            )
        finally:
            await self.finite_operations.run(
                "news_story_push_delivery_close",
                self.delivery.close,
                timeout_seconds=5.0,
                allow_shutdown=True,
            )

    async def execute(self, candidate: ModelCandidate) -> bool:
        if candidate.kind != "news_story_push":
            raise ValueError("news_story_push_candidate_kind_invalid")
        now_ms = _now_ms()
        lease_token = secrets.token_hex(16)
        claim = await self.db.run_business(
            "news_story_push_claim",
            self._claim_sync,
            candidate.target_key,
            lease_token,
            now_ms,
            operation_timeout_seconds=0.5,
        )
        if claim is None:
            return False

        if str(claim["translation_status"]) == "pending":
            translation = await self._translate(claim)
            source_payload = dict(claim["source_payload"])
            try:
                rendered = self.delivery.render(source_payload, translation)
                if not isinstance(rendered, Mapping) or not rendered:
                    raise NewsPushDeliveryError(
                        "news_story_push_render_result_invalid",
                        retryable=False,
                    )
                delivery_payload = dict(rendered)
                try:
                    payload_fingerprint = _payload_fingerprint(delivery_payload)
                except (TypeError, ValueError):
                    raise NewsPushDeliveryError(
                        "news_story_push_render_result_invalid",
                        retryable=False,
                    ) from None
            except NewsPushDeliveryError as error:
                terminal_payload = {
                    "schema_version": PUSH_PAYLOAD_SCHEMA_VERSION,
                    "story_id": candidate.target_key,
                    "translation": dict(translation),
                    "terminal_error": error.code,
                }
                terminalized = await self.db.run_business(
                    "news_story_push_render_failure",
                    self._render_failure_sync,
                    candidate.target_key,
                    lease_token,
                    translation["status"],
                    terminal_payload,
                    _payload_fingerprint(terminal_payload),
                    error.code,
                    _now_ms(),
                    operation_timeout_seconds=1.0,
                )
                return bool(terminalized)
            claim = await self.db.run_business(
                "news_story_push_freeze_payload",
                self._freeze_payload_sync,
                candidate.target_key,
                lease_token,
                translation["status"],
                delivery_payload,
                payload_fingerprint,
                _now_ms(),
                operation_timeout_seconds=1.0,
            )
            if claim is None:
                return False

        delivery_payload = dict(claim.get("delivery_payload") or {})
        payload_fingerprint = str(claim.get("payload_fingerprint") or "")
        attempt = await self.db.run_business(
            "news_story_push_start_delivery",
            self._start_delivery_sync,
            candidate.target_key,
            lease_token,
            _now_ms(),
            operation_timeout_seconds=0.5,
        )
        if attempt is None:
            return False
        attempt_count = int(attempt["delivery_attempts"])

        if not delivery_payload or _payload_fingerprint(delivery_payload) != payload_fingerprint:
            await self._record_delivery_failure(
                story_id=candidate.target_key,
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

        try:
            receipt = await self.finite_operations.run(
                "news_story_push_delivery",
                self.delivery.deliver,
                delivery_payload,
                idempotency_key=candidate.target_key,
                timeout_seconds=_DELIVERY_TIMEOUT_SECONDS,
                on_submitted=mark_submitted,
            )
            if not isinstance(receipt, NewsPushReceipt) or not receipt.provider.strip():
                raise NewsPushDeliveryError(
                    "news_story_push_receipt_invalid",
                    retryable=False,
                )
        except asyncio.CancelledError:
            if not submitted:
                await asyncio.shield(
                    self.db.run_business(
                        "news_story_push_release_delivery",
                        self._release_delivery_sync,
                        candidate.target_key,
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
                candidate.target_key,
                lease_token,
                _now_ms(),
                operation_timeout_seconds=0.5,
            )
            return bool(released)
        except NewsPushDeliveryError as error:
            await self._record_delivery_failure(
                story_id=candidate.target_key,
                lease_token=lease_token,
                attempt_count=attempt_count,
                error=error,
            )
            return True

        completed = await self.db.run_business(
            "news_story_push_complete",
            self._complete_sync,
            candidate.target_key,
            lease_token,
            _receipt_payload(receipt),
            _now_ms(),
            operation_timeout_seconds=1.0,
        )
        return bool(completed)

    async def _translate(self, claim: Mapping[str, Any]) -> dict[str, Any]:
        source_payload = dict(claim["source_payload"])
        provider_evidence = dict(source_payload["provider_evidence"])
        title = str(provider_evidence["title"])
        if _looks_chinese_original(title):
            return {
                "status": "not_needed",
                "title_zh": title,
                "provider": None,
                "model": None,
                "error_code": None,
            }
        try:
            result = await self.model_adapter.run(
                "news_story_push_translation",
                self.translator.translate_title,
                title,
                timeout_seconds=_TRANSLATION_TIMEOUT_SECONDS,
            )
            if not isinstance(result, NewsPushTranslation):
                raise NewsPushTranslationError("news_story_push_translation_result_invalid")
            translated = result.title_zh.strip()
            if not translated or not _contains_han(translated):
                raise NewsPushTranslationError("news_story_push_translation_title_invalid")
            return {
                "status": "translated",
                "title_zh": translated,
                "provider": result.provider,
                "model": result.model,
                "error_code": None,
            }
        except asyncio.CancelledError:
            raise
        except ResourceAdmissionTimeout:
            return {
                "status": "unavailable",
                "title_zh": None,
                "provider": None,
                "model": None,
                "error_code": "news_story_push_translation_admission_timeout",
            }
        except NewsPushTranslationError as error:
            return {
                "status": "unavailable",
                "title_zh": None,
                "provider": None,
                "model": None,
                "error_code": error.code,
            }

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
            _baseline_at_ms, initialized = repos.news.initialize_push_baseline(now_ms=now_ms)
            candidates = repos.news.story_provider_evidence()
            inserted = 0
            suppressed = 0
            for candidate in candidates.values():
                evidence = dict(candidate["provider_evidence"])
                score = float(evidence["provider_score"])
                if score <= PUSH_PROVIDER_SCORE_THRESHOLD:
                    continue
                # Only the exact current eligible set observed during the first
                # enablement turn is baseline-suppressed. A later unseen
                # ``story_id`` is a new current Story even when its selected
                # Item predates the baseline (for example after the earliest
                # cluster member expires and canonical identity changes).
                should_suppress = initialized
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
            terminalized = repos.news.terminalize_exhausted_push_deliveries(
                now_ms=now_ms,
                max_attempts=_DELIVERY_MAX_ATTEMPTS,
            )
        return {
            "inserted": inserted,
            "suppressed": suppressed,
            "terminalized": terminalized,
        }

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

    def _health_sync(self, now_ms: int) -> dict[str, Any]:
        with self.db.worker_session("news_story_push_health", 0.5) as repos:
            return cast(
                dict[str, Any],
                repos.news.push_health_snapshot(now_ms=now_ms),
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
                repos.news.record_push_translation(
                    story_id=story_id,
                    lease_token=lease_token,
                    translation_status=translation_status,
                    delivery_payload=delivery_payload,
                    payload_fingerprint=payload_fingerprint,
                    now_ms=now_ms,
                ),
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


def _looks_chinese_original(value: str) -> bool:
    """Conservatively recognize Chinese while rejecting Japanese/Korean scripts.

    Han characters alone are shared across languages. The provider does not
    supply a trustworthy per-item language, so explicit Hiragana, Katakana, or
    Hangul always wins and requires translation; otherwise a Han-bearing title
    is preserved as Chinese.
    """

    for character in value:
        codepoint = ord(character)
        if (
            0x3040 <= codepoint <= 0x30FF  # Hiragana and Katakana
            or 0x31F0 <= codepoint <= 0x31FF  # Katakana phonetic extensions
            or 0xFF66 <= codepoint <= 0xFF9F  # half-width Katakana
            or 0x1100 <= codepoint <= 0x11FF  # Hangul Jamo
            or 0x3130 <= codepoint <= 0x318F  # Hangul compatibility Jamo
            or 0xAC00 <= codepoint <= 0xD7AF  # Hangul syllables
        ):
            return False
    return _contains_han(value)


def _contains_han(value: str) -> bool:
    return any(
        0x3400 <= ord(character) <= 0x4DBF or 0x4E00 <= ord(character) <= 0x9FFF or 0xF900 <= ord(character) <= 0xFAFF
        for character in value
    )


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
    "NewsPushDelivery",
    "NewsPushDeliveryError",
    "NewsPushReceipt",
    "NewsPushTranslation",
    "NewsPushTranslationError",
    "NewsPushTranslator",
    "NewsStoryPush",
]
