"""Tracefold availability policy around the native DSPy News Program.

DSPy owns Predictor rendering, structured-output fallback and provider I/O.  This
module owns only the News business boundary: one route deadline, primary breaker,
full fallback restart, public error projection and durable audit aggregation.
"""

from __future__ import annotations

import asyncio
import math
import time
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from typing import Literal

import dspy  # type: ignore[import-untyped]

from ..artifact_identity import canonical_sha
from .contracts import (
    ProgramCallTrace,
    ProgramTrace,
    ProgramUsage,
    SemanticJudgeError,
    SemanticJudgment,
    TriageContext,
    aggregate_program_usage,
)
from .identity import EXECUTION_ENVELOPE_SHA256
from .lm import AuditedConfiguredLM, LMCallContext, LMCallLedger, LMOutputTruncatedError
from .module import NativeNewsProgram, NativeProgramResult
from .runtime import (
    PROGRAM_JUDGMENT_MAX_CALLS,
    PROGRAM_PREDICTOR_MAX_CALLS,
    PROGRAM_PRIMARY_BREAKER_FAILURES,
    PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS,
    PROGRAM_RETRYABLE_LM_ERROR_TYPES,
    PROGRAM_ROUTE_DEADLINE_SECONDS,
    PROGRAM_ROUTE_MAX_CALLS,
    PROGRAM_VERSION,
)

RouteName = Literal["primary", "fallback"]


@dataclass(frozen=True, slots=True)
class RouteLMs:
    """The two explicit model slots used by one complete Program route."""

    event_semantics: dspy.BaseLM
    reader_card: dspy.BaseLM


@dataclass(frozen=True, slots=True)
class _RouteAnswer:
    result: NativeProgramResult
    calls: tuple[ProgramCallTrace, ...]


