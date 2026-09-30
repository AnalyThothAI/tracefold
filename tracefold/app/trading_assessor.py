"""One typed DSPy Predict over a frozen CaseView; no runtime tools or plan selection."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tracefold.app.learning_runtime import GenerativeLM
from tracefold.trading.engine.case_view import CaseView
from tracefold.trading.engine.forecast import Driver, Forecast, LegProbabilities


class _Probabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    p_tp: Decimal = Field(ge=0, le=1)
    p_sl: Decimal = Field(ge=0, le=1)
    p_timeout: Decimal = Field(ge=0, le=1)


class _Driver(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(pattern=r"^e[1-9][0-9]*$")
    leans: Literal["long", "short", "neither"]
    note: str = Field(min_length=1, max_length=160)


class ForecastOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    drivers: list[_Driver] = Field(default_factory=list, max_length=12)
    long: _Probabilities
    short: _Probabilities


class ForecastSignature(dspy.Signature):
    """Estimate TP, SL, and timeout chances for both frozen paper legs; never choose an action."""

    case_view_json: str = dspy.InputField(desc="Frozen LIVE facts e1...eN, leg geometry, and PIT base rates")
    assessment: ForecastOutput = dspy.OutputField(desc="Drivers with evidence aliases and three outcomes per side")


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    forecast: Forecast | None
    status: Literal["complete", "failed"]
    error_code: str | None
    notes: tuple[str, ...]
    usage: dict[str, Any]
    error_metadata: dict[str, Any] = field(default_factory=dict)


def program_sha256(path: Path) -> str:
    if path.suffix != ".json":
        raise ValueError("trading_program_json_required")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_program(path: Path, expected_sha256: str) -> dspy.Predict:
    if not expected_sha256 or program_sha256(path) != expected_sha256:
        raise ValueError("trading_program_sha_mismatch")
    program = dspy.Predict(ForecastSignature)
    program.load(str(path))
    if program.lm is not None:
        raise ValueError("trading_program_embedded_lm_forbidden")
    return program


def _from_output(value: object, view: CaseView) -> tuple[Forecast, tuple[str, ...]]:
    wire = value if isinstance(value, ForecastOutput) else ForecastOutput.model_validate(value)

    def probabilities(item: _Probabilities) -> LegProbabilities:
        return LegProbabilities(item.p_tp, item.p_sl, item.p_timeout).normalized()

    known = {str(item["ref"]) for item in view.model_input["facts"]}
    notes: list[str] = []
    drivers = []
    for item in wire.drivers:
        if item.ref not in known:
            notes.append(f"unknown_evidence_alias:{item.ref}")
            continue
        drivers.append(Driver(item.ref, item.leans, item.note))
    return Forecast(probabilities(wire.long), probabilities(wire.short), tuple(drivers)), tuple(notes)


def _error_code(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status == 429:
        return "rate_limit"
    provider_code = getattr(exc, "code", None)
    if provider_code in ("timeout", "rate_limit", "truncated", "parse", "schema"):
        return str(provider_code)
    if isinstance(exc, (ValidationError, ValueError)) or "schema" in name:
        return "schema"
    if isinstance(exc, TimeoutError) or "timeout" in name:
        return "timeout"
    if "rate" in name or "429" in name:
        return "rate_limit"
    if "truncat" in name or "length" in name:
        return "truncated"
    if "parse" in name or "adapter" in name or "json" in name:
        return "parse"
    return "provider"


def usage_totals(raw: dict[str, Any]) -> dict[str, int | None]:
    """DSPy nests token usage by LM; unknown observations remain unknown."""
    values = (
        [raw]
        if any(key in raw for key in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens"))
        else [value for value in raw.values() if isinstance(value, dict)]
    )
    result: dict[str, int | None] = {}
    for target, keys in (
        ("input_tokens", ("input_tokens", "prompt_tokens")),
        ("output_tokens", ("output_tokens", "completion_tokens")),
    ):
        counts = [value.get(keys[0], value.get(keys[1])) for value in values]
        observed = [count for count in counts if isinstance(count, int) and count >= 0]
        result[target] = sum(observed) if observed else None
    return result


def _failure_metadata(exc: BaseException) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    result: dict[str, Any] = {"error_type": type(exc).__name__}
    if isinstance(status, int):
        result["http_status"] = status
    headers = getattr(response, "headers", {}) or {}
    request_id = getattr(exc, "request_id", None) or headers.get("x-request-id")
    if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
        result["provider_request_ref"] = request_id
    retry = getattr(exc, "retry_after", None)
    if retry is None:
        retry = headers.get("retry-after")
    try:
        if retry is not None and math.isfinite(float(retry)):
            result["retry_after_seconds"] = max(0, float(retry))
    except (TypeError, ValueError):
        pass
    return result


class TradingAssessor:
    def __init__(self, *, program: dspy.Predict, lm: GenerativeLM, timeout_s: float, concurrent: int) -> None:
        if timeout_s <= 0 or concurrent <= 0 or program.lm is not None:
            raise ValueError("trading_assessor_config_invalid")
        self.program = program
        self.lm = lm
        self.timeout_s = timeout_s
        self._slots = asyncio.Semaphore(concurrent)
        self.capacity = concurrent

    async def assess(self, view: CaseView) -> AssessmentResult:
        queued = time.monotonic()
        async with self._slots:
            started = time.monotonic()
            deadline = started + self.timeout_s
            metadata: dict[str, Any] = {"queue_ms": round((started - queued) * 1000)}
            errors: list[dict[str, Any]] = []
            for attempt in range(2):
                try:
                    with dspy.context(adapter=dspy.JSONAdapter(), track_usage=True):
                        prediction = await asyncio.wait_for(
                            self.program.acall(lm=self.lm, case_view_json=view.prompt_json()),
                            timeout=max(0.1, deadline - time.monotonic()),
                        )
                    forecast, notes = _from_output(prediction.assessment, view)
                    metadata.update(
                        attempts=attempt + 1, provider_ms=round((time.monotonic() - started) * 1000), errors=errors
                    )
                    return AssessmentResult(
                        forecast, "complete", None, notes, usage_totals(prediction.get_lm_usage() or {}), metadata
                    )
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    code = _error_code(exc)
                    detail = _failure_metadata(exc)
                    detail.update(code=code, attempt=attempt + 1)
                    errors.append(detail)
                    delay = max(1.0, float(detail.get("retry_after_seconds", 1.0)))
                    if (
                        attempt == 0
                        and code in ("timeout", "rate_limit", "provider")
                        and delay + 5 < deadline - time.monotonic()
                    ):
                        await asyncio.sleep(delay)
                        continue
                    metadata.update(
                        attempts=attempt + 1, provider_ms=round((time.monotonic() - started) * 1000), errors=errors
                    )
                    return AssessmentResult(None, "failed", code, (), {}, metadata)
        raise AssertionError("assessor_unreachable")


__all__ = [
    "AssessmentResult",
    "ForecastOutput",
    "ForecastSignature",
    "TradingAssessor",
    "load_program",
    "program_sha256",
]
