"""One typed DSPy Predict over a frozen CaseView; no runtime tools or plan selection."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
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


class TradingAssessor:
    def __init__(self, *, program: dspy.Predict, lm: GenerativeLM, timeout_s: float, concurrent: int) -> None:
        if timeout_s <= 0 or concurrent <= 0 or program.lm is not None:
            raise ValueError("trading_assessor_config_invalid")
        self.program = program
        self.lm = lm
        self.timeout_s = timeout_s
        self._slots = asyncio.Semaphore(concurrent)

    async def assess(self, view: CaseView) -> AssessmentResult:
        deadline = time.monotonic() + self.timeout_s
        async with self._slots:
            for attempt in range(2):
                try:
                    with dspy.context(adapter=dspy.JSONAdapter(), track_usage=True):
                        prediction = await asyncio.wait_for(
                            self.program.acall(lm=self.lm, case_view_json=view.prompt_json()),
                            timeout=max(0.1, deadline - time.monotonic()),
                        )
                    forecast, notes = _from_output(prediction.assessment, view)
                    return AssessmentResult(forecast, "complete", None, notes, prediction.get_lm_usage() or {})
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    code = _error_code(exc)
                    if (
                        attempt == 0
                        and code in ("timeout", "rate_limit", "provider")
                        and deadline - time.monotonic() > 5
                    ):
                        continue
                    return AssessmentResult(None, "failed", code, (), {})
        raise AssertionError("assessor_unreachable")


__all__ = [
    "AssessmentResult",
    "ForecastOutput",
    "ForecastSignature",
    "TradingAssessor",
    "load_program",
    "program_sha256",
]