class _RouteFailure(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        output_failure: bool,
        calls: tuple[ProgramCallTrace, ...],
        finish_reason: str | None = None,
        failing_predictor: str | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.output_failure = output_failure
        self.calls = calls
        self.finish_reason = finish_reason
        self.failing_predictor = failing_predictor
        super().__init__(code)


_RETRYABLE_LM_ERRORS = tuple(getattr(dspy, name) for name in PROGRAM_RETRYABLE_LM_ERROR_TYPES)


class RoutedSemanticJudge:
    """Framework-neutral SemanticJudge backed by one native DSPy Module."""

    def __init__(
        self,
        program: NativeNewsProgram,
        *,
        primary: RouteLMs,
        fallback: RouteLMs | None = None,
        route_deadline_seconds: float | None = PROGRAM_ROUTE_DEADLINE_SECONDS,
        primary_breaker_enabled: bool = True,
    ) -> None:
        if route_deadline_seconds is not None and (
            not math.isfinite(route_deadline_seconds) or route_deadline_seconds <= 0
        ):
            raise ValueError("news_program_route_deadline_invalid")
        self.program = program
        self.artifact = program.artifact
        self.primary = primary
        self.fallback = fallback
        self.route_deadline_seconds = route_deadline_seconds
        self.primary_breaker_enabled = primary_breaker_enabled
        self._validate_route(primary, "primary")
        if fallback is not None:
            self._validate_route(fallback, "fallback")
        self._primary_failures = 0
        self._primary_open_until = 0.0

    def _validate_route(self, lms: RouteLMs, route: RouteName) -> None:
        for predictor, lm in (
            ("event_semantics", lms.event_semantics),
            ("reader_card", lms.reader_card),
        ):
            if not isinstance(lm, AuditedConfiguredLM):
                raise TypeError("news_program_route_lm_invalid")
            declared_predictor = lm.predictor
            declared_route = lm.route
            expected_binding = getattr(getattr(self.artifact, predictor).model_bindings, route)
            declared_binding = lm.model_binding
            if (declared_predictor, declared_route, declared_binding) != (predictor, route, expected_binding):
                raise ValueError("news_program_route_lm_binding_mismatch")

    async def judge(self, context: TriageContext) -> SemanticJudgment:
        typed = context if isinstance(context, TriageContext) else TriageContext.model_validate(context)
        started = time.perf_counter()
        context_sha = canonical_sha(typed.model_dump(mode="json"))
        ledger = LMCallLedger(
            max_calls_per_predictor=PROGRAM_PREDICTOR_MAX_CALLS,
            max_calls_per_route=PROGRAM_ROUTE_MAX_CALLS,
            max_calls_per_scope=PROGRAM_JUDGMENT_MAX_CALLS,
        )
        calls: list[ProgramCallTrace] = []
        primary_failure: _RouteFailure | None = None

        if self.primary_breaker_enabled and time.monotonic() < self._primary_open_until:
            primary_failure = _RouteFailure(
                "primary_circuit_open",
                retryable=False,
                output_failure=False,
                calls=(),
                failing_predictor="event_semantics",
            )
        else:
            try:
                answer = await self._run_route(
                    "primary",
                    self.primary,
                    context=typed,
                    context_sha=context_sha,
                    ledger=ledger,
                )
            except _RouteFailure as exc:
                primary_failure = exc
                calls.extend(exc.calls)
                if self.primary_breaker_enabled and exc.retryable and not exc.output_failure:
                    self._record_primary_failure()
            else:
                calls.extend(answer.calls)
                self._primary_failures = 0
                self._primary_open_until = 0.0
                return self._judgment(
                    answer.result,
                    calls,
                    context_sha=context_sha,
                    route="primary",
                    fallback_from=None,
                    started=started,
                )

        if self.fallback is None:
            if primary_failure is None:  # pragma: no cover - guarded by the primary return above
                raise RuntimeError("news_program_primary_failure_missing")
            raise self._public_error(primary_failure, calls, context_sha=context_sha)
        try:
            answer = await self._run_route(
                "fallback",
                self.fallback,
                context=typed,
                context_sha=context_sha,
                ledger=ledger,
            )
        except _RouteFailure as fallback_failure:
            calls.extend(fallback_failure.calls)
            if primary_failure is None:  # pragma: no cover - guarded by the primary return above
                raise RuntimeError("news_program_primary_failure_missing") from fallback_failure
            raise self._public_error(
                fallback_failure,
                calls,
                context_sha=context_sha,
                primary_failure=primary_failure,
            ) from None
        calls.extend(answer.calls)
        if primary_failure is None:  # pragma: no cover - guarded by the primary return above
            raise RuntimeError("news_program_primary_failure_missing")
        return self._judgment(
            answer.result,
            calls,
            context_sha=context_sha,
            route="fallback",
            fallback_from=primary_failure.code,
            started=started,
        )

    async def _run_route(
        self,
        route: RouteName,
        lms: RouteLMs,
        *,
        context: TriageContext,
        context_sha: str,
        ledger: LMCallLedger,
    ) -> _RouteAnswer:
        start_index = len(ledger.receipts)
        route_started = time.monotonic()
        route_deadline = None if self.route_deadline_seconds is None else route_started + self.route_deadline_seconds
        lm_context = LMCallContext(
            program_version=PROGRAM_VERSION,
            program_sha256=self.artifact.program_sha256,
            context_sha256=context_sha,
            deadline_at_monotonic=route_deadline,
        )
        try:
            timeout = nullcontext() if route_deadline is None else asyncio.timeout_at(route_deadline)
            async with timeout:
                with ledger.scope(lm_context):
                    result = await self.program.acall(
                        context=context,
                        event_lm=lms.event_semantics,
                        card_lm=lms.reader_card,
                    )
                    if result.instruction_rejected is not None:
                        raise ValueError(result.instruction_rejected)
        except TimeoutError as exc:
            calls = self._route_calls(ledger, start_index, deadline=True)
            raise _RouteFailure(
                "news_program_route_deadline",
                retryable=True,
                output_failure=False,
                calls=calls,
                finish_reason=calls[-1].finish_reason if calls else None,
                failing_predictor=calls[-1].predictor if calls else "event_semantics",
            ) from exc
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError)):
                raise
            code = self._exception_code(exc)
            current = ledger.receipts[start_index:]
            if (
                not isinstance(exc, (dspy.LMError, LMOutputTruncatedError))
                and current
                and current[-1].terminal_disposition == "provider_success"
            ):
                with suppress(dspy.LMError):
                    ledger.domain_failure(code)
            calls = self._route_calls(ledger, start_index)
            failure = self._classify_failure(exc, code=code, calls=calls)
            raise failure from exc

        if self.route_deadline_seconds is not None and time.monotonic() - route_started > self.route_deadline_seconds:
            late = getattr(ledger, "late_completion", None)
            if callable(late):
                late("news_program_route_deadline")
            calls = self._route_calls(ledger, start_index, deadline=True)
            raise _RouteFailure(
                "news_program_route_deadline",
                retryable=True,
                output_failure=False,
                calls=calls,
                finish_reason=calls[-1].finish_reason if calls else None,
                failing_predictor=calls[-1].predictor if calls else "event_semantics",
            )

        successful_calls = list(self._route_calls(ledger, start_index))
        if len(successful_calls) > PROGRAM_ROUTE_MAX_CALLS:
            raise _RouteFailure(
                "news_program_route_call_budget_exhausted",
                retryable=False,
                output_failure=False,
                calls=tuple(successful_calls),
                failing_predictor=successful_calls[-1].predictor,
            )
        self._require_complete(result)
        semantics = result.semantics
        card = result.card
        if semantics is None or card is None:  # pragma: no cover - checked by _require_complete
            raise RuntimeError("news_program_native_result_incomplete")
        semantics_state = semantics.model_dump(mode="json")
        card_state = card.model_dump(mode="json")
        semantics_sha = canonical_sha(semantics_state)
        for index in range(len(successful_calls) - 1, -1, -1):
            call = successful_calls[index]
            if call.predictor == "reader_card" and call.terminal_disposition == "provider_success":
                successful_calls[index] = call.model_copy(
                    update={
                        "upstream_sha256": semantics_sha,
                        "output_sha256": canonical_sha(card_state),
                        "validated_output": card_state,
                    }
                )
                break
        for index in range(len(successful_calls) - 1, -1, -1):
            call = successful_calls[index]
            if call.predictor == "event_semantics" and call.terminal_disposition == "provider_success":
                successful_calls[index] = call.model_copy(
                    update={
                        "output_sha256": semantics_sha,
                        "validated_output": semantics_state,
                        "normalizations": result.normalizations,
                    }
                )
                break
        return _RouteAnswer(result=result, calls=tuple(successful_calls))

    @staticmethod
    def _route_calls(
        ledger: LMCallLedger,
        start_index: int,
        *,
        deadline: bool = False,
    ) -> tuple[ProgramCallTrace, ...]:
        calls = [receipt.to_program_call_trace() for receipt in ledger.receipts[start_index:]]
        if deadline and calls:
            calls[-1] = calls[-1].model_copy(update={"error_code": "news_program_route_deadline"})
        return tuple(calls)

    @staticmethod
    def _exception_code(exc: BaseException) -> str:
        if isinstance(exc, LMOutputTruncatedError):
            return "news_program_output_truncated"
        if isinstance(exc, dspy.LMError):
            raw = str(getattr(exc, "code", "") or "lm_error")
            return raw if raw.startswith("news_program_") else f"news_program_lm_{raw}"
        text = str(exc)
        if text.startswith("news_program_") or text == "primary_circuit_open":
            return text
        return f"news_program_{type(exc).__name__.casefold()}"

    @staticmethod
    def _classify_failure(
        exc: BaseException,
        *,
        code: str,
        calls: tuple[ProgramCallTrace, ...],
    ) -> _RouteFailure:
        latest = calls[-1] if calls else None
        terminal = latest.terminal_disposition if latest is not None else None
        output_failure = code != "news_program_route_deadline" and (
            isinstance(exc, LMOutputTruncatedError)
            or terminal
            in {
                "adapter_parse_error",
                "domain_validation_error",
            }
        )
        return _RouteFailure(
            code,
            retryable=isinstance(exc, _RETRYABLE_LM_ERRORS),
            output_failure=output_failure,
            calls=calls,
            finish_reason=latest.finish_reason if latest is not None else None,
            failing_predictor=latest.predictor if latest is not None else None,
        )

    @staticmethod
    def _require_complete(result: NativeProgramResult) -> None:
        if (
            result.instruction_rejected is not None
            or result.semantics is None
            or result.card is None
            or result.verdict is None
            or result.editorial is None
        ):
            raise ValueError(result.instruction_rejected or "news_program_native_result_incomplete")

    def _record_primary_failure(self) -> None:
        self._primary_failures += 1
        if self._primary_failures >= PROGRAM_PRIMARY_BREAKER_FAILURES:
            self._primary_failures = 0
            self._primary_open_until = time.monotonic() + PROGRAM_PRIMARY_BREAKER_OPEN_SECONDS

    def _judgment(
        self,
        result: NativeProgramResult,
        calls: list[ProgramCallTrace],
        *,
        context_sha: str,
        route: RouteName,
        fallback_from: str | None,
        started: float,
    ) -> SemanticJudgment:
        self._require_complete(result)
        semantics = result.semantics
        card = result.card
        verdict = result.verdict
        editorial = result.editorial
        if semantics is None or card is None or verdict is None or editorial is None:  # pragma: no cover
            raise RuntimeError("news_program_native_result_incomplete")
        answering_model = next(
            (
                call.model
                for call in reversed(calls)
                if call.route == route
                and call.predictor == "reader_card"
                and call.terminal_disposition == "provider_success"
            ),
            None,
        )
        trace = ProgramTrace(
            program_version=PROGRAM_VERSION,
            program_sha256=self.artifact.program_sha256,
            context_sha256=context_sha,
            envelope_sha256=EXECUTION_ENVELOPE_SHA256,
            event_semantics_sha256=canonical_sha(semantics.model_dump(mode="json")),
            reader_card_sha256=canonical_sha(card.model_dump(mode="json")),
            verdict_sha256=canonical_sha(verdict.model_dump(mode="json")),
            editorial_sha256=editorial.editorial_sha256,
            answering_route=route,
            fallback_from=fallback_from,
            calls=tuple(calls),
        )
        return SemanticJudgment(
            verdict=verdict,
            editorial=editorial,
            program_version=PROGRAM_VERSION,
            program_sha256=self.artifact.program_sha256,
            trace=trace,
            usage=ProgramUsage(
                wall_latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
                **aggregate_program_usage(calls),
            ),
            answering_model=answering_model,
            fallback_from=fallback_from,
        )

    def _public_error(
        self,
        failure: _RouteFailure,
        calls: list[ProgramCallTrace],
        *,
        context_sha: str,
        primary_failure: _RouteFailure | None = None,
    ) -> SemanticJudgeError:
        trace = ProgramTrace(
            program_version=PROGRAM_VERSION,
            program_sha256=self.artifact.program_sha256,
            context_sha256=context_sha,
            envelope_sha256=EXECUTION_ENVELOPE_SHA256,
            fallback_from=primary_failure.code if primary_failure is not None else None,
            calls=tuple(calls),
        )
        return SemanticJudgeError(
            failure.code,
            retryable=failure.retryable,
            output_failure=failure.output_failure or bool(primary_failure and primary_failure.output_failure),
            attempts=len(calls),
            partial_trace=trace,
            finish_reason=failure.finish_reason,
            failing_predictor=failure.failing_predictor,
            primary_code=primary_failure.code if primary_failure is not None else None,
        )


__all__ = ["RouteLMs", "RoutedSemanticJudge"]
