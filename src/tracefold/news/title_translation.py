from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import uuid4

from tracefold.platform.model_candidate import ModelCandidate
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

TITLE_TRANSLATION_LOCALE = "zh-CN"
TITLE_TRANSLATION_WORKFLOW_VERSION = "news_story_display_title_zh_v1"
TITLE_TRANSLATION_PROMPT_VERSION = "title_zh_v3"
TITLE_TRANSLATION_MAX_ATTEMPTS = 3
TITLE_TRANSLATION_LEASE_MS = 30_000
TITLE_TRANSLATION_MODEL_TIMEOUT_SECONDS = 8.0
TITLE_TRANSLATION_RETRY_DELAYS_MS = (30_000, 120_000)
TITLE_TRANSLATION_RETENTION_MS = 48 * 60 * 60 * 1_000
TITLE_TRANSLATION_RECONCILE_INTERVAL_MS = 5_000


class NewsTitleTranslationExpectedError(RuntimeError):
    """A bounded provider or validation failure safe to persist by code."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        normalized = str(code or "").strip().lower()
        if not normalized:
            normalized = "news_title_translation_error"
        self.code = normalized[:120]
        self.retryable = bool(retryable)
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class NewsTitleTranslationResult:
    title_zh: str
    provider: str
    model: str


class NewsTitleTranslator(Protocol):
    def translate(self, source_title: str) -> NewsTitleTranslationResult: ...

    def close(self) -> None: ...


def story_title_fingerprint(source_title: str) -> str:
    return hashlib.sha256(str(source_title).encode("utf-8")).hexdigest()


def looks_zh_cn_title(value: str) -> bool:
    has_han = False
    for character in str(value):
        codepoint = ord(character)
        if (
            0x3040 <= codepoint <= 0x30FF
            or 0x31F0 <= codepoint <= 0x31FF
            or 0xFF66 <= codepoint <= 0xFF9F
            or 0x1100 <= codepoint <= 0x11FF
            or 0x3130 <= codepoint <= 0x318F
            or 0xAC00 <= codepoint <= 0xD7AF
        ):
            return False
        has_han = has_han or (
            0x3400 <= codepoint <= 0x4DBF or 0x4E00 <= codepoint <= 0x9FFF or 0xF900 <= codepoint <= 0xFAFF
        )
    return has_han


class NewsStoryTitleTranslationCandidate:
    """One durable Story-display-title candidate for the serial model arbiter.

    The candidate owns target reconciliation, claims, retry classification and
    exact-binding publication. PostgreSQL and the translator are internal seams;
    callers learn only the standard native-model ``peek``/``execute`` interface.
    """

    def __init__(
        self,
        *,
        db: Any,
        model_adapter: Any,
        translator: NewsTitleTranslator | None,
        runtime_id: str,
        stable_order: int = 25,
    ) -> None:
        self.db = db
        self.model_adapter = model_adapter
        self.translator = translator
        self.lease_owner = f"news_story_title_translation:{runtime_id}"
        self.stable_order = int(stable_order)
        self._next_reconcile_at_ms = 0

    async def peek(self, *, now_ms: int) -> ModelCandidate | None:
        try:
            target = await self.db.run_business(
                "news_title_translation_peek",
                self._peek_sync,
                int(now_ms),
                operation_timeout_seconds=1.0,
            )
        except ResourceAdmissionTimeout:
            return None
        if target is None:
            return None
        # Never export an old backlog timestamp to the process-wide arbiter.
        # Existing Brief/Macro work can therefore win by deadline/stable order.
        return ModelCandidate(
            kind="news_story_title_translation",
            target_key=_target_key(
                str(target["story_id"]),
                str(target["source_title_fingerprint"]),
            ),
            due_at_ms=int(now_ms),
            stable_order=self.stable_order,
        )

    async def execute(self, candidate: ModelCandidate) -> bool:
        story_id, source_title_fingerprint = _parse_target_key(candidate.target_key)
        try:
            claim = await self.db.run_business(
                "news_title_translation_claim",
                self._claim_sync,
                story_id,
                source_title_fingerprint,
                operation_timeout_seconds=0.5,
            )
        except ResourceAdmissionTimeout:
            return False
        if claim is None:
            return False
        translator = self.translator
        if translator is None:
            # Reconciliation normally materializes ``unavailable`` directly.
            # This fence covers a configuration transition between peek/claim.
            return await self._fail_claim(
                claim,
                NewsTitleTranslationExpectedError(
                    "news_title_translation_not_configured",
                    retryable=False,
                ),
            )
        try:
            result = await self.model_adapter.run(
                "news_story_title_translation",
                translator.translate,
                str(claim["source_title"]),
                timeout_seconds=TITLE_TRANSLATION_MODEL_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            # The committed lease/attempt is recovered conservatively after
            # restart. Releasing it here could submit the provider call twice.
            raise
        except ResourceOperationOverrun:
            return await self._fail_claim(
                claim,
                NewsTitleTranslationExpectedError(
                    "news_title_translation_timeout",
                    retryable=True,
                ),
            )
        except NewsTitleTranslationExpectedError as error:
            return await self._fail_claim(claim, error)
        if not isinstance(result, NewsTitleTranslationResult):
            raise TypeError("news_title_translation_result_invalid")
        try:
            completed = await self.db.run_business(
                "news_title_translation_complete",
                self._complete_sync,
                claim,
                result,
                operation_timeout_seconds=1.0,
            )
        except ResourceAdmissionTimeout:
            return False
        return bool(completed)

    async def close(self) -> None:
        if self.translator is None:
            return
        await self.model_adapter.run(
            "news_title_translation_client_close",
            self.translator.close,
            timeout_seconds=5.0,
            allow_shutdown=True,
        )

    async def _fail_claim(
        self,
        claim: dict[str, Any],
        error: NewsTitleTranslationExpectedError,
    ) -> bool:
        try:
            failed = await self.db.run_business(
                "news_title_translation_fail",
                self._fail_sync,
                claim,
                error,
                operation_timeout_seconds=1.0,
            )
        except ResourceAdmissionTimeout:
            return False
        return bool(failed)

    def _peek_sync(self, now_ms: int) -> dict[str, Any] | None:
        with self.db.worker_session("news_title_translation_peek", 1.0) as repos, repos.transaction():
            if int(now_ms) >= self._next_reconcile_at_ms:
                repos.news.reconcile_story_title_translation_targets(
                    now_ms=int(now_ms),
                    configured=self.translator is not None,
                    locale=TITLE_TRANSLATION_LOCALE,
                    workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                    prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
                    max_attempts=TITLE_TRANSLATION_MAX_ATTEMPTS,
                    retry_delays_ms=TITLE_TRANSLATION_RETRY_DELAYS_MS,
                    retention_ms=TITLE_TRANSLATION_RETENTION_MS,
                )
                self._next_reconcile_at_ms = int(now_ms) + TITLE_TRANSLATION_RECONCILE_INTERVAL_MS
            return cast(
                dict[str, Any] | None,
                repos.news.peek_story_title_translation_target(
                    now_ms=int(now_ms),
                    locale=TITLE_TRANSLATION_LOCALE,
                    workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                    prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
                ),
            )

    def _claim_sync(
        self,
        story_id: str,
        source_title_fingerprint: str,
    ) -> dict[str, Any] | None:
        now_ms = _now_ms()
        with self.db.worker_session("news_title_translation_claim", 0.5) as repos, repos.transaction():
            return cast(
                dict[str, Any] | None,
                repos.news.claim_story_title_translation(
                    story_id=story_id,
                    source_title_fingerprint=source_title_fingerprint,
                    locale=TITLE_TRANSLATION_LOCALE,
                    workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                    prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
                    lease_owner=self.lease_owner,
                    lease_token=str(uuid4()),
                    lease_expires_at_ms=now_ms + TITLE_TRANSLATION_LEASE_MS,
                    now_ms=now_ms,
                    max_attempts=TITLE_TRANSLATION_MAX_ATTEMPTS,
                ),
            )

    def _complete_sync(
        self,
        claim: dict[str, Any],
        result: NewsTitleTranslationResult,
    ) -> bool:
        with self.db.worker_session("news_title_translation_complete", 1.0) as repos, repos.transaction():
            return bool(
                repos.news.complete_story_title_translation(
                    story_id=str(claim["story_id"]),
                    source_title_fingerprint=str(claim["source_title_fingerprint"]),
                    locale=TITLE_TRANSLATION_LOCALE,
                    workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                    prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
                    lease_owner=str(claim["lease_owner"]),
                    lease_token=str(claim["lease_token"]),
                    title_zh=result.title_zh,
                    provider=result.provider,
                    model=result.model,
                    now_ms=_now_ms(),
                )
            )

    def _fail_sync(
        self,
        claim: dict[str, Any],
        error: NewsTitleTranslationExpectedError,
    ) -> bool:
        with self.db.worker_session("news_title_translation_fail", 1.0) as repos, repos.transaction():
            return bool(
                repos.news.fail_story_title_translation(
                    story_id=str(claim["story_id"]),
                    source_title_fingerprint=str(claim["source_title_fingerprint"]),
                    locale=TITLE_TRANSLATION_LOCALE,
                    workflow_version=TITLE_TRANSLATION_WORKFLOW_VERSION,
                    prompt_version=TITLE_TRANSLATION_PROMPT_VERSION,
                    lease_owner=str(claim["lease_owner"]),
                    lease_token=str(claim["lease_token"]),
                    error_code=error.code,
                    retryable=error.retryable,
                    retry_delays_ms=TITLE_TRANSLATION_RETRY_DELAYS_MS,
                    max_attempts=TITLE_TRANSLATION_MAX_ATTEMPTS,
                    now_ms=_now_ms(),
                )
            )


def _target_key(story_id: str, source_title_fingerprint: str) -> str:
    if len(story_id) != 64 or len(source_title_fingerprint) != 64:
        raise ValueError("news_title_translation_target_invalid")
    return f"{story_id}:{source_title_fingerprint}"


def _parse_target_key(value: str) -> tuple[str, str]:
    parts = str(value).split(":")
    if len(parts) != 2 or any(len(part) != 64 for part in parts):
        raise ValueError("news_title_translation_target_invalid")
    return parts[0], parts[1]


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = [
    "TITLE_TRANSLATION_LOCALE",
    "TITLE_TRANSLATION_MAX_ATTEMPTS",
    "TITLE_TRANSLATION_PROMPT_VERSION",
    "TITLE_TRANSLATION_WORKFLOW_VERSION",
    "NewsStoryTitleTranslationCandidate",
    "NewsTitleTranslationExpectedError",
    "NewsTitleTranslationResult",
    "NewsTitleTranslator",
    "looks_zh_cn_title",
    "story_title_fingerprint",
]
